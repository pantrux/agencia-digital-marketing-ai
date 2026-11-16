"""Coteja la sonoridad de los másteres retenidos en Synology."""

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

if __package__:
    from .audit_studio_kit_retention import file_identity
    from .audit_studio_production_latency import (
        PORTAL_COHORTS,
        UNATTRIBUTED,
        WINDOW,
        cohort,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from audit_studio_kit_retention import file_identity
    from audit_studio_production_latency import (
        PORTAL_COHORTS,
        UNATTRIBUTED,
        WINDOW,
        cohort,
    )

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ".recibo-puente-v1.json"
SIGNING_PREFIX = b"agencia-puente-gpu:recibo:v1\n"
SHA = re.compile(r"[a-f0-9]{64}\Z")
PROJECT = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
JOB = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
MASTER = re.compile(r"master(?:-corrected)?\.(mp4|mov)\Z")
LOUDNESS_JSON = re.compile(r'\{[^{}]*"input_i"[^{}]*\}')
MAX_RECEIPT = 65536
ORIGIN_PAGE_SIZE = 32
ORIGIN_QUERY = """
    SELECT o.project_slug, o.portal_started_at,
           COALESCE(o.provider_mode,'production') AS provider_mode,
           lower(u.email) LIKE '%.invalid' AS reserved_test_domain
    FROM orders o JOIN users u ON u.id=o.user_id
    WHERE o.kind='production' AND o.project_slug IN ({})
"""


def plain(path, is_expected_type):
    info = path.lstat()
    if not is_expected_type(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Ruta ajena al árbol de másteres")
    return info


def directory(path):
    try:
        plain(path, stat.S_ISDIR)
        return True
    except (FileNotFoundError, ValueError):
        return False


def audio_accepted(lufs, peak):
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (lufs, peak)):
        raise ValueError("Sonoridad no numérica")
    return -15 <= lufs <= -13 and peak <= -1


def render_folders(nas):
    projects = nas / "proyectos"
    plain(nas, stat.S_ISDIR)
    plain(projects, stat.S_ISDIR)
    for project in projects.iterdir():
        rendered = project / "06_renders"
        if not PROJECT.fullmatch(project.name) or not directory(project) or not directory(rendered):
            continue
        for job in rendered.iterdir():
            folder = job / "render"
            if JOB.fullmatch(job.name) and directory(job) and directory(folder):
                yield project.name, folder


def read_master(nas, folder, key, now):
    receipt = folder / RECEIPT
    before = plain(receipt, stat.S_ISREG)
    if not now - WINDOW <= before.st_mtime <= now:
        return None
    if before.st_nlink != 1 or not 0 < before.st_size <= MAX_RECEIPT:
        raise ValueError("Recibo fuera del contrato")
    with receipt.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_RECEIPT + 1)
        after = os.fstat(stream.fileno())
    if (
        len(raw) != before.st_size
        or (opened.st_ino, opened.st_mtime_ns) != (before.st_ino, before.st_mtime_ns)
        or (after.st_ino, after.st_mtime_ns) != (opened.st_ino, opened.st_mtime_ns)
    ):
        raise ValueError("El recibo cambió durante la lectura")
    data = json.loads(raw)
    body, signature = data["body"].encode(), data["signature"]
    expected = hmac.digest(key, SIGNING_PREFIX + body, hashlib.sha256).hex()
    if not SHA.fullmatch(signature) or not hmac.compare_digest(signature, expected):
        raise ValueError("Firma del recibo inválida")
    decoded = json.loads(body)
    if not SHA.fullmatch(decoded["request_sha256"]):
        raise ValueError("Identidad de solicitud inválida")
    result = decoded["result"]
    nvenc = result["codec"] == "h264_nvenc" and result["hardware_acceleration"] is True
    prores = result["codec"] == "prores_ks" and result["hardware_acceleration"] is False
    if not (nvenc or prores) or result["audio_verified"] is not True:
        raise ValueError("Clase de máster sin medición")
    (artifact,) = result["files"]
    relative = folder.relative_to(nas).as_posix()
    match = re.fullmatch(
        r"master(?:-corrected)?\." + ("mp4" if nvenc else "mov"), artifact["path"].split("/")[-1]
    )
    if (
        match is None
        or artifact["path"] != f"{relative}/{match.group(0)}"
        or not SHA.fullmatch(artifact["sha256"])
        or type(artifact["bytes"]) is not int
        or artifact["bytes"] <= 0
    ):
        raise ValueError("Máster fuera del contrato")
    media = plain(folder / match.group(0), stat.S_ISREG)
    if media.st_nlink != 1 or media.st_size != artifact["bytes"]:
        raise ValueError("Máster incompatible con el recibo")
    audio = result["audio"]
    return ("nvenc" if nvenc else "prores_4444"), audio_accepted(
        audio["integrated_lufs"], audio["true_peak_dbtp"]
    )


def verified_ffmpeg(values):
    executable = Path(values["GPU_FFMPEG_PATH"])
    expected = values["GPU_FFMPEG_SHA256"].lower()
    if not executable.is_absolute() or not SHA.fullmatch(expected):
        raise ValueError("FFmpeg carece de anclaje de integridad")
    with executable.open("rb") as binary:
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise ValueError("FFmpeg difiere de la huella configurada")
    return executable


def measure_audio(ffmpeg, media):
    process = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "file",
            "-f",
            "mov",
            "-i",
            str(media),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=True,
    )
    blocks = LOUDNESS_JSON.findall(process.stderr)
    if not blocks:
        raise ValueError("FFmpeg no produjo una medición")
    audio = json.loads(blocks[-1])
    return float(audio["input_i"]), float(audio["input_tp"])


def summarize(values):
    total, accepted = len(values), sum(values)
    return {
        "medidos": total,
        "conformes": accepted,
        "fuera_de_objetivo": total - accepted,
        "porcentaje_observado": round(100 * accepted / total, 3) if total else None,
    }


def read_origins(query, projects):
    origins = {}
    projects = sorted(projects)
    for cursor in range(0, len(projects), ORIGIN_PAGE_SIZE):
        batch = projects[cursor : cursor + ORIGIN_PAGE_SIZE]
        sql = ORIGIN_QUERY.format(",".join("?" for _ in batch))
        for row in query(sql, batch):
            project = row["project_slug"]
            if project not in batch or project in origins:
                raise ValueError("RESULTADO_ORIGEN_D1")
            origins[project] = cohort(row)
    return origins


def audit(nas, token, now, origins_for, *, measure_unsigned=None):
    key = token.encode()
    totals = {"nvenc": [], "prores_4444": []}
    cohorts = {name: [] for name in (*PORTAL_COHORTS, UNATTRIBUTED)}
    nvenc_by_project = {}
    unsigned_folders = []
    invalid = missing = 0
    for project, folder in render_folders(nas):
        try:
            (folder / RECEIPT).lstat()
        except FileNotFoundError:
            missing += 1
            unsigned_folders.append(folder)
            continue
        except OSError:
            invalid += 1
            continue
        try:
            sample = read_master(nas, folder, key, now)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            invalid += 1
            continue
        if sample is None:
            continue
        codec, accepted = sample
        totals[codec].append(accepted)
        if codec == "nvenc":
            nvenc_by_project.setdefault(project, []).append(accepted)
    origins = origins_for(nvenc_by_project.keys())
    for project, samples in nvenc_by_project.items():
        cohorts[origins.get(project, UNATTRIBUTED)].extend(samples)
    report = {
        "utc": datetime.fromtimestamp(now, UTC).isoformat(),
        "ventana_dias": WINDOW // 86400,
        "objetivo_porcentaje_nvenc": 99.5,
        "recibos_invalidos": invalid,
        "directorios_sin_recibo": missing,
        "muestra": {name: summarize(values) for name, values in totals.items()},
        "cohortes_nvenc": {name: summarize(values) for name, values in cohorts.items()},
    }
    if measure_unsigned is not None:
        report["historico_sin_recibo"] = audit_unsigned(unsigned_folders, now, measure_unsigned)
    return report


def audit_unsigned(folders, now, measure):
    totals = {"mp4": [], "mov": []}
    candidates = ambiguous = unmeasured = 0
    for folder in folders:
        masters = [path for path in folder.iterdir() if MASTER.fullmatch(path.name)]
        if len(masters) != 1:
            ambiguous += 1
            continue
        (media,) = masters
        try:
            baseline = plain(media, stat.S_ISREG)
            if baseline.st_nlink != 1 or baseline.st_size <= 0:
                raise ValueError("Archivo ajeno al contrato de máster")
        except (OSError, ValueError):
            ambiguous += 1
            continue
        if not now - WINDOW <= baseline.st_mtime <= now:
            continue
        candidates += 1
        try:
            accepted = audio_accepted(*measure(media))
            if file_identity(plain(media, stat.S_ISREG)) != file_identity(baseline):
                raise ValueError("El máster cambió durante la medición")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            unmeasured += 1
            continue
        totals[media.suffix[1:]].append(accepted)
    return {
        "candidatos_en_ventana": candidates,
        "sin_master_univoco": ambiguous,
        "sin_medicion": unmeasured,
        "muestra": {name: summarize(values) for name, values in totals.items()},
    }


def main():
    from audit_studio_kit_retention import NAS
    from check_env import parse_env
    from provision_order import query_orders

    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--nas", type=Path, default=NAS)
    args.add_argument("--medir-sin-recibo", action="store_true")
    ns = args.parse_args()
    values, errors = parse_env((ROOT / ".env").read_text(encoding="utf-8-sig"))
    token = values.get("GPU_BRIDGE_BEARER_TOKEN")
    if errors or not token or token == "CHANGE_ME":
        raise ValueError("Falta la credencial de firma de recibos")
    measure = partial(measure_audio, verified_ffmpeg(values)) if ns.medir_sin_recibo else None
    now = int(datetime.now(UTC).timestamp())

    def origins_for(projects):
        return read_origins(lambda sql, params: query_orders(values, sql, params), projects)

    report = audit(ns.nas, token, now, origins_for, measure_unsigned=measure)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT / name) for name in ("sre", "scripts/ci", "docker/kitsu-zou")]
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SystemExit("Falló el cotejo de sonoridad de Studio") from None
