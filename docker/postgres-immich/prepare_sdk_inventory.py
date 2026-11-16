"""Inventario estático del SDK detenido; no inicia procesos, red ni escáneres."""

from __future__ import annotations

import argparse
import base64
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
from urllib.parse import quote

FIELDS = ("V", "A", "L", "o", "C")
HEADER = frozenset("PVACLoUcT")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("sdk-inventory-inputs.json")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def decode(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Clave JSON duplicada.")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique)


def regular(path: Path, expected: str, maximum: int) -> bytes:
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise ValueError("Pin SHA256 inválido.")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        raise ValueError("Entrada no regular, enlazada o demasiado grande.")
    if getattr(before, "st_file_attributes", 0) & 0x400:
        raise ValueError("Entrada con reparse point.")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        finished = os.fstat(stream.fileno())
    after = path.lstat()

    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)

    if len(raw) > maximum or not identity(before) == identity(opened) == identity(
        finished
    ) == identity(after):
        raise ValueError("Entrada cambió durante la lectura.")
    if sha256(raw) != expected:
        raise ValueError("SHA256 de entrada diferente del pin.")
    return raw


def parse_installed(raw: bytes, maximum: int = 2 * 1024 * 1024) -> dict:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum or b"\x00" in raw:
        raise ValueError("Base APK vacía, inválida o fuera de límite.")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("Base APK no UTF8.") from error
    if "\r" in text:
        raise ValueError("La base instalada debe conservar sus LF originales.")
    packages = {}
    for block in text.strip("\n").split("\n\n"):
        row = {}
        for line in block.splitlines():
            if line.startswith("F:"):
                break  # Después comienzan directorios/archivos y sus campos repetidos.
            if len(line) < 2 or line[1] != ":":
                raise ValueError("Cabecera APK mal formada.")
            if line[0] in HEADER:
                if line[0] in row:
                    raise ValueError("Campo de cabecera APK duplicado.")
                row[line[0]] = line[2:]
        if not all(row.get(key) for key in "PVAC"):
            raise ValueError("Paquete sin identidad completa.")
        if not re.fullmatch(r"\.?[a-z0-9][a-z0-9+_.-]*", row["P"]):
            raise ValueError("Nombre APK no canónico.")
        if row["A"] not in ("x86_64", "noarch"):
            raise ValueError("Arquitectura fuera del SDK acreditado.")
        try:
            control = base64.b64decode(row["C"][2:], validate=True)
        except ValueError as error:
            raise ValueError("Identidad Q1 inválida.") from error
        if not row["C"].startswith("Q1") or len(control) != 20:
            raise ValueError("Identidad Q1 inválida.")
        if row["P"] in packages:
            raise ValueError("Paquete APK duplicado.")
        packages[row["P"]] = row
    return packages


def capture_database(capture: dict) -> bytes:
    if capture.get("schema_version") != 1 or capture.get("read_only") is not True:
        raise ValueError("La captura no acredita lectura.")
    if any(
        capture.get(key) is not False
        for key in ("sdk_started", "sdk_executed", "container_mutated")
    ):
        raise ValueError("La captura declara ejecución o mutación.")
    container = capture.get("container", {})
    if (
        container.get("running") is not False
        or container.get("no_foreign") is not True
        or type(container.get("pid")) is not int
        or container["pid"] != 0
    ):
        raise ValueError("El SDK capturado no estaba detenido y acreditado.")
    code = capture.get("process", {}).get("returncode")
    if type(code) is not int or code != 0:
        raise ValueError("La captura no terminó correctamente.")
    raw = base64.b64decode(capture["database_b64"], validate=True)
    if len(raw) != capture["database_bytes"] or sha256(raw) != capture["database_sha256"]:
        raise ValueError("Los bytes de la base no coinciden con la captura.")
    return raw


def expected_union(base: dict, lock: dict) -> tuple[dict, dict]:
    expected = {name: dict(row) for name, row in base.items()}
    added = {}
    for package in lock["packages"]:
        name = package["name"]
        if name in expected or not SHA256.fullmatch(package["archive_sha256"]):
            raise ValueError("Paquete agregado duplicado o sin SHA256.")
        expected[name] = {
            "P": name,
            "V": package["version"],
            "A": package["arch"],
            "L": package["license"],
            "o": package["origin"],
            "C": package["control_identity"],
        }
        added[name] = package
    return expected, added


def compare_packages(expected: dict, actual: dict) -> dict:
    missing = sorted(expected.keys() - actual.keys())
    unexpected = sorted(actual.keys() - expected.keys())
    different = []
    for name in sorted(expected.keys() & actual.keys()):
        for field in FIELDS:
            if expected[name].get(field, "") != actual[name].get(field, ""):
                different.append(
                    {
                        "name": name,
                        "field": field,
                        "declared": expected[name].get(field, ""),
                        "observed": actual[name].get(field, ""),
                    }
                )
    return {
        "matches": not (missing or unexpected or different),
        "declared": len(expected),
        "observed": len(actual),
        "compared_fields": list(FIELDS),
        "missing": missing,
        "unexpected": unexpected,
        "different": different,
    }


def apk_component(row: dict, os_version: str, source: str, archive: dict | None) -> dict:
    name, version, arch = row["P"], row["V"], row["A"]
    purl = f"pkg:apk/alpine/{quote(name, safe='.-_')}@{quote(version, safe='.-_')}?arch={arch}&distro={os_version}"
    props = {
        "agencia:apk:origin_raw": row.get("o", ""),
        "agencia:apk:license_raw": row.get("L", ""),
        "agencia:apk:control_identity": row["C"],
        "agencia:apk:declared_source": source,
        "agencia:apk:homepage": row.get("U", ""),
        "agencia:apk:source_commit": row.get("c", ""),
    }
    if row.get("o"):
        props["aquasecurity:trivy:SrcName"] = row["o"]
        props["aquasecurity:trivy:SrcVersion"] = version
    component = {"type": "library", "bom-ref": purl, "name": name, "version": version, "purl": purl}
    if row.get("L"):
        component["licenses"] = [{"license": {"name": row["L"]}}]
    if archive is not None:
        component["hashes"] = [{"alg": "SHA-256", "content": archive["archive_sha256"]}]
        component["externalReferences"] = [{"type": "distribution", "url": archive["url"]}]
    component["properties"] = [
        {"name": key, "value": value} for key, value in sorted(props.items())
    ]
    return component


def make_sbom(
    actual: dict, base_names: set, added: dict, os_version: str, database_sha: str
) -> dict:
    components = [
        {
            "type": "operating-system",
            "bom-ref": "sdk-os:alpine",
            "name": "alpine",
            "version": os_version,
        }
    ]
    components.extend(
        apk_component(row, os_version, "base" if name in base_names else "added", added.get(name))
        for name, row in sorted(actual.items())
    )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "postgres-immich-stopped-sdk-apk-inventory",
                "version": database_sha,
            },
            "tools": {
                "components": [
                    {"type": "application", "name": "agency-sdk-inventory", "version": "1"}
                ]
            },
            "properties": [
                {
                    "name": "agencia:alcance",
                    "value": "110 APK observados; Rust y bibliotecas manuales separados",
                },
                {"name": "agencia:approval", "value": "pending"},
            ],
        },
        "components": components,
        "dependencies": [
            {"ref": "sdk-os:alpine", "dependsOn": [c["bom-ref"] for c in components[1:]]}
        ],
    }


def validate_scan_coverage(report: dict, actual: dict, os_version: str = "3.24.4") -> dict:
    os_info = report.get("Metadata", {}).get("OS", {})
    if os_info.get("Family") != "alpine" or os_info.get("Name") != os_version:
        raise ValueError("El escáner no acredita el OS fijado.")
    observed = {}
    vulnerabilities = []
    for result in report.get("Results", []):
        if result.get("Class") != "os-pkgs" or result.get("Type") != "alpine":
            if result.get("Packages"):
                raise ValueError("El informe contiene otro ecosistema.")
            continue
        for row in result.get("Packages", []):
            name = row.get("Name")
            if name in observed or name not in actual:
                raise ValueError("El escáner duplicó o agregó un paquete.")
            expected = actual[name]
            if (row.get("Version"), row.get("Arch")) != (expected["V"], expected["A"]):
                raise ValueError("Versión o arquitectura omitida/diferente.")
            if expected.get("o") and (row.get("SrcName"), row.get("SrcVersion")) != (
                expected["o"],
                expected["V"],
            ):
                raise ValueError("Origen Alpine perdido o diferente.")
            observed[name] = row
        vulnerabilities.extend(result.get("Vulnerabilities") or [])
    if observed.keys() != actual.keys():
        raise ValueError("La cobertura del escáner no es completa.")
    return {
        "packages": len(observed),
        "vulnerabilities": len(vulnerabilities),
        "high_critical": sum(v.get("Severity") in ("HIGH", "CRITICAL") for v in vulnerabilities),
        "approved": False,
        "scope": "Sólo cobertura APK; no acredita Rust, ELF manuales o aprobación Root.",
    }


def base_os(raw: bytes, manifest_sha: str, config_sha: str) -> dict:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as outer:
        members = {m.name: m for m in outer}
        if len(members) != len(outer.getmembers()):
            raise ValueError("Nombre duplicado en el archivo OCI.")

        def member(name):
            info = members[name]
            if not info.isfile() or info.size > 256 * 1024 * 1024:
                raise ValueError("Blob OCI no regular o fuera de límite.")
            return outer.extractfile(info).read()

        def blob(digest):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Digest OCI no válido.")
            data = member("blobs/sha256/" + digest[7:])
            if sha256(data) != digest[7:]:
                raise ValueError("Blob OCI alterado.")
            return data

        index = decode(member("index.json"))
        if [m["digest"] for m in index["manifests"]] != ["sha256:" + manifest_sha]:
            raise ValueError("Índice OCI fuera del subject esperado.")
        manifest = decode(blob("sha256:" + manifest_sha))
        if manifest["config"]["digest"] != "sha256:" + config_sha:
            raise ValueError("Config OCI diferente.")
        config = decode(blob("sha256:" + config_sha))
        layers = manifest["layers"]
        diffs = config["rootfs"]["diff_ids"]
        if len(layers) != len(diffs):
            raise ValueError("Capas y DiffIDs no coinciden.")
        files = {}
        for layer, diff in zip(layers, diffs, strict=True):
            compressed = blob(layer["digest"])
            if compressed.startswith(b"\x1f\x8b"):
                with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                    plain = stream.read(384 * 1024 * 1024 + 1)
            else:
                plain = compressed
            if len(plain) > 384 * 1024 * 1024 or sha256(plain) != diff.removeprefix("sha256:"):
                raise ValueError("DiffID no corresponde a bytes descomprimidos acotados.")
            with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as archive:
                for entry in archive:
                    name = entry.name.removeprefix("./")
                    if name in ("etc/alpine-release", "usr/lib/os-release", "etc/os-release"):
                        if entry.issym():
                            files[name] = {"link": entry.linkname}
                        elif entry.isfile() and entry.size <= 4096:
                            content = archive.extractfile(entry).read()
                            files[name] = {
                                "sha256": sha256(content),
                                "content": content.decode("utf-8", errors="strict"),
                            }
                        else:
                            raise ValueError("Identidad de OS no regular.")
                    if name.startswith("etc/.wh.") or name.startswith("usr/lib/.wh."):
                        target = name.replace("/.wh.", "/", 1)
                        if target in files:
                            del files[target]
        if files.get("etc/os-release") != {"link": "../usr/lib/os-release"}:
            raise ValueError("Enlace os-release diferente de la base fijada.")
        release = files["etc/alpine-release"]["content"].strip()
        os_release = files["usr/lib/os-release"]["content"].splitlines()
        if "ID=alpine" not in os_release or f"VERSION_ID={release}" not in os_release:
            raise ValueError("Identidad Alpine inconsistente.")
        return {
            "family": "alpine",
            "version": release,
            "base_manifest_sha256": manifest_sha,
            "base_config_sha256": config_sha,
            "layers_hashed": len(layers),
            "files": files,
            "scope": "Archivos de la base OCI y versión del paquete instalado; no lectura nueva del filesystem del SDK.",
        }


def non_apk(lock: dict, channel_raw: bytes, formatter: dict, compile_profile: dict) -> dict:
    channel = tomllib.loads(channel_raw.decode("utf-8", errors="strict"))
    historical_sdk = compile_profile["backed"]["sdk"]["receipt"]
    if historical_sdk["success"] is not True or historical_sdk["artifact"]["verified"] is not True:
        raise ValueError("No hay aceptación histórica del SDK.")
    components = []
    for row in lock["rust_sdk"]:
        package = channel["pkg"][row["component"]]
        target = package["target"]["x86_64-unknown-linux-musl"]
        if (row["archive_sha256"], row["url"], row["version"]) != (
            target["xz_hash"],
            target["xz_url"],
            package["version"],
        ):
            raise ValueError("Rust no coincide con el manifiesto fechado.")
        components.append(
            {
                **row,
                "git_commit": package["git_commit_hash"],
                "identity_type": "Rust distribution, no APK/crate",
                "osv_package_identity": None,
            }
        )
        if row["component"] in historical_sdk["artifact"]["versions"]:
            components[-1]["observed_cli_version"] = historical_sdk["artifact"]["versions"][
                row["component"]
            ]
    package = channel["pkg"]["rustfmt-preview"]
    target = package["target"]["x86_64-unknown-linux-musl"]
    if (
        formatter["archive_sha256"],
        formatter["archive_url"],
        formatter["rustfmt_version"],
        formatter["rust_commit"],
    ) != (target["xz_hash"], target["xz_url"], package["version"], package["git_commit_hash"]):
        raise ValueError("rustfmt no coincide con el manifiesto fechado.")
    artifact = compile_profile["augmentation"]["augment"]["receipt"]["artifact"]
    if (
        artifact["files"] != formatter["files_to_copy"]
        or artifact["driver"] != formatter["driver"]
        or artifact["verified"] is not True
    ):
        raise ValueError("Fingerprint histórico de rustfmt/driver diferente.")
    proof = compile_profile["augmentation"]["restart"]["receipt"]["proof"]
    components.append(
        {
            "component": "rustfmt-preview",
            "distribution_version": package["version"],
            "observed_cli_version": artifact["version"],
            "archive_sha256": target["xz_hash"],
            "url": target["xz_url"],
            "git_commit": package["git_commit_hash"],
            "files": formatter["files_to_copy"],
            "driver": formatter["driver"],
            "osv_package_identity": None,
        }
    )
    return {
        "channel_date": channel["date"],
        "channel_sha256": sha256(channel_raw),
        "components": components,
        "historical_sdk_artifact_receipt_sha256": historical_sdk["artifact_receipt_sha256"],
        "historical_rust_payload": {
            "files": proof["rust_payload_files"],
            "tree_sha256": proof["rust_payload_sha256"],
        },
        "fresh_binary_inspection": False,
        "approved": False,
        "scope": "Procedencia y fingerprints históricos fijados; no auditoría universal de LLVM/Cargo/stdlib ni ejecución nueva.",
    }


def prepare(manifest_path: Path) -> tuple[dict, dict, dict]:
    manifest_raw = manifest_path.read_bytes()
    manifest = decode(manifest_raw)
    data = {}
    provenance = {}
    for name, pin in manifest["inputs"].items():
        path = ROOT / pin["path"]
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("Entrada fuera del workspace.")
        data[name] = regular(path, pin["sha256"], pin["maximum_bytes"])
        provenance[name] = {
            "path": pin["path"],
            "sha256": sha256(data[name]),
            "bytes": len(data[name]),
        }
    capture = decode(data["capture"])
    raw = capture_database(capture)
    actual = parse_installed(raw)
    base = parse_installed(data["base_installed"])
    lock = decode(data["sdk_lock"])
    if sha256(data["base_installed"]) != lock["base_installed_sha256"]:
        raise ValueError("La base no coincide con el lock SDK.")
    expected, added = expected_union(base, lock)
    if (len(actual), len(base), len(added)) != (110, 45, 65):
        raise ValueError("El conteo no corresponde a la captura fijada 110/45/65.")
    os_proof = base_os(
        data["base_archive"], manifest["base_manifest_sha256"], manifest["base_config_sha256"]
    )
    if actual["alpine-release"]["V"] != os_proof["version"] + "-r0":
        raise ValueError("El paquete instalado declara otra versión de OS.")
    formatter = decode(data["formatter_inspection"])
    nonapk = non_apk(lock, data["rust_channel"], formatter, decode(data["compile_profile"]))
    inventory = {
        "schema_version": 1,
        "approved": False,
        "sdk_started": False,
        "sdk_executed": False,
        "scanner_executed": False,
        "cid": capture["cid"],
        "container": capture["container"],
        "database_sha256": sha256(raw),
        "database_bytes": len(raw),
        "inputs": provenance,
        "comparison": compare_packages(expected, actual),
        "os": os_proof,
        "packages": [
            {
                "name": name,
                "version": row["V"],
                "arch": row["A"],
                "license_raw": row.get("L", ""),
                "origin_raw": row.get("o", ""),
                "homepage": row.get("U", ""),
                "source_commit": row.get("c", ""),
                "control_identity": row["C"],
                "declared_source": "base" if name in base else "added",
                "archive": added.get(name),
            }
            for name, row in sorted(actual.items())
        ],
        "non_apk": nonapk,
        "primary_research": manifest["primary_research"],
        "limitations": manifest["limitations"],
    }
    sbom = make_sbom(actual, set(base), added, os_proof["version"], sha256(raw))
    profile = {
        "schema_version": 1,
        "operation": "Root-only proposal; no scan executed",
        "approved": False,
        "expected_os": {"family": "alpine", "version": os_proof["version"]},
        "expected_apk_count": len(actual),
        "required_report_flags": ["--list-all-pkgs", "--format", "json"],
        "severity_gate": ["HIGH", "CRITICAL"],
        "ignore_unfixed": False,
        "coverage": "Exact 110 names/versions/architectures and declared SrcName; duplicates/extras/omissions rejected.",
        "third_party_sbom_warning": "Conservar: inventario construido desde captura APK, no generado por Trivy.",
        "non_apk_gate": "Revisión Root separada de Rust/rustfmt/driver y componentes manuales; 0 APK findings no los acredita.",
        "database": manifest["proposed_database"],
        "source_manifest_sha256": sha256(manifest_raw),
        "inventory_sha256": sha256(encode(inventory)),
        "sbom_sha256": sha256(encode(sbom)),
        "subject": {
            "cid": capture["cid"],
            "container": capture["container"],
            "capture_sha256": provenance["capture"]["sha256"],
            "database_sha256": sha256(raw),
        },
    }
    return inventory, sbom, profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--output-prefix", type=Path, help="Publica tres JSON nuevos, nunca sobrescribe."
    )
    parser.add_argument(
        "--validate-report", type=Path, help="Valida datos de un escaneo efectuado aparte por Root."
    )
    arguments = parser.parse_args()
    inventory, sbom, profile = prepare(arguments.manifest)
    if arguments.validate_report:
        rows = {
            row["name"]: {"V": row["version"], "A": row["arch"], "o": row["origin_raw"]}
            for row in inventory["packages"]
        }
        coverage = validate_scan_coverage(
            decode(arguments.validate_report.read_bytes()), rows, inventory["os"]["version"]
        )
        print(json.dumps(coverage, ensure_ascii=False))
        return 0 if coverage["high_critical"] == 0 else 1
    if arguments.output_prefix:
        paths = [
            Path(str(arguments.output_prefix) + suffix)
            for suffix in (".inventory.json", ".cdx.json", ".profile.json")
        ]
        if any(path.exists() for path in paths):
            raise ValueError("Salida existente; usar otro prefijo, sin sobrescribir evidencia.")
        for path, value in zip(paths, (inventory, sbom, profile), strict=True):
            raw = encode(value)
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            print(json.dumps({"path": str(path), "sha256": sha256(raw), "bytes": len(raw)}))
    print(
        json.dumps(
            {
                "apk_count": len(inventory["packages"]),
                "matches": inventory["comparison"]["matches"],
                "os": inventory["os"]["version"],
                "approved": False,
                "scanner_executed": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
