"""Valida fuentes y construye un contexto offline de QuickJS como datos."""

import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import stat
import tarfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

import yaml

ACQUISITION = "eb3e5cfdefc5dbb78d95a7410f67c0f292d8e84162bc23b0958908a77de07d8d"
COMMIT = "df4efb9ef2cb25c417ecb57986da462d11b244ed"
TREE = "f353343a7637a8a132d278d74accb50e2e61aeba"
PATCH = "d019ef46398d2eedcfba892e272f14d6593bdad56f9fc72e6fb6cd410c440104"
C_ORIGINAL = "4f15a9e33819635474a9e43aa523f42ea3a6b452bfd8467223fcfbf6e447cc43"
C_DERIVED = "0302bbac5694bc1435ff59896a3ba51de7c286d07606e9d0ed9c57ba1a0c2c29"
VARIANTS = ("debug-asyncify", "debug-sync", "release-asyncify", "release-sync")
TOOLS = ("tsx", "fs-extra", "prettier")
MAX_FILE = 16 * 1024**2
MAX_EXPANDED = 48 * 1024**2


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def git_hash(raw, kind):
    return hashlib.sha1(f"{kind} {len(raw)}\0".encode() + raw).hexdigest()


def regular(path, expected):
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and 0 < info.st_size <= MAX_FILE
        and not getattr(info, "st_file_attributes", 0) & 0x400,
        "ARCHIVO_ENTRADA",
    )
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    require(len(raw) == info.st_size and sha(raw) == expected, "SHA_ENTRADA")
    return raw


def safe_path(path):
    parts = path.split("/")
    require(
        re.fullmatch(r"[A-Za-z0-9_./@+ -]{1,240}", path)
        and all(part not in ("", ".", "..", ".git") for part in parts),
        "RUTA",
    )
    return PurePosixPath(path)


def authenticated_tree(document):
    require(document["sha"] == TREE and document["truncated"] is False, "ARBOL_IDENTIDAD")
    entries = {}
    children = defaultdict(list)
    for row in document["tree"]:
        path = safe_path(row["path"])
        kind, mode = row["type"], row["mode"]
        require(
            path not in entries
            and (kind, mode) in {("blob", "100644"), ("blob", "100755"), ("tree", "040000")}
            and re.fullmatch(r"[0-9a-f]{40}", row["sha"]),
            "ARBOL_ENTRADA",
        )
        entries[path] = row
        children[path.parent].append((path, row))
    require(len(entries) == 789, "ARBOL_COBERTURA")
    for directory, rows in children.items():
        expected = TREE if directory == PurePosixPath(".") else entries[directory]["sha"]
        ordered = sorted(
            rows, key=lambda pair: pair[0].name + ("/" if pair[1]["type"] == "tree" else "")
        )
        raw = b"".join(
            f"{row['mode'].lstrip('0')} {path.name}\0".encode() + bytes.fromhex(row["sha"])
            for path, row in ordered
        )
        require(git_hash(raw, "tree") == expected, "ARBOL_SHA")
    require(
        set(children)
        == {PurePosixPath(".")} | {p for p, r in entries.items() if r["type"] == "tree"},
        "ARBOL_DIRECTORIOS",
    )
    return entries


def archive_files(raw, prefix):
    files, folders = {}, set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            path = safe_path(member.name)
            require(path.parts[0] == prefix and len(files) + len(folders) < 4096, "TAR_RAIZ")
            rel = PurePosixPath(*path.parts[1:])
            require(rel not in files and rel not in folders, "TAR_DUPLICADO")
            if member.isdir():
                require(member.size == 0, "TAR_DIRECTORIO")
                folders.add(rel)
                continue
            require(
                member.isfile() and len(path.parts) > 1 and 0 <= member.size <= MAX_FILE,
                "TAR_NODO",
            )
            total += member.size
            require(total <= MAX_EXPANDED, "TAR_TAMANO")
            body = archive.extractfile(member).read(member.size + 1)
            require(len(body) == member.size, "TAR_TRUNCADO")
            files[rel] = (body, bool(member.mode & 0o111))
    require(
        not any(parent in files for path in files.keys() | folders for parent in path.parents),
        "TAR_PADRE",
    )
    return files, folders


def apply_fix(original, patch):
    require(sha(original) == C_ORIGINAL and sha(patch) == PATCH, "PARCHE_ENTRADAS")
    hunks = re.split(rb"^@@ [^\n]+\n", patch, flags=re.MULTILINE)
    require(len(hunks) == 4 and hunks[0] == b"", "PARCHE_HUNKS")
    result = original
    for hunk in hunks[1:]:
        lines = hunk.rstrip(b"\n").split(b"\n")
        require(all(line.startswith((b" ", b"+", b"-")) for line in lines), "PARCHE_LINEA")
        before = b"\n".join(line[1:] for line in lines if not line.startswith(b"+"))
        after = b"\n".join(line[1:] for line in lines if not line.startswith(b"-"))
        require(result.count(before) == 1, "PARCHE_CONTEXTO")
        result = result.replace(before, after, 1)
    require(sha(result) == C_DERIVED, "PARCHE_SALIDA")
    return result


def linux(name):
    return name != "fsevents" and (not name.startswith("@esbuild/") or name == "@esbuild/linux-x64")


def locked_tools(package, lock):
    selectors = {selector: row for name, row in lock.items() for selector in name.split(", ")}
    pending = [(name, "npm:" + package["devDependencies"][name]) for name in TOOLS]
    result = {}
    for name, wanted in pending:
        row = selectors[f"{name}@{wanted}"]
        require(row["resolution"] == f"{name}@npm:{row['version']}", "YARN_RESOLUCION")
        if name in result:
            require(result[name] == row, "YARN_VARIAS_VERSIONES")
            continue
        result[name] = row
        pending.extend(
            (dep, spec) for dep, spec in row.get("dependencies", {}).items() if linux(dep)
        )
    require(len(result) == 10, "YARN_CLAUSURA")
    return result


def acquired_file(root, record):
    leaf = safe_path(record["file"])
    require(len(leaf.parts) == 1, "RECIBO_RUTA")
    raw = regular(root / leaf, record["sha256"])
    require(len(raw) == record["size"], "RECIBO_TAMANO")
    return raw


def npm_payload(root, receipts, locked):
    files = {}
    require(
        len(receipts) == len(locked) and {r["name"] for r in receipts} == locked.keys(),
        "NPM_CLAUSURA",
    )
    for record in receipts:
        name, version = record["name"], record["version"]
        meta = json.loads(acquired_file(root, record["metadata"]))
        raw = acquired_file(root, record["tarball"])
        require(
            (meta["name"], meta["version"]) == (name, version)
            and version == locked[name]["version"]
            and meta["dist"]["integrity"] == record["integrity"]
            and record["integrity"]
            == "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode()
            and meta["dist"]["shasum"] == record["shasum"] == hashlib.sha1(raw).hexdigest(),
            "NPM_IDENTIDAD_SRI",
        )
        package_files, _ = archive_files(raw, "package")
        package = json.loads(package_files[PurePosixPath("package.json")][0])
        require((package["name"], package["version"]) == (name, version), "NPM_MANIFEST")
        for doc in (meta, package):
            deps = doc.get("dependencies", {}) | doc.get("optionalDependencies", {})
            require(
                {k: "npm:" + v for k, v in deps.items() if linux(k)}
                == {k: v for k, v in locked[name].get("dependencies", {}).items() if linux(k)},
                "NPM_ARISTAS",
            )
        files.update(
            {
                PurePosixPath("node_modules", name) / key: value
                for key, value in package_files.items()
            }
        )
    return files


def inputs(root):
    receipt = json.loads(regular(root / "acquisition.json", ACQUISITION))
    tree = authenticated_tree(json.loads(acquired_file(root, receipt["tree"])))
    source, folders = archive_files(
        acquired_file(root, receipt["source"]), "quickjs-emscripten-" + COMMIT
    )
    require(
        source.keys() == {p for p, row in tree.items() if row["type"] == "blob"}
        and folders - {PurePosixPath(".")}
        == {p for p, row in tree.items() if row["type"] == "tree"},
        "FUENTE_COBERTURA",
    )
    for path, (body, executable) in source.items():
        row = tree[path]
        require(
            len(body) == row["size"]
            and executable == (row["mode"] == "100755")
            and git_hash(body, "blob") == row["sha"],
            "FUENTE_BLOB",
        )
    package = json.loads(source[PurePosixPath("package.json")][0])
    lock = yaml.safe_load(source[PurePosixPath("yarn.lock")][0])
    locked = locked_tools(package, lock)
    files = npm_payload(root, receipt["npm"], locked)
    chosen = {
        "LICENSE",
        "package.json",
        "tsconfig.json",
        "yarn.lock",
        "c/interface.c",
        "scripts/generate.ts",
        "scripts/helpers.ts",
        "scripts/emcc.sh",
        "exportedRuntimeMethods.json",
        "exportedRuntimeMethods.asyncify.json",
        "packages/quickjs-ffi-types/src/ffi-types.ts",
        "vendor/quickjs/VERSION",
        "vendor/quickjs/LICENSE",
        *(
            f"packages/variant-quickjs-wasmfile-{variant}/{name}"
            for variant in VARIANTS
            for name in ("Makefile", "package.json")
        ),
    }
    chosen.update(str(p) for p in source if p.parts[0] == "templates")
    chosen.update(
        str(p)
        for p in source
        if p.parts[:2] == ("vendor", "quickjs") and len(p.parts) == 3 and p.suffix in (".c", ".h")
    )
    files.update({PurePosixPath(path): source[PurePosixPath(path)] for path in chosen})
    patch = regular(root / "bellard-841dd.patch", PATCH)
    key = PurePosixPath("vendor/quickjs/quickjs.c")
    files[key] = (apply_fix(files[key][0], patch), False)
    return files, len(source), len(locked)


def context(files):
    recipe = Path(__file__).with_name("build-quickjs-wasm-offline.sh").read_bytes()
    files[PurePosixPath("build-quickjs-wasm-offline.sh")] = (recipe, True)
    listing = "".join(f"{sha(raw)}  {path}\n" for path, (raw, _) in sorted(files.items()))
    files[PurePosixPath("SHA256SUMS")] = (listing.encode(), False)
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0, filename="") as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path, (raw, executable) in sorted(files.items()):
                entry = tarfile.TarInfo(path.as_posix())
                entry.size = len(raw)
                entry.mode = 0o755 if executable else 0o644
                archive.addfile(entry, io.BytesIO(raw))
    return stream.getvalue(), sha(listing.encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materials", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Archivo TAR.GZ nuevo")
    args = parser.parse_args()
    files, source_count, tool_count = inputs(args.materials)
    raw, contents_sha = context(files)
    if args.output is not None:
        with args.output.open("xb") as stream:
            stream.write(raw)
    print(
        json.dumps(
            {
                "source_files_verified": source_count,
                "tools": tool_count,
                "context_files": len(files),
                "context_bytes": len(raw),
                "context_sha256": sha(raw),
                "contents_sha256": contents_sha,
                "written": args.output is not None,
                "wasm_compiled": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
