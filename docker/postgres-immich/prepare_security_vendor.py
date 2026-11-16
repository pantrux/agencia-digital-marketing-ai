"""Deriva datos Cargo fijados; nunca ejecuta Cargo, build.rs, red ni procesos."""

import argparse
import copy
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tomllib
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
HELPER_PIN = "367e9f945bdc859e1cbd4816bb4db4ccd7b970b02f9da713a374158fd795c8be"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
OUTPUT_NAMES = {"Cargo.lock", "vendor.tar.gz", "vendor-lock.json", "build-script-audit.json"}
MAX_FILE = 64 * 1024**2
MAX_TOTAL = 2 * 1024**3


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def resolve_input(base, name):
    if not isinstance(name, str) or not name or any(c in name for c in "\\:\x00"):
        raise ValueError("Ruta de entrada ambigua.")
    parts = name.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise ValueError("La entrada debe permanecer bajo el workspace.")
    return Path(base).absolute().joinpath(*parts)


def regular(path, maximum):
    path = Path(path).absolute()
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Ancestro enlazado o ajeno.")
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & 0x400
        or before.st_size > maximum
    ):
        raise ValueError("Archivo no regular, enlazado o excesivo.")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)

    if (
        len(raw) > maximum
        or len(raw) != before.st_size
        or not all(identity(s) == identity(before) for s in (opened, after, current))
    ):
        raise ValueError("La entrada cambió durante su lectura.")
    return raw


def helper():
    path = BASE / "docker/postgres-immich/prepare_vendor.py"
    raw = regular(path, 128 * 1024)
    if digest(raw) != HELPER_PIN:
        raise ValueError("El inspector Cargo original cambió.")
    namespace = {"__name__": "vendor_data_helper", "__file__": str(path)}
    exec(compile(raw, str(path), "exec"), namespace)
    return namespace


def patch_lock(raw, changes):
    text = raw.decode("utf-8", "strict")
    before = tomllib.loads(text)
    blocks = re.split(r"(?m)(?=^\[\[package\]\]\r?$)", text)
    expected = copy.deepcopy(before)
    for change in changes:
        matching = [row for row in expected.get("package", []) if row.get("name") == change["name"]]
        if len(matching) != 1:
            raise ValueError("El lock debe contener una sola versión del paquete corregido.")
        row = matching[0]
        if (
            row.get("version") != change["old_version"]
            or row.get("checksum") != change["old_checksum"]
            or row.get("source") != REGISTRY
        ):
            raise ValueError("El bloque original del lock no coincide.")
        row.update(version=change["version"], checksum=change["checksum"])
        found = 0
        for index, block in enumerate(blocks):
            if not block.startswith("[[package]]"):
                continue
            parsed = tomllib.loads(block)["package"][0]
            if parsed["name"] != change["name"]:
                continue
            found += 1
            for key, value in [("version", change["version"]), ("checksum", change["checksum"])]:
                block, count = re.subn(
                    rf'(?m)^({key}\s*=\s*)"[^"\r\n]+"',
                    lambda m, v=value: m[1] + '"' + v + '"',
                    block,
                )
                if count != 1:
                    raise ValueError("El campo del lock no es único.")
            blocks[index] = block
        if found != 1:
            raise ValueError("El bloque del lock no es único.")
    result = "".join(blocks).encode("utf-8")
    if tomllib.loads(result.decode()) != expected:
        raise ValueError("La modificación alteró la semántica ajena del lock.")
    return result


def safe_name(name):
    if not isinstance(name, str) or any(ord(c) < 32 or c in "\\:" for c in name):
        raise ValueError("Nombre TAR ambiguo.")
    name.encode("utf-8", "strict")
    parts = name.removesuffix("/").split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise ValueError("Escape TAR rechazado.")
    return "/".join(parts)


def derive(lock_raw, vendor_raw, inventory, changes, candidate_bytes):
    """Inspecciona todos los bytes antes de entregar una propuesta publicable."""
    h = helper()
    crates = inventory["crates"]
    prefixes = {row["name"] + "-" + row["version"]: row for row in crates}
    if len(prefixes) != len(crates) or len(crates) != inventory["crate_count"]:
        raise ValueError("El inventario repite u omite crates.")
    registry = {
        (p["name"], p["version"], p["checksum"])
        for p in tomllib.loads(lock_raw.decode())["package"]
        if p.get("source") == REGISTRY
    }
    if registry != {(p["name"], p["version"], p["archive_sha256"]) for p in crates}:
        raise ValueError("Lock y vendor no contienen la misma selección.")
    locked = patch_lock(lock_raw, changes)
    items = {
        prefix: {
            "name": row["name"],
            "version": row["version"],
            "checksum": row["archive_sha256"],
            "prefix": prefix,
            "files": [],
            "directories": [],
        }
        for prefix, row in prefixes.items()
    }
    seen, folded, total, checksums = set(), set(), 0, set()
    with tarfile.open(fileobj=io.BytesIO(vendor_raw), mode="r:gz") as archive:
        for index, member in enumerate(archive):
            name = safe_name(member.name)
            if (
                index >= 100000
                or name in seen
                or name.casefold() in folded
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Vendor con duplicados, enlaces o tipos no admitidos.")
            seen.add(name)
            folded.add(name.casefold())
            prefix, slash, relative = name.partition("/")
            if prefix not in prefixes or (not slash and not member.isdir()):
                raise ValueError("Crate ajeno al inventario.")
            if member.isdir():
                if member.size:
                    raise ValueError("Directorio TAR con datos.")
                if relative:
                    items[prefix]["directories"].append(relative)
                continue
            if not 0 <= member.size <= MAX_FILE:
                raise ValueError("Archivo TAR excesivo.")
            total += member.size
            if total > MAX_TOTAL:
                raise ValueError("Vendor excesivo.")
            raw = archive.extractfile(member).read(MAX_FILE + 1)
            if len(raw) != member.size:
                raise ValueError("Miembro TAR truncado.")
            row = prefixes[prefix]
            if relative == ".cargo-checksum.json":
                if json.loads(raw) != {"files": row["files"], "package": row["archive_sha256"]}:
                    raise ValueError("Checksum Cargo original incorrecto.")
                checksums.add(prefix)
            else:
                if row["files"].get(relative) != digest(raw):
                    raise ValueError("Un archivo original no coincide con su inventario.")
                items[prefix]["files"].append(
                    {
                        "path": relative,
                        "data": raw,
                        "sha256": digest(raw),
                        "mode": 0o755 if member.mode & 0o111 else 0o644,
                    }
                )
    if checksums != set(prefixes):
        raise ValueError("Faltan checksums Cargo originales.")
    for prefix, item in items.items():
        if {f["path"] for f in item["files"]} != set(prefixes[prefix]["files"]):
            raise ValueError("Faltan archivos del vendor original.")
    replacements = []
    for change in changes:
        old = change["name"] + "-" + change["old_version"]
        if old not in items:
            raise ValueError("El paquete a sustituir no existe.")
        new = h["inspect_crate"](candidate_bytes[change["name"]], change)
        paths = [f["path"] for f in new["files"]]
        if len({p.casefold() for p in paths}) != len(paths):
            raise ValueError("Candidato con rutas ambiguas por mayúsculas.")
        contents = {f["path"]: f["data"] for f in new["files"]}
        package = tomllib.loads(contents["Cargo.toml"].decode("utf-8", "strict"))["package"]
        if (
            package.get("name") != change["name"]
            or package.get("version") != change["version"]
            or package.get("license") != "MIT OR Apache-2.0"
            or not {"LICENSE-MIT", "LICENSE-APACHE", "build.rs"} <= contents.keys()
            or digest(contents["build.rs"]) != change["build_script_sha256"]
        ):
            raise ValueError("Nombre, licencia o build.rs candidato no coincide.")
        del items[old]
        if new["prefix"] in items:
            raise ValueError("El candidato colisiona con otro crate.")
        items[new["prefix"]] = new
        replacements.append(new)
    ordered = [items[name] for name in sorted(items)]
    audit = h["audit_sources"](replacements)
    audit["scope"] = "Sólo dos candidatos, inspeccionados como datos; ningún script ejecutado."
    audit["manual_build_script_review"] = {
        "anyhow": "Lanza RUSTC para probe y --version; wrappers y flags deberán fijarse/limpiarse en el futuro SDK. No Git/red identificados en build.rs fijado.",
        "crossbeam-epoch": "Sólo consulta CARGO_CFG_SANITIZE y emite cfg; no crea procesos.",
    }
    return {
        "lock": locked,
        "items": ordered,
        "crate_count": len(ordered),
        "audit": audit,
        "crates": [
            {
                "name": i["name"],
                "version": i["version"],
                "archive_sha256": i["checksum"],
                "files": {f["path"]: f["sha256"] for f in i["files"]},
                "tree_sha256": digest(canonical({f["path"]: f["sha256"] for f in i["files"]})),
            }
            for i in ordered
        ],
    }


def emit_vendor(bundle, stream):
    h = helper()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for item in bundle["items"]:
                for directory in sorted(item["directories"]):
                    member = tarfile.TarInfo(item["prefix"] + "/" + directory)
                    member.type, member.mode, member.mtime = tarfile.DIRTYPE, 0o755, 0
                    archive.addfile(member)
                for file in sorted(item["files"], key=lambda f: f["path"]):
                    h["add_bytes"](
                        archive, item["prefix"] + "/" + file["path"], file["data"], file["mode"]
                    )
                checks = {f["path"]: f["sha256"] for f in item["files"]}
                h["add_bytes"](
                    archive,
                    item["prefix"] + "/.cargo-checksum.json",
                    canonical({"files": checks, "package": item["checksum"]}),
                )


def exclusive(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def publish(destination, outputs, plan, expected_sha256, access, finish_check=None):
    if digest(canonical(plan)) != expected_sha256 or not re.fullmatch(
        r"[a-f0-9]{64}", expected_sha256
    ):
        raise ValueError("Falta el SHA externo exacto del plan.")
    if (
        set(outputs) != set(plan["output_hashes"])
        or not set(outputs) <= OUTPUT_NAMES
        or any(digest(raw) != plan["output_hashes"][name] for name, raw in outputs.items())
    ):
        raise ValueError("Las salidas no coinciden con el plan.")
    destination = Path(destination)
    access.check(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise ValueError("No se adopta ni sobrescribe un derivado existente.")
    access.mkdir(destination)
    access.check(destination)
    identity = destination.stat()
    exclusive(
        destination / ".owner.json",
        canonical(
            {
                "owner": plan["owner"],
                "plan_sha256": expected_sha256,
                "device": identity.st_dev,
                "inode": identity.st_ino,
            }
        ),
    )
    for name, raw in outputs.items():
        access.check(destination)
        current = destination.lstat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise ValueError("El directorio derivado cambió de identidad.")
        path = destination / name
        exclusive(path, raw)
        access.check(path)
        if digest(regular(path, MAX_TOTAL)) != plan["output_hashes"][name]:
            raise ValueError("Una salida cambió después de escribirla.")
    if finish_check:
        finish_check()
    receipt = {
        "success": True,
        "owner": plan["owner"],
        "plan_sha256": expected_sha256,
        "outputs": plan["output_hashes"],
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "sdk_executed": False,
        "runtime_verified": False,
    }
    exclusive(destination / "receipt.json", canonical(receipt))
    return receipt


def prepare(config, config_raw, access):
    root = resolve_input(BASE, config["security_root"])
    access.check(root)
    proof_raw = regular(root / "root-identity-v1.json", 16384)
    if digest(proof_raw) != config["root_receipt_sha256"]:
        raise ValueError("La identidad raíz no coincide con su pin.")
    proof = json.loads(proof_raw)
    info = root.lstat()
    if (
        proof["root"] != str(root)
        or proof["owner"] != config["root_owner"]
        or proof["root_identity"] != {"device": info.st_dev, "inode": info.st_ino}
    ):
        raise ValueError("La raíz no tiene la identidad respaldada.")
    marker = root / proof["marker"]["name"]
    access.check(marker)
    marker_info = marker.lstat()
    if digest(regular(marker, 1024)) != proof["marker"]["sha256"] or (
        marker_info.st_dev,
        marker_info.st_ino,
    ) != (proof["marker"]["device"], proof["marker"]["inode"]):
        raise ValueError("El marcador propio cambió.")
    captured = []

    def read(spec):
        path = resolve_input(BASE, spec["path"])
        raw = regular(path, spec.get("maximum_bytes", 128 * 1024**2))
        if digest(raw) != spec["sha256"]:
            raise ValueError("La entrada no coincide con el SHA fijado: " + path.name)
        captured.append((path, spec["sha256"]))
        return raw

    source = read(config["source"])
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        members = [m for m in archive.getmembers() if m.name == config["source"]["lock_member"]]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 2 * 1024**2:
            raise ValueError("Cargo.lock fuente no es único o regular.")
        lock = archive.extractfile(members[0]).read()
    if digest(lock) != config["source"]["lock_sha256"]:
        raise ValueError("El lock original cambió.")
    old_inventory = json.loads(read(config["vendor_lock"]))
    if old_inventory["crate_count"] != 244:
        raise ValueError("Se requieren los 244 crates originales.")
    original_vendor = read(config["vendor_archive"])
    candidate_bytes = {}
    for row in config["changes"]:
        candidate_bytes[row["name"]] = read({"path": row["path"], "sha256": row["checksum"]})
    bundle = derive(lock, original_vendor, old_inventory, config["changes"], candidate_bytes)
    output = io.BytesIO()
    emit_vendor(bundle, output)
    vendor = output.getvalue()
    new_inventory = copy.deepcopy(old_inventory)
    new_inventory.update(
        owner=config["derived_owner"],
        crates=bundle["crates"],
        cargo_lock_sha256=digest(bundle["lock"]),
        vendor_sha256=digest(vendor),
        vendor_size=len(vendor),
        original_vendor_lock_sha256=config["vendor_lock"]["sha256"],
        security_clean=False,
    )
    outputs = {
        "Cargo.lock": bundle["lock"],
        "vendor.tar.gz": vendor,
        "vendor-lock.json": canonical(new_inventory),
        "build-script-audit.json": canonical(bundle["audit"]),
    }
    plan = {
        "schema_version": 1,
        "owner": config["derived_owner"],
        "derived_root": str(root / "derived-v1"),
        "root_identity_receipt_sha256": digest(proof_raw),
        "preparer_sha256": digest(regular(Path(__file__), 128 * 1024)),
        "inputs_manifest_sha256": digest(config_raw),
        "inputs": [{"path": str(p), "sha256": pin} for p, pin in captured],
        "output_hashes": {k: digest(v) for k, v in outputs.items()},
        "output_sizes": {k: len(v) for k, v in outputs.items()},
        "crate_count": 244,
        "unchanged_crates": 242,
        "changed_crates": [
            {k: row[k] for k in ("name", "old_version", "version", "checksum")}
            for row in config["changes"]
        ],
        "sdk_executed": False,
        "security_clean": False,
    }

    def revalidate():
        access.check(root)
        current = root.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("La raíz cambió de identidad.")
        for path, pin in captured:
            if digest(regular(path, 128 * 1024**2)) != pin:
                raise ValueError("Una entrada cambió después del plan.")

    revalidate()
    return plan, outputs, revalidate


def native_access(config):
    checked = []
    for spec in config["helpers"]:
        path = resolve_input(BASE, spec["path"])
        raw = regular(path, 256 * 1024)
        if digest(raw) != spec["sha256"]:
            raise ValueError("El helper privado no coincide con su pin.")
        checked.append((path, raw))
    path, raw = checked[0]
    namespace = {"__name__": "security_vendor_private_access", "__file__": str(path)}
    exec(compile(raw, str(path), "exec"), namespace)
    return namespace["NativeAccess"]()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write-plan", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    if bool(args.plan_sha256) != args.apply:
        parser.error("--apply exige --plan-sha256 y el modo plan no lo utiliza.")
    manifest_path = Path(__file__).with_name("security-vendor-inputs.json")
    config_raw = regular(manifest_path, 32768)
    config = json.loads(config_raw)
    if (
        config["schema_version"] != 1
        or {c["name"] for c in config["changes"]} != {"anyhow", "crossbeam-epoch"}
        or len(config["changes"]) != 2
    ):
        raise ValueError("El manifiesto no describe exactamente las dos correcciones aprobadas.")
    access = native_access(config)
    plan, outputs, revalidate = prepare(config, config_raw, access)
    plan_raw = canonical(plan)
    path = resolve_input(BASE, config["security_root"]) / "derived-v1.plan.json"
    if args.apply:
        saved = regular(path, 128 * 1024)
        if saved != plan_raw or digest(saved) != args.plan_sha256:
            raise ValueError("Plan respaldado distinto de las entradas actuales.")
        result = publish(
            Path(plan["derived_root"]), outputs, plan, args.plan_sha256, access, revalidate
        )
    elif args.write_plan:
        exclusive(path, plan_raw)
        access.check(path)
        result = {
            "plan_path": str(path),
            "plan_sha256": digest(plan_raw),
            "outputs": plan["output_hashes"],
            "derived_created": False,
        }
    else:
        result = {"plan": plan, "plan_sha256": digest(plan_raw), "derived_created": False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, tarfile.TarError) as error:
        print(
            json.dumps(
                {"success": False, "error": str(error), "sdk_executed": False}, ensure_ascii=False
            )
        )
        raise SystemExit(2) from None
