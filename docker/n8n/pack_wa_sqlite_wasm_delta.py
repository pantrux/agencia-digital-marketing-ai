"""Verifica y prepara el delta de wa-sqlite para el editor de n8n."""

import argparse
import io
import json
import os
import re
import stat
import tarfile
from pathlib import Path

from stage_server_wa_sqlite import (
    REV,
    SQLITE_OLD_ID,
    WA_LEAF,
    read_pinned,
    require,
    safe,
    sha,
    sri,
    tar_gz,
)

BASELINE_SHA = "16dcd4442cb7073182c1eb2951e495c4415f7de8924d188fafb670a8798da662"
CONTEXT_SHA = "e9152893ca882710fa5e4c494a7a19356391b214bc1c331f7d9b7b259785d413"
SDK_MANIFEST = "sha256:40a30b0e22be2b1fce570a5fcbf8124c4ccb22962b49850546863da07d374a51"
SOURCE_ID = b"2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
VARIANTS = ("wa-sqlite", "wa-sqlite-async", "wa-sqlite-jspi")
DIST = frozenset(f"{name}.{ext}" for name in VARIANTS for ext in ("mjs", "wasm"))
OUTPUT = "wa-sqlite-1.0.9-sqlite3.54.1.tgz"
MAX_FILE = 4 * 1024**2
WASM_MAGIC = b"\0asm\1\0\0\0"


def read_baseline(root):
    raw = read_pinned(root / WA_LEAF, 1214640, BASELINE_SHA)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for row in archive:
            require(
                row.isfile()
                and row.name.startswith("package/")
                and safe(row.name)
                and row.name not in files
                and 0 <= row.size <= MAX_FILE
                and row.mode == 0o644,
                "Miembro inesperado en el paquete original.",
            )
            with archive.extractfile(row) as stream:
                files[row.name] = (stream.read(), row.mode)
    require(len(files) == 57, "Inventario inesperado en el paquete original.")
    return files


def linked(info):
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def read_dist(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and not linked(info), "Directorio dist enlazado o ajeno.")
    require({leaf.name for leaf in path.iterdir()} == DIST, "Inventario dist distinto.")
    built = {}
    for name in sorted(DIST):
        leaf = path / name
        info = leaf.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and not linked(info)
            and info.st_nlink == 1
            and 0 < info.st_size <= MAX_FILE,
            "Salida dist enlazada, no regular o fuera del límite.",
        )
        with leaf.open("rb") as stream:
            require(os.path.samestat(info, os.fstat(stream.fileno())), "Salida dist sustituida.")
            raw = stream.read(info.st_size + 1)
        require(len(raw) == info.st_size, "Salida dist truncada o sustituida.")
        built[name] = raw
    return built


def repack(original, built):
    require(set(built) == DIST, "Se requieren las seis salidas de dist.")
    require(
        {path for path in original if path.startswith("package/dist/")}
        == {f"package/dist/{name}" for name in DIST},
        "Conjunto dist original inesperado.",
    )
    files = dict(original)
    replaced = {}
    for name, raw in sorted(built.items()):
        require(0 < len(raw) <= MAX_FILE, "Salida dist fuera del límite.")
        path = f"package/dist/{name}"
        before, mode = original[path]
        require(raw != before, "La salida sigue siendo la original.")
        if name.endswith(".wasm"):
            require(
                before.startswith(WASM_MAGIC) and SQLITE_OLD_ID in before,
                "No se reconoce el WASM original.",
            )
            require(
                raw.startswith(WASM_MAGIC)
                and SOURCE_ID + b"\0" in raw
                and b"3.54.1\0" in raw
                and SQLITE_OLD_ID not in raw,
                "El WASM nuevo carece de las marcas de SQLite 3.54.1.",
            )
        else:
            source = raw.decode("utf-8")
            paired = name.removesuffix(".mjs") + ".wasm"
            require(
                re.search(r"\bexport\s+default\b", source)
                and {f for f in DIST if f.endswith(".wasm") and f in source} == {paired},
                "El cargador no conserva el export default o su WASM asociado.",
            )
            if name == "wa-sqlite-jspi.mjs":
                require(
                    "WebAssembly.Suspending" in source and "WebAssembly.promising" in source,
                    "El cargador JSPI carece de las primitivas esperadas.",
                )
        files[path] = (raw, mode)
        replaced[name] = {"old_sha256": sha(before), "new_sha256": sha(raw), "bytes": len(raw)}
    package = tar_gz(files)
    return package, {
        "schema": 1,
        "upstream_commit": REV,
        "baseline_sha256": BASELINE_SHA,
        "sqlite_version_marker": "3.54.1",
        "sqlite_source_id_marker": SOURCE_ID.decode("ascii"),
        "package_sha256": sha(package),
        "package_integrity": sri(package),
        "package_bytes": len(package),
        "replaced": replaced,
        "preserved_members": len(original) - len(replaced),
        "browser_verified": False,
        "production_approved": False,
    }


def write_new(output, raw):
    require(output.name == OUTPUT, "Nombre de paquete derivado inesperado.")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--materials", required=True, type=Path)
    cli.add_argument("--dist", type=Path)
    cli.add_argument("--output", type=Path)
    args = cli.parse_args()
    if args.output is not None and args.dist is None:
        cli.error("--output requiere --dist.")
    original = read_baseline(args.materials)
    read_pinned(args.materials / "wa-sqlite-rebuild-3.54.1.tar.gz", 2749039, CONTEXT_SHA)
    if args.dist is None:
        receipt = {
            "schema": 1,
            "baseline_sha256": BASELINE_SHA,
            "baseline_members": len(original),
            "build_context_sha256": CONTEXT_SHA,
            "required_sdk_manifest": SDK_MANIFEST,
            "expected_dist": sorted(DIST),
            "package_name": OUTPUT,
            "wasm_available": False,
        }
    else:
        package, receipt = repack(original, read_dist(args.dist))
        if args.output is not None:
            write_new(args.output, package)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
