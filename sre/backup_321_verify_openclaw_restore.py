"""Comprueba la recuperación independiente de OpenClaw desde Synology y R2."""

import argparse
import os
import sys
import time
from contextlib import closing
from datetime import date
from pathlib import Path, PurePosixPath

if __package__:
    from . import backup_321_openclaw_clock as clock
    from . import backup_321_verify_r2_restore as restore
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.append("/opt/media-platform/backup-openclaw-clock-v1")
    import backup_321_openclaw_clock as clock
    import backup_321_verify_r2_restore as restore


source = clock.source
core = clock.core
backup = source.backup
retention = restore.retention
TAG = "agency-openclaw-clock"
PREFIX = "openclaw-clock-"
FILES = ("SHA256SUMS", backup.ARCHIVE)


def read_record(path, limit=16384):
    core.private_directory(path.parent)
    raw, _ = core.read_verified(path, maximum=limit)
    return source.prepare.load_json(raw), backup.digest(raw)


def read_run(run_id, profile):
    value = run_id.removeprefix(PREFIX).removeprefix("acceptance-")
    core.require(
        run_id.startswith(PREFIX) and date.fromisoformat(value).isoformat() == value,
        "CORRIDA_OPENCLAW",
    )
    root = clock.ROOT
    journal = root / "journal" / run_id
    capture, _ = read_record(root / "capture" / run_id / "capture.json", 8192)
    result, _ = read_record(journal / "result.json")
    manifest = result["manifest_sha256"]
    core.require(
        result.get("run_id") == run_id
        and result.get("success") is True
        and result.get("openclaw_backup_complete") is True
        and capture.keys() == source.CAPTURE_FIELDS
        and capture.get("success") is True
        and capture.get("action") == "capture"
        and capture.get("scope") == "agency-state"
        and capture.get("synthetic") is False
        and capture.get("archive_name") == backup.ARCHIVE
        and result.get("agents_in_archive") == capture.get("agents") == len(backup.AGENTS)
        and result.get("files_in_archive") == capture.get("files")
        and backup.valid_pin(manifest)
        and set(result["destinations"]) == set(core.TARGETS),
        "RECIBO_OPENCLAW",
    )
    staging = root / "staging" / run_id
    core.request_settings(
        {
            "run_id": run_id,
            "staging": str(staging),
            "manifest_sha256": manifest,
            "journal": str(journal.parent),
            "restic": profile["restic"],
            "restic_sha256": profile["restic_sha256"],
            "timeout_seconds": 120,
            "targets": profile["targets"],
        },
        "run",
    )
    core.require(
        profile["restic"] == str(retention.RESTIC)
        and profile["restic_sha256"] == retention.RESTIC_SHA
        and profile["targets"]["nas"]["repository"] == "/mnt/producciones/_backups/openclaw"
        and profile["targets"]["r2"]["repository"].endswith(
            "/studio-backups-bucket/openclaw-restic-v1"
        ),
        "PERFIL_OPENCLAW",
    )
    snapshots = {}
    for name, item in result["destinations"].items():
        child, child_sha = read_record(journal / name / "result.json")
        snapshot = item.get("snapshot_id")
        core.require(
            item.get("success") is True
            and item.get("reconciliation_required") is False
            and child_sha == item.get("receipt_sha256")
            and backup.valid_pin(snapshot)
            and child.get("success") is True
            and child.get("backup_attempted") is True
            and child.get("reconciliation_required") is False
            and child.get("destination") == name
            and child.get("scope") == "openclaw-clock"
            and child.get("run_id") == run_id
            and child.get("repository_id") == profile["targets"][name]["id"]
            and child.get("manifest_sha256") == manifest
            and child.get("snapshot_id") == snapshot,
            "RECIBO_DESTINO_OPENCLAW",
        )
        snapshots[name] = snapshot
    core.require(snapshots["nas"] != snapshots["r2"], "SNAPSHOTS_OPENCLAW")
    return staging, manifest, capture, snapshots


def read_dump(stream, name, limit):
    data = bytearray()
    with closing(stream(PurePosixPath("/") / name)) as chunks:
        for chunk in chunks:
            core.require(len(data) + len(chunk) <= limit, "ARCHIVO_OPENCLAW_COTA")
            data.extend(chunk)
    return bytes(data)


def verify_archive(stream, manifest_sha, capture):
    digest = capture["archive_sha256"]
    size = capture["archive_size"]
    core.require(
        backup.valid_pin(digest) and type(size) is int and 0 < size <= backup.MAX_FRAME,
        "RECIBO_CAPTURA_OPENCLAW",
    )
    manifest = read_dump(stream, "SHA256SUMS", 128)
    core.require(
        backup.digest(manifest) == manifest_sha
        and manifest == f"{digest}  {backup.ARCHIVE}\n".encode("ascii"),
        "MANIFIESTO_OPENCLAW",
    )
    frame = read_dump(stream, backup.ARCHIVE, size)
    core.require(len(frame) == size and backup.digest(frame) == digest, "SHA_OPENCLAW")
    inner, files = backup.unpack_tar(backup.zstd(frame, decompress=True), "agency-state")
    core.require(
        backup.digest(inner) == capture["manifest_sha256"] and len(files) == capture["files"],
        "OPENCLAW_MANIFIESTO",
    )
    for agent, model in source.prepare.AGENTS.items():
        config = source.prepare.load_json(files[f"{agent}/config.json"])
        core.require(config == {"id": agent, "model": model}, "OPENCLAW_PROYECCION")
    return {
        "agentes": len(backup.AGENTS),
        "memorias": len(files) - len(backup.AGENTS),
        "archivo_sha256": digest,
    }


def environment(profile, name):
    target = profile["targets"][name]
    password = Path(target["password_file"])
    core.private_directory(password.parent)
    core.private(password, (0o400, 0o600))
    env = {
        **core.COMMAND_ENV,
        "TZ": "UTC",
        "RESTIC_PASSWORD_FILE": str(password),
        "RESTIC_REPOSITORY": target["repository"],
    }
    if name == "r2":
        for key in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY"):
            path = Path(profile["credentials"]["R2_PLATFORM_" + key])
            core.private_directory(path.parent)
            raw, _ = core.read_verified(path, maximum=258, modes=(0o400, 0o600))
            value = raw.rstrip(b"\r\n").decode("ascii")
            core.require(
                1 <= len(value) <= 256
                and value.isprintable()
                and not any(c.isspace() for c in value)
                and value != "CHANGE_ME",
                "CREDENCIAL_R2",
            )
            env["AWS_" + key] = value
    return env


def verify_destination(profile, name, snapshot, run_id, staging, manifest, capture):
    start = time.monotonic()
    env = environment(profile, name)
    cfg = retention.run_restic(env, "cat", "config")
    core.require(
        cfg.get("id") == profile["targets"][name]["id"]
        and type(cfg.get("version")) is int
        and cfg["version"] in (1, 2),
        "REPO_ID",
    )
    entries = retention.inventory(retention.run_restic(env, "snapshots"))
    item = entries.get(snapshot)
    core.require(
        item is not None
        and item.get("hostname") == retention.HOST
        and {TAG, "run:" + run_id}.issubset(item.get("tags", ()))
        and set(item.get("paths", ())) == {str(staging / file) for file in FILES},
        "SNAPSHOT_OPENCLAW",
    )
    deadline = time.monotonic() + 120

    def stream(path):
        return restore.stream_dump(env, snapshot, path, deadline)

    result = verify_archive(stream, manifest, capture)
    return {**result, "segundos": round(time.monotonic() - start, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--solo-recibos", action="store_true")
    args = parser.parse_args()
    try:
        host = os.uname() if hasattr(os, "uname") else None
        core.require(
            host is not None
            and host.sysname == "Linux"
            and host.nodename == retention.HOST
            and os.geteuid() == os.getegid() == 0,
            "HOST_O_IDENTIDAD",
        )
        profile = clock.load_profile()
        staging, manifest, capture, snapshots = read_run(args.run_id, profile)
        if args.solo_recibos:
            report = {"snapshots": snapshots}
        else:
            report = {
                "destinos": {
                    name: verify_destination(
                        profile, name, snapshots[name], args.run_id, staging, manifest, capture
                    )
                    for name in core.TARGETS
                }
            }
        result = {"success": True, "run_id": args.run_id, "manifest_sha256": manifest, **report}
    except (*retention.COMMAND_ERRORS, AttributeError, UnicodeError) as error:
        result = {"success": False, "error": core.output_code(error)}
    print(core.encode(result).decode(), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
