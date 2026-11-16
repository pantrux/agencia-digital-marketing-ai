"""Restaura el arranque y el filtro del proyecto existente de colectores."""

import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import types
from pathlib import Path

WORK = Path("/opt/media-platform/observability-nas-v1")
OPERATOR_SHA = "47e2dcefe309b15d9b6d0b1fa82ad0f3553f14540cc29154c934dde1a9eeb064"
PREPARED_SHA = "717048b03fba029fc29588318e4b790181c05873bc9f68c5d273a319b6ca1752"
COLLECTOR_CONFIG_SHA = "08ce5302718847694d68f7e090d22896654e2fd3577746723409e77e601580d6"
ERRORS = (OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt)


def require(condition, error):
    if not condition:
        raise ValueError(error)


def operator():
    fd = os.open(WORK / "nas_operator.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        node = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(node.st_mode)
            and node.st_uid == node.st_gid == 0
            and node.st_nlink == 1
            and stat.S_IMODE(node.st_mode) == 0o444
            and 0 < node.st_size < 65536,
            "OPERADOR_IDENTIDAD",
        )
        source = stream.read(65536)
    require(hashlib.sha256(source).hexdigest() == OPERATOR_SHA, "OPERADOR_SHA")
    base = types.ModuleType("nas_operator")
    exec(compile(source, "<nas-operator>", "exec"), base.__dict__)
    require(base.WORK == WORK, "OPERADOR_RUTA")
    return base


def host_identity(host):
    return {
        "parent": host["parent"],
        "lan": host["lan"],
        "daemon": {key: value for key, value in host["daemon"].items() if key != "version"},
    }


def receipt(base, observed, *, check_runtime=True):
    base.directory(WORK, 0, 0o700)
    require(base.read(WORK / ".owner", 0, 0o600, 128) == base.OWNER.encode(), "OWNER")
    raw = base.read(WORK / "prepared.json", 0, 0o600, 32768)
    data = json.loads(raw)
    require(
        base.sha(raw) == PREPARED_SHA
        and data["owner"] == base.OWNER
        and data["directory"] == base.directory(WORK, 0, 0o700)
        and data["files"] == base.FILES
        and set(data["containers"]) == set(base.IMAGES),
        "RECIBO",
    )
    if observed is None:
        observed = data["host"]
        actual = base.docker_json(
            "info", "--format", '{"id":{{json .ID}},"root":{{json .DockerRootDir}}}'
        )
        require(
            actual == {key: observed["daemon"][key] for key in ("id", "root")}, "DOCKER_IDENTIDAD"
        )
    else:
        require(host_identity(data["host"]) == host_identity(observed), "RECIBO_HOST")
    require(
        data["firewall"]
        == json.loads(
            base.canonical(
                {
                    "lan": observed["lan"],
                    "address": data["address"],
                    "policy": base.delta(observed["lan"], data["address"]),
                }
            )
        ),
        "RECIBO_FIREWALL",
    )
    if check_runtime:
        require(
            data["images"]
            == {service: base.inspect_image(info) for service, info in base.IMAGES.items()},
            "IMAGEN_RECIBO",
        )
        check_files(base, data)
    return data


def check_files(base, data):
    require(
        base.sha(base.read(WORK / "compose.nas.yml", 0, 0o444, 32768))
        == data["files"]["compose.nas.yml"],
        "COMPOSE_SHA",
    )
    config = WORK / "otel-collector.nas.yml"
    require(base.sha(base.read(config, 0, 0o444, 32768)) == COLLECTOR_CONFIG_SHA, "CONFIG_SHA")
    journal = WORK / "collector-scrape-time-v1"
    base.directory(journal, 0, 0o700)
    expected = {
        "prepared": PREPARED_SHA,
        "cid": data["containers"]["otel-collector"],
        "before": data["files"]["otel-collector.nas.yml"],
        "after": COLLECTOR_CONFIG_SHA,
    }
    require(
        json.loads(base.read(journal / "intent.json", 0, 0o600, 1024)) == expected, "CONFIG_INTENTO"
    )
    tls = WORK / "collector-tls"
    base.directory(tls, 61210, 0o700)
    for name, sha in base.TLS.items():
        require(base.sha(base.read(tls / name, 61210, 0o400, 4096)) == sha, "TLS_SHA")


def states(base, data):
    running = {}
    for service, cid in data["containers"].items():
        (row,) = base.docker_json("container", "inspect", cid)
        state = row["State"]
        require(state["Status"] in {"created", "exited", "running"}, "CID_ESTADO")
        running[service] = state["Running"]
        base.inspect_container(service, cid, data["images"][service], state["Running"])
    return running


def firewall_present(base, data):
    lan, address = data["host"]["lan"], data["address"]
    try:
        base.verify_firewall(lan, address, False)
    except ValueError as error:
        require(str(error) == "FIREWALL_COLISION", "FIREWALL_IDENTIDAD")
        base.verify_firewall(lan, address, True)
        return True
    return False


def start(base, data, out):
    active = states(base, data)
    ids = data["containers"]
    lan, address = data["host"]["lan"], data["address"]
    try:
        protected = firewall_present(base, data)
    except ERRORS:
        if any(active.values()):
            out["stopped_unprotected"] = base.stop_owned(ids, data["images"])
        raise
    if any(active.values()) and not protected:
        out["stopped_unprotected"] = base.stop_owned(ids, data["images"])
        active = states(base, data)
    if not all(active.values()):
        require(
            base.disk_available(data["host"]["daemon"]["root"]) >= 64 * 1024**2, "DISCO_ARRANQUE"
        )
    if not protected:
        base.change_firewall(lan, address)
    try:
        if not all(active.values()):
            if any(active.values()):
                out["stopped_partial"] = base.stop_owned(ids, data["images"])
            base.start(data, out)
        else:
            networks = base.inspect_networks(data["subnet"], address, ids, active=True)
            require(networks == data["networks"], "REDES")
        base.verify_firewall(lan, address, True)
    except ERRORS:
        out["stopped_on_error"] = base.stop_owned(ids, data["images"])
        if not protected:
            base.clear_firewall(lan, address)
        raise
    out.update(running=ids, services_started=True, restored_firewall=not protected)


def stop(base, data, out):
    out["stopped"] = base.stop_owned(data["containers"], data["images"])
    base.clear_firewall(data["host"]["lan"], data["address"])


def inspect(base, data, out):
    out["running"] = states(base, data)
    out["firewall_present"] = firewall_present(base, data)
    out["config_sha256"] = COLLECTOR_CONFIG_SHA
    out["prepared_sha256"] = PREPARED_SHA


def code(error):
    return (
        str(error)
        if type(error) is ValueError and re.fullmatch(r"[A-Z0-9_]+", str(error))
        else type(error).__name__
    )


def main():
    out = {"success": False, "services_started": False}
    try:
        require(len(sys.argv) == 2 and sys.argv[1] in {"start", "stop", "inspect"}, "ACCION")
        action = sys.argv[1]
        out["action"] = action
        require(os.geteuid() == 0 and os.uname().nodename == "vm-media-01", "HOST")
        os.umask(0o077)
        base = operator()
        base.directory(WORK, 0, 0o700)
        signal.signal(signal.SIGTERM, base.interrupted)
        signal.signal(signal.SIGHUP, base.interrupted)
        lock = WORK / "systemd.lock"
        fd = None
        if action != "inspect":
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if fd is not None:
                node = os.fstat(fd)
                require(
                    stat.S_ISREG(node.st_mode)
                    and node.st_uid == node.st_gid == 0
                    and node.st_nlink == 1
                    and stat.S_IMODE(node.st_mode) == 0o600,
                    "LOCK_IDENTIDAD",
                )
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed = None if action == "stop" else base.host()
            data = receipt(base, observed, check_runtime=action != "stop")
            {"start": start, "stop": stop, "inspect": inspect}[action](base, data, out)
        finally:
            if fd is not None:
                os.close(fd)
        out["success"] = True
    except ERRORS as error:
        out["error"] = code(error)
    print(json.dumps(out, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
