"""Verifica y materializa el paquete Prisma del ensayo de SupoClip."""

import argparse
import hashlib
import json
import os
import re
import stat
import tarfile
from pathlib import Path

ARCHIVE_SHA = "f6191359c1cabadf13562923a07a984bef77575a6b6b2890924402c3a96dc220"
ARCHIVE_BYTES = 40110240
MANIFEST_SHA = "ca73c53930e8e3e96728ffef8377e90e92aade7d9b342863f5b3ae207a3412cf"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def name_is_allowed(name):
    parts = name.split("/")
    return (
        len(parts) > 1
        and parts[0] in {"prisma-source", "prisma-bin"}
        and all(part not in {"", ".", ".."} for part in parts)
        and re.fullmatch(r"[A-Za-z0-9_@.+() /!#&$,-]+", name) is not None
    )


def verified_source(path):
    if path.resolve(strict=True) != path:
        raise ValueError("El archivo debe ser una ruta física absoluta")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    stream = os.fdopen(os.open(path, flags), "rb")
    try:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != ARCHIVE_BYTES
            or hashlib.file_digest(stream, "sha256").hexdigest() != ARCHIVE_SHA
        ):
            raise ValueError("El archivo no coincide con la cápsula sellada")
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def install_file(path, payload, mode):
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def verify(path, destination=None):
    with verified_source(path) as stream, tarfile.open(fileobj=stream, mode="r:gz") as archive:
        entries = archive.getmembers()
        index = {entry.name: entry for entry in entries}
        if len(entries) != 4705 or len(index) != len(entries):
            raise ValueError("Número de entradas inesperado o duplicado")
        entry = index.pop("manifest.json")
        if not entry.isfile() or entry.size > 2 * 1024**2 or entry.mode != 0o444:
            raise ValueError("Manifiesto irregular")
        raw = archive.extractfile(entry).read()
        if sha(raw) != MANIFEST_SHA:
            raise ValueError("Manifiesto distinto")
        manifest = json.loads(raw)
        if manifest["files"].keys() != index.keys() or len(manifest["packages"]) != 41:
            raise ValueError("Inventario distinto")
        payloads = {}
        for name, member in index.items():
            row = manifest["files"][name]
            mode = 0o555 if name == "prisma-bin/schema-engine" else 0o444
            if (
                not name_is_allowed(name)
                or not member.isfile()
                or (member.uid, member.gid, member.mode, member.mtime) != (0, 0, mode, 0)
                or member.size != row["bytes"]
                or oct(mode) != row["mode"]
            ):
                raise ValueError(f"Entrada irregular: {name}")
            data = archive.extractfile(member).read()
            if len(data) != member.size or sha(data) != row["sha256"]:
                raise ValueError(f"Huella de archivo distinta: {name}")
            if destination:
                payloads[name] = (data, mode)
    if destination:
        if os.geteuid() != 0 or destination.parent.resolve(strict=True) != destination.parent:
            raise ValueError("Se requiere root y un directorio padre físico absoluto")
        destination.mkdir(mode=0o700, exist_ok=False)
        for name, (data, mode) in payloads.items():
            install_file(destination / name, data, mode)
        install_file(destination / "manifest.json", raw, 0o444)
        for directory, children, _ in os.walk(destination):
            for name in children:
                (Path(directory) / name).chmod(0o555)
        destination.chmod(0o700)
    return {
        "archive_sha256": ARCHIVE_SHA,
        "manifest_sha256": MANIFEST_SHA,
        "files": len(entries),
        "root": str(destination) if destination else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--destino", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.archive.absolute(), args.destino), sort_keys=True))
