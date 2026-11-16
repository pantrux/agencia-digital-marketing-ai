"""Repite sólo la instalación congelada del baseline sobre el almacén original RO."""

import json
import os
import platform
import re
import socket
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed_server_linux_baseline as base  # noqa: E402

OWNER = "agencia-digital-marketing-ai:n8n-server-baseline-replay:v2"
INDEX = {
    "sha256": "9f134672f2ec03d43bfd7809e837b83154ad4f1c2ccc4e2410d5aa35a3c42d14",
    "bytes": 49_029_120,
}
KEYS = "6c9131b3dd533604432ea361b49cae43385216e16812adb0d1d3c8d3719c3123"
STORE = Path("/store")


def verify_store(op, store):
    base.need(store.resolve(strict=True) == store, "STORE_RUTA")
    version = store / "v11"
    for path in (store, version, version / "files", version / "index.db"):
        base.need(path.resolve(strict=True) == path, "STORE_RUTA")
        base.need(os.statvfs(path).f_flag & os.ST_RDONLY, "STORE_DEBE_ESTAR_RO")
    # pnpm 12 nativo identifica también los file: por SRI e ID en SQLite.
    base.need(
        {path.name for path in store.iterdir()} == {"v11"}
        and {path.name for path in version.iterdir()} == {"files", "index.db"},
        "STORE_ARBOL_FISICO",
    )
    keys, actual = base.store_keys(op, store)
    base.need(actual == INDEX and len(keys) == 3125 and base.VENDOR_KEY in keys, "STORE_INDICE")
    base.need(op.sha(("\n".join(sorted(keys)) + "\n").encode()) == KEYS, "STORE_CATALOGO")
    return actual


def parse_log(raw):
    verified = re.findall(
        rb"(?m)^\xe2\x9c\x93 Lockfile passes supply-chain policies \((\d+) entries in [^\r\n]+\)\r?$",
        raw,
    )
    progress = re.findall(
        rb"(?m)^Progress: resolved (\d+), reused (\d+), downloaded (\d+), added (\d+), done\r?$",
        raw,
    )
    base.need(verified == [b"3476"] and len(progress) == 1, "REPLAY_EVIDENCIA")
    resolved, reused, downloaded, added = map(int, progress[0])
    base.need(
        resolved == added == 3164 and 0 < reused <= resolved and downloaded == 0, "REPLAY_PROGRESO"
    )
    return {
        "policy_entries": 3476,
        "resolved": resolved,
        "reused": reused,
        "downloaded": downloaded,
        "added": added,
    }


def command(tool, work):
    return [
        "--config.package-import-method=copy"
        if flag == "--config.package-import-method=hardlink"
        else flag
        for flag in base.command(tool, work, STORE, base.REGISTRY, True)
    ]


def replay(op, inputs):
    base.need(
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and os.geteuid() == os.getegid() == 1000
        and Path("/lib/ld-musl-x86_64.so.1").is_file()
        and {name for _, name in socket.if_nameindex()} == {"lo"},
        "LINUX_MUSL_RED_AISLADA",
    )
    parent = Path("/work")
    node = parent.lstat()
    base.need(
        parent.resolve(strict=True) == parent
        and stat.S_ISDIR(node.st_mode)
        and node.st_uid == node.st_gid == 1000
        and stat.S_IMODE(node.st_mode) == 0o700,
        "WORK_PRIVADO",
    )
    base.need(
        all(os.statvfs(path).f_flag & os.ST_RDONLY for path in (inputs.delta, inputs.v5)),
        "ENTRADAS_RO",
    )
    base.storage(parent, 5 * 1024**3, 350_000)
    before = verify_store(op, STORE)
    os.umask(0o077)
    run = parent / "baseline-replay-v2"
    run.mkdir(mode=0o700)
    identity = run.stat()
    proof = {"owner": OWNER, "device": identity.st_dev, "inode": identity.st_ino}
    op.write(run / ".agency-owner", op.encoded(proof))
    tool = run / "pnpm"
    op.write(tool, inputs.binary)
    tool.chmod(0o700)
    version = op.execute(
        [str(tool), "--version"], run, op.environment(run, "version"), run / "version.log", 10
    )
    base.need(
        version["status"] == "completed"
        and version["exit_code"] == 0
        and (run / "version.log").read_bytes().strip() == b"12.4.2",
        "PNPM_VERSION",
    )
    env = op.environment(run, "offline")
    work = run / "workspace"
    base.workspace(op, inputs, work)
    vendor = work / base.VENDOR_PATH
    op.read(vendor, base.VENDOR, 2 * 1024**2)
    cache = Path(env["XDG_CACHE_HOME"]) / "pnpm" / base.CACHE.removeprefix("cache/")
    for name in inputs.metadata:
        op.write(cache / (name + ".jsonl"), inputs.metadata_raw(name))
    log = run / "offline.log"
    process = op.execute(command(tool, work), work, env, log, 900)
    process["lock_unchanged"] = op.sha((work / "pnpm-lock.yaml").read_bytes()) == base.LOCK
    op.write(run / "offline-process.json", op.encoded(process))
    after = verify_store(op, STORE)
    base.need(
        process["status"] == "completed"
        and process["exit_code"] == 0
        and process["lock_unchanged"]
        and before == after
        and (work / "node_modules/.pnpm").is_dir(),
        "REPLAY_PROCESO",
    )
    result = {
        **proof,
        **parse_log(log.read_bytes()),
        "process": process,
        "index": after,
        "store_catalog_sha256": KEYS,
        "store_read_only": True,
        "vendor_sha256": base.VENDOR,
        "store_rows": 3125,
        "external_network": False,
        "scripts_executed": False,
        "compiled": False,
        "production_approved": False,
        "lock_sha256": base.LOCK,
    }
    op.write(run / "receipt.json", op.encoded(result))
    return result


def main():
    op = base.load_helper(Path("/ops/n8n-js-loopback-store-v1.py"), base.LOOPBACK)
    result = replay(op, base.Inputs(op, Path("/inputs"), Path("/v5")))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
