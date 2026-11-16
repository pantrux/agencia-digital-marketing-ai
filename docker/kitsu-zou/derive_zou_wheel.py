"""Deriva sólo metadatos de Zou; no importa, instala ni ejecuta su código."""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import struct
import sys
import types
import zipfile
import zlib
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import packaging
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

OLD_DIST = "zou-1.1.1.dist-info"
NEW_DIST = "zou-1.1.1+agency1.dist-info"
OUTPUT = "zou-1.1.1+agency1-py3-none-any.whl"
WORKSPACE = Path(__file__).resolve().parents[2]
OWNER = "agencia-digital-marketing-ai:zou-wheel-derivative:v1"
MARKER = ".agency-zou-derived-owner.json"
ROOT_RELATIVE = ".tools/kitsu-zou-wheel-derivative-v1"
MANIFEST = Path(__file__).with_name("zou-wheel-derivative-inputs.json")
CHANGES = {
    b"Version: 1.1.1": b"Version: 1.1.1+agency1",
    b"Requires-Dist: psycopg[binary]==3.3.4": b"Requires-Dist: psycopg==3.3.4",
    b'Requires-Dist: psycopg[c]==3.3.4; extra == "prodpg"': (
        b'Requires-Dist: psycopg==3.3.4; extra == "prodpg"'
    ),
}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_json(data):
    return (
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def strict_json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("JSON_CONSTANT")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


def plain(path, *, directory=False):
    path = Path(path).absolute()
    for parent in [*reversed(path.parents), path]:
        info = parent.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "FILE_LINK",
        )
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        if directory
        else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "FILE_TYPE",
    )
    return info


def read_file(path, maximum):
    before = plain(path)
    require(before.st_size <= maximum, "FILE_SIZE")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(fd)
        require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino), "FILE_CHANGED")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        after = os.fstat(fd)
        require(
            len(raw) <= maximum
            and (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            and after.st_nlink == 1,
            "FILE_CHANGED",
        )
        return raw
    finally:
        os.close(fd)


def write_new(path, raw, check_private=None):
    # O_EXCL acredita esta creación; un fallo deja el recurso para diagnóstico, sin borrar ajenos.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if check_private:
            check_private(path, private=True)  # Comprueba el archivo aún vacío.
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def safe_name(name):
    require(isinstance(name, str) and name.isascii() and 0 < len(name) <= 240, "ZIP_PATH")
    parts = PurePosixPath(name).parts
    require(
        not name.startswith("/")
        and "\\" not in name
        and all(p not in {"", ".", ".."} for p in name.split("/"))
        and all(not p.endswith((" ", ".")) for p in parts)
        and re.fullmatch(r"[A-Za-z0-9_+./-]+", name),
        "ZIP_PATH",
    )
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)
    }
    require(all(p.split(".")[0].upper() not in reserved for p in parts), "ZIP_PATH")


def inspect_wheel(raw, dist, *, max_total=32 * 1024 * 1024):
    """Valida ZIP y RECORD completos en memoria; nunca extrae rutas del archivo."""
    require(0 < len(raw) <= 2 * 1024 * 1024, "ZIP_SIZE")
    members, folded, spans = {}, set(), []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            require(not archive.comment and 0 < len(archive.infolist()) <= 10000, "ZIP_SIZE")
            require(sum(i.file_size for i in archive.infolist()) <= max_total, "ZIP_SIZE")
            for info in archive.infolist():
                name = info.filename
                safe_name(name)
                require(name.casefold() not in folded, "ZIP_DUPLICATE")
                folded.add(name.casefold())
                require(
                    not info.is_dir()
                    and stat.S_IFMT(info.external_attr >> 16) in {0, stat.S_IFREG}
                    and not info.flag_bits & (1 | 8)
                    and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                    "ZIP_TYPE",
                )
                require(not name.endswith(("/RECORD.jws", "/RECORD.p7s")), "ZIP_SIGNATURE")
                require(name.startswith("zou/") or name.startswith(dist + "/"), "ZIP_ROOT")
                # Contrasta también cabecera local, nombre y límites físicos, sin extraer.
                start = info.header_offset
                require(0 <= start <= len(raw) - 30, "ZIP_LOCAL")
                fields = struct.unpack_from("<4s5H3I2H", raw, start)
                require(fields[0] == b"PK\x03\x04", "ZIP_LOCAL")
                name_size, extra_size = fields[-2:]
                body = start + 30 + name_size + extra_size
                encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
                require(
                    raw[start + 30 : start + 30 + name_size].decode(encoding) == name
                    and fields[2] == info.flag_bits
                    and fields[3] == info.compress_type
                    and fields[6:9] == (info.CRC, info.compress_size, info.file_size)
                    and body + info.compress_size <= archive.start_dir,
                    "ZIP_LOCAL",
                )
                spans.append((start, body + info.compress_size))
                members[name] = archive.read(info)  # zipfile contrasta CRC y tamaño.
            spans.sort()
            require(
                spans[0][0] == 0
                and all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))
                and spans[-1][1] == archive.start_dir,
                "ZIP_OVERLAP",
            )
    except (zipfile.BadZipFile, UnicodeError, struct.error, RuntimeError):
        raise ValueError("ZIP_INVALID") from None
    record = dist + "/RECORD"
    require(record in members and dist + "/METADATA" in members, "WHEEL_REQUIRED")
    seen = set()
    try:
        for row in csv.reader(io.StringIO(members[record].decode("utf-8"), newline="")):
            require(len(row) == 3 and row[0] not in seen, "RECORD_DUPLICATE")
            name, checksum, size = row
            require(name in members, "RECORD_PATH")
            seen.add(name)
            if name == record:
                require(checksum == size == "", "RECORD_SELF")
            else:
                expected = base64.urlsafe_b64encode(hashlib.sha256(members[name]).digest())
                require(
                    checksum == "sha256=" + expected.rstrip(b"=").decode()
                    and size == str(len(members[name])),
                    "RECORD_HASH",
                )
    except (UnicodeError, csv.Error):
        raise ValueError("RECORD_INVALID") from None
    require(seen == set(members), "RECORD_COVERAGE")
    return members


def replace_metadata(raw):
    """Sustituye tres cabeceras exactas; conserva descripción, extras y demás bytes."""
    require(b"\r" not in raw and b"\n\n" in raw, "METADATA_FORMAT")
    headers, body = raw.split(b"\n\n", 1)
    lines = headers.split(b"\n")
    metadata = BytesParser().parsebytes(raw, headersonly=True)
    require(
        metadata.get_all("Name") == ["zou"]
        and metadata.get_all("Version") == ["1.1.1"]
        and metadata.get_all("Requires-Python") == [">=3.11, <3.15"],
        "METADATA_IDENTITY",
    )
    for before, after in CHANGES.items():
        require(lines.count(before) == 1, "METADATA_EXPECTED_LINE")
        lines[lines.index(before)] = after
    return b"\n".join(lines) + b"\n\n" + body


def derive_bytes(raw):
    original = inspect_wheel(raw, OLD_DIST)
    transformed = {}
    for name, body in original.items():
        if name == OLD_DIST + "/RECORD":
            continue
        target = name.replace(OLD_DIST + "/", NEW_DIST + "/", 1)
        transformed[target] = replace_metadata(body) if name == OLD_DIST + "/METADATA" else body
    record = NEW_DIST + "/RECORD"
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\n")
    for name, body in sorted(transformed.items()):
        checksum = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + checksum.decode(), str(len(body))))
    writer.writerow((record, "", ""))
    transformed[record] = text.getvalue().encode("utf-8")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in sorted(transformed.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body, compresslevel=9)
    result = stream.getvalue()
    require(inspect_wheel(result, NEW_DIST) == transformed, "OUTPUT_VERIFICATION")
    return result, {
        "original_sha256": digest(raw),
        "wheel_sha256": digest(result),
        "wheel_bytes": len(result),
        "members": len(transformed),
        "unchanged_members": len(original) - 2,
        "original_metadata_sha256": digest(original[OLD_DIST + "/METADATA"]),
        "metadata_sha256": digest(transformed[NEW_DIST + "/METADATA"]),
        "preserved_members_sha256": digest(
            "\n".join(
                f"{name} {digest(body)}"
                for name, body in sorted(original.items())
                if name not in {OLD_DIST + "/METADATA", OLD_DIST + "/RECORD"}
            ).encode()
        ),
    }


def validate_closure(raw_metadata, *, extras=()):
    """Resuelve únicamente contra versiones existentes y sus METADATA, sin red ni pip."""
    metadata = {}
    for name, raw in raw_metadata.items():
        m = BytesParser().parsebytes(raw, headersonly=True)
        require(
            m.get_all("Name") is not None
            and len(m.get_all("Name")) == 1
            and m.get_all("Version") is not None
            and len(m.get_all("Version")) == 1
            and canonicalize_name(m["Name"]) == name,
            "GRAPH_METADATA",
        )
        metadata[name] = m
    environment = default_environment()
    environment.update(
        os_name="posix",
        sys_platform="linux",
        platform_system="Linux",
        platform_machine="x86_64",
        python_version="3.12",
        python_full_version="3.12.16",
        implementation_name="cpython",
        implementation_version="3.12.16",
        platform_python_implementation="CPython",
        platform_release="",
        platform_version="",
    )
    selected, visited, edges = {"zou": set(extras)}, set(), set()
    pending = ["zou"]
    while pending:
        name = pending.pop()
        require(name in metadata, "GRAPH_MISSING")
        requested = selected[name]
        signature = (name, tuple(sorted(requested)))
        if signature in visited:
            continue
        visited.add(signature)
        m = metadata[name]
        require(requested <= set(m.get_all("Provides-Extra", [])), "GRAPH_EXTRA")
        require(Version("3.12.16") in SpecifierSet(m.get("Requires-Python", "")), "GRAPH_PYTHON")
        for line in m.get_all("Requires-Dist", []):
            requirement = Requirement(line)
            require(requirement.url is None, "GRAPH_DIRECT_URL")
            if requirement.marker and not any(
                requirement.marker.evaluate(environment | {"extra": extra})
                for extra in requested | {""}
            ):
                continue
            dependency = canonicalize_name(requirement.name)
            require(dependency in metadata, "GRAPH_MISSING")
            require(
                Version(metadata[dependency]["Version"]) in requirement.specifier,
                "GRAPH_VERSION",
            )
            edges.add((name, str(requirement)))
            if dependency not in selected or not requirement.extras <= selected[dependency]:
                selected.setdefault(dependency, set()).update(requirement.extras)
                pending.append(dependency)
    return {
        "selected_count": len(selected),
        "selected": sorted(selected),
        "excluded": sorted(set(metadata) - set(selected)),
        "active_edges": len(edges),
        "edges_sha256": digest(canonical_json(sorted(edges))),
        "extras": {name: sorted(value) for name, value in sorted(selected.items())},
    }


def validate_baseline_sca(report, inventory, selected):
    """Acredita el subconjunto de versiones upstream del informe histórico, sin nueva consulta."""
    expected = {canonicalize_name(row["name"]): row["version"] for row in inventory}
    require(len(expected) == len(inventory), "SCA_INVENTORY")
    seen = set()
    for row in report["dependencies"]:
        name = canonicalize_name(row["name"])
        require(name in expected and name not in seen, "SCA_COVERAGE")
        require(row["version"] == expected[name], "SCA_VERSION")
        require(row.get("vulns") == [] and "skip_reason" not in row, "SCA_FINDINGS")
        seen.add(name)
    require(seen == set(expected) and set(selected) <= seen, "SCA_COVERAGE")
    return {
        "scope": "historical-upstream-version-subset",
        "original_versions": len(expected),
        "selected_upstream_versions": len(selected),
        "vulnerabilities": 0,
        "skipped": 0,
        "new_queries": False,
        "local_zou_version_queried": False,
        "native_sca_verified": False,
    }


def publish(root, raw, plan_dto, expected_plan, *, check_private=None):
    """Sólo publica bytes ya previstos en un directorio acreditado por el controlador."""
    plan_sha = digest(canonical_json(plan_dto))
    require(expected_plan == plan_sha, "PLAN_PIN")
    require(digest(raw) == plan_dto["wheel"]["wheel_sha256"], "OUTPUT_HASH")
    intent = {
        "schema_version": 1,
        "owner": OWNER,
        "plan_sha256": plan_sha,
        "wheel_sha256": digest(raw),
        "filename": OUTPUT,
    }
    intent_path, target, receipt_path = (
        root / "derived-intent.json",
        root / OUTPUT,
        root / "derived-receipt.json",
    )
    if not intent_path.exists():
        require(not target.exists() and not receipt_path.exists(), "OUTPUT_WITHOUT_INTENT")
        write_new(intent_path, canonical_json(intent), check_private)
    else:
        require(strict_json(read_file(intent_path, 4096)) == intent, "INTENT_CHANGED")
    created = not target.exists()
    if created:
        require(not receipt_path.exists(), "RECEIPT_WITHOUT_OUTPUT")
        write_new(target, raw, check_private)
    require(read_file(target, 2 * 1024 * 1024) == raw, "OUTPUT_CHANGED")
    receipt = {
        **intent,
        "wheel_bytes": len(raw),
        "manifest_sha256": plan_dto["manifest_sha256"],
        "installed": False,
        "sdk_executed": False,
        "runtime_verified": False,
    }
    if receipt_path.exists():
        require(strict_json(read_file(receipt_path, 4096)) == receipt, "RECEIPT_CHANGED")
    else:
        write_new(receipt_path, canonical_json(receipt), check_private)
    return {**receipt, "created": created, "receipt_sha256": digest(canonical_json(receipt))}


def load_access(manifest):
    pin = manifest["access_helper"]
    path = WORKSPACE / pin["path"]
    raw = read_file(path, 65536)
    require(digest(raw) == pin["sha256"], "ACCESS_HELPER_PIN")
    module = types.ModuleType("zou_derivative_access")
    module.__file__ = str(path)
    # Compila exactamente los bytes acreditados; no consulta loaders ni cachés .pyc.
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module.check_access


def inspect_context(manifest_path=MANIFEST):
    manifest_raw = read_file(manifest_path, 128 * 1024)
    manifest = strict_json(manifest_raw)
    require(manifest["schema_version"] == 1 and manifest["owner"] == OWNER, "MANIFEST_CONTRACT")
    require(manifest["root_relative"] == ROOT_RELATIVE, "ROOT_PATH")
    require(manifest["original"]["filename"] == "zou-1.1.1-py3-none-any.whl", "SOURCE_PATH")
    require(
        manifest["output"] == {"filename": OUTPUT, "version": "1.1.1+agency1"}
        and manifest["runtime"]["environment"] == {"PSYCOPG_IMPL": "python"},
        "MANIFEST_CONTRACT",
    )
    require(packaging.__version__ == "26.2", "PACKAGING_VERSION")
    check_private = load_access(manifest)
    root = WORKSPACE / ROOT_RELATIVE
    info = plain(root, directory=True)
    identity = manifest["root_identity"]
    marker_raw = read_file(root / MARKER, 4096)
    require(
        digest(marker_raw) == manifest["marker_sha256"]
        and strict_json(marker_raw) == identity
        and identity["root"] == str(root)
        and identity["owner"] == OWNER
        and (info.st_dev, info.st_ino) == (identity["device"], identity["inode"]),
        "ROOT_IDENTITY",
    )
    allowed = {
        MARKER,
        manifest["original"]["filename"],
        OUTPUT,
        "derived-intent.json",
        "derived-receipt.json",
    }
    require({p.name for p in root.iterdir()} <= allowed, "ROOT_FOREIGN_ENTRY")
    check_private(root, private=True)
    for p in root.iterdir():
        plain(p)
        check_private(p, private=True)
    source = read_file(root / manifest["original"]["filename"], 2 * 1024 * 1024)
    require(
        digest(source) == manifest["original"]["sha256"]
        and len(source) == manifest["original"]["bytes"],
        "ORIGINAL_PIN",
    )
    derived, proof = derive_bytes(source)
    require(
        proof["original_metadata_sha256"] == manifest["original"]["metadata_sha256"], "METADATA_PIN"
    )
    original_members = inspect_wheel(source, OLD_DIST)
    require(
        digest(original_members[OLD_DIST + "/licenses/LICENSE"])
        == manifest["original"]["license_sha256"],
        "LICENSE_PIN",
    )
    inputs = {}
    for row in manifest["evidence_inputs"]:
        raw = read_file(WORKSPACE / row["path"], 4 * 1024 * 1024)
        require(digest(raw) == row["sha256"], "EVIDENCE_PIN")
        inputs[row["role"]] = strict_json(raw)
    inventory, audit = inputs["inventory"], inputs["wheelhouse_audit"]
    rows = audit["files"]
    require(len(rows) == len(inventory["packages"]) == 142, "INVENTORY_COUNT")
    expected = {canonicalize_name(r["name"]): r for r in inventory["packages"]}
    require(len(expected) == len(rows), "INVENTORY_DUPLICATE")
    cache = WORKSPACE / ".tools/cache/zou-runtime-linux-cp312-v1"
    ci = plain(cache, directory=True)
    require(
        str(ci.st_dev) == audit["directory"]["device"]
        and str(ci.st_ino) == audit["directory"]["inode"]
        and audit["cache_root"] == str(cache),
        "WHEELHOUSE_IDENTITY",
    )
    metadata, wheel_inputs = {}, []
    for row in rows:
        name = canonicalize_name(row["name"])
        require(name in expected and name not in metadata, "INVENTORY_DUPLICATE")
        require(
            row["sha256"] == expected[name]["sha256"]
            and row["version"] == expected[name]["version"]
            and Path(row["filename"]).name == row["filename"]
            and row["cache_path"] == str(cache / row["filename"]),
            "WHEELHOUSE_PATH",
        )
        raw = read_file(cache / row["filename"], 100 * 1024 * 1024)
        require(digest(raw) == row["sha256"], "WHEELHOUSE_HASH")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [
                i
                for i in archive.infolist()
                if i.filename.count("/") == 1 and i.filename.endswith(".dist-info/METADATA")
            ]
            require(len(names) == 1 and names[0].file_size <= 1024 * 1024, "WHEELHOUSE_METADATA")
            metadata[name] = archive.read(names[0])
        m = BytesParser().parsebytes(metadata[name], headersonly=True)
        require(m["Version"] == row["version"], "WHEELHOUSE_VERSION")
        require(
            [str(Requirement(r)) for r in m.get_all("Requires-Dist", [])]
            == [str(Requirement(r)) for r in expected[name]["requires_dist"]],
            "WHEELHOUSE_REQUIRES",
        )
        wheel_inputs.append(
            {
                "name": name,
                "version": row["version"],
                "sha256": row["sha256"],
                "metadata_sha256": digest(metadata[name]),
            }
        )
    metadata["zou"] = replace_metadata(metadata["zou"])
    closures = {
        "default": validate_closure(metadata),
        "prodpg": validate_closure(metadata, extras=("prodpg",)),
    }
    for name, closure in closures.items():
        require(
            closure["selected_count"] == 141
            and closure["excluded"] == ["psycopg-binary"]
            and closure["extras"]["psycopg"] == []
            and closure["active_edges"] == (249 if name == "default" else 250),
            "GRAPH_CONTRACT",
        )
    plan_dto = {
        "schema_version": 1,
        "owner": OWNER,
        "operation": "derive-metadata-only",
        "root_identity": identity,
        "manifest_sha256": digest(manifest_raw),
        "controller_sha256": digest(read_file(Path(__file__), 128 * 1024)),
        "tool": {
            "python": sys.version.split()[0],
            "zlib": zlib.ZLIB_RUNTIME_VERSION,
            "packaging": packaging.__version__,
        },
        "wheel": proof,
        "closure": closures,
        "wheel_metadata_inputs_sha256": digest(canonical_json(wheel_inputs)),
        "baseline_python_sca": validate_baseline_sca(
            inputs["baseline_osv"], inventory["packages"], closures["default"]["selected"]
        ),
        "installed": False,
        "sdk_executed": False,
        "native_runtime_verified": False,
    }
    return root, derived, plan_dto, check_private


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Plan cerrado de rueda Zou local; sin instalación."
    )
    parser.add_argument("--operation", choices=("plan", "apply"), default="plan")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    if args.operation == "apply":
        require(args.manifest_sha256 == digest(read_file(MANIFEST, 128 * 1024)), "MANIFEST_PIN")
    root, raw, plan_dto, check_private = inspect_context()
    if args.operation == "plan":
        result = {"plan": plan_dto, "plan_sha256": digest(canonical_json(plan_dto))}
    else:
        result = publish(root, raw, plan_dto, args.plan_sha256, check_private=check_private)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, zipfile.BadZipFile):
        print(json.dumps({"success": False, "error": "ZOU_DERIVATIVE_REJECTED"}))
        raise SystemExit(1) from None
