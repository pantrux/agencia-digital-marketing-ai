"""Publica si el último día exigible de D1 tiene recibos de Synology y R2."""

import json
import os
import re
import ssl
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from . import studio_production_metrics as otlp
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import studio_production_metrics as otlp

JOURNAL = Path("/var/lib/media-platform/backup-d1-clock-v1/journal")
SHA256 = re.compile(r"[a-f0-9]{64}")
TARGETS = ("nas", "r2")


def expected_run(now):
    local = now.astimezone(ZoneInfo("America/Santiago"))
    day = local.date()
    # El último disparo es a las 03:12. La unidad dispone de 13 minutos para concluir.
    if (local.hour, local.minute) < (3, 27):
        day -= timedelta(days=1)
    return f"d1-clock-{day.isoformat()}"


def confirmed(run_id, result, receipts):
    if (
        result.get("run_id") != run_id
        or result.get("success") is not True
        or result.get("d1_backup_complete") is not True
        or not SHA256.fullmatch(str(result.get("manifest_sha256", "")))
        or result.get("destinations") != receipts
        or set(receipts) != set(TARGETS)
    ):
        return False
    for receipt in receipts.values():
        if (
            receipt.get("success") is not True
            or type(receipt.get("exit_code")) is not int
            or receipt["exit_code"] != 0
            or receipt.get("reconciliation_required") is not False
            or not SHA256.fullmatch(str(receipt.get("receipt_sha256", "")))
            or not SHA256.fullmatch(str(receipt.get("snapshot_id", "")))
        ):
            return False
    return receipts["nas"]["snapshot_id"] != receipts["r2"]["snapshot_id"]


def read_receipt(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("RECIBO_PRIVACIDAD")
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("RECIBO_COTA")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("RECIBO_FORMATO")
    return value


def backup_status(run_id):
    try:
        directory = os.open(JOURNAL / run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != 0 or info.st_mode & 0o077:
                raise ValueError("DIARIO_PRIVACIDAD")
            result = read_receipt(directory, "result.json")
            receipts = {
                target: read_receipt(directory, f"{target}.result.json") for target in TARGETS
            }
        finally:
            os.close(directory)
        return "confirmado" if confirmed(run_id, result, receipts) else "incompleto"
    except FileNotFoundError:
        return "ausente"
    except (OSError, ValueError):
        return "invalido"


def metrics_payload(status, time_ns):
    metrics = [
        {
            "name": f"studio_d1_backup_{suffix}",
            "gauge": {"dataPoints": [{"timeUnixNano": str(time_ns), "asInt": str(value)}]},
        }
        for suffix, value in (
            ("expected_day_confirmed", int(status == "confirmado")),
            ("report_unixtime_seconds", time_ns // 1_000_000_000),
        )
    ]
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        otlp.label("service.name", "agency-studio-d1-backup"),
                        otlp.label("service.instance.id", "vm-media-01"),
                    ]
                },
                "scopeMetrics": [{"scope": {"name": "studio.d1.backup"}, "metrics": metrics}],
            }
        ]
    }


def main():
    run_id = expected_run(datetime.now(UTC))
    status = backup_status(run_id)
    try:
        credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
        tls = ssl.create_default_context(cafile=str(credentials / "nas-ca"))
        tls.minimum_version = ssl.TLSVersion.TLSv1_3
        tls.load_cert_chain(credentials / "nas-client", credentials / "nas-key")
        ack = otlp.post(
            otlp.client(tls),
            otlp.PROMETHEUS,
            metrics_payload(status, time.time_ns()),
            {},
            limit=16384,
        )
        otlp.check_ack(*ack)
    except (OSError, KeyError, TypeError, ValueError) as failure:
        print(json.dumps({"success": False, "error": type(failure).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({"success": True, "run_id": run_id, "status": status}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
