"""Comprueba la salud de los contenedores vigentes de SupoClip sin mutarlos."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

if __package__:
    from . import supoclip_boot as boot
else:
    import types

    helper = Path(__file__).resolve().with_name("supoclip_boot.py")
    if helper.parent != Path("/opt/media-platform/supoclip-boot-v1"):
        raise SystemExit('{"success":false,"error":"BOOT_RUTA"}')
    try:
        with os.fdopen(
            os.open(helper, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
        ) as stream:
            identity = os.fstat(stream.fileno())
            raw = stream.read(65537)
        expected = "005ec7f86fa1da2de1124eb55dc6e76150e9651156dcf48e2b938b5c8ceff99e"
        if not (
            stat.S_ISREG(identity.st_mode)
            and (identity.st_uid, identity.st_gid) == (0, 0)
            and identity.st_nlink == 1
            and stat.S_IMODE(identity.st_mode) == 0o444
            and 0 < len(raw) <= 65536
            and hashlib.sha256(raw).hexdigest() == expected
        ):
            raise ValueError("BOOT_PIN")
    except (OSError, ValueError):
        raise SystemExit('{"success":false,"error":"BOOT_PIN"}') from None
    boot = types.ModuleType("supoclip_boot")
    exec(compile(raw, str(helper), "exec"), boot.__dict__)


ROLES = boot.ROLES
DOCKER_HEALTH = ("postgres", "valkey", "frontend", "model")
BACKEND = "http://127.0.0.1:8000/health/"


def probe():
    accepted, _, network = boot.inputs()
    rows = boot.containers(accepted)
    boot.need(
        all(
            row["state"] == ("exited" if role == "migrate" else "running")
            for role, row in rows.items()
        ),
        "CONTENEDOR_ESTADO",
    )
    boot.need(
        all(rows[role]["health"] == "healthy" for role in DOCKER_HEALTH), "HEALTHCHECK_DOCKER"
    )
    boot.network_ok(rows, network)
    boot.need(boot.firewall(), "FIREWALL_FALTANTE")
    backend = rows["backend"]["id"]
    boot.need(boot.http(backend, BACKEND + "db", "healthy", "database"), "SALUD_BACKEND_DB")
    boot.need(boot.http(backend, BACKEND + "redis", "healthy", "redis"), "SALUD_BACKEND_REDIS")
    boot.need(boot.http(backend, BACKEND + "worker", "healthy", "worker"), "SALUD_WORKER_HEARTBEAT")
    boot.need(boot.local_proxy(), "SALUD_PROXY")
    boot.need(boot.containers(accepted) == rows, "CONTENEDOR_CAMBIO")
    return dict.fromkeys(
        (
            "containers",
            "docker_health",
            "network",
            "firewall",
            "backend_database",
            "backend_redis",
            "worker_heartbeat",
            "proxy_http",
            "container_state_stable",
        ),
        True,
    )


def main():
    try:
        boot.need(os.geteuid() == 0 and os.uname().nodename == "vm-media-01", "HOST")
        result = {"success": True, "checks": probe()}
    except boot.ERRORS as error:
        detail = str(error)
        result = {
            "success": False,
            "error": detail if re.fullmatch(r"[A-Z0-9_]+", detail) else type(error).__name__,
        }
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
