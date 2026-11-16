"""Instala el delta de wa-sqlite sobre el workspace y el store privados de Linux."""

import argparse
import io
import json
import os
import platform
import re
import socket
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay_server_linux_baseline_v2 as replay  # noqa: E402
import seed_server_linux_baseline as base  # noqa: E402

OWNER = "agencia-digital-marketing-ai:n8n-server-sqlite-overlay:20261113-v1"
ROOT = Path("/work/baseline-replay-v2/workspace")
ORIGINAL = Path("/baseline-workspace")
STORE = Path("/store")
ORIGINAL_STORE = Path("/baseline-store")
SCRATCH = Path("/scratch")
DELTA = Path("/delta")
INPUTS = Path("/inputs")
V5 = Path("/v5")
OPS = Path("/ops")
EDITOR = "packages/frontend/editor-ui"
EDITOR_MANIFEST = EDITOR + "/package.json"
VENDOR = "vendor/wa-sqlite-1.0.9-sqlite3.54.1.tgz"
SRI = "sha512-Ny5kin2AyEZC11N+zCR0UKaqDrKDdCrXm5eCC0w4Uukv8IVRNHV921MeQkhNWj1UcQfOUzm51Xpi39lr6WOXlg=="
VENDOR_KEY = SRI + "\tfile:" + VENDOR
OVERLAY_SHA = "578ce1e81213224e3028759340a8a32be6e060f23f082d84a8a704eb51e078f8"
SANITY_SHA = "40aa3ef271b603e009c4ce8b5644cf8093e74555cbae49d180926dc47c6f3872"
OLD_EDITOR = "fbd1b6d543468e60deee9f05d9997f0dab74b4fda9e0fae1e10428231939a832"
NEW_FILES = {
    "source/pnpm-lock.yaml": (
        1530914,
        "f49f5522634b6e5eaf742cd31b50c0eb35489142cf1cd3c65e6ed0fc8d56e392",
    ),
    f"source/{EDITOR_MANIFEST}": (
        7141,
        "c37d29685046908dc67613ec813dbea6084ae29e2e854d923dbab7fbc2bc4f2c",
    ),
    f"source/{VENDOR}": (
        1233922,
        "f777c87f5809534b0e1ae79abb8a5f7656614063da7599c82e926085ec5b458b",
    ),
    "evidence/package-receipt.json": (
        1899,
        "d055607228891b0ab35ceeeeb0add4f06b4a56183bd9dd3a77bde0c787230c79",
    ),
    "evidence/server-delta-receipt.json": (
        2170,
        "6e39bad7dd5e691d8937526fceca77048791109bc94e55181e645856786ad82c",
    ),
}
OPS_PINS = {
    "receive_server_linux_baseline.py": "211fcd93220ea68fe7d4b4cfe9f43c343f2a59ead575aced9bb889cb4810d451",
    "seed_server_linux_baseline.py": "8137679311a2c3b9626abb2fa0959e589ae47ec94f4491897971793310039cda",
    "replay_server_linux_baseline_v2.py": "864a1a69356baea58ba0e0d601d7f41e2b2a7b3989994957ea35dac9a2450984",
    "n8n-js-loopback-store-v1.py": base.LOOPBACK,
}
MODULES = "node_modules/.modules.yaml"
MIN_BYTES = 512 * 1024**2
MIN_INODES = 16384


def mount(path, read_only):
    base.need(
        path.resolve(strict=True) == path
        and bool(os.statvfs(path).f_flag & os.ST_RDONLY) == read_only,
        "MONTAJE_IDENTIDAD",
    )


def mounts():
    base.need(
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and os.geteuid() == os.getegid() == 1000
        and {name for _, name in socket.if_nameindex()} == {"lo"}
        and Path("/lib/ld-musl-x86_64.so.1").is_file(),
        "LINUX_MUSL_RED_AISLADA",
    )
    for path in (Path("/"), ORIGINAL, ORIGINAL_STORE, DELTA, INPUTS, V5, OPS):
        mount(path, True)
    for path in (ROOT, STORE, SCRATCH):
        mount(path, False)
        node = path.stat()
        base.need(node.st_uid == node.st_gid == 1000 and stat.S_ISDIR(node.st_mode), "WORK_UID")
        base.storage(path, MIN_BYTES, MIN_INODES)
    base.need(
        stat.S_IMODE(SCRATCH.stat().st_mode) == 0o700 and not any(SCRATCH.iterdir()),
        "SCRATCH_NUEVO",
    )


def load_payload(op, source):
    files = {}
    for name, (size, digest) in NEW_FILES.items():
        body = op.read(source / name, digest, size)
        base.need(len(body) == size, "DELTA_BYTES")
        files[name] = body
    manifest = "".join(f"{NEW_FILES[name][1]}  {name}\n" for name in sorted(NEW_FILES)).encode()
    base.need((source / "SHA256SUMS").read_bytes() == manifest, "DELTA_MANIFIESTO")
    return files


def validate_preimage(op, root):
    op.read(root / "pnpm-lock.yaml", base.LOCK, 2 * base.MIB)
    op.read(root / EDITOR_MANIFEST, OLD_EDITOR, 32 * 1024)
    op.read(root / base.VENDOR_PATH, base.VENDOR, 2 * base.MIB)
    with (root / MODULES).open("rb") as stream:
        raw = stream.read(4 * base.MIB + 1)
    base.need(len(raw) <= 4 * base.MIB, "WORKSPACE_MODULES_LIMITE")
    if raw.lstrip().startswith(b"{"):
        store_dir = json.loads(raw).get("storeDir")
    else:
        rows = re.findall(rb"(?m)^storeDir: ['\"]?([^'\"\s]+)['\"]?\s*$", raw)
        store_dir = rows[0].decode() if len(rows) == 1 else None
    base.need(
        store_dir == "/store/v11"
        and (root / "node_modules/.pnpm").is_dir()
        and (root / EDITOR / "node_modules/wa-sqlite").is_symlink(),
        "WORKSPACE_STORE_ORIGINAL",
    )


def checked_keys(op):
    original_index = replay.verify_store(op, ORIGINAL_STORE)
    old_keys, overlay_index = base.store_keys(op, STORE)
    base.need(
        overlay_index == original_index
        and len(old_keys) == 3125
        and op.sha(("\n".join(sorted(old_keys)) + "\n").encode()) == replay.KEYS
        and VENDOR_KEY not in old_keys,
        "STORE_PREIMAGEN",
    )
    return old_keys, original_index


def replace(path, raw):
    pending = path.with_name(path.name + ".agency-new")
    with pending.open("xb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    os.replace(pending, path)


def warm(op, tool, vendor):
    project = SCRATCH / "warm-project"
    project.mkdir(mode=0o700)
    reference = "file:" + VENDOR
    op.write(project / VENDOR, vendor)
    op.write(
        project / "package.json",
        op.encoded(
            {
                "name": "agency-wa-sqlite-cache",
                "version": "0.0.0",
                "private": True,
                "dependencies": {"wa-sqlite": reference},
            }
        ),
    )
    lock = f"""lockfileVersion: '9.0'

settings:
  autoInstallPeers: true
  excludeLinksFromLockfile: false

importers:
  .:
    dependencies:
      wa-sqlite:
        specifier: {reference}
        version: {reference}

packages:
  wa-sqlite@{reference}:
    resolution: {{integrity: {SRI}, tarball: {reference}}}
    version: 1.0.9

snapshots:
  wa-sqlite@{reference}: {{}}
""".encode()
    op.write(project / "pnpm-lock.yaml", lock)
    command = [
        flag for flag in replay.command(tool, project) if flag != "--config.frozen-store=true"
    ]
    log = SCRATCH / "warm.log"
    result = op.execute(command, project, op.environment(SCRATCH, "warm"), log, 90)
    base.need(
        result["status"] == "completed"
        and result["exit_code"] == 0
        and (project / "pnpm-lock.yaml").read_bytes() == lock
        and (project / "node_modules/wa-sqlite").is_dir(),
        "STORE_WARM",
    )
    return result


def policy(op, inputs, tool):
    env = op.environment(SCRATCH, "offline")
    cache = Path(env["XDG_CACHE_HOME"]) / "pnpm" / base.CACHE.removeprefix("cache/")
    for name in inputs.metadata:
        op.write(cache / (name + ".jsonl"), inputs.metadata_raw(name))
    command = replay.command(tool, ROOT)
    log = SCRATCH / "policy.log"
    result = op.execute([*command, "--lockfile-only"], ROOT, env, log, 90)
    checks = re.findall(
        rb"(?m)^\xe2\x9c\x93 Lockfile passes supply-chain policies \((\d+) entries in [^\r\n]+\)\r?$",
        log.read_bytes(),
    )
    base.need(
        result["status"] == "completed" and result["exit_code"] == 0 and checks == [b"3476"],
        "POLITICA_ACTUAL",
    )
    return command, env, result


def installed(op, data):
    package = (ROOT / EDITOR / "node_modules/wa-sqlite").resolve(strict=True)
    base.need(package.is_relative_to(ROOT / "node_modules/.pnpm"), "WA_SQLITE_RESOLUCION")
    proof = json.loads(data["evidence/package-receipt.json"])
    package_body = data["source/" + VENDOR]
    with tarfile.open(fileobj=io.BytesIO(package_body), mode="r:gz") as tar:
        before = tar.extractfile("package/package.json").read()
    base.need(
        op.read(package / "package.json", op.sha(before), 32 * 1024) == before, "WA_SQLITE_PAQUETE"
    )
    dist = package / "dist"
    base.need({item.name for item in dist.iterdir()} == proof["replaced"].keys(), "WA_SQLITE_DIST")
    for name, record in proof["replaced"].items():
        body = op.read(dist / name, record["new_sha256"], 4 * base.MIB)
        base.need(len(body) == record["bytes"], "WA_SQLITE_DIST_BYTES")
    return {"path": str(package.relative_to(ROOT)), "files": proof["replaced"]}


def install():
    mounts()
    op = base.load_helper(OPS / "n8n-js-loopback-store-v1.py", base.LOOPBACK)
    for name, digest in OPS_PINS.items():
        op.read(OPS / name, digest, 64 * 1024)
    data = load_payload(op, DELTA)
    validate_preimage(op, ORIGINAL)
    validate_preimage(op, ROOT)
    old_keys, original_index = checked_keys(op)
    os.umask(0o077)
    inputs = base.Inputs(op, INPUTS, V5)
    tool = SCRATCH / "pnpm"
    op.write(tool, inputs.binary)
    tool.chmod(0o700)
    version_log = SCRATCH / "version.log"
    version = op.execute(
        [str(tool), "--version"], SCRATCH, op.environment(SCRATCH, "version"), version_log, 10
    )
    base.need(
        version["exit_code"] == 0 and version_log.read_bytes().strip() == b"12.4.2", "PNPM_VERSION"
    )
    for name in (VENDOR, EDITOR_MANIFEST, "pnpm-lock.yaml"):
        replace(ROOT / name, data["source/" + name])
    warm_result = warm(op, tool, data["source/" + VENDOR])
    keys, warmed_index = base.store_keys(op, STORE)
    base.need(keys == old_keys | {VENDOR_KEY}, "STORE_DELTA_INDICE")
    command, env, policy_result = policy(op, inputs, tool)
    log = SCRATCH / "install.log"
    result = op.execute(command, ROOT, env, log, 480)
    base.need(result["status"] == "completed" and result["exit_code"] == 0, "PNPM_INSTALACION")
    progress = re.findall(
        rb"(?m)^Progress: resolved \d+, reused \d+, downloaded (\d+), added \d+, done\r?$",
        log.read_bytes(),
    )
    base.need(all(n == b"0" for n in progress), "PNPM_DESCARGAS")
    for name in (VENDOR, EDITOR_MANIFEST, "pnpm-lock.yaml"):
        size, digest = NEW_FILES["source/" + name]
        base.need(len(op.read(ROOT / name, digest, size)) == size, "WORKSPACE_DELTA_FISICO")
    installed_package = installed(op, data)
    validate_preimage(op, ORIGINAL)
    base.need(replay.verify_store(op, ORIGINAL_STORE) == original_index, "STORE_ORIGINAL_CAMBIO")
    base.need(base.store_keys(op, STORE) == (keys, warmed_index), "STORE_FROZEN_CAMBIO")
    receipt = {
        "schema": 1,
        "owner": OWNER,
        "success": True,
        "overlay_sha256": OVERLAY_SHA,
        "store_before": original_index,
        "store_after": warmed_index,
        "store_rows": len(keys),
        "store_added_key": VENDOR_KEY,
        "installed": installed_package,
        "warm": warm_result,
        "policy": policy_result | {"verified_entries": 3476},
        "install": result | {"registry_downloads": 0, "progress_reported": bool(progress)},
        "production_approved": False,
    }
    op.write(SCRATCH / "receipt.json", op.encoded(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.install:
        result = install()
    else:
        result = {
            "owner": OWNER,
            "min_bytes": MIN_BYTES,
            "min_inodes": MIN_INODES,
            "archive_sha256": OVERLAY_SHA,
            "overlay_sanity_sha256": SANITY_SHA,
            "readonly": [
                PurePosixPath(path.as_posix()).as_posix()
                for path in (ORIGINAL, ORIGINAL_STORE, DELTA, INPUTS, V5, OPS)
            ],
            "writable": [
                PurePosixPath(path.as_posix()).as_posix() for path in (ROOT, STORE, SCRATCH)
            ],
            "policy_entries": 3476,
            "store_expected_rows": 3125,
            "sources": OPS_PINS,
        }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
