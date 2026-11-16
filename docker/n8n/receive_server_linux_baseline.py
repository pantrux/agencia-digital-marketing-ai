"""Recibe la cápsula de referencia del servidor n8n sin ejecutar su contenido."""

import argparse
import gzip
import hashlib
import json
import os
import re
import signal
import sys
import tarfile
import tempfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from types import ModuleType

ROOT = "n8n-server-linux-transfer-v2"
OWNER = "agencia-digital-marketing-ai:n8n-server-linux-transfer:v2"
ARCHIVE = "b3405a0d625db295e62cc9cf3edd06eca25548c4c1c91e3c108b23397df406ed"
MANIFEST = "203830d3062236403e21f1922839965cada6b8533d6f82b8dd9ebef0bdfa034c"
READER = "f3ae4889a03f90ec989cff4b9c2b74dc4331916ccdb36d25738f427082aa9b2b"
ARCHIVE_BYTES = 439_342_873
PAYLOAD_BYTES = 453_650_879
TAR_BYTES = 457_154_560
ENTRIES = 4542
MIB = 1024**2
MIN_BYTES = 1024**3
MIN_INODES = 7000


def need(condition, code):
    if not condition:
        raise ValueError(code)


def load_helper(path, digest):
    need(path.is_file() and path.resolve(strict=True) == path.absolute(), "HELPER_RUTA")
    with path.open("rb") as stream:
        raw = stream.read(32_769)
    need(len(raw) <= 32_768 and hashlib.sha256(raw).hexdigest() == digest, "HELPER_SHA")
    module = ModuleType("n8n_helper_pinned")
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def parts_of(name):
    parts = PurePosixPath(name).parts
    need(
        name.isascii()
        and 0 < len(name) <= 512
        and "/".join(parts) == name
        and not any(p in {".", "..", "/", ".git", ".env", "node_modules"} for p in parts)
        and re.fullmatch(r"[A-Za-z0-9_./@+%~-]+", name)
        and (
            name in {"transfer-manifest.json", "artifact-index.json"}
            or (len(parts) > 1 and parts[0] in {"cache", "evidence", "source", "tarballs"})
        ),
        "TAR_RUTA",
    )
    return parts


class Bounded:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, size):
        raw = self.stream.read(min(size, self.limit + 1 - self.count))
        self.count += len(raw)
        need(self.count <= self.limit, "TAR_LIMITE")
        return raw


def spool(incoming, destination):
    wire = Bounded(incoming, ARCHIVE_BYTES)
    digest = hashlib.sha256()
    while chunk := wire.read(MIB):
        digest.update(chunk)
        destination.write(chunk)
    need(wire.count == ARCHIVE_BYTES and digest.hexdigest() == ARCHIVE, "CAPSULA_SHA")
    destination.seek(0)


def inspect_archive(reader, compressed, output=None):
    manifest, seen, payload = None, set(), 0
    with gzip.GzipFile(fileobj=compressed, mode="rb") as gz:
        bounded = Bounded(gz, TAR_BYTES)
        with tarfile.open(fileobj=bounded, mode="r|") as tar:
            for row in tar:
                parts = parts_of(row.name)
                need(
                    len(seen) < ENTRIES
                    and row.name not in seen
                    and row.type == tarfile.REGTYPE
                    and not row.issparse()
                    and not row.linkname
                    and row.uid == row.gid == row.mtime == 0
                    and row.mode == 0o644
                    and set(row.pax_headers) <= {"path"}
                    and 0 < row.size <= 96 * MIB,
                    "TAR_MIEMBRO",
                )
                payload += row.size
                need(payload <= PAYLOAD_BYTES, "TAR_PAYLOAD")
                with tar.extractfile(row) as stream:
                    if manifest is None:
                        need(row.name == "transfer-manifest.json" and row.size < MIB, "MANIFIESTO")
                        digest, raw = reader.copy_member(stream, row.size, collect=True)
                        need(digest == MANIFEST, "MANIFIESTO_SHA")
                        manifest = json.loads(raw)
                        need(len(manifest) == ENTRIES - 1, "MANIFIESTO_COBERTURA")
                        if output is not None:
                            from io import BytesIO

                            need(
                                output.copy(parts, BytesIO(raw), row.size, False) == digest,
                                "TAR_SHA",
                            )
                    else:
                        item = manifest.get(row.name)
                        need(item is not None and item["bytes"] == row.size, "TAR_COBERTURA")
                        if output is None:
                            digest, _ = reader.copy_member(stream, row.size)
                        else:
                            digest = output.copy(parts, stream, row.size, False)
                        need(digest == item["sha256"], "TAR_SHA")
                seen.add(row.name)
        while chunk := bounded.read(MIB):
            need(not chunk.strip(b"\0"), "TAR_TRAILER")
    need(
        manifest is not None
        and seen == manifest.keys() | {"transfer-manifest.json"}
        and len(seen) == ENTRIES
        and payload == PAYLOAD_BYTES
        and bounded.count == TAR_BYTES,
        "CAPSULA_COBERTURA",
    )
    return {"entries": ENTRIES, "payload_bytes": payload, "tar_bytes": bounded.count}


def storage(path, min_bytes, min_inodes):
    fs = os.statvfs(path)
    need(
        fs.f_bavail * fs.f_frsize >= min_bytes and fs.f_favail >= min_inodes, "ESPACIO_INSUFICIENTE"
    )


def receive(reader):
    need(sys.platform == "linux" and os.geteuid() == os.getegid() == 0, "LINUX_ROOT")
    os.umask(0o077)
    with ExitStack() as stack:

        def directory(parent, name):
            fd = reader.directory(parent, name)
            stack.callback(os.close, fd)
            return fd

        root = directory(None, "/")
        opt = directory(root, "opt")
        parent = directory(opt, "media-platform")
        for fd in (root, opt, parent):
            reader.checked_dir(fd)
        reader.checked_dir(parent, mode=0o700)
        storage(parent, MIN_BYTES, MIN_INODES)
        os.mkdir(ROOT, 0o700, dir_fd=parent)
        transfer = directory(parent, ROOT)
        node = reader.checked_dir(transfer, mode=0o700)
        owner = {"owner": OWNER, "device": node.st_dev, "inode": node.st_ino}
        reader.publish(transfer, ".agency-owner", reader.encoded(owner))
        try:
            with tempfile.TemporaryFile(dir=f"/proc/self/fd/{transfer}") as compressed:
                spool(sys.stdin.buffer, compressed)
                os.mkdir("inputs", 0o700, dir_fd=transfer)
                inputs = directory(transfer, "inputs")
                output = reader.Output(inputs)
                counts = inspect_archive(reader, compressed, output)
                output.seal()
                target = reader.checked_dir(inputs, device=node.st_dev, mode=0o555)
            receipt = {
                **owner,
                **counts,
                "inputs_device": target.st_dev,
                "inputs_inode": target.st_ino,
                "archive_sha256": ARCHIVE,
                "archive_bytes": ARCHIVE_BYTES,
                "manifest_sha256": MANIFEST,
                "received": True,
                "compiled": False,
                "production_approved": False,
            }
            reader.publish(transfer, "receipt.json", reader.encoded(receipt))
            os.fsync(parent)
            return receipt
        except Exception as error:
            reader.publish(
                transfer, "failure.json", reader.encoded({**owner, "type": type(error).__name__})
            )
            raise


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--reader", type=Path)
    modes = cli.add_mutually_exclusive_group()
    modes.add_argument("--receive", action="store_true")
    modes.add_argument("--inspect-capsule", type=Path)
    args = cli.parse_args()
    result = {"archive_sha256": ARCHIVE, "manifest_sha256": MANIFEST, "received": False}
    if args.receive or args.inspect_capsule:
        if args.reader is None:
            cli.error("La operación requiere --reader.")
        reader = load_helper(args.reader, READER)
        if args.receive:
            signal.signal(signal.SIGALRM, reader.expired)
            signal.alarm(900)
            try:
                result = receive(reader)
            finally:
                signal.alarm(0)
        else:
            with args.inspect_capsule.open("rb") as stream:
                need(
                    stream.seek(0, os.SEEK_END) == ARCHIVE_BYTES
                    and stream.seek(0) == 0
                    and hashlib.file_digest(stream, "sha256").hexdigest() == ARCHIVE,
                    "CAPSULA_SHA",
                )
                stream.seek(0)
                result |= inspect_archive(reader, stream)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
