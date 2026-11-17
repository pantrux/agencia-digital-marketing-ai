"""Empaqueta una entrega audiovisual cuyo máster exacto aprobó Elena."""

import argparse
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
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
    revision_leaf,
    validate,
)
from production_inbox import MEDIA, UUID, matches, render_format, render_resolution
from production_intake import fingerprint
from production_intake import identity as file_identity
from production_lucas import submit_with_receipt, verify_file
from production_mateo import ORDER_ERRORS, lookup, strategy_reply
from production_sofia import JOURNAL, LIMIT
from production_valeria import MATEO, source_paths

LUCAS = Path("/var/lib/agency-production-lucas")
ELENA = Path("/var/lib/agency-production-elena")
VALERIA = Path("/var/lib/agency-production-valeria")
MAX_KIT = 3 * 1024**3
MAX_MASTER = MAX_KIT - 1024**2
CHUNK = 1024**2


def digest_stream(stream, size, output=None):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, CHUNK))
        if not chunk:
            raise ValueError("El archivo es más corto que el tamaño acreditado")
        if output is not None:
            output.write(chunk)
        digest.update(chunk)
        remaining -= len(chunk)
    if stream.read(1):
        raise ValueError("El archivo excede el tamaño acreditado")
    return digest.hexdigest()


@contextmanager
def open_media(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ValueError("Tamaño o tipo de archivo fuera del contrato de entrega")
        yield stream, before.st_size
        named = os.open(path, flags)
        try:
            stable = (
                file_identity(before)
                == file_identity(os.fstat(stream.fileno()))
                == file_identity(os.fstat(named))
            )
        finally:
            os.close(named)
        if not stable:
            raise ValueError("El archivo de entrega cambió durante la lectura")


def package(order, master, render, qa, brief, *, caption=None, output=None):
    """Devuelve un ZIP reproducible con el máster, el brief y su auditoría."""
    media = render["master"]
    size = media["bytes"]
    digest = media["sha256"]
    verdict = qa["dictamen"]
    if (
        render["order_id"] != order
        or qa["order_id"] != order
        or qa["job_id"] != render["job_id"]
        or qa["master_sha256"] != digest
        or type(size) is not int
        or not 0 < size <= MAX_MASTER
        or verdict["decision"] != "aprobado"
        or type(verdict["puntaje"]) is not int
        or not 85 <= verdict["puntaje"] <= 100
        or verdict["reglas_duras_incumplidas"] != []
    ):
        raise ValueError("El máster exacto requiere un dictamen aprobado")
    files = {
        "02-brief.json": canonical(brief) + b"\n",
        "03-render.json": canonical(render) + b"\n",
        "04-dictamen-qa.json": canonical(qa) + b"\n",
    }
    if caption is not None:
        if not isinstance(caption, str) or not 1 <= len(caption.strip()) <= 700:
            raise ValueError("Caption fuera del contrato de entrega")
        files["05-caption.txt"] = (caption.strip() + "\n").encode()
    if "creditos_musica" in qa:
        credits = qa["creditos_musica"]
        if not isinstance(credits, str) or not 1 <= len(credits.strip()) <= 400:
            raise ValueError("Crédito de música fuera del contrato")
        files["06-creditos-musica.txt"] = (credits.strip() + "\n").encode()
    files["SHA256SUMS"] = (
        f"{digest}  01-video.mp4\n"
        + "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in files.items())
    ).encode("ascii")

    def entry(name):
        info = zipfile.ZipInfo(name)
        info.create_system = 0
        info.external_attr = 0o600 << 16
        return info

    destination = io.BytesIO() if output is None else output
    source = io.BytesIO(master) if isinstance(master, bytes) else master
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as kit:
        video = entry("01-video.mp4")
        video.file_size = size
        with kit.open(video, "w") as member:
            if digest_stream(source, size, member) != digest:
                raise ValueError("La huella del máster no coincide con el dictamen")
        for name, body in files.items():
            kit.writestr(entry(name), body)
    return destination.getvalue() if output is None else None


def approved_revision(identity, *, max_revision=3):
    for revision in range(max_revision, 0, -1):
        suffix = "" if revision == 1 else f"-v{revision}"
        review = ELENA / f"{identity}{suffix}.json"
        if not review.is_file():
            if revision > 1 and (LUCAS / review.name).is_file():
                return None
            continue
        qa = json.loads(read_regular(review, LIMIT))
        job = f"{identity}-v{revision}"
        if qa["order_id"] != identity or qa["job_id"] != job:
            raise ValueError("Dictamen fuera de la orden")
        if qa["dictamen"]["decision"] != "aprobado":
            return None
        names = (f"{identity}{suffix}.json",)
        if revision == 2:
            names += (f"{identity}.json",)
        for name in names:
            source = LUCAS / name
            if source.is_file():
                render = json.loads(read_regular(source, LIMIT))
                if render["order_id"] == identity and render["job_id"] == job:
                    return render, qa
        raise ValueError("No se encontró el render de la revisión aprobada")
    return None


def approved_caption(order, render, direction, strategy):
    expected = (order["order_id"], render["source_sha256"], render["brief_sha256"])
    for receipt in (strategy, direction):
        if tuple(receipt[key] for key in ("order_id", "source_sha256", "brief_sha256")) != expected:
            raise ValueError("El caption pertenece a otro encargo")
    chosen = direction["escaleta"]["angulo"]
    angles = strategy_reply(json.dumps({"angulos": strategy["angulos"]}))
    selected = next((angle for angle in angles if angle["angulo"] == chosen), None)
    if selected is None:
        raise ValueError("No se encontró el caption del ángulo seleccionado")
    return selected["copy"]


def load_assignments(path):
    assignments = json.loads(read_regular(path, LIMIT))
    for identity, entry in assignments.items():
        if (
            not matches(UUID, identity)
            or entry.keys() != {"project", "task", "id", "title"}
            or not all(matches(UUID, entry[k]) for k in ("project", "task", "id"))
            or not isinstance(entry["title"], str)
            or not 1 <= len(entry["title"]) <= 120
        ):
            raise ValueError("Asignación de entrega inválida")
    return assignments


def approved_master(order, render, qa, brief):
    brief_hash = hashlib.sha256(canonical(brief)).hexdigest()
    if (
        brief["order_id"] != order["order_id"]
        or brief["user_id"] != order["user_id"]
        or render["brief_sha256"] != brief_hash
        or qa["brief_sha256"] != brief_hash
        or render["source_sha256"] != order["media"]["sha256"]
        or render["job_id"] not in {order["order_id"] + f"-v{i}" for i in (1, 2, 3)}
    ):
        raise ValueError("Brief o crudo ajeno a la entrega")
    master = MEDIA / render["master"]["path"]
    expected = (
        MEDIA
        / "proyectos"
        / order["media"]["project_slug"]
        / "06_renders"
        / render["job_id"]
        / "render"
    )
    if (
        master.parent != expected
        or master.name not in {"master.mp4", "master-corrected.mp4"}
        or master.resolve(strict=True) != master
    ):
        raise ValueError("Ruta del máster inválida")
    return master


def approved_graphic(order, intake, render, qa, direction, gpu, receipt):
    identity = order["order_id"]
    job = render["job_id"]
    verdict = qa["dictamen"]
    if (
        render_format(order) != "prores4444"
        or render["order_id"] != identity
        or qa["order_id"] != identity
        or qa["job_id"] != job
        or qa["master_sha256"] != render["master"]["sha256"]
        or verdict["decision"] != "aprobado"
        or not 85 <= verdict["puntaje"] <= 100
        or verdict["reglas_duras_incumplidas"] != []
    ):
        raise ValueError("La gráfica requiere el dictamen del máster aprobado")
    direction_hash = hashlib.sha256(canonical(direction)).hexdigest()
    if not hmac.compare_digest(direction_hash, render.get("direction_sha256", "")):
        raise ValueError("La escaleta no coincide con la revisión aprobada")
    if not gpu:
        raise ValueError("Credencial del puente no disponible")
    original = direction["escaleta"]["render"]
    props = original["props"]
    video, audio = source_paths(intake)
    if intake["order"] != order or props["video_path"] != video or props["audio_path"] != audio:
        raise ValueError("Las pistas de la gráfica difieren de la ingesta")
    source = intake["audio"]
    if fingerprint(MEDIA / audio) != {k: source[k] for k in ("bytes", "sha256")}:
        raise ValueError("El audio de la gráfica cambió")
    body = {
        **original,
        "job_id": job + "-grafica",
        "format": "prores4444",
        "props": {name: value for name, value in props.items() if name != "video_path"},
    }
    result = submit_with_receipt(
        "render",
        body,
        gpu,
        receipt,
        source={
            "master": qa["master_sha256"],
            "direction": direction_hash,
            "audio": source["sha256"],
        },
    )
    media, audio = result["media"], result["audio"]
    resolution = render_resolution(order)
    if (
        (result["codec"], result["resolution"]) != ("prores_ks", resolution)
        or result["hardware_acceleration"] is not False
        or result["watermark"] is not False
        or result["audio_verified"] is not True
        or result["alpha_verified"] is not True
        or (media["width"], media["height"], media["fps"]) != (*RENDER_SIZES[resolution], 30)
        or (media["video_codec"], media["video_profile"], media["codec_tag"])
        != ("prores", "4444", "ap4h")
        or (media["audio_codec"], media["sample_rate"]) != ("pcm_s24le", 48000)
        or media["pixel_format"] not in {"yuva444p10le", "yuva444p12le"}
        or (media["color_space"], media["color_transfer"], media["color_primaries"])
        != ("bt709", "bt709", "bt709")
        or not abs(media["duration_seconds"] - qa["mediciones"]["duration"]) <= 0.1
        or not -15 <= audio["integrated_lufs"] <= -13
        or not -120 <= audio["true_peak_dbtp"] <= -1
    ):
        raise ValueError("La gráfica ProRes está fuera del contrato de entrega")
    [graphic] = result["files"]
    prefix = f"proyectos/{order['media']['project_slug']}/06_renders/{body['job_id']}/render"
    verify_file(graphic, {f"{prefix}/master.mov", f"{prefix}/master-corrected.mov"})
    return graphic


def stage_package(order, master, render, qa, brief, *, caption=None, expected=None):
    slug = order["media"]["project_slug"]
    project = MEDIA / "proyectos" / slug
    if project.resolve(strict=True) != project or not project.is_dir():
        raise ValueError("Directorio de proyecto inválido")
    directory = project / "05_revision"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.resolve(strict=True) != directory:
        raise ValueError("Directorio de entrega inválido")
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".kit-studio-")
    try:
        with os.fdopen(fd, "w+b") as zip_file, open_media(master, MAX_MASTER) as (stream, size):
            if render["master"]["bytes"] != size:
                raise ValueError("El tamaño del máster cambió")
            package(order["order_id"], stream, render, qa, brief, caption=caption, output=zip_file)
            length = zip_file.tell()
            if length > MAX_KIT:
                raise ValueError("Kit fuera del tamaño admitido")
            zip_file.flush()
            os.fsync(zip_file.fileno())
            zip_file.seek(0)
            digest = digest_stream(zip_file, length)
        if expected is not None and digest != expected:
            raise ValueError("La huella del kit difiere del manifiesto aprobado")
        destination = directory / f"kit-studio-{digest}.zip"
        try:
            os.link(temporary, destination)
        except FileExistsError:
            with open_media(destination, MAX_KIT) as (existing, size):
                if size != length or digest_stream(existing, size) != digest:
                    raise ValueError("Kit existente con contenido distinto") from None
        return destination.relative_to(MEDIA).as_posix(), digest, length
    finally:
        os.unlink(temporary)


def deliver_pending_order(identity, entry, profile, dispatch, target, gpu):
    from kitsu_review import attach_delivery_zip, attach_preview

    intake = json.loads(read_regular(JOURNAL / target.name, LIMIT))
    order = intake["order"]
    validate(order, identity)
    if not eligible(order, profile):
        return False
    max_revision = 3 if is_test_order(order, profile) else qa_rounds(order, profile)
    if not (revision := approved_revision(identity, max_revision=max_revision)):
        return False
    if not (brief := lookup(order, dispatch)):
        return False
    render, qa = revision
    master = approved_master(order, render, qa, brief)
    direction = json.loads(read_regular(VALERIA / target.name, LIMIT))
    strategy = json.loads(read_regular(MATEO / target.name, LIMIT))
    caption = approved_caption(order, render, direction, strategy)
    relative, digest, length = stage_package(order, master, render, qa, brief, caption=caption)
    slug = order["media"]["project_slug"]
    project = entry["project"]
    os.environ["AGENCY_MEDIA_PROJECTS"] = slug
    os.environ["KITSU_REVIEW_PROJECTS"] = json.dumps({project: slug})
    task = entry["task"]
    preview_args = (
        project,
        task,
        1,
        master.relative_to(MEDIA).as_posix(),
        render["master"]["sha256"],
    )
    extra = {}
    if render_format(order) == "prores4444":
        leaf = revision_leaf(identity, int(render["job_id"][-1]))
        approved_direction = json.loads(read_regular(VALERIA / leaf, LIMIT))
        # El MP4 aprobado puede preparar su vista previa mientras el puente genera el MOV.
        with ThreadPoolExecutor(max_workers=1) as executor:
            preview = executor.submit(attach_preview, *preview_args)
            extra["prores"] = approved_graphic(
                order,
                intake,
                render,
                qa,
                approved_direction,
                gpu,
                target.with_suffix(".prores.receipt"),
            )
            preview.result()
    else:
        attach_preview(*preview_args)
    result = attach_delivery_zip(project, task, 1, relative, digest)
    if result["sha256"] != digest or result["bytes"] != length:
        raise ValueError("El adjunto de Kitsu difiere del kit aprobado")
    publish(
        target,
        canonical(
            {
                "order_id": identity,
                "kitsu": result,
                "id": entry["id"],
                "project": project,
                **extra,
            }
        )
        + b"\n",
    )
    return True


def run_pending(profile, assignments, dispatch, output, gpu=None):
    from call_stdio import ENV_BY_SERVER, runtime_settings

    os.environ.update(
        {k: v for k, v in runtime_settings().items() if k in ENV_BY_SERVER["mcp-kitsu-review"]}
    )
    state = output / "reintentos"
    state.mkdir(mode=0o700, exist_ok=True)
    if state.resolve(strict=True) != output.resolve(strict=True) / state.name:
        raise ValueError("Directorio de reintentos inválido")
    failures = 0
    with closing(sqlite3.connect(state / "entrega.sqlite3")) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS retries (
            order_id TEXT PRIMARY KEY, failures INTEGER NOT NULL, due_at INTEGER NOT NULL
        ) STRICT""")
        for identity, entry in sorted(assignments.items()):
            target = output / f"{identity}.json"
            if target.exists() or not (JOURNAL / target.name).is_file():
                continue
            retry = db.execute(
                "SELECT failures,due_at FROM retries WHERE order_id=?", (identity,)
            ).fetchone()
            if retry and retry[1] > int(time.time()):
                continue
            try:
                delivered = deliver_pending_order(identity, entry, profile, dispatch, target, gpu)
            except ORDER_ERRORS as error:
                attempts = min(retry[0] + 1, 5) if retry else 1
                delay = min(30 * 2 ** (attempts - 1), 300)
                with db:
                    db.execute(
                        """INSERT INTO retries (order_id, failures, due_at) VALUES (?, ?, ?)
                        ON CONFLICT(order_id) DO UPDATE
                        SET failures=excluded.failures,due_at=excluded.due_at""",
                        (identity, attempts, int(time.time()) + delay),
                    )
                logging.warning(
                    "Entrega pendiente para %s (%s). Reintento en %s s.",
                    identity,
                    type(error).__name__,
                    delay,
                )
                failures += 1
                continue
            if delivered:
                if retry:
                    with db:
                        db.execute("DELETE FROM retries WHERE order_id=?", (identity,))
                break
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dispatch", type=Path)
    parser.add_argument("--gpu", type=Path)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    if args.dispatch is not None:
        dispatch = read_regular(args.dispatch, 64).decode("ascii")
        if not re.fullmatch(r"[a-f0-9]{64}", dispatch):
            raise ValueError("Credencial de despacho inválida")
        gpu = read_regular(args.gpu, 256).decode("ascii") if args.gpu else None
        run_pending(profile, load_assignments(args.fixture), dispatch, args.output, gpu)
        return
    fixture = profile["fixture"]
    params = json.loads(read_regular(args.fixture, LIMIT))
    if params.keys() != {"project", "task", "brief", "kit_sha256"}:
        raise ValueError("Parámetros de entrega inválidos")
    target = args.output / (fixture + ".json")
    if target.exists():
        return
    receipt = json.loads(read_regular(JOURNAL / (fixture + ".json"), LIMIT))
    order = receipt["order"]
    validate(order, fixture)
    if order["mode"] != "sandbox" or order["plan_id"] != "free":
        raise ValueError("La orden de entrega no corresponde al piloto")
    leaf = fixture + "-v3.json"
    render = json.loads(read_regular(LUCAS / leaf, LIMIT))
    qa = json.loads(read_regular(ELENA / leaf, LIMIT))
    brief = params["brief"]
    if render["job_id"] != fixture + "-v3":
        raise ValueError("Versión de prueba inválida")
    master = approved_master(order, render, qa, brief)
    kit_path, digest, length = stage_package(
        order, master, render, qa, brief, expected=params["kit_sha256"]
    )
    from call_stdio import ENV_BY_SERVER, runtime_settings
    from kitsu_review import attach_delivery_zip

    os.environ.update(
        {k: v for k, v in runtime_settings().items() if k in ENV_BY_SERVER["mcp-kitsu-review"]}
    )
    result = attach_delivery_zip(params["project"], params["task"], 1, kit_path, digest)
    if result["sha256"] != digest or result["bytes"] != length:
        raise ValueError("El adjunto de Kitsu difiere del kit aprobado")
    publish(target, canonical({"order_id": fixture, "kitsu": result}) + b"\n")


if __name__ == "__main__":
    import sys

    try:
        runtime = Path(sys.argv.pop(1))
        if runtime.resolve(strict=True) != runtime or runtime.stat().st_mode & 0o077:
            raise ValueError("Runtime de revisión inválido")
        venv = runtime / "venv"
        if Path(sys.prefix) != venv:
            python = str(venv / "bin/python")
            os.execv(python, [python, "-E", "-s", "-B", sys.argv[0], str(runtime), *sys.argv[1:]])
        sys.path.insert(0, str(runtime / "mcp_servers"))
        main()
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        kind = str(error)
        code = kind if re.fullmatch(r"[A-Z_]{5,60}", kind) else "ENTREGA_RECHAZADA"
        raise SystemExit(code) from None
