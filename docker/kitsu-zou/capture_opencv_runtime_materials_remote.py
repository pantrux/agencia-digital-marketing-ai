"""Lee cinco bibliotecas del export propio; nunca extrae un TAR ni ejecuta ELF."""

import base64
import hashlib
import json
import os
import stat
import struct
import tarfile
import types

TARGETS = {
    name: "usr/lib/" + name
    for name in ("ld-linux-x86-64.so.2", "libc.so.6", "libm.so.6", "libgcc_s.so.1", "libz.so.1")
}
AUDITOR_SHA = "4d46898d6700d5858bc778a3ddb68acc0c0f2d44b923a35c9c681a789fa18dd2"
PROFILE_SHA = "747a33d04bf5238ccc374cc3ac7cd3b1f49050bf9df5cb668d4191cc851001e1"
CAPTURE_SHA = "fbdb485a006e697b498a26a57981e8909daa59b67df427a785998763dff659d3"


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(info):
    return {
        key: getattr(info, "st_" + key) for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
    }


def relative(name):
    require(
        isinstance(name, str)
        and name
        and len(name) <= 4096
        and not name.startswith("/")
        and "\\" not in name
        and "\0" not in name,
        "Nombre TAR inválido.",
    )
    while name.startswith("./"):
        name = name[2:]
    parts = name.rstrip("/").split("/")
    require(all(p not in ("", ".", "..") for p in parts), "Ruta TAR no canónica.")
    return "/".join(parts)


def link_path(parent, target):
    require(
        target and len(target) <= 4096 and "\\" not in target and "\0" not in target,
        "Enlace TAR inválido.",
    )
    parts = [] if target.startswith("/") else parent.split("/") if parent else []
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            require(parts, "Enlace intenta salir del TAR.")
            parts.pop()
        else:
            parts.append(part)
    require(parts, "Enlace sin destino.")
    return "/".join(parts)


def resolve(nodes, name):
    pending = relative(name).split("/")
    resolved = []
    followed = set()
    while pending:
        part = pending.pop(0)
        path = "/".join([*resolved, part])
        item = nodes.get(path)
        require(item is not None or pending, "Falta el archivo objetivo.")
        if item is not None and (item.issym() or item.islnk()):
            require(path not in followed and len(followed) < 40, "Ciclo de enlaces TAR.")
            followed.add(path)
            parent = "/".join(resolved) if item.issym() else ""
            target = link_path(parent, item.linkname)
            pending = target.split("/") + pending
            resolved = []
        else:
            if pending and item is not None:
                require(item.isdir(), "Un archivo se usa como directorio.")
            resolved.append(part)
    path = "/".join(resolved)
    require(path in nodes and nodes[path].isreg(), "El objetivo no es un archivo regular.")
    return path


def select_files(stream, size, targets, *, maximum_file=8 * 1024**2):
    require(
        1024 <= size <= 1024**3 and size % 512 == 0 and 0 < len(targets) <= 8,
        "TAR o selección fuera de límites.",
    )
    stream.seek(0)
    nodes = {}
    try:
        with tarfile.open(fileobj=stream, mode="r:") as package:
            for item in package:
                if item.name.rstrip("/") in ("", ".") and item.isdir():
                    continue
                name = relative(item.name)
                require(
                    name not in nodes and len(nodes) < 25000,
                    "Miembro duplicado o inventario excesivo.",
                )
                nodes[name] = item
            end = package.offset
            require(end <= size - 1024, "Falta terminador TAR.")
            stream.seek(end)
            remaining = size - end
            while remaining:
                block = stream.read(min(remaining, 1024**2))
                require(block and not any(block), "Trailer TAR inválido.")
                remaining -= len(block)
            results = {}
            total = 0
            for key, name in targets.items():
                path = resolve(nodes, name)
                item = nodes[path]
                require(0 < item.size <= maximum_file, "Biblioteca fuera de límites.")
                total += item.size
                require(total <= 16 * 1024**2, "Selección total excesiva.")
                with package.extractfile(item) as source:
                    data = source.read(item.size + 1)
                require(len(data) == item.size, "Biblioteca TAR truncada.")
                results[key] = {
                    "requested_member": name,
                    "resolved_member": path,
                    "data": data,
                    "sha256": digest(data),
                    "bytes": len(data),
                }
            return results
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ValueError("TAR de bibliotecas inválido.") from error


def open_owned_directory(path):
    require(path.startswith("/") and ".." not in path.split("/"), "Directorio remoto inválido.")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.split("/")[1:]:
            require(part, "Componente de directorio vacío.")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            require(
                info.st_uid == 0 and not info.st_mode & 0o022,
                "Directorio con propiedad/permisos ajenos.",
            )
        result, current = current, None
        return result
    finally:
        if current is not None:
            os.close(current)


def read_leaf(directory, name, maximum):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == 0
            and before.st_nlink == 1
            and not before.st_mode & 0o022
            and before.st_size <= maximum,
            "Archivo remoto no regular o excesivo.",
        )
        raw = source.read(maximum + 1)
        require(
            len(raw) == before.st_size
            and fingerprint(before)
            == fingerprint(os.fstat(source.fileno()))
            == fingerprint(os.stat(name, dir_fd=directory, follow_symlinks=False)),
            "Archivo remoto cambió durante lectura.",
        )
        return raw


def capture(request):
    require(
        os.name == "posix" and os.geteuid() == 0, "La captura requiere lector Linux privilegiado."
    )
    raw = base64.b64decode(request["auditor_source_b64"], validate=True)
    require(digest(raw) == AUDITOR_SHA, "Auditor histórico alterado.")
    module = types.ModuleType("opencv_frozen_archive_auditor")
    module.__dict__["_AUDIT_SOURCE_SHA256"] = AUDITOR_SHA
    exec(compile(raw, "<auditor-inmutable>", "exec"), module.__dict__)
    profile_raw = base64.b64decode(request["audit_profile_b64"], validate=True)
    previous_raw = base64.b64decode(request["previous_capture_b64"], validate=True)
    require(
        digest(profile_raw) == PROFILE_SHA and digest(previous_raw) == CAPTURE_SHA,
        "Referencias históricas alteradas.",
    )
    audit = json.loads(profile_raw)
    previous = json.loads(previous_raw)
    original_request = {
        "audit": audit,
        "audit_sha256": digest(canonical(audit)),
        "controller_source_b64": request["controller_source_b64"],
        "layout_source_b64": request["layout_source_b64"],
    }
    before = module.capture(original_request)
    require(
        before["archive"] == previous["archive"]
        and before["archive_sha256"] == previous["archive_sha256"]
        and before["container"]["running"] is False
        and before["container"]["pid"] == 0,
        "Exportación o época SDK distinta/no detenida.",
    )
    directory = open_owned_directory(audit["sdk_plan"]["paths"]["control"])
    try:
        info = os.fstat(directory)
        expected = audit["backed"]["prepare"]["receipt"]["directories"]["control"]
        require(
            {"device": info.st_dev, "inode": info.st_ino} == expected["directory"]
            and digest(read_leaf(directory, ".opencv-sdk-owner.json", 16384))
            == expected["marker_sha256"],
            "Directorio de exportación ajeno.",
        )
        fd = os.open(
            "sdk-rootfs.tar", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == 0
                and info.st_nlink == 1
                and not info.st_mode & 0o022
                and {
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "ctime_ns": info.st_ctime_ns,
                }
                == before["archive"],
                "Identidad del archivo TAR alterada.",
            )
            checksum = hashlib.sha256()
            while block := stream.read(1024**2):
                checksum.update(block)
            require(checksum.hexdigest() == before["archive_sha256"], "Hash completo TAR alterado.")
            files = select_files(stream, info.st_size, TARGETS)
            require(
                fingerprint(info)
                == fingerprint(os.fstat(stream.fileno()))
                == fingerprint(os.stat("sdk-rootfs.tar", dir_fd=directory, follow_symlinks=False)),
                "TAR cambió durante la captura.",
            )
    finally:
        os.close(directory)
    after = module.capture(original_request)
    require(after == before, "La atestación histórica cambió durante la captura.")
    return {
        "success": True,
        "archive": before["archive"],
        "archive_sha256": before["archive_sha256"],
        "container": before["container"],
        "historical_attestation_sha256": digest(canonical(before)),
        "historical_attestation_unchanged": True,
        "files": {
            key: {
                **{k: v for k, v in row.items() if k != "data"},
                "data_b64": base64.b64encode(row["data"]).decode(),
            }
            for key, row in files.items()
        },
        "sdk_executed": False,
        "container_mutated": False,
        "elf_executed": False,
    }


def main():
    import signal
    import sys

    def timeout(_signal, _frame):
        raise TimeoutError("La lectura agotó 150 segundos.")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(150)
    stream = sys.stdin.buffer

    def exact(size):
        value = bytearray()
        while len(value) < size:
            part = stream.read(size - len(value))
            require(part, "Petición truncada.")
            value.extend(part)
        return bytes(value)

    try:
        size = struct.unpack(">I", exact(4))[0]
        require(0 < size <= 3 * 1024**2, "Petición excesiva.")
        request = json.loads(exact(size))
        require(not stream.read(1), "Bytes adicionales en petición.")
        print(json.dumps(capture(request)), flush=True)
        return 0
    except Exception as error:
        print(
            json.dumps(
                {"success": False, "error_type": type(error).__name__, "detail": str(error)[:200]}
            ),
            flush=True,
        )
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
