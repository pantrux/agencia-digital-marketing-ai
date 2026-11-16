"""Verifica el aislamiento y ejecuta únicamente la receta wa-sqlite."""

import hashlib
import json
import os
import socket
import stat
import sys
from pathlib import Path

ROOT = Path("/opt/wa-sqlite")
INPUTS = Path("/inputs")
WORK = Path("/work")
EMSCRIPTEN = Path("/emsdk/upstream/emscripten")
SOURCE = "57f04b592c1facb3cfc9babe4911bf33025bc0bfba96136a608fd81bf9708470"
NODE = "c9f0030334c1d8f0d84e57ce49ce5afe1381126b14065f279e7e369248e04bb0"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def inspect_no_git(root):
    require(root.resolve(strict=True) == root and root.is_dir(), "EMSCRIPTEN_RUTA")
    count = 0

    def failed(error):
        raise error

    for _, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        count += len(dirs) + len(files)
        require(count <= 10_000 and ".git" not in dirs and ".git" not in files, "EMSCRIPTEN_GIT")
    require(count > 0, "EMSCRIPTEN_VACIO")
    return count


def check_environment():
    require(
        os.geteuid() == os.getegid() == 1000
        and os.uname().sysname == "Linux"
        and os.uname().machine == "x86_64"
        and {name for _, name in socket.if_nameindex()} == {"lo"},
        "AISLAMIENTO_SDK",
    )
    node = WORK.lstat()
    require(
        WORK.resolve(strict=True) == WORK
        and stat.S_ISDIR(node.st_mode)
        and node.st_uid == node.st_gid == 1000
        and stat.S_IMODE(node.st_mode) == 0o700
        and not any(WORK.iterdir())
        and INPUTS.resolve(strict=True) == INPUTS
        and all(os.statvfs(path).f_flag & os.ST_RDONLY for path in (Path("/"), INPUTS))
        and not (os.statvfs(WORK).f_flag & os.ST_RDONLY),
        "MONTAJES_SDK",
    )


def main():
    require(len(sys.argv) == 1, "ARGUMENTOS_SDK")
    check_environment()
    count = inspect_no_git(EMSCRIPTEN)
    with Path("/usr/bin/node").open("rb") as source:
        require(hashlib.file_digest(source, "sha256").hexdigest() == NODE, "NODE_PIN")
    raw = (ROOT / "source-stage.py").read_bytes()
    require(hashlib.sha256(raw).hexdigest() == SOURCE, "STAGE_PIN")
    original = {"__name__": "wa_sqlite_stage_original"}
    exec(compile(raw, str(ROOT / "source-stage.py"), "exec"), original)
    desc = original["INPUTS"]["sqlite"]
    raw = original["read"](INPUTS / desc["leaf"], desc["sha256"], desc["bytes"], desc["bytes"])
    files = original["context"](raw, desc)
    os.umask(0o077)
    target = WORK / desc["work"]
    target.mkdir(mode=0o700)
    for leaf, (data, mode) in sorted(files.items()):
        path = target / leaf
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode & 0o700)
    with (WORK / ".sdk-preflight.json").open("x", encoding="ascii") as stream:
        json.dump(
            {
                "schema": 1,
                "stage": "sqlite",
                "git_entries": 0,
                "walked_entries": count,
                "network": ["lo"],
                "uid": 1000,
                "gid": 1000,
                "root_read_only": True,
                "inputs_read_only": True,
                "source_sha256": desc["sha256"],
                "node_sha256": NODE,
            },
            stream,
            sort_keys=True,
        )
        stream.write("\n")
    os.execve(
        "/bin/sh",
        ["/bin/sh", str(ROOT / "build-wa-sqlite-wasm-offline.sh")],
        {"PATH": "/work/no-path", "LC_ALL": "C", "TZ": "UTC"},
    )


if __name__ == "__main__":
    main()
