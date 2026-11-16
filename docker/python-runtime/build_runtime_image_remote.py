"""Operaciones finitas de Root sobre una imagen propia; nunca inicia contenedores."""

import base64
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
import types

OWNER = "agencia-digital-marketing-ai:python-runtime-image:v1"
ROOT = "/var/lib/media-platform/build/python-runtime-image-v1"
PARENT = "/var/lib/media-platform/build"
LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"
CAPTURE_SHA = "515f17990013507d254f8f1555746bcce006191292bde10cb3a6f46b6cd69002"
STAGES = ("prepare", "receive", "base", "build", "final", "fetch")
REQUIRED_INPUTS = {
    "context.tar",
    "inventory.json",
    "base-os19.cdx.json",
    "manual-runtime14.cdx.json",
    "runtime-image-gate.json",
}
MAX_ARCHIVE = 256 * 1024**2
MAX_LOG = 1024**2


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def envelope(value):
    return {"value": value, "sha256": digest(canonical(value))}


def unpack(value):
    require(
        type(value) is dict
        and set(value) == {"value", "sha256"}
        and value == envelope(value["value"]),
        "Recibo no íntegro.",
    )
    return value["value"]


def stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def node(info, *, directory=False, metadata=False):
    require(
        info.st_uid == 0
        and info.st_gid == 0
        and stat.S_IMODE(info.st_mode)
        in ((0o700,) if directory else (0o600, 0o644) if metadata else (0o600,))
        and (
            stat.S_ISDIR(info.st_mode)
            if directory
            else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        ),
        "Nodo ajeno/enlazado o modo incompatible.",
    )


def identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def open_directory(path, *, private=True):
    require(
        path.startswith("/") and all(name not in ("", ".", "..") for name in path[1:].split("/")),
        "Directorio absoluto no canónico.",
    )
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in path[1:].split("/"):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            require(
                info.st_uid == 0 and info.st_gid == 0 and not stat.S_IMODE(info.st_mode) & 0o022,
                "Ancestro no administrado.",
            )
        if private:
            node(os.fstat(descriptor), directory=True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def leaf_name(name):
    require(
        isinstance(name, str)
        and name not in ("", ".", "..")
        and not any(c in name for c in "/\\:\0"),
        "Nombre de salida no cerrado.",
    )


def read_leaf(parent, name, maximum=MAX_LOG):
    leaf_name(name)
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    metadata = name == "build-metadata.json"
    node(before, metadata=metadata)
    require(before.st_size <= maximum, "Archivo excede límite.")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(stamp(opened) == stamp(before), "Archivo cambiado antes de abrir.")
        raw = stream.read(maximum + 1)
        require(
            len(raw) == opened.st_size and stamp(os.fstat(stream.fileno())) == stamp(opened),
            "Archivo cambiado durante lectura.",
        )
    require(
        stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == stamp(before),
        "Archivo cambiado después de leer.",
    )
    return raw


def write_new(parent, name, raw):
    leaf_name(name)
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        node(os.fstat(stream.fileno()))
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(parent)
    require(read_leaf(parent, name, max(MAX_LOG, len(raw))) == raw, "Relectura distinta.")


def require_absent(directory, names):
    for name in names:
        leaf_name(name)
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise FileExistsError("No se adopta salida previa: " + name)


def exact(stream, size):
    require(type(size) is int and 0 <= size <= MAX_ARCHIVE, "Tamaño de flujo inválido.")
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(min(65536, size - len(data)))
        require(chunk, "Flujo truncado.")
        data.extend(chunk)
    return bytes(data)


def load(raw, expected, name):
    require(digest(raw) == expected, "Fuente fuera del pin: " + name)
    module = types.ModuleType(name)
    module.__file__ = "<" + name + ">"
    exec(compile(raw, module.__file__, "exec"), vars(module))
    return module


def modules(request):
    profile = request["profile"]
    require(
        profile["owner"] == OWNER
        and profile["namespace"] == ROOT
        and profile["image_built"] is False
        and profile["runtime_approved"] is False
        and profile["inputs"] is not None
        and request["stage"] in STAGES
        and request["profile_sha256"] == digest(canonical(profile)),
        "Perfil no acredita la operación.",
    )
    raws = {
        name: base64.b64decode(value, validate=True) for name, value in request["sources"].items()
    }
    require(
        set(raws) == {"inventory", "parser", "layout", "capture"},
        "Fuentes remotas fuera del cierre.",
    )
    require(all(len(raw) <= 131072 for raw in raws.values()), "Fuente excesiva.")
    inventory = load(raws["inventory"], profile["sources"]["inventory"], "python_runtime_inventory")
    parser = inventory.load_parser(raws["parser"])
    layout = load(raws["layout"], LAYOUT_SHA, "python_runtime_layout")
    capture = load(raws["capture"], CAPTURE_SHA, "python_runtime_capture_support")
    return inventory, parser, layout, capture


def verify_host(layout, proof):
    host = layout.inspect_host()
    layout.host_contract(host, apply=True)
    with layout.LocalFilesystem() as filesystem:
        current = layout.make_plan(filesystem.inspect(), host)["inventory"]["targets"]
    require(
        proof.get("success") is True
        and proof.get("owner") == layout.OWNER
        and len(proof.get("targets", [])) == 2
        and {row["path"] for row in proof["targets"]}
        == {"/opt/media-platform", "/var/lib/media-platform"},
        "Layout no respaldado.",
    )
    for row in proof["targets"]:
        actual = current[row["path"]]
        require(
            layout.directory_identity(actual["node"]) == row["directory"]
            and layout.digest(actual["marker"]) == row["marker_sha256"]
            and layout.digest(actual["receipt"]) == row["receipt_sha256"],
            "Layout físico distinto.",
        )
    return host


def run(
    arguments,
    seconds,
    capture,
    *,
    output_fd=None,
    input_fd=None,
    environment=None,
    heartbeat_fd=None,
):
    """Retiene el líder con WNOWAIT; sólo su PGID se cierra mediante helper fijado."""
    require(
        os.name == "posix" and 0 < seconds <= 180 and arguments[0] == "/usr/bin/docker",
        "Comando/plazo no admitido.",
    )

    def child_limits():
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_ARCHIVE, MAX_ARCHIVE))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))

    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL if input_fd is None else input_fd,
        stdout=subprocess.PIPE if output_fd is None else output_fd,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
        env=environment,
        preexec_fn=child_limits,
    )
    deadline, output, errors = time.monotonic() + seconds, bytearray(), bytearray()
    heartbeat_until = time.monotonic() + 5
    previous = os.get_blocking(heartbeat_fd) if heartbeat_fd is not None else None
    try:
        if heartbeat_fd is not None:
            os.set_blocking(heartbeat_fd, False)
        with selectors.DefaultSelector() as selector:
            for pipe, target in ((process.stdout, output), (process.stderr, errors)):
                if pipe is not None:
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, target)
            while True:
                require(time.monotonic() < deadline, "Docker agotó el plazo.")
                if heartbeat_fd is not None:
                    try:
                        pulse = os.read(heartbeat_fd, 4096)
                    except BlockingIOError:
                        pulse = None
                    require(pulse != b"", "El CLI cerró su canal.")
                    if pulse is not None:
                        require(not pulse.replace(b".", b""), "Latido inválido.")
                        heartbeat_until = time.monotonic() + 5
                    require(time.monotonic() < heartbeat_until, "El CLI perdió el latido.")
                if output_fd is not None:
                    require(
                        os.fstat(output_fd).st_size <= MAX_ARCHIVE,
                        "Export excedió256MiB; parcial conservado.",
                    )
                if (
                    not selector.get_map()
                    and os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
                    is not None
                ):
                    break
                for key, _mask in selector.select(0.02):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk)
                        require(
                            len(output) + len(errors) <= MAX_LOG, "Salida diagnóstica excedió1MiB."
                        )
    finally:
        try:
            capture.cleanup_process(process)
        finally:
            if heartbeat_fd is not None:
                os.set_blocking(heartbeat_fd, previous)
    return {"returncode": process.returncode, "stdout": bytes(output), "stderr": bytes(errors)}


def daemon_contract(value):
    require(
        type(value) is dict
        and set(value) == {"ID", "ServerVersion", "DockerRootDir"}
        and all(isinstance(v, str) and v for v in value.values())
        and value["ServerVersion"].startswith("29.")
        and value["DockerRootDir"] == "/var/lib/docker",
        "Daemon no respaldado/canónico.",
    )


def build_arguments(base_manifest):
    require(
        base_manifest == "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72",
        "Base build ajena.",
    )
    return [
        "buildx",
        "build",
        "--builder",
        "default",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--pull=false",
        "--provenance=false",
        "--sbom=false",
        "--progress",
        "plain",
        "--build-context",
        "pythonbase=oci-layout://" + ROOT + "/base-oci@" + base_manifest,
        "--iidfile",
        ROOT + "/image-id.txt",
        "--metadata-file",
        ROOT + "/build-metadata.json",
        "--file",
        "Dockerfile",
        "--load",
        "-",
    ]


def known_build_ids(directory, core):
    values = {}
    try:
        metadata = core.decode(read_leaf(directory, "build-metadata.json"))
        values.update(
            manifest=metadata.get("containerimage.digest"),
            config=metadata.get("containerimage.config.digest"),
        )
    except (FileNotFoundError, ValueError, KeyError):
        pass
    try:
        values["iidfile"] = read_leaf(directory, "image-id.txt").decode("ascii", "strict").strip()
    except (FileNotFoundError, ValueError, UnicodeError):
        pass
    return {
        key: value
        for key, value in values.items()
        if isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value)
    }


def oci_layout(directory, raw, core, *, create=False):
    """Publica/verifica sólo los bytes OCI autenticados, por FD y exclusión."""
    entries = {
        name: body
        for name, member, body in core.tar_rows(raw, outer=True)
        if member.isfile() and name != "manifest.json"
    }
    if create:
        os.mkdir("base-oci", 0o700, dir_fd=directory)
    root = os.open("base-oci", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        node(os.fstat(root), directory=True)
        if create:
            os.mkdir("blobs", 0o700, dir_fd=root)
        blobs = os.open("blobs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            node(os.fstat(blobs), directory=True)
            if create:
                os.mkdir("sha256", 0o700, dir_fd=blobs)
            hashes = os.open("sha256", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=blobs)
            try:
                node(os.fstat(hashes), directory=True)
                if create:
                    for name, body in entries.items():
                        write_new(
                            hashes if name.startswith("blobs/sha256/") else root,
                            name.rsplit("/", 1)[-1],
                            body,
                        )
                require(
                    set(os.listdir(root)) == {"blobs", "index.json", "oci-layout"}
                    and set(os.listdir(blobs)) == {"sha256"}
                    and set(os.listdir(hashes))
                    == {
                        name.rsplit("/", 1)[-1]
                        for name in entries
                        if name.startswith("blobs/sha256/")
                    },
                    "OCI local tiene archivos ajenos/faltantes.",
                )
                for name, body in entries.items():
                    require(
                        read_leaf(
                            hashes if name.startswith("blobs/sha256/") else root,
                            name.rsplit("/", 1)[-1],
                            MAX_ARCHIVE,
                        )
                        == body,
                        "OCI local cambió.",
                    )
            finally:
                os.close(hashes)
        finally:
            os.close(blobs)
    finally:
        os.close(root)


def stage_receipt(descriptor, stage, request, core):
    backed = request["previous"].get(stage)
    physical = core.decode(read_leaf(descriptor, stage + ".receipt.json"))
    value = unpack(physical)
    require(
        backed == physical
        and value.get("success") is True
        and value.get("stage") == stage
        and value.get("owner") == OWNER
        and value.get("profile_sha256") == request["profile_sha256"],
        "Etapa previa no respaldada: " + stage,
    )
    return value


def execute(request, input_stream, output_stream):
    core, parser, layout, capture = modules(request)
    stage, profile = request["stage"], request["profile"]
    verify_host(layout, request["layout"])
    daemon_contract(request["daemon"])
    parent, directory = open_directory(PARENT), None
    known = {}
    try:
        leaf = ROOT.rsplit("/", 1)[1]
        if stage == "prepare":
            os.mkdir(leaf, 0o700, dir_fd=parent)
        directory = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        node(os.fstat(directory), directory=True)
        current = identity(os.fstat(directory))
        if stage == "prepare":
            marker = canonical(
                {
                    "owner": OWNER,
                    "path": ROOT,
                    "directory": current,
                    "profile_sha256": request["profile_sha256"],
                }
            )
            write_new(directory, ".owner.json", marker)
            os.mkdir("docker", 0o700, dir_fd=directory)
            configuration = os.open(
                "docker", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            try:
                write_new(configuration, "config.json", b"{}")
            finally:
                os.close(configuration)
        else:
            proof = stage_receipt(directory, "prepare", request, core)
            marker = read_leaf(directory, ".owner.json")
            require(
                proof["directory"] == current and proof["marker_sha256"] == digest(marker),
                "Namespace no corresponde al respaldo.",
            )
        env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "DOCKER_CONFIG": ROOT + "/docker",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
        }
        configuration = os.open(
            "docker", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
        )
        try:
            node(os.fstat(configuration), directory=True)
            require(
                set(os.listdir(configuration)) == {"config.json"}
                and read_leaf(configuration, "config.json") == b"{}",
                "Configuración Docker propia dejó de estar vacía.",
            )
        finally:
            os.close(configuration)

        def docker(args, seconds=20, **kwargs):
            return run(
                ["/usr/bin/docker", *args],
                seconds,
                capture,
                environment=env,
                heartbeat_fd=input_stream.fileno() if stage != "receive" else None,
                **kwargs,
            )

        def query(args):
            result = docker(args)
            require(result["returncode"] == 0 and not result["stderr"], "Consulta Docker falló.")
            return core.decode(result["stdout"])

        actual_daemon = query(
            [
                "info",
                "--format",
                '{"ID":{{json .ID}},"ServerVersion":{{json .ServerVersion}},"DockerRootDir":{{json .DockerRootDir}}}',
            ]
        )
        require(actual_daemon == request["daemon"], "Daemon cambió respecto del respaldo.")
        write_new(
            directory,
            stage + ".intent.json",
            canonical(
                {"stage": stage, "profile_sha256": request["profile_sha256"], "directory": current}
            ),
        )
        result = {
            "success": True,
            "stage": stage,
            "owner": OWNER,
            "path": ROOT,
            "directory": current,
            "marker_sha256": digest(marker),
            "profile_sha256": request["profile_sha256"],
            "daemon": actual_daemon,
            "runtime_approved": False,
            "binary_executed": False,
        }
        if stage == "receive":
            require(
                set(profile["inputs"]["outputs"]) == REQUIRED_INPUTS, "Inputs fuera del cierre."
            )
            for name in sorted(REQUIRED_INPUTS):
                expected = profile["inputs"]["outputs"][name]
                raw = exact(input_stream, expected["bytes"])
                require(digest(raw) == expected["sha256"], "Entrada transferida difiere del pin.")
                write_new(directory, name, raw)
            derived, proof = core.derive_context(read_leaf(directory, "context.tar", MAX_ARCHIVE))
            require(proof == profile["inputs"]["derivation"], "Derivación contexto distinta.")
            write_new(directory, "context-derived.tar", derived)
            result["derivation"] = proof
        elif stage == "base":
            stage_receipt(directory, "receive", request, core)
            inspected = query(["image", "inspect", core.BASE_MANIFEST])
            require(
                type(inspected) is list
                and len(inspected) == 1
                and inspected[0]["Id"] in {core.BASE_MANIFEST, core.BASE_CONFIG}
                and inspected[0]["Architecture"] == "amd64"
                and inspected[0]["Os"] == "linux",
                "Base física ausente/ajena; no pull.",
            )
            result["inspect_id"] = inspected[0]["Id"]
            save = os.open(
                "base.tar",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                exported = docker(["image", "save", core.BASE_MANIFEST], 120, output_fd=save)
                os.fsync(save)
            finally:
                os.close(save)
            write_new(directory, "base-save.log", exported["stdout"] + exported["stderr"])
            require(
                exported["returncode"] == 0 and not exported["stderr"],
                "Export base falló; datos conservados.",
            )
            raw = read_leaf(directory, "base.tar", MAX_ARCHIVE)
            base = core.read_oci(raw, core.BASE_MANIFEST, parser)
            require(base["config_digest"] == core.BASE_CONFIG, "Config base distinta.")
            # Directorio OCI nuevo; sólo blobs/documentos ya autenticados y nombres cerrados.
            oci_layout(directory, raw, core, create=True)
            result["archive"] = {"name": "base.tar", "bytes": len(raw), "sha256": digest(raw)}
        elif stage == "build":
            base_receipt = stage_receipt(directory, "base", request, core)
            raw = read_leaf(directory, "base.tar", MAX_ARCHIVE)
            require(digest(raw) == base_receipt["archive"]["sha256"], "Base capturada cambió.")
            core.read_oci(raw, core.BASE_MANIFEST, parser)
            oci_layout(directory, raw, core)
            derived = read_leaf(directory, "context-derived.tar", MAX_ARCHIVE)
            require(
                digest(derived) == profile["inputs"]["derivation"]["derived_sha256"],
                "Contexto derivado cambió.",
            )
            require_absent(
                directory,
                ("image-id.txt", "build-metadata.json", "build.log", "build-identities.json"),
            )
            write_new(directory, "image-id.txt", b"")
            context_fd = os.open(
                "context-derived.tar", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
            )
            try:
                built = docker(build_arguments(core.BASE_MANIFEST), 180, input_fd=context_fd)
            finally:
                os.close(context_fd)
            write_new(directory, "build.log", built["stdout"] + built["stderr"])
            metadata = core.decode(read_leaf(directory, "build-metadata.json"))
            iid = read_leaf(directory, "image-id.txt").decode("ascii", "strict").strip()
            # Persistir cualquier identidad sintácticamente válida ANTES de rechazos posteriores.
            known = known_build_ids(directory, core)
            write_new(directory, "build-identities.json", canonical(known))
            require(built["returncode"] == 0, "Build falló; IDs/log conservados.")
            result["identities"] = core.build_identity(metadata, iid)
        elif stage == "final":
            build = stage_receipt(directory, "build", request, core)
            known = build["identities"]
            inspected = query(["image", "inspect", known["manifest"]])
            require(
                type(inspected) is list
                and len(inspected) == 1
                and inspected[0]["Id"] in {known["manifest"], known["config"]},
                "Inspect final ajeno.",
            )
            save = os.open(
                "final.tar",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                exported = docker(["image", "save", known["manifest"]], 120, output_fd=save)
                os.fsync(save)
            finally:
                os.close(save)
            write_new(directory, "final-save.log", exported["stdout"] + exported["stderr"])
            require(
                exported["returncode"] == 0 and not exported["stderr"],
                "Export final falló; IDs conservados.",
            )
            raw = read_leaf(directory, "final.tar", MAX_ARCHIVE)
            final = core.read_oci(raw, known["manifest"], parser)
            require(final["config_digest"] == known["config"], "Config final distinta del build.")
            result["identities"] = known
            result["archive"] = {"name": "final.tar", "bytes": len(raw), "sha256": digest(raw)}
        elif stage == "fetch":
            base = stage_receipt(directory, "base", request, core)
            final = stage_receipt(directory, "final", request, core)
            artifacts = [base["archive"], final["archive"]]
            frame(output_stream, {"artifacts": artifacts})
            for row in artifacts:
                raw = read_leaf(directory, row["name"], MAX_ARCHIVE)
                require(
                    len(raw) == row["bytes"] and digest(raw) == row["sha256"],
                    "Archive cambió antes del fetch.",
                )
                output_stream.write(raw)
                output_stream.flush()
            result["artifacts"] = artifacts
            result["identities"] = final["identities"]
        verify_host(layout, request["layout"])
        require(
            identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False)) == current,
            "Namespace reemplazado.",
        )
        receipt = envelope(result)
        write_new(directory, stage + ".receipt.json", canonical(receipt))
        return receipt
    except BaseException as failure:
        if directory is not None and stage == "build":
            # Incluso un timeout puede haber dejado metadata/IID válidos; no inferir su ausencia.
            known.update(known_build_ids(directory, core))
        failure.identities = getattr(failure, "identities", known)
        if directory is not None:
            try:
                write_new(
                    directory,
                    request["stage"] + ".failure.json",
                    canonical(
                        {
                            "success": False,
                            "verified": False,
                            "stage": request["stage"],
                            "error_type": type(failure).__name__,
                            "identities": failure.identities,
                            "runtime_approved": False,
                        }
                    ),
                )
            except Exception:
                pass
        raise
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)


def frame(output, value):
    raw = canonical(value)
    require(len(raw) <= MAX_LOG, "Marco excesivo.")
    output.write(struct.pack(">I", len(raw)) + raw)
    output.flush()


def main(request):
    old_handler = signal.signal(
        signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("Plazo global agotado."))
    )
    signal.setitimer(signal.ITIMER_REAL, 240)
    try:
        receipt = execute(request, sys.stdin.buffer, sys.stdout.buffer)
        frame(sys.stdout.buffer, receipt)
        return 0
    except BaseException as failure:
        frame(
            sys.stdout.buffer,
            {
                "success": False,
                "verified": False,
                "stage": request.get("stage"),
                "error_type": type(failure).__name__,
                "identities": getattr(failure, "identities", {}),
                "runtime_approved": False,
            },
        )
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
