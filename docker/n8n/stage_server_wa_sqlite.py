"""Prepara wa-sqlite fijado y un contexto separado con SQLite corregido."""

import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import tarfile
import zipfile
from pathlib import Path

from prepare_runner_urllib3 import read_pinned, require, sha

REV = "779219540f66cecaa159da32b3b8936697ba10a7"
WA_SHA = "0f5c0cf77bf3b4ebc1e78e96bd468e5869e838671b20d8aa10f8a8bb2adda843"
WA_SRI = "sha512-uguFGpJLuBbbUsszSQG2gntXg3+W1JY+LfxvW5nYcS41n3OxHoNb3REwm4OW7S8g7H8WXAhQNM+4Mh56f70XeA=="
WA_URL = f"https://codeload.github.com/rhashimoto/wa-sqlite/tar.gz/{REV}"
WA_LEAF = "wa-sqlite-1.0.9-upstream-nogit.tgz"
SQLITE_SHA = "0a9c8e34352dad46006aa82836d5383bff9dc6bc9ce5a60553c05aa30e6ad1f9"
EXT_SHA3 = "ee39ddf5eaa21e1d0ebcbceeab42822dd0c4f82d8039ce173fd4814807faabfa"
SQLITE_OLD_ID = (
    b"2025-06-06 14:52:32 b77dc5e0f596d2140d9ac682b2893ff65d3a4140aa86067a3efebe29dc914c95"
)


def sri(raw):
    return "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode()


def safe(path):
    return (
        len(path.encode()) <= 220
        and "\\" not in path
        and not any(ord(c) < 32 or ord(c) == 127 for c in path)
        and all(
            p not in ("", ".", "..", ".git") and not p.startswith(".env") for p in path.split("/")
        )
    )


def tar_gz(files):
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=6) as stream:
        with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for name, (raw, mode) in sorted(files.items()):
                require(safe(name), "Ruta de salida fuera del contexto.")
                member = tarfile.TarInfo(name)
                member.mode = mode
                member.size = len(raw)
                tar.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def wa_files(path):
    raw = read_pinned(path, 3479831, WA_SHA)
    require(sri(raw) == WA_SRI, "Integridad wa-sqlite distinta del lock.")
    prefix = f"wa-sqlite-{REV}/"
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as tar:
        for member in tar:
            if member.isdir() or not member.name.startswith(prefix):
                continue
            name = member.name.removeprefix(prefix)
            if name not in {
                "package.json",
                "README.md",
                "LICENSE",
                "Makefile",
            } and not name.startswith(("dist/", "src/", "test/")):
                continue
            require(
                member.isfile()
                and 0 <= member.size <= 2 * 1024**2
                and name not in files
                and safe(name),
                "Miembro wa-sqlite no regular o duplicado.",
            )
            payload = tar.extractfile(member).read(member.size + 1)
            require(len(payload) == member.size, "Miembro wa-sqlite truncado.")
            files[name] = (payload, 0o644)
    meta = json.loads(files["package.json"][0])
    require(
        (meta["name"], meta["version"], meta["main"], meta["types"])
        == ("wa-sqlite", "1.0.9", "src/sqlite-api.js", "src/types/index.d.ts")
        and not (meta.keys() & {"dependencies", "optionalDependencies", "peerDependencies"})
        and not (
            meta["scripts"].keys() & {"prepare", "prepack", "install", "preinstall", "postinstall"}
        ),
        "Contrato npm wa-sqlite inesperado.",
    )
    return files


def baseline(files):
    roots = {
        "package.json",
        "LICENSE",
        "README.md",
        "src/sqlite-constants.js",
        "src/sqlite-api.js",
        "src/FacadeVFS.js",
        "src/VFS.js",
        "src/WebLocksMixin.js",
    }
    selected = {
        "package/" + name: row
        for name, row in files.items()
        if name in roots or name.startswith(("dist/", "src/types/", "src/examples/", "test/"))
    }
    required = {
        "src/types/index.d.ts",
        "src/examples/AccessHandlePoolVFS.js",
        "dist/wa-sqlite.mjs",
        "dist/wa-sqlite.wasm",
    } | roots
    require(
        {"package/" + name for name in required} <= selected.keys(),
        "Faltan entradas de UI wa-sqlite.",
    )
    wasm = {}
    for variant in ("wa-sqlite", "wa-sqlite-async", "wa-sqlite-jspi"):
        binary = selected[f"package/dist/{variant}.wasm"][0]
        require(
            binary.startswith(b"\0asm\1\0\0\0")
            and SQLITE_OLD_ID in binary
            and b"3.50.1\0" in binary,
            "SQLite del WASM original no acreditado.",
        )
        wasm[variant] = {"size": len(binary), "sha256": sha(binary)}
    return tar_gz(selected), wasm


def sqlite_context(files, amalgamation, extension):
    raw = read_pinned(amalgamation, 2990966, SQLITE_SHA)
    ext = extension.read_bytes()
    require(
        0 < len(ext) <= 512 * 1024 and hashlib.sha3_256(ext).hexdigest() == EXT_SHA3,
        "Extensión SQLite con SHA3-256 inesperado.",
    )
    result = {
        name: row
        for name, row in files.items()
        if name.startswith("src/") or name in {"LICENSE", "README.md", "Makefile", "package.json"}
    }
    result["deps/extension-functions.c"] = (ext, 0o644)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for leaf in ("sqlite3.c", "sqlite3.h", "sqlite3ext.h"):
            member = z.getinfo("sqlite-amalgamation-3540100/" + leaf)
            require(0 < member.file_size <= 12 * 1024**2, "Archivo SQLite fuera del límite.")
            result["deps/version-3.54.1/" + leaf] = (z.read(member), 0o644)
    header = result["deps/version-3.54.1/sqlite3.h"][0]
    require(
        re.search(rb'^#define SQLITE_VERSION +"3\.54\.1"$', header, re.M),
        "Cabecera SQLite de otra versión.",
    )
    listing = "".join(f"{sha(row[0])}  {name}\n" for name, row in sorted(result.items()))
    result["SHA256SUMS"] = (listing.encode(), 0o644)
    return tar_gz(result)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--materials", type=Path, required=True)
    p.add_argument("--sqlite", type=Path, required=True)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()
    files = wa_files(args.materials / "wa-sqlite-77921954-source.tar.gz")
    package, wasm = baseline(files)
    build = sqlite_context(files, args.sqlite, args.materials / "extension-functions.c")
    outputs = {WA_LEAF: package, "wa-sqlite-rebuild-3.54.1.tar.gz": build}
    receipt = {
        "schema": 1,
        "original_wasm": wasm,
        "original_sqlite": "3.50.1",
        "sqlite_source_candidate": "3.54.1",
        "sqlite_rebuilt": False,
        "production_approved": False,
        "outputs": {
            name: {"sha256": sha(raw), "integrity": sri(raw), "bytes": len(raw)}
            for name, raw in outputs.items()
        },
    }
    if args.write:
        outputs["wa-sqlite-materials-receipt.json"] = (
            json.dumps(receipt, sort_keys=True) + "\n"
        ).encode()
        require(
            not any(
                (args.materials / name).exists() or (args.materials / name).is_symlink()
                for name in outputs
            ),
            "Salida wa-sqlite ya existente.",
        )
        for name, raw in outputs.items():
            with (args.materials / name).open("xb") as stream:
                stream.write(raw)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
