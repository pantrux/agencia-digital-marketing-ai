"""Prepara contextos separados para los runners de n8n a partir de entradas fijadas."""

import argparse
import gzip
import hashlib
import io
import json
import os
import posixpath
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BACKUP = REPO / ".tools/backup-source-build"
INPUTS = REPO / ".tools/n8n-remediation"
JS = BACKUP / "n8n-runner-javascript-linux-20261113-v1/linux-runner-quickjs-v1.tar.gz"
JS_SHA = "24d1f3e8cb8531c422250a52883664608b7c97966fb21f5387d6c34de903154e"
PYTHON = INPUTS / "runner-python-build-context-v1.tar.gz"
PYTHON_SHA = "51c87a724c65b9c53f9d821680dbe7fdff0944b316e974f76739debcfb7aaa47"
SERVER = (
    BACKUP
    / "n8n-server-source-v1/markdown-it-14.3.3-remediation-v1/linux-production-tree-markdown-native-quickjs-v5.tar.gz"
)
SERVER_SHA = "8deff584486ac2c633ea196e61cff228867e5178dd244b52c5ebbd3bb4e5bf93"
LAUNCHER = BACKUP / "n8n-launcher-crossbuild-v1/task-runner-launcher"
LAUNCHER_SHA = "65ec6a93ea72e692326a9d34146aaf273c7929db4b7d285ca5b1e2b14a9913ec"
CONFIG = INPUTS / "runner-python-build-v1/n8n-task-runners.json"
CONFIG_SHA = "968482d51c077adb6fd261c0932efc9732cb13ac04eb6aa35147cca0f2371365"
QUICKJS = BACKUP / "n8n-quickjs-wasm-inputs-v1/quickjs-linux-results-20261113-v8.tar"
QUICKJS_SHA = "05acec0c24c34c6001244aff9df28239b9376e1a78ff28ff28b5652fe4d16f2e"
LICENSES = {
    "LICENSE.md": "ce4c9345171f6c32a3fdc8f04f53f6c949daf604ea94d772a22891e4848119ef",
    "LICENSE_EE.md": "6110c69fd3b92928328a89e863dc2c862ea449f2335c352a9c4a137a36b13040",
}
PATCH = {
    "flatted-async.js": (
        "c3b9a667bbdf61834ff813d734e62d5fc16a46b72131f597a5decb3945d2624b",
        "a67a0fb6f31f98356540a7c7bd8f0a77109202399c9f9ff09a3486597bc01888",
    ),
    "flatted-async.js.map": (
        "be802de72fcc65809f09af2875ab9cedc1dacf427d1f8b20fefd7a7710e19c3d",
        "431163a1c9ff03c67a188e8050e594bbc8a099f976f85a4e2b9e2c37c3d44e0c",
    ),
    "parse-flatted.js": (
        "732daf05ee0c3310fb958967d6691cdaea5068f69f2cf420ad719b656387be8a",
        "732daf05ee0c3310fb958967d6691cdaea5068f69f2cf420ad719b656387be8a",
    ),
}
PREFIX = "task-runner-javascript"
PATCH_DIRECTORY = "/node_modules/@n8n/backend-common/dist/utils/"
MIB = 1024**2


def need(condition, code):
    if not condition:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    need(path.is_file() and not path.is_symlink(), path.name)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pinned(path, digest):
    need(file_sha(path) == digest, path.name)
    return path


def safe(name):
    path = PurePosixPath(name)
    return (
        bool(name)
        and path.as_posix() == name
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
    )


def read(archive, row, size=MIB):
    need(row.isfile() and row.sparse is None and row.size <= size, row.name)
    return archive.extractfile(row).read()


def replacements():
    files = {}
    with tarfile.open(pinned(SERVER, SERVER_SHA), "r|gz") as archive:
        for row in archive:
            path, marker, leaf = row.name.rpartition(PATCH_DIRECTORY)
            if marker and leaf in PATCH:
                need(
                    path.startswith("compiled/node_modules/.pnpm/@n8n+backend-common@file+")
                    and leaf not in files,
                    row.name,
                )
                data = read(archive, row)
                need(sha(data) == PATCH[leaf][1], row.name)
                files[leaf] = data
                if files.keys() == PATCH.keys():
                    break
    need(files.keys() == PATCH.keys(), "Cobertura Flatted")
    return files


def pack_javascript(destination):
    files = replacements()
    all_names, patched, links = set(), set(), {}
    source = pinned(JS, JS_SHA)
    with (
        destination.open("xb") as stream,
        gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0, compresslevel=3) as zipped,
    ):
        with (
            tarfile.open(source, "r|gz") as archive,
            tarfile.open(fileobj=zipped, mode="w|") as output,
        ):
            for row in archive:
                name = row.name
                need(
                    safe(name)
                    and (name == PREFIX or name.startswith(PREFIX + "/"))
                    and name not in all_names,
                    name,
                )
                need(row.isfile() or row.isdir() or row.issym(), name)
                all_names.add(name)
                data = archive.extractfile(row) if row.isfile() else None
                if row.issym():
                    links[name] = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), row.linkname)
                    )
                else:
                    row.mode = 0o755 if row.isdir() or row.mode & 0o111 else 0o644
                path, marker, leaf = name.rpartition(PATCH_DIRECTORY)
                if marker and leaf in files:
                    need(
                        path.startswith(PREFIX + "/node_modules/.pnpm/@n8n+backend-common@file+")
                        and leaf not in patched,
                        name,
                    )
                    need(sha(read(archive, row)) == PATCH[leaf][0], name)
                    data = io.BytesIO(files[leaf])
                    row.size = len(files[leaf])
                    row.pax_headers.pop("size", None)
                    patched.add(leaf)
                row.uid = row.gid = 0
                row.uname = row.gname = "root"
                for field in ("uid", "gid", "uname", "gname", "mode"):
                    row.pax_headers.pop(field, None)
                output.addfile(row, data)
    need(
        len(all_names) == 42683 and len(links) == 1571 and set(links.values()) <= all_names,
        "Cobertura del runner JavaScript",
    )
    need(
        patched == files.keys() and PREFIX + "/dist/start.js" in all_names,
        "Entradas del runner JavaScript",
    )
    return {
        "files": len(all_names),
        "links": len(links),
        "flatted": {name: sha(raw) for name, raw in files.items()},
    }


def python_files(root):
    with tarfile.open(pinned(PYTHON, PYTHON_SHA), "r:gz") as archive:
        members = archive.getmembers()
        files = {row.name: read(archive, row, 3 * MIB) for row in members if safe(row.name)}
    need(len(members) == len(files) == 58, "Contexto Python")
    manifest = dict(
        line.split("  ", 1)[::-1] for line in files.pop("SHA256SUMS").decode().splitlines()
    )
    need(
        len(manifest) == 57 and {name: sha(data) for name, data in files.items()} == manifest,
        "Manifiesto Python",
    )
    need(
        files.pop("n8n-task-runners.json") == pinned(CONFIG, CONFIG_SHA).read_bytes(),
        "Config Python",
    )
    files.pop("Dockerfile")
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return {"inputs": len(files), "payload_rebuilt": False}


def common(root, language, launcher, launcher_sha):
    shutil.copyfile(pinned(launcher, launcher_sha), root / "task-runner-launcher")
    shutil.copyfile(pinned(CONFIG, CONFIG_SHA), root / "n8n-task-runners.json")
    license_root = root / "launcher-license"
    license_root.mkdir()
    for name, digest in LICENSES.items():
        source = INPUTS / "runner-launcher-build-v1/source" / name
        shutil.copyfile(pinned(source, digest), license_root / name)
    shutil.copyfile(HERE / f"Dockerfile.runner-{language}-runtime", root / "Dockerfile")


def prepare(language, destination, launcher, launcher_sha):
    need(not destination.exists() and destination.parent.is_dir(), "Destino nuevo requerido")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        root = Path(temporary) / "context"
        root.mkdir()
        common(root, language, launcher, launcher_sha)
        if language == "javascript":
            payload = pack_javascript(root / "runner-javascript.tar.gz")
            verify = root / "verify"
            verify.mkdir()
            for name in ("verify-runner-javascript-runtime.cjs", "verify-quickjs-wasm-sdk.mjs"):
                shutil.copyfile(HERE / name, verify / name)
            with tarfile.open(pinned(QUICKJS, QUICKJS_SHA), "r:") as archive:
                (verify / "quickjs-build.json").write_bytes(
                    read(archive, archive.getmember("quickjs-build.json"))
                )
        else:
            payload = python_files(root)
        members = {
            path.relative_to(root).as_posix(): file_sha(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }
        manifest = "".join(f"{digest}  {name}\n" for name, digest in members.items()).encode()
        (root / "SHA256SUMS").write_bytes(manifest)
        receipt = {
            "runner": language,
            "launcher_sha256": launcher_sha,
            "config_sha256": CONFIG_SHA,
            "context_manifest_sha256": sha(manifest),
            "members": len(members),
            "payload": payload,
            "production_approved": False,
            "success": True,
        }
        (root / "context.json").write_text(
            json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.rename(root, destination)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("javascript", "python"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, default=LAUNCHER)
    parser.add_argument("--launcher-sha256", default=LAUNCHER_SHA)
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(args.language, args.output, args.launcher, args.launcher_sha256), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
