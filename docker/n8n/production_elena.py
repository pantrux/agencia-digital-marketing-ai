"""Evalúa el máster codificado y registra el dictamen audiovisual de Elena."""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
import unicodedata
import uuid
from array import array
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from difflib import SequenceMatcher
from heapq import nlargest
from pathlib import Path

from production_handoff import (
    RENDER_SIZES,
    canonical,
    eligible,
    is_test_order,
    load_profile,
    publish,
    qa_rounds,
    read_regular,
    render_plan,
    revision_from_stem,
    validate,
)
from production_inbox import MEDIA, UUID, matches, render_resolution
from production_intake import identity
from production_mateo import ORDER_ERRORS, dispatch_request, lookup
from production_sofia import INTAKE_LIMIT, JOURNAL, LIMIT, MAX_TRANSCRIPT, MCP, validate_transcript
from production_valeria import (
    AD_MUSIC_ID,
    MAX_JPEG,
    MAX_VISUAL_BYTES,
    VisualResponseFormatError,
    run_visual,
    sample_frames,
    visual_payload,
    visual_request,
)

AD_MUSIC_CREDIT = (
    "Carefree — Kevin MacLeod (incompetech.com), CC BY 4.0. Fragmento editado y mezclado. "
    "https://creativecommons.org/licenses/by/4.0/"
)
BAND_SAMPLE_BYTES = 12000
BAND_COMPARISON_BYTES = 22000
RPC_ATTACHMENT_LIMIT = 8

VALERIA = Path("/var/lib/agency-production-valeria")
LUCAS = Path("/var/lib/agency-production-lucas")
FIELDS = {"puntaje", "marca_free", "observaciones", "defectos"}
AUDIT_ERRORS = (*ORDER_ERRORS, IndexError, StopIteration, subprocess.SubprocessError)


def paired_band_samples(originals, masters):
    """Muestra el original completo y el máster con la misma escala y posición de la banda."""
    # La banda del puente mide 264 px en 1080p y 528 px en 4K.
    fit = "force_original_aspect_ratio=decrease:force_divisible_by=2"
    filters = (
        f"[0:v]scale=360:552:{fit},"
        "pad=360:640:trunc((ow-iw)/4)*2:trunc((oh-ih+88)/4)*2:color=0x141a15,setsar=1[original];"
        f"[1:v]scale=360:640:{fit},pad=360:640:(ow-iw)/2:(oh-ih)/2:color=0x141a15,setsar=1[master];"
        "[original][master]hstack=inputs=2"
    )
    with tempfile.TemporaryDirectory(prefix="studio-qa-encuadre-") as directory:
        original, master = (Path(directory) / name for name in ("original.jpg", "master.jpg"))
        pairs = []
        for (stamp, original_bytes), (_, master_bytes) in zip(originals, masters, strict=True):
            original.write_bytes(original_bytes)
            master.write_bytes(master_bytes)
            for quality in (8, 16, 24):
                image = subprocess.run(
                    [
                        "/usr/bin/ffmpeg",
                        "-v",
                        "error",
                        "-nostdin",
                        "-filter_complex_threads",
                        "1",
                        "-protocol_whitelist",
                        "file",
                        "-i",
                        str(original),
                        "-protocol_whitelist",
                        "file",
                        "-i",
                        str(master),
                        "-filter_complex",
                        filters,
                        "-frames:v",
                        "1",
                        "-threads",
                        "1",
                        "-c:v",
                        "mjpeg",
                        "-q:v",
                        str(quality),
                        "-f",
                        "image2pipe",
                        "pipe:1",
                    ],
                    stdin=subprocess.DEVNULL,
                    check=True,
                    capture_output=True,
                    timeout=10,
                ).stdout
                if (
                    4 <= len(image) <= BAND_COMPARISON_BYTES
                    and image.startswith(b"\xff\xd8")
                    and image.endswith(b"\xff\xd9")
                ):
                    pairs.append((stamp, image))
                    break
            else:
                raise ValueError("No se pudo preparar la comparación de encuadre")
    return pairs


def make_verdict(
    review, metrics, duration, *, plan="Free", resolution="1080p", samples=3, evidence_limit=8
):
    if (
        type(review) is not dict
        or review.keys() != FIELDS
        or type(review["puntaje"]) is not int
        or not 0 <= review["puntaje"] <= 100
        or type(review["marca_free"]) is not str
        or len(review["marca_free"]) > 100
        or type(review["observaciones"]) is not list
        or not 1 <= len(review["observaciones"]) <= evidence_limit
        or type(review["defectos"]) is not list
        or len(review["defectos"]) > evidence_limit
    ):
        raise ValueError("Dictamen visual inválido")
    for name, key in (("observaciones", "detalle"), ("defectos", "correccion")):
        for evidence in review[name]:
            if (
                type(evidence) is not dict
                or evidence.keys() != {"muestra", key}
                or type(evidence["muestra"]) is not int
                or not 1 <= evidence["muestra"] <= samples
                or type(evidence[key]) is not str
                or not 1 <= len(evidence[key].strip()) <= 600
            ):
                raise ValueError("Evidencia visual fuera de las muestras")
    failures = []
    if (
        metrics["width"],
        metrics["height"],
        metrics["fps"],
        metrics["codec"],
        metrics["color"],
    ) != (
        *RENDER_SIZES[resolution],
        30,
        "h264",
        "bt709",
    ) or not math.isclose(metrics["duration"], duration, abs_tol=0.1):
        failures.append(f"Formato del máster fuera del plan {plan}")
    if (
        not -15 <= metrics["lufs"] <= -13
        or not math.isfinite(metrics["peak"])
        or metrics["peak"] > -1
    ):
        failures.append("Audio fuera del intervalo de sonoridad o pico verdadero")
    normalized = re.sub(r"[\W_]+", " ", review["marca_free"].casefold()).strip()
    if plan == "Free" and normalized != "creado con agencia ai free":
        failures.append("Marca de agua Free sin confirmar")
    if plan != "Free" and normalized:
        failures.append(f"Marca de agua Free detectada en el plan {plan}")
    if review["defectos"]:
        failures.append("Elena detectó defectos que requieren corrección")
    return {
        **review,
        "reglas_duras_incumplidas": failures,
        "decision": "aprobado" if review["puntaje"] >= 85 and not failures else "rechazado",
    }


def matching_music_window(actual, reference):
    if len(actual) < 3800 or len(reference) < len(actual):
        return False

    def power(samples):
        return sum(sample * sample for sample in samples)

    def similarity(a, b, a_power):
        norm = math.sqrt(a_power * power(b))
        return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0

    # Preselecciona posiciones con una muestra dispersa y verifica la forma de onda completa.
    sparse = actual[::128]
    sparse_power = power(sparse)
    length = len(actual)
    offsets = nlargest(
        32,
        range(len(reference) - length + 1),
        key=lambda start: similarity(sparse, reference[start : start + length : 128], sparse_power),
    )
    actual_power = power(actual)
    return any(
        similarity(actual, reference[start : start + length], actual_power) > 0.8
        for start in offsets
    )


def close_music_matches(ff, reference):
    pcm = ["-vn", "-ac", "1", "-ar", "2000", "-f", "s16le", "-"]
    actual = array(
        "h",
        ff(
            "ffmpeg", ["-v", "error", "-nostdin"], ["-af", "atrim=start=26.5:end=28.5", *pcm], 30
        ).stdout,
    )
    original = array(
        "h",
        subprocess.run(
            ["/usr/bin/ffmpeg", "-v", "error", "-nostdin", "-i", str(reference), "-t", "240", *pcm],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout,
    )
    return matching_music_window(actual, original)


def decode(
    path,
    expected,
    duration,
    *,
    stamps=None,
    speech=None,
    music_reference=None,
    sample_bytes=None,
    sample_width=None,
):
    if path.resolve(strict=True) != path:
        raise ValueError("Ruta del máster inválida")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0:
            raise ValueError("El máster debe ser un archivo regular")
        if hashlib.file_digest(file, "sha256").hexdigest() != expected:
            raise ValueError("La huella del máster cambió")
        args = ["-protocol_whitelist", "file", "-f", "mov", "-i", f"/proc/self/fd/{fd}"]

        def ff(bin_name, before, after, timeout):
            file.seek(0)
            return subprocess.run(
                ["/usr/bin/" + bin_name, *before, *args, *after],
                pass_fds=(fd,),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=timeout,
            )

        probe = json.loads(
            ff(
                "ffprobe", ["-v", "error"], ["-show_streams", "-show_format", "-of", "json"], 20
            ).stdout
        )
        video = next(x for x in probe["streams"] if x["codec_type"] == "video")
        rate, div = map(int, video["avg_frame_rate"].split("/"))
        log = ff(
            "ffmpeg",
            ["-hide_banner", "-nostdin"],
            ["-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
            75,
        )
        summary = log.stderr.decode(errors="replace").rsplit("Summary:", 1)[1]
        loudness = re.search(r"I:\s*(-?\d+(?:\.\d+)?) LUFS", summary)
        peak = re.search(r"Peak:\s*(-?\d+(?:\.\d+)?) dBFS", summary)
        if not loudness or not peak or not div:
            raise ValueError("Medición final de audio o fps indisponible")
        metrics = {
            "width": video["width"],
            "height": video["height"],
            "codec": video["codec_name"],
            "color": video["color_space"],
            "fps": rate / div,
            "duration": float(probe["format"]["duration"]),
            "lufs": float(loudness[1]),
            "peak": float(peak[1]),
        }
        if speech is not None:
            bed = ff(
                "ffmpeg",
                ["-hide_banner", "-nostdin"],
                ["-vn", "-af", "atrim=start=26.5:end=28.5,volumedetect", "-f", "null", "-"],
                30,
            ).stderr.decode(errors="replace")
            volume = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", bed)
            metrics["musica_cierre_dbfs"] = float(volume[1]) if volume else None
            metrics["musica_catalogo"] = (
                close_music_matches(ff, music_reference)
                if music_reference is not None
                else probe["format"].get("tags", {}).get("comment") == AD_MUSIC_ID
            )
            ff(
                "ffmpeg",
                ["-v", "error", "-nostdin", "-n"],
                [
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-t",
                    "30",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(speech),
                ],
                40,
            )
        images = []
        checkpoints = stamps or [
            round(min(duration, metrics["duration"]) * fraction, 5) for fraction in (0.2, 0.5, 0.8)
        ]
        limit = sample_bytes or (min(MAX_JPEG, 63000 // len(checkpoints)) if stamps else MAX_JPEG)
        sizes = (
            tuple((sample_width, quality) for quality in (8, 12, 18, 24, 31))
            if sample_width
            else ((360, 5), (360, 8), (288, 10), (192, 16), (144, 22))
        )
        for timestamp in checkpoints:
            for width, quality in sizes:
                image = ff(
                    "ffmpeg",
                    ["-v", "error", "-nostdin", "-ss", str(timestamp)],
                    [
                        "-map",
                        "0:v:0",
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale={width}:-2",
                        "-threads",
                        "1",
                        "-c:v",
                        "mjpeg",
                        "-q:v",
                        str(quality),
                        "-f",
                        "image2pipe",
                        "pipe:1",
                    ],
                    10,
                ).stdout
                if len(image) <= limit:
                    break
            if (
                not 4 <= len(image) <= (sample_bytes or MAX_JPEG)
                or not image.startswith(b"\xff\xd8")
                or not image.endswith(b"\xff\xd9")
            ):
                raise ValueError("Imagen de auditoría inválida")
            images.append((timestamp, image))
        if identity(info) != identity(os.fstat(fd)) or identity(info) != identity(
            path.stat(follow_symlinks=False)
        ):
            raise ValueError("El máster cambió durante la auditoría")
    return metrics, images


MCP_MIX_TRANSCRIPT = """
import json,sys
from pathlib import Path
root = Path(sys.executable).parent.parent.parent
sys.path.insert(0, str(root / "mcp_servers"))
from call_stdio import runtime_settings
from speaches_client import transcribe_audio
token = Path(runtime_settings()["GPU_BRIDGE_TOKEN_FILE"])
reply = transcribe_audio(Path(sys.argv[1]), token, language="es", total_seconds=70)
result = {field: reply[field] for field in ("language", "duration", "text")}
result["words"] = [{field: word[field] for field in ("word", "start", "end")} for word in reply["words"]]
print(json.dumps(result))
"""


def verify_mix_speech(speech, captions):
    reply = subprocess.run(
        [str(MCP / "venv/bin/python"), "-I", "-B", "-c", MCP_MIX_TRANSCRIPT, str(speech)],
        capture_output=True,
        check=True,
        timeout=82,
    )
    if not 0 < len(reply.stdout) <= MAX_TRANSCRIPT:
        raise ValueError("El resultado de voz del anuncio es inválido")
    transcript = validate_transcript(json.loads(reply.stdout))
    return compare_mix_speech(captions, transcript["words"])


def compare_mix_speech(captions, words):
    def tokens(text):
        clean = unicodedata.normalize("NFKD", text.casefold())
        clean = "".join(letter for letter in clean if not unicodedata.combining(letter))
        return re.findall(r"[^\W_]+", clean, re.UNICODE)

    expected = [
        (part, cue["start_frame"] / 30, cue["end_frame"] / 30)
        for cue in captions
        for part in tokens(cue["text"])
    ]
    actual = [
        (part, (word["start"] + word["end"]) / 2) for word in words for part in tokens(word["word"])
    ]
    aligned = 0
    matcher = SequenceMatcher(
        None, [item[0] for item in expected], [item[0] for item in actual], autojunk=False
    )
    for left, right, count in matcher.get_matching_blocks():
        for offset in range(count):
            _, begin, end = expected[left + offset]
            if begin - 0.8 <= actual[right + offset][1] <= end + 0.8:
                aligned += 1
    coverage = aligned / len(expected) if expected else 0
    return {
        "palabras_esperadas": len(expected),
        "palabras_alineadas": aligned,
        "cobertura": round(coverage, 3),
        "aprobado": coverage >= 0.72,
    }


def scene_checks(scenes, *, required=()):
    intervals, choices, rest = [], {0, *required}, []
    offset = 0
    for scene in scenes:
        length = scene["source_end_frame"] - scene["source_start_frame"]
        if label := scene.get("label"):
            start, end = offset + label["start_frame"], offset + label["end_frame"]
            intervals.append((start, end, label))
            choices.add((start + end) // 2)
            if end < offset + length:
                rest.append(end)
        else:
            choices.add(offset + length // 2)
        offset += length
    choices.add(offset - 1)
    for frame in rest:
        if len(choices) < 8:
            choices.add(frame)
    return [
        {
            "segundo": (frame * 1000 // 30) / 1000,
            "rotulo_esperado": next(
                (label for start, end, label in intervals if start <= frame < end), None
            ),
        }
        for frame in sorted(choices)
    ]


def visual_review(request):
    try:
        return run_visual(request)
    except VisualResponseFormatError:
        # Un ID distinto evita reutilizar la respuesta anterior del Gateway.
        corrected = {
            **request,
            "idempotencyKey": str(
                uuid.uuid5(uuid.UUID(request["idempotencyKey"]), "elena:corregir-formato:v1")
            ),
            "message": (
                "Emite el dictamen como un único objeto JSON completo, sin Markdown ni comentarios. "
                + request["message"]
            ),
        }
        return run_visual(corrected)


def scene_review_batches(instructions, evidence, images, request_id, verdict_options):
    """Audita todos los fotogramas a resolución fija dentro del contrato del cliente RPC."""

    def request_for(first, last):
        doc = {
            **evidence,
            "segundos_de_muestras": evidence["segundos_de_muestras"][first:last],
            "rotulos_por_muestra": evidence["rotulos_por_muestra"][first:last],
            "muestras_del_montaje": list(range(first + 1, last + 1)),
        }
        prompt = (
            instructions + f" Esta solicitud abarca solo un lote de {last - first} imágenes. "
            f"Numera muestra del 1 al {last - first} según el orden local de los adjuntos. "
            "Usa los segundos y muestras_del_montaje para ubicar cada imagen en el vídeo. "
        )
        return visual_request(
            "agency-critic",
            prompt + "\n" + json.dumps(doc, ensure_ascii=False),
            images[first:last],
            str(uuid.uuid5(uuid.UUID(request_id), f"elena:lote:v1:{first}:{last}")),
        )

    batches = []
    first = 0
    while first < len(images):
        last = min(first + RPC_ATTACHMENT_LIMIT, len(images))
        request = request_for(first, last)
        while len(visual_payload(request)) > MAX_VISUAL_BYTES - 1024:
            last -= 1
            if last <= first:
                raise ValueError("Una muestra de auditoría excede el límite del cliente visual")
            request = request_for(first, last)
        batches.append((first, last, request))
        first = last

    def audit_batch(batch):
        first, last, request = batch
        review = visual_review(request)
        make_verdict(review, samples=last - first, **verdict_options)
        return review

    with ThreadPoolExecutor(max_workers=3) as workers:
        reviews = list(workers.map(audit_batch, batches))
    marks = [row["marca_free"] for row in reviews if row["marca_free"]]
    mark = next(iter(marks), "")
    if verdict_options["plan"] == "Free":
        mark = next(
            (
                item
                for item in marks
                if re.sub(r"[\W_]+", " ", item.casefold()).strip() == "creado con agencia ai free"
            ),
            mark,
        )
    merged = {
        "puntaje": min(row["puntaje"] for row in reviews),
        "marca_free": mark,
        **{
            key: [
                {**finding, "muestra": first + finding["muestra"]}
                for (first, _, _), review in zip(batches, reviews, strict=True)
                for finding in review[key]
            ]
            for key in ("observaciones", "defectos")
        },
    }
    verdict = make_verdict(
        merged, samples=len(images), evidence_limit=8 * len(batches), **verdict_options
    )
    mapping = [list(range(first + 1, last + 1)) for first, last, _ in batches]
    return verdict, mapping


def review_video(
    path,
    digest,
    props,
    angle,
    plan,
    resolution,
    request_id,
    *,
    synthetic=False,
    music_reference=None,
    source_order=None,
    objective=None,
):
    scenes = props.get("scenes")
    checks = scene_checks(scenes) if scenes else []
    ad = props.get("ad")
    destination = ad.get("destination") if ad else None
    animated_endcard = bool(ad and (destination or ad.get("engine") == "hyperframes"))
    if ad:
        cue = max(ad["captions"], key=lambda row: row["end_frame"] - row["start_frame"])
        frame = (cue["start_frame"] + cue["end_frame"]) // 2
        stamp = (frame * 1000 // 30) / 1000
        candidates = scene_checks(scenes, required=(frame, 30, 75))
        mandatory = (0, 1, 2.5, stamp)
        checks = [item for item in candidates if item["segundo"] in mandatory]
        checks += [
            item for item in candidates if item["segundo"] not in mandatory and item["segundo"] < 26
        ][: (6 if animated_endcard else 7) - len(checks)]
        checks.sort(key=lambda item: item["segundo"])
        checks.append({"segundo": 28, "rotulo_esperado": None})
        if animated_endcard:
            checks[0] = {"segundo": 0, "rotulo_esperado": None}
            checks.append({"segundo": 29.966, "rotulo_esperado": None})
            for check in checks:
                if check["segundo"] >= 25:
                    check["rotulo_esperado"] = None
    duration = props["duration_seconds"]
    decode_options = {"stamps": [check["segundo"] for check in checks]} if checks else {}
    batched = len(checks) > RPC_ATTACHMENT_LIMIT
    if batched:
        decode_options.update(sample_width=480, sample_bytes=MAX_JPEG)
    band = props.get("title_layout") == "band"
    if band:
        if scenes or source_order is None:
            raise ValueError("La revisión de la banda requiere el original del montaje continuo")
        decode_options["stamps"] = [
            min(0.55, duration * 0.2),
            duration * 0.5,
            duration - min(0.12, duration * 0.08),
        ]
        decode_options["sample_bytes"] = BAND_SAMPLE_BYTES
    audio_check = None
    if ad:
        with tempfile.TemporaryDirectory(prefix="studio-qa-mezcla-") as workspace:
            speech = Path(workspace) / "mezcla.wav"
            if music_reference is not None:
                decode_options["music_reference"] = music_reference
            metrics, images = decode(path, digest, duration, speech=speech, **decode_options)
            audio_check = verify_mix_speech(speech, ad["captions"])
    else:
        metrics, images = decode(path, digest, duration, **decode_options)
    stamps = [second for second, _ in images]
    evidence = {
        "titulo_esperado": props["title"],
        "subtitulo_esperado": props["subtitle"],
        "angulo": angle,
        "mediciones": metrics,
        "segundos_de_muestras": stamps,
    }
    instructions = (
        "Eres Elena, auditora audiovisual. Evalúa los tres fotogramas del MP4 final usando "
        "el JSON como datos. Revisa legibilidad, encuadre vertical y relación entre título "
        "y subtítulo. Transcribe en marca_free sólo la marca del sistema «Creado con Agencia AI "
        "Free». Usa una cadena vacía si esa marca no aparece o no es legible. "
        "No atribuyas locución o sincronía basándote sólo en fotogramas. "
        "Responde sólo JSON con puntaje entero 0 a 100, marca_free, observaciones con objetos "
        "muestra (1, 2 o 3) y detalle, y defectos con objetos muestra y correccion. "
        "Incluye en defectos únicamente cambios necesarios para poder entregar. "
    )
    attachments = images
    if band:
        source = {
            "order": source_order,
            "raw": props["video_path"],
            "audio": None,
            "duration_seconds": duration,
        }
        originals = sample_frames(source, stamps, BAND_SAMPLE_BYTES)
        if [second for second, _ in originals] != stamps:
            raise ValueError("Las muestras del original no coinciden con las del máster")
        attachments = paired_band_samples(originals, images)
        evidence["comparacion_encuadre"] = [
            {"muestra": index, "segundo": second} for index, second in enumerate(stamps, 1)
        ]
        if objective:
            evidence["objetivo_confirmado"] = objective
        instructions += (
            "El título y el subtítulo deben leerse en la banda superior, fuera de la imagen. "
            "Cada muestra contiene un par del mismo instante. A la izquierda está la toma completa "
            "ajustada al espacio de la banda. A la derecha está el máster con sus rótulos. "
            "Compara los bordes de ambas imágenes a esa misma escala. Para afirmar que la edición "
            "recortó, identifica qué contenido aparece en un borde de la izquierda y falta en el "
            "mismo borde de la derecha. Los márgenes de ajuste proporcional son parte del diseño. "
            "Describe en observaciones los cortes de encuadre que ya estaban en la grabación. "
            "Si un corte de origen impide cumplir el objetivo confirmado, regístralo como defecto "
            "y explica qué toma hace falta. Registra como defecto cualquier recorte introducido "
            "por la edición, banda sobre el rostro o producto, o texto cortado. "
            "Cita el número del par correspondiente al máster en el dictamen y evalúa su calidad final. "
        )
    if checks:
        evidence["rotulos_por_muestra"] = [check["rotulo_esperado"] for check in checks]
        instructions = (
            "Eres Elena, auditora audiovisual. Revisa las muestras suministradas en el orden indicado."
            + (" La primera es la apertura." if not batched else "")
            + (
                (
                    " Revisa el CTA en la penúltima muestra; la última enseña el fin del fundido."
                    if animated_endcard
                    else " La última es el cierre."
                )
                if not batched
                else ""
            )
            + " Usa los segundos y rótulos esperados del JSON. En un intervalo sin rótulo"
            " debe quedar despejado; los textos esperados sólo aparecen en sus tiempos."
            " Si un rótulo tapa ojos, rostro o producto principal, decláralo como defecto"
            " y pide una corrección concreta de posición, texto o duración."
            " Revisa contraste y legibilidad en móvil. Si el texto se lee completo, una mejora"
            " de tamaño o estilo va en observaciones. Usa defectos para problemas que impiden"
            " publicar, como texto ilegible, recortado, en un tiempo incorrecto o sobre el sujeto."
            " No infieras locución desde imágenes."
            " Transcribe en marca_free sólo la marca «Creado con Agencia AI Free» si es visible."
            " Usa cadena vacía en otro caso. Devuelve únicamente JSON con puntaje entero de 0 a 100,"
            " marca_free, observaciones (objetos con muestra y detalle) y defectos"
            " (objetos con muestra y correccion)."
            + (f" muestra es un entero de 1 a {len(images)}." if not batched else "")
        )
    if ad:
        for sample in checks:
            frame = round(sample["segundo"] * 30)
            sample["subtitulo_esperado"] = next(
                (
                    cue["text"]
                    for cue in ad["captions"]
                    if cue["start_frame"] <= frame < cue["end_frame"]
                ),
                None,
            )
        evidence["muestras_anuncio"] = checks
        evidence["auditoria_voz"] = audio_check
        evidence["criterio_musical"] = (
            "Fuente de catálogo y medición del cierre del MP4; la escucha es una revisión aparte."
        )
        evidence["cta_final"] = ad["cta"]
        evidence["marca_final"] = ad["brand"]
        if animated_endcard:
            instructions += (
                " En el fotograma cero debe verse el sujeto del gancho, aunque su rótulo todavía esté entrando. "
                "A los 29.966 s se espera un fundido a negro, sin texto legible."
            )
        if destination:
            evidence["destino_confirmado"] = destination
            instructions += (
                " Comprueba en el cierre el icono del canal seleccionado y la URL del destino "
                " confirmado, visible a los 28 s. Se admite mostrarla sin https:// ni www. "
                "Rechaza un destino "
                "distinto, una URL ilegible o un icono de otro canal."
            )
        instructions += (
            " En este anuncio hay subtítulos separados del rótulo. Revisa el gancho al comienzo "
            "y al menos un subtítulo visible en móvil cuando coincida con la muestra. "
            "Contrasta el objeto prometido por el gancho con el fotograma inicial y el de un "
            "segundo: si sólo se ve parcialmente al comenzar o el encuadre tarda en "
            "mostrarlo, registra un defecto en apertura. Comprueba si la caja de subtítulos "
            "oculta el elemento destacado o si queda legible sin ocupar una franja vacía. "
            "En la placa de cierre se exige marca y CTA completos en lugar del rótulo de escena. "
            "Rechaza si el CTA falta, no coincide o invade una zona ilegible."
        )
    if synthetic:
        instructions += "Considera que el crudo es una demostración técnica con barras de color. "
    batches = []
    verdict_options = {
        "metrics": metrics,
        "duration": duration,
        "plan": plan,
        "resolution": resolution,
    }
    if batched:
        qa, batches = scene_review_batches(
            instructions, evidence, attachments, request_id, verdict_options
        )
    else:
        request = visual_request(
            "agency-critic",
            instructions + "\n" + json.dumps(evidence, ensure_ascii=False),
            attachments,
            request_id,
        )
        qa = make_verdict(visual_review(request), samples=len(images), **verdict_options)
    if audio_check is not None and not audio_check["aprobado"]:
        qa["decision"] = "rechazado"
        qa["reglas_duras_incumplidas"].append("La voz de la mezcla no coincide con los subtítulos")
        sample = next(i for i, check in enumerate(checks, 1) if check["subtitulo_esperado"])
        qa["defectos"].append(
            {
                "muestra": sample,
                "correccion": "Selecciona frases originales claras, conserva su voz y ajusta sus cortes "
                "para que los subtítulos puedan verificarse en el nuevo render.",
            }
        )
    if (
        ad
        and not -42
        <= (metrics["musica_cierre_dbfs"] if metrics["musica_cierre_dbfs"] is not None else -100)
        <= -10
    ):
        qa["decision"] = "rechazado"
        qa["reglas_duras_incumplidas"].append("Música ausente o con nivel incorrecto al cierre")
        qa["defectos"].append(
            {
                "muestra": len(checks),
                "correccion": "Comprueba la pista de catálogo y corrige el nivel audible durante la placa final.",
            }
        )
    if ad and not metrics["musica_catalogo"]:
        qa["decision"] = "rechazado"
        qa["reglas_duras_incumplidas"].append("La exportación no acredita la pista de catálogo")
        qa["defectos"].append(
            {
                "muestra": len(checks),
                "correccion": "Vuelve a exportar esta escaleta con la pista del catálogo aprobada.",
            }
        )
    return {
        "mediciones": metrics,
        **({"revision_voz": audio_check} if ad else {}),
        **({"creditos_musica": AD_MUSIC_CREDIT} if metrics.get("musica_catalogo") else {}),
        "dictamen": qa,
        "muestras": [{"segundo": s, "sha256": hashlib.sha256(p).hexdigest()} for s, p in images],
        **({"lotes_muestras": batches} if batches else {}),
        **(
            {
                "referencia_original": {
                    "sha256": source_order["media"]["sha256"],
                    "muestras": [
                        {"segundo": s, "sha256": hashlib.sha256(p).hexdigest()}
                        for s, p in originals
                    ],
                    "comparaciones": [
                        {"segundo": s, "sha256": hashlib.sha256(p).hexdigest()}
                        for s, p in attachments
                    ],
                }
            }
            if band
            else {}
        ),
    }


def audit_preview(order, edl, job, asset, out):
    path = MEDIA / asset["path"]
    props = edl["escaleta"]["render"]["props"]
    expected = {
        "order_id": order["order_id"],
        "job_id": job,
        "brief_sha256": edl["brief_sha256"],
        "direction_sha256": hashlib.sha256(canonical(edl)).hexdigest(),
        "master_sha256": asset["sha256"],
        "fase": "vista_previa",
        "rubrica": "reel-v1",
    }
    if out.exists():
        saved = json.loads(read_regular(out, LIMIT))
        if any(saved[key] != value for key, value in expected.items()):
            raise ValueError("La revisión previa pertenece a otro montaje")
        return saved
    request_key = hashlib.sha256(canonical(expected)).hexdigest()
    request_id = str(uuid.uuid5(uuid.UUID(order["order_id"]), "vista-previa:" + request_key))
    record = {
        **expected,
        **review_video(
            path,
            asset["sha256"],
            props,
            edl["escaleta"]["angulo"],
            render_plan(order),
            "1080p",
            request_id,
        ),
        "modelo": "openai/gpt-6.1-sol",
        "runtime": "openclaw",
        "ejecucion": "modelRun",
    }
    publish(out, canonical(record) + b"\n")
    return record


def audit(
    order,
    edl,
    master_hash,
    request_id,
    out,
    rendered=None,
    revision=1,
    *,
    synthetic=False,
    revision_id=None,
    objective=None,
):
    decision = edl["escaleta"]
    render = decision["render"]
    job = order["media"]
    plan = render_plan(order)
    resolution = render_resolution(order)
    if revision_id is not None and not matches(UUID, revision_id):
        raise ValueError("Identidad de revisión inválida")
    revised_job = f"{order['order_id']}-v{revision}" if revision_id is None else f"{revision_id}-r"
    if (
        edl["order_id"] != order["order_id"]
        or edl["source_sha256"] != job["sha256"]
        or decision["estado"] != "lista_para_stills"
        or render["project_id"] != job["project_slug"]
        or render["job_id"] != order["order_id"] + "-v1"
        or (render["plan"], render["format"], render["resolution"]) != (plan, "mp4", resolution)
    ):
        raise ValueError("La escaleta no corresponde al máster de prueba")
    directory = (
        MEDIA
        / "proyectos"
        / job["project_slug"]
        / "06_renders"
        / (render["job_id"] if rendered is None else revised_job)
        / "render"
    )
    master = directory / "master.mp4"
    if rendered is not None:
        asset = rendered["master"]
        direction_hash = hashlib.sha256(canonical(edl)).hexdigest()
        if (
            ("direction_sha256" in rendered and rendered["direction_sha256"] != direction_hash)
            or rendered.get("resolution", "1080p") != resolution
            or (
                rendered["order_id"],
                rendered["source_sha256"],
                rendered["brief_sha256"],
                rendered["job_id"],
                asset["sha256"],
            )
            != (
                order["order_id"],
                job["sha256"],
                edl["brief_sha256"],
                revised_job,
                master_hash,
            )
        ):
            raise ValueError("Recibo de Lucas ajeno al máster")
        master = MEDIA / asset["path"]
        if master.parent != directory or master.name not in {"master.mp4", "master-corrected.mp4"}:
            raise ValueError("Ruta del máster corregido inválida")
    props = render["props"]
    record = {
        "order_id": order["order_id"],
        "job_id": directory.parent.name,
        "master_sha256": master_hash,
        "brief_sha256": edl["brief_sha256"],
        "direction_sha256": hashlib.sha256(canonical(edl)).hexdigest(),
        **review_video(
            master,
            master_hash,
            props,
            decision["angulo"],
            plan,
            resolution,
            request_id,
            synthetic=synthetic,
            source_order=order,
            objective=objective,
        ),
        "modelo": "openai/gpt-6.1-sol",
        "runtime": "openclaw",
        "ejecucion": "modelRun",
    }
    publish(out, canonical(record) + b"\n")
    return record


def report_rejection(order, brief, rendered, review, dispatch, revision):
    job = f"{order['order_id']}-v{revision}"
    master_hash = (rendered.get("master") or rendered["preview"])["sha256"]
    brief_hash = hashlib.sha256(canonical(brief)).hexdigest()
    if (review["order_id"], review["job_id"], review["master_sha256"], review["brief_sha256"]) != (
        order["order_id"],
        job,
        master_hash,
        brief_hash,
    ) or (
        rendered["order_id"],
        rendered["job_id"],
        rendered["brief_sha256"],
        rendered["source_sha256"],
    ) != (order["order_id"], job, brief_hash, order["media"]["sha256"]):
        raise ValueError("El rechazo no corresponde a la revisión vigente")
    result = dispatch_request(
        {
            "action": "quality_rejected",
            "order_id": order["order_id"],
            "user_id": order["user_id"],
            "upload_id": order["upload_id"],
            "source_sha256": order["media"]["sha256"],
            "master_sha256": master_hash,
            "revision": revision,
            "brief_confirmed_at": brief["confirmed_at"],
        },
        dispatch,
    )
    if result != {"recorded": True}:
        raise ValueError("Studio no confirmó el rechazo de calidad")


def audit_pending(path, version, profile, dispatch, output):
    identity, revision = version
    intake = JOURNAL / f"{identity}.json"
    direction = VALERIA / path.name
    target = output / path.name
    if not direction.is_file() or not intake.is_file():
        return False
    order = json.loads(read_regular(intake, INTAKE_LIMIT))["order"]
    validate(order, identity)
    synthetic = is_test_order(order, profile)
    if not eligible(order, profile) or revision > qa_rounds(order, profile):
        return False
    terminal = not synthetic and revision == qa_rounds(order, profile)
    review = None
    if target.exists():
        if not terminal:
            return False
        review = json.loads(read_regular(target, LIMIT))
        if review["dictamen"]["decision"] != "rechazado":
            return False
    if not (brief := lookup(order, dispatch)):
        return False
    edl = json.loads(read_regular(direction, LIMIT))
    rendered = json.loads(read_regular(path, LIMIT))
    if edl["brief_sha256"] != hashlib.sha256(canonical(brief)).hexdigest():
        raise ValueError("El máster no corresponde al brief vigente")
    asset = rendered.get("master") or rendered.get("preview")
    if not isinstance(asset, dict):
        raise ValueError("Lucas no acreditó un artefacto revisable")
    digest = asset["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Huella de máster inválida")
    if review is None and "master" not in rendered:
        raw = read_regular(path.with_suffix(".preview-review.receipt"), LIMIT)
        review = json.loads(raw)
        if (
            order.get("output_edit") != "reel"
            or hashlib.sha256(raw).hexdigest() != rendered["preview_review_sha256"]
            or review["fase"] != "vista_previa"
            or review["dictamen"]["decision"] != "rechazado"
            or (
                review["order_id"],
                review["job_id"],
                review["brief_sha256"],
                review["master_sha256"],
            )
            != (identity, f"{identity}-v{revision}", edl["brief_sha256"], digest)
            or review["direction_sha256"] != hashlib.sha256(canonical(edl)).hexdigest()
        ):
            raise ValueError("La vista previa rechazada no corresponde a esta revisión")
        publish(target, raw)
    elif review is None:
        request_id = str(uuid.uuid5(uuid.UUID(identity), f"elena:v{revision}:" + digest))
        review = audit(
            order,
            edl,
            digest,
            request_id,
            target,
            rendered,
            revision,
            synthetic=synthetic,
            objective=brief["objective"],
        )
    if terminal and review["dictamen"]["decision"] == "rechazado":
        report_rejection(order, brief, rendered, review, dispatch, revision)
    return True


def run_pending(profile, dispatch, output):
    # El directorio de reintentos mantiene los cambios de SQLite fuera de los dictámenes.
    state = (output / "reintentos").absolute()
    state.mkdir(mode=0o700, exist_ok=True)
    database = state / "elena.sqlite3"
    if state.resolve(strict=True) != state or database.is_symlink():
        raise ValueError("Directorio de reintentos de auditoría inválido")
    deferred_error = None
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS retries (
                revision_id TEXT PRIMARY KEY, receipt TEXT NOT NULL,
                failures INTEGER NOT NULL, due_at INTEGER NOT NULL
            ) STRICT"""
        )
        for path in sorted(LUCAS.glob("*.json")):
            if not (version := revision_from_stem(path.stem)):
                continue
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            receipt = f"{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"
            previous = db.execute(
                "SELECT receipt,failures,due_at FROM retries WHERE revision_id=?", (path.stem,)
            ).fetchone()
            if previous and previous[0] == receipt and previous[2] > int(time.time()):
                continue
            started = time.monotonic()
            try:
                advanced = audit_pending(path, version, profile, dispatch, output)
            except AUDIT_ERRORS as error:
                failures = min(previous[1] + 1, 4) if previous and previous[0] == receipt else 1
                delay = min(60 * 2 ** (failures - 1), 300)
                with db:
                    db.execute(
                        """INSERT INTO retries (revision_id,receipt,failures,due_at) VALUES (?,?,?,?)
                        ON CONFLICT(revision_id) DO UPDATE SET receipt=excluded.receipt,
                        failures=excluded.failures,due_at=excluded.due_at""",
                        (path.stem, receipt, failures, int(time.time()) + delay),
                    )
                reference = hashlib.sha256(path.stem.encode("ascii")).hexdigest()[:16]
                logging.error(
                    "Elena difiere la revisión %s durante %d s por %s",
                    reference,
                    delay,
                    type(error).__name__,
                )
                # Una auditoría larga ya consumió el turno de este disparo.
                if deferred_error is not None or time.monotonic() - started >= 15:
                    raise
                deferred_error = error
                continue
            if previous:
                with db:
                    db.execute("DELETE FROM retries WHERE revision_id=?", (path.stem,))
            if advanced:
                break
    if deferred_error is not None:
        raise deferred_error


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("profile", type=Path)
    cli.add_argument("fixture", type=Path)
    cli.add_argument("output", type=Path)
    cli.add_argument("--pending", action="store_true")
    opts = cli.parse_args()
    profile = load_profile(opts.profile)
    if opts.pending:
        dispatch = read_regular(opts.fixture, 64).decode("ascii")
        if not re.fullmatch(r"[a-f0-9]{64}", dispatch):
            raise ValueError("Credencial de despacho inválida")
        run_pending(profile, dispatch, opts.output)
        return
    pinned = json.loads(read_regular(opts.fixture, 4096))
    fixture = profile["fixture"]
    revision = pinned.get("revision", 1)
    if type(revision) is not int or revision not in {1, 2, 3}:
        raise ValueError("Revisión de prueba inválida")
    source = fixture + ".json"
    out = opts.output / (source if revision == 1 else f"{fixture}-v{revision}.json")
    if out.exists():
        return
    if not re.fullmatch(r"[a-f0-9]{64}", pinned["sha256"]):
        raise ValueError("Huella de máster inválida")
    receipt = json.loads(read_regular(JOURNAL / source, INTAKE_LIMIT))
    order = receipt["order"]
    validate(order, fixture)
    edl = json.loads(read_regular(VALERIA / source, LIMIT))
    rendered = None
    if revision > 1:
        filename = source if revision == 2 else f"{fixture}-v{revision}.json"
        rendered = json.loads(read_regular(LUCAS / filename, LIMIT))
    audit(
        order, edl, pinned["sha256"], pinned["request_id"], out, rendered, revision, synthetic=True
    )


if __name__ == "__main__":
    try:
        main()
    except (*AUDIT_ERRORS, sqlite3.Error) as error:
        logging.error("Falló la auditoría audiovisual de Elena (%s)", type(error).__name__)
        raise SystemExit(1) from None
