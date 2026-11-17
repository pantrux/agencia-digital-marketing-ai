"""Genera la escaleta de Valeria con fotogramas del original verificado."""

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import subprocess
import time
import uuid
from contextlib import closing
from pathlib import Path

from production_handoff import (
    canonical,
    eligible,
    is_test_order,
    load_profile,
    publish,
    qa_rounds,
    read_regular,
    render_plan,
    revision_leaf,
    validate,
)
from production_inbox import MEDIA, UUID, matches, render_resolution
from production_intake import VIDEO_FORMAT, fingerprint, identity
from production_mateo import ORDER_ERRORS, confirmed, dispatch_request, lookup, strategy_reply
from production_sofia import JOURNAL, LIMIT, transcript_for

MATEO = Path("/var/lib/agency-production-mateo")
LUCAS = Path("/var/lib/agency-production-lucas")
ELENA = Path("/var/lib/agency-production-elena")
SOFIA = Path("/var/lib/agency-production-sofia")
PROFILE = {"angulo", "titulo", "subtitulo", "observacion", "apto_sin_cambios"}
OPTIONAL = {"subtitulo_ampliado"}
MAX_JPEG = 25000
MAX_VISUAL_BYTES = 110000


class VisualResponseFormatError(ValueError):
    """El modelo confirmado devolvió una respuesta fuera del contrato visual."""


def source_paths(receipt):
    job = receipt["order"]["media"]
    project = f"proyectos/{job['project_slug']}"
    raw = f"{project}/01_raw/{job['asset_id']}.{job['extension']}"
    audio = receipt["audio"]
    audio_path = f"{project}/03_assets/intake-source.wav" if audio is not None else None
    if receipt["raw"] != raw or (audio is not None and audio["path"] != audio_path):
        raise ValueError("Rutas de medios fuera del proyecto")
    return raw, audio_path


def reel_scenes(items, duration, *, ad=False):
    def boundary(value, maximum):
        if type(value) not in (int, float) or not 0 <= value <= maximum:
            raise VisualResponseFormatError("Tiempo de escena fuera del intervalo")
        return round(value * 30)

    if type(items) is not list or not 2 <= len(items) <= (12 if ad else 6):
        raise VisualResponseFormatError("Cantidad de escenas fuera del contrato")
    scenes, cursor, total = [], 0, 0
    for item in items:
        if type(item) is not dict or item.keys() - {"rotulo"} != {"desde", "hasta"}:
            raise VisualResponseFormatError("Escena inválida")
        start = boundary(item["desde"], duration)
        end = boundary(item["hasta"], duration)
        length = end - start
        if (not ad and start < cursor) or end > math.floor(duration * 30) or length < 15:
            raise VisualResponseFormatError("Los cortes deben avanzar sin superponerse")
        scene = {"source_start_frame": start, "source_end_frame": end}
        if "rotulo" in item:
            label = item["rotulo"]
            if type(label) is not dict or label.keys() - {"tamano"} != {
                "texto",
                "desde",
                "hasta",
                "posicion",
            }:
                raise VisualResponseFormatError("Rótulo inválido")
            title, position = label["texto"], label["posicion"]
            if (
                type(title) is not str
                or not title.strip()
                or re.search(r"[\x00-\x1f\x7f\ud800-\udfff]", title)
                or len(title.encode("utf-16-le")) // 2 > 80
                or position not in ("superior", "inferior")
                or label.get("tamano", "normal") not in ("normal", "grande")
            ):
                raise VisualResponseFormatError("Texto o posición de rótulo inválidos")
            first, last = (boundary(label[k], length / 30) for k in ("desde", "hasta"))
            if not 15 <= last - first <= 180:
                raise VisualResponseFormatError("El rótulo debe durar entre 0,5 y 6 segundos")
            scene["label"] = {
                "text": title.strip(),
                "start_frame": first,
                "end_frame": last,
                "position": "top" if position == "superior" else "bottom",
                **({"size": "large"} if label.get("tamano") == "grande" else {}),
            }
        scenes.append(scene)
        cursor = end
        total += length
    if not 900 <= total <= 1350 or not any("label" in scene for scene in scenes):
        raise VisualResponseFormatError("El reel requiere 30 a 45 segundos y al menos un rótulo")
    if ad:
        ordered = sorted(scenes, key=lambda scene: scene["source_start_frame"])
        hook = scenes[0].get("label")
        if total != 900 or not hook or hook["start_frame"] > 15 or hook["end_frame"] < 60:
            raise VisualResponseFormatError(
                "El anuncio necesita 30 segundos y un gancho desde la apertura"
            )
        if any(
            a["source_end_frame"] > b["source_start_frame"]
            for a, b in zip(ordered, ordered[1:], strict=False)
        ):
            raise VisualResponseFormatError("Los tramos del anuncio se superponen")
    return scenes, total


def ad_captions(scenes, words):
    cues, offset = [], 0
    for scene in scenes:
        start, end = scene["source_start_frame"], scene["source_end_frame"]
        group = []
        for word in words:
            first, last = round(word["start"] * 30), round(word["end"] * 30)
            text = word["word"].strip()
            if (
                not text
                or first < start
                or last > end
                or last <= first
                or offset + last - start > 780
            ):
                continue
            first, last = offset + first - start, offset + last - start
            if group and (
                len(group) >= 6
                or first - group[-1][1] >= 15
                or last - group[0][0] > 75
                or len(" ".join([part[2] for part in group] + [text]).encode("utf-16-le")) // 2 > 68
            ):
                cues.append((group[0][0], group[-1][1], " ".join(part[2] for part in group)))
                group = []
            group.append((first, last, text))
            if text.endswith((".", "?", "!")):
                cues.append((group[0][0], group[-1][1], " ".join(part[2] for part in group)))
                group = []
        if group:
            cues.append((group[0][0], group[-1][1], " ".join(part[2] for part in group)))
        offset += end - start
    result = []
    i = 0
    while i < len(cues):
        start, end, text = cues[i]
        start = max(start, result[-1]["end_frame"] if result else 0)
        # Una pausa muy corta pertenece a la tarjeta siguiente para conservar las palabras.
        while i + 1 < len(cues) and cues[i + 1][0] - start < 9:
            i += 1
            end = cues[i][1]
            text += " " + cues[i][2]
        following = cues[i + 1][0] if i + 1 < len(cues) else 780
        end = min(max(end, start + 15), following, 780)
        if end - start < 9 and result:
            last = result[-1]
            last["end_frame"] = max(last["end_frame"], end)
            last["text"] += " " + text
        elif end - start >= 9:
            result.append({"start_frame": start, "end_frame": end, "text": text})
        else:
            raise VisualResponseFormatError("No se pudo conservar el subtítulo breve")
        i += 1
    if any(len(cue["text"].encode("utf-16-le")) // 2 > 68 for cue in result):
        raise VisualResponseFormatError(
            "Los subtítulos necesitan pausas para mantener la legibilidad"
        )
    if not 1 <= len(result) <= 60:
        raise VisualResponseFormatError(
            "El anuncio requiere subtítulos respaldados por la voz original"
        )
    return result


def make_edl(receipt, angles, answer, *, brief=None, transcript=None):
    order = receipt["order"]
    plan = render_plan(order)
    resolution = render_resolution(order)
    raw, audio = source_paths(receipt)
    duration = receipt["duration_seconds"]
    reel = order.get("output_edit") == "reel"
    if type(duration) not in (float, int) or not 0 < duration <= 600:
        raise ValueError("Ingesta fuera del contrato de escaleta")
    if (
        type(answer) is not dict
        or answer.keys() - (OPTIONAL | ({"escenas"} if reel else set())) != PROFILE
        or type(answer["angulo"]) is not int
        or not 1 <= answer["angulo"] <= 3
        or type(answer["apto_sin_cambios"]) is not bool
        or type(answer.get("subtitulo_ampliado", False)) is not bool
    ):
        raise VisualResponseFormatError("Decisión de Valeria inválida")
    for key, limit in {"titulo": 120, "subtitulo": 240, "observacion": 600}.items():
        text = answer[key]
        if (
            type(text) is not str
            or not text.strip()
            or re.search(r"[\x00-\x1f\x7f\ud800-\udfff]", text)
            or len(text.encode("utf-16-le")) // 2 > limit
        ):
            raise VisualResponseFormatError("Texto fuera del contrato de escaleta")
    reasons = []
    if not answer["apto_sin_cambios"]:
        reasons.append("La revisión indica que deben ajustarse cortes, encuadre u ocultaciones.")
    if audio is None:
        reasons.append("El render final requiere una pista de audio verificable.")
    ad = brief.get("ad_cta") if brief and reel else None
    scenes = None
    ad_props = {}
    frames = min(18000, int(duration * 30 + 0.5))
    if reel and not reasons:
        scenes, frames = reel_scenes(answer.get("escenas"), duration, ad=bool(ad))
        if ad:
            ad_props = {
                "ad": {
                    "cta": ad,
                    "brand": brief["ad_brand"],
                    "captions": ad_captions(scenes, transcript["words"] if transcript else []),
                }
            }
    if frames < 3:
        reasons.append("La fuente dura menos de tres fotogramas a 30 fps.")
    render = (
        None
        if reasons
        else {
            "project_id": order["media"]["project_slug"],
            "job_id": order["order_id"] + "-v1",
            "composition": "AgencyVertical",
            "plan": plan,
            "format": "mp4",
            "resolution": resolution,
            "props": {
                "title": answer["titulo"],
                "subtitle": answer["subtitulo"],
                "duration_seconds": frames / 30,
                "video_path": raw,
                "audio_path": audio,
                **({"scenes": scenes} if scenes else {}),
                **ad_props,
                **({"subtitle_size": "large"} if answer.get("subtitulo_ampliado") else {}),
            },
        }
    )
    return {
        "angulo": angles[answer["angulo"] - 1]["angulo"],
        "observacion": answer["observacion"],
        "estado": "requiere_revision" if reasons else "lista_para_stills",
        "motivos": reasons,
        "tramo_fuente_segundos": [0, duration],
        "render": render,
    }


def sample_images(receipt, *, ad=False, transcript=None):
    job = receipt["order"]["media"]
    path = MEDIA / source_paths(receipt)[0]
    if path.resolve(strict=True) != path:
        raise ValueError("Ruta del original no canónica")
    duration = receipt["duration_seconds"]
    if type(duration) not in (float, int) or not 0 < duration <= 600:
        raise ValueError("Duración de ingesta inválida")
    images = []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size != job["bytes"]:
            raise ValueError("Identidad del original cambió")
        if hashlib.file_digest(source, "sha256").hexdigest() != job["sha256"]:
            raise ValueError("Huella del original cambió")
        reel = receipt["order"].get("output_edit") == "reel"
        fractions = (0.01, 0.25, 0.5, 0.75, 0.98) if reel else (0.15, 0.5, 0.85)
        limit = 12000 if reel else MAX_JPEG
        stamps = [round(duration * fraction, 5) for fraction in fractions]
        if ad:
            words = transcript["words"] if transcript else []
            anchors = [
                word["start"]
                for word in words
                if re.search(r"[.?!]$", word["word"]) and 0 <= word["start"] < duration
            ]
            targets = [duration * (i + 0.5) / 8 for i in range(8)]
            step = duration / len(targets)
            stamps = []
            for target in targets:
                nearby = [stamp for stamp in anchors if abs(stamp - target) < step / 2]
                stamps.append(min(nearby, key=lambda stamp: abs(stamp - target), default=target))
            limit = 7000
        for stamp in stamps:
            args = [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-ss",
                str(stamp),
                "-protocol_whitelist",
                "file",
                "-f",
                VIDEO_FORMAT[job["extension"]],
                "-i",
                f"/proc/self/fd/{fd}",
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-threads",
                "1",
                "-c:v",
                "mjpeg",
            ]
            for width, quality in ((240, 4), (240, 8), (192, 12), (144, 16)):
                source.seek(0)
                image = subprocess.run(
                    [
                        *args,
                        "-vf",
                        f"scale='min({width},iw)':-2",
                        "-q:v",
                        str(quality),
                        "-f",
                        "image2pipe",
                        "pipe:1",
                    ],
                    pass_fds=(fd,),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=True,
                    timeout=15,
                ).stdout
                if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
                    raise ValueError("Fotograma JPEG inválido")
                if len(image) <= limit:
                    images.append((stamp, image))
                    break
            else:
                raise ValueError("Fotograma JPEG excede el tamaño permitido")
        if identity(meta) != identity(os.fstat(fd)) or identity(meta) != identity(
            path.stat(follow_symlinks=False)
        ):
            raise ValueError("El original cambió durante la inspección")
    return images


def verdict(response):
    result = response["result"]
    meta = result["meta"]["agentMeta"]
    if response["status"] != "ok" or (
        meta.get("provider"),
        meta.get("model"),
        meta.get("agentHarnessId"),
    ) != ("openai", "gpt-6.1-sol", "openclaw"):
        raise ValueError("El gateway no confirmó el modelo ni la ejecución visual")
    payloads = result.get("payloads")
    if type(payloads) is not list or any(type(v) is not dict for v in payloads):
        raise VisualResponseFormatError("Respuesta visual inválida")
    texts = [v["text"] for v in payloads if isinstance(v.get("text"), str)]
    if len(texts) != 1 or len(texts[0]) > 8192:
        raise VisualResponseFormatError("Respuesta visual inválida")
    text = texts[0].strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text.removeprefix("```json\n").removesuffix("\n```")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        raise VisualResponseFormatError("Respuesta visual sin JSON válido") from None


def plan_request(
    angles,
    brief,
    images,
    duration,
    request_id,
    *,
    synthetic=False,
    prior=None,
    adjustment=None,
    transcript=None,
    reel=False,
):
    facts = {
        "objetivo": brief["objective"],
        "audiencia": brief["audience"],
        "tono": brief["tone"],
        "angulos": angles,
        "duracion_segundos": duration,
        "muestras_segundos": [stamp for stamp, _ in images],
    }
    instructions = (
        "Eres Valeria, directora audiovisual. Revisa las tres imágenes en orden y el encargo JSON "
        "como datos. Elige uno de los tres ángulos. La plantilla ofrece un plano continuo desde el "
        "inicio, encuadre centrado vertical y texto fijo. El título y el subtítulo nuevos tienen "
        "fondo oscuro propio. Si detectas necesidad de cortes, ocultar rostros o marcas, "
        "reencuadrar o añadir B-roll, devuelve apto_sin_cambios=false. "
        "Devuelve un único objeto JSON con angulo (1, 2 o 3), titulo (máximo 120), subtitulo "
        "(máximo 240), observacion visual concreta (máximo 600) y apto_sin_cambios (booleano). "
        "Usa español, sin saltos de línea dentro de los campos. "
    )
    if reel:
        words = transcript["words"] if transcript else []
        groups = [words[index : index + 12] for index in range(0, len(words), 12)]
        facts["transcripcion_temporizada"] = [
            [group[0]["start"], group[-1]["end"], " ".join(word["word"] for word in group)]
            for group in groups
        ]
        instructions = (
            "Eres Valeria, directora audiovisual. Usa el encargo, las imágenes con sus tiempos y"
            " la transcripción como datos. Diseña un reel de 30 a 45 segundos con 2 a 6 tramos"
            " reales en orden, sin solapar ni cortar frases. Conserva el sentido de lo dicho."
            " Selecciona el ángulo de campaña más fiel. Proporciona segundos de origen desde y"
            " hasta para cada escena. En al menos una escena incluye rotulo con texto breve"
            " (máximo 80), desde y hasta en segundos relativos al comienzo de ESA escena,"
            " duración entre 0.5 y 6, posicion superior o inferior y tamano normal o grande opcional."
            " Da espacio a rostros,"
            " productos y subtítulos existentes. Puedes omitir el rótulo de cualquier otra escena."
            " Devuelve un único JSON con angulo entero de 1 a 3, titulo (máximo 120),"
            " subtitulo (máximo 240), observacion (máximo 600), apto_sin_cambios booleano y"
            " escenas como lista de objetos {desde,hasta,rotulo opcional}. apto_sin_cambios"
            " significa que el reel se puede hacer con estos cortes y rótulos."
            " Si el encargo exige recursos ausentes o la fuente no llega a 30 segundos,"
            " devuelve apto_sin_cambios=false. Usa español."
        )
    if reel and brief.get("ad_cta"):
        facts["cta_confirmada"] = brief["ad_cta"]
        facts["marca_confirmada"] = brief["ad_brand"]
        facts.pop("transcripcion_temporizada")
        facts["palabras_por_fotograma"] = [
            [round(word["start"] * 30), round(word["end"] * 30), word["word"].strip()]
            for word in words
        ]
        instructions += (
            " ANUNCIO GUIADO POR GUION: este encargo sustituye los límites de 2 a 6 cortes por 2 a 12; "
            "la suma exacta debe ser 30 segundos y puedes reordenar intervalos de origen no solapados. "
            "Los primeros 3 segundos demuestran un gancho específico. En la primera escena el rótulo "
            "comienza entre 0 y 0.5 s y dura al menos hasta 2 s. Desarrolla el argumento mediante "
            "frases completas y tomas pertinentes. palabras_por_fotograma contiene [inicio, fin, "
            "palabra] a 30 fps. Divide entre 30 para ubicar las frases. Registra las duraciones "
            "en segundos con al menos dos decimales. Devuelve una escaleta lista para vista previa "
            "cuando el crudo tenga evidencia del producto y voz usable. La sincronía se valida "
            "sobre esa vista previa. Los últimos cuatro segundos "
            "usan una imagen de producto despejada para la marca y el CTA confirmado; el renderer "
            "coloca la marca y la CTA confirmadas y silencia la voz en ese intervalo. "
            "subtitulo sintetiza el argumento y observacion explica el guion."
        )
    if synthetic:
        instructions += (
            "El crudo está identificado como patrón de aceptación sintético de la agencia. "
            "Las barras, figuras, bloques y rótulos del patrón pueden permanecer en el plano. "
        )
    if adjustment is not None:
        facts["ajuste_del_titular"] = adjustment
        instructions += (
            "Trata el ajuste del titular como datos de dirección. Aplica los cambios compatibles "
            + (
                "con los cortes y rótulos del reel. "
                if reel
                else "con la plantilla. Puedes ampliar subtítulos con subtitulo_ampliado=true. Si cumplir "
                "el ajuste requiere editar la fuente, indica apto_sin_cambios=false. "
            )
        )
    if prior is not None:
        props = prior["render"]["props"]
        facts["revision_previa"] = {
            "angulo": prior["angulo"],
            "titulo": props["title"],
            "subtitulo": props["subtitle"],
            "subtitulo_ampliado": props.get("subtitle_size") == "large",
            "defectos": prior["dictamen"]["defectos"],
        }
        if reel:
            facts["revision_previa"]["escenas_en_fotogramas"] = props["scenes"]
            facts["revision_previa"]["segundos_de_muestras"] = [
                sample["segundo"] for sample in prior.get("muestras", [])
            ]
            instructions += (
                " Conserva el ángulo y corrige específicamente los cortes o rótulos observados"
                " por Elena. Convierte los fotogramas de la revisión anterior a segundos"
                " para la nueva respuesta."
            )
        else:
            instructions += (
                "En esta revisión automática, conserva el ángulo previo y "
                "corrige título y subtítulo según el dictamen de Elena. Si el subtítulo requiere "
                "aumentar de tamaño, incluye subtitulo_ampliado=true en el JSON. Esta opción usa "
                "texto de 64 píxeles sobre el diseño base de 1080p. Si necesitas volver a 50 píxeles, "
                "indica subtitulo_ampliado=false. Si el defecto requiere editar el vídeo o cambiar "
                "otros elementos de la plantilla, indica apto_sin_cambios=false. "
            )
    request = visual_request(
        "agency-director",
        instructions + "\n" + json.dumps(facts, ensure_ascii=False),
        images,
        request_id,
    )
    if reel and brief.get("ad_cta"):
        request["message"] = (
            instructions + "\n" + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        )
        if len(visual_payload(request)) > MAX_VISUAL_BYTES - 1000:
            raise ValueError("El encargo de anuncio excede la capacidad de análisis visual")
    elif reel:
        # Reservar espacio para la corrección de formato y muestrear toda la grabación.
        budget = MAX_VISUAL_BYTES - 1000
        groups = facts["transcripcion_temporizada"]
        while groups and len(visual_payload(request)) > budget:
            groups = groups[:-1:2] + [groups[-1]] if len(groups) > 2 else []
            facts["transcripcion_temporizada"] = groups
            request["message"] = instructions + "\n" + json.dumps(facts, ensure_ascii=False)
    return request


def visual_request(agent, message, images, request_id):
    return {
        "agentId": agent,
        "message": message,
        "deliver": False,
        "modelRun": True,
        "promptMode": "none",
        "cleanupBundleMcpOnRunEnd": True,
        "idempotencyKey": request_id,
        "timeout": 55,
        "attachments": [
            {
                "type": "image",
                "fileName": f"muestra-{i}.jpg",
                "mimeType": "image/jpeg",
                "content": base64.b64encode(image).decode(),
            }
            for i, (_, image) in enumerate(images, 1)
        ],
    }


def visual_payload(request):
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()


def run_visual(request):
    raw = visual_payload(request)
    if len(raw) > MAX_VISUAL_BYTES:
        raise ValueError("Muestras visuales fuera del límite del gateway local")
    p = subprocess.run(
        ["/usr/local/bin/node", str(Path(__file__).with_name("production_visual_rpc.mjs"))],
        input=raw,
        env=dict(os.environ, PATH="/usr/local/bin:/usr/bin:/bin"),
        capture_output=True,
        check=True,
        timeout=72,
    )
    if len(p.stdout) > 512 * 1024:
        raise ValueError("Respuesta visual fuera del límite")
    return verdict(json.loads(p.stdout))


def process(
    receipt, strategy, brief, target, request_id, *, synthetic=False, prior=None, adjustment=None
):
    started = time.monotonic()
    order = receipt["order"]
    digest = hashlib.sha256(canonical(brief)).hexdigest()
    if (
        strategy["order_id"],
        strategy["source_sha256"],
        strategy["brief_sha256"],
        strategy["ejecucion"],
    ) != (
        order["order_id"],
        order["media"]["sha256"],
        digest,
        "isolated-agent-runtime",
    ):
        raise ValueError("La estrategia no corresponde al encargo confirmado")
    angles = strategy_reply(json.dumps({"angulos": strategy["angulos"]}))
    reel = order.get("output_edit") == "reel"
    transcript = transcript_for(receipt, SOFIA / (order["order_id"] + ".json")) if reel else None
    images = (
        sample_images(receipt, ad=True, transcript=transcript)
        if reel and brief.get("ad_cta")
        else sample_images(receipt)
    )
    request = plan_request(
        angles,
        brief,
        images,
        receipt["duration_seconds"],
        request_id,
        synthetic=synthetic,
        prior=prior,
        adjustment=adjustment,
        reel=reel,
        transcript=transcript,
    )
    for attempt in (1, 2):
        try:
            answer = run_visual(request)
            if (
                type(answer) is dict
                and prior is not None
                and prior["render"]["props"].get("subtitle_size") == "large"
            ):
                answer.setdefault("subtitulo_ampliado", True)
            edl = make_edl(receipt, angles, answer, brief=brief, transcript=transcript)
            break
        except VisualResponseFormatError:
            # Reservar tiempo para la llamada visual y el acuse de Studio dentro del turno.
            if attempt == 2 or time.monotonic() - started >= 65:
                raise
            logging.warning(
                "Valeria vuelve a solicitar la escaleta de %s por formato inválido",
                order["order_id"],
            )
            request["idempotencyKey"] = str(
                uuid.uuid5(uuid.UUID(request_id), "corregir-formato:v1")
            )
            request["message"] = (
                "Corrige el formato de la respuesta. Devuelve solo el objeto JSON requerido. "
                "Usa un entero para angulo y valores JSON true o false para los booleanos. "
                "Evita claves adicionales, Markdown y saltos de línea dentro de los valores. "
                + request["message"]
            )
    if prior is not None and edl["render"] is not None:
        previous = prior["render"]["props"]
        props = edl["render"]["props"]
        changes = ("scenes", "ad") if reel else ("title", "subtitle", "subtitle_size")
        if edl["angulo"] != prior["angulo"] or all(
            props.get(key) == previous.get(key) for key in changes
        ):
            edl["estado"] = "requiere_revision"
            edl["motivos"] = ["La propuesta debe conservar el ángulo y aplicar la corrección."]
            edl["render"] = None
    if edl["render"] and fingerprint(MEDIA / receipt["audio"]["path"]) != {
        k: receipt["audio"][k] for k in ("sha256", "bytes")
    }:
        raise ValueError("El audio preparado cambió")
    publish(
        target,
        canonical(
            {
                "order_id": order["order_id"],
                "source_sha256": order["media"]["sha256"],
                "brief_sha256": digest,
                "modelo": "openai/gpt-6.1-sol",
                "runtime": "openclaw",
                "ejecucion": "modelRun",
                "muestras": [
                    {"segundo": stamp, "sha256": hashlib.sha256(image).hexdigest()}
                    for stamp, image in images
                ],
                "escaleta": edl,
            }
        )
        + b"\n",
    )


def prior_rejection(order, brief, revision, output, review):
    identity = order["order_id"]
    leaf = revision_leaf(identity, revision)
    job = f"{identity}-v{revision}"
    art = json.loads(read_regular(LUCAS / leaf, LIMIT))
    direction = json.loads(read_regular(output / leaf, LIMIT))
    digest = hashlib.sha256(canonical(brief)).hexdigest()
    expected = (identity, digest)
    if (
        any((x["order_id"], x["brief_sha256"]) != expected for x in (review, art, direction))
        or any(x["source_sha256"] != order["media"]["sha256"] for x in (art, direction))
        or (review["job_id"], art["job_id"]) != (job, job)
        or review["master_sha256"] != (art.get("master") or art["preview"])["sha256"]
        or direction["escaleta"]["estado"] != "lista_para_stills"
    ):
        raise ValueError("El dictamen anterior no corresponde a la orden vigente")
    return {
        **direction["escaleta"],
        "dictamen": review["dictamen"],
        **({"muestras": review["muestras"]} if order.get("output_edit") == "reel" else {}),
    }


def pending_direction(output, identity, rounds):
    for revision in range(1, rounds + 1):
        target = output / revision_leaf(identity, revision)
        if not target.exists():
            return revision, target, None
        saved = json.loads(read_regular(target, LIMIT))
        if saved["escaleta"]["estado"] == "requiere_revision":
            return revision, target, saved
    return None


def report_manual_review(order, brief, revision, saved, token):
    if (
        saved["order_id"] != order["order_id"]
        or saved["source_sha256"] != order["media"]["sha256"]
        or saved["brief_sha256"] != hashlib.sha256(canonical(brief)).hexdigest()
        or saved["escaleta"]["render"] is not None
    ):
        raise ValueError("La escaleta de revisión no corresponde a la orden vigente")
    result = dispatch_request(
        {
            "action": "quality_manual_review",
            "order_id": order["order_id"],
            "user_id": order["user_id"],
            "upload_id": order["upload_id"],
            "source_sha256": order["media"]["sha256"],
            "direction_sha256": hashlib.sha256(canonical(saved)).hexdigest(),
            "revision": revision,
            "brief_confirmed_at": brief["confirmed_at"],
        },
        token,
    )
    if result != {"recorded": True}:
        raise ValueError("Studio no confirmó la revisión manual")


def direction_digest(raw):
    return hashlib.sha256(canonical(json.loads(raw))).hexdigest()


def sync_adjustment_directory(directory):
    if hasattr(os, "O_DIRECTORY"):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def reconcile_adjustment(order, folder, output, token):
    pending = folder / "pending.json"
    if not pending.exists():
        return False
    if folder.resolve(strict=True) != folder:
        raise ValueError("Directorio de ajustes inválido")
    raw = read_regular(pending, LIMIT)
    result = json.loads(raw)
    identity = order["order_id"]
    number, revision = result["number"], result["revision"]
    if (
        type(number) is not int
        or number < 1
        or result["order_id"] != identity
        or result["user_id"] != order["user_id"]
        or result["upload_id"] != order["upload_id"]
        or result["source_sha256"] != order["media"]["sha256"]
    ):
        raise ValueError("El ajuste pendiente no corresponde a esta producción")
    target = output / revision_leaf(identity, revision)
    previous = read_regular(folder / f"previous-{number}.json", LIMIT)
    replacement = read_regular(folder / f"candidate-{number}.json", LIMIT)
    before, after = direction_digest(previous), direction_digest(replacement)
    current = direction_digest(read_regular(target, LIMIT))
    if (
        before != result["review_sha256"]
        or after != result["direction_sha256"]
        or current not in (before, after)
    ):
        raise ValueError("Cambió la escaleta durante el ajuste")
    sync_adjustment_directory(folder)
    sync_adjustment_directory(output)
    if dispatch_request(result, token) != {"recorded": True}:
        raise ValueError("Studio no confirmó el ajuste")
    if current != after:
        swap = folder / f"swap-{number}.json"
        publish(swap, replacement)
        os.replace(swap, target)
        sync_adjustment_directory(output)
    publish(folder / f"recorded-{number}.json", raw)
    pending.unlink()
    sync_adjustment_directory(folder)
    return True


def direction_request_id(identity, key, format_round):
    if format_round:
        key += f":formato:{format_round}"
    return str(uuid.uuid5(uuid.UUID(identity), key))


def handle_adjustment(receipt, strategy, revision, target, saved, folder, token, format_round=0):
    order = receipt["order"]
    identity = order["order_id"]
    context = dispatch_request({"action": "manual_context", "order_id": identity}, token)
    if context == {"brief": None, "adjustment": None}:
        return False
    if not isinstance(context, dict) or context.keys() != {"brief", "adjustment"}:
        raise ValueError("Studio devolvió un contexto de ajuste inválido")
    brief = confirmed({"brief": context["brief"]}, order)
    adjustment = context["adjustment"]
    number = adjustment["number"]
    instructions = adjustment["instructions"]
    previous = read_regular(target, LIMIT)
    if (
        brief is None
        or type(number) is not int
        or number < 1
        or type(instructions) is not str
        or not 0 < len(instructions) <= 800
        or adjustment["revision"] != revision
        or adjustment["review_sha256"] != direction_digest(previous)
        or json.loads(previous) != saved
        or saved["order_id"] != identity
        or saved["source_sha256"] != order["media"]["sha256"]
        or saved["brief_sha256"] != hashlib.sha256(canonical(brief)).hexdigest()
        or saved["escaleta"]["render"] is not None
    ):
        raise ValueError("El ajuste no corresponde a la escaleta en revisión")
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.resolve(strict=True) != folder:
        raise ValueError("Directorio de ajustes inválido")
    publish(folder / f"previous-{number}.json", previous)
    candidate = folder / f"candidate-{number}.json"
    if not candidate.exists():
        process(
            receipt,
            strategy,
            brief,
            candidate,
            direction_request_id(identity, f"valeria-ajuste:v{revision}:{number}", format_round),
            adjustment=instructions,
        )
    raw = read_regular(candidate, LIMIT)
    proposed = json.loads(raw)
    state = proposed["escaleta"]["estado"]
    if (
        state not in ("lista_para_stills", "requiere_revision")
        or (state == "lista_para_stills") != isinstance(proposed["escaleta"]["render"], dict)
        or any(proposed[k] != saved[k] for k in ("order_id", "source_sha256", "brief_sha256"))
    ):
        raise ValueError("Escaleta corregida inválida")
    result = {
        "action": "manual_adjustment_result",
        "order_id": identity,
        "user_id": order["user_id"],
        "upload_id": order["upload_id"],
        "source_sha256": order["media"]["sha256"],
        "revision": revision,
        "brief_confirmed_at": brief["confirmed_at"],
        "number": number,
        "review_sha256": adjustment["review_sha256"],
        "direction_sha256": direction_digest(raw),
        "status": "ready" if state == "lista_para_stills" else "needs_review",
    }
    publish(folder / "pending.json", canonical(result) + b"\n")
    return reconcile_adjustment(order, folder, target.parent, token)


def process_pending(path, strategy, profile, token, output, format_round):
    receipt = json.loads(read_regular(path, LIMIT))
    order = receipt["order"]
    validate(order, path.stem)
    if not eligible(order, profile):
        return False
    synthetic = is_test_order(order, profile)
    identity = order["order_id"]
    adjustments = output / f".ajustes-{identity}"
    if reconcile_adjustment(order, adjustments, output, token):
        return True
    pending = pending_direction(output, identity, qa_rounds(order, profile))
    if pending is None:
        return False
    revision, target, saved = pending
    if saved is not None:
        if not synthetic:
            if brief := lookup(order, token):
                report_manual_review(order, brief, revision, saved, token)
            elif order["plan_id"] != "free":
                return handle_adjustment(
                    receipt,
                    json.loads(read_regular(strategy, LIMIT)),
                    revision,
                    target,
                    saved,
                    adjustments,
                    token,
                    format_round,
                )
        return False
    review = None
    if revision > 1:
        review_path = ELENA / revision_leaf(identity, revision - 1)
        if (
            not review_path.is_file()
            or (ELENA / target.name).exists()
            or (LUCAS / target.name).exists()
            or (synthetic and (ELENA / revision_leaf(identity, 3)).exists())
        ):
            return False
        review = json.loads(read_regular(review_path, LIMIT))
        decision = review["dictamen"]
        if decision["decision"] != "rechazado" or not decision["defectos"]:
            return False
    if not (brief := lookup(order, token)):
        return False
    request_key, extra = "valeria-modelRun:v2", {}
    if review is not None:
        extra["prior"] = prior_rejection(order, brief, revision - 1, output, review)
        request_key = f"valeria-correccion-tipografia:v{revision}"
    process(
        receipt,
        json.loads(read_regular(strategy, LIMIT)),
        brief,
        target,
        direction_request_id(identity, request_key, format_round),
        synthetic=synthetic,
        **extra,
    )
    if not synthetic and target.exists():
        saved = json.loads(read_regular(target, LIMIT))
        if saved["escaleta"]["estado"] == "requiere_revision":
            report_manual_review(order, brief, revision, saved, token)
    return True


def run_pending(profile, token, output):
    # Lucas observa el directorio de escaletas y SQLite escribe en el subdirectorio.
    state = output / "reintentos"
    state.mkdir(mode=0o700, exist_ok=True)
    if state.resolve(strict=True) != state:
        raise ValueError("Directorio de reintentos de dirección inválido")
    started = time.monotonic()
    with closing(sqlite3.connect(state / "valeria.sqlite3")) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS retries (
                order_id TEXT PRIMARY KEY, failures INTEGER NOT NULL,
                due_at INTEGER NOT NULL, format_round INTEGER NOT NULL
            ) STRICT"""
        )
        for path in sorted(JOURNAL.glob("*.json")):
            strategy = MATEO / path.name
            if not matches(UUID, path.stem) or not strategy.is_file():
                continue
            previous = db.execute(
                "SELECT failures, due_at, format_round FROM retries WHERE order_id=?", (path.stem,)
            ).fetchone()
            failures, due_at, format_round = previous or (0, 0, 0)
            if due_at > int(time.time()):
                continue
            try:
                advanced = process_pending(path, strategy, profile, token, output, format_round)
            except (*ORDER_ERRORS, subprocess.SubprocessError) as error:
                failures = min(failures + 1, 3)
                # Dos activaciones del temporizador pueden atender otras órdenes antes del reintento.
                delay = min(95 * 2 ** (failures - 1), 300)
                due_at = int(time.time()) + delay
                # Solo una respuesta confirmada como inválida habilita otra solicitud al modelo.
                if isinstance(error, VisualResponseFormatError):
                    format_round += 1
                with db:
                    db.execute(
                        """INSERT INTO retries (order_id, failures, due_at, format_round)
                        VALUES (?, ?, ?, ?) ON CONFLICT(order_id) DO UPDATE SET
                        failures=excluded.failures, due_at=excluded.due_at,
                        format_round=excluded.format_round""",
                        (path.stem, failures, due_at, format_round),
                    )
                logging.error(
                    "Valeria difiere la orden %s durante %d s por %s",
                    path.stem,
                    delay,
                    type(error).__name__,
                )
                if time.monotonic() - started >= 15:
                    return
                continue
            if previous:
                with db:
                    db.execute("DELETE FROM retries WHERE order_id=?", (path.stem,))
            if advanced:
                return


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("profile", type=Path)
    cli.add_argument("token", type=Path)
    cli.add_argument("out", type=Path)
    opts = cli.parse_args()
    profile = load_profile(opts.profile)
    token = read_regular(opts.token, 64).decode("ascii")
    if not re.fullmatch(r"[a-f0-9]{64}", token) or any(
        p.resolve(strict=True) != p for p in (JOURNAL, MATEO, opts.out)
    ):
        raise ValueError("Entorno de dirección inválido")
    run_pending(profile, token, opts.out)


if __name__ == "__main__":
    try:
        main()
    except (*ORDER_ERRORS, subprocess.SubprocessError, sqlite3.Error):
        raise SystemExit("Falló el turno visual de Valeria") from None
