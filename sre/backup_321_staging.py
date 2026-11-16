"""Ensambla las cuatro capturas verificadas en un staging privado 3-2-1."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__:
    from . import backup_321_capture_d1 as d1
    from . import backup_321_orchestrator as core
    from . import openclaw_backup as openclaw
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_capture_d1 as d1
    import backup_321_orchestrator as core
    import openclaw_backup as openclaw

PG_SERVICES = ("postgres-apps", "postgres-immich")
ZSTD_MAGIC = openclaw.ZSTD_MAGIC
MAX_AGE = timedelta(hours=24)


def is_pin(value):
    return isinstance(value, str) and core.SHA256.fullmatch(value) is not None


def is_private_path(value):
    return (
        isinstance(value, str)
        and (path := Path(value)).is_absolute()
        and path != core.ROOT
        and path.is_relative_to(core.ROOT)
        and ".." not in path.parts
    )


def validate_request(request):
    core.require(
        isinstance(request, dict)
        and set(request) == {"root", "run_id", "sources", "postgres", "d1", "openclaw"}
        and is_private_path(request["root"])
        and isinstance(request["run_id"], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", request["run_id"]),
        "SOLICITUD_STAGING",
    )
    fields = {"receipt", "sha256"}
    for service in ("postgres", "d1", "openclaw"):
        item = request[service]
        expected = fields.copy()
        if service == "openclaw":
            expected |= {"hostname", "manifest_sha256", "started_at_utc", "completed_at_utc"}
        core.require(
            isinstance(item, dict)
            and set(item) == expected
            and is_private_path(item["receipt"])
            and is_pin(item["sha256"]),
            "SOLICITUD_RECIBO",
        )
    oc = request["openclaw"]
    core.require(
        oc["hostname"] == "openclaw-ia" and is_pin(oc["manifest_sha256"]), "OPENCLAW_ORIGEN"
    )
    sources = request["sources"]
    core.require(
        isinstance(sources, dict)
        and set(sources) == {*PG_SERVICES, "d1_account_id"}
        and isinstance(sources["d1_account_id"], str)
        and re.fullmatch(r"[a-f0-9]{32}", sources["d1_account_id"]),
        "SOLICITUD_FUENTES",
    )
    for service in PG_SERVICES:
        row = sources[service]
        core.require(
            isinstance(row, dict)
            and set(row) == {"cid", "image", "pgdata"}
            and is_pin(row["cid"])
            and isinstance(row["image"], str)
            and re.fullmatch(r"sha256:[a-f0-9]{64}", row["image"])
            and is_private_path(row["pgdata"]),
            "SOLICITUD_POSTGRES",
        )
    core.require(
        all(
            sources[PG_SERVICES[0]][key] != sources[PG_SERVICES[1]][key]
            for key in ("cid", "pgdata")
        ),
        "POSTGRES_FUENTES_DISTINTAS",
    )


def read_receipt(spec, run_id):
    path = Path(spec["receipt"])
    core.private_directory(path.parent)
    core.require(path.parent.name == run_id, "RECIBO_CORRIDA")
    raw, _ = core.read_verified(path, maximum=32768)
    core.require(hashlib.sha256(raw).hexdigest() == spec["sha256"], "SHA_RECIBO")
    value = json.loads(raw, object_pairs_hook=core.unique_fields)
    core.require(isinstance(value, dict) and value.get("success") is True, "RECIBO_FALLO")
    return path.parent, value


def receipt_window(record, now):
    try:
        start = datetime.fromisoformat(record["started_at_utc"])
        end = datetime.fromisoformat(record["completed_at_utc"])
        valid = (
            start.utcoffset() == end.utcoffset() == timedelta(0)
            and now - MAX_AGE <= start <= end <= now
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("VENTANA_CAPTURA") from error
    core.require(valid, "VENTANA_CAPTURA")
    return start, end


def entry(row, path, window):
    core.require(
        row.get("path") == str(path)
        and is_pin(row.get("sha256"))
        and type(row.get("bytes")) is int
        and 0 < row["bytes"] <= core.MAX_STAGING_BYTES,
        "RECIBO_PAYLOAD",
    )
    return {
        "path": path,
        "sha256": row["sha256"],
        "bytes": row["bytes"],
        "started_at_utc": window[0].isoformat(),
        "completed_at_utc": window[1].isoformat(),
    }


def read_postgres(request, now):
    parent, receipt = read_receipt(request["postgres"], request["run_id"])
    core.require(receipt.get("scope") == "production", "POSTGRES_ALCANCE")
    captures = receipt.get("captures")
    core.require(
        receipt.get("step") == "captured"
        and receipt.get("run_id") == request["run_id"]
        and receipt.get("run_directory") == str(parent)
        and receipt.get("backup_set_complete") is False
        and isinstance(captures, dict)
        and set(captures) == set(PG_SERVICES),
        "POSTGRES_RECIBO",
    )
    payloads = {}
    for service, filename in zip(
        PG_SERVICES, ("postgres.sql.zst", "postgres-immich.sql.zst"), strict=True
    ):
        row = captures[service]
        source = row.get("source") if isinstance(row, dict) else None
        expected = request["sources"][service]
        staging_name = service + ".sql.zst"
        core.require(
            isinstance(source, dict)
            and all(source.get(key) == value for key, value in expected.items())
            and row.get("cid") == expected["cid"]
            and type(source.get("pid")) is int
            and source["pid"] > 0
            and isinstance(source.get("started"), str)
            and row.get("service") == service
            and row.get("staging_name") == staging_name
            and is_pin(row.get("receipt_sha256")),
            "POSTGRES_FUENTE",
        )
        window = receipt_window(row, now)
        try:
            stable_epoch = datetime.fromisoformat(source["started"]) <= window[0]
        except (TypeError, ValueError) as error:
            raise ValueError("POSTGRES_EPOCA") from error
        core.require(stable_epoch, "POSTGRES_EPOCA")
        payloads[staging_name] = entry(row, parent / service / filename, window)
    return payloads


def read_d1(request, now):
    parent, row = read_receipt(request["d1"], request["run_id"])
    core.require(
        row.get("step") == "captured"
        and row.get("run_id") == request["run_id"]
        and row.get("run_directory") == str(parent)
        and row.get("backup_set_complete") is False
        and row.get("account_id") == request["sources"]["d1_account_id"]
        and row.get("database_id") == d1.DATABASE_ID
        and row.get("resource") == d1.d1.RESOURCE
        and row.get("staging_name") == d1.STAGING_NAME
        and row.get("owner_verified") is True
        and row.get("sqlite_integrity_verified") is True
        and row.get("cloud_restore_verified") is False
        and type(row.get("sql_bytes")) is int
        and 0 < row["sql_bytes"] <= d1.d1.MAX_SQL
        and is_pin(row.get("sql_sha256"))
        and is_pin(row.get("export_receipt_sha256")),
        "D1_RECIBO",
    )
    result = entry(row, parent / d1.STAGING_NAME, receipt_window(row, now))
    core.require(result["bytes"] <= d1.MAX_FRAME, "D1_COTA_FRAME")
    return result


def read_openclaw(request, now):
    spec = request["openclaw"]
    parent, row = read_receipt(spec, request["run_id"])
    core.require(
        row.get("action") == "capture"
        and row.get("scope") == "agency-state"
        and row.get("synthetic") is False
        and row.get("source_attestation") == "external-staging-manifest"
        and row.get("agents") == len(openclaw.AGENTS)
        and row.get("archive_name") == openclaw.ARCHIVE
        and row.get("manifest_sha256") == spec["manifest_sha256"]
        and type(row.get("files")) is int
        and len(openclaw.AGENTS) <= row["files"] <= openclaw.MAX_FILES,
        "OPENCLAW_ALCANCE",
    )
    path = parent / openclaw.ARCHIVE
    result = entry(
        {"path": str(path), "bytes": row.get("archive_size"), "sha256": row.get("archive_sha256")},
        path,
        receipt_window(spec, now),
    )
    core.require(result["bytes"] <= openclaw.MAX_FRAME, "OPENCLAW_COTA_FRAME")
    return {**result, "manifest_sha256": row["manifest_sha256"], "files": row["files"]}


def copy_payload(source, destination):
    path = source["path"]
    core.private_directory(path.parent)
    expected = core.identity(core.private(path, (0o600,)))
    core.require(expected[6] == source["bytes"], "TAMANO_PAYLOAD")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    remaining = source["bytes"]
    with os.fdopen(descriptor, "rb") as original:
        core.require(core.identity(os.fstat(descriptor)) == expected, "PAYLOAD_CAMBIO")
        with destination.open("xb") as copied:
            header = original.read(len(ZSTD_MAGIC))
            core.require(header == ZSTD_MAGIC, "PAYLOAD_ZSTD")
            digest.update(header)
            copied.write(header)
            remaining -= len(header)
            while remaining > 0 and (block := original.read(min(remaining, 1024**2))):
                digest.update(block)
                copied.write(block)
                remaining -= len(block)
            core.require(remaining == 0 and not original.read(1), "TAMANO_PAYLOAD")
            core.require(
                core.identity(os.fstat(descriptor)) == core.identity(path.lstat()) == expected,
                "PAYLOAD_CAMBIO",
            )
            core.require(digest.hexdigest() == source["sha256"], "SHA_PAYLOAD")
            copied.flush()
            os.fsync(copied.fileno())


def verify_openclaw(payload, source):
    frame, _ = core.read_verified(payload, maximum=openclaw.MAX_FRAME)
    try:
        manifest, files = openclaw.unpack_tar(openclaw.zstd(frame, decompress=True), "agency-state")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError("OPENCLAW_TAR") from error
    core.require(
        hashlib.sha256(manifest).hexdigest() == source["manifest_sha256"]
        and len(files) == source["files"],
        "OPENCLAW_TAR",
    )


def assemble(request, out):
    root = Path(request["root"])
    core.private_directory(root)
    run = root / request["run_id"]
    with core.exclusive():
        if os.path.lexists(run):
            raise FileExistsError("CORRIDA_EXISTENTE")
        now = datetime.now(UTC)
        payloads = read_postgres(request, now)
        payloads[d1.STAGING_NAME] = read_d1(request, now)
        payloads[openclaw.ARCHIVE] = read_openclaw(request, now)
        total = sum(item["bytes"] for item in payloads.values())
        core.require(total <= core.MAX_STAGING_BYTES, "STAGING_COTA")
        disk = os.statvfs(root)
        core.require(
            disk.f_frsize * disk.f_bavail >= total + core.MIN_JOURNAL_FREE_BYTES,
            "STAGING_DISCO",
        )
        run.mkdir(mode=0o700)
        core.sync_directory(root)
        out.update(run_id=request["run_id"], step="copy")
        core.write_record(
            run / "intent.json",
            {
                "run_id": request["run_id"],
                "provenance": {
                    key: request[key] for key in ("sources", "postgres", "d1", "openclaw")
                },
                "bytes": total,
            },
        )
        try:
            pending = run / ".staging.partial"
            pending.mkdir(mode=0o700)
            for name in core.FILES:
                copy_payload(payloads[name], pending / name)
            out["step"] = "verify"
            verify_openclaw(pending / openclaw.ARCHIVE, payloads[openclaw.ARCHIVE])
            manifest = "".join(
                f"{payloads[name]['sha256']}  {name}\n" for name in core.FILES
            ).encode()
            manifest_pin = hashlib.sha256(manifest).hexdigest()
            with (pending / "SHA256SUMS").open("xb") as output:
                output.write(manifest)
                output.flush()
                os.fsync(output.fileno())
            core.sync_directory(pending)
            identities = core.check_staging(pending, manifest_pin)
            staging = run / "staging"
            pending.rename(staging)
            core.sync_directory(run)
            core.check_unchanged(staging, identities)
            out.update(
                success=True,
                step="staged",
                staging=str(staging),
                manifest_sha256=manifest_pin,
                bytes=total,
                files={
                    name: {key: value for key, value in item.items() if key != "path"}
                    for name, item in payloads.items()
                },
            )
        except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            out["error"] = core.output_code(error)
        core.write_record(run / "result.json", out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("assemble",))
    parser.parse_args()
    out = {"success": False, "backup_set_complete": False, "restoration_verified": False}
    try:
        host = os.uname() if hasattr(os, "uname") else None
        core.require(
            host is not None
            and host.sysname == "Linux"
            and host.nodename == "vm-media-01"
            and os.geteuid() == os.getegid() == 0,
            "HOST_O_IDENTIDAD",
        )
        os.umask(0o077)
        signal.signal(signal.SIGTERM, core.interrupted)
        signal.signal(signal.SIGHUP, core.interrupted)
        raw = sys.stdin.buffer.read(32769)
        core.require(0 < len(raw) <= 32768, "SOLICITUD_COTA")
        request = json.loads(raw, object_pairs_hook=core.unique_fields)
        validate_request(request)
        assemble(request, out)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        out.update(success=False, error=core.output_code(error))
    print(core.encode(out).decode(), flush=True)
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
