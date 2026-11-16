"""Deriva el wheel puro MoviePy 2.2.1, con plan por defecto y sin red."""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from email.parser import BytesParser
from pathlib import Path

SOURCE_NAME = "moviepy-2.2.1-py3-none-any.whl"
SOURCE_SIZE = 129871
SOURCE_SHA256 = "6b56803fec2ac54b557404126ac1160e65448e03798fa282bd23e8fab3795060"
SOURCE_URL = (
    "https://files.pythonhosted.org/packages/9a/73/7d3b2010baa0b5eb1e4dfa9e4385e89b6716be76f2fa21a6c0fe34b68e5a/"
    + SOURCE_NAME
)
ORIGINAL = "2.2.1"
DERIVED = "2.2.1+agency1"
OUTPUT_NAME = f"moviepy-{DERIVED}-py3-none-any.whl"
MAX_WHEEL = 2 * 1024 * 1024
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)
}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def record_hash(data):
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def inspect_wheel(raw, version):
    """Verifica el ZIP en memoria y coteja el RECORD sin extraer miembros."""
    require(0 < len(raw) <= MAX_WHEEL, "ZIP_SIZE")
    dist = f"moviepy-{version}.dist-info/"
    members, folded = {}, set()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            require(
                not archive.comment
                and 0 < len(infos) <= 512
                and sum(item.file_size for item in infos) <= MAX_WHEEL,
                "ZIP_SIZE",
            )
            for item in infos:
                name = item.filename
                require(
                    name == item.orig_filename
                    and re.fullmatch(r"[A-Za-z0-9_+./-]{1,240}", name)
                    and name.startswith(("moviepy/", dist))
                    and all(
                        part not in {"", ".", ".."}
                        and not part.endswith(".")
                        and part.split(".", 1)[0].upper() not in RESERVED
                        for part in name.split("/")
                    )
                    and not name.endswith(("/RECORD.jws", "/RECORD.p7s")),
                    "ZIP_PATH",
                )
                require(name.casefold() not in folded, "ZIP_DUPLICATE")
                folded.add(name.casefold())
                require(
                    stat.S_IFMT(item.external_attr >> 16) in {0, stat.S_IFREG}
                    and not item.flag_bits & 1
                    and item.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                    "ZIP_TYPE",
                )
                members[name] = archive.read(item)
    except (zipfile.BadZipFile, RuntimeError, UnicodeError):
        raise ValueError("ZIP_INVALID") from None
    record = dist + "RECORD"
    require(
        {record, dist + "METADATA", dist + "WHEEL", "moviepy/__init__.py"} <= members.keys(),
        "WHEEL_REQUIRED",
    )
    require(
        any(name.startswith(dist) and "LICENCE" in name.upper() for name in members),
        "WHEEL_LICENSE",
    )
    wheel = BytesParser().parsebytes(members[dist + "WHEEL"], headersonly=True)
    require(
        wheel.get_all("Tag") == ["py3-none-any"] and wheel.get_all("Root-Is-Purelib") == ["true"],
        "WHEEL_TAG",
    )
    seen = set()
    try:
        for row in csv.reader(
            io.StringIO(members[record].decode("utf-8"), newline=""), strict=True
        ):
            require(len(row) == 3, "RECORD_ROW")
            name, checksum, size = row
            require(name in members and name not in seen, "RECORD_PATH")
            seen.add(name)
            expected = (
                ("", "")
                if name == record
                else (record_hash(members[name]), str(len(members[name])))
            )
            require((checksum, size) == expected, "RECORD_HASH")
    except (UnicodeError, csv.Error):
        raise ValueError("RECORD_INVALID") from None
    require(seen == members.keys(), "RECORD_COVERAGE")
    return members


def rewrite_metadata(raw):
    require(b"\r" not in raw and b"\n\n" in raw, "METADATA_FORMAT")
    headers, body = raw.split(b"\n\n", 1)
    metadata = BytesParser().parsebytes(raw, headersonly=True)
    require(
        metadata.get_all("Name") == ["moviepy"] and metadata.get_all("Version") == [ORIGINAL],
        "METADATA_IDENTITY",
    )
    pillow = [r for r in metadata.get_all("Requires-Dist", []) if re.match(r"^pillow\b", r, re.I)]
    require(pillow == ["pillow<12.0,>=9.2.0"], "METADATA_PILLOW")
    lines = headers.split(b"\n")
    for old, new in (
        (b"Version: 2.2.1", b"Version: 2.2.1+agency1"),
        (b"Requires-Dist: pillow<12.0,>=9.2.0", b"Requires-Dist: pillow<14,>=13"),
    ):
        require(lines.count(old) == 1, "METADATA_LINE")
        lines[lines.index(old)] = new
    return b"\n".join(lines) + b"\n\n" + body


def derive_bytes(raw):
    require(len(raw) == SOURCE_SIZE and sha256(raw) == SOURCE_SHA256, "SOURCE_PIN")
    before = inspect_wheel(raw, ORIGINAL)
    old_dist, new_dist = (f"moviepy-{version}.dist-info/" for version in (ORIGINAL, DERIVED))
    members = {}
    for name, data in before.items():
        if name == old_dist + "RECORD":
            continue
        target = new_dist + name[len(old_dist) :] if name.startswith(old_dist) else name
        members[target] = rewrite_metadata(data) if name == old_dist + "METADATA" else data
    record_name = new_dist + "RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, data in sorted(members.items()):
        writer.writerow((name, record_hash(data), len(data)))
    writer.writerow((record_name, "", ""))
    members[record_name] = record.getvalue().encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    derived = stream.getvalue()
    require(inspect_wheel(derived, DERIVED) == members, "OUTPUT_VERIFY")
    return derived, {
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "source_bytes": SOURCE_SIZE,
        "output_name": OUTPUT_NAME,
        "output_sha256": sha256(derived),
        "output_bytes": len(derived),
        "members": len(members),
        "member_contents_preserved": len(members) - 2,
        "pillow": ">=13,<14",
    }


def publish_new(target, data):
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--apply", type=Path, metavar="DIRECTORIO_NUEVO_EXISTENTE")
    args = parser.parse_args()
    require(args.source.name == SOURCE_NAME, "SOURCE_NAME")
    with args.source.open("rb") as stream:
        raw = stream.read(SOURCE_SIZE + 1)
    derived, receipt = derive_bytes(raw)
    if args.apply is not None:
        directory = args.apply.absolute()
        require(
            directory.is_dir()
            and directory.resolve(strict=True) == directory
            and not any(directory.iterdir()),
            "OUTPUT_DIR",
        )
        publish_new(directory / OUTPUT_NAME, derived)
    print(json.dumps({**receipt, "published": args.apply is not None}, sort_keys=True))


if __name__ == "__main__":
    main()
