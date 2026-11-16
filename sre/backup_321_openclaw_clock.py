"""Ejecuta la copia independiente de los cinco agentes de la agencia."""

import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from . import backup_321_openclaw_transport as source
    from . import backup_321_orchestrator as core
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_openclaw_transport as source
    import backup_321_orchestrator as core

CONFIG = core.ROOT / "backup-openclaw-clock-config-v1"
ROOT = core.ROOT / "backup-openclaw-clock-v1"
FILE = core.OPENCLAW_FILES[0]
REPO_SCOPES = {
    "R2_PLATFORM_ACCESS_KEY_ID",
    "R2_PLATFORM_SECRET_ACCESS_KEY",
    "BACKUP_NAS_DSM_RECEIPT_FILE",
}


def load_profile():
    core.private_directory(CONFIG)
    raw, _ = core.read_verified(CONFIG / "profile.json", maximum=8192)
    pin, _ = core.read_verified(CONFIG / "profile.sha256", maximum=65)
    core.require(hashlib.sha256(raw).hexdigest().encode() == pin.rstrip(b"\n"), "PERFIL_SHA")
    profile = json.loads(raw, object_pairs_hook=core.unique_fields)
    core.require(
        isinstance(profile, dict)
        and set(profile) == {"openclaw", "targets", "restic", "restic_sha256", "credentials"}
        and isinstance(profile["openclaw"], dict)
        and set(profile["openclaw"]) == {"profile", "profile_sha256"}
        and isinstance(profile["credentials"], dict)
        and set(profile["credentials"]) == REPO_SCOPES,
        "PERFIL_CAMPOS",
    )
    return profile


def produce_backup(run_id, profile):
    core.require(
        source.RUN_ID.fullmatch(run_id) and run_id.startswith("openclaw-clock-"), "CORRIDA"
    )
    journal = ROOT / "journal"
    run_directory = journal / run_id
    staging = ROOT / "staging" / run_id
    request = {
        "run_id": run_id,
        "staging": str(staging),
        "manifest_sha256": "0" * 64,
        "journal": str(journal),
        "restic": profile["restic"],
        "restic_sha256": profile["restic_sha256"],
        "timeout_seconds": 360,
        "targets": profile["targets"],
    }
    core.request_settings(request, "run")
    core.require(
        request["targets"]["nas"]["repository"] == "/mnt/producciones/_backups/openclaw"
        and request["targets"]["r2"]["repository"].endswith(
            "/studio-backups-bucket/openclaw-restic-v1"
        ),
        "DESTINOS_OPENCLAW",
    )
    if os.path.lexists(run_directory):
        core.private_directory(run_directory)
        raw, _ = core.read_verified(run_directory / "result.json", maximum=16384)
        saved = json.loads(raw, object_pairs_hook=core.unique_fields)
        core.require(
            saved.get("success") is True and saved.get("openclaw_backup_complete") is True,
            "RESPALDO_OPENCLAW_REQUIERE_CONCILIACION",
        )
        return {"success": True, "status": "ya-confirmado", "run_id": run_id}

    for key, value in profile["credentials"].items():
        path = Path(value)
        core.private_directory(path.parent)
        if key == "BACKUP_NAS_DSM_RECEIPT_FILE":
            core.private(path, (0o600,))
        else:
            raw, _ = core.read_verified(path, maximum=258, modes=(0o400, 0o600))
            value = raw.rstrip(b"\r\n").decode("ascii")
        os.environ[key] = value

    ingress = ROOT / "capture"
    capture = ingress / run_id
    if not os.path.lexists(capture):
        transport = profile["openclaw"]
        source.transport(
            {"run_id": run_id}, transport["profile"], transport["profile_sha256"], ingress=ingress
        )
    core.private_directory(capture)
    raw, _ = core.read_verified(capture / "capture.json", maximum=8192)
    row = json.loads(raw, object_pairs_hook=core.unique_fields)
    core.require(
        set(row) == source.CAPTURE_FIELDS
        and row["success"] is True
        and row["scope"] == "agency-state"
        and row["agents"] == len(source.backup.AGENTS)
        and row["archive_name"] == FILE,
        "CAPTURA_OPENCLAW",
    )
    data, _ = core.read_verified(capture / FILE, maximum=source.backup.MAX_FRAME)
    digest = hashlib.sha256(data).hexdigest()
    core.require(
        len(data) == row["archive_size"] and digest == row["archive_sha256"], "CAPTURA_SHA"
    )
    manifest = f"{digest}  {FILE}\n".encode("ascii")
    core.private_directory(staging.parent)
    if not os.path.lexists(staging):
        staging.mkdir(mode=0o700)
        source.write_private(staging / FILE, data)
        source.write_private(staging / "SHA256SUMS", manifest)
        core.sync_directory(staging)
        core.sync_directory(staging.parent)

    request["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    output = {
        "success": False,
        "backup_set_complete": False,
        "restoration_verified": False,
        "files_in_archive": row["files"],
        "agents_in_archive": row["agents"],
    }
    core.run_backup(request, output, scope="openclaw-clock")
    return output


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
        local = datetime.now(ZoneInfo("America/Santiago"))
        if local.hour < 4:
            return 0
        output = produce_backup(f"openclaw-clock-{local.date().isoformat()}", load_profile())
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        output = {"success": False, "error": core.output_code(error)}
    print(core.encode(output).decode(), flush=True)
    return 0 if output["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
