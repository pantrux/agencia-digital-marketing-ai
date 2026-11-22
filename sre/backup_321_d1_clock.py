"""Consume la orden de Cloudflare y respalda únicamente D1 desde vm-media-01."""

import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from . import backup_321_orchestrator as core
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_orchestrator as core

CONFIG = core.ROOT / "backup-d1-clock-config-v1"
ROOT = core.ROOT / "backup-d1-clock-v1"
DESTINATION_NAS = "/mnt/producciones/_backups/d1"
BUCKET = "studio-backups-bucket"
WINDOW_MINUTES = 12
LOCAL_FALLBACK_MINUTE = 8
FILE = core.D1_FILES[0]
REPO_SCOPES = {
    "R2_PLATFORM_ACCESS_KEY_ID",
    "R2_PLATFORM_SECRET_ACCESS_KEY",
    "BACKUP_NAS_DSM_RECEIPT_FILE",
}


def confirm_order(raw, key, now):
    core.require(0 < len(raw) <= 512, "ORDEN_COTA")
    order = json.loads(raw, object_pairs_hook=core.unique_fields)
    core.require(
        isinstance(order, dict)
        and set(order) == {"day", "scheduled_ms", "signature"}
        and isinstance(order["day"], str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", order["day"])
        and type(order["scheduled_ms"]) is int
        and 0 < order["scheduled_ms"] < 253402300800000
        and isinstance(order["signature"], str)
        and re.fullmatch(r"[a-f0-9]{64}", order["signature"]),
        "ORDEN_FORMATO",
    )
    day, millis = order["day"], order["scheduled_ms"]
    message = f"studio-d1-clock-v1\n{day}\n{millis}\n".encode("ascii")
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    core.require(hmac.compare_digest(expected, order["signature"]), "ORDEN_FIRMA")
    stamp = datetime.fromtimestamp(millis / 1000, UTC)
    local = stamp.astimezone(ZoneInfo("America/Santiago"))
    core.require(
        millis % 60_000 == 0
        and (local.hour, local.minute) == (3, 0)
        and local.date().isoformat() == day
        and timedelta(0) <= now - stamp < timedelta(minutes=WINDOW_MINUTES + 1),
        "ORDEN_VENTANA",
    )
    return day


def order_origin(raw, key, now):
    local = now.astimezone(ZoneInfo("America/Santiago"))
    core.require(local.hour == 3 and local.minute <= WINDOW_MINUTES, "ORDEN_VENTANA")
    if raw is None:
        return "temporizador-local" if local.minute >= LOCAL_FALLBACK_MINUTE else None
    core.require(confirm_order(raw, key, now) == local.date().isoformat(), "ORDEN_FECHA")
    return "r2-firmada"


def read_text(path, width):
    file = Path(path)
    core.private_directory(file.parent)
    raw, _ = core.read_verified(file, maximum=width + 2, modes=(0o400, 0o600))
    value = raw.rstrip(b"\r\n").decode("ascii")
    core.require(
        re.fullmatch(r"[!-~]+", value) and len(value) <= width and value != "CHANGE_ME",
        "CREDENCIAL_FORMATO",
    )
    return value


def load_profile():
    core.private_directory(CONFIG)
    raw, _ = core.read_verified(CONFIG / "profile.json", maximum=8192)
    pin = read_text(CONFIG / "profile.sha256", 64)
    core.require(hashlib.sha256(raw).hexdigest() == pin, "PERFIL_SHA")
    profile = json.loads(raw, object_pairs_hook=core.unique_fields)
    core.require(
        isinstance(profile, dict)
        and set(profile) == {"d1", "targets", "restic", "restic_sha256", "credentials"}
        and isinstance(profile["d1"], dict)
        and set(profile["d1"]) == {"account_id", "database_id", "token_file"}
        and isinstance(profile["credentials"], dict)
        and set(profile["credentials"]) == REPO_SCOPES,
        "PERFIL_CAMPOS",
    )
    return profile


def fetch_order(client, day, d1):
    token = read_text(d1["token_file"], 4096)
    account = d1["account_id"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/r2/buckets/{BUCKET}/objects/d1-clock/v1/{day}.json"
    with client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as response:
        if response.status_code == 404:
            return None
        core.require(response.status_code == 200, f"ORDEN_R2_HTTP_{response.status_code}")
        payload = bytearray()
        for block in response.iter_bytes(chunk_size=513):
            payload.extend(block)
            core.require(len(payload) <= 512, "ORDEN_COTA")
    return payload


def backup_request(profile, run_id, staging, manifest):
    request = {
        "run_id": run_id,
        "staging": str(staging),
        "manifest_sha256": manifest,
        "journal": str(ROOT / "journal"),
        "restic": profile["restic"],
        "restic_sha256": profile["restic_sha256"],
        "timeout_seconds": 360,
        "targets": profile["targets"],
    }
    core.request_settings(request, "run")
    account = profile["d1"]["account_id"]
    core.require(
        request["targets"]["nas"]["repository"] == DESTINATION_NAS
        and request["targets"]["r2"]["repository"]
        == f"s3:https://{account}.r2.cloudflarestorage.com/{BUCKET}/d1-restic-v1",
        "DESTINOS_D1",
    )
    return request


def produce_backup(day, profile, capture, *, origin="operador"):
    run_id = f"d1-clock-{day}"
    run_directory = ROOT / "journal" / run_id
    staging = ROOT / "staging" / run_id
    request = backup_request(profile, run_id, staging, "0" * 64)
    if os.path.lexists(run_directory):
        core.private_directory(run_directory)
        raw, _ = core.read_verified(run_directory / "result.json", maximum=16384)
        saved = json.loads(raw, object_pairs_hook=core.unique_fields)
        core.require(
            saved.get("success") is True and saved.get("d1_backup_complete") is True,
            "RESPALDO_D1_REQUIERE_CONCILIACION",
        )
        return {
            "success": True,
            "status": "ya-confirmado",
            "run_id": run_id,
            "order_origin": saved.get("order_origin"),
        }

    for name, path in profile["credentials"].items():
        if name == "BACKUP_NAS_DSM_RECEIPT_FILE":
            core.private(Path(path), (0o600,))
            os.environ[name] = path
        else:
            os.environ[name] = read_text(path, 256)

    capture_directory = ROOT / "capture" / run_id
    receipt = {}
    if os.path.lexists(capture_directory):
        core.private_directory(capture_directory)
        raw, _ = core.read_verified(capture_directory / "result.json", maximum=16384)
        receipt = json.loads(raw, object_pairs_hook=core.unique_fields)
    else:
        capture.run_capture(
            {"root": str(ROOT / "capture"), "run_id": run_id, **profile["d1"]}, receipt
        )
    if receipt.get("success") is False:
        code = receipt.get("error")
        if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            raise ValueError(f"CAPTURA_D1_FALLIDA_{code}")
    core.require(
        receipt.get("success") is True
        and receipt.get("owner_verified") is True
        and receipt.get("sqlite_integrity_verified") is True
        and receipt.get("database_id") == profile["d1"]["database_id"]
        and receipt.get("run_id") == run_id,
        "CAPTURA_D1_FALLIDA",
    )
    source = capture_directory / FILE
    data, _ = core.read_verified(source, maximum=capture.MAX_FRAME)
    digest = hashlib.sha256(data).hexdigest()
    core.require(digest == receipt.get("sha256"), "CAPTURA_D1_SHA")
    manifest = f"{digest}  {FILE}\n".encode("ascii")
    core.private_directory(staging.parent)
    if not staging.exists():
        staging.mkdir(mode=0o700)
        for name, payload in ((FILE, data), ("SHA256SUMS", manifest)):
            with (staging / name).open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        core.sync_directory(staging)
        core.sync_directory(staging.parent)
    request["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    output = {
        "success": False,
        "backup_set_complete": False,
        "restoration_verified": False,
        "order_origin": origin,
    }
    core.run_backup(request, output, scope="d1-clock")
    return output


def run_scheduled(now, profile_loader, capture, httpx):
    local = now.astimezone(ZoneInfo("America/Santiago"))
    if local.hour < 3:
        return {"success": True, "status": "antes-de-la-ventana"}
    day = local.date().isoformat()
    profile = profile_loader()
    capture.validate_request(
        {"root": str(ROOT / "capture"), "run_id": f"d1-clock-{day}", **profile["d1"]}
    )
    if local.hour > 3 or local.minute > WINDOW_MINUTES:
        # Un arranque tardío toma el estado vigente del día. No atribuye
        # la captura a una orden anterior ni reutiliza una firma vencida.
        return produce_backup(day, profile, capture, origin="temporizador-recuperacion")

    key = bytes.fromhex(read_text(CONFIG / "trigger.key", 64))
    core.require(len(key) == 32, "ORDEN_CLAVE")
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=10.0) as client:
            raw = fetch_order(client, day, profile["d1"])
    except httpx.HTTPError as error:
        raise ValueError("ORDEN_R2_TRANSPORTE") from error
    origin = order_origin(raw, key, datetime.now(UTC))
    if origin is None:
        return {"success": True, "status": "esperando-orden"}
    return produce_backup(day, profile, capture, origin=origin)


def main():
    try:
        core.require(
            os.uname().sysname == "Linux"
            and os.uname().nodename == "vm-media-01"
            and os.geteuid() == os.getegid() == 0,
            "HOST_O_IDENTIDAD",
        )
        os.umask(0o077)
        signal.signal(signal.SIGTERM, core.interrupted)
        signal.signal(signal.SIGHUP, core.interrupted)
        now = datetime.now(UTC)

        # El árbol de instalación incluye el proveedor HTTPX previamente fijado.
        sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
        import backup_321_capture_d1 as capture
        import httpx

        output = run_scheduled(now, load_profile, capture, httpx)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        output = {"success": False, "error": core.output_code(error)}
    print(core.encode(output).decode(), flush=True)
    return 0 if output["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
