"""Prepara una captura nueva de studio-saas-db para la copia 3-2-1."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from . import backup_321_orchestrator as core
    from . import d1_export as d1
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_orchestrator as core
    import d1_export as d1

DATABASE_ID = "df52f4fa-1b33-40c3-a67f-002be91aeb44"
STAGING_NAME = "d1.sql.zst"
ZSTD_MAGIC = bytes.fromhex("28b52ffd")
MAX_FRAME = d1.MAX_SQL + 1024**2
ZSTD = Path("/usr/bin/zstd")
PRLIMIT = Path("/usr/bin/prlimit")


def validate_request(request):
    core.require(
        isinstance(request, dict)
        and set(request) == {"root", "run_id", "account_id", "database_id", "token_file"}
        and all(isinstance(value, str) for value in request.values())
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", request["run_id"])
        and re.fullmatch(r"[a-f0-9]{32}", request["account_id"]),
        "SOLICITUD_CAPTURA_D1",
    )
    core.require(request["database_id"] == DATABASE_ID, "D1_BASE_NO_ACREDITADA")


def check_tools():
    for path in (ZSTD, PRLIMIT):
        core.private(path.resolve(strict=True), (0o555, 0o755))
        core.require(os.access(path, os.X_OK), "D1_ZSTD_NO_DISPONIBLE")


def zstd(data, arguments, maximum):
    with os.fdopen(os.memfd_create("d1-zstd", os.MFD_CLOEXEC), "w+b") as output:
        subprocess.run(
            [str(PRLIMIT), f"--fsize={maximum}:{maximum}", "--", str(ZSTD), *arguments],
            input=data,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env=core.COMMAND_ENV,
            timeout=30,
            check=True,
        )
        output.seek(0)
        result = output.read(maximum + 1)
    core.require(0 < len(result) <= maximum, "D1_ZSTD_COTA")
    return result


def compress_sql(sql):
    frame = zstd(sql, ("-q", "-T1", "--check", "-c"), MAX_FRAME)
    core.require(
        frame.startswith(ZSTD_MAGIC) and zstd(frame, ("-q", "-M64MB", "-d", "-c"), len(sql)) == sql,
        "D1_ZSTD_INTEGRIDAD",
    )
    return frame


def validate_export(stage, result, started, observed):
    path = stage / d1.OUTPUT_NAME
    receipt_path = stage / d1.RECEIPT_NAME
    raw, _ = core.read_verified(receipt_path, maximum=8192)
    receipt = json.loads(raw, object_pairs_hook=core.unique_fields)
    core.require(
        isinstance(receipt, dict)
        and receipt == result
        and receipt.get("success") is True
        and receipt.get("resource") == d1.RESOURCE
        and receipt.get("database_id") == DATABASE_ID
        and receipt.get("owner_verified") is True
        and receipt.get("sqlite_integrity_verified") is True
        and receipt.get("cloud_restore_verified") is False
        and receipt.get("output_path") == str(path)
        and receipt.get("receipt_path") == str(receipt_path)
        and type(receipt.get("bytes")) is int
        and isinstance(receipt.get("sha256"), str)
        and core.SHA256.fullmatch(receipt["sha256"]),
        "D1_RECIBO_EXPORTACION",
    )
    try:
        began = datetime.fromisoformat(receipt["operation_started_at_utc"])
        finished = datetime.fromisoformat(receipt["completed_at_utc"])
        within_window = started <= began <= finished <= observed
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("D1_VENTANA_CAPTURA") from error
    core.require(within_window, "D1_VENTANA_CAPTURA")
    sql, _ = core.read_verified(path, maximum=d1.MAX_SQL)
    core.require(
        len(sql) == receipt["bytes"] and hashlib.sha256(sql).hexdigest() == receipt["sha256"],
        "D1_SQL_SHA",
    )
    return sql, receipt, hashlib.sha256(raw).hexdigest()


def run_capture(request, out):
    root = Path(request["root"])
    token_file = Path(request["token_file"])
    core.private_directory(root)
    core.private_directory(token_file.parent)
    core.require(root != core.ROOT and not token_file.is_relative_to(root), "D1_RUTAS_CAPTURA")
    with core.exclusive():
        disk = os.statvfs(root)
        core.require(
            disk.f_frsize * disk.f_bavail >= d1.MAX_SQL + MAX_FRAME + core.MIN_JOURNAL_FREE_BYTES,
            "D1_DISCO_CAPTURA",
        )
        check_tools()
        token, _ = core.read_verified(token_file, maximum=4098, modes=(0o400, 0o600))
        token = (
            token.removesuffix(b"\r\n") if token.endswith(b"\r\n") else token.removesuffix(b"\n")
        )
        run = root / request["run_id"]
        run.mkdir(mode=0o700)
        core.sync_directory(root)
        stage = run / "export"
        stage.mkdir(mode=0o700)
        out.update(run_id=request["run_id"], run_directory=str(run), step="export")
        core.write_record(
            run / "intent.json",
            {
                "run_id": request["run_id"],
                "account_id": request["account_id"],
                "database_id": DATABASE_ID,
                "resource": d1.RESOURCE,
            },
        )
        try:
            settings = d1.Settings.from_values(
                {
                    "CF_ACCOUNT_ID": request["account_id"],
                    "CF_D1_DATABASE_ID": DATABASE_ID,
                    "CF_PLATFORM_API_TOKEN": token.decode("ascii"),
                    "CF_D1_EXPORT_STAGING_PATH": str(stage),
                }
            )
            core.write_record(stage / d1.STAGE_MARKER, d1.stage_marker(settings))
            started = datetime.now(UTC)
            result = asyncio.run(d1.export_database(settings))
            sql, receipt, receipt_sha = validate_export(stage, result, started, datetime.now(UTC))
            out["step"] = "compress"
            frame = compress_sql(sql)
            payload = run / STAGING_NAME
            with payload.open("xb") as output:
                output.write(frame)
                output.flush()
                os.fsync(output.fileno())
            digest, physical = core.read_verified(payload)
            core.require(
                digest == hashlib.sha256(frame).hexdigest() and physical[6] == len(frame),
                "D1_ZSTD_SHA",
            )
            out.update(
                success=True,
                step="captured",
                account_id=settings.account_id,
                database_id=settings.database_id,
                resource=d1.RESOURCE,
                staging_name=STAGING_NAME,
                path=str(payload),
                bytes=physical[6],
                sha256=digest,
                sql_bytes=len(sql),
                sql_sha256=receipt["sha256"],
                export_receipt_sha256=receipt_sha,
                started_at_utc=receipt["operation_started_at_utc"],
                completed_at_utc=receipt["completed_at_utc"],
                sqlite_integrity_verified=True,
                owner_verified=True,
                cloud_restore_verified=False,
            )
        except d1.ExportError as error:
            out["error"] = d1.public_error_code(error)
        except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            out["error"] = core.output_code(error)
        core.write_record(run / "result.json", out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture",))
    parser.parse_args()
    out = {"success": False, "backup_set_complete": False, "cloud_restore_verified": False}
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
        run_capture(request, out)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        out.update(success=False, error=core.output_code(error))
    print(core.encode(out).decode(), flush=True)
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
