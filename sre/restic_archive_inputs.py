"""Inspección estática de ZIP Go: nombres RAW, CRC, UTF-8 y HashZip/Hash1."""

import base64
import codecs
import hashlib
import os
import re
import stat
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from go_module_semver import validate_module

MAX_ZIP = 500 * 1024**2
MAX_ENTRIES = 100000
MAX_NAMES = 8 * 1024**2
H1_SOURCE_SHA = "24de6a735c1605e39b6381e6962787faa5c59adab896fb66dbc1ac8b6afcb297"
ZIP_SOURCE_SHA = "3a0fd2c978e20233b6bd904a9c510dd746a7a50825c35a2e4ab1d8618f9de77b"


class ArchiveError(ValueError):
    def __init__(self, code, member=None):
        self.code, self.member = code, member
        super().__init__(code)


@dataclass(frozen=True)
class ZipLimits:
    max_compressed: int = MAX_ZIP
    max_expanded: int = MAX_ZIP
    max_entries: int = MAX_ENTRIES
    max_name_bytes: int = MAX_NAMES

    def __post_init__(self):
        for value, maximum in (
            (self.max_compressed, MAX_ZIP),
            (self.max_expanded, MAX_ZIP),
            (self.max_entries, MAX_ENTRIES),
            (self.max_name_bytes, MAX_NAMES),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError("El límite debe ser positivo y no ampliar el máximo.")


def _filename(raw):
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ArchiveError("NAME_NOT_UTF8") from None


def _path(name):
    for component in name.split("/"):
        if (
            not component
            or set(component) == {"."}
            or component.endswith((".", " "))
            or any(
                not (
                    c.isascii()
                    and (c.isalnum() or c in "!#$%&()+,-.=@[]^_{}~ ")
                    or not c.isascii()
                    and c.isalpha()
                )
                for c in component
            )
        ):
            raise ArchiveError("PATH_INVALID", name)
        short = component.split(".", 1)[0].upper()
        if short in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *("COM" + str(n) for n in range(1, 10)),
            *("LPT" + str(n) for n in range(1, 10)),
        }:
            raise ArchiveError("PATH_INVALID", name)


def _collision(nodes, name, directory):
    pieces = name.split("/")
    for count in range(1, len(pieces) + 1):
        path = "/".join(pieces[:count])
        is_directory = count < len(pieces) or directory
        # casefold completo es conservador frente a SimpleFold de Go. Puede
        # rechazar más nombres; no se atribuye equivalencia exacta de Unicode.
        folded = path.casefold()
        previous = nodes.get(folded)
        if previous is not None:
            if previous != (path, True) or not is_directory:
                raise ArchiveError("PATH_COLLISION", name)
        else:
            nodes[folded] = (path, is_directory)


def _central(stream, archive, infos, prefix, limits):
    stream.seek(archive.start_dir)
    central_start = archive.start_dir
    rows, names, nodes, expanded, name_bytes = [], set(), {}, 0, 0
    for info in infos:
        fixed = stream.read(46)
        if len(fixed) != 46:
            raise ArchiveError("ZIP_INVALID")
        values = struct.unpack("<4s6H3I5H2I", fixed)
        if values[0] != b"PK\x01\x02" or values[13] != 0:
            raise ArchiveError("ZIP_INVALID")
        raw = stream.read(values[10])
        if (
            len(raw) != values[10]
            or len(stream.read(values[11] + values[12])) != values[11] + values[12]
        ):
            raise ArchiveError("ZIP_INVALID")
        name = _filename(raw)
        name_bytes += len(raw)
        if name_bytes > limits.max_name_bytes:
            raise ArchiveError("NAME_LIMIT")
        if name in names:
            raise ArchiveError("PATH_COLLISION", name)
        names.add(name)
        if not name.startswith(prefix):
            raise ArchiveError("PREFIX_INVALID", name)
        relative = name[len(prefix) :]
        directory = name.endswith("/")
        normalized = relative[:-1] if directory else relative
        if normalized:
            _path(normalized)
            _collision(nodes, normalized, directory)
        elif not directory:
            raise ArchiveError("PATH_INVALID", name)
        if values[3] & ~0x80E or values[4] not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ArchiveError("ZIP_FLAGS", relative)
        if values[3] != info.flag_bits or values[4] != info.compress_type:
            raise ArchiveError("ZIP_INVALID", relative)
        kind = stat.S_IFMT(info.external_attr >> 16) if info.create_system == 3 else 0
        if kind not in ((0, stat.S_IFDIR) if directory else (0, stat.S_IFREG)):
            raise ArchiveError("FILE_TYPE", relative)
        if directory and info.file_size:
            raise ArchiveError("FILE_TYPE", relative)
        if not directory:
            base = relative.rsplit("/", 1)[-1]
            if base.casefold() == "go.mod" and relative != "go.mod":
                raise ArchiveError("PATH_INVALID", relative)
            if relative in ("go.mod", "LICENSE") and info.file_size > 16 * 1024**2:
                raise ArchiveError("FILE_LIMIT", relative)
            expanded += info.file_size
            if expanded > limits.max_expanded:
                raise ArchiveError("EXPANDED_LIMIT")
        cursor = stream.tell()
        offset = info.header_offset
        if values[16] != 0xFFFFFFFF and values[16] != offset:
            raise ArchiveError("ZIP_INVALID", relative)
        stream.seek(offset)
        header = stream.read(30)
        if len(header) != 30:
            raise ArchiveError("ZIP_INVALID", relative)
        local = struct.unpack("<4s5H3I2H", header)
        if local[0] != b"PK\x03\x04" or local[2] != values[3] or local[3] != values[4]:
            raise ArchiveError("ZIP_INVALID", relative)
        local_name = stream.read(local[9])
        _filename(local_name)
        if local_name != raw:
            raise ArchiveError("LOCAL_NAME_MISMATCH", relative)
        extra = stream.read(local[10])
        if len(extra) != local[10] or stream.tell() + info.compress_size > central_start:
            raise ArchiveError("ZIP_INVALID", relative)
        if not local[2] & 8:
            if (
                local[6] != info.CRC
                or local[7] not in (info.compress_size, 0xFFFFFFFF)
                or local[8] not in (info.file_size, 0xFFFFFFFF)
            ):
                raise ArchiveError("ZIP_INVALID", relative)
        stream.seek(cursor)
        rows.append((raw, name, relative, directory, info))
    return rows, expanded


def _snapshot(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_nlink, info.st_mtime_ns)


DEFAULT_LIMITS = ZipLimits()


def inspect_module_zip(path, module, version, *, expected_h1=None, limits=DEFAULT_LIMITS):
    """No extrae: HashZip incluye todas las entradas, también directorios vacíos."""
    validate_module(module, version)
    if expected_h1 is not None:
        try:
            if (
                not expected_h1.startswith("h1:")
                or len(base64.b64decode(expected_h1[3:], validate=True)) != 32
            ):
                raise ValueError
        except (ValueError, AttributeError):
            raise ArchiveError("H1_INVALID") from None
    path = Path(path)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & 0x400
        or not 0 < before.st_size <= limits.max_compressed
    ):
        raise ArchiveError("ARCHIVE_LIMIT_OR_TYPE")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        with os.fdopen(descriptor, "rb") as stream:
            if _snapshot(os.fstat(stream.fileno())) != _snapshot(before):
                raise ArchiveError("ARCHIVE_CHANGED")
            full, compressed_count = hashlib.sha256(), 0
            while chunk := stream.read(65536):
                compressed_count += len(chunk)
                if compressed_count > before.st_size:
                    raise ArchiveError("ARCHIVE_CHANGED")
                full.update(chunk)
            if compressed_count != before.st_size:
                raise ArchiveError("ARCHIVE_CHANGED")
            stream.seek(0)
            hashes, license_rows, source_files, files, actual = [], [], 0, 0, 0
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                if len(infos) > limits.max_entries:
                    raise ArchiveError("ENTRY_LIMIT")
                rows, declared = _central(
                    stream, archive, infos, module + "@" + version + "/", limits
                )
                for raw_name, _name, relative, directory, info in rows:
                    decoder = (
                        codecs.getincrementaldecoder("utf-8")("strict")
                        if not directory and relative.lower().endswith((".go", ".s"))
                        else None
                    )
                    count, file_hash, prefix = 0, hashlib.sha256(), bytearray()
                    with archive.open(info) as incoming:
                        while chunk := incoming.read(65536):
                            count += len(chunk)
                            if count > info.file_size or actual + len(chunk) > limits.max_expanded:
                                raise ArchiveError("EXPANDED_LIMIT", relative)
                            actual += len(chunk)
                            file_hash.update(chunk)
                            if len(prefix) < 65536:
                                prefix.extend(chunk[: 65536 - len(prefix)])
                            if decoder is not None:
                                try:
                                    decoder.decode(chunk)
                                except UnicodeError:
                                    raise ArchiveError("SOURCE_NOT_UTF8", relative) from None
                        if decoder is not None:
                            try:
                                decoder.decode(b"", final=True)
                            except UnicodeError:
                                raise ArchiveError("SOURCE_NOT_UTF8", relative) from None
                    if count != info.file_size:
                        raise ArchiveError("ZIP_INVALID", relative)
                    digest = file_hash.hexdigest()
                    hashes.append((raw_name, digest))
                    if not directory:
                        files += 1
                        source_files += int(decoder is not None)
                        if (
                            relative.rsplit("/", 1)[-1]
                            .upper()
                            .startswith(("LICENSE", "COPYING", "COPYRIGHT", "NOTICE"))
                        ):
                            try:
                                text = prefix.decode("utf-8")
                                valid_text = True
                            except UnicodeError:
                                text, valid_text = "", False
                            license_rows.append(
                                {
                                    "path": relative,
                                    "bytes": count,
                                    "sha256": digest,
                                    "prefix_utf8": valid_text,
                                    "spdx_declared": sorted(
                                        set(
                                            re.findall(
                                                r"SPDX-License-Identifier:\s*([^\r\n]+)", text
                                            )
                                        )
                                    ),
                                }
                            )
                if actual != declared:
                    raise ArchiveError("ZIP_INVALID")
            summary = hashlib.sha256()
            # Go slices.Sort sobre strings compara sus bytes UTF-8. El nombre
            # completo, no sólo la ruta relativa, entra en cada línea Hash1.
            for name, digest in sorted(hashes):
                summary.update(digest.encode("ascii") + b"  " + name + b"\n")
            h1 = "h1:" + base64.b64encode(summary.digest()).decode("ascii")
            if expected_h1 is not None and h1 != expected_h1:
                raise ArchiveError("H1_MISMATCH")
            if _snapshot(os.fstat(stream.fileno())) != _snapshot(before) or _snapshot(
                path.lstat()
            ) != _snapshot(before):
                raise ArchiveError("ARCHIVE_CHANGED")
            return {
                "sha256": full.hexdigest(),
                "compressed_bytes": before.st_size,
                "expanded_bytes": actual,
                "entries": len(hashes),
                "files": files,
                "source_utf8_files": source_files,
                "h1": h1,
                "h1_declared_match": expected_h1 is not None,
                "sumdb_verified_by_python": False,
                "licenses": license_rows,
                "extracted": False,
                "executed": False,
            }
    except (zipfile.BadZipFile, EOFError, struct.error, NotImplementedError):
        raise ArchiveError("ZIP_INVALID") from None
    except UnicodeError:
        raise ArchiveError("NAME_NOT_UTF8") from None
