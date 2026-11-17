"""Ejecuta en el puente GPU las escaletas acreditadas del piloto."""

import argparse
import hashlib
import http.client
import json
import logging
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from production_elena import audit_preview
from production_handoff import (
    RENDER_SIZES,
    canonical,
    eligible,
    load_profile,
    publish,
    qa_rounds,
    read_regular,
    render_plan,
    revision_from_stem,
    validate,
)
from production_inbox import MEDIA, render_format, render_resolution
from production_intake import fingerprint
from production_mateo import ORDER_ERRORS, lookup
from production_sofia import JOURNAL, LIMIT
from production_valeria import source_paths

VALERIA = Path("/var/lib/agency-production-valeria")


def render_payload(body):
    return json.dumps(body, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()


def submit(kind, body, token):
    client = http.client.HTTPConnection("192.168.0.6", 8090, timeout=1205)
    try:
        client.request(
            "POST",
            "/" + kind,
            render_payload(body),
            {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": "Bearer " + token,
            },
        )
        with client.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"Puente GPU devolvió HTTP {response.status}")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("Respuesta del puente fuera del límite")
            return json.loads(raw)
    finally:
        client.close()


def submit_with_receipt(kind, body, token, journal, *, source=None):
    identity = hashlib.sha256(canonical({"request": body, "source": source})).hexdigest()
    try:
        row = json.loads(read_regular(journal, 131072))
    except FileNotFoundError:
        result = submit(kind, body, token)
        publish(journal, canonical({"request_sha256": identity, "result": result}) + b"\n")
        return result
    if row["request_sha256"] != identity:
        raise ValueError("El recibo del puente no corresponde a esta solicitud")
    return row["result"]


def verify_file(item, expected_paths):
    if item["path"] not in expected_paths:
        raise ValueError("Salida del puente fuera del proyecto")
    path = MEDIA / item["path"]
    # La caché de directorios de NFS puede tardar hasta 60 s en ver una escritura SMB.
    for _ in range(36):
        try:
            if path.resolve(strict=True) != path:
                raise ValueError("Ruta de salida inválida")
            expected = {k: item[k] for k in ("bytes", "sha256")}
            if fingerprint(path) != expected:
                raise ValueError("La salida no coincide con el puente")
            return
        except FileNotFoundError:
            time.sleep(2)
    raise ValueError("La salida todavía no aparece en NFS")


def verify_stills(data, body, frames):
    if (
        (data["width"], data["height"], data["frames"])
        != (*RENDER_SIZES[body["resolution"]], frames)
        or data["watermark"] is not (body["plan"] == "Free")
        or len(data["files"]) != len(frames)
    ):
        raise ValueError("Capturas fuera del contrato del plan")
    prefix = f"proyectos/{body['project_id']}/05_revision/{body['job_id']}/stills"
    for frame, item in zip(frames, data["files"], strict=True):
        verify_file(item, {f"{prefix}/still-{frame}.png"})


def verify_mp4(result, body):
    audio, media = result["audio"], result["media"]
    if (
        (result["codec"], result["hardware_acceleration"], result["audio_verified"])
        != ("h264_nvenc", True, True)
        or result["watermark"] is not (body["plan"] == "Free")
        or (media["width"], media["height"], media["fps"])
        != (*RENDER_SIZES[body["resolution"]], 30)
        or not -15 <= audio["integrated_lufs"] <= -13
        or not -120 <= audio["true_peak_dbtp"] <= -1
    ):
        raise ValueError("Máster fuera del contrato del plan")
    [master] = result["files"]
    prefix = f"proyectos/{body['project_id']}/06_renders/{body['job_id']}/render"
    verify_file(master, {f"{prefix}/master.mp4", f"{prefix}/master-corrected.mp4"})
    return master


def run(order, receipt, edl, token, revision, target):
    job = order["media"]
    plan = render_plan(order)
    resolution = render_resolution(order)
    watermark = plan == "Free"
    if (
        edl["order_id"] != order["order_id"]
        or edl["source_sha256"] != job["sha256"]
        or edl["escaleta"]["estado"] != "lista_para_stills"
    ):
        raise ValueError("Escaleta fuera de la orden verificada")
    original = edl["escaleta"]["render"]
    if (
        original["project_id"],
        original["job_id"],
        original["plan"],
        original["composition"],
        original["format"],
        original["resolution"],
    ) != (
        job["project_slug"],
        order["order_id"] + "-v1",
        plan,
        "AgencyVertical",
        "mp4",
        resolution,
    ):
        raise ValueError("Escaleta fuera del contrato del plan")
    video, audio = source_paths(receipt)
    props = original["props"]
    reel = order.get("output_edit") == "reel"
    if reel != ("scenes" in props):
        raise ValueError("La escaleta no corresponde al montaje solicitado")
    if props["video_path"] != video or props["audio_path"] != audio:
        raise ValueError("Las pistas no coinciden con la ingesta")
    if fingerprint(MEDIA / video) != {k: job[k] for k in ("sha256", "bytes")} or fingerprint(
        MEDIA / audio
    ) != {k: receipt["audio"][k] for k in ("sha256", "bytes")}:
        raise ValueError("Las huellas de ingesta cambiaron")
    if type(revision) is not int or revision not in {1, 2, 3}:
        raise ValueError("Revisión de render inválida")
    body = {**original, "job_id": f"{order['order_id']}-v{revision}"}
    count = int(props["duration_seconds"] * 30 + 0.5)
    if not 3 <= count <= 18000:
        raise ValueError("Duración fuera del contrato de render")
    frames = [0, count // 2, count - 1]
    source = {
        "video": job["sha256"],
        "audio": receipt["audio"]["sha256"],
        "brief": edl["brief_sha256"],
    }
    record = {
        "order_id": order["order_id"],
        "source_sha256": job["sha256"],
        "brief_sha256": edl["brief_sha256"],
        "job_id": body["job_id"],
    }
    if reel:
        preview = {**body, "job_id": body["job_id"] + "-preview", "resolution": "1080p"}
        draft = submit_with_receipt(
            "render", preview, token, target.with_suffix(".preview.receipt"), source=source
        )
        asset = verify_mp4(draft, preview)
        report = target.with_suffix(".preview-review.receipt")
        verdict = audit_preview(order, edl, body["job_id"], asset, report)["dictamen"]["decision"]
        record.update(
            preview=asset,
            preview_review_sha256=hashlib.sha256(read_regular(report, LIMIT)).hexdigest(),
        )
        if verdict == "rechazado":
            publish(target, canonical(record) + b"\n")
            return
        if verdict != "aprobado":
            raise ValueError("Estado de revisión previa inválido")
    stills = submit_with_receipt(
        "stills",
        {**body, "frames": frames},
        token,
        target.with_name(target.stem + ".stills.receipt"),
        source=source,
    )
    verify_stills(stills, body, frames)
    result = submit_with_receipt(
        "render",
        body,
        token,
        target.with_name(target.stem + ".render.receipt"),
        source=source,
    )
    master = verify_mp4(result, body)
    direction = {}
    if render_format(order) == "prores4444":
        direction["direction_sha256"] = hashlib.sha256(canonical(edl)).hexdigest()
    publish(
        target,
        canonical(
            {
                **record,
                "master": master,
                "codec": result["codec"],
                "resolution": resolution,
                "watermark": watermark,
                "audio": result["audio"],
                "frames": stills["files"],
                **direction,
            }
        )
        + b"\n",
    )


def render_pending(path, target, version, profile, token, dispatch):
    identity, revision = version
    intake = JOURNAL / f"{identity}.json"
    if not intake.is_file():
        return False
    receipt = json.loads(read_regular(intake, LIMIT))
    order = receipt["order"]
    validate(order, identity)
    if (
        not eligible(order, profile)
        or revision > qa_rounds(order, profile)
        or not (brief := lookup(order, dispatch))
    ):
        return False
    edl = json.loads(read_regular(path, LIMIT))
    if edl["escaleta"]["estado"] != "lista_para_stills":
        return False
    if edl["brief_sha256"] != hashlib.sha256(canonical(brief)).hexdigest():
        raise ValueError("La escaleta no corresponde al brief vigente")
    run(order, receipt, edl, token, revision, target)
    return True


def run_pending(profile, token, dispatch, output):
    # Elena vigila cambios del directorio principal, no los de la base de reintentos.
    state = output / "reintentos"
    state.mkdir(mode=0o700, exist_ok=True)
    if state.resolve(strict=True) != state:
        raise ValueError("Directorio de reintentos de render inválido")
    with closing(sqlite3.connect(state / "lucas.sqlite3")) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS retries (
                revision_id TEXT PRIMARY KEY, failures INTEGER NOT NULL, due_at INTEGER NOT NULL
            ) STRICT"""
        )
        for path in sorted(VALERIA.glob("*.json")):
            if not (version := revision_from_stem(path.stem)):
                continue
            target = output / path.name
            previous = db.execute(
                "SELECT failures,due_at FROM retries WHERE revision_id=?", (path.stem,)
            ).fetchone()
            if target.exists():
                if previous:
                    with db:
                        db.execute("DELETE FROM retries WHERE revision_id=?", (path.stem,))
                continue
            if previous and previous[1] > int(time.time()):
                continue
            try:
                rendered = render_pending(path, target, version, profile, token, dispatch)
            except (*ORDER_ERRORS, subprocess.SubprocessError):
                failures = min(previous[0] + 1, 4) if previous else 1
                # La primera espera supera los 55 s del temporizador de Lucas.
                due_at = int(time.time()) + min(60 * 2 ** (failures - 1), 300)
                with db:
                    db.execute(
                        """INSERT INTO retries (revision_id, failures, due_at) VALUES (?, ?, ?)
                        ON CONFLICT(revision_id) DO UPDATE
                        SET failures=excluded.failures, due_at=excluded.due_at""",
                        (path.stem, failures, due_at),
                    )
                raise
            if previous:
                with db:
                    db.execute("DELETE FROM retries WHERE revision_id=?", (path.stem,))
            if rendered:
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("token", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--revision", type=int, choices=(2, 3), default=2)
    parser.add_argument("--dispatch", type=Path)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    token = read_regular(args.token, 1024).decode("ascii").strip()
    if not re.fullmatch(r"[!-~]{32,1024}", token):
        raise ValueError("Credencial del puente inválida")
    if args.dispatch is not None:
        dispatch = read_regular(args.dispatch, 64).decode("ascii")
        if not re.fullmatch(r"[a-f0-9]{64}", dispatch):
            raise ValueError("Credencial de despacho inválida")
        run_pending(profile, token, dispatch, args.output)
        return
    fixture = profile["fixture"]
    source = fixture + ".json"
    target = args.output / (source if args.revision == 2 else fixture + "-v3.json")
    if target.exists():
        return
    receipt = json.loads(read_regular(JOURNAL / source, LIMIT))
    order = receipt["order"]
    validate(order, fixture)
    edl = json.loads(read_regular(VALERIA / source, LIMIT))
    run(order, receipt, edl, token, args.revision, target)


if __name__ == "__main__":
    try:
        main()
    except (*ORDER_ERRORS, subprocess.SubprocessError, sqlite3.Error) as error:
        if type(error) is ValueError:
            logging.error("Falló el render de Lucas. %s", error)
        else:
            logging.error("Falló el render de Lucas (%s)", type(error).__name__)
        raise SystemExit(1) from None
