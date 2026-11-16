"""Empaqueta la distribución fuente fijada de imageio-ffmpeg sin ejecutar setup.py."""

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

SOURCE_ROOT = "imageio_ffmpeg-0.6.0"
SOURCE_NAME = SOURCE_ROOT + ".tar.gz"
SOURCE_SIZE = 25210
SOURCE_SHA256 = "e2556bed8e005564a9f925bb7afa4002d82770d6b08825078b7697ab88ba1755"
SOURCE_URL = (
    "https://files.pythonhosted.org/packages/44/bd/c3343c721f2a1b0c9fc71c1aebf1966a3b7f08c2eea8ed5437a2865611d6/"
    + SOURCE_NAME
)
OUTPUT_NAME = SOURCE_ROOT + "-py3-none-any.whl"
DIST = SOURCE_ROOT + ".dist-info/"
PAYLOAD = {
    "imageio_ffmpeg/__init__.py",
    "imageio_ffmpeg/_definitions.py",
    "imageio_ffmpeg/_io.py",
    "imageio_ffmpeg/_parsing.py",
    "imageio_ffmpeg/_utils.py",
    "imageio_ffmpeg/binaries/README.md",
    "imageio_ffmpeg/binaries/__init__.py",
}
SOURCE_DIRS = {"", "imageio_ffmpeg", "imageio_ffmpeg/binaries", "imageio_ffmpeg.egg-info", "tests"}
REQUIRED = PAYLOAD | {"PKG-INFO", "LICENSE"}
SOURCE_FILES = REQUIRED | {
    "MANIFEST.in",
    "README.md",
    "imageio_ffmpeg.egg-info/PKG-INFO",
    "imageio_ffmpeg.egg-info/SOURCES.txt",
    "imageio_ffmpeg.egg-info/dependency_links.txt",
    "imageio_ffmpeg.egg-info/not-zip-safe",
    "imageio_ffmpeg.egg-info/top_level.txt",
    "setup.cfg",
    "setup.py",
    "tests/test_io.py",
    "tests/test_parsing.py",
    "tests/test_special.py",
    "tests/test_terminate.py",
    "tests/testutils.py",
}
MAX_EXPANDED = 256 * 1024


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_source(raw):
    """Lee miembros fijados como datos. No extrae el TAR ni ejecuta su contenido."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as archive:
            uncompressed = archive.read(MAX_EXPANDED + 1)
        require(len(uncompressed) <= MAX_EXPANDED, "TAR_SIZE")
        members, seen = {}, set()
        with tarfile.open(fileobj=io.BytesIO(uncompressed), mode="r:") as archive:
            for item in archive:
                name = item.name
                require(name == SOURCE_ROOT or name.startswith(SOURCE_ROOT + "/"), "TAR_MEMBER")
                relative = name.removeprefix(SOURCE_ROOT + "/") if name != SOURCE_ROOT else ""
                require(relative not in seen, "TAR_MEMBER")
                seen.add(relative)
                if item.isdir():
                    require(relative in SOURCE_DIRS and item.size == 0, "TAR_MEMBER")
                    continue
                require(
                    item.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
                    and relative in SOURCE_FILES
                    and 0 < item.size <= 64 * 1024,
                    "TAR_MEMBER",
                )
                if relative in REQUIRED:
                    with archive.extractfile(item) as stream:
                        data = stream.read(item.size + 1)
                    require(len(data) == item.size, "TAR_SIZE")
                    members[relative] = data
    except (EOFError, OSError, tarfile.TarError):
        raise ValueError("TAR_INVALID") from None
    require(members.keys() == REQUIRED, "TAR_REQUIRED")
    metadata = BytesParser().parsebytes(members["PKG-INFO"], headersonly=True)
    require(
        metadata.get_all("Metadata-Version") == ["2.1"]
        and metadata.get_all("Name") == ["imageio-ffmpeg"]
        and metadata.get_all("Version") == ["0.6.0"]
        and metadata.get_all("Requires-Python") == [">=3.9"]
        and metadata.get_all("License") == ["BSD-2-Clause"]
        and metadata.get_all("License-File") == ["LICENSE"]
        and not metadata.get_all("Requires-Dist"),
        "METADATA",
    )
    return members


def build_bytes(raw):
    require(len(raw) == SOURCE_SIZE and sha256(raw) == SOURCE_SHA256, "SOURCE_PIN")
    source = read_source(raw)
    files = {name: source[name] for name in PAYLOAD}
    files[DIST + "METADATA"] = source["PKG-INFO"]
    files[DIST + "LICENSE"] = source["LICENSE"]
    files[DIST + "WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: agency-imageio-ffmpeg-sdist-wheel-v1\n"
        b"Root-Is-Purelib: true\nTag: py3-none-any\n"
    )
    record_name = DIST + "RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow((name, "sha256=" + digest, len(data)))
    writer.writerow((record_name, "", ""))
    files[record_name] = record.getvalue().encode()
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    data = result.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(archive.namelist() == sorted(files), "WHEEL_MEMBERS")
        require(all(archive.read(name) == body for name, body in files.items()), "WHEEL_READBACK")
    return data, {
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "source_bytes": SOURCE_SIZE,
        "wheel_name": OUTPUT_NAME,
        "wheel_sha256": sha256(data),
        "wheel_bytes": len(data),
        "metadata_sha256": sha256(source["PKG-INFO"]),
        "license_sha256": sha256(source["LICENSE"]),
        "members": len(files),
        "package_files_preserved": len(PAYLOAD),
        "bundled_executables": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--apply", type=Path, metavar="DIRECTORIO_NUEVO_EXISTENTE")
    args = parser.parse_args()
    require(args.source.name == SOURCE_NAME, "SOURCE_NAME")
    with args.source.open("rb") as stream:
        raw = stream.read(SOURCE_SIZE + 1)
    data, receipt = build_bytes(raw)
    if args.apply is not None:
        directory = args.apply.absolute()
        require(
            directory.is_dir()
            and directory.resolve(strict=True) == directory
            and not any(directory.iterdir()),
            "OUTPUT_DIR",
        )
        path = directory / OUTPUT_NAME
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        require(path.read_bytes() == data, "OUTPUT_READBACK")
    print(json.dumps({**receipt, "published": args.apply is not None}, sort_keys=True))


if __name__ == "__main__":
    main()
