"""Captura ambas fuentes PostgreSQL en un directorio privado nuevo."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from . import backup_321_orchestrator as core
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_orchestrator as core

SOURCES = {
    "postgres-apps": {
        "file": "capture-postgres-backup.sh",
        "sha256": "c8b4c4fcdbde8fedf43ed9c7ddb699bac07cefbda436cd20fb5c2fa5a04cb673",
        "payload": "postgres.sql.zst",
        "project": "agency-media",
    },
    "postgres-immich": {
        "file": "capture-immich-backup.sh",
        "sha256": "301c7acaff0c61e1793ae26ac9cb3010dc5a99dac7f63672fe564c1db0f68283",
        "payload": "postgres-immich.sql.zst",
        "project": "agency-media-immich-db",
        "image": "sha256:a00b7ac5ed30069abde2702a2b2b3d8321ea0f22d2ba64cc9f836c86b4c47601",
    },
}
IMMICH_PRODUCCION = {
    "image": "sha256:4e2f960dcbc4c5fecf8c8b8a07231700c2f8920350539220ef6e8c6053c78dae",
    "project": "agency-media-immich-lan",
    "owner": "agencia-digital-marketing-ai:postgres-immich-lan:v1",
    "pgdata": "/var/lib/media-platform/immich-lan-v1/postgres",
}
IMMICH_ENSAYO_OWNER = "agencia-digital-marketing-ai:postgres-immich-backup:v1"
APPS_PRODUCCION_IMAGE = "sha256:400e83e5e7aef5630f51e818b190327062420590c3cff4228c543921f64922de"
APPS_PRODUCCION_PGDATA = "/var/lib/media-platform/kitsu-base-v1/postgres-apps"
BASE = Path(__file__).resolve().parent
DATA = "/var/lib/postgresql/data"
DISCO_RESERVA = 64 * 1024**2


def validate_request(request):
    core.require(
        isinstance(request, dict)
        and set(request) == {"root", "run_id", "scope", "postgres-apps", "postgres-immich"}
        and isinstance(request["root"], str)
        and isinstance(request["run_id"], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", request["run_id"])
        and request["scope"] in ("acceptance", "production"),
        "SOLICITUD_CAPTURA",
    )
    for service in SOURCES:
        row = request[service]
        core.require(
            isinstance(row, dict)
            and set(row) == {"cid", "image", "pgdata"}
            and all(isinstance(value, str) for value in row.values())
            and core.SHA256.fullmatch(row["cid"])
            and row["image"].startswith("sha256:")
            and core.SHA256.fullmatch(row["image"][7:]),
            "SOLICITUD_POSTGRES",
        )
        path = Path(row["pgdata"])
        core.require(
            path.is_absolute() and path.is_relative_to(core.ROOT) and path != core.ROOT,
            "POSTGRES_PGDATA_RUTA",
        )
    core.require(
        request["postgres-apps"]["cid"] != request["postgres-immich"]["cid"]
        and request["postgres-apps"]["pgdata"] != request["postgres-immich"]["pgdata"],
        "POSTGRES_FUENTES_DISTINTAS",
    )
    core.require(
        request["postgres-immich"]["image"]
        == (IMMICH_PRODUCCION if request["scope"] == "production" else SOURCES["postgres-immich"])[
            "image"
        ],
        "IMMICH_CAPTURADOR_NO_ACREDITA_IMAGEN",
    )
    if request["scope"] == "production":
        core.require(
            request["postgres-apps"]["image"] == APPS_PRODUCCION_IMAGE
            and request["postgres-apps"]["pgdata"] == APPS_PRODUCCION_PGDATA
            and request["postgres-immich"]["pgdata"] == IMMICH_PRODUCCION["pgdata"],
            "POSTGRES_PRODUCCION_IDENTIDAD",
        )


def observed_container(service, expected, scope):
    project = SOURCES[service]["project"]
    owner = None
    if service == "postgres-immich":
        owner = IMMICH_ENSAYO_OWNER
        if scope == "production":
            project = IMMICH_PRODUCCION["project"]
            owner = IMMICH_PRODUCCION["owner"]
    fmt = (
        '{"cid":{{json .Id}},"image":{{json .Image}},"pid":{{json .State.Pid}},'
        '"started":{{json .State.StartedAt}},"status":{{json .State.Status}},'
        '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
        '"user":{{json .Config.User}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
        '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
        '"owner":{{json (index .Config.Labels "agency.owner")}},"mounts":{{json .Mounts}}}'
    )
    with tempfile.TemporaryDirectory(prefix="agency-backup-docker-", dir="/run") as cli_config:
        argv = [
            "/usr/bin/docker",
            "--host",
            "unix:///var/run/docker.sock",
            "--config",
            cli_config,
            "inspect",
            "--type",
            "container",
            "--format",
            fmt,
            expected["cid"],
        ]
        response = subprocess.run(
            argv,
            env=core.COMMAND_ENV,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=15,
        )
    core.require(0 < len(response.stdout) <= 32768, "POSTGRES_INSPECCION_COTA")
    row = json.loads(response.stdout)
    mounts = [m for m in row["mounts"] if m["Destination"] == DATA]
    core.require(
        row["cid"] == expected["cid"]
        and row["image"] == expected["image"]
        and row["pid"] > 0
        and row["status"] == "running"
        and row["health"] == "healthy"
        and row["user"] == "70:70"
        and row["project"] == project
        and row["service"] == service
        and (owner is None or row["owner"] == owner)
        and len(mounts) == 1
        and mounts[0]["Type"] == "bind"
        and mounts[0]["RW"] is True
        and mounts[0]["Source"] == expected["pgdata"],
        "POSTGRES_IDENTIDAD",
    )
    return {
        **{key: row[key] for key in ("cid", "image", "pid", "started")},
        "pgdata": mounts[0]["Source"],
    }


def load_sources():
    scripts = {}
    for service, source in SOURCES.items():
        path = BASE / source["file"]
        if service == "postgres-immich" and not path.exists():
            path = BASE.parent / "docker/postgres-immich/capture-backup.sh"
        raw, _ = core.read_verified(
            path, maximum=32768, modes=(0o400, 0o444, 0o500, 0o555, 0o600, 0o644, 0o700, 0o755)
        )
        core.require(hashlib.sha256(raw).hexdigest() == source["sha256"], "CAPTURADOR_SHA")
        scripts[service] = raw
    return scripts


def validate_capture(service, expected, capture, raw):
    child = json.loads(raw, object_pairs_hook=core.unique_fields)
    payload = capture / SOURCES[service]["payload"]
    core.require(
        isinstance(child, dict)
        and set(child) == {"cid", "service", "path", "sha256", "bytes"}
        and child["cid"] == expected["cid"]
        and child["service"] == service
        and child["path"] == str(payload)
        and type(child["bytes"]) is int
        and 0 < child["bytes"] <= core.MAX_STAGING_BYTES
        and isinstance(child["sha256"], str)
        and core.SHA256.fullmatch(child["sha256"]),
        "POSTGRES_RECIBO",
    )
    core.private_directory(capture)
    core.require(core.private(payload, (0o600,)).st_size == child["bytes"], "POSTGRES_TAMANO")
    digest, physical = core.read_verified(payload)
    core.require(digest == child["sha256"] and physical[6] == child["bytes"], "POSTGRES_SHA")
    return child


def run_captures(request, out):
    root = Path(request["root"])
    core.private_directory(root)
    core.require(root != core.ROOT, "RAIZ_CAPTURA")
    with core.exclusive():
        disk = os.statvfs(root)
        core.require(disk.f_frsize * disk.f_bavail > DISCO_RESERVA, "DISCO_CAPTURA")
        scripts = load_sources()
        scope = request["scope"]
        sources = {
            service: observed_container(service, request[service], scope) for service in SOURCES
        }
        run = root / request["run_id"]
        run.mkdir(mode=0o700)
        core.sync_directory(root)
        out.update(scope=scope, run_id=request["run_id"], run_directory=str(run))
        core.write_record(
            run / "intent.json",
            {
                "scope": request["scope"],
                "sources": sources,
                "capturers": {name: spec["sha256"] for name, spec in SOURCES.items()},
            },
        )
        out["captures"] = {}
        try:
            bytes_remaining = core.MAX_STAGING_BYTES
            for service in SOURCES:
                disk = os.statvfs(run)
                write_limit = min(bytes_remaining, disk.f_frsize * disk.f_bavail - DISCO_RESERVA)
                core.require(write_limit > 0, "CAPTURA_COTA")
                out["step"] = service
                expected = request[service]
                core.require(
                    observed_container(service, expected, scope) == sources[service],
                    "POSTGRES_CAMBIO",
                )
                capture = run / service
                arguments = [expected["cid"]]
                if service == "postgres-immich":
                    arguments.append(expected["pgdata"])
                arguments.append(str(capture))
                if service == "postgres-immich" and scope == "production":
                    arguments.append(scope)
                started = datetime.now(UTC).isoformat()
                limit = f"--fsize={write_limit}:{write_limit}"
                code, stdout = core.invoke(
                    run,
                    service,
                    arguments,
                    scripts[service],
                    core.COMMAND_ENV,
                    420,
                    prefix=("/usr/bin/prlimit", limit, "--"),
                )
                core.require(code == 0, "POSTGRES_CAPTURADOR")
                raw, _ = core.read_verified(stdout, maximum=4096)
                child = validate_capture(service, expected, capture, raw)
                bytes_remaining -= child["bytes"]
                core.require(bytes_remaining >= 0, "CAPTURA_COTA")
                core.require(
                    observed_container(service, expected, scope) == sources[service],
                    "POSTGRES_CAMBIO",
                )
                result = {
                    **child,
                    "source": sources[service],
                    "started_at_utc": started,
                    "completed_at_utc": datetime.now(UTC).isoformat(),
                    "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                    "staging_name": service + ".sql.zst",
                }
                core.write_record(run / (service + ".result.json"), result)
                out["captures"][service] = result
            out.update(success=True, step="captured")
        except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            out["error"] = core.output_code(error)
        core.write_record(run / "result.json", out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture",))
    parser.parse_args()
    out = {
        "success": False,
        "backup_set_complete": False,
        "scope": None,
        "missing": ["d1.sql.zst", "openclaw.tar.zst"],
    }
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
        raw = sys.stdin.buffer.read(8193)
        core.require(0 < len(raw) <= 8192, "SOLICITUD_COTA")
        request = json.loads(raw, object_pairs_hook=core.unique_fields)
        validate_request(request)
        run_captures(request, out)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        out.update(success=False, error=core.output_code(error))
    print(core.encode(out).decode(), flush=True)
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
