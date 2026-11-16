"""Receptor y controlador POSIX del único constructor PostgreSQL Immich propio."""

import argparse
import base64
import hashlib
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
import time
import types
from datetime import UTC, datetime
from pathlib import PurePosixPath

OWNER = "agencia-digital-marketing-ai:postgres-immich-builder:v1"
BUILD_ID = "d3f427770834e2d2c20337a9a3d3c816"
UPSTREAM_BASE = "docker.io/library/postgres:16.15-alpine3.24@sha256:ac838714b7d794185a866e5a6b479b954e7d8a070568aa0acac6fe07c3070bf5"
BASE = "sha256:c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a"
# Docker 29 identifica container.Image por el manifiesto, no por el JSON de configuración.
IMAGE_ID = BASE
HISTORICAL_SHA = "c77cdcbc4c327db69fd72eeaa01ef73a77fb333fa199b2f049129e6dcfe0899e"
KIT_SHA = "d3f427770834e2d2c20337a9a3d3c8161a191a16d2cb174ea2d66cdfab4b03a0"
INPUTS = "/opt/media-platform/build/contexts/postgres-immich/" + BUILD_ID + "/inputs"
OUTPUTS = "/var/lib/media-platform/build/postgres-immich/" + BUILD_ID + "/outputs"
CONTROL = "/var/lib/media-platform/build/postgres-immich/" + BUILD_ID + "/control"
STAGING = "/home/jandrade/.agencia-pg-immich-stage-" + BUILD_ID
BASE_ENV = [
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "GOSU_VERSION=1.19",
    "LANG=en_US.utf8",
    "PG_MAJOR=16",
    "PG_VERSION=16.15",
    "PG_SHA256=c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed",
    "DOCKER_PG_LLVM_DEPS=llvm21-dev \t\tclang21",
    "PGDATA=/var/lib/postgresql/data",
]
BUILD_ENV = ["AGENCY_INPUT_MANIFEST_SHA256=" + KIT_SHA, "AGENCY_BUILD_OWNER=" + OWNER]
MAX_OUTPUT = 4 * 1024**2
MAX_TOTAL = 768 * 1024**2
MAX_FILE = 256 * 1024**2
SHA = re.compile(r"[a-f0-9]{64}")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON con claves repetidas.")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=unique)


def validate_inventory(files, expected_checksum=KIT_SHA, *, expected_count=78):
    if not isinstance(files, dict) or len(files) != expected_count:
        raise ValueError("El kit no tiene el número exacto de archivos aprobado.")
    total = 0
    for name, item in files.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_+./@-]+", name)
            or name.startswith("/")
            or any(p in {"", ".", ".."} for p in name.split("/"))
            or not isinstance(item, dict)
            or set(item) != {"sha256", "size"}
            or not SHA.fullmatch(str(item["sha256"]))
            or type(item["size"]) is not int
            or not 0 < item["size"] <= MAX_FILE
        ):
            raise ValueError("Un archivo del kit tiene ruta, hash o tamaño no admitido.")
        total += item["size"]
    if total > MAX_TOTAL:
        raise ValueError("La transferencia supera el límite total del kit.")
    checksums = "".join(
        f"{item['sha256']}  {name}\n" for name, item in sorted(files.items())
    ).encode()
    if digest(checksums) != expected_checksum:
        raise ValueError("El inventario no coincide con SHA256SUMS respaldado.")
    return checksums


def require_backup(mode, actual, backed):
    required = {
        "create": ("prepare",),
        "start": ("prepare", "create"),
        "sdk": ("prepare", "create", "start"),
        "graph": ("prepare", "create", "start", "sdk"),
        "stop": ("prepare", "create"),
    }.get(mode, ())
    for stage in required:
        if stage not in actual or actual[stage] != backed.get(stage):
            raise ValueError("Falta el respaldo literal de un recibo previo a esta transición.")


def derive_create_arguments(arguments):
    if not isinstance(arguments, list) or arguments.count(UPSTREAM_BASE) != 1:
        raise ValueError("El histórico no contiene una única base upstream fijada.")
    position = arguments.index(UPSTREAM_BASE)
    return (
        arguments[:position]
        + [
            "--tmpfs",
            "/var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=1048576",
            "--user",
            "0:0",
            BASE,
        ]
        + arguments[position + 1 :]
    )


def check_base_image(image):
    if (
        not isinstance(image, list)
        or len(image) != 1
        or image[0].get("Id") != IMAGE_ID
        or image[0].get("Config", {}).get("User") != "70:70"
        or sorted(image[0].get("Config", {}).get("Env", [])) != sorted(BASE_ENV)
    ):
        raise ValueError(
            "La imagen propia local no conserva manifiesto, usuario y entorno fijados."
        )


def check_container(value, cid):
    if (
        not isinstance(cid, str)
        or not SHA.fullmatch(cid)
        or value.get("Id") != cid
        or value.get("Image") != IMAGE_ID
    ):
        raise ValueError("CID o imagen interna distintos del recurso propio.")
    config, host = value.get("Config", {}), value.get("HostConfig", {})
    labels = config.get("Labels") or {}
    if (
        labels.get("cl.e-scale.agencia.owner") != OWNER
        or labels.get("cl.e-scale.agencia.build-id") != BUILD_ID
        or config.get("Image") != BASE
        or config.get("Entrypoint") != ["/bin/sh"]
        or config.get("Cmd") != ["/inputs/offline-container.sh", "hold"]
        or config.get("WorkingDir") != "/work"
        or config.get("User") != "0:0"
        or sorted(config.get("Env", [])) != sorted(BASE_ENV + BUILD_ENV)
    ):
        raise ValueError("La configuración del contenedor no acredita el contrato propio.")
    expected = {
        "NetworkMode": "none",
        "NanoCpus": 4_000_000_000,
        "Memory": 12 * 1024**3,
        "MemorySwap": 12 * 1024**3,
        "PidsLimit": 256,
        "Privileged": False,
    }
    if (
        any(host.get(k) != v for k, v in expected.items())
        or host.get("CapAdd")
        or host.get("CapDrop") not in (["NET_RAW"], ["CAP_NET_RAW"])
        or host.get("SecurityOpt") != ["no-new-privileges"]
        or host.get("RestartPolicy", {}).get("Name") != "no"
        or host.get("PortBindings")
        or host.get("PublishAllPorts")
        or host.get("Binds")
        or host.get("VolumesFrom")
        or host.get("Devices")
        or host.get("DeviceRequests")
        or host.get("PidMode")
        or host.get("IpcMode", "private") != "private"
        or host.get("UsernsMode")
        or host.get("Tmpfs") != {"/var/lib/postgresql/data": "rw,nosuid,nodev,noexec,size=1048576"}
    ):
        raise ValueError("NoForeign: límites, capacidades, red, puertos o namespaces distintos.")
    mounts = host.get("Mounts")
    expected_mounts = {(INPUTS, "/inputs", True), (OUTPUTS, "/outputs", False)}
    if not isinstance(mounts, list) or len(mounts) != 2:
        raise ValueError("NoForeign: se requieren exactamente dos binds declarados.")
    seen = set()
    for mount in mounts:
        if (
            mount.get("Type") != "bind"
            or mount.get("BindOptions", {}).get("Propagation", "rprivate") != "rprivate"
            or mount.get("VolumeOptions")
            or mount.get("TmpfsOptions")
        ):
            raise ValueError("NoForeign: tipo o propagación de montaje no admitidos.")
        seen.add((mount.get("Source"), mount.get("Target"), mount.get("ReadOnly", False)))
    if seen != expected_mounts:
        raise ValueError("NoForeign: los binds no coinciden con las rutas propias.")
    observed = value.get("Mounts", [])
    actual_binds = set()
    destinations = set()
    for mount in observed:
        destination = mount.get("Destination")
        if destination in destinations:
            raise ValueError("NoForeign: destino de montaje repetido.")
        destinations.add(destination)
        if mount.get("Type") == "bind":
            actual_binds.add((mount.get("Source"), mount.get("Destination"), not mount.get("RW")))
        elif not (
            mount.get("Type") == "tmpfs"
            and mount.get("Destination") == "/var/lib/postgresql/data"
            and not mount.get("Name")
            and not mount.get("Source")
        ):
            raise ValueError("NoForeign: volumen anónimo o montaje adicional.")
    if actual_binds != expected_mounts or len(observed) not in (2, 3):
        raise ValueError("NoForeign: el inventario efectivo de montajes cambió.")
    return {
        "cid": cid,
        "image_id": value["Image"],
        "created": value.get("Created"),
        "running": value.get("State", {}).get("Running") is True,
        "pid": value.get("State", {}).get("Pid"),
        "started_at": value.get("State", {}).get("StartedAt"),
        "limits": expected,
        "no_foreign": True,
    }


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
                values["memory.max"] != str(12 * 1024**3)
                or values["memory.swap.max"] != "0"
                or values["pids.max"] != "256"
                or not quota.isdigit()
                or not period.isdigit()
                or int(period) <= 0
                or int(quota) != 4 * int(period)
            ):
                raise ValueError("El kernel no acredita los límites 4 CPU/12 GiB/256 procesos.")
            return {"verified": True, "pid": pid, "cgroup": relative, "values": values}
        finally:
            os.close(descriptor)


def stop_owned(docker, cid):
    before = check_container(docker.inspect(cid, timeout=2), cid)
    killed = False
    if before["running"]:
        try:
            docker.command(["stop", "--time", "5", cid], timeout=6)
        except (TimeoutError, ValueError):
            pass
        if check_container(docker.inspect(cid, timeout=2), cid)["running"]:
            docker.command(["kill", cid], timeout=2)
            killed = True
    after = check_container(docker.inspect(cid, timeout=2), cid)
    if after["running"]:
        raise RuntimeError("No se acredita la detención del CID propio.")
    return {
        "cid": cid,
        "stopped": True,
        "kill_used": killed,
        "before_pid": before["pid"],
        "after_pid": after["pid"],
    }


class StageError(RuntimeError):
    def __init__(self, cause, cleanup):
        super().__init__("La etapa falló; se intentó detener únicamente el CID acreditado.")
        self.cause, self.cleanup = cause, cleanup


def execute_stage(docker, mode, cid, sink, budget):
    if mode not in {"sdk", "graph"} or not 20 <= budget <= {"sdk": 600, "graph": 180}[mode]:
        raise ValueError("Etapa o presupuesto fuera del contrato.")
    deadline = time.monotonic() + budget
    before = check_container(docker.inspect(cid, timeout=2), cid)
    if not before["running"]:
        raise ValueError("La etapa requiere el contenedor propio en ejecución.")
    try:
        effective = docker.limits(before)
        result = docker.command(
            ["exec", cid, "/bin/sh", "/inputs/offline-container.sh", mode],
            timeout=max(0.1, deadline - time.monotonic() - 20),
            sink=sink,
        )
        if result["returncode"] != 0:
            failure = ValueError("El proceso de la etapa devolvió un código no cero.")
            failure.process = result["process"]
            raise failure
        after = check_container(docker.inspect(cid, timeout=2), cid)
        if not after["running"] or (after["pid"], after["started_at"]) != (
            before["pid"],
            before["started_at"],
        ):
            raise ValueError("La identidad de ejecución cambió durante la etapa.")
        return {
            "process": result["process"],
            "container_before": before,
            "container_after": after,
            "effective_limits": effective,
        }
    except BaseException as error:
        try:
            cleanup = stop_owned(docker, cid)
        except BaseException as failure:
            cleanup = {"stopped": False, "error_type": type(failure).__name__}
        raise StageError(error, cleanup) from error


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


def load_layout(request):
    source = base64.b64decode(request["layout_source_b64"], validate=True)
    if len(source) > 256 * 1024 or digest(source) != request["plan"]["layout_helper_sha256"]:
        raise ValueError("El verificador del layout difiere del respaldo.")
    module = types.ModuleType("pg_sdk_layout")
    exec(compile(source, "<layout-propio-fijado>", "exec"), module.__dict__)
    return module


def check_request(request):
    plan = request["plan"]
    if (
        plan.get("schema_version") != 1
        or plan.get("plan_revision") != 2
        or plan.get("owner") != OWNER
        or plan.get("build_id") != BUILD_ID
        or plan.get("staging") != STAGING
        or plan.get("inputs") != INPUTS
        or plan.get("outputs") != OUTPUTS
        or plan.get("control") != CONTROL
        or plan.get("historical_sha256") != HISTORICAL_SHA
        or plan.get("kit_sha256") != KIT_SHA
        or plan.get("target")
        != {"hostname": "vm-media-01", "ip": "192.168.0.85", "port": 22, "user": "jandrade"}
        or plan.get("bound_to_vault") is not True
        or digest(canonical({k: v for k, v in plan.items() if k != "plan_sha256"}))
        != plan.get("plan_sha256")
        or plan.get("remote_sha256") != globals().get("_SOURCE_SHA256")
    ):
        raise ValueError("El plan no acredita fuente, rutas y hashes fijos del rol PostgreSQL.")
    validate_inventory(plan["files"])
    historic = base64.b64decode(plan["historical_plan_b64"], validate=True)
    if len(historic) > 65536 or digest(historic) != HISTORICAL_SHA:
        raise ValueError("El plan histórico fue alterado.")
    historical = json_object(historic)
    expected = derive_create_arguments(historical["create_arguments"])
    if plan["create_arguments"] != expected:
        raise ValueError(
            "Los argumentos no conservan el histórico con base propia, UID y tmpfs aprobados."
        )
    return plan


def verify_host(request, *, root=True):
    layout = load_layout(request)
    host = layout.inspect_host()
    layout.host_contract(host, apply=root)
    if not root:
        if os.geteuid() != 1000:
            raise ValueError("El receptor requiere exclusivamente UID 1000.")
        return host
    with layout.LocalFilesystem() as filesystem:
        targets = layout.make_plan(filesystem.inspect(), host)["inventory"]["targets"]
    expected = request["plan"]["layout"]
    if (
        expected.get("success") is not True
        or expected.get("owner") != layout.OWNER
        or len(expected.get("targets", [])) != 2
        or {t.get("path") for t in expected["targets"]}
        != {"/opt/media-platform", "/var/lib/media-platform"}
    ):
        raise ValueError("Falta el layout respaldado.")
    for item in expected["targets"]:
        actual = targets[item["path"]]
        if (
            layout.directory_identity(actual["node"]) != item["directory"]
            or layout.digest(actual["marker"]) != item["marker_sha256"]
            or layout.digest(actual["receipt"]) != item["receipt_sha256"]
        ):
            raise ValueError("El layout real no coincide con los inodos y recibos respaldados.")
    return host


def receive(request, stream):
    plan = check_request(request)
    verify_host(request, root=False)
    home = open_directory("/home/jandrade", uid=1000)
    name = PurePosixPath(STAGING).name
    stage = None
    try:
        os.mkdir(name, 0o700, dir_fd=home)
        stage = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
        os.fchmod(stage, 0o700)
        checked_node(os.fstat(stage), 1000, directory=True)
        marker = {
            "owner": OWNER,
            "path": STAGING,
            "directory": identity(os.fstat(stage)),
            "plan_sha256": plan["plan_sha256"],
        }
        write_file(stage, ".owner.json", canonical(marker), uid=1000)
        for filename, expected in sorted(plan["files"].items()):
            parent, leaf = open_parent(stage, filename, create=True, uid=1000)
            try:
                descriptor = os.open(
                    leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                with os.fdopen(descriptor, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    checked_node(os.fstat(output.fileno()), 1000)
                    sha, remaining = hashlib.sha256(), expected["size"]
                    while remaining:
                        chunk = exact_read(stream, min(65536, remaining))
                        remaining -= len(chunk)
                        output.write(chunk)
                        sha.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                    if sha.hexdigest() != expected["sha256"]:
                        raise ValueError("El archivo transferido no coincide con su hash.")
                os.fsync(parent)
            finally:
                os.close(parent)
        write_file(stage, "SHA256SUMS", validate_inventory(plan["files"]), uid=1000)
        if stream.read(1):
            raise ValueError("La transferencia incluye bytes adicionales no declarados.")
        return {
            "success": True,
            "stage": "transfer",
            "source_sha256": plan["remote_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "owner": OWNER,
            "staging": STAGING,
            "directory": identity(os.fstat(stage)),
            "marker_sha256": digest(canonical(marker)),
            "files": len(plan["files"]),
        }
    finally:
        if stage is not None:
            os.close(stage)
        os.close(home)


MARKER = ".pg-sdk-owner.json"


def marked_directory(path, *, create=False, shared=False):
    parts = PurePosixPath(path).parts
    parent = open_directory(str(PurePosixPath(path).parent))
    try:
        if create:
            os.mkdir(parts[-1], 0o700, dir_fd=parent)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
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
                if (
                    actual.get("owner")
                    not in (
                        {OWNER, "agencia-digital-marketing-ai:component-build:v4.1"}
                        if shared
                        else {OWNER}
                    )
                    or actual.get("path") != path
                    or actual.get("directory") != expected["directory"]
                ):
                    raise ValueError("Un marcador no acredita el directorio propio esperado.")
            if identity(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != identity(
                os.fstat(descriptor)
            ):
                raise ValueError("El directorio cambió al abrirse.")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent)


def common_directories():
    paths = [
        "/opt/media-platform/build",
        "/opt/media-platform/build/contexts",
        "/opt/media-platform/build/contexts/postgres-immich",
        "/var/lib/media-platform/build",
        "/var/lib/media-platform/build/postgres-immich",
    ]
    for path in paths:
        parent = open_directory(str(PurePosixPath(path).parent))
        try:
            try:
                os.stat(PurePosixPath(path).name, dir_fd=parent, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                exists = False
        finally:
            os.close(parent)
        shared = path in paths[:2] + paths[3:4]
        if shared and not exists:
            raise ValueError("Falta un ancestro compartido del sistema de builds ya preparado.")
        descriptor = marked_directory(path, create=not exists, shared=shared)
        os.close(descriptor)


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


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def exact_tree(directory, files, uid):
    found = set()

    def walk(descriptor, prefix=""):
        with os.scandir(descriptor) as entries:
            for entry in entries:
                name = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if name not in {"apk", "rust"}:
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


class Store:
    def __init__(self, request):
        self.request, self.plan = request, request["plan"]
        self.descriptors = {}

    def close(self):
        for descriptor in self.descriptors.values():
            os.close(descriptor)
        self.descriptors.clear()

    def open(self):
        for key, path in {"inputs": INPUTS, "outputs": OUTPUTS, "control": CONTROL}.items():
            self.descriptors[key] = marked_directory(path)
        return self

    def snapshot(self):
        result = {}
        for key, path in {"inputs": INPUTS, "outputs": OUTPUTS, "control": CONTROL}.items():
            descriptor = self.descriptors[key]
            checked_node(os.fstat(descriptor), 0, directory=True)
            current = open_directory(path)
            try:
                if identity(os.fstat(current)) != identity(os.fstat(descriptor)):
                    raise ValueError("La ruta propia dejó de corresponder al inodo retenido.")
            finally:
                os.close(current)
            result[key] = {
                "path": path,
                "directory": identity(os.fstat(descriptor)),
                "marker_sha256": digest(read_file(descriptor, MARKER)),
            }
        if read_file(self.descriptors["outputs"], ".owner") != OWNER.encode():
            raise ValueError("El marcador del builder fue alterado.")
        return result

    def receipt(self, stage):
        try:
            data = read_file(self.descriptors["control"], stage + ".json")
        except FileNotFoundError:
            return None
        value = json_object(data)
        if canonical(value) != data or value.get("plan_sha256") != self.plan["plan_sha256"]:
            raise ValueError("Un recibo de control no corresponde al plan canónico.")
        return envelope(value)

    def receipts(self):
        return {
            name: result
            for name in ("prepare", "create", "start", "sdk", "graph", "stop")
            if (result := self.receipt(name)) is not None
        }

    def verify(self):
        prepared = self.receipt("prepare")
        if prepared is None or prepared["receipt"].get("directories") != self.snapshot():
            raise ValueError("Los directorios propios cambiaron desde prepare.")
        exact_tree(self.descriptors["inputs"], set(self.plan["files"]) | {"SHA256SUMS", MARKER}, 0)
        for name, expected in self.plan["files"].items():
            file_digest(self.descriptors["inputs"], name, expected)
        if read_file(self.descriptors["inputs"], "SHA256SUMS", maximum=65536) != validate_inventory(
            self.plan["files"]
        ):
            raise ValueError("El inventario de inputs fue alterado.")
        return prepared

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

    def begin(self, stage):
        os.mkdir("." + stage + "-started", 0o700, dir_fd=self.descriptors["control"])

    def docker(self):
        configuration = CONTROL + "/docker-config"
        descriptor = open_directory(configuration)
        try:
            checked_node(os.fstat(descriptor), 0, directory=True)
            if read_file(descriptor, "config.json") != b"{}":
                raise ValueError("La configuración Docker privada cambió.")
        finally:
            os.close(descriptor)
        return Docker(configuration)

    def prepare(self):
        transfer = self.request["backed"].get("transfer")
        if (
            not isinstance(transfer, dict)
            or transfer.get("success") is not True
            or transfer.get("plan_sha256") != self.plan["plan_sha256"]
            or transfer.get("staging") != STAGING
        ):
            raise ValueError("Falta el respaldo del staging transferido antes de promoverlo.")
        source = open_directory(STAGING, uid=1000)
        try:
            checked_node(os.fstat(source), 1000, directory=True)
            marker = read_file(source, ".owner.json", uid=1000)
            expected_marker = {
                "owner": OWNER,
                "path": STAGING,
                "directory": identity(os.fstat(source)),
                "plan_sha256": self.plan["plan_sha256"],
            }
            if (
                marker != canonical(expected_marker)
                or digest(marker) != transfer["marker_sha256"]
                or transfer["directory"] != identity(os.fstat(source))
            ):
                raise ValueError("El staging cambió después del respaldo.")
            # Antes de modificar namespaces root se verifican todos los bytes recibidos.
            exact_tree(source, set(self.plan["files"]) | {"SHA256SUMS", ".owner.json"}, 1000)
            for name, expected in self.plan["files"].items():
                file_digest(source, name, expected, uid=1000)
            if read_file(source, "SHA256SUMS", uid=1000, maximum=65536) != validate_inventory(
                self.plan["files"]
            ):
                raise ValueError("El staging no contiene SHA256SUMS exacto.")
            common_directories()
            for path in (str(PurePosixPath(INPUTS).parent), str(PurePosixPath(OUTPUTS).parent)):
                descriptor = marked_directory(path, create=True)
                os.close(descriptor)
            for key, path in {"inputs": INPUTS, "outputs": OUTPUTS, "control": CONTROL}.items():
                self.descriptors[key] = marked_directory(path, create=True)
            for name, expected in self.plan["files"].items():
                copy_file(source, self.descriptors["inputs"], name, expected)
            write_file(
                self.descriptors["inputs"], "SHA256SUMS", validate_inventory(self.plan["files"])
            )
            write_file(self.descriptors["outputs"], ".owner", OWNER.encode())
            os.mkdir("docker-config", 0o700, dir_fd=self.descriptors["control"])
            config = os.open(
                "docker-config",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.descriptors["control"],
            )
            try:
                write_file(config, "config.json", b"{}")
            finally:
                os.close(config)
            result = self.put(
                "prepare",
                {
                    "success": True,
                    "directories": self.snapshot(),
                    "transfer": transfer,
                    "files": 78,
                    "kit_sha256": KIT_SHA,
                    "sdk_executed": False,
                    "docker_executed": False,
                },
            )
            self.verify()
            return result
        finally:
            os.close(source)


def validate_artifact(store, stage, previous):
    data = read_file(store.descriptors["outputs"], stage + "-receipt.json")
    value = json_object(data)
    common = {
        "stage": stage,
        "verified": True,
        "sdk_executed": True,
        "compilation_performed": False,
        "compile_enabled": False,
        "input_manifest_sha256": KIT_SHA,
        "cargo_lock_sha256": "dae0885db162d70f216360330c235669e9dda610a93b31eb593c5755fde856ab",
    }
    if any(value.get(k) != v for k, v in common.items()):
        raise ValueError("El recibo del SDK no acredita esta etapa.")
    if stage == "sdk":
        if (
            value.get("xtask_patched_sha256")
            != "9055ec9b313bcc827c52705f37bca6ceba4468de2073cb5de319e703684969b1"
        ):
            raise ValueError("El recibo SDK no acredita xtask frozen.")
    else:
        if (
            value.get("target_graph_observed") is not True
            or value.get("wasm_bindgen_shared_selected") is not False
            or value.get("compilation_equivalence_claimed") is not False
            or value.get("features_review_required_before_compile") is not True
            or value.get("requires_backup_before_compile") is not True
            or value.get("sdk_receipt_sha256")
            != previous["sdk"]["receipt"]["artifact_receipt_sha256"]
        ):
            raise ValueError("El recibo del grafo no corresponde al SDK respaldado.")
        for name, expected in [
            ("vchord-tree.txt", value["tree_sha256"]["vchord"]),
            ("xtask-tree.txt", value["tree_sha256"]["xtask"]),
            ("vchord-features.txt", value["features_sha256"]),
        ]:
            if (
                digest(read_file(store.descriptors["outputs"], name, maximum=256 * 1024))
                != expected
            ):
                raise ValueError("Un árbol no corresponde a su recibo observado.")
    return {"artifact_receipt_sha256": digest(data), "artifact": value}


def execute_request(request, mode):
    action_started = time.monotonic()
    plan = check_request(request)
    verify_host(request)
    store = Store(request)
    try:
        if mode == "prepare":
            return {"success": True, "result": store.prepare()}
        if mode == "stop":
            # La salida es escribible por el SDK: su alteración no puede impedir
            # detener el CID propio. Sólo se usa evidencia host protegida.
            store.descriptors["control"] = marked_directory(CONTROL)
            actual = store.receipts()
            require_backup(mode, actual, request["backed"])
            prepared = actual["prepare"]["receipt"]["directories"]["control"]["directory"]
            if identity(os.fstat(store.descriptors["control"])) != prepared:
                raise ValueError("El directorio de control cambió de identidad.")
            cid = actual["create"]["receipt"]["cid"]
            stopped = stop_owned(store.docker(), cid)
            if "stop" in actual:
                return {"success": True, "result": actual["stop"], "observation": stopped}
            return {"success": True, "result": store.put("stop", {"success": True, **stopped})}
        store.open()
        store.verify()
        actual = store.receipts()
        require_backup(mode, actual, request["backed"])
        for stage in {
            "create": ("prepare",),
            "start": ("prepare", "create"),
            "sdk": ("prepare", "create", "start"),
            "graph": ("prepare", "create", "start", "sdk"),
        }[mode]:
            if actual[stage]["receipt"].get("success") is not True:
                raise ValueError("Una etapa anterior falló; no se permite continuar.")
        docker = store.docker()
        if (
            docker.capture(["version", "--format", "{{.Server.Version}}"], timeout=5)
            .decode()
            .strip()
            != "29.9.3"
        ):
            raise ValueError("La versión del daemon Docker no es la revisada.")
        store.begin(mode)
        if mode == "create":
            image = json_object(docker.capture(["image", "inspect", BASE], timeout=10))
            check_base_image(image)
            result = docker.command(plan["create_arguments"], timeout=20)
            output = result["output"].decode("ascii").strip()
            if result["returncode"] != 0 or not SHA.fullmatch(output):
                raise ValueError("Docker create no devolvió un CID completo verificable.")
            request["created_cid"] = output
            write_file(store.descriptors["control"], "cid.txt", output.encode())
            observed = docker.inspect(output)
            if observed.get("State", {}).get("Status") != "created" or observed.get(
                "State", {}
            ).get("Running"):
                raise ValueError("El contenedor nuevo no permanece sin arrancar.")
            return {
                "success": True,
                "result": store.put(
                    "create",
                    {
                        "success": True,
                        "cid": output,
                        "container": check_container(observed, output),
                        "inspection_sha256": digest(canonical(observed)),
                        "process": result["process"],
                        "started": False,
                    },
                ),
            }
        cid = actual["create"]["receipt"]["cid"]
        if read_file(store.descriptors["control"], "cid.txt", maximum=128).decode() != cid:
            raise ValueError("El CID guardado no coincide con su recibo respaldado.")
        observed = docker.inspect(cid)
        before = check_container(observed, cid)
        if mode == "start":
            if observed.get("State", {}).get("Status") != "created" or before["running"]:
                raise ValueError("start sólo admite el CID creado y nunca iniciado.")
            try:
                process = docker.command(["start", cid], timeout=10)
                if process["returncode"] != 0 or process["output"].decode().strip() != cid:
                    raise ValueError("Docker start no confirmó el CID exacto.")
                after = check_container(docker.inspect(cid), cid)
                if not after["running"]:
                    raise ValueError("El contenedor no está en ejecución.")
                effective = docker.limits(after)
                result = store.put(
                    "start",
                    {
                        "success": True,
                        "cid": cid,
                        "container": after,
                        "effective_limits": effective,
                        "process": process["process"],
                    },
                )
                return {"success": True, "result": result}
            except BaseException as error:
                try:
                    cleanup = stop_owned(docker, cid)
                except BaseException as failure:
                    cleanup = {"stopped": False, "error_type": type(failure).__name__}
                return {
                    "success": False,
                    "result": store.put(
                        "start",
                        {
                            "success": False,
                            "cid": cid,
                            "error_type": type(error).__name__,
                            "cleanup": cleanup,
                        },
                    ),
                }
        started = actual["start"]["receipt"]["container"]
        if any(before[key] != started[key] for key in ("cid", "created", "pid", "started_at")):
            raise ValueError("El contenedor cambió desde el arranque respaldado.")
        budget = request.get("budget", {"sdk": 600, "graph": 180}[mode])
        if type(budget) not in (int, float) or not 20 <= budget <= {"sdk": 600, "graph": 180}[mode]:
            raise ValueError("Presupuesto de etapa inválido.")
        budget -= time.monotonic() - action_started
        if budget < 20:
            raise TimeoutError("La verificación previa agotó el presupuesto de la etapa.")
        docker.heartbeat_fd = request.get("_heartbeat_fd")
        log_name = mode + ".log"
        descriptor = os.open(
            log_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=store.descriptors["control"],
        )
        fields = {"success": False, "cid": cid, "budget_seconds": budget}
        with os.fdopen(descriptor, "wb", buffering=0) as sink:
            try:
                fields.update(execute_stage(docker, mode, cid, sink, budget))
                store.verify()
                fields.update(validate_artifact(store, mode, actual))
                fields["success"] = True
            except BaseException as error:
                if isinstance(error, StageError):
                    fields.update(
                        error_type=type(error.cause).__name__,
                        cleanup=error.cleanup,
                        process=getattr(error.cause, "process", None),
                    )
                else:
                    try:
                        cleanup = stop_owned(docker, cid)
                    except BaseException as failure:
                        cleanup = {"stopped": False, "error_type": type(failure).__name__}
                    fields.update(error_type=type(error).__name__, cleanup=cleanup)
            finally:
                sink.flush()
                os.fsync(sink.fileno())
        log = read_file(store.descriptors["control"], log_name, maximum=MAX_OUTPUT)
        fields.update(log_path=CONTROL + "/" + log_name, log_sha256=digest(log), log_bytes=len(log))
        return {"success": fields["success"], "result": store.put(mode, fields)}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("receive", "prepare", "create", "start", "sdk", "graph", "stop"),
        required=True,
    )
    args = parser.parse_args()
    os.umask(0o077)
    request = {}
    try:
        if platform.system() != "Linux":
            raise ValueError("El ejecutor remoto requiere Linux.")
        stream = sys.stdin.buffer.raw
        length = struct.unpack(">I", exact_read(stream, 4))[0]
        if not 0 < length <= 256 * 1024:
            raise ValueError("Cabecera de petición excesiva.")
        request = json_object(exact_read(stream, length))
        if args.mode == "receive":
            result = {"success": True, "result": receive(request, stream)}
        else:
            if args.mode in {"sdk", "graph"}:
                request["_heartbeat_fd"] = sys.stdin.fileno()
            elif stream.read(1):
                raise ValueError("Bytes adicionales fuera de la petición.")

            def interrupted(_signal, _frame):
                raise InterruptedError("La sesión fue interrumpida.")

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
