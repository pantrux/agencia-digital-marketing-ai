"""Evalúa el máster codificado y registra el dictamen audiovisual de Elena."""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import stat
import subprocess
import uuid
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
from production_inbox import MEDIA, render_resolution
from production_intake import identity
from production_mateo import dispatch_request, lookup
from production_sofia import JOURNAL, LIMIT
from production_valeria import MAX_JPEG, run_visual, visual_request

VALERIA = Path("/var/lib/agency-production-valeria")
LUCAS = Path("/var/lib/agency-production-lucas")
FIELDS = {"puntaje", "marca_free", "observaciones", "defectos"}


def make_verdict(review, metrics, duration, *, plan="Free", resolution="1080p"):
    if (
        type(review) is not dict
        or review.keys() != FIELDS
        or type(review["puntaje"]) is not int
        or not 0 <= review["puntaje"] <= 100
        or type(review["marca_free"]) is not str
        or len(review["marca_free"]) > 100
        or type(review["observaciones"]) is not list
        or not 1 <= len(review["observaciones"]) <= 8
        or type(review["defectos"]) is not list
        or len(review["defectos"]) > 8
    ):
        raise ValueError("Dictamen visual inválido")
    for name, key in (("observaciones", "detalle"), ("defectos", "correccion")):
        for evidence in review[name]:
            if (
                type(evidence) is not dict
                or evidence.keys() != {"muestra", key}
                or type(evidence["muestra"]) is not int
                or not 1 <= evidence["muestra"] <= 3
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


def decode(path, expected, duration):
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
        images = []
        for fraction in (0.2, 0.5, 0.8):
            timestamp = round(min(duration, metrics["duration"]) * fraction, 5)
            for width, quality in ((360, 5), (360, 8), (288, 10), (192, 16)):
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
                if len(image) <= MAX_JPEG:
                    break
            if (
                not 4 <= len(image) <= MAX_JPEG
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


def audit(order, edl, master_hash, request_id, out, rendered=None, revision=1, *, synthetic=False):
    decision = edl["escaleta"]
    render = decision["render"]
    job = order["media"]
    plan = render_plan(order)
    resolution = render_resolution(order)
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
        / (render["job_id"] if rendered is None else f"{order['order_id']}-v{revision}")
        / "render"
    )
    master = directory / "master.mp4"
    if rendered is not None:
        asset = rendered["master"]
        if rendered.get("resolution", "1080p") != resolution or (
            rendered["order_id"],
            rendered["source_sha256"],
            rendered["brief_sha256"],
            rendered["job_id"],
            asset["sha256"],
        ) != (
            order["order_id"],
            job["sha256"],
            edl["brief_sha256"],
            f"{order['order_id']}-v{revision}",
            master_hash,
        ):
            raise ValueError("Recibo de Lucas ajeno al máster")
        master = MEDIA / asset["path"]
        if master.parent != directory or master.name not in {"master.mp4", "master-corrected.mp4"}:
            raise ValueError("Ruta del máster corregido inválida")
    props = render["props"]
    metrics, images = decode(master, master_hash, props["duration_seconds"])
    evidence = {
        "titulo_esperado": props["title"],
        "subtitulo_esperado": props["subtitle"],
        "angulo": decision["angulo"],
        "mediciones": metrics,
        "segundos_de_muestras": [s for s, _ in images],
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
    if synthetic:
        instructions += "Considera que el crudo es una demostración técnica con barras de color. "
    review = run_visual(
        visual_request(
            "agency-critic",
            instructions + "\n" + json.dumps(evidence, ensure_ascii=False),
            images,
            request_id,
        )
    )
    qa = make_verdict(review, metrics, props["duration_seconds"], plan=plan, resolution=resolution)
    record = {
        "order_id": order["order_id"],
        "job_id": directory.parent.name,
        "master_sha256": master_hash,
        "brief_sha256": edl["brief_sha256"],
        "mediciones": metrics,
        "dictamen": qa,
        "modelo": "openai/gpt-6.1-sol",
        "runtime": "openclaw",
        "ejecucion": "modelRun",
        "muestras": [{"segundo": s, "sha256": hashlib.sha256(p).hexdigest()} for s, p in images],
    }
    publish(out, canonical(record) + b"\n")
    return record


def report_rejection(order, brief, rendered, review, dispatch, revision):
    job = f"{order['order_id']}-v{revision}"
    master_hash = rendered["master"]["sha256"]
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


def run_pending(profile, dispatch, output):
    for path in sorted(LUCAS.glob("*.json")):
        if not (version := revision_from_stem(path.stem)):
            continue
        identity, revision = version
        intake = JOURNAL / f"{identity}.json"
        direction = VALERIA / path.name
        target = output / path.name
        if not direction.is_file() or not intake.is_file():
            continue
        order = json.loads(read_regular(intake, LIMIT))["order"]
        validate(order, identity)
        synthetic = is_test_order(order, profile)
        if not eligible(order, profile) or revision > qa_rounds(order, profile):
            continue
        terminal = not synthetic and revision == qa_rounds(order, profile)
        review = None
        if target.exists():
            if not terminal:
                continue
            review = json.loads(read_regular(target, LIMIT))
            if review["dictamen"]["decision"] != "rechazado":
                continue
        if not (brief := lookup(order, dispatch)):
            continue
        edl = json.loads(read_regular(direction, LIMIT))
        rendered = json.loads(read_regular(path, LIMIT))
        if edl["brief_sha256"] != hashlib.sha256(canonical(brief)).hexdigest():
            raise ValueError("El máster no corresponde al brief vigente")
        digest = rendered["master"]["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Huella de máster inválida")
        if review is None:
            request_id = str(uuid.uuid5(uuid.UUID(identity), f"elena:v{revision}:" + digest))
            review = audit(
                order, edl, digest, request_id, target, rendered, revision, synthetic=synthetic
            )
        if terminal and review["dictamen"]["decision"] == "rechazado":
            report_rejection(order, brief, rendered, review, dispatch, revision)
        break


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
    receipt = json.loads(read_regular(JOURNAL / source, LIMIT))
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
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        StopIteration,
        subprocess.SubprocessError,
    ) as error:
        logging.error("Falló la auditoría audiovisual de Elena (%s)", type(error).__name__)
        raise SystemExit(1) from None
