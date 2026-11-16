"""Empaqueta las dos entradas WASM selladas y prepara una etapa Linux sin red."""

import argparse
import hashlib
import io
import json
import os
import socket
import stat
import tarfile
from pathlib import Path, PurePosixPath

SDK = "sha256:40a30b0e22be2b1fce570a5fcbf8124c4ccb22962b49850546863da07d374a51"
OUTPUT = "n8n-server-wasm-sdk-v1.tar.gz"
INPUTS = {
    "sqlite": {
        "root": "n8n-server-source-v1",
        "leaf": "wa-sqlite-rebuild-3.54.1.tar.gz",
        "sha256": "e9152893ca882710fa5e4c494a7a19356391b214bc1c331f7d9b7b259785d413",
        "bytes": 2_749_039,
        "files": 45,
        "expanded": 10_702_294,
        "manifest": "6cefd1eacb3f3c8fa6b202ed9ff5569c94130ece82d3e9f64eb7e496a93defb1",
        "work": "wa-sqlite",
    },
    "quickjs": {
        "root": "n8n-quickjs-wasm-inputs-v1",
        "leaf": "context-linux-x64.tar.gz",
        "sha256": "066d2e04ea3c5da743853e27a047febb932cc89d77ea3f5c08c798b151187c7d",
        "bytes": 6_946_122,
        "files": 212,
        "expanded": 21_836_261,
        "manifest": "f1dff1a53ab5b2efd14608705d29c08a3fa0c8f6ba32f99ab315139de0d38423",
        "work": "workspace",
    },
}
SQLITE_RECIPE = "build-wa-sqlite-wasm-offline.sh"
SQLITE_RECIPE_SHA = "f87eda4204e63be35c9219e5e3aa7df29a930774d26fa8972806a8283524a674"
QUICKJS_RECIPE = "build-quickjs-wasm-offline.sh"
QUICKJS_RECIPE_SHA = "499a6f9fc12d8d6af90078b72a5da9cb10b356a12a7ab214a07126520695d97d"


def need(ok, code):
    if not ok:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path, digest, limit, size=None):
    node = path.lstat()
    need(
        path.resolve(strict=True) == path
        and stat.S_ISREG(node.st_mode)
        and node.st_nlink == 1
        and 0 < node.st_size <= limit,
        "ENTRADA_TIPO",
    )
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    need(
        len(raw) == node.st_size and (size is None or len(raw) == size) and sha(raw) == digest,
        "ENTRADA_PIN",
    )
    return raw


def context(raw, desc):
    need(len(raw) == desc["bytes"] and sha(raw) == desc["sha256"], "CONTEXTO_PIN")
    files, count = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as archive:
        for row in archive:
            path = PurePosixPath(row.name)
            count += row.size
            need(
                row.type == tarfile.REGTYPE
                and row.name == path.as_posix()
                and not path.is_absolute()
                and "\\" not in row.name
                and ":" not in row.name
                and not any(p in {".", "..", ".git"} or p.startswith(".env") for p in path.parts)
                and row.name not in files
                and len(files) < desc["files"]
                and row.mode in {0o644, 0o755}
                and count <= desc["expanded"],
                "CONTEXTO_MIEMBRO",
            )
            with archive.extractfile(row) as stream:
                data = stream.read()
            need(len(data) == row.size, "CONTEXTO_TRUNCADO")
            files[row.name] = (data, row.mode)
    listing = files["SHA256SUMS"][0]
    need(
        len(files) == desc["files"]
        and count == desc["expanded"]
        and sha(listing) == desc["manifest"],
        "CONTEXTO_COBERTURA",
    )
    rows = [line.split("  ", 1) for line in listing.decode().splitlines()]
    need(
        len(rows) == len(files) - 1
        and {name: digest for digest, name in rows}
        == {name: sha(data) for name, (data, _) in files.items() if name != "SHA256SUMS"},
        "CONTEXTO_MANIFIESTO",
    )
    return files


def prepare():
    # El mismo escritor determinista que creó el contexto wa-sqlite.
    from stage_server_wa_sqlite import tar_gz

    here = Path(__file__).resolve().parent
    private = here.parents[1] / ".tools/backup-source-build"
    files = {}
    for name, row in INPUTS.items():
        raw = read(private / row["root"] / row["leaf"], row["sha256"], row["bytes"], row["bytes"])
        verified = context(raw, row)
        if name == "quickjs":
            need(sha(verified[QUICKJS_RECIPE][0]) == QUICKJS_RECIPE_SHA, "QUICKJS_RECETA")
        files[row["leaf"]] = (raw, 0o644)
    files[SQLITE_RECIPE] = (read(here / SQLITE_RECIPE, SQLITE_RECIPE_SHA, 32 * 1024), 0o644)
    files[Path(__file__).name] = (Path(__file__).read_bytes(), 0o644)
    manifest = {name: {"sha256": sha(raw), "bytes": len(raw)} for name, (raw, _) in files.items()}
    description = {
        "schema": 1,
        "subject": "n8n-2.46.6-wasm-build-inputs",
        "sdk_linux_amd64": SDK,
        "members": manifest,
        "wasm_built": False,
        "production_approved": False,
    }
    files["wasm-sdk-inputs.json"] = (
        (json.dumps(description, sort_keys=True) + "\n").encode(),
        0o644,
    )
    bundle = tar_gz(files)
    return bundle, {
        **description,
        "archive_bytes": len(bundle),
        "archive_sha256": sha(bundle),
        "archive_name": OUTPUT,
    }


def stage(name):
    need(
        os.geteuid() == os.getegid() == 1000
        and os.uname().sysname == "Linux"
        and os.uname().machine == "x86_64"
        and {name for _, name in socket.if_nameindex()} == {"lo"},
        "SDK_AISLADO",
    )
    inputs, parent = Path("/inputs"), Path("/work")
    node = parent.lstat()
    need(
        parent.resolve(strict=True) == parent
        and stat.S_ISDIR(node.st_mode)
        and node.st_uid == node.st_gid == 1000
        and stat.S_IMODE(node.st_mode) == 0o700,
        "WORK_IDENTIDAD",
    )
    need(
        inputs.resolve(strict=True) == inputs and os.statvfs(inputs).f_flag & os.ST_RDONLY,
        "INPUTS_RO",
    )
    emroot = Path("/emsdk/upstream/emscripten")
    need(
        not os.path.lexists(emroot / ".git")
        and (emroot / "emcc").is_file()
        and Path("/emsdk/.emscripten").is_file(),
        "SDK_LAYOUT",
    )
    row = INPUTS[name]
    raw = read(inputs / row["leaf"], row["sha256"], row["bytes"], row["bytes"])
    files = context(raw, row)
    dest = parent / row["work"]
    if name == "sqlite":
        recipe = inputs / SQLITE_RECIPE
        read(recipe, SQLITE_RECIPE_SHA, 32 * 1024)
    else:
        recipe = dest / QUICKJS_RECIPE
        need(sha(files[QUICKJS_RECIPE][0]) == QUICKJS_RECIPE_SHA, "QUICKJS_RECETA")
    os.umask(0o077)
    dest.mkdir(mode=0o700)
    for leaf, (data, mode) in sorted(files.items()):
        path = dest / leaf
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode & 0o700)
    os.execve(
        "/bin/sh", ["/bin/sh", str(recipe)], {"PATH": "/work/no-path", "LC_ALL": "C", "TZ": "UTC"}
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output", type=Path)
    mode.add_argument("--stage", choices=sorted(INPUTS))
    args = parser.parse_args()
    if args.stage:
        stage(args.stage)
        return
    bundle, receipt = prepare()
    if args.output:
        need(
            args.output.name == OUTPUT
            and args.output.parent.resolve(strict=True) == args.output.absolute().parent,
            "SALIDA_RUTA",
        )
        fd = os.open(
            args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(bundle)
            stream.flush()
            os.fsync(stream.fileno())
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
