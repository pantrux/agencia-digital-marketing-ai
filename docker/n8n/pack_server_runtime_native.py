"""Project the sealed Linux n8n tree and append its approved native files."""

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import posixpath
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath

ARCHIVE = "ebfb9802515222afbdb8fdad0d89c2f66948181fbd55b930dca667ac15c40658"
MEMBERS = 242_860
ROOT = "compiled/"
STORE = ROOT + "node_modules/.pnpm/"
SCOPE = ROOT + "node_modules/@napi-rs/"
SQLITE = (
    STORE + "sqlite3@5.1.7_bluebird@3.7.2_supports-color@8.1.1/node_modules/sqlite3/"
    "build/Release/node_sqlite3.node"
)
SQLITE_PIN = "c6bb7401f3743bb4fcb21db3ad8162c51839b3e4daf6bd3a2c5c3e251cd89d83"
AGENT = STORE + "agent-browser@0.26.0/node_modules/agent-browser/bin/agent-browser-"
AGENT_PIN = "6adf44364db0de3186c562328318ddc21ab388bf19380015c938c418608fe7d3"
AGENT_ARM64_PIN = "fca20648bc64491f3a0a0f5eaac7d6f6017d547f9cdff9bf6307905eeb2f5426"
NATIVE = {
    "canvas": (
        "1.0.11",
        "a8777e81afdd0ec855ed6df9200e62674386fdbdebadc909fc949897f319dfca",
        "skia.linux-x64-musl.node",
        "aaffa1cf32d5c77783c39aa918c640d8e85f51a0d2e2455bc449321d7c91590e",
    ),
    "image": (
        "1.15.0",
        "b345757a26e4dbf8863ceb4092b01bc155e742bad482a52a9a3c98e49ba271a2",
        "image.linux-x64-musl.node",
        "e8dac9890cd707a2422c16c04dcedf49c3539c3a70f75d9e7142b193b364c00b",
    ),
}
PHANTOM = (
    "*/resolve/*/test",
    "*/import-in-the-middle/*/test",
    "*/github-from-package/*/example",
    "*/tedious/*/benchmarks",
)
METADATA = {
    ROOT + "pnpm-lock.yaml",
    ROOT + "pnpm-workspace.yaml",
    ROOT + "node_modules/.pnpm-workspace-state-v1.json",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_pinned(path, expected, limit):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= limit, path.name)
    raw = path.read_bytes()
    require(len(raw) <= limit and sha(raw) == expected, path.name)
    return raw


def native_files(name, path):
    version, pin, binary, binary_pin = NATIVE[name]
    raw = read_pinned(path, pin, 40 * 1024**2)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        entries = archive.getmembers()
        expected = {"package.json", "README.md", binary}
        if name == "canvas":
            expected.add("sbom.cdx.json")
        require({row.name for row in entries} == {"package/" + leaf for leaf in expected}, name)
        require(len(entries) == len(expected) and all(row.isfile() for row in entries), name)
        files = {row.name.split("/", 1)[1]: archive.extractfile(row).read() for row in entries}
    manifest = json.loads(files["package.json"])
    require(manifest["name"] == f"@napi-rs/{name}-linux-x64-musl", name)
    require(manifest["version"] == version and sha(files[binary]) == binary_pin, name)
    return files


def remove_reason(path, phantom_dirs):
    if path in METADATA:
        return "pnpm_metadata"
    if path.endswith((".d.ts", ".d.ts.map")):
        return "typescript_declarations"
    if not path.startswith(STORE):
        return None
    package = path[len(STORE) :].split("/", 1)[0]
    if (
        path.endswith(".map")
        and not path.endswith(".wasm.map")
        and not ("@file+" in package and "packages+" in package)
    ):
        return "third_party_source_maps"
    if path.startswith(AGENT) and path.removeprefix(AGENT) in {
        "darwin-arm64",
        "darwin-x64",
        "win32-x64.exe",
        "linux-x64",
        "linux-arm64",
    }:
        return "agent_browser_platforms"
    if path in phantom_dirs or any(
        str(parent) in phantom_dirs for parent in PurePosixPath(path).parents
    ):
        return "third_party_examples"
    return None


def add(output, name, data=b"", *, mode=0o644, kind=tarfile.REGTYPE, link=""):
    member = tarfile.TarInfo(name)
    member.type, member.mode, member.linkname, member.size = kind, mode, link, len(data)
    output.addfile(member, io.BytesIO(data))


def runtime_mode(member):
    if member.isdir():
        return 0o755
    if member.isfile():
        return 0o755 if member.mode & 0o111 else 0o644
    return member.mode


def project(archive, output, native, sqlite):
    retained, links, seen, phantom_dirs = set(), {}, set(), set()
    removed, removed_bytes, normalized_modes = Counter(), Counter(), Counter()
    binding_paths = {SCOPE + f"{name}-linux-x64-musl" for name in NATIVE}
    essentials = {
        ROOT + "bin/n8n",
        ROOT + "dist/agent-library-bundle.js",
        ROOT + "dist/commands/start.js",
        ROOT + "dist/public-api/v1/openapi.yml",
        SQLITE.split("/build/Release/")[0] + "/package.json",
        AGENT + "linux-musl-x64",
        AGENT + "linux-musl-arm64",
        AGENT.removesuffix("-") + ".js",
    }
    for member in archive:
        path = member.name
        normalized = PurePosixPath(path)
        require(
            (path == "compiled" or path.startswith(ROOT))
            and normalized.as_posix() == path
            and ".." not in normalized.parts
            and "\\" not in path
            and path not in seen
            and (member.isfile() or member.isdir() or member.issym()),
            f"Unexpected source member: {path}",
        )
        require(
            path != SQLITE and path not in binding_paths, f"Native input already present: {path}"
        )
        seen.add(path)
        if (
            member.isdir()
            and path.startswith(STORE)
            and any(fnmatch.fnmatchcase(path, pat) for pat in PHANTOM)
        ):
            phantom_dirs.add(path)
        reason = remove_reason(path, phantom_dirs)
        if reason:
            removed[reason] += 1
            removed_bytes[reason] += member.size
            continue
        if member.issym():
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), member.linkname))
            require(target.startswith(ROOT), f"Link outside source: {path}")
            links[path] = target
        retained.add(path)
        agent_binary = path in (AGENT + "linux-musl-x64", AGENT + "linux-musl-arm64")
        stream = (
            archive.extractfile(member) if member.isfile() and (output or agent_binary) else None
        )
        if agent_binary:
            data = stream.read()
            expected = AGENT_PIN if path.endswith("x64") else AGENT_ARM64_PIN
            require(sha(data) == expected, "agent-browser pin")
            member.mode = 0o755
            stream = io.BytesIO(data)
        mode = runtime_mode(member)
        if mode != member.mode:
            normalized_modes[f"{member.mode:04o}->{mode:04o}"] += 1
            member.mode = mode
        if output:
            output.addfile(member, stream)
    require(len(seen) == MEMBERS and essentials <= retained, "Source archive coverage")
    require(all(target in retained for target in links.values()), "Retained symbolic links")
    require(sum(path.endswith(".wasm.map") for path in retained) == 2, "QuickJS debug maps")
    require(any("/dist/node-definitions/" in path for path in retained), "AI node definitions")
    require(any("/instance-ai/skills/" in path for path in retained), "AI skills")
    require(any("/instance-ai/knowledge-base/" in path for path in retained), "AI knowledge base")
    require(any("/n8n-editor-ui/dist/index.html" in path for path in retained), "Editor asset")
    if output:
        if SCOPE.rstrip("/") not in retained:
            add(output, SCOPE.rstrip("/"), mode=0o755, kind=tarfile.DIRTYPE)
        for name, files in native.items():
            directory = SCOPE + f"{name}-linux-x64-musl"
            add(output, directory, mode=0o755, kind=tarfile.DIRTYPE)
            for leaf, data in sorted(files.items()):
                add(
                    output,
                    directory + "/" + leaf,
                    data,
                    mode=0o755 if leaf.endswith(".node") else 0o644,
                )
        for directory in (SQLITE.rsplit("/", 2)[0], SQLITE.rsplit("/", 1)[0]):
            if directory not in retained:
                add(output, directory, mode=0o755, kind=tarfile.DIRTYPE)
        add(output, SQLITE, sqlite, mode=0o755)
        for arch in ("arm64", "x64"):
            add(
                output,
                AGENT + "linux-" + arch,
                kind=tarfile.LNKTYPE,
                link=AGENT + "linux-musl-" + arch,
            )
    return {
        "source_members": len(seen),
        "retained_symlinks": len(links),
        "removed": removed,
        "removed_bytes": removed_bytes,
        "normalized_modes": normalized_modes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "canvas", "image"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--sqlite", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    require(bool(args.sqlite) == bool(args.output), "SQLite and output are required together")
    native = {name: native_files(name, getattr(args, name)) for name in NATIVE}
    sqlite = read_pinned(args.sqlite, SQLITE_PIN, 2_130_712) if args.sqlite else None
    with args.source.open("rb") as source:
        require(hashlib.file_digest(source, "sha256").hexdigest() == ARCHIVE, "Source archive pin")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r|gz") as archive:
            if args.output:
                with args.output.open("xb") as dest:
                    try:
                        with gzip.GzipFile(
                            filename="", fileobj=dest, mode="wb", mtime=0, compresslevel=3
                        ) as compressor:
                            with tarfile.open(fileobj=compressor, mode="w|") as output:
                                result = project(archive, output, native, sqlite)
                    except BaseException:
                        dest.close()
                        args.output.unlink()
                        raise
            else:
                result = project(archive, None, native, None)
    result.update(source_sha256=ARCHIVE, output_written=bool(args.output))
    if args.output:
        with args.output.open("rb") as stream:
            result["output_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
