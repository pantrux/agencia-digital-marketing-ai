"""Prepara el CLI Prisma de SupoClip desde el lock y los insumos comprobados."""

import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / ".tools/backup-source-build"
FRONTEND = ARTIFACTS / "supoclip-frontend-v1"
NPM = ARTIFACTS / "supoclip-frontend-npm-v1"
INTEGRATION = ARTIFACTS / "supoclip-integration-v1"
LOCK_SHA = "d3d98cbe5f080c30a77cc16e7f6e128b2d3d52cb08a9ffa609f80b7ffb8bc2fe"
NPM_SHA = "ad5068deb8af4bea1309148ab49dc31d8bceb2556d709abd2b1c071e8f6c9afd"
ABI_SHA = "dab55c02cdccb4f6db7f20783092ab3865cd16017459b8b021c5753e4ccd9408"
MIGRATIONS_SHA = "5be6e0901b3b9534950581be148ede142eebabfd193ce2e7c3000a3c9fe18336"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def checked(path, expected):
    data = path.read_bytes()
    if sha(data) != expected:
        raise ValueError(f"Huella inesperada en {path.name}")
    return data


def closure(lock):
    root = lock["importers"]["."]["dependencies"]["prisma"]["version"]
    pending = {"prisma@" + root}
    versions = {}
    seen = set()
    while pending:
        snapshot = pending.pop()
        if snapshot in seen:
            continue
        seen.add(snapshot)
        name, version = snapshot.split("(", 1)[0].rsplit("@", 1)
        versions.setdefault(name, set()).add(version)
        row = lock["snapshots"][snapshot]
        for group in ("dependencies", "optionalDependencies"):
            pending.update(f"{key}@{value}" for key, value in row.get(group, {}).items())
    if len(seen) != 41 or {k for k, v in versions.items() if len(v) != 1} != {"citty"}:
        raise ValueError("El cierre de dependencias Prisma cambió")
    if versions.pop("citty") != {"0.1.6", "0.2.2"}:
        raise ValueError("Las versiones de citty cambiaron")
    if lock["snapshots"]["nypm@0.6.6"]["dependencies"]["citty"] != "0.2.2":
        raise ValueError("La dependencia de nypm cambió")
    if lock["snapshots"]["giget@2.0.0"]["dependencies"]["citty"] != "0.1.6":
        raise ValueError("La dependencia de giget cambió")
    placements = {
        f"prisma-source/node_modules/{key}": (key, next(iter(values)))
        for key, values in versions.items()
    }
    placements["prisma-source/node_modules/citty"] = ("citty", "0.1.6")
    placements["prisma-source/node_modules/nypm/node_modules/citty"] = ("citty", "0.2.2")
    return placements


def unpack_package(raw, name, version):
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        files = {}
        for member in archive:
            parts = member.name.split("/")
            if parts[0] != "package" or any(p in {"", ".", ".."} or "\\" in p for p in parts):
                raise ValueError(f"Ruta inválida en {name}")
            if member.isdir():
                continue
            if len(parts) < 2 or not member.isfile():
                raise ValueError(f"Tipo de entrada inesperado en {name}")
            path = "/".join(parts[1:])
            if path in files:
                raise ValueError(f"Entrada duplicada en {name}")
            files[path] = archive.extractfile(member).read()
    metadata = json.loads(files["package.json"])
    if (metadata["name"], metadata["version"]) != (name, version):
        raise ValueError(f"Identidad npm distinta en {name}")
    return files


def migrations():
    manifest = json.loads(checked(INTEGRATION / "migration-manifest.json", MIGRATIONS_SHA))
    source = manifest["prisma"]["source"]
    raw = checked(ROOT / source["path"], source["sha256"])
    if len(raw) != source["bytes"]:
        raise ValueError("Tamaño inesperado del TAR de frontend")
    rows = manifest["prisma"]["rows"]
    paths = {f"prisma/migrations/{name}/migration.sql": row for name, row in rows.items()}
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for item in archive:
            path = item.name.removeprefix("frontend/")
            if path not in paths and path not in {
                "prisma/schema.prisma",
                "prisma/migrations/migration_lock.toml",
            }:
                continue
            if not item.isfile() or path in files:
                raise ValueError(f"Entrada Prisma no ordinaria o duplicada: {path}")
            data = archive.extractfile(item).read()
            if path in paths and paths[path] != {"sha256": sha(data), "bytes": len(data)}:
                raise ValueError(f"Migración distinta: {path}")
            files[path] = data
    if len(rows) != 27 or len(files) != 29:
        raise ValueError("Conjunto de migraciones incompleto")
    return {f"prisma-source/{name}": (raw, 0o444) for name, raw in files.items()}


def native():
    abi = json.loads(checked(FRONTEND / "schema-engine-wolfi-abi.json", ABI_SHA))
    out = {
        "prisma-bin/schema-engine": (
            checked(FRONTEND / "schema-engine", abi["subject_sha256"]),
            0o555,
        )
    }
    if set(abi["libraries"]) != {"libcrypto.so.3", "libssl.so.3"}:
        raise ValueError("El cierre OpenSSL cambió")
    for name, entry in abi["libraries"].items():
        apk = checked(ROOT / entry["apk_path"], entry["apk_sha256"])
        with tarfile.open(fileobj=io.BytesIO(apk), mode="r:gz", ignore_zeros=True) as archive:
            [member] = [m for m in archive if m.name == "usr/lib/" + name and m.isfile()]
            data = archive.extractfile(member).read()
        if sha(data) != entry["library_sha256"]:
            raise ValueError(f"Biblioteca distinta: {name}")
        out[f"prisma-bin/{name}"] = (data, 0o444)
    return out


def manifest(files):
    return {
        path: {"bytes": len(raw), "sha256": sha(raw), "mode": oct(mode)}
        for path, (raw, mode) in sorted(files.items())
    }


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def add(archive, name, data, mode):
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), mode
    archive.addfile(info, io.BytesIO(data))


def prepare(output):
    lock_raw = checked(FRONTEND / "lock-resolution/pnpm-lock.yaml", LOCK_SHA)
    lock = yaml.safe_load(lock_raw)
    rows = json.loads(checked(NPM / "tarball-receipt-v2.json", NPM_SHA))["tarballs"]
    placements = closure(lock)
    files = {**migrations(), **native()}
    for path, (name, version) in placements.items():
        key = f"{name}@{version}"
        row = rows[key]
        raw = checked(NPM / row["path"], row["sha256"])
        expected = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii")
        if (
            row["integrity"] != expected
            or lock["packages"][key]["resolution"]["integrity"] != expected
        ):
            raise ValueError(f"Integridad npm distinta: {key}")
        for leaf, payload in unpack_package(raw, name, version).items():
            dest = f"{path}/{leaf}"
            if dest in files:
                raise ValueError(f"Destino duplicado: {dest}")
            files[dest] = (payload, 0o444)
    descriptor = encode(
        {
            "schema": 1,
            "lock_sha256": LOCK_SHA,
            "npm_receipt_sha256": NPM_SHA,
            "abi_sha256": ABI_SHA,
            "migration_manifest_sha256": MIGRATIONS_SHA,
            "packages": placements,
            "files": manifest(files),
        }
    )
    files["manifest.json"] = (descriptor, 0o444)
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        output.open("xb") as target,
        gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for name, (data, mode) in sorted(files.items()):
                if not re.fullmatch(r"[A-Za-z0-9_@.+() /!#&$,-]+", name) or not PurePosixPath(
                    name
                ).is_relative_to("."):
                    raise ValueError(f"Ruta no admitida: {name}")
                add(archive, name, data, mode)
    with output.open("rb") as stream:
        archive_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "sha256": archive_sha,
        "bytes": output.stat().st_size,
        "manifest_sha256": sha(descriptor),
        "files": len(files),
        "snapshots": len(placements),
        "migrations": 27,
        "lifecycle_executed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.out), sort_keys=True))
