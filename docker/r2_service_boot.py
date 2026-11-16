"""Supervisa el contenedor R2 existente después de montar el NAS acreditado."""

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path

PROFILE = Path("/opt/media-platform/r2-service-boot-v1/container.json")
MEDIA = "/mnt/producciones"
SOURCE = "192.168.0.8:/volume2/producciones"
RUNTIME = Path("/var/lib/media-platform/r2-sync-consumer/runtime.nas-provision-v1.json")
RUNTIME_BIND = "/run/secrets/runtime.json"
RUNTIME_UID = 65532
MARKER = ".agency-r2-service-root.json"
OWNER = "agencia-digital-marketing-ai:r2-service:v1"
HEX = r"[0-9a-f]{64}"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
INSPECT = (
    '{"cid":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
    '"owner":{{json (index .Config.Labels "agency.owner")}},'
    '"deployment":{{json (index .Config.Labels "agency.deployment")}},'
    '"role":{{json (index .Config.Labels "agency.role")}},'
    '"config_sha256":{{json (index .Config.Labels "agency.config")}},'
    '"policy":{{json .HostConfig.RestartPolicy}},"mounts":{{json .Mounts}},'
    '"status":{{json .State.Status}},"pid":{{json .State.Pid}}}'
)


def need(condition, code):
    if not condition:
        raise ValueError(code)


def read_regular(path, maximum, *, private=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    with os.fdopen(os.open(path, flags), "rb") as stream:
        initial = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(initial.st_mode)
            and initial.st_nlink == 1
            and 0 < initial.st_size <= maximum,
            "R2_ARCHIVO",
        )
        if private:
            need(
                (initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) == (0, 0, 0o400),
                "R2_PERMISOS",
            )
        content = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        named = path.lstat()
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        need(
            len(content) == initial.st_size
            and all(
                getattr(initial, field) == getattr(after, field) == getattr(named, field)
                for field in fields
            ),
            "R2_ARCHIVO_CAMBIO",
        )
        return content


def profile():
    parent = PROFILE.parent
    info = parent.lstat()
    need(
        parent.resolve() == parent
        and stat.S_ISDIR(info.st_mode)
        and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 0, 0o700),
        "R2_PERFIL_DIRECTORIO",
    )
    value = json.loads(read_regular(PROFILE, 4096, private=True))
    patterns = {
        "cid": HEX,
        "image": "sha256:" + HEX,
        "deployment": UUID,
        "config_sha256": HEX,
        "media_marker_sha256": HEX,
    }
    need(
        type(value) is dict and set(value) == {*patterns, "media_device", "media_inode"},
        "R2_PERFIL",
    )
    need(
        all(
            type(value[key]) is str and re.fullmatch(pattern, value[key])
            for key, pattern in patterns.items()
        )
        and all(
            type(value[key]) is int and value[key] > 0 for key in ("media_device", "media_inode")
        ),
        "R2_PERFIL",
    )
    return value


def run(*args, timeout=12):
    return subprocess.run(
        args, env={"LC_ALL": "C"}, check=True, capture_output=True, timeout=timeout
    ).stdout


def docker(*args, timeout=12):
    return run("/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args, timeout=timeout)


def container(pin, *, adoption=False):
    row = json.loads(docker("inspect", "--type", "container", "--format", INSPECT, pin["cid"]))
    need(
        all(row[key] == pin[key] for key in ("cid", "image", "deployment", "config_sha256"))
        and row["name"] == "/media-r2-serve-" + pin["deployment"]
        and row["owner"] == OWNER
        and row["role"] == "serve",
        "R2_CONTENEDOR_IDENTIDAD",
    )
    names = {"no", "unless-stopped"} if adoption else {"no"}
    need(
        row["policy"].get("Name") in names and row["policy"].get("MaximumRetryCount") == 0,
        "R2_RESTART_DOCKER",
    )
    mounts = [mount for mount in row["mounts"] if mount["Destination"] == "/media"]
    need(
        len(mounts) == 1
        and mounts[0]["Type"] == "bind"
        and mounts[0]["Source"] == MEDIA
        and mounts[0]["RW"] is True
        and mounts[0]["Propagation"] == "rprivate",
        "R2_BIND_DOCKER",
    )
    configs = [mount for mount in row["mounts"] if mount["Destination"] == RUNTIME_BIND]
    need(
        len(configs) == 1
        and configs[0]["Type"] == "bind"
        and configs[0]["Source"] == str(RUNTIME)
        and configs[0]["RW"] is False,
        "R2_CONFIG_BIND",
    )
    return row


def check_mount(path, *task):
    result = (
        run(
            "/usr/bin/findmnt",
            *task,
            "--noheadings",
            "--raw",
            "--mountpoint",
            path,
            "-o",
            "TARGET,SOURCE,FSTYPE,OPTIONS",
        )
        .decode("ascii")
        .splitlines()
    )
    need(len(result) == 1, "R2_NFS_MONTAJE")
    target, source, filesystem, options = result[0].split()
    need(
        (target, source, filesystem) == (path, SOURCE, "nfs4")
        and {"rw", "vers=4.1"} <= set(options.split(",")),
        "R2_NFS_ORIGEN",
    )


def nas_mount(pin):
    check_mount(MEDIA)
    root = Path(MEDIA)
    info = root.lstat()
    identity = (info.st_dev, info.st_ino)
    # Linux reasigna st_dev al montar NFS. El inodo y el marcador identifican el export.
    need(
        stat.S_ISDIR(info.st_mode) and info.st_ino == pin["media_inode"],
        "R2_NFS_IDENTIDAD",
    )
    raw = read_regular(root / MARKER, 4096)
    need(hashlib.sha256(raw).hexdigest() == pin["media_marker_sha256"], "R2_NFS_MARCADOR")
    check_mount(MEDIA)
    current = root.lstat()
    need(
        stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity,
        "R2_NFS_IDENTIDAD",
    )
    return identity


def container_mount(row, identity):
    pid = row["pid"]
    need(row["status"] == "running" and type(pid) is int and pid > 0, "R2_CONTENEDOR_ESTADO")
    check_mount("/media", "--task", str(pid))
    inside = Path(f"/proc/{pid}/root/media").lstat()
    need(
        stat.S_ISDIR(inside.st_mode) and (inside.st_dev, inside.st_ino) == identity,
        "R2_NFS_NAMESPACE",
    )


def rebase_runtime(raw, pin, identity):
    configuration = json.loads(raw)
    proof = configuration["media_proof"]
    device, inode = identity
    need(
        configuration["media_root"] == "/media"
        and type(proof["device"]) is int
        and proof["inode"] == inode == pin["media_inode"]
        and proof["marker_name"] == MARKER
        and proof["marker_sha256"] == pin["media_marker_sha256"],
        "R2_CONFIG_IDENTIDAD",
    )
    proof["device"] = pin["media_device"]
    template = json.dumps(configuration, separators=(",", ":")).encode("ascii")
    need(hashlib.sha256(template).hexdigest() == pin["config_sha256"], "R2_CONFIG_PLANTILLA")
    proof["device"] = device
    return json.dumps(configuration, separators=(",", ":")).encode("ascii")


def runtime_config(pin, identity, *, write=False):
    parent = RUNTIME.parent
    folder = parent.lstat()
    info = RUNTIME.lstat()
    need(
        parent.resolve(strict=True) == parent
        and stat.S_ISDIR(folder.st_mode)
        and (folder.st_uid, folder.st_gid, stat.S_IMODE(folder.st_mode))
        == (RUNTIME_UID, RUNTIME_UID, 0o700)
        and stat.S_ISREG(info.st_mode)
        and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
        == (RUNTIME_UID, RUNTIME_UID, 0o400),
        "R2_CONFIG_PERMISOS",
    )
    raw = read_regular(RUNTIME, 16384)
    updated = rebase_runtime(raw, pin, identity)
    if raw == updated:
        return
    need(write, "R2_CONFIG_DISPOSITIVO")
    fd, path = tempfile.mkstemp(prefix=".runtime-nfs-", dir=parent)
    temporary = Path(path)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fchown(stream.fileno(), RUNTIME_UID, RUNTIME_UID)
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
        need(read_regular(RUNTIME, 16384) == raw, "R2_CONFIG_CAMBIO")
        os.replace(temporary, RUNTIME)
    finally:
        temporary.unlink(missing_ok=True)


def health():
    until = time.monotonic() + 20
    while time.monotonic() < until:
        client = http.client.HTTPConnection("192.168.0.85", 3010, timeout=2)
        try:
            client.request("GET", "/healthz")
            response = client.getresponse()
            raw = response.read(257)
            if response.status == 200 and len(raw) <= 256:
                value = json.loads(raw)
                if (
                    type(value) is dict
                    and set(value) == {"ready", "busy"}
                    and all(type(flag) is bool for flag in value.values())
                    and value["ready"] != value["busy"]
                ):
                    return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            client.close()
        time.sleep(0.25)
    raise ValueError("R2_HEALTH")


def start(pin):
    row = container(pin)
    started = row["status"] == "running"
    try:
        identity = nas_mount(pin)
        runtime_config(pin, identity, write=not started)
        if not started:
            need(row["status"] in {"created", "exited"}, "R2_CONTENEDOR_ESTADO")
            started = True
            docker("start", pin["cid"])
        container_mount(container(pin), identity)
        health()
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        if started:
            stop(pin)
        raise


def stop(pin, *, handoff=False):
    row = container(pin, adoption=handoff)
    if handoff and row["policy"]["Name"] == "unless-stopped":
        return False
    state = row["status"]
    if state in {"running", "paused"}:
        docker("stop", "--time", "20", pin["cid"], timeout=30)
        need(container(pin)["status"] == "exited", "R2_PARADA")
    else:
        need(state in {"created", "exited"}, "R2_PARADA_ESTADO")
    return True


def notify_ready():
    address = os.environ["NOTIFY_SOCKET"]
    if address.startswith("@"):
        address = "\x00" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.sendto(b"READY=1", address)


def supervise(pin):
    need(bool(os.environ.get("NOTIFY_SOCKET")), "R2_NOTIFY")
    start(pin)
    notify_ready()
    docker("wait", pin["cid"], timeout=None)
    raise ValueError("R2_CONTENEDOR_TERMINO")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "preflight", "check", "stop"))
    action = parser.parse_args().action
    try:
        need(os.geteuid() == 0 and os.uname().nodename == "vm-media-01", "R2_HOST")
        pin = profile()
        result = {"success": True, "action": action}
        if action == "run":
            supervise(pin)
        if action in {"preflight", "check"}:
            identity = nas_mount(pin)
            runtime_config(pin, identity)
            container_mount(container(pin, adoption=action == "preflight"), identity)
            health()
        elif action == "stop":
            result["retained_by_docker"] = not stop(pin, handoff=True)
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        message = str(error)
        print(
            json.dumps(
                {
                    "success": False,
                    "error": message if re.fullmatch(r"R2_[A-Z_]+", message) else "R2_FALLO",
                }
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
