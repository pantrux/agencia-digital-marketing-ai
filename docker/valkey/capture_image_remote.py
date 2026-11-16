"""Captura acotada de una imagen fijada como datos; nunca inicia contenedores."""

import base64
import gzip
import hashlib
import io
import json
import os
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime

OWNER = "agencia-digital-marketing-ai:valkey-oci-capture:v1"
ROOT = "/var/lib/media-platform/valkey-oci-capture-de6fbe9626fea9f6-v1"
MANIFEST = "sha256:de6fbe9626fea9f6f297b6867d2c8cf8f0394ee5ed171613166fc093cb49d0a6"
CONFIG = "sha256:631d75bac338990caa2d3c1c9c7ba5f4c38584f7a4c04cdfb363d861a33fb692"
IMAGE = "docker.io/valkey/valkey@" + MANIFEST
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_LAYER = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"
HEX = re.compile(r"[0-9a-f]{64}")


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Clave JSON duplicada.")
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("JSON inválido.")),
    )


def envelope(value):
    return {"value": value, "sha256": digest(canonical(value))}


def check_envelope(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"value", "sha256"}
        or envelope(value["value"]) != value
    ):
        raise ValueError("Recibo no íntegro.")
    return value["value"]


def require_prepare(backed, physical, profile_sha256, directory):
    value = check_envelope(backed)
    if (
        backed != physical
        or value.get("owner") != OWNER
        or value.get("stage") != "prepare"
        or value.get("success") is not True
        or value.get("profile_sha256") != profile_sha256
        or value.get("directory") != directory
    ):
        raise ValueError("La preparación respaldada no acredita este directorio y perfil.")
    return value


def image_command(mode, output):
    if mode == "inspect":
        return ["/usr/bin/docker", "image", "inspect", IMAGE]
    if mode == "pull":
        return ["/usr/bin/docker", "image", "pull", "--platform", "linux/amd64", IMAGE]
    if mode == "save":
        # stdout queda unido al FD propio, nunca a una ruta elegida por Docker.
        return ["/usr/bin/docker", "image", "save", IMAGE]
    raise ValueError("Sólo se permiten inspect, pull y save del digest fijado.")


def missing_image(returncode, stderr):
    return (
        returncode == 1
        and stderr == ("Error response from daemon: No such image: " + IMAGE + "\n").encode()
    )


def validate_inspect(value):
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError("Inspect no contiene una imagen única.")
    item = value[0]
    if (
        item.get("Id") not in (MANIFEST, CONFIG)
        or item.get("Architecture") != "amd64"
        or item.get("Os") != "linux"
        or not isinstance(item.get("RepoDigests"), list)
        or not {IMAGE, IMAGE.removeprefix("docker.io/")}.intersection(item["RepoDigests"])
    ):
        raise ValueError("Inspect no acredita la plataforma y los dos tipos de digest fijados.")
    return {key: item[key] for key in ("Id", "Architecture", "Os", "RepoDigests")}


def capture_commands(runner, output_fd):
    result = runner("inspect")
    pulled = False
    if result["returncode"]:
        if not missing_image(result["returncode"], result["stderr"]):
            raise ValueError("Inspect falló sin acreditar ausencia inequívoca; no se hará pull.")
        result = runner("pull")
        if result["returncode"]:
            raise ValueError("Falló el pull del único digest autorizado.")
        pulled = True
        result = runner("inspect")
    if result["returncode"]:
        raise ValueError("No se pudo inspeccionar la imagen fijada.")
    inspected = validate_inspect(decode(result["stdout"]))
    if runner("save", output_fd)["returncode"]:
        raise ValueError("Docker save no terminó correctamente.")
    return {"pulled": pulled, "inspect": inspected}


def identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def check_node(info, *, uid=0, directory=False):
    if (
        info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)
    ):
        raise ValueError("Nodo ajeno, enlazado o con permisos incompatibles.")


def write_new(parent, name, raw, *, uid=0):
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        check_node(os.fstat(output.fileno()), uid=uid)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    os.fsync(parent)


def read_owned(parent, name, *, uid=0, maximum=65536):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        check_node(info, uid=uid)
        if info.st_size > maximum:
            raise ValueError("Archivo propio excede el límite.")
        raw = source.read(maximum + 1)
        if len(raw) != info.st_size or stamp(os.fstat(source.fileno())) != stamp(info):
            raise ValueError("El archivo cambió durante la lectura.")
    return raw


def create_namespace(parent, name, profile_sha256, *, uid=0):
    os.mkdir(name, 0o700, dir_fd=parent)
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        os.fchmod(descriptor, 0o700)
        check_node(os.fstat(descriptor), uid=uid, directory=True)
        marker = {
            "schema": 1,
            "owner": OWNER,
            "path": ROOT,
            "profile_sha256": profile_sha256,
            "directory": identity(os.fstat(descriptor)),
        }
        write_new(descriptor, ".owner.json", canonical(marker), uid=uid)
        return marker
    finally:
        os.close(descriptor)


def open_owned(parent, name, profile_sha256, *, uid=0):
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        check_node(os.fstat(descriptor), uid=uid, directory=True)
        marker = decode(read_owned(descriptor, ".owner.json", uid=uid))
        if marker != {
            "schema": 1,
            "owner": OWNER,
            "path": ROOT,
            "profile_sha256": profile_sha256,
            "directory": identity(os.fstat(descriptor)),
        }:
            raise ValueError("El marcador no acredita la identidad propia.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _blob_name(value):
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or not HEX.fullmatch(value[7:])
    ):
        raise ValueError("Digest OCI no admitido.")
    return "blobs/sha256/" + value[7:]


def validate_oci(stream, expected):
    """Verifica el contenedor OCI y DiffID sin extraer ni ejecutar sus miembros."""
    stream.seek(0, io.SEEK_END)
    size = stream.tell()
    if not 0 < size <= MAX_ARCHIVE:
        raise ValueError("TAR fuera del límite de 256 MiB.")
    stream.seek(0)
    names, members = set(), {}
    with tarfile.open(fileobj=stream, mode="r:") as archive:
        for item in archive:
            name = item.name.rstrip("/") if item.isdir() else item.name
            if (
                name in names
                or not name
                or name.startswith("/")
                or "\\" in name
                or any(p in ("", ".", "..") for p in name.split("/"))
                or len(names) >= 128
            ):
                raise ValueError("Nombre OCI duplicado o inseguro.")
            names.add(name)
            if item.isdir() and name in ("blobs", "blobs/sha256"):
                continue
            if not item.isreg() or item.size < 0 or item.size > MAX_ARCHIVE:
                raise ValueError("El TAR contiene enlaces o tipos/tamaños no admitidos.")
            if name not in ("oci-layout", "index.json", "manifest.json", "repositories"):
                if not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name):
                    raise ValueError("Miembro OCI no reconocido.")
            source = archive.extractfile(item)
            with source:
                raw = source.read(item.size + 1)
            if len(raw) != item.size:
                raise ValueError("Miembro OCI truncado.")
            if name.startswith("blobs/") and digest(raw) != name.rsplit("/", 1)[1]:
                raise ValueError("Un blob OCI no coincide con su SHA256.")
            members[name] = raw
    if decode(members.get("oci-layout", b"{}")) != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("Formato OCI no reconocido.")
    index = decode(members.get("index.json", b"{}"))
    rows = index.get("manifests", [])
    if index.get("schemaVersion") != 2 or len(rows) != 1:
        raise ValueError("El índice no contiene exclusivamente la imagen fijada.")
    row = rows[0]
    if row.get("digest") != expected["platform_digest"]:
        raise ValueError("El índice no apunta al manifiesto fijado.")
    manifest_name = _blob_name(row["digest"])
    manifest_raw = members[manifest_name]
    manifest = decode(manifest_raw)
    if (
        row.get("size") != len(manifest_raw)
        or manifest != expected["manifest"]
        or manifest.get("schemaVersion") != 2
    ):
        raise ValueError("El manifiesto no coincide con el candidato fijado.")
    required = {manifest_name}
    config_row = manifest["config"]
    config_name = _blob_name(config_row["digest"])
    config_raw = members[config_name]
    required.add(config_name)
    config = decode(config_raw)
    layers = manifest["layers"]
    if (
        len(config_raw) != config_row["size"]
        or config.get("os") != "linux"
        or config.get("architecture") != "amd64"
        or not 0 < len(layers) <= 32
        or config.get("rootfs", {}).get("type") != "layers"
        or len(config["rootfs"].get("diff_ids", [])) != len(layers)
    ):
        raise ValueError("Configuración/plataforma/capas OCI incompatibles.")
    expanded = 0
    for layer, diff_id in zip(layers, config["rootfs"]["diff_ids"], strict=True):
        name = _blob_name(layer["digest"])
        required.add(name)
        raw = members[name]
        if (
            len(raw) != layer["size"]
            or layer.get("mediaType") != "application/vnd.oci.image.layer.v1.tar+gzip"
        ):
            raise ValueError("Capa OCI de tamaño o tipo diferente.")
        sha, length = hashlib.sha256(), 0
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as source:
            while chunk := source.read(65536):
                length += len(chunk)
                expanded += len(chunk)
                if length > MAX_LAYER or expanded > MAX_EXPANDED:
                    raise ValueError("Capa expandida fuera del límite.")
                sha.update(chunk)
        if diff_id != "sha256:" + sha.hexdigest():
            raise ValueError("DiffID de capa diferente.")
    if {name for name in members if name.startswith("blobs/")} != required:
        raise ValueError("El TAR incorpora blobs ajenos a la imagen única.")
    return {
        "manifest_digest": row["digest"],
        "config_digest": config_row["digest"],
        "layers_verified": len(layers),
        "expanded_bytes": expanded,
        "binary_accepted": False,
        "sca_executed": False,
    }


def check_request(request):
    if set(request) != {
        "stage",
        "profile",
        "profile_sha256",
        "layout",
        "layout_source_b64",
        "prepare",
        "capture",
    } or request["stage"] not in ("prepare", "capture", "fetch"):
        raise ValueError("Petición fuera del contrato cerrado.")
    profile = request["profile"]
    candidate = profile["candidate"]
    if (
        digest(canonical(profile)) != request["profile_sha256"]
        or profile["namespace"] != ROOT
        or profile["owner"] != OWNER
        or profile["max_archive_bytes"] != MAX_ARCHIVE
        or profile["binary_accepted"] is not False
        or candidate["image"] != IMAGE
        or candidate["platform_digest"] != MANIFEST
        or candidate["manifest"]["config"]["digest"] != CONFIG
    ):
        raise ValueError("El perfil no acredita las rutas y los digests fijados.")
    raw = base64.b64decode(request["layout_source_b64"], validate=True)
    if len(raw) > 65536 or digest(raw) != LAYOUT_SHA:
        raise ValueError("La fuente de layout no coincide con su pin.")
    return raw


def verify_host(request):
    raw = check_request(request)
    layout = {"__name__": "layout_valkey", "__file__": "host_layout.py"}
    exec(compile(raw, "host_layout-fijado", "exec"), layout)
    host = layout["inspect_host"]()
    layout["host_contract"](host, apply=True)
    with layout["LocalFilesystem"]() as filesystem:
        targets = layout["make_plan"](filesystem.inspect(), host)["inventory"]["targets"]
    proof = request["layout"]
    if (
        proof.get("success") is not True
        or proof.get("owner") != layout["OWNER"]
        or len(proof.get("targets", [])) != 2
        or {x.get("path") for x in proof["targets"]}
        != {"/opt/media-platform", "/var/lib/media-platform"}
    ):
        raise ValueError("Falta la identidad respaldada del layout existente.")
    for item in proof["targets"]:
        actual = targets[item["path"]]
        if (
            layout["directory_identity"](actual["node"]) != item["directory"]
            or layout["digest"](actual["marker"]) != item["marker_sha256"]
            or layout["digest"](actual["receipt"]) != item["receipt_sha256"]
        ):
            raise ValueError("El layout real difiere del respaldado.")
    return host


def open_parent():
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ("var", "lib", "media-platform"):
            other = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = other
            info = os.fstat(descriptor)
            if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("Ancestro ajeno o modificable por otro usuario.")
        check_node(os.fstat(descriptor), directory=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def cleanup_process(process):
    """No señala un PGID si waitid no acredita que el hijo sigue retenido."""
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    timer, started = None, time.monotonic()
    try:
        timer = signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            retained = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError as error:
            raise ValueError("El hijo ya no está retenido; no se señalará su PGID.") from error
        if retained is not None and retained.si_pid != process.pid:
            raise ValueError("waitid no acredita el hijo propio; no se señalará su PGID.")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
    finally:
        try:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        finally:
            try:
                if timer is not None:
                    remaining, interval = timer
                    # Una alarma vencida se entrega después de cerrar los recursos propios.
                    remaining = (
                        max(0.000001, remaining - (time.monotonic() - started)) if remaining else 0
                    )
                    signal.setitimer(signal.ITIMER_REAL, remaining, interval)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def run_image(mode, output_fd, deadline, *, heartbeat_fd=None):
    """El PID permanece sin recolectar hasta cerrar sólo su grupo de procesos."""
    environment = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": ROOT,
        "DOCKER_CONFIG": ROOT + "/docker-config",
        "LANG": "C",
        "LC_ALL": "C",
    }

    def limits():
        import resource

        if output_fd is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARCHIVE, MAX_ARCHIVE))

    process = None
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    pulse_until = time.monotonic() + 5
    old_blocking = os.get_blocking(heartbeat_fd) if heartbeat_fd is not None else None
    try:
        process = subprocess.Popen(
            image_command(mode, None),
            stdin=subprocess.DEVNULL,
            stdout=output_fd if output_fd is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            env=environment,
            preexec_fn=limits,
        )
        if heartbeat_fd is not None:
            os.set_blocking(heartbeat_fd, False)
        with selectors.DefaultSelector() as selector:
            for name in ("stdout", "stderr"):
                pipe = getattr(process, name)
                if pipe is not None:
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Docker agotó el plazo de la captura.")
                if heartbeat_fd is not None:
                    try:
                        pulse = os.read(heartbeat_fd, 4096)
                    except BlockingIOError:
                        pulse = None
                    if pulse == b"" or (pulse is not None and pulse.replace(b".", b"")):
                        raise InterruptedError("El canal del responsable se cerró o fue alterado.")
                    if pulse:
                        pulse_until = time.monotonic() + 5
                    if time.monotonic() >= pulse_until:
                        raise TimeoutError("Se perdió el latido del responsable.")
                if output_fd is not None and os.fstat(output_fd).st_size > MAX_ARCHIVE:
                    raise ValueError("Docker save excedió 256 MiB; se conserva el parcial.")
                for key, _mask in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        outputs[key.data].extend(chunk)
                        if len(outputs[key.data]) > 1024 * 1024:
                            raise ValueError("Docker excedió la salida diagnóstica acotada.")
                ended = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                if ended is not None and not selector.get_map():
                    break
    finally:
        try:
            if process is not None:
                cleanup_process(process)
        finally:
            if heartbeat_fd is not None:
                os.set_blocking(heartbeat_fd, old_blocking)
    return {"returncode": process.returncode, **{k: bytes(v) for k, v in outputs.items()}}


def frame(output, value):
    raw = canonical(value)
    if len(raw) > 65536:
        raise ValueError("Recibo excede el límite.")
    output.write(struct.pack(">I", len(raw)))
    output.write(raw)
    output.flush()


def _same_namespace(parent, descriptor):
    path_info = os.stat(ROOT.rsplit("/", 1)[1], dir_fd=parent, follow_symlinks=False)
    check_node(path_info, directory=True)
    if identity(path_info) != identity(os.fstat(descriptor)):
        raise ValueError("La ruta propia cambió de identidad.")


def execute(request, output, *, heartbeat_fd=None):
    verify_host(request)
    stage, profile_sha = request["stage"], request["profile_sha256"]
    parent, directory = open_parent(), None
    deadline = time.monotonic() + (300 if stage == "capture" else 120)
    try:
        name = ROOT.rsplit("/", 1)[1]
        if stage == "prepare":
            create_namespace(parent, name, profile_sha)
        directory = open_owned(parent, name, profile_sha)
        marker_raw = read_owned(directory, ".owner.json")
        common = {
            "owner": OWNER,
            "stage": stage,
            "success": True,
            "profile_sha256": profile_sha,
            "directory": identity(os.fstat(directory)),
            "marker_sha256": digest(marker_raw),
            "path": ROOT,
            "checked_at_utc": datetime.now(UTC).isoformat(),
            "binary_accepted": False,
        }
        if stage == "prepare":
            os.mkdir("docker-config", 0o700, dir_fd=directory)
            result = envelope(common)
            write_new(directory, "prepare.json", canonical(result))
            _same_namespace(parent, directory)
            frame(output, result)
            return
        physical = decode(read_owned(directory, "prepare.json"))
        prepared = require_prepare(request["prepare"], physical, profile_sha, common["directory"])
        if prepared.get("marker_sha256") != common["marker_sha256"]:
            raise ValueError("El marcador no coincide con la preparación respaldada.")
        if stage == "capture":
            configuration = os.open(
                "docker-config", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            try:
                check_node(os.fstat(configuration), directory=True)
                if os.listdir(configuration):
                    raise ValueError(
                        "La configuración Docker propia dejó de estar vacía; no se adopta."
                    )
            finally:
                os.close(configuration)
            write_new(directory, "capture.intent.json", canonical(common))
            descriptor = os.open(
                "image.tar.partial",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "w+b") as capture:
                os.fchmod(capture.fileno(), 0o600)
                check_node(os.fstat(capture.fileno()))
                commands = capture_commands(
                    lambda mode, fd=None: run_image(mode, fd, deadline, heartbeat_fd=heartbeat_fd),
                    capture.fileno(),
                )
                capture.flush()
                os.fsync(capture.fileno())
                info = os.fstat(capture.fileno())
                check_node(info)
                proof = validate_oci(capture, request["profile"]["candidate"])
                capture.seek(0)
                sha = hashlib.file_digest(capture, "sha256").hexdigest()
                if stamp(os.fstat(capture.fileno())) != stamp(info):
                    raise ValueError("La captura cambió durante la verificación.")
            _same_namespace(parent, directory)
            result = envelope(
                {
                    **common,
                    "archive": {
                        "name": "image.tar.partial",
                        "size": info.st_size,
                        "sha256": sha,
                        **identity(info),
                    },
                    "oci": proof,
                    **commands,
                }
            )
            write_new(directory, "capture.json", canonical(result))
            frame(output, result)
            return
        captured = request["capture"]
        physical_capture = decode(read_owned(directory, "capture.json"))
        receipt = check_envelope(captured)
        if (
            captured != physical_capture
            or receipt.get("stage") != "capture"
            or receipt.get("success") is not True
            or receipt.get("owner") != OWNER
            or receipt.get("profile_sha256") != profile_sha
            or receipt.get("directory") != common["directory"]
            or receipt.get("marker_sha256") != common["marker_sha256"]
        ):
            raise ValueError("Falta la captura física respaldada.")
        write_new(directory, "fetch.intent.json", canonical(common))
        descriptor = os.open("image.tar.partial", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            check_node(info)
            archive = receipt["archive"]
            if (
                identity(info) != {key: archive[key] for key in ("device", "inode")}
                or info.st_size != archive["size"]
            ):
                raise ValueError("La captura perdió su identidad.")
            if hashlib.file_digest(source, "sha256").hexdigest() != archive["sha256"]:
                raise ValueError("El SHA256 de la captura física cambió.")
            source.seek(0)
            frame(output, {"stage": "fetch-header", "capture": captured})
            sha, size = hashlib.sha256(), 0
            while chunk := source.read(65536):
                if time.monotonic() >= deadline:
                    raise TimeoutError("La transferencia agotó su plazo.")
                output.write(chunk)
                sha.update(chunk)
                size += len(chunk)
            if (
                stamp(os.fstat(source.fileno())) != stamp(info)
                or sha.hexdigest() != archive["sha256"]
                or size != archive["size"]
            ):
                raise ValueError("La captura cambió durante la transferencia.")
        _same_namespace(parent, directory)
        result = envelope(
            {
                **common,
                "archive_sha256": sha.hexdigest(),
                "archive_bytes": size,
                "capture_receipt_sha256": captured["sha256"],
            }
        )
        write_new(directory, "fetch.json", canonical(result))
        frame(output, result)
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)


def main(request):
    def expired(_sig, _frame):
        raise TimeoutError("El recolector alcanzó su plazo externo.")

    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, expired)
    signal.alarm(305 if request.get("stage") == "capture" else 125)
    try:
        execute(request, sys.stdout.buffer, heartbeat_fd=sys.stdin.fileno())
        return 0
    except Exception:
        # No se imprime stderr del daemon ni otra configuración del host.
        frame(
            sys.stdout.buffer,
            {"success": False, "stage": request.get("stage"), "error": "CAPTURA_RECHAZADA"},
        )
        return 1
    finally:
        signal.alarm(0)
