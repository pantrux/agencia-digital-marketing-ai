"""Describe el constructor y valida árboles musl; no invoca Docker ni el SDK."""

import argparse
import hashlib
import json
import re
import stat
from pathlib import Path

BASE = "docker.io/library/postgres:16.15-alpine3.24@sha256:ac838714b7d794185a866e5a6b479b954e7d8a070568aa0acac6fe07c3070bf5"
OWNER = "agencia-digital-marketing-ai:postgres-immich-builder:v1"
MANIFEST_SHA = "8d5a521ef8b2827f5198b9dcb5148c05f0aad743c7ce39912c75491139f5f745"
NATIVE_LOCK_SHA = "a805dd0dd8bae497bf8326e48f1edecbce72e3588d857d314330b90638c1cd41"
VENDOR_LOCK_SHA = "6bd7c4db4c0d297da3a073939f6e78c6d7837bc65d2a245053dac531f9199c57"
XTASK_BEFORE = "748e21eac842f629d236958227b2bcf35498dc1479f28f37f9825bf7c6a3f3fd"
XTASK_AFTER = "9055ec9b313bcc827c52705f37bca6ceba4468de2073cb5de319e703684969b1"
PATCH_SHA = "295124d77f90169a58adbee3e2b8e98f1770de669e023738c1ba9098d3b5c500"
TARGET = "x86_64-unknown-linux-musl"
MAX_TREE = 256 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def checked_file(path, expected=None, maximum=1024**3):
    path = Path(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & 0x400
        or info.st_size > maximum
    ):
        raise ValueError("Una entrada no es un archivo ordinario acotado.")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if expected is not None and digest != expected:
        raise ValueError("Una entrada difiere de su SHA-256 fijado.")
    return {"source": str(path.resolve()), "sha256": digest, "size": info.st_size}


def frozen_xtask(original, expected_before=XTASK_BEFORE, expected_after=XTASK_AFTER):
    if sha256(original) != expected_before:
        raise ValueError("xtask no coincide con la fuente original revisada.")
    replacements = (
        (
            b'["metadata", "--format-version", "1"]',
            b'["metadata", "--format-version", "1", "--frozen"]',
            1,
        ),
        (b'["rustc"]', b'["rustc", "--frozen"]', 2),
        (b'["clippy"]', b'["clippy", "--frozen"]', 1),
    )
    result = original
    for before, after, count in replacements:
        if result.count(before) != count:
            raise ValueError("Las cuatro invocaciones Cargo originales no son exactas.")
        result = result.replace(before, after)
    if sha256(result) != expected_after:
        raise ValueError("El resultado del parche xtask no coincide con su hash revisado.")
    return result


def parse_tree(text, root, allowed):
    if not isinstance(text, str) or len(text.encode()) > MAX_TREE:
        raise ValueError("El árbol no está completo dentro del límite de 256 KiB.")
    if any(ord(char) < 32 and char not in "\r\n" for char in text):
        raise ValueError("El árbol contiene controles o secuencias de terminal.")
    packages = set()
    for line in text.splitlines():
        if line in {"", "[build-dependencies]"}:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+) v([^\s()]+)((?: \([^()\r\n]+\))*)", line)
        if match is None or match.group(1, 2) not in allowed:
            raise ValueError("El árbol contiene una línea o paquete ajeno al lock.")
        for annotation in re.findall(r"\(([^()]+)\)", match[3]):
            if annotation not in {"proc-macro", "*"} and not re.fullmatch(
                r"/work/source(?:/[A-Za-z0-9_+.-]+)*", annotation
            ):
                raise ValueError(
                    "El árbol contiene una fuente distinta del vendor/workspace propio."
                )
            if ".." in annotation.split("/"):
                raise ValueError("Una ruta del árbol contiene un escape.")
        package = match.group(1, 2)
        if package[0] in {"wasm-bindgen-shared", "wasm-bindgen", "git2"}:
            raise ValueError("El árbol selecciona una ruta Git no autorizada para este incremento.")
        packages.add(package)
    if (root, "0.0.0") not in packages:
        raise ValueError("El árbol no contiene la raíz de workspace esperada.")
    return packages


def validate_graph(vchord, xtask, allowed):
    library = parse_tree(vchord, "vchord", allowed)
    tool = parse_tree(xtask, "xtask", allowed)
    if not {("rusqlite", "0.38.0"), ("libsqlite3-sys", "0.36.0"), ("pgrx", "0.17.0")} <= library:
        raise ValueError(
            "El árbol normal/build no acredita rusqlite, libsqlite3-sys y pgrx previstos."
        )
    if ("object", "0.38.1") not in tool:
        raise ValueError("El árbol xtask no acredita su dependencia prevista.")
    return {
        "target": TARGET,
        "target_graph_observed": True,
        "trees": {"vchord": sorted(library), "xtask": sorted(tool)},
        "tree_sha256": {"vchord": sha256(vchord.encode()), "xtask": sha256(xtask.encode())},
        "wasm_bindgen_shared_selected": False,
        "compile_enabled": False,
        "compilation_equivalence_claimed": False,
    }


def container_plan(inputs, outputs, input_manifest_sha256):
    source = re.fullmatch(
        r"/opt/media-platform/build/contexts/postgres-immich/([a-f0-9]{32})/inputs", inputs
    )
    target = re.fullmatch(
        r"/var/lib/media-platform/build/postgres-immich/([a-f0-9]{32})/outputs", outputs
    )
    if (
        source is None
        or target is None
        or source[1] != target[1]
        or not re.fullmatch(r"[a-f0-9]{64}", input_manifest_sha256)
    ):
        raise ValueError("Las rutas propias, su ID o el pin del inventario no son válidos.")
    args = [
        "create",
        "--pull",
        "never",
        "--platform",
        "linux/amd64",
        "--name",
        "agency-postgres-immich-" + source[1],
        "--label",
        "cl.e-scale.agencia.owner=" + OWNER,
        "--label",
        "cl.e-scale.agencia.build-id=" + source[1],
        "--network",
        "none",
        "--cpus",
        "4",
        "--memory",
        "12g",
        "--memory-swap",
        "12g",
        "--pids-limit",
        "256",
        "--restart",
        "no",
        "--stop-timeout",
        "10",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "NET_RAW",
        "--mount",
        f"type=bind,src={inputs},dst=/inputs,readonly",
        "--mount",
        f"type=bind,src={outputs},dst=/outputs",
        "--env",
        "AGENCY_INPUT_MANIFEST_SHA256=" + input_manifest_sha256,
        "--env",
        "AGENCY_BUILD_OWNER=" + OWNER,
        "--workdir",
        "/work",
        "--entrypoint",
        "/bin/sh",
        BASE,
        "/inputs/offline-container.sh",
        "hold",
    ]
    return {
        "schema_version": 1,
        "owner": OWNER,
        "base": BASE,
        "create_arguments": args,
        "persist_cid_before_start": True,
        "maximum_lifetime_seconds": 7200,
        "controller_timeout_required": True,
        "stages": ["sdk", "graph"],
        "stage_timeouts_seconds": {"sdk": 900, "graph": 180},
        "graph_receipt_requires_backup_before_compile": True,
        "sdk_executed": False,
        "compile_enabled": False,
        "runtime_image_built": False,
        "production_mounts": False,
        "secrets_injected": False,
    }


def input_inventory(workspace):
    workspace = Path(workspace)
    sources = workspace / "docker/postgres-immich"
    native = workspace / ".tools/postgres-immich-build/native-inputs-8d5a521ef8b2827f"
    vendor = workspace / ".tools/postgres-immich-build/vendor-8d5a521ef8b2827f"
    manifest_path = sources / "musl-build-inputs.json"
    checked_file(manifest_path, MANIFEST_SHA)
    manifest = json.loads(manifest_path.read_bytes())
    checked_file(native / "native-sdk-lock.json", NATIVE_LOCK_SHA)
    lock = json.loads((native / "native-sdk-lock.json").read_bytes())
    files = {}

    def add(name, path, expected=None):
        if not re.fullmatch(r"[A-Za-z0-9_+./@-]+", name) or ".." in name.split("/"):
            raise ValueError("Nombre de kit no admitido.")
        files[name] = checked_file(path, expected)

    add("musl-build-inputs.json", manifest_path, MANIFEST_SHA)
    add("native-sdk-lock.json", native / "native-sdk-lock.json", NATIVE_LOCK_SHA)
    add("vendor-lock.json", vendor / "vendor-lock.json", VENDOR_LOCK_SHA)
    add(
        "vendor.tar.gz",
        vendor / "vendor.tar.gz",
        "c0209f4b1b6fc31d56e495886cb4729c0c52634504c7a0c627b29d2859a0b2c1",
    )
    add(
        "vectorchord-source.tar.gz",
        vendor / "downloads/vectorchord-source.tar.gz",
        manifest["vectorchord_source"]["sha256"],
    )
    add(
        "pgvector-source.tar.gz",
        workspace / ".tools/cache/phase4-sources/pgvector-extension.tar.gz",
        manifest["pgvector_source"]["sha256"],
    )
    add(
        "sqlite-amalgamation-3540100.zip",
        workspace / ".tools/cache/phase4-sources/sqlite-amalgamation-3540100.zip",
        manifest["sqlite_link_candidate"]["source"]["sha256"],
    )
    for name in ("offline_recipe.py", "offline-container.sh"):
        add(name, sources / name)
    add("xtask-offline.patch", sources / "xtask-offline.patch", PATCH_SHA)
    for component in lock["rust_sdk"]:
        name = component["component"] + ".tar.xz"
        add("rust/" + name, native / "downloads" / name, component["archive_sha256"])
    for package in lock["packages"]:
        name = package["name"] + "-" + package["version"] + ".apk"
        add("apk/" + name, native / "downloads" / name, package["archive_sha256"])
    checksums = "".join(f"{entry['sha256']}  {name}\n" for name, entry in sorted(files.items()))
    return {
        "manifest_sha256": MANIFEST_SHA,
        "native_lock_sha256": NATIVE_LOCK_SHA,
        "files": files,
        "checksum_manifest": checksums,
        "checksum_manifest_sha256": sha256(checksums.encode()),
        "sdk_executed": False,
        "compile_enabled": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--inputs", required=True)
    plan.add_argument("--outputs", required=True)
    plan.add_argument("--manifest-sha256", required=True)
    commands.add_parser("inventory")
    graph = commands.add_parser("validate-graph")
    graph.add_argument("--vchord", type=Path, required=True)
    graph.add_argument("--xtask", type=Path, required=True)
    graph.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        result = container_plan(args.inputs, args.outputs, args.manifest_sha256)
    elif args.command == "inventory":
        result = input_inventory(Path(__file__).resolve().parents[2])
    else:
        checked_file(args.manifest, MANIFEST_SHA)
        manifest = json.loads(args.manifest.read_bytes())
        allowed = {
            (p["name"], p["version"])
            for p in manifest["cargo"]["registry_crates"]
            + manifest["cargo"]["local_workspace_crates"]
        }
        for path in (args.vchord, args.xtask):
            checked_file(path, maximum=MAX_TREE)
        result = validate_graph(args.vchord.read_text(), args.xtask.read_text(), allowed)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
