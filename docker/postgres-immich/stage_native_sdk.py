"""Descarga y cataloga entradas nativas fijadas; no ejecuta SDK ni instala APK."""

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import stat
import struct
import subprocess
import tarfile
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

WORKSPACE = Path(__file__).resolve().parents[2]
PINNED_HELPERS = {
    "prepare_vendor.py": "367e9f945bdc859e1cbd4816bb4db4ccd7b970b02f9da713a374158fd795c8be",
    "native_inputs.py": "b00b1a1a1822b13ed145128209664a15f3efa781b39cd850d0d63bcc0666192b",
}
TRUST_KEY_ID = "alpine-devel@lists.alpinelinux.org-6165ee59.rsa.pub"
ALPINE_PUBLIC_SUM256 = "207e4696d3c05f7cb05966aee557307151f1f00217af4143c1bcaf33b8df733f"
HOST = "x86_64-unknown-linux-musl"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def elf_info(stream):
    stream.seek(0)
    header = stream.read(64)
    if not header.startswith(b"\x7fELF"):
        return None
    if len(header) != 64 or header[4:6] != b"\x02\x01":
        raise ValueError("El SDK contiene ELF distinto de 64 bits little-endian.")
    machine = struct.unpack_from("<H", header, 18)[0]
    offset = struct.unpack_from("<Q", header, 32)[0]
    width, count = struct.unpack_from("<HH", header, 54)
    if machine != 62 or count > 2048 or (count and width != 56):
        raise ValueError("El SDK no cumple arquitectura/program headers amd64.")
    stream.seek(offset)
    raw = stream.read(width * count)
    if len(raw) != width * count:
        raise ValueError("ELF con program headers truncados.")
    loads, dynamic, interpreter = [], None, None
    for index in range(count):
        kind, _flags, off, address, _physical, size, _memory, _alignment = struct.unpack_from(
            "<IIQQQQQQ", raw, index * width
        )
        if kind == 1:
            loads.append((address, off, size))
        if kind == 2:
            dynamic = (off, size)
        if kind == 3:
            if size > 4096:
                raise ValueError("Intérprete ELF excesivo.")
            stream.seek(off)
            interpreter = stream.read(size).rstrip(b"\0").decode()
    entries = []
    if dynamic:
        if dynamic[1] > 1024**2:
            raise ValueError("Tabla dinámica ELF excesiva.")
        stream.seek(dynamic[0])
        raw = stream.read(dynamic[1])
        for off in range(0, len(raw) - 15, 16):
            tag, value = struct.unpack_from("<qQ", raw, off)
            if tag == 0:
                break
            entries.append((tag, value))
    tags, strings = dict(entries), b""
    if 5 in tags:
        if tags.get(10, 0) > 64 * 1024**2:
            raise ValueError("Tabla de cadenas ELF excesiva.")
        address = tags[5]
        matches = [
            off + address - start for start, off, size in loads if start <= address < start + size
        ]
        if len(matches) != 1:
            raise ValueError("Tabla de cadenas ELF no resoluble.")
        stream.seek(matches[0])
        strings = stream.read(tags.get(10, 0))

    def text(index):
        if index >= len(strings) or b"\0" not in strings[index:]:
            raise ValueError("Cadena ELF inválida.")
        return strings[index:].split(b"\0", 1)[0].decode()

    return {
        "machine": machine,
        "interpreter": interpreter,
        "needed": [text(value) for tag, value in entries if tag == 1],
        "soname": text(tags[14]) if 14 in tags else None,
        "glibc_versions": sorted({v.decode() for v in re.findall(rb"GLIBC_[0-9.]+", strings)}),
    }


def inspect_sdk(path, component, expected_sha256, *, max_bytes=8 * 1024**3):
    path = Path(path)
    if digest(path) != expected_sha256:
        raise ValueError("El SDK no coincide con el SHA-256 respaldado.")
    files, links, roles, total, prefix = [], [], {}, 0, None
    with tarfile.open(path, "r:xz") as archive:
        for count, member in enumerate(archive):
            if count >= 100000:
                raise ValueError("El inventario SDK excede el límite de entradas.")
            parts = member.name.removesuffix("/").split("/")
            if any(p in {"", ".", ".."} for p in parts) or any(
                ord(c) < 32 or c in "\\:" for c in member.name
            ):
                raise ValueError("El SDK contiene una ruta ambigua o fuera de su raíz.")
            if prefix is None:
                prefix = parts[0]
                if not prefix.startswith(component + "-") or not prefix.endswith(HOST):
                    raise ValueError("El SDK no tiene la raíz y host esperados.")
            if parts[0] != prefix or member.name in roles:
                raise ValueError("El SDK mezcla raíces o repite nombres.")
            role = "directory" if member.isdir() else "file"
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                if roles.get(parent) not in (None, "directory"):
                    raise ValueError("El SDK mezcla archivo y directorio.")
                roles[parent] = "directory"
            if roles.get(member.name) not in (None, role):
                raise ValueError("El SDK mezcla tipos de entrada.")
            roles[member.name] = role
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                target = member.linkname
                if target.startswith("/") or "\\" in target or any(ord(c) < 32 for c in target):
                    raise ValueError("El SDK contiene un enlace fuera de la raíz.")
                resolved = posixpath.normpath(
                    target
                    if member.islnk()
                    else posixpath.join(posixpath.dirname(member.name), target)
                )
                if resolved != prefix and not resolved.startswith(prefix + "/"):
                    raise ValueError("El SDK contiene un enlace fuera de la raíz.")
                links.append({"path": member.name, "target": resolved})
                continue
            if not member.isfile() or member.size < 0:
                raise ValueError("El SDK contiene un tipo especial.")
            total += member.size
            if total > max_bytes:
                raise ValueError("El SDK supera el presupuesto descomprimido.")
            with archive.extractfile(member) as stream:
                sha, size, is_elf = hashlib.sha256(), 0, False
                while data := stream.read(1024**2):
                    if size == 0:
                        is_elf = data.startswith(b"\x7fELF")
                    size += len(data)
                    sha.update(data)
                if size != member.size:
                    raise ValueError("Un archivo SDK está truncado.")
                metadata = elf_info(stream) if is_elf else None
            files.append(
                {"path": member.name, "size": size, "sha256": sha.hexdigest(), "elf": metadata}
            )
    destinations = {link["path"]: link["target"] for link in links}
    for link in links:
        current, seen = link["target"], set()
        while current in destinations:
            if current in seen:
                raise ValueError("El SDK contiene un ciclo de enlaces.")
            seen.add(current)
            current = destinations[current]
        if current not in roles:
            raise ValueError("Un enlace SDK no tiene destino dentro del archivo.")
    if not files:
        raise ValueError("El SDK no contiene archivos regulares.")
    return {
        "component": component,
        "archive_sha256": expected_sha256,
        "archive_size": path.stat().st_size,
        "files": files,
        "links": links,
        "total_uncompressed_bytes": total,
        "sdk_executed": False,
    }


def download_file(url, destination, expected_sha256=None, *, expected_size=None):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {"static.rust-lang.org", "dl-cdn.alpinelinux.org", "alpinelinux.org"}
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("La descarga no pertenece al origen HTTPS autorizado.")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("No se sobrescribe una descarga existente.")
    maximum = expected_size or 512 * 1024**2
    if type(maximum) is not int or not 0 < maximum <= 512 * 1024**2:
        raise ValueError("Tamaño de descarga fuera del límite.")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    command = [
        str(system / "System32/curl.exe"),
        "-q",
        "--fail",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--connect-timeout",
        "15",
        "--max-time",
        "180",
        "--max-filesize",
        str(maximum),
        "--write-out",
        "%{stderr}%{http_code}",
        url,
    ]
    with destination.open("xb") as stream:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.PIPE,
            timeout=182,
            check=True,
            env={"SystemRoot": str(system), "PATH": str(system / "System32")},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        stream.flush()
        os.fsync(stream.fileno())
    size = destination.stat().st_size
    if (
        result.stderr != b"200"
        or size > maximum
        or (expected_size is not None and size != expected_size)
    ):
        raise ValueError("La descarga no acredita HTTP 200 y tamaño esperado.")
    if expected_sha256 is not None and digest(destination) != expected_sha256:
        raise ValueError("El SHA-256 descargado no coincide.")
    return destination


def load_pinned_module(path, expected_sha256):
    path = Path(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & 0x400
        or digest(path) != expected_sha256
    ):
        raise ValueError("El verificador local no coincide con el código revisado.")
    spec = importlib.util.spec_from_file_location("native_stage_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish(stage, final, receipt):
    stage, final = Path(stage), Path(final)
    if final.exists() or final.is_symlink() or stage.parent != final.parent:
        raise ValueError("No se reemplaza un destino ni se publica fuera de la raíz propia.")
    with (stage / "receipt.json").open("xb") as stream:
        stream.write(
            json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())
    stage.rename(final)
    return receipt


def ordinary_file(path, expected_sha256, maximum):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & 0x400
        or info.st_size > maximum
        or digest(path) != expected_sha256
    ):
        raise ValueError("Una entrada local no es ordinaria o difiere de su pin.")
    return path.read_bytes()


def reuse_directory(vendor, name):
    if not re.fullmatch(r"\.native-staging-[0-9a-f]{32}", name):
        raise ValueError("Sólo se reutiliza un nombre de staging propio dentro de la raíz.")
    stage = vendor.ROOT / name
    for path in (stage, stage / "downloads"):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("El staging reutilizado contiene un enlace o tipo ajeno.")
    marker = stage / ".owner.json"
    identity = json.loads(ordinary_file(marker, digest(marker), 4096))
    if identity != {"owner": vendor.OWNER, "manifest_sha256": vendor.MANIFEST_SHA256}:
        raise ValueError("El staging reutilizado no pertenece al propietario y manifiesto exactos.")
    return stage / "downloads"


def source_sbom(packages, base, sdk, manifest_sha256):
    components = []
    for record in packages:
        name, version = record["name"], record["version"]
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "bom-ref": "apk:" + name,
                "purl": f"pkg:apk/alpine/{name}@{quote(version)}?arch=x86_64&distro=3.24",
                "hashes": [{"alg": "SHA-256", "content": record["archive_sha256"]}],
                "properties": [{"name": "agencia:alcance", "value": "sdk_no_instalado"}],
            }
        )
    for name, record in sorted(base.items()):
        components.append(
            {
                "type": "library",
                "name": name,
                "version": record["V"],
                "bom-ref": "base:" + name,
                "purl": f"pkg:apk/alpine/{name}@{quote(record['V'])}?arch=x86_64&distro=3.24",
                "properties": [{"name": "agencia:alcance", "value": "base_reutilizada"}],
            }
        )
    for component in sdk:
        components.append(
            {
                "type": "application",
                "name": "rust-" + component["component"],
                "version": component["version"],
                "bom-ref": "sdk:" + component["component"],
                "hashes": [{"alg": "SHA-256", "content": component["archive_sha256"]}],
                "licenses": [{"expression": "MIT OR Apache-2.0"}],
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "postgres-immich-sdk-staging",
                "version": manifest_sha256,
            },
            "properties": [
                {"name": "agencia:sdk_ejecutado", "value": "false"},
                {"name": "agencia:no_acredita_runtime_final", "value": "true"},
            ],
        },
        "components": components,
    }


def prepare_inputs(reuse_staging=None):
    stager_sha256 = digest(Path(__file__))
    helpers = {
        name: load_pinned_module(Path(__file__).with_name(name), sha)
        for name, sha in PINNED_HELPERS.items()
    }
    vendor, native = helpers["prepare_vendor.py"], helpers["native_inputs.py"]
    manifest = vendor.load_manifest(Path(__file__).with_name("musl-build-inputs.json"))
    vendor.verify_root()
    root = vendor.ROOT
    reuse = reuse_directory(vendor, reuse_staging) if reuse_staging is not None else None
    final = root / ("native-inputs-" + vendor.MANIFEST_SHA256[:16])
    if final.exists() or final.is_symlink():
        raise ValueError("Ya existe una preparación; no se adopta ni sobrescribe.")
    stage = root / (".native-staging-" + uuid.uuid4().hex)
    stage.mkdir()
    vendor.put_json(
        stage / ".owner.json", {"owner": vendor.OWNER, "manifest_sha256": vendor.MANIFEST_SHA256}
    )
    downloads = stage / "downloads"
    downloads.mkdir()
    try:
        key_path = downloads / TRUST_KEY_ID
        download_file(
            "https://alpinelinux.org/keys/" + TRUST_KEY_ID, key_path, ALPINE_PUBLIC_SUM256
        )
        trusted = {TRUST_KEY_ID: key_path.read_bytes()}
        indexes, main_records = {}, None
        for repository, entry in manifest["apk_indexes"].items():
            destination = downloads / (repository + "-APKINDEX.tar.gz")
            cached = (
                WORKSPACE
                / ".tools/cache/phase4-sources"
                / ("alpine3.24-" + repository + "-APKINDEX.tar.gz")
            )
            if cached.exists():
                data = ordinary_file(cached, entry["sha256"], entry["size"])
                with destination.open("xb") as stream:
                    stream.write(data)
            else:
                download_file(
                    entry["url"], destination, entry["sha256"], expected_size=entry["size"]
                )
                data = destination.read_bytes()
            authenticated = native.verified_control(data, trusted)
            if authenticated["payload"] or "APKINDEX" not in authenticated["files"]:
                raise ValueError("El índice no tiene el formato autenticado esperado.")
            indexes[repository] = {
                "sha256": digest(destination),
                "signing_key": authenticated["signing_key"],
                "key_sha256": authenticated["key_sha256"],
            }
            if repository == "main":
                main_records = native.parse_records(authenticated["files"]["APKINDEX"].decode())
        base_entry = manifest["base"]["postgres_sdk_files"]["lib/apk/db/installed"]
        base_path = root / "apk-sdk-inspection/base-installed"
        base_data = ordinary_file(base_path, base_entry["sha256"], base_entry["size"])
        base = native.parse_records(base_data.decode())
        seeds = {entry["name"]: entry["version"] for entry in manifest["apk_direct_inputs"]}
        for entry in manifest["apk_direct_inputs"]:
            if main_records[entry["name"]]["C"] != entry["index_checksum"]:
                raise ValueError("Un pin directo no coincide con el índice autenticado.")
        supplemental = {"isl26": "0.26-r2"}
        closure = native.resolve_closure(main_records, {**seeds, **supplemental}, base)
        if len(closure["packages"]) != 65 or len(closure["base_reused"]) != 20:
            raise ValueError("La clausura difiere de los 65 APK y 20 paquetes base revisados.")

        def get_package(record):
            name, version = record["P"], record["V"]
            if not re.fullmatch(r"[a-z0-9][a-z0-9+_.-]*", name) or not re.fullmatch(
                r"[A-Za-z0-9+_.-]+", version
            ):
                raise ValueError("Identidad APK no válida para una URL fija.")
            url = "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/" + quote(
                name + "-" + version + ".apk"
            )
            filename = name + "-" + version + ".apk"
            path = downloads / filename
            if reuse is not None:
                cached = reuse / filename
                data = ordinary_file(cached, digest(cached), int(record["S"]))
                if len(data) != int(record["S"]):
                    raise ValueError("Un APK reutilizado no cumple el tamaño autenticado.")
                native.verify_apk(data, trusted, record)
                with path.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                download_file(url, path, expected_size=int(record["S"]))
            return {
                **native.verify_apk(path.read_bytes(), trusted, record),
                "url": url,
                "size": path.stat().st_size,
                "license": record.get("L", ""),
                "origin": record.get("o", name),
            }

        packages = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            futures = [pool.submit(get_package, record) for record in closure["packages"].values()]
            for future in concurrent.futures.as_completed(futures):
                packages.append(future.result())
                if len(packages) % 10 == 0 or len(packages) == 65:
                    vendor.put_json(
                        stage / ("progress-" + str(len(packages)) + ".json"),
                        {"apk_verified": len(packages), "sdk_executed": False},
                    )
                    print(json.dumps({"apk_verified": len(packages), "total": 65}), flush=True)
        packages.sort(key=lambda p: p["name"])
        catalog, sdk = {}, []
        for component, entry in manifest["rust_sdk"]["components"].items():
            path = downloads / (component + ".tar.xz")
            cached = reuse / path.name if reuse is not None else None
            if cached is not None and cached.exists():
                data = ordinary_file(cached, entry["sha256"], 512 * 1024**2)
                with path.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                download_file(entry["url"], path, entry["sha256"])
            item = inspect_sdk(path, component, entry["sha256"])
            catalog[component] = item
            sdk.append(
                {k: v for k, v in item.items() if k not in {"files", "links"}}
                | {
                    "version": entry["version"],
                    "url": entry["url"],
                    "file_count": len(item["files"]),
                    "link_count": len(item["links"]),
                }
            )
            print(
                json.dumps(
                    {
                        "sdk_catalogued": component,
                        "files": len(item["files"]),
                        "sdk_executed": False,
                    }
                ),
                flush=True,
            )
        lock = {
            "schema_version": 1,
            "manifest_sha256": vendor.MANIFEST_SHA256,
            "stager_sha256": stager_sha256,
            "helper_sha256": PINNED_HELPERS,
            "trust_key_sha256": ALPINE_PUBLIC_SUM256,
            "indexes": indexes,
            "base_installed_sha256": base_entry["sha256"],
            "supplemental_pins": supplemental,
            "packages": packages,
            "base_reused": {name: base[name] for name in closure["base_reused"]},
            "dependency_edges": closure["edges"],
            "rust_sdk": sdk,
            "sdk_executed": False,
            "compiler_executed": False,
            "installed": False,
            "native_solver_confirmation_pending": True,
            "native_apk_verify_pending": True,
            "security_clean": False,
            "build_enabled": False,
            "runtime_verified": False,
        }
        vendor.put_json(stage / "native-sdk-lock.json", lock)
        vendor.put_json(stage / "sdk-files.json", catalog)
        vendor.put_json(
            stage / "sdk-source-sbom.cdx.json",
            source_sbom(packages, lock["base_reused"], sdk, vendor.MANIFEST_SHA256),
        )
        receipt = {
            "success": True,
            "owner": vendor.OWNER,
            "manifest_sha256": vendor.MANIFEST_SHA256,
            "path": str(final),
            "apk_verified": len(packages),
            "base_reused": len(lock["base_reused"]),
            "sdk_components": len(sdk),
            "files": {
                name: digest(stage / name)
                for name in ("native-sdk-lock.json", "sdk-files.json", "sdk-source-sbom.cdx.json")
            },
            "sdk_executed": False,
            "compiler_executed": False,
            "installed": False,
            "security_clean": False,
            "build_enabled": False,
        }
        vendor.verify_root()
        return publish(stage, final, receipt)
    except Exception as error:
        vendor.put_json(
            stage / "failure.json",
            {
                "success": False,
                "error": vendor.error_fields(error),
                "sdk_executed": False,
                "compiler_executed": False,
                "installed": False,
            },
        )
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--reuse-staging")
    args = parser.parse_args(argv)
    if not args.prepare:
        print(
            json.dumps(
                {
                    "mode": "plan",
                    "apk_count": 65,
                    "sdk_components": 3,
                    "sdk_executed": False,
                    "installed": False,
                    "build_enabled": False,
                }
            )
        )
        return 0
    try:
        print(json.dumps(prepare_inputs(args.reuse_staging)), flush=True)
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "sdk_executed": False,
                    "compiler_executed": False,
                    "installed": False,
                }
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
