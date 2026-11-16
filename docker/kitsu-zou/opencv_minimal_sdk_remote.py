"""SDK Zou/OpenCV mínimo propio: transiciones POSIX cerradas y etapas offline.

Las primitivas de proceso/FD se copian explícitamente del controlador V4
acreditado; no se altera ni se intercepta ese módulo o el builder global.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import time
import types
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

OWNER = "agencia-digital-marketing-ai:opencv-zou-minimal-sdk:v1"
BASE_MANIFEST = "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72"
BASE_CONFIG = "sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7"
BASE_REFERENCE = "cgr.dev/chainguard/wolfi-base@" + BASE_MANIFEST
BASE_ENV = [
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin",
    "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
]
BASE_LABELS = {
    "dev.chainguard.image.title": "wolfi-base",
    "dev.chainguard.package.main": "",
    "org.opencontainers.image.authors": "Chainguard Team https://www.chainguard.dev/",
    "org.opencontainers.image.created": "2026-11-05T19:11:38Z",
    "org.opencontainers.image.source": "https://github.com/chainguard-images/images/tree/main/images/wolfi-base",
    "org.opencontainers.image.title": "wolfi-base",
    "org.opencontainers.image.url": "https://images.chainguard.dev/directory/image/wolfi-base/overview",
    "org.opencontainers.image.vendor": "Chainguard",
}
ORIGINAL_MANIFEST_SHA = "c44452916dfeca1c58eec70ec8fed546a88b7543ee4c3c57ac80608460076eca"
RECIPE_SHA = "fc3341fa0c8c20ad7bc2ab9b858f127cd0e0ec76b233f05273ef0df2d45aed7f"
PACKAGER_SHA = "831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9"
NATIVE_SHA = "b00b1a1a1822b13ed145128209664a15f3efa781b39cd850d0d63bcc0666192b"
MAX_OUTPUT = 4 * 1024**2
MAX_TOTAL = 768 * 1024**2
MAX_FILE = 256 * 1024**2
MARKER = ".opencv-sdk-owner.json"
SHA = re.compile(r"[a-f0-9]{64}")
LIMITS = {
    "cpu": 2,
    "memory_bytes": 6 * 1024**3,
    "pids": 128,
    "stage_seconds": {"sdk": 600, "build": 1800},
    "capture_bytes": MAX_OUTPUT,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_object(raw):
    def pairs(items):
        value = {}
        for name, item in items:
            if name in value:
                raise ValueError("JSON duplicado.")
            value[name] = item
        return value

    def invalid(_value):
        raise ValueError("JSON no finito.")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def derive_paths(build_id):
    if not re.fullmatch(r"[a-f0-9]{32}", build_id):
        raise ValueError("Identificador de build no admitido.")
    code = "/opt/media-platform/build/contexts/opencv-zou-minimal/" + build_id
    data = "/var/lib/media-platform/build/opencv-zou-minimal/" + build_id
    return {
        "staging": "/home/jandrade/.agencia-opencv-zou-stage-" + build_id,
        "namespace": code,
        "inputs": code + "/inputs",
        "source": code + "/source",
        "overlay": code + "/overlay",
        "numpy_headers": code + "/numpy-headers",
        "data_namespace": data,
        "outputs": data + "/outputs",
        "control": data + "/control",
    }


def validate_profile(profile):
    if (
        profile.get("schema_version") != 1
        or profile.get("owner") != OWNER
        or profile.get("paths") != derive_paths(profile.get("build_id", ""))
        or not SHA.fullmatch(str(profile.get("kit_sha256", "")))
        or profile["build_id"] != profile["kit_sha256"][:32]
        or profile.get("base")
        != {"reference": BASE_REFERENCE, "manifest": BASE_MANIFEST, "config": BASE_CONFIG}
        or profile.get("limits") != LIMITS
        or profile.get("target")
        != {"hostname": "vm-media-01", "ip": "192.168.0.85", "port": 22, "user": "jandrade"}
    ):
        raise ValueError("El perfil no acredita rol, rutas, destino, base o límites fijos.")


def inventory_digest(files):
    return digest(
        "".join(f"{row['sha256']}  {name}\n" for name, row in sorted(files.items())).encode()
    )


def validate_inventory(files, expected):
    if not isinstance(files, dict) or not 0 < len(files) <= 256:
        raise ValueError("Inventario ausente o excesivo.")
    total = 0
    for name, row in files.items():
        if (
            not re.fullmatch(r"[A-Za-z0-9_+./@-]+", name)
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or set(row) != {"size", "sha256"}
            or type(row["size"]) is not int
            or not 0 < row["size"] <= MAX_FILE
            or not SHA.fullmatch(str(row["sha256"]))
        ):
            raise ValueError("Archivo del inventario no admitido.")
        total += row["size"]
    if total > MAX_TOTAL or inventory_digest(files) != expected:
        raise ValueError("El inventario no coincide con el pin externo o excede el límite.")


def labels(profile):
    return {
        **BASE_LABELS,
        "agencia.owner": OWNER,
        "agencia.role": "opencv-zou-minimal-sdk",
        "agencia.build-id": profile["build_id"],
        "agencia.kit-sha256": profile["kit_sha256"],
    }


def container_environment(profile):
    return [
        *BASE_ENV,
        "AGENCY_BUILD_OWNER=" + OWNER,
        "AGENCY_INPUT_MANIFEST_SHA256=" + profile["kit_sha256"],
    ]


def hold_arguments():
    return ["-c", "trap 'exit 0' TERM INT; while :; do sleep 1; done"]


def mount_contract(profile):
    paths = profile["paths"]
    return [
        {"Type": "bind", "Source": paths[key], "Target": destination, "ReadOnly": key != "outputs"}
        for key, destination in [
            ("inputs", "/inputs"),
            ("source", "/source"),
            ("overlay", "/overlay"),
            ("numpy_headers", "/numpy-headers"),
            ("outputs", "/work"),
        ]
    ]


def create_arguments(profile):
    validate_profile(profile)
    result = [
        "create",
        "--pull=never",
        "--name",
        "agency-opencv-zou-" + profile["build_id"],
        "--user=0:0",
        "--network=none",
        "--cpus=2",
        "--memory=" + str(6 * 1024**3),
        "--memory-swap=" + str(6 * 1024**3),
        "--pids-limit=128",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=67108864,mode=1777",
        "--entrypoint=/bin/sh",
    ]
    for name, value in labels(profile).items():
        result.extend(["--label", name + "=" + value])
    for value in container_environment(profile):
        result.extend(["--env", value])
    for row in mount_contract(profile):
        result.extend(
            [
                "--mount",
                f"type=bind,source={row['Source']},target={row['Target']}"
                + (",readonly" if row["ReadOnly"] else ""),
            ]
        )
    return [*result, BASE_REFERENCE, *hold_arguments()]


def check_container(value, cid, profile):
    validate_profile(profile)
    if (
        not re.fullmatch(r"[a-f0-9]{64}", cid)
        or value.get("Id") != cid
        or value.get("Image") not in {BASE_MANIFEST, BASE_CONFIG}
        or value.get("Name") != "/agency-opencv-zou-" + profile["build_id"]
    ):
        raise ValueError("El CID o su imagen no son propios.")
    config, host = value.get("Config", {}), value.get("HostConfig", {})
    if (
        config.get("Image") != BASE_REFERENCE
        or config.get("User") != "0:0"
        or config.get("Labels") != labels(profile)
        or config.get("Env") != container_environment(profile)
        or config.get("Entrypoint") != ["/bin/sh"]
        or config.get("Cmd") != hold_arguments()
        or config.get("Volumes") not in (None, {})
    ):
        raise ValueError("La configuración del SDK difiere del contrato propio.")
    required = {
        "NanoCpus": 2_000_000_000,
        "Memory": 6 * 1024**3,
        "MemorySwap": 6 * 1024**3,
        "PidsLimit": 128,
        "NetworkMode": "none",
        "Privileged": False,
        "ReadonlyRootfs": False,
    }
    if any(host.get(key) != expected for key, expected in required.items()):
        raise ValueError("Límites o namespaces del SDK no acreditados.")
    if (
        host.get("CapAdd") not in (None, [])
        or host.get("CapDrop") not in (["ALL"], ["CAP_ALL"])
        or host.get("SecurityOpt")
        not in (["no-new-privileges"], ["no-new-privileges=true"], ["no-new-privileges:true"])
        or any(
            host.get(key) not in (None, [], {})
            for key in ["Binds", "PortBindings", "Devices", "DeviceRequests", "Links", "ExtraHosts"]
        )
    ):
        raise ValueError("El SDK tiene privilegios, puertos, dispositivos o binds ajenos.")
    if (
        any(host.get(key) not in (None, "") for key in ["PidMode", "UTSMode", "UsernsMode"])
        or host.get("IpcMode") not in (None, "", "private")
        or host.get("CgroupnsMode") not in (None, "", "private")
        or host.get("PublishAllPorts", False) is not False
        or host.get("VolumesFrom") not in (None, [])
        or host.get("Runtime") not in (None, "", "runc")
    ):
        raise ValueError("El SDK comparte namespaces o recursos del host.")
    expected = {(row["Source"], row["Target"], row["ReadOnly"]) for row in mount_contract(profile)}
    declared = host.get("Mounts", [])
    if (
        len(declared) != 5
        or {(r.get("Source"), r.get("Target"), r.get("ReadOnly", False)) for r in declared}
        != expected
        or any(
            r.get("Type") != "bind"
            or set(r) - {"Type", "Source", "Target", "ReadOnly", "BindOptions", "Consistency"}
            for r in declared
        )
    ):
        raise ValueError("Los mounts declarados no coinciden semánticamente.")
    for row in declared:
        options = row.get("BindOptions") or {}
        if (
            set(options)
            - {
                "Propagation",
                "NonRecursive",
                "CreateMountpoint",
                "ReadOnlyNonRecursive",
                "ReadOnlyForceRecursive",
            }
            or options.get("Propagation", "rprivate") != "rprivate"
            or any(
                options.get(key, False) is not False
                for key in [
                    "NonRecursive",
                    "CreateMountpoint",
                    "ReadOnlyNonRecursive",
                    "ReadOnlyForceRecursive",
                ]
            )
            or row.get("Consistency", "") not in ("", "default")
        ):
            raise ValueError("Opciones de bind no autorizadas.")
    mounted = value.get("Mounts", [])
    if (
        len(mounted) != 5
        or len({r.get("Destination") for r in mounted}) != 5
        or {(r.get("Source"), r.get("Destination"), not r.get("RW", False)) for r in mounted}
        != expected
        or any(r.get("Type") != "bind" or r.get("Propagation") != "rprivate" for r in mounted)
    ):
        raise ValueError("Los mounts efectivos no corresponden a las raíces propias.")
    if host.get("Tmpfs") != {"/tmp": "rw,nosuid,nodev,size=67108864,mode=1777"}:
        raise ValueError("Tmpfs del SDK diferente.")
    state = value.get("State", {})
    return {
        "cid": cid,
        "running": state.get("Running") is True,
        "pid": state.get("Pid"),
        "started_at": state.get("StartedAt"),
        "created": value.get("Created"),
        "status": state.get("Status"),
    }


def require_backed_receipt(stage, actual, backed):
    value = actual.get(stage)
    receipt = value.get("receipt", {}) if isinstance(value, dict) else {}
    if (
        not isinstance(receipt, dict)
        or value != backed.get(stage)
        or value != envelope(receipt)
        or receipt.get("stage") != stage
        or receipt.get("success") is not True
    ):
        raise ValueError("Falta un recibo canónico positivo y respaldado de la etapa: " + stage)
    return receipt


def require_backup(mode, actual, backed):
    required = {
        "create": ["prepare"],
        "start": ["prepare", "create"],
        "sdk": ["prepare", "create", "start"],
        "export": ["prepare", "create", "start", "sdk"],
        "approve": ["prepare", "create", "start", "sdk"],
        "build": ["prepare", "create", "start", "sdk", "approve"],
        "stop": ["prepare", "create"],
    }.get(mode, [])
    for stage in required:
        require_backed_receipt(stage, actual, backed)


def cleanup_cid(actual, backed, plan, intent=None):
    """Resolver sólo el CID duradero, sin depender del SDK o de sus entradas."""
    if "create" in actual:
        require_backup("stop", actual, backed)
        cid = actual["create"]["receipt"].get("cid", "")
    else:
        require_backed_receipt("prepare", actual, backed)
        saved = backed.get("create_unverified", {})
        if (
            "create" in backed
            or not isinstance(intent, dict)
            or set(intent) != {"cid", "plan_sha256"}
            or not isinstance(saved, dict)
            or set(saved) != {"cid", "verified", "plan_sha256"}
            or saved.get("verified") is not False
            or saved.get("cid") != intent.get("cid")
            or saved.get("plan_sha256") != plan["plan_sha256"]
            or intent.get("plan_sha256") != plan["plan_sha256"]
        ):
            raise ValueError(
                "El CID no verificado no tiene intent y respaldo previos para cleanup."
            )
        cid = intent["cid"]
    if not SHA.fullmatch(str(cid)):
        raise ValueError("El CID para parada no es una identidad completa.")
    return cid


def apk_arguments(packages):
    paths = []
    for row in packages:
        if not re.fullmatch(r"apks/[A-Za-z0-9_.+-]+\.apk", row["path"]):
            raise ValueError("APK fuera de whitelist.")
        paths.append("/inputs/data/" + row["path"])
    return [
        "/bin/sh",
        "-c",
        'exec apk "$@"',
        "apk-sdk",
        "--no-network",
        "--no-cache",
        "--repositories-file",
        "/dev/null",
        "--allow-untrusted",
        "--no-scripts",
        "add",
        *paths,
    ]


def stage_arguments(mode, profile, cid, approved_sha=None):
    if mode == "sdk":
        return ["exec", cid, *apk_arguments(profile["managed_packages"])]
    if mode == "build" and SHA.fullmatch(str(approved_sha)):
        return [
            "exec",
            cid,
            "/usr/bin/bash",
            "-c",
            'umask 077; exec /usr/bin/bash "$@"',
            "opencv-private-build",
            "/inputs/code/docker/kitsu-zou/build-opencv-minimal.sh",
            "--execute",
            "/inputs/data/manifest-approved.json",
            approved_sha,
            "/inputs/data",
            "/work",
            "/source",
            "/overlay",
            "/numpy-headers",
            "/usr/bin/python3.12",
        ]
    raise ValueError("Etapa o aprobación no admitida.")


def stop_owned(docker, cid, profile):
    before = check_container(docker.inspect(cid), cid, profile)
    if before["running"]:
        result = docker.command(["stop", "--time", "5", cid], timeout=7)
        if result["returncode"] != 0:
            raise ValueError("No se detuvo el CID propio.")
    after = check_container(docker.inspect(cid), cid, profile)
    if after["running"]:
        raise ValueError("El CID propio sigue ejecutándose.")
    return {"stopped": True, "cid": cid, "observation": after}


# Primitivas copiadas por bytes del controlador V4 acreditado (a13ab960…).


def run_bounded(arguments, timeout, *, sink=None, environment=None, heartbeat_fd=None):
    if os.name != "posix" or timeout <= 0:
        raise ValueError("El recolector requiere POSIX y un plazo positivo.")
    previous_blocking = os.get_blocking(heartbeat_fd) if heartbeat_fd is not None else None
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=environment,
        close_fds=True,
    )
    started, output, total = time.monotonic(), bytearray(), 0
    error, pulse_deadline = None, started + 5

    def pulse():
        nonlocal pulse_deadline
        if heartbeat_fd is None:
            return
        try:
            value = os.read(heartbeat_fd, 4096)
        except BlockingIOError:
            value = None
        if value == b"":
            raise InterruptedError("El CLI cerró su canal de latido.")
        if value is not None:
            if value.replace(b".", b""):
                raise ValueError("El latido tiene bytes no admitidos.")
            pulse_deadline = time.monotonic() + 5
        if time.monotonic() >= pulse_deadline:
            raise TimeoutError("No llegó un latido del CLI en cinco segundos.")

    try:
        if heartbeat_fd is not None:
            os.set_blocking(heartbeat_fd, False)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                pulse()
                remaining = started + timeout - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("El proceso propio agotó su plazo.")
                for key, _mask in selector.select(min(0.05, remaining)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT:
                        raise ValueError("La salida excede 4 MiB; no se acepta truncada.")
                    if sink is not None:
                        sink.write(chunk)
                    else:
                        output.extend(chunk)
            # WNOWAIT conserva el PID/PGID hasta terminar el grupo propio.
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                pulse()
                if time.monotonic() >= started + timeout:
                    raise TimeoutError("El proceso cerró stdout pero no terminó dentro del plazo.")
                time.sleep(0.01)
    except BaseException as failure:
        error = failure
    finally:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
        process.stdout.close()
        process.wait(timeout=2)
        if heartbeat_fd is not None:
            os.set_blocking(heartbeat_fd, previous_blocking)
    evidence = {
        "pid": process.pid,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "output_bytes": total,
        "returncode": process.returncode,
    }
    if error is not None:
        error.process = evidence
        raise error
    return {"returncode": process.returncode, "output": bytes(output), "process": evidence}


def identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def checked_node(info, uid, *, directory=False):
    kind, mode = (stat.S_ISDIR, 0o700) if directory else (stat.S_ISREG, 0o600)
    if (
        not kind(info.st_mode)
        or info.st_uid != uid
        or info.st_gid != uid
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        raise ValueError("El nodo no tiene tipo, propietario y permisos privados exactos.")


def open_directory(path, *, uid=0):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current, anchor_device = "", None
    try:
        for part in PurePosixPath(path).parts[1:]:
            current += "/" + part
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            expected_uid = uid if current.startswith("/home/jandrade") else 0
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != expected_uid
                or info.st_gid != expected_uid
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise ValueError("Un ancestro no es un directorio propio seguro.")
            if current in {"/opt/media-platform", "/var/lib/media-platform"}:
                anchor_device = info.st_dev
            elif anchor_device is not None and info.st_dev != anchor_device:
                raise ValueError("Un subdirectorio propio cruza a otro sistema de archivos.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_file(directory, name, *, uid=0, maximum=1024 * 1024):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        checked_node(info, uid)
        if info.st_size > maximum:
            raise ValueError("Archivo mayor que su límite.")
        value = stream.read(maximum + 1)
        if len(value) != info.st_size or identity(
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        ) != identity(info):
            raise ValueError("El archivo cambió mientras se leía.")
        return value


def write_file(directory, name, data, *, uid=0):
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        if os.geteuid() == 0:
            os.fchown(stream.fileno(), uid, uid)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)


def open_parent(directory, name, *, create=False, uid=0):
    parts = name.split("/")
    current = os.dup(directory)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            checked_node(os.fstat(child), uid, directory=True)
            os.close(current)
            current = child
        return current, parts[-1]
    except BaseException:
        os.close(current)
        raise


def exact_read(stream, size):
    pieces = []
    remaining = size
    while remaining:
        data = stream.read(remaining)
        if not data:
            raise ValueError("La transferencia terminó antes del tamaño declarado.")
        pieces.append(data)
        remaining -= len(data)
    return b"".join(pieces)


def file_digest(directory, name, expected, *, uid=0):
    parent, leaf = open_parent(directory, name, uid=uid)
    try:
        descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            checked_node(info, uid)
            if info.st_size != expected["size"]:
                raise ValueError("Tamaño diferente del kit fijado.")
            calculator, remaining = hashlib.sha256(), expected["size"]
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("Un archivo del kit se truncó durante la verificación.")
                remaining -= len(chunk)
                calculator.update(chunk)
            if stream.read(1):
                raise ValueError("Un archivo del kit creció durante la verificación.")
            sha = calculator.hexdigest()
            if sha != expected["sha256"] or identity(
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            ) != identity(info):
                raise ValueError("Un archivo del kit cambió o no cumple su hash.")
            return sha
    finally:
        os.close(parent)


def copy_file(source, target, name, expected):
    parent, leaf = open_parent(source, name, uid=1000)
    destination, dest_leaf = open_parent(target, name, create=True)
    try:
        incoming = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(incoming, "rb") as reader:
            info = os.fstat(reader.fileno())
            checked_node(info, 1000)
            if info.st_size != expected["size"]:
                raise ValueError("La fuente cambió de tamaño.")
            outgoing = os.open(
                dest_leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination,
            )
            with os.fdopen(outgoing, "wb") as writer:
                sha, count = hashlib.sha256(), 0
                while chunk := reader.read(65536):
                    count += len(chunk)
                    if count > expected["size"]:
                        raise ValueError("La fuente creció durante la copia.")
                    writer.write(chunk)
                    sha.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if count != expected["size"] or sha.hexdigest() != expected["sha256"]:
                raise ValueError("La copia no coincide con el kit aprobado.")
            if identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) != identity(info):
                raise ValueError("La fuente dejó de corresponder al descriptor abierto.")
        os.fsync(destination)
    finally:
        os.close(parent)
        os.close(destination)


def exact_tree(directory, files, uid):
    found = set()
    parents = {
        str(parent)
        for name in files
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }

    def walk(descriptor, prefix=""):
        with os.scandir(descriptor) as entries:
            for entry in entries:
                name = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if name not in parents:
                        raise ValueError("Directorio adicional en el kit.")
                    checked_node(info, uid, directory=True)
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    try:
                        walk(child, name + "/")
                    finally:
                        os.close(child)
                else:
                    if name not in files or name in found:
                        raise ValueError("Archivo adicional o repetido en el kit.")
                    checked_node(info, uid)
                    found.add(name)

    walk(directory)
    if found != set(files):
        raise ValueError("El kit no contiene exactamente su whitelist.")


class Docker:
    def __init__(self, configuration):
        self.environment = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "DOCKER_CONFIG": configuration,
        }
        self.heartbeat_fd = None

    def command(self, arguments, timeout, sink=None):
        return run_bounded(
            ["/usr/bin/docker", *arguments],
            timeout,
            sink=sink,
            environment=self.environment,
            heartbeat_fd=self.heartbeat_fd if arguments[0] == "exec" else None,
        )

    def capture(self, arguments, timeout=2):
        result = self.command(arguments, timeout)
        if result["returncode"] != 0:
            raise ValueError("La consulta Docker terminó con error.")
        return result["output"]

    def inspect(self, cid, timeout=2):
        value = json_object(self.capture(["inspect", "--type", "container", cid], timeout))
        if not isinstance(value, list) or len(value) != 1:
            raise ValueError("La inspección no devolvió exactamente un contenedor.")
        return value[0]

    def limits(self, observed):
        pid = observed["pid"]
        if type(pid) is not int or pid <= 0:
            raise ValueError("PID del contenedor no acreditado.")
        descriptor = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

        def text(directory, name):
            file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                value = os.read(file, 4097)
                if len(value) > 4096:
                    raise ValueError("Metadato cgroup excesivo.")
                return value.decode("ascii").strip()
            finally:
                os.close(file)

        try:
            lines = text(descriptor, "cgroup").splitlines()
            if len(lines) != 1 or not lines[0].startswith("0::/"):
                raise ValueError("Se requiere cgroup v2 para acreditar límites reales.")
            relative = lines[0][3:]
            if (
                not re.fullmatch(r"/[A-Za-z0-9_./:@-]+", relative)
                or ".." in relative.split("/")
                or observed["cid"] not in relative
            ):
                raise ValueError("El cgroup no identifica el CID propio.")
            group = open_directory("/sys/fs/cgroup" + relative)
            try:
                values = {
                    name: text(group, name)
                    for name in ("memory.max", "memory.swap.max", "cpu.max", "pids.max")
                }
            finally:
                os.close(group)
            quota, period = values["cpu.max"].split()
            if (
                values["memory.max"] != str(6 * 1024**3)
                or values["memory.swap.max"] != "0"
                or values["pids.max"] != "128"
                or not quota.isdigit()
                or not period.isdigit()
                or int(period) <= 0
                or int(quota) != 2 * int(period)
            ):
                raise ValueError("El kernel no acredita los límites 2 CPU/6 GiB/128 procesos.")
            return {"verified": True, "pid": pid, "cgroup": relative, "values": values}
        finally:
            os.close(descriptor)


def envelope(value):
    return {"receipt": value, "receipt_sha256": digest(canonical(value))}


def load_layout(request):
    source = base64.b64decode(request["layout_source_b64"], validate=True)
    if len(source) > 256 * 1024 or digest(source) != request["plan"]["layout_helper_sha256"]:
        raise ValueError("El helper de layout no coincide con el pin.")
    module = types.ModuleType("opencv_zou_layout")
    exec(compile(source, "<layout-propio-fijado>", "exec"), module.__dict__)
    return module


def check_request(request):
    plan = request["plan"]
    validate_profile(plan)
    validate_inventory(plan["files"], plan["kit_sha256"])
    if (
        plan.get("bound_to_vault") is not True
        or plan.get("remote_sha256") != globals().get("_SOURCE_SHA256")
        or digest(canonical({k: v for k, v in plan.items() if k != "plan_sha256"}))
        != plan.get("plan_sha256")
    ):
        raise ValueError("El plan no está ligado a fuente y respaldo explícito.")
    return plan


def verify_host(request, *, root=True):
    module = load_layout(request)
    observed = module.inspect_host()
    module.host_contract(observed, apply=root)
    if not root:
        if os.geteuid() != 1000:
            raise ValueError("Staging requiere UID 1000.")
        return
    with module.LocalFilesystem() as filesystem:
        targets = module.make_plan(filesystem.inspect(), observed)["inventory"]["targets"]
    expected = request["plan"]["layout"]
    if (
        expected.get("success") is not True
        or expected.get("owner") != module.OWNER
        or len(expected.get("targets", [])) != 2
    ):
        raise ValueError("Falta el layout completo respaldado.")
    for item in expected["targets"]:
        if item.get("path") not in {"/opt/media-platform", "/var/lib/media-platform"}:
            raise ValueError("Raíz ajena.")
        current = targets[item["path"]]
        if (
            module.directory_identity(current["node"]) != item["directory"]
            or module.digest(current["marker"]) != item["marker_sha256"]
            or module.digest(current["receipt"]) != item["receipt_sha256"]
        ):
            raise ValueError("El layout difiere del respaldo.")


def receive(request, stream):
    plan = check_request(request)
    verify_host(request, root=False)
    path = plan["paths"]["staging"]
    home = open_directory("/home/jandrade", uid=1000)
    stage = None
    try:
        name = PurePosixPath(path).name
        os.mkdir(name, 0o700, dir_fd=home)
        stage = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
        checked_node(os.fstat(stage), 1000, directory=True)
        marker = {
            "owner": OWNER,
            "path": path,
            "directory": identity(os.fstat(stage)),
            "plan_sha256": plan["plan_sha256"],
        }
        write_file(stage, ".owner.json", canonical(marker), uid=1000)
        for name, item in sorted(plan["files"].items()):
            parent, leaf = open_parent(stage, name, create=True, uid=1000)
            try:
                fd = os.open(
                    leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                with os.fdopen(fd, "wb") as output:
                    sha, remaining = hashlib.sha256(), item["size"]
                    while remaining:
                        raw = exact_read(stream, min(65536, remaining))
                        output.write(raw)
                        sha.update(raw)
                        remaining -= len(raw)
                    output.flush()
                    os.fsync(output.fileno())
                if sha.hexdigest() != item["sha256"]:
                    raise ValueError("Transferencia alterada.")
            finally:
                os.close(parent)
        if stream.read(1):
            raise ValueError("Bytes fuera del kit cerrado.")
        exact_tree(stage, set(plan["files"]) | {".owner.json"}, 1000)
        return {
            "stage": "transfer",
            "success": True,
            "owner": OWNER,
            "staging": path,
            "directory": identity(os.fstat(stage)),
            "marker_sha256": digest(canonical(marker)),
            "files": len(plan["files"]),
            "plan_sha256": plan["plan_sha256"],
            "source_sha256": plan["remote_sha256"],
        }
    finally:
        if stage is not None:
            os.close(stage)
        os.close(home)


def marked_directory(path, *, create=False, shared=False):
    parent = open_directory(str(PurePosixPath(path).parent))
    try:
        name = PurePosixPath(path).name
        if create:
            os.mkdir(name, 0o700, dir_fd=parent)
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            checked_node(os.fstat(descriptor), 0, directory=True)
            expected = {"owner": OWNER, "path": path, "directory": identity(os.fstat(descriptor))}
            if create:
                write_file(descriptor, MARKER, canonical(expected))
            else:
                try:
                    actual = json_object(read_file(descriptor, MARKER))
                except FileNotFoundError:
                    if not shared:
                        raise
                    actual = json_object(read_file(descriptor, ".component-build-owner.json"))
                allowed = (
                    {OWNER, "agencia-digital-marketing-ai:component-build:v4.1"}
                    if shared
                    else {OWNER}
                )
                if (
                    actual.get("owner") not in allowed
                    or actual.get("path") != path
                    or actual.get("directory") != expected["directory"]
                ):
                    raise ValueError("No se adopta un directorio por su nombre.")
            if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(
                os.fstat(descriptor)
            ):
                raise ValueError("El directorio cambió durante la apertura.")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent)


def common_directories():
    paths = [
        ("/opt/media-platform/build", True),
        ("/opt/media-platform/build/contexts", True),
        ("/opt/media-platform/build/contexts/opencv-zou-minimal", False),
        ("/var/lib/media-platform/build", True),
        ("/var/lib/media-platform/build/opencv-zou-minimal", False),
    ]
    for path, shared in paths:
        parent = open_directory(str(PurePosixPath(path).parent))
        try:
            try:
                os.stat(PurePosixPath(path).name, dir_fd=parent, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                exists = False
        finally:
            os.close(parent)
        if shared and not exists:
            raise ValueError("Falta un ancestro compartido ya acreditado.")
        fd = marked_directory(path, create=not exists, shared=shared)
        os.close(fd)


def load_code(path, expected, name):
    raw = Path(path).read_bytes()
    if len(raw) > 256 * 1024 or digest(raw) != expected:
        raise ValueError("Código diferente del pin.")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def write_selected(root, files, links=None):
    """Sólo archivos nuevos y enlaces de biblioteca internos con recibo explícito."""
    root = Path(root)
    root.mkdir(mode=0o700)
    for name, raw in sorted(files.items()):
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("Selección fuera de raíz.")
        target = root.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o600)
    for name, link in sorted((links or {}).items()):
        path = PurePosixPath(name)
        if PurePosixPath(link).is_absolute() or "/" in link or link in {"", ".", ".."}:
            raise ValueError("Enlace de biblioteca no local.")
        target_name = str(path.parent / link)
        if target_name not in files and target_name not in (links or {}):
            raise ValueError("Enlace sin destino seleccionado.")
        target = root.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.symlink(link, target)


def materialize_build_data(plan):
    paths = plan["paths"]
    code = Path(paths["inputs"]) / "code"
    data = Path(paths["inputs"]) / "data"
    packager = load_code(
        code / "docker/kitsu-zou/package_opencv_minimal.py", PACKAGER_SHA, "opencv_frozen_packager"
    )
    manifest_raw = (data / "manifest-sdk.json").read_bytes()
    if digest(manifest_raw) != plan["sdk_manifest_sha256"]:
        raise ValueError("Manifiesto SDK alterado.")
    manifest = json_object(manifest_raw)
    packager.verify_cached_inputs(manifest, data)
    extractor = load_code(
        code / "sre/prepare_backup_sdk.py", plan["extractor_sha256"], "opencv_frozen_extract"
    )
    extractor.extract_archive(
        (data / manifest["source"]["path"]).read_bytes(),
        "tar.gz",
        "opencv-" + manifest["source"]["commit"],
        Path(paths["source"]),
        max_bytes=256 * 1024**2,
        max_file=64 * 1024**2,
        max_members=10000,
    )
    for selected in manifest["source"]["selected_files"]:
        raw = (Path(paths["source"]) / selected["path"]).read_bytes()
        if len(raw) != selected["size"] or digest(raw) != selected["sha256"]:
            raise ValueError("Fuente extraída diferente.")
    numpy = {}
    with zipfile.ZipFile(data / manifest["numpy_headers"]["path"]) as package:
        for row in manifest["numpy_headers"]["selection"]:
            raw = package.read(row["member"])
            if len(raw) != row["size"] or digest(raw) != row["sha256"]:
                raise ValueError("Header NumPy alterado.")
            name = row["member"].removeprefix("numpy/_core/include/")
            if name == row["member"]:
                raise ValueError("Header NumPy fuera de whitelist.")
            numpy[name] = raw
    write_selected(paths["numpy_headers"], numpy)
    index_info = manifest["wolfi_index"]
    index = packager.authenticate_index(
        (data / index_info["path"]).read_bytes(),
        (data / index_info["key_path"]).read_bytes(),
        index_sha256=index_info["sha256"],
        key_sha256=index_info["public_signing_digest"],
    )
    files, links = {}, {}
    library_names = set(manifest["runtime_closure"]["packages"])
    for row in [*manifest["packages"], *manifest["direct_headers"]]:
        if row["name"] not in library_names and row not in manifest["direct_headers"]:
            continue
        verified = packager.verify_wolfi_apk(
            (data / row["path"]).read_bytes(), index, row["name"], row["version"], row["sha256"]
        )
        wanted = (
            {item["path"]: item for item in row["selection"]}
            if row in manifest["direct_headers"]
            else None
        )
        with tarfile.open(fileobj=io.BytesIO(verified.payload), mode="r:") as package:
            for member in package:
                name = member.name.rstrip("/")
                take = (
                    name in wanted
                    if wanted is not None
                    else name.startswith("usr/lib/") and ".so" in PurePosixPath(name).name
                )
                if not take:
                    continue
                if member.isfile():
                    raw = package.extractfile(member).read()
                    if wanted and (
                        len(raw) != wanted[name]["size"] or digest(raw) != wanted[name]["sha256"]
                    ):
                        raise ValueError("Header directo alterado.")
                    if name in files and files[name] != raw:
                        raise ValueError("Overlay ambiguo.")
                    files[name] = raw
                elif member.issym():
                    links[name] = member.linkname
                else:
                    raise ValueError("Tipo no admitido en overlay.")
    write_selected(paths["overlay"], files, links)
    return {
        "source_files": 7654,
        "numpy_headers": len(numpy),
        "overlay_files": len(files),
        "overlay_links": len(links),
        "sdk_executed": False,
        "python313_installed": False,
        "direct_header_apks_installed": False,
    }


def tree_proof(path, *, symlinks=False):
    root = open_directory(path)
    rows = []

    def walk(fd, prefix=""):
        for name in sorted(os.listdir(fd)):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = prefix + name
            if stat.S_ISDIR(info.st_mode):
                checked_node(info, 0, directory=True)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, relative + "/")
                finally:
                    os.close(child)
                rows.append({"path": relative, "type": "directory", **identity(info)})
            elif stat.S_ISLNK(info.st_mode) and symlinks:
                target = os.readlink(name, dir_fd=fd)
                if (
                    info.st_uid != 0
                    or info.st_gid != 0
                    or not target
                    or "/" in target
                    or target in {".", ".."}
                ):
                    raise ValueError("Enlace del overlay no acreditado.")
                rows.append({"path": relative, "type": "link", "target": target, **identity(info)})
            else:
                checked_node(info, 0)
                if info.st_size > 64 * 1024**2:
                    raise ValueError("Archivo materializado excesivo.")
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(child, "rb") as stream:
                    if identity(os.fstat(stream.fileno())) != identity(info):
                        raise ValueError("Archivo cambiado.")
                    sha, count = hashlib.sha256(), 0
                    while block := stream.read(65536):
                        count += len(block)
                        sha.update(block)
                        if count > info.st_size:
                            raise ValueError("Archivo creció.")
                if count != info.st_size:
                    raise ValueError("Archivo truncado.")
                rows.append(
                    {
                        "path": relative,
                        "type": "file",
                        "size": count,
                        "sha256": sha.hexdigest(),
                        **identity(info),
                    }
                )
            if len(rows) > 12000:
                raise ValueError("Árbol materializado excesivo.")

    try:
        walk(root)
        return {
            "directory": identity(os.fstat(root)),
            "entries": len(rows),
            "tree_sha256": digest(canonical(rows)),
        }
    finally:
        os.close(root)


def material_proofs(plan):
    return {
        key: tree_proof(plan["paths"][key], symlinks=key == "overlay")
        for key in ("source", "overlay", "numpy_headers")
    }


class Store:
    def __init__(self, request, *, prepare=False, local_only=False):
        self.request, self.plan = request, request["plan"]
        self.paths, self.descriptors = self.plan["paths"], {}
        if not prepare:
            self.descriptors["control"] = marked_directory(self.paths["control"])
            if not local_only:
                for key in ("inputs", "outputs"):
                    self.descriptors[key] = marked_directory(self.paths[key])

    def close(self):
        for descriptor in self.descriptors.values():
            os.close(descriptor)

    def receipt(self, stage):
        try:
            raw = read_file(self.descriptors["control"], stage + ".json")
        except FileNotFoundError:
            return None
        value = json_object(raw)
        if (
            raw != canonical(value)
            or value.get("stage") != stage
            or value.get("plan_sha256") != self.plan["plan_sha256"]
            or value.get("source_sha256") != self.plan["remote_sha256"]
        ):
            raise ValueError("Recibo ajeno o no canónico.")
        return envelope(value)

    def receipts(self):
        return {
            name: receipt
            for name in ["prepare", "create", "start", "sdk", "export", "approve", "build", "stop"]
            if (receipt := self.receipt(name)) is not None
        }

    def put(self, stage, fields):
        value = {
            "stage": stage,
            "plan_sha256": self.plan["plan_sha256"],
            "source_sha256": self.plan["remote_sha256"],
            "completed_at_utc": datetime.now(UTC).isoformat(),
            **fields,
        }
        write_file(self.descriptors["control"], stage + ".json", canonical(value))
        return envelope(value)

    def snapshot(self):
        return {
            key: {
                "path": self.paths[key],
                "directory": identity(os.fstat(fd)),
                "marker_sha256": digest(read_file(fd, MARKER)),
            }
            for key, fd in self.descriptors.items()
        }

    def verify(self):
        prepared = self.receipt("prepare")
        if prepared is None or prepared["receipt"]["directories"] != self.snapshot():
            raise ValueError("Directorios cambiados desde prepare.")
        if prepared["receipt"]["material_proofs"] != material_proofs(self.plan):
            raise ValueError("Fuente, overlay o headers cambiaron desde prepare.")
        names = set(self.plan["files"]) | {MARKER}
        approval = self.receipt("approve")
        if approval:
            names.add("data/manifest-approved.json")
        exact_tree(self.descriptors["inputs"], names, 0)
        for name, row in self.plan["files"].items():
            file_digest(self.descriptors["inputs"], name, row)
        if approval:
            value = approval["receipt"]
            file_digest(
                self.descriptors["inputs"],
                "data/manifest-approved.json",
                {"sha256": value["manifest_sha256"], "size": value["manifest_bytes"]},
            )
        return prepared

    def verify_control(self):
        prepared = self.receipt("prepare")
        descriptor = self.descriptors["control"]
        current = {
            "path": self.paths["control"],
            "directory": identity(os.fstat(descriptor)),
            "marker_sha256": digest(read_file(descriptor, MARKER)),
        }
        if prepared is None or prepared["receipt"]["directories"].get("control") != current:
            raise ValueError("El control local cambió desde su respaldo.")

    def docker(self):
        path = self.paths["control"] + "/docker-config"
        fd = open_directory(path)
        try:
            checked_node(os.fstat(fd), 0, directory=True)
            if read_file(fd, "config.json") != b"{}":
                raise ValueError("Configuración Docker alterada.")
        finally:
            os.close(fd)
        return Docker(path)

    def prepare(self):
        backed = self.request["backed"].get("transfer")
        if (
            not backed
            or backed.get("plan_sha256") != self.plan["plan_sha256"]
            or backed.get("success") is not True
        ):
            raise ValueError("Falta el respaldo de staging antes de promoverlo.")
        source = open_directory(self.paths["staging"], uid=1000)
        try:
            marker = read_file(source, ".owner.json", uid=1000)
            if (
                digest(marker) != backed["marker_sha256"]
                or identity(os.fstat(source)) != backed["directory"]
            ):
                raise ValueError("Staging cambió desde su respaldo.")
            exact_tree(source, set(self.plan["files"]) | {".owner.json"}, 1000)
            for name, row in self.plan["files"].items():
                file_digest(source, name, row, uid=1000)
            common_directories()
            for key in ("namespace", "data_namespace"):
                fd = marked_directory(self.paths[key], create=True)
                os.close(fd)
            for key in ("inputs", "outputs", "control"):
                self.descriptors[key] = marked_directory(self.paths[key], create=True)
            for name, row in self.plan["files"].items():
                copy_file(source, self.descriptors["inputs"], name, row)
            materials = materialize_build_data(self.plan)
            os.mkdir("docker-config", 0o700, dir_fd=self.descriptors["control"])
            fd = open_directory(self.paths["control"] + "/docker-config")
            try:
                write_file(fd, "config.json", b"{}")
            finally:
                os.close(fd)
            result = self.put(
                "prepare",
                {
                    "success": True,
                    "directories": self.snapshot(),
                    "materials": materials,
                    "material_proofs": material_proofs(self.plan),
                    "files": len(self.plan["files"]),
                    "kit_sha256": self.plan["kit_sha256"],
                    "docker_executed": False,
                },
            )
            self.verify()
            return result
        finally:
            os.close(source)


def check_base_image(value):
    configuration = value.get("Config", {})
    if (
        value.get("Id") not in {BASE_MANIFEST, BASE_CONFIG}
        or value.get("Architecture") != "amd64"
        or value.get("Os") != "linux"
        or configuration.get("User") != "0"
        or configuration.get("Env") != BASE_ENV
        or configuration.get("Labels") != BASE_LABELS
        or configuration.get("Volumes") not in (None, {})
        or configuration.get("Entrypoint") not in (None, [])
        or configuration.get("Cmd") != ["/bin/sh", "-l"]
    ):
        raise ValueError("La imagen base local difiere del pin y contrato Wolfi.")


def inside_sdk(kit_sha, manifest_sha):
    if os.geteuid() != 0 or not SHA.fullmatch(kit_sha) or not SHA.fullmatch(manifest_sha):
        raise ValueError("SDK requiere identidad y pins explícitos.")
    if {item.name for item in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise ValueError("El SDK tiene una interfaz de red externa.")
    data = Path("/inputs/data")
    raw = (data / "manifest-sdk.json").read_bytes()
    if digest(raw) != manifest_sha:
        raise ValueError("El manifiesto SDK no coincide.")
    manifest = json_object(raw)
    expected = {name: row["V"] for name, row in manifest["base"]["packages"].items()}
    expected.update({row["name"]: row["version"] for row in manifest["packages"]})
    installed = {}
    database = Path("/lib/apk/db/installed").read_bytes()
    if len(database) > 4 * 1024**2:
        raise ValueError("DB APK excesiva.")
    for block in database.decode("utf-8").strip().split("\n\n"):
        fields = {
            line[0]: line[2:]
            for line in block.splitlines()
            if len(line) > 1 and line[1] == ":" and line[0] in {"P", "V"}
        }
        if set(fields) != {"P", "V"} or fields["P"] in installed:
            raise ValueError("DB APK ambigua.")
        installed[fields["P"]] = fields["V"]
    if installed != expected or any(
        re.search(r"git|python-3\.13|py3\.13|py3-imath", name) for name in installed
    ):
        raise ValueError("El solver APK no conservó la clausura completa autorizada.")
    tools = []
    for row in manifest["sdk_entrypoints"]:
        path = Path(row["path"])
        raw = path.read_bytes()
        if digest(raw) != row["sha256"]:
            raise ValueError("Una herramienta instalada difiere del APK autenticado.")
        tools.append({"path": row["path"], "sha256": row["sha256"]})
    for path in ("/usr/bin/git", "/bin/git", "/usr/local/bin/git", "/usr/sbin/git", "/sbin/git"):
        if os.path.lexists(path):
            raise ValueError("Git no debe existir en el SDK.")
    receipt = {
        "stage": "sdk",
        "verified": True,
        "kit_sha256": kit_sha,
        "sdk_manifest_sha256": manifest_sha,
        "installed_packages": installed,
        "tools": tools,
        "network_interfaces": ["lo"],
        "source_and_headers_mounted_read_only": all(
            os.statvfs(path).f_flag & os.ST_RDONLY
            for path in ("/inputs", "/source", "/overlay", "/numpy-headers")
        ),
        "compiler_executed": False,
        "native_output_sca_verified": False,
    }
    if not receipt["source_and_headers_mounted_read_only"]:
        raise ValueError("Entradas no montadas RO.")
    with Path("/work/sdk-receipt.json").open("x", encoding="utf-8") as output:
        output.write(json.dumps(receipt, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    Path("/work/sdk-receipt.json").chmod(0o600)
    return receipt


def sdk_artifact(store):
    raw = read_file(store.descriptors["outputs"], "sdk-receipt.json", maximum=256 * 1024)
    value = json_object(raw)
    expected = {name: row["V"] for name, row in store.plan["base_packages"].items()}
    expected.update({row["name"]: row["version"] for row in store.plan["managed_packages"]})
    if (
        value.get("stage") != "sdk"
        or value.get("verified") is not True
        or value.get("kit_sha256") != store.plan["kit_sha256"]
        or value.get("sdk_manifest_sha256") != store.plan["sdk_manifest_sha256"]
        or value.get("installed_packages") != expected
        or value.get("network_interfaces") != ["lo"]
        or value.get("source_and_headers_mounted_read_only") is not True
        or value.get("tools")
        != [{"path": row["path"], "sha256": row["sha256"]} for row in store.plan["sdk_entrypoints"]]
    ):
        raise ValueError("El SDK real no corresponde a las entradas respaldadas.")
    return {
        "sdk_artifact_sha256": digest(raw),
        "installed_packages": len(expected),
        "tool_bytes_verified": len(value["tools"]),
    }


def approved_manifest(request, actual):
    proof = request.get("sdk_sca", {})
    required = {
        "kind",
        "verified",
        "kit_sha256",
        "base_manifest",
        "sdk_receipt_sha256",
        "high_critical",
        "report_sha256",
        "db_next_update_utc",
    }
    if (
        set(proof) != required
        or proof.get("kind") != "opencv-minimal-sdk-sca-v1"
        or proof.get("verified") is not True
        or proof.get("kit_sha256") != request["plan"]["kit_sha256"]
        or proof.get("base_manifest") != BASE_MANIFEST
        or proof.get("sdk_receipt_sha256") != actual["sdk"]["receipt_sha256"]
        or proof.get("high_critical") != 0
        or not SHA.fullmatch(str(proof.get("report_sha256", "")))
    ):
        raise ValueError("Falta SCA del SDK real ligada a base, kit y recibo.")
    next_update = datetime.fromisoformat(proof["db_next_update_utc"].replace("Z", "+00:00"))
    if next_update.tzinfo is None or next_update <= datetime.now(UTC):
        raise ValueError("DB SCA vencida.")
    raw = base64.b64decode(request["approved_manifest_b64"], validate=True)
    if len(raw) > 2 * 1024**2 or digest(raw) != request["approved_manifest_sha256"]:
        raise ValueError("El derivado habilitado no coincide con el respaldo previo.")
    baseline = Path(request["plan"]["paths"]["inputs"], "data/manifest-sdk.json").read_bytes()
    expected = json_object(baseline)
    expected["build_enabled"] = True
    expected["acceptance"] = {
        **expected["acceptance"],
        "native_sdk_sca_verified": True,
        "root_pins_backed_for_new_manifest": True,
    }
    if canonical(json_object(raw)) != canonical(expected):
        raise ValueError("El derivado modifica más que la aprobación declarada.")
    return raw


def validate_input_sca(profile):
    value = profile.get("input_sca", {})
    if (
        value.get("apk_coverage") != {"expected": 91, "reported": 91, "complete": True}
        or value.get("numpy_header_coverage") != [["numpy", "2.4.6"]]
        or value.get("high_critical") != []
        or not SHA.fullmatch(str(value.get("report_sha256", "")))
    ):
        raise ValueError("La SCA de entradas no tiene cobertura completa sin HIGH/CRITICAL.")
    expiry = datetime.fromisoformat(value["db_metadata"]["NextUpdate"].replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(UTC):
        raise ValueError("La DB de entradas SDK venció.")


class LimitedSink:
    def __init__(self, stream):
        self.stream = stream
        self.count = 0

    def write(self, raw):
        if self.count + len(raw) > MAX_OUTPUT:
            raise ValueError("La salida combinada de la etapa excede 4 MiB.")
        self.count += len(raw)
        return self.stream.write(raw)


def check_build_output(store):
    fd = open_directory(store.paths["outputs"] + "/wheels")
    try:
        names = os.listdir(fd)
        wanted = "opencv_python_headless-4.13.0.92+agency1-cp312-cp312-linux_x86_64.whl"
        if names != [wanted]:
            raise ValueError("Conjunto de ruedas diferente del esperado.")
        raw = read_file(fd, wanted, maximum=256 * 1024**2)
    finally:
        os.close(fd)
    cache_fd = open_directory(store.paths["outputs"] + "/build")
    try:
        cache = read_file(cache_fd, "CMakeCache.txt", maximum=1024**2).decode("utf-8")
    finally:
        os.close(cache_fd)
    fields = {}
    for line in cache.splitlines():
        if line and not line.startswith(("#", "//")) and ":" in line and "=" in line:
            key, value = line.split("=", 1)
            fields[key.split(":", 1)[0]] = value
    if (
        fields.get("CMAKE_DISABLE_FIND_PACKAGE_Git") not in {"ON", "TRUE", "1"}
        or fields.get("OPENCV_VCSVERSION") != "4.13.0+agency1"
        or fields.get("GIT_EXECUTABLE") not in {None, "", "GIT_EXECUTABLE-NOTFOUND"}
    ):
        raise ValueError("Cache no acredita versión fija y detección Git deshabilitada.")
    return {
        "wheel_path": store.paths["outputs"] + "/wheels/" + wanted,
        "wheel_sha256": digest(raw),
        "wheel_bytes": len(raw),
        "cmake_cache_sha256": digest(cache.encode()),
        "compiler_executed": True,
        "native_import_verified": False,
        "wheel_final_sca_verified": False,
    }


def execute_request(request, mode):
    plan = check_request(request)
    verify_host(request)
    store = Store(request, prepare=mode == "prepare", local_only=mode == "stop")
    try:
        if mode == "prepare":
            return {"success": True, "result": store.prepare()}
        actual = store.receipts()
        docker = store.docker()
        if mode == "stop":
            store.verify_control()
            intent = (
                json_object(read_file(store.descriptors["control"], "created-cid.json"))
                if "create" not in actual
                else None
            )
            cid = cleanup_cid(actual, request["backed"], plan, intent)
            stopped = stop_owned(docker, cid, plan)
            if "stop" in actual:
                return {"success": True, "result": actual["stop"], "observation": stopped}
            return {"success": True, "result": store.put("stop", {"success": True, **stopped})}
        require_backup(mode, actual, request["backed"])
        store.verify()
        if mode in actual:
            return {"success": actual[mode]["receipt"]["success"], "result": actual[mode]}
        os.mkdir("." + mode + "-started", 0o700, dir_fd=store.descriptors["control"])
        if mode == "create":
            image = json_object(docker.capture(["image", "inspect", BASE_REFERENCE], timeout=5))
            if type(image) is not list or len(image) != 1:
                raise ValueError("Base local no única.")
            check_base_image(image[0])
            result = docker.command(create_arguments(plan), timeout=20)
            cid = result["output"].decode("ascii").strip()
            if result["returncode"] != 0 or not SHA.fullmatch(cid):
                raise ValueError("Create no devolvió CID completo.")
            request["created_cid"] = cid
            write_file(
                store.descriptors["control"],
                "created-cid.json",
                canonical({"cid": cid, "plan_sha256": plan["plan_sha256"]}),
            )
            observed = check_container(docker.inspect(cid), cid, plan)
            if observed["running"] or observed["status"] != "created":
                raise ValueError("El CID nuevo no quedó detenido.")
            return {
                "success": True,
                "result": store.put(
                    "create",
                    {"success": True, "cid": cid, "verified": True, "observation": observed},
                ),
            }
        cid = actual["create"]["receipt"]["cid"]
        before = check_container(docker.inspect(cid), cid, plan)
        if mode == "start":
            if before["running"] or before["status"] != "created":
                raise ValueError("Sólo se inicia una vez el CID creado.")
            result = docker.command(["start", cid], timeout=10)
            after = check_container(docker.inspect(cid), cid, plan)
            if result["returncode"] != 0 or not after["running"]:
                raise ValueError("El SDK no inició correctamente.")
            limits = docker.limits(after)
            return {
                "success": True,
                "result": store.put(
                    "start",
                    {"success": True, "cid": cid, "observation": after, "kernel_limits": limits},
                ),
            }
        if not before["running"] or any(
            before[k] != actual["start"]["receipt"]["observation"][k]
            for k in ["cid", "pid", "started_at"]
        ):
            raise ValueError("El SDK no conserva la instancia iniciada y respaldada.")
        docker.limits(before)
        if mode == "approve":
            raw = approved_manifest(request, actual)
            parent, leaf = open_parent(store.descriptors["inputs"], "data/manifest-approved.json")
            try:
                write_file(parent, leaf, raw)
            finally:
                os.close(parent)
            return {
                "success": True,
                "result": store.put(
                    "approve",
                    {
                        "success": True,
                        "manifest_sha256": digest(raw),
                        "manifest_bytes": len(raw),
                        "sdk_sca": request["sdk_sca"],
                    },
                ),
            }
        if mode == "export":
            target = store.paths["control"] + "/sdk-rootfs.tar"
            if os.path.lexists(target):
                raise ValueError("No se sobrescribe una exportación anterior.")
            result = docker.command(["export", "--output", target, cid], timeout=180)
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                checked_node(info, 0)
                if result["returncode"] != 0 or info.st_size > 3 * 1024**3:
                    raise ValueError("Export excesivo o fallido.")
                sha = hashlib.sha256()
                while raw := stream.read(1024**2):
                    sha.update(raw)
            return {
                "success": True,
                "result": store.put(
                    "export",
                    {
                        "success": True,
                        "cid": cid,
                        "path": target,
                        "sha256": sha.hexdigest(),
                        "bytes": info.st_size,
                    },
                ),
            }
        if mode not in {"sdk", "build"}:
            raise ValueError("Etapa no admitida.")
        if mode == "sdk":
            validate_input_sca(plan)
        log_name = mode + ".log"
        fd = os.open(
            log_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=store.descriptors["control"],
        )
        fields = {"success": False, "cid": cid}
        budget = plan["limits"]["stage_seconds"][mode]
        started = time.monotonic()
        docker.heartbeat_fd = request["_heartbeat_fd"]
        with os.fdopen(fd, "wb", buffering=0) as log_stream:
            sink = LimitedSink(log_stream)
            try:
                approved_sha = (
                    actual["approve"]["receipt"]["manifest_sha256"] if mode == "build" else None
                )
                result = docker.command(
                    stage_arguments(mode, plan, cid, approved_sha), timeout=budget - 10, sink=sink
                )
                fields["process"] = result["process"]
                if result["returncode"] != 0:
                    raise ValueError("La etapa SDK terminó con error.")
                if mode == "sdk":
                    command = [
                        "exec",
                        cid,
                        "/usr/bin/python3.12",
                        "-I",
                        "-B",
                        "/inputs/code/docker/kitsu-zou/opencv_minimal_sdk_remote.py",
                        "--inside-sdk",
                        "--kit-sha256",
                        plan["kit_sha256"],
                        "--manifest-sha256",
                        plan["sdk_manifest_sha256"],
                    ]
                    remaining = budget - (time.monotonic() - started) - 5
                    if remaining <= 0:
                        raise TimeoutError("Etapa SDK sin reserva de cierre.")
                    verify = docker.command(command, timeout=remaining, sink=sink)
                    if verify["returncode"] != 0:
                        raise ValueError("Atestación SDK falló.")
                    fields.update(sdk_artifact(store))
                else:
                    fields.update(check_build_output(store))
                after = check_container(docker.inspect(cid), cid, plan)
                if any(after[k] != before[k] for k in ["cid", "pid", "started_at"]):
                    raise ValueError("Instancia cambió durante la etapa.")
                store.verify()
                fields["success"] = True
            except BaseException as error:
                fields.update(
                    error_type=type(error).__name__,
                    process=getattr(error, "process", fields.get("process")),
                )
                try:
                    fields["cleanup"] = stop_owned(docker, cid, plan)
                except BaseException as cleanup_error:
                    fields["cleanup"] = {
                        "stopped": False,
                        "error_type": type(cleanup_error).__name__,
                    }
            finally:
                log_stream.flush()
                os.fsync(log_stream.fileno())
        raw = read_file(store.descriptors["control"], log_name, maximum=MAX_OUTPUT)
        fields.update(
            log_path=store.paths["control"] + "/" + log_name,
            log_sha256=digest(raw),
            log_bytes=len(raw),
            budget_seconds=budget,
        )
        return {"success": fields["success"], "result": store.put(mode, fields)}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=[
            "receive",
            "prepare",
            "create",
            "start",
            "sdk",
            "export",
            "approve",
            "build",
            "stop",
        ],
    )
    parser.add_argument("--inside-sdk", action="store_true")
    parser.add_argument("--kit-sha256")
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args()
    os.umask(0o077)
    request = {}
    try:
        if platform.system() != "Linux":
            raise ValueError("Sólo Linux.")
        if args.inside_sdk:
            inside_sdk(args.kit_sha256, args.manifest_sha256)
            return 0
        if not args.mode:
            raise ValueError("Etapa obligatoria.")
        stream = sys.stdin.buffer.raw
        length = struct.unpack(">I", exact_read(stream, 4))[0]
        if not 0 < length <= 2 * 1024**2:
            raise ValueError("Petición excesiva.")
        request = json_object(exact_read(stream, length))
        if args.mode == "receive":
            result = {"success": True, "result": receive(request, stream)}
        else:
            if args.mode in {"sdk", "build"}:
                request["_heartbeat_fd"] = sys.stdin.fileno()
            elif stream.read(1):
                raise ValueError("Bytes fuera de petición.")

            def interrupted(_signal, _frame):
                raise InterruptedError("Canal interrumpido.")

            for name in ("SIGINT", "SIGTERM", "SIGHUP"):
                signal.signal(getattr(signal, name), interrupted)
            result = execute_request(request, args.mode)
    except BaseException as error:
        result = {"success": False, "stage": args.mode, "error_type": type(error).__name__}
        if SHA.fullmatch(str(request.get("created_cid", ""))):
            result["created_cid"] = request["created_cid"]
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
