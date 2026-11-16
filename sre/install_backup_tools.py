"""Plan e instalación local de CLI fijados; nunca configura ni ejecuta respaldos."""

import argparse
import bz2
import hashlib
import io
import json
import os
import platform
import re
import stat
import struct
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from backup_tools_access_background import check_access

OWNER = "agencia-digital-marketing-ai:backup-tools:v1"
MINIMUM_GO = "go1.27.3"
INSTALL_ROOTS = {
    "windows-amd64": r"C:\GPU-Bridge\backup-tools",
    "linux-amd64": "/opt/media-platform-tools",
}
VERSIONS = {"restic": "0.19.1", "rclone": "1.75.2"}
MARKER_FILE = ".agency-backup-tools-owner.json"
RECEIPT_FILE = "install-receipt.json"
LOCK_FILE = ".install.lock"
MAX_BINARY = 128 * 1024**2
MAX_ARCHIVE = 64 * 1024**2
SHA = re.compile(r"[a-f0-9]{64}")
GO = re.compile(r"go1\.(\d+)\.(\d+)")
DEFAULT_MANIFEST = Path(__file__).with_name("backup-tools-artifacts.json")


def native_platform():
    system = platform.system().lower()
    if (
        system not in {"windows", "linux"}
        or platform.machine().lower() not in {"amd64", "x86_64"}
        or struct.calcsize("P") != 8
    ):
        raise ValueError("Solo se admiten Windows y Linux amd64 nativos.")
    return system + "-amd64"


def plain_path(path, *, missing=False):
    path = Path(path).absolute()
    if str(path).startswith(("\\\\", "//")) or ".." in path.parts:
        raise ValueError("Se requiere una ruta local absoluta sin escapes.")
    for component in (*reversed(path.parents), path):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            if missing:
                continue
            raise ValueError("Falta una ruta requerida.") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("No se admiten enlaces ni puntos de reanálisis.")
    return path


def read_regular(path, limit):
    path = plain_path(path)
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ValueError("Se requiere un archivo regular único y acotado.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Se requiere un archivo regular único y acotado.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("El archivo excede su límite.")
    return data


def decode_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON ambiguo por claves duplicadas.")
            result[key] = value
        return result

    def reject(_value):
        raise ValueError("JSON no finito.")

    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=reject)


def load_manifest(path):
    result = decode_json(read_regular(path, 65536))
    validate_manifest(result)
    return result


def valid_hash(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def expected_archive(tool, version, target):
    system = target.split("-")[0]
    if tool == "restic":
        name = f"restic_{version}_{system}_amd64"
        return (
            (name + ".bz2", "bz2", None)
            if system == "linux"
            else (name + ".zip", "zip", name + ".exe")
        )
    name = f"rclone-v{version}-{system}-amd64"
    return name + ".zip", "zip", name + "/rclone" + (".exe" if system == "windows" else "")


def validate_manifest(document):
    fields = {
        "schema_version",
        "owner",
        "as_of_utc",
        "minimum_go",
        "installation_enabled",
        "artifacts",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
    ):
        raise ValueError("El manifiesto no tiene un esquema implementado.")
    if (
        document["owner"] != OWNER
        or document["minimum_go"] != MINIMUM_GO
        or type(document["installation_enabled"]) is not bool
    ):
        raise ValueError("La propiedad o política del manifiesto no coincide.")
    if not isinstance(document["as_of_utc"], str) or not document["as_of_utc"].endswith("Z"):
        raise ValueError("Falta fecha UTC de revisión.")
    datetime.fromisoformat(document["as_of_utc"].replace("Z", "+00:00"))
    if not isinstance(document["artifacts"], list):
        raise ValueError("Falta inventario de artefactos.")
    seen = set()
    for entry in document["artifacts"]:
        if not isinstance(entry, dict) or set(entry) != {
            "tool",
            "version",
            "platform",
            "source",
            "archive",
            "binary",
            "review",
        }:
            raise ValueError("Artefacto incompleto o con campos ajenos.")
        tool, version, target = entry["tool"], entry["version"], entry["platform"]
        if (
            not isinstance(tool, str)
            or tool not in VERSIONS
            or version != VERSIONS[tool]
            or not isinstance(target, str)
            or target not in {"windows-amd64", "linux-amd64"}
            or (tool, target) in seen
        ):
            raise ValueError("No se admiten versiones, plataformas o roles sustituidos.")
        seen.add((tool, target))
        source = entry["source"]
        expected_repo = f"https://github.com/{tool}/{tool}"
        if not isinstance(source, dict) or set(source) != {
            "repository",
            "revision",
            "release_url",
            "checksums_url",
            "license_url",
            "license_spdx",
        }:
            raise ValueError("Falta procedencia oficial.")
        revision = source["revision"]
        if (
            not isinstance(revision, str)
            or re.fullmatch(r"[a-f0-9]{40}", revision) is None
            or source
            != {
                "repository": expected_repo,
                "revision": revision,
                "release_url": expected_repo + f"/releases/tag/v{version}",
                "checksums_url": expected_repo + f"/releases/download/v{version}/SHA256SUMS",
                "license_url": f"https://raw.githubusercontent.com/{tool}/{tool}/{revision}/LICENSE",
                "license_spdx": "BSD-2-Clause" if tool == "restic" else "MIT",
            }
        ):
            raise ValueError("La procedencia no corresponde a la publicación oficial.")
        archive = entry["archive"]
        name, kind, member = expected_archive(tool, version, target)
        if (
            not isinstance(archive, dict)
            or set(archive) != {"filename", "url", "format", "member", "sha256", "size"}
            or (archive["filename"], archive["format"], archive["member"]) != (name, kind, member)
            or archive["url"] != expected_repo + f"/releases/download/v{version}/{name}"
            or not valid_hash(archive["sha256"])
            or type(archive["size"]) is not int
            or not 0 < archive["size"] <= MAX_ARCHIVE
        ):
            raise ValueError("El archivo comprimido no tiene identidad oficial y acotada.")
        binary = entry["binary"]
        if (
            not isinstance(binary, dict)
            or set(binary) != {"filename", "sha256", "size", "format", "go_version"}
            or binary["filename"] != tool + (".exe" if target == "windows-amd64" else "")
            or binary["format"] != ("pe-amd64" if target == "windows-amd64" else "elf-amd64")
            or not valid_hash(binary["sha256"])
            or type(binary["size"]) is not int
            or not 0 < binary["size"] <= MAX_BINARY
            or not isinstance(binary["go_version"], str)
            or GO.fullmatch(binary["go_version"]) is None
        ):
            raise ValueError("El ejecutable no tiene identidad y formato completos.")
        review = entry["review"]
        if (
            not isinstance(review, dict)
            or set(review) != {"status", "high", "critical", "coverage_verified", "report_sha256"}
            or not isinstance(review["status"], str)
            or review["status"] not in {"blocked", "verified"}
            or any(type(review[key]) is not int or review[key] < 0 for key in ("high", "critical"))
            or type(review["coverage_verified"]) is not bool
            or not valid_hash(review["report_sha256"])
        ):
            raise ValueError("Falta revisión de seguridad explícita.")
    if seen != {(tool, target) for tool in VERSIONS for target in ("windows-amd64", "linux-amd64")}:
        raise ValueError("No se pueden omitir artefactos obligatorios.")


def selected_entries(document, target, tools):
    if target not in INSTALL_ROOTS or not tools or not set(tools) <= set(VERSIONS):
        raise ValueError("Selección de destino o herramientas no permitida.")
    return [
        entry
        for entry in document["artifacts"]
        if entry["platform"] == target and entry["tool"] in tools
    ]


def security_ready(entry):
    review = entry["review"]
    version = tuple(map(int, GO.fullmatch(entry["binary"]["go_version"]).groups()))
    minimum = tuple(map(int, GO.fullmatch(MINIMUM_GO).groups()))
    return (
        review["status"] == "verified"
        and review["coverage_verified"] is True
        and review["high"] == review["critical"] == 0
        and version >= minimum
    )


def make_plan(document, target, tools=("restic", "rclone")):
    validate_manifest(document)
    entries = selected_entries(document, target, tools)
    return {
        "mode": "plan",
        "owner": OWNER,
        "destination": INSTALL_ROOTS[target],
        "platform": target,
        "installation_allowed": document["installation_enabled"]
        and all(security_ready(entry) for entry in entries),
        "artifacts": entries,
        "network_used": False,
        "binaries_executed": False,
    }


def go_version(content):
    offset = content.find(b"\xff Go buildinf:")
    if offset < 0 or offset + 32 >= len(content) or not content[offset + 15] & 2:
        raise ValueError("No se pudo verificar la versión Go embebida.")
    position, size, shift = offset + 32, 0, 0
    for _ in range(10):
        if position >= len(content):
            raise ValueError("Buildinfo Go truncado.")
        value = content[position]
        position += 1
        size |= (value & 127) << shift
        if value < 128:
            break
        shift += 7
    else:
        raise ValueError("Buildinfo Go no válido.")
    if size > 32 or position + size > len(content):
        raise ValueError("Buildinfo Go fuera de límites.")
    return content[position : position + size].decode("ascii")


def validate_binary(content, metadata):
    if (
        len(content) != metadata["size"]
        or hashlib.sha256(content).hexdigest() != metadata["sha256"]
    ):
        raise ValueError("El ejecutable extraído no coincide con su pin.")
    if metadata["format"] == "elf-amd64":
        valid = (
            len(content) >= 20
            and content[:6] == b"\x7fELF\x02\x01"
            and struct.unpack_from("<H", content, 18)[0] == 62
        )
    else:
        valid = False
        if len(content) >= 64 and content[:2] == b"MZ":
            offset = struct.unpack_from("<I", content, 60)[0]
            valid = (
                offset <= len(content) - 26
                and content[offset : offset + 4] == b"PE\0\0"
                and struct.unpack_from("<H", content, offset + 4)[0] == 0x8664
                and struct.unpack_from("<H", content, offset + 24)[0] == 0x20B
            )
    if not valid or go_version(content) != metadata["go_version"]:
        raise ValueError("El formato, arquitectura o compilador del binario difiere del pin.")


def extract_binary(path, entry):
    archive, metadata = entry["archive"], entry["binary"]
    data = read_regular(path, min(archive["size"], MAX_ARCHIVE))
    if len(data) != archive["size"] or hashlib.sha256(data).hexdigest() != archive["sha256"]:
        raise ValueError("El archivo comprimido no coincide con su pin.")
    try:
        if archive["format"] == "bz2":
            decoder = bz2.BZ2Decompressor()
            content = decoder.decompress(data, max_length=metadata["size"] + 1)
            if not decoder.eof or decoder.unused_data:
                raise ValueError("BZ2 truncado, concatenado o con expansión excesiva.")
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                seen, total, chosen = set(), 0, None
                members = package.infolist()
                if len(members) > 64:
                    raise ValueError("ZIP con demasiadas entradas.")
                for member in members:
                    name = member.orig_filename
                    parts = name.rstrip("/").split("/")
                    mode = member.external_attr >> 16
                    total += member.file_size
                    if (
                        not name
                        or name.startswith("/")
                        or any(char in name for char in ("\\", ":", "\0"))
                        or any(part in {"", ".", ".."} for part in parts)
                        or name.casefold() in seen
                        or member.flag_bits & 1
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or total > 256 * 1024**2
                    ):
                        raise ValueError("ZIP con rutas, enlaces o tamaño no permitidos.")
                    seen.add(name.casefold())
                    if name == archive["member"]:
                        if member.is_dir() or member.file_size != metadata["size"]:
                            raise ValueError("La entrada ejecutable no corresponde al pin.")
                        chosen = member
                if chosen is None:
                    raise ValueError("Falta el ejecutable único fijado.")
                with package.open(chosen) as stream:
                    content = stream.read(metadata["size"] + 1)
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as error:
        raise ValueError("El archivo comprimido no es válido.") from error
    validate_binary(content, metadata)
    return content


def marker(target, platform_id):
    return {"schema_version": 1, "owner": OWNER, "platform": platform_id, "root": str(target)}


def inspect_target(
    target, platform_id, entries, manifest_sha, *, own_lock=False, check_receipt=True
):
    plain_path(target, missing=True)
    check_access(target.parent, private=platform_id.startswith("windows"))
    if not target.exists():
        return {}
    if not target.is_dir():
        raise ValueError("El destino está ocupado por otro recurso.")
    check_access(target, private=True)
    if not (target / MARKER_FILE).is_file() or decode_json(
        read_regular(target / MARKER_FILE, 4096)
    ) != marker(target, platform_id):
        raise ValueError("El destino existente no tiene propiedad acreditada.")
    check_access(target / MARKER_FILE, private=True)
    expected = {entry["binary"]["filename"]: entry["binary"] for entry in entries}
    allowed = set(expected) | {MARKER_FILE, RECEIPT_FILE}
    if own_lock:
        allowed.add(LOCK_FILE)
    if any(path.name not in allowed for path in target.iterdir()):
        raise ValueError("El destino contiene archivos ajenos o una instalación en curso.")
    found = {}
    for name, metadata in expected.items():
        path = target / name
        if path.exists():
            check_access(path)
            data = read_regular(path, metadata["size"])
            if (
                len(data) != metadata["size"]
                or hashlib.sha256(data).hexdigest() != metadata["sha256"]
            ):
                raise ValueError("Un binario existente fue alterado; no se sobrescribe.")
            found[name] = metadata["sha256"]
    receipt = target / RECEIPT_FILE
    if receipt.exists() and check_receipt:
        check_access(receipt, private=True)
        value = decode_json(read_regular(receipt, 65536))
        if value != {
            **marker(target, platform_id),
            "manifest_sha256": manifest_sha,
            "binaries": found,
        }:
            raise ValueError("El recibo existente no acredita esta instalación.")
    return found


def publish_file(target, content, *, executable=False, replace=False):
    descriptor, name = tempfile.mkstemp(prefix=".backup-tool-", dir=target.parent)
    temporary = Path(name)
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755 if executable else 0o600)
        if replace:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        if temporary.exists():
            current = os.lstat(temporary)
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError("El temporal propio cambió de identidad.")
            temporary.unlink()


def apply_install(manifest_path, archives, trusted_sha256, platform_id, tools=("restic", "rclone")):
    if not valid_hash(trusted_sha256) or platform_id != native_platform():
        raise ValueError("Falta hash externo o la plataforma no es nativa.")
    raw = read_regular(manifest_path, 65536)
    if hashlib.sha256(raw).hexdigest() != trusted_sha256:
        raise ValueError("El manifiesto difiere del hash externo revisado.")
    document = decode_json(raw)
    validate_manifest(document)
    entries = selected_entries(document, platform_id, tools)
    if not document["installation_enabled"] or not all(security_ready(entry) for entry in entries):
        raise ValueError("La revisión de seguridad bloquea la instalación de estos binarios.")
    if os.name != "nt" and os.geteuid() != 0:
        raise ValueError("La instalación Linux requiere el propietario root.")
    target = Path(INSTALL_ROOTS[platform_id])
    all_entries = selected_entries(document, platform_id, tuple(VERSIONS))
    found = inspect_target(target, platform_id, all_entries, trusted_sha256)
    receipt_path = target / RECEIPT_FILE
    receipt_before = read_regular(receipt_path, 65536) if receipt_path.exists() else None
    payloads = {
        entry["binary"]["filename"]: extract_binary(
            Path(archives) / entry["archive"]["filename"], entry
        )
        for entry in entries
    }
    if not target.exists():
        target.mkdir(mode=0o700)
        publish_file(
            target / MARKER_FILE, json.dumps(marker(target, platform_id), sort_keys=True).encode()
        )
        check_access(target, private=True)
    descriptor = os.open(target / LOCK_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    lock_identity = os.fstat(descriptor)
    os.close(descriptor)
    installed = 0
    try:
        for name, data in payloads.items():
            if name not in found:
                publish_file(target / name, data, executable=True)
                installed += 1
        current = inspect_target(
            target, platform_id, all_entries, trusted_sha256, own_lock=True, check_receipt=False
        )
        if (receipt_before is None and receipt_path.exists()) or (
            receipt_before is not None and read_regular(receipt_path, 65536) != receipt_before
        ):
            raise ValueError("El recibo previo cambió durante la instalación.")
        if installed or receipt_before is None:
            receipt = {
                **marker(target, platform_id),
                "manifest_sha256": trusted_sha256,
                "binaries": current,
            }
            publish_file(
                receipt_path,
                json.dumps(receipt, sort_keys=True).encode(),
                replace=receipt_before is not None,
            )
    finally:
        current = os.lstat(target / LOCK_FILE)
        if (current.st_dev, current.st_ino) != (lock_identity.st_dev, lock_identity.st_ino):
            raise ValueError("El lock propio cambió de identidad; no se elimina.")
        (target / LOCK_FILE).unlink()
    inspect_target(target, platform_id, all_entries, trusted_sha256)
    return {
        "mode": "apply",
        "destination": str(target),
        "platform": platform_id,
        "installed": installed,
        "binaries_executed": False,
        "network_used": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--archives", type=Path)
    parser.add_argument("--platform", choices=("windows-amd64", "linux-amd64"))
    parser.add_argument("--tools", nargs="+", choices=tuple(VERSIONS), default=list(VERSIONS))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = args.platform or native_platform()
        if args.apply:
            if args.archives is None:
                raise ValueError("Se requieren archivos locales verificados para aplicar.")
            result = apply_install(
                args.manifest, args.archives, args.manifest_sha256, target, args.tools
            )
        else:
            document = load_manifest(args.manifest)
            result = make_plan(document, target, args.tools)
            if args.archives is not None:
                for entry in selected_entries(document, target, args.tools):
                    extract_binary(args.archives / entry["archive"]["filename"], entry)
                result["archive_and_binary_integrity_verified"] = True
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, UnicodeError, RecursionError, struct.error, IndexError, TypeError):
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "plan",
                    "verified": False,
                    "error": "La operación fue rechazada por configuración, integridad, seguridad o propiedad.",
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
