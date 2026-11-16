"""Inventario nativo de un wheel propio; no ejecuta scanners, consultas ni ELF."""

import argparse
import base64
import csv
import email.parser
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import types
import zipfile
import zlib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SPEC_PATH = "docker/kitsu-zou/opencv-wheel-inventory-spec.json"
OWNER = "agencia-digital-marketing-ai:opencv-wheel-inventory:v1"
OUTPUT_RELATIVE = ".tools/backup-source-build/opencv-wheel-inventory-v1"
RELOCATOR_SHA = "b7b14e650a0eb900f585591077680fa91a335c0224057ad99cb24e68afd771f6"
PARSER_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
PACKAGER_SHA = "831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9"
WHEEL_SHA = "1c03ca69b42b10ca1ff75bdc64113ca2885d7f502edbba369b9779f2da1b51cd"
RECEIPT_SHA = "b88343d49decd86328a6d74d4c02b01789c83ed4d12ce7339a81d38eeada3c18"
APPROVAL_SHA = "666f51e18365eb14a4d1c386fdf8c803e2b01076db75e9e296e4f5898a78ecca"
DIST = "opencv_python_headless-4.13.0.92+agency1.dist-info"
NUMPY_REQUIRES = ['numpy<2.0; python_version < "3.9"', 'numpy>=2; python_version >= "3.9"']
RPATH = "$ORIGIN/opencv_python_headless.libs:$ORIGIN"
ACL_SOURCE_SHA256 = "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b"
SOURCE_SHA = "6a7508554941c1a698c243b2212b2985ce59a65a3e0348d53c2158607d801e61"
MANIFEST_SHA = "1207f5840f3d663775ee54fd377688ef00081681a1a50952df61c5d1ee01cd3a"
EXTERNAL_NAMES = {
    "imath",
    "libdeflate-libs",
    "libopenjph0.32",
    "libpng16-16",
    "libstdc++",
    "openexr-libiex",
    "openexr-libilmthread",
    "openexr-libopenexr",
    "openexr-libopenexrcore",
    "python-3.12-base",
}
DOCUMENTS = {"inventory.json", "wheel-source.cdx.json", "osv-requests.json"}
REFERENCE_PATHS = {
    "wheel": ".tools/backup-source-build/opencv-relocated-wheel-v1/wheel/opencv_python_headless-4.13.0.92+agency1-cp312-cp312-linux_x86_64.whl",
    "relocation_receipt": ".tools/backup-source-build/opencv-relocated-wheel-v1/receipt.json",
    "manifest": ".tools/backup-source-build/opencv-relocation-inputs-v1/manifest-approved.json",
    "sdk_approval": ".tools/evidence/phase4-zou-opencv-openexr3-sdk-root-approval-v1.json",
    "sdk_cdx": ".tools/evidence/phase4-zou-opencv-openexr3-inputs-current-sca-v1.cdx.json",
    "sdk_sca_receipt": ".tools/evidence/phase4-zou-opencv-openexr3-inputs-current-sca-v1.receipt.json",
    "sdk_stage": ".tools/evidence/phase4-zou-opencv-openexr3-sdk-v1.sdk.b8549de8bb885ffd.json",
    "relocator": "docker/kitsu-zou/prepare_opencv_relocated_wheel.py",
    "parser": "docker/postgres-immich/final_image_remote.py",
    "packager": "docker/kitsu-zou/package_opencv_minimal.py",
    "source_archive": ".tools/zou-native-research-v1/opencv-4.13.0-fe38fc6.tar.gz",
}


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def json_object(raw):
    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "JSON duplicado.")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _s: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


def safe_name(name):
    require(
        isinstance(name, str)
        and name
        and not name.startswith("/")
        and "\\" not in name
        and ":" not in name
        and all(p not in ("", ".", "..") for p in name.split("/")),
        "Nombre de archivo inseguro.",
    )
    return name


def read_members(raw, expected):
    require(0 < len(raw) <= 64 * 1024**2 and 0 < len(expected) <= 64, "ZIP fuera de límites.")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        rows = archive.infolist()
        names = [r.filename for r in rows]
        require(
            len(names) == len(set(names)) and set(names) == set(expected),
            "Conjunto ZIP distinto de los pins.",
        )
        require(sum(r.file_size for r in rows) <= 256 * 1024**2, "ZIP expandido excesivo.")
        files = {}
        for row in rows:
            safe_name(row.filename)
            mode = (row.external_attr >> 16) & 0xFFFF
            require(
                not row.is_dir()
                and not row.flag_bits & 1
                and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                and 0 < row.file_size <= 128 * 1024**2,
                "Miembro ZIP no regular o excesivo.",
            )
            data = archive.read(row)
            require(
                len(data) == expected[row.filename]["bytes"]
                and sha(data) == expected[row.filename]["sha256"],
                "Miembro ZIP no coincide con tamaño/SHA.",
            )
            files[row.filename] = data
        return files


def validate_record(files, dist):
    record = dist + "/RECORD"
    rows = list(csv.reader(io.StringIO(files[record].decode("utf-8"))))
    require(
        all(len(r) == 3 for r in rows)
        and len(rows) == len(files)
        and len({r[0] for r in rows}) == len(rows)
        and {r[0] for r in rows} == set(files),
        "RECORD incompleto o duplicado.",
    )
    for name, checksum, size in rows:
        if name == record:
            require(checksum == size == "", "RECORD propio inválido.")
        else:
            require(
                size == str(len(files[name]))
                and checksum
                == "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest())
                .decode()
                .rstrip("="),
                "RECORD no coincide con bytes.",
            )


def wheel_metadata(raw):
    text = raw.decode("utf-8", "strict")
    message = email.parser.Parser().parsestr(text)
    for key in ("Name", "Version", "Requires-Python", "Metadata-Version"):
        require(len(message.get_all(key, [])) == 1, "Header METADATA ambiguo.")
    require(
        message["Name"] == "opencv-python-headless"
        and message["Version"] == "4.13.0.92+agency1"
        and message["Requires-Python"] == ">=3.12,<3.13"
        and message.get_all("Requires-Dist", []) == NUMPY_REQUIRES,
        "METADATA no conserva identidad/requisitos propios.",
    )
    return {
        "name": message["Name"],
        "version": message["Version"],
        "requires_python": message["Requires-Python"],
        "requires_dist": NUMPY_REQUIRES,
        "license_expression": message.get("License-Expression"),
        "official_pypi_artifact": False,
        "identity_kind": "own_native_distribution",
    }


def gzip_sections(raw):
    require(0 < len(raw) <= 64 * 1024**2, "APK comprimido excesivo.")
    result = []
    remaining = raw
    while remaining:
        require(len(result) < 3, "Demasiados miembros gzip.")
        try:
            decoder = zlib.decompressobj(31)
            plain = decoder.decompress(remaining, 128 * 1024**2 + 1)
        except zlib.error as error:
            raise ValueError("gzip APK inválido.") from error
        require(
            len(plain) <= 128 * 1024**2 and decoder.eof and not decoder.unconsumed_tail,
            "gzip truncado o excesivo.",
        )
        consumed = len(remaining) - len(decoder.unused_data)
        require(consumed > 0, "gzip sin progreso.")
        result.append((remaining[:consumed], plain))
        remaining = decoder.unused_data
    require(len(result) == 2, "Sólo se admite el APK Wolfi de dos gzip fijado.")
    return result


def tar_files(raw):
    require(0 < len(raw) <= 128 * 1024**2, "TAR de payload excesivo.")
    files = {}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for item in archive:
            name = item.name.rstrip("/")
            if name in (".", "") and item.isdir():
                continue
            safe_name(name)
            require(name not in seen and len(seen) < 20000, "TAR duplicado o excesivo.")
            seen.add(name)
            require(item.isdir() or item.isreg() or item.issym(), "Tipo TAR no admitido.")
            if item.isreg():
                require(0 <= item.size <= 128 * 1024**2, "Miembro TAR excesivo.")
                with archive.extractfile(item) as stream:
                    data = stream.read(item.size + 1)
                require(len(data) == item.size, "Miembro TAR truncado.")
                files[name] = data
    return files


def apk_payload(raw, record):
    (control_compressed, control), (payload_compressed, payload) = gzip_sections(raw)
    control_files = tar_files(control)
    require(".PKGINFO" in control_files, "Falta control Wolfi.")
    values = {}
    for line in control_files[".PKGINFO"].decode("utf-8", "strict").splitlines():
        if not line or line.startswith("#"):
            continue
        require(" = " in line, "PKGINFO no canónico.")
        key, value = line.split(" = ", 1)
        values.setdefault(key, []).append(value)
    for key, field in (("pkgname", "P"), ("pkgver", "V"), ("arch", "A"), ("license", "L")):
        require(
            values.get(key) == [record[field]],
            "Identidad APK no coincide con registro autenticado.",
        )
    require(
        "Q1" + base64.b64encode(hashlib.sha1(control_compressed).digest()).decode() == record["C"],
        "Q1 de control distinto del índice.",
    )
    require(
        values.get("datahash") == [sha(payload_compressed)],
        "SHA256 del payload comprimido distinto.",
    )
    return tar_files(payload), {
        "archive_sha256": sha(raw),
        "payload_tar_sha256": sha(payload),
        "control_q1_matches": True,
        "payload_sha256_matches": True,
        "fresh_signature_verification": False,
        "authentication": "pinned_archive_and_prior_index_receipt_reference",
    }


def resolve_dependencies(bundled, catalog, declared):
    internal = {row["soname"] for row in bundled.values() if row.get("soname")}
    providers = {}
    for row in catalog:
        name = row["soname"]
        require(name not in internal, "Proveedor externo colisiona con ELF del wheel.")
        if name in providers:
            require(
                providers[name]["sha256"] == row["sha256"],
                "Dos proveedores con mismo SONAME y bytes diferentes.",
            )
        else:
            providers[name] = row
    pending = [dep for row in bundled.values() for dep in row["needed"]]
    seen = set()
    confirmed = set()
    missing = []
    edges = []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in internal:
            edges.append({"soname": name, "scope": "wheel_bundled"})
            continue
        if name in providers:
            confirmed.add(name)
            edges.append(
                {
                    "soname": name,
                    "scope": "external_candidate_by_bytes",
                    "sha256": providers[name]["sha256"],
                }
            )
            pending.extend(providers[name]["needed"])
        else:
            candidates = declared.get(name, [])
            missing.append(
                {
                    "soname": name,
                    "status": "declared_sdk_without_payload_bytes"
                    if candidates
                    else "missing_provider_identity_and_bytes",
                    "declared_candidates": candidates,
                }
            )
    return {
        "edges": sorted(edges, key=lambda r: r["soname"]),
        "external_confirmed": sorted(confirmed),
        "unconfirmed": sorted(missing, key=lambda r: r["soname"]),
        "runtime_environment_verified": False,
    }


def osv_request(commit):
    require(
        isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "Commit fuente inválido."
    )
    body = {"commit": commit}
    return {
        "method": "POST",
        "endpoint": "https://api.osv.dev/v1/query",
        "body": body,
        "body_sha256": sha(canonical(body)),
        "documentation": "https://google.github.io/osv.dev/post-v1-query/",
        "executed": False,
        "pagination": {
            "request_field": "page_token",
            "response_field": "next_page_token",
            "require_exhausted": True,
            "reject_repeated_tokens": True,
            "preserve_each_raw_response_sha256": True,
        },
        "coverage": "Sólo commit fuente OpenCV; no cubre terceros estáticos ni paquetes externos del runtime.",
    }


def validate_spec(spec):
    require(
        set(spec)
        == {
            "schema_version",
            "owner",
            "output_relative",
            "references",
            "wheel_members",
            "external_archives",
            "source_identity",
            "osv_protocol",
            "scan_performed",
            "queries_performed",
            "security_approved",
            "runtime_verified",
        },
        "Campos de spec distintos del contrato.",
    )
    require(
        spec["schema_version"] == 1
        and spec["owner"] == OWNER
        and spec["output_relative"] == OUTPUT_RELATIVE
        and all(
            spec[k] is False
            for k in (
                "scan_performed",
                "queries_performed",
                "security_approved",
                "runtime_verified",
            )
        ),
        "Identidad o alcance de spec incorrectos.",
    )
    required = {
        "wheel": WHEEL_SHA,
        "relocation_receipt": RECEIPT_SHA,
        "manifest": MANIFEST_SHA,
        "sdk_approval": APPROVAL_SHA,
        "relocator": RELOCATOR_SHA,
        "parser": PARSER_SHA,
        "packager": PACKAGER_SHA,
        "source_archive": SOURCE_SHA,
    }
    require(
        set(spec["references"]) == set(required) | {"sdk_cdx", "sdk_sca_receipt", "sdk_stage"},
        "Conjunto de referencias distinto.",
    )
    for key, row in spec["references"].items():
        validate_reference(row)
        require(row["path"] == REFERENCE_PATHS[key], "Ruta fuera de las entradas fijadas.")
        if key in required:
            require(row["sha256"] == required[key], "Referencia de otra fuente/artefacto.")
    require(
        len(spec["wheel_members"]) == 10
        and len(spec["external_archives"]) == 10
        and {r["name"] for r in spec["external_archives"]} == EXTERNAL_NAMES,
        "Conjunto de miembros/proveedores no fijado.",
    )
    for name, row in spec["wheel_members"].items():
        safe_name(name)
        require(set(row) == {"sha256", "bytes"}, "Pin de miembro ambiguo.")
        validate_pin(row)
    for row in spec["external_archives"]:
        require(
            set(row) == {"name", "version", "record", "archive", "authentication_receipt"}
            and row["record"]["P"] == row["name"]
            and row["record"]["V"] == row["version"]
            and row["record"]["A"] == "x86_64",
            "Identidad de proveedor incorrecta.",
        )
        validate_reference(row["archive"])
        validate_reference(row["authentication_receipt"])
        require(
            row["archive"]["path"]
            == ".tools/kitsu-zou-opencv-sdk-v1/kit-755ea032b52a767f/data/apks/"
            + row["name"]
            + "-"
            + row["version"]
            + ".apk"
            and row["authentication_receipt"]["path"]
            == ".tools/zou-native-research-v1/opencv-inputs-v1/receipts/"
            + row["name"]
            + "-"
            + row["version"]
            + ".json",
            "Ruta de proveedor fuera del cierre fijado.",
        )
    require(
        spec["source_identity"]
        == {
            "version": "4.13.0",
            "commit": "fe38fc608f6acb8b68953438a62305d8318f4fcd",
            "sha256": SOURCE_SHA,
            "url": "https://codeload.github.com/opencv/opencv/tar.gz/fe38fc608f6acb8b68953438a62305d8318f4fcd",
            "license": "Apache-2.0",
        },
        "Fuente distinta del sujeto aprobado.",
    )
    require(
        spec["osv_protocol"]
        == {
            "documentation": "https://google.github.io/osv.dev/post-v1-query/",
            "endpoint": "https://api.osv.dev/v1/query",
            "method": "POST",
            "request_next_page_field": "page_token",
            "response_next_page_field": "next_page_token",
            "require_pagination_exhausted": True,
        },
        "Protocolo de consulta distinto del revisado.",
    )


def validate_pin(row):
    require(
        type(row["bytes"]) is int
        and 0 < row["bytes"] <= 128 * 1024**2
        and isinstance(row["sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
        "Pin de tamaño/hash no válido.",
    )


def validate_reference(row):
    require(set(row) == {"path", "sha256", "bytes"}, "Referencia ambigua.")
    safe_name(row["path"])
    validate_pin(row)


def load_relocator():
    path = PROJECT / "docker/kitsu-zou/prepare_opencv_relocated_wheel.py"
    # El bootstrap sólo carga una fuente local conocida, comprobada antes de compilar Python.
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and not getattr(info, "st_file_attributes", 0) & 0x400,
        "Relocalizador no regular.",
    )
    raw = path.read_bytes()
    require(len(raw) <= 128 * 1024 and sha(raw) == RELOCATOR_SHA, "Relocalizador distinto del pin.")
    module = types.ModuleType("opencv_inventory_relocator")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(raw, str(path), "exec"), vars(module))
    require(module.read_regular(path, 128 * 1024) == raw, "Fuente cambió durante bootstrap.")
    return module


def read_reference(relocator, row):
    raw = relocator.read_regular(PROJECT / row["path"], row["bytes"])
    require(
        len(raw) == row["bytes"] and sha(raw) == row["sha256"],
        "Bytes diferentes del pin: " + row["path"],
    )
    return raw


def elf_summary(raw, parser):
    info = parser.elf_info(raw)
    return {
        "bytes": len(raw),
        "sha256": sha(raw),
        "elf_class": "ELF64",
        "machine": "x86_64",
        "endianness": "little",
        **{
            k: info[k]
            for k in (
                "soname",
                "needed",
                "rpath",
                "runpath",
                "version_requirements",
                "version_definitions",
            )
        },
        "dynamic_symbols": {
            key: {"count": len(info[key]), "sha256": sha(canonical(info[key]))}
            for key in ("defined", "undefined", "weak")
        },
    }


def declared_providers(manifest):
    declared = {}
    for record in manifest["base"]["packages"].values():
        for token in record.get("p", "").split():
            if token.startswith("so:"):
                name = token[3:].split("=", 1)[0]
                declared.setdefault(name, []).append(
                    {
                        "name": record["P"],
                        "version": record["V"],
                        "arch": record["A"],
                        "license": record["L"],
                        "index_q1": record["C"],
                        "payload_bytes_verified_here": False,
                    }
                )
    return declared


def python_smoke_identity(manifest, approval, archive, files):
    entries = [r for r in manifest["sdk_entrypoints"] if r["package"] == "python-3.12-base"]
    require(len(entries) == 1, "Intérprete Python ambiguo.")
    entry = entries[0]
    require(
        archive["name"] == entry["package"]
        and archive["version"] == entry["version"] == manifest["python"]["package_version"],
        "Versión Python distinta del SDK/lock.",
    )
    require(
        entry["resolved_member"] in files
        and sha(files[entry["resolved_member"]]) == entry["sha256"]
        and [r for r in approval["tools"] if r["path"] == entry["path"]]
        == [{"path": entry["path"], "sha256": entry["sha256"]}],
        "Bytes Python no coinciden con atestación SDK.",
    )
    return {
        "version": manifest["python"]["version"],
        "package": entry["package"],
        "package_version": entry["version"],
        "path": entry["path"],
        "sha256": entry["sha256"],
        "wheel_tag": "cp312-cp312-linux_x86_64",
        "executed": False,
        "required_environment": "Runtime Linux Wolfi/glibc compatible, con los proveedores ABI comprobados; no importar en el host Ubuntu por suposición.",
    }


def validate_provenance(refs, manifest, source):
    relocation = json_object(refs["relocation_receipt"])
    approval = json_object(refs["sdk_approval"])
    stage = json_object(refs["sdk_stage"])
    sca = json_object(refs["sdk_sca_receipt"])
    cdx = json_object(refs["sdk_cdx"])
    require(
        relocation.get("success") is True
        and relocation.get("originals_unchanged") is True
        and relocation["wheel"]["sha256"] == WHEEL_SHA
        and relocation["wheel"]["record_verified"] is True
        and relocation["wheel"]["members"] == 10
        and relocation["elf_executed"] is False
        and relocation["sdk_executed"] is False,
        "Recibo de wheel no corresponde al sujeto.",
    )
    require(
        all(manifest["source"][k] == v for k, v in source.items())
        and sha(refs["source_archive"]) == source["sha256"],
        "Fuente OpenCV no coincide.",
    )
    require(
        approval["verified"] is True
        and approval["runtime_approved"] is False
        and approval["kind"] == "openexr3-sdk-sca-approval-v1"
        and approval["coverage"]
        == {
            "high_critical": 0,
            "installed_apk": 89,
            "numpy": 1,
            "partial_apk": 5,
            "total": 95,
            "unknown_or_unscored": 0,
        },
        "Aprobación SDK de otro alcance.",
    )
    require(
        approval["input_sca_receipt_sha256"] == sha(refs["sdk_sca_receipt"])
        and approval["sbom_sha256"] == sha(refs["sdk_cdx"])
        and stage["receipt_sha256"]
        == sha(canonical(stage["receipt"]))
        == approval["sdk_receipt_sha256"]
        and stage["receipt"]["success"] is True
        and stage["receipt"]["sdk_artifact_sha256"] == approval["sdk_artifact_sha256"]
        and stage["receipt"]["attestation"]["tools"] == approval["tools"]
        and stage["receipt"]["attestation"]["database_sha256"] == approval["database_sha256"],
        "Cadena de recibos SDK no coincide.",
    )
    require(
        sca["sbom_sha256"] == sha(refs["sdk_cdx"])
        and sca["input_sca_passed"] is True
        and sca["coverage"]["packages"] == 95
        and sca["coverage"]["by_severity"] == approval["remaining_findings"] == {"MEDIUM": 10},
        "Cobertura histórica SDK distinta.",
    )
    python_rows = [r for r in cdx["components"] if r.get("name") == "python-3.12-base"]
    require(
        len(python_rows) == 1
        and python_rows[0]["version"] == manifest["python"]["package_version"],
        "SBOM no liga Python al lock.",
    )
    return approval


def cdx_document(inventory):
    # No se asigna un purl PyPI a un binario propio ni APK a código C/C++ manual.
    source = inventory["source"]
    components = [
        {
            "type": "library",
            "bom-ref": "opencv-source",
            "name": "OpenCV",
            "version": source["version"],
            "hashes": [{"alg": "SHA-256", "content": source["sha256"]}],
            "licenses": [{"expression": source["license"]}],
            "externalReferences": [
                {"type": "vcs", "url": "https://github.com/opencv/opencv/tree/" + source["commit"]}
            ],
            "properties": [
                {
                    "name": "agency:identity-scope",
                    "value": "archivo fuente; no un artefacto oficial PyPI",
                }
            ],
        }
    ]
    for name, row in inventory["bundled_elf"].items():
        components.append(
            {
                "type": "file",
                "bom-ref": "wheel:" + name,
                "name": name,
                "hashes": [{"alg": "SHA-256", "content": row["sha256"]}],
                "properties": [
                    {
                        "name": "agency:identity-scope",
                        "value": "ELF compilado manualmente; no representa un paquete OS",
                    }
                ],
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "file",
                "name": inventory["wheel"]["filename"],
                "hashes": [{"alg": "SHA-256", "content": WHEEL_SHA}],
            },
            "properties": [
                {"name": "agency:security-approved", "value": "false"},
                {
                    "name": "agency:coverage-limit",
                    "value": "Sólo fuente OpenCV y cuatro archivos ELF. Proveedores externos y terceros estáticos se describen separados; no quedan certificados.",
                },
            ],
        },
        "components": components,
        "dependencies": [
            {"ref": "wheel:" + name, "dependsOn": ["opencv-source"]}
            for name in inventory["bundled_elf"]
        ],
    }


def prepare_documents():
    relocator = load_relocator()
    raw_spec = relocator.read_regular(PROJECT / SPEC_PATH, 128 * 1024)
    spec = json_object(raw_spec)
    validate_spec(spec)
    refs = {key: read_reference(relocator, row) for key, row in spec["references"].items()}
    relocation_spec, _ = relocator.load_spec()
    inputs = relocator.capture_inputs(relocation_spec)
    packager, parser = relocator.frozen_sources()
    manifest = relocator.configuration(packager, inputs, relocation_spec)
    require(
        refs["manifest"] == inputs["manifest-approved.json"],
        "Manifiesto diferente de la captura/compilación.",
    )
    approval = validate_provenance(refs, manifest, spec["source_identity"])
    derived, _proofs, _elf = relocator.transformed_inputs(inputs, relocation_spec, packager, parser)
    relocator.verify_wheel(
        PROJECT / spec["references"]["wheel"]["path"], packager, derived, manifest
    )
    files = read_members(refs["wheel"], spec["wheel_members"])
    validate_record(files, DIST)
    metadata = wheel_metadata(files[DIST + "/METADATA"])
    require(
        files[DIST + "/WHEEL"]
        == b"Wheel-Version: 1.0\nGenerator: agencia-opencv-minimal-v1\nRoot-Is-Purelib: false\nTag: cp312-cp312-linux_x86_64\n",
        "Tag WHEEL distinto.",
    )
    bundled = {
        name: elf_summary(raw, parser) for name, raw in files.items() if raw.startswith(b"\x7fELF")
    }
    require(
        len(bundled) == 4
        and all(r["rpath"] == RPATH and r["runpath"] is None for r in bundled.values()),
        "ELF/RPATH del wheel no corresponde al derivado.",
    )
    for name, raw in files.items():
        if name in bundled:
            packager.elf_dynamic(raw)
    packages = []
    catalog = []
    python_identity = None
    declared = declared_providers(manifest)
    for row in spec["external_archives"]:
        raw = read_reference(relocator, row["archive"])
        previous_raw = read_reference(relocator, row["authentication_receipt"])
        previous = json_object(previous_raw)
        require(
            previous["record"] == row["record"]
            and previous["sha256"] == row["archive"]["sha256"]
            and previous["signature_mode"] == "index-rsa256-control-q1-payload-sha256"
            and previous["individual_signature"] is False
            and previous["scripts_executed"] is False,
            "Referencia previa de autenticación distinta.",
        )
        manifest_rows = [r for r in manifest["packages"] if r["name"] == row["name"]]
        require(
            len(manifest_rows) == 1
            and manifest_rows[0]["record"] == row["record"]
            and manifest_rows[0]["sha256"] == sha(raw),
            "Proveedor no coincide con lock autenticado.",
        )
        payload, proof = apk_payload(raw, row["record"])
        require(
            proof["payload_tar_sha256"] == previous["payload_tar_sha256"],
            "Payload no coincide con recibo autenticado.",
        )
        identity = {
            "name": row["name"],
            "version": row["version"],
            "arch": row["record"]["A"],
            "license": row["record"]["L"],
            "archive": row["archive"],
            "prior_authentication_receipt": row["authentication_receipt"],
            "proof": proof,
            "included_in_wheel": False,
            "runtime_installed_verified": False,
        }
        packages.append(identity)
        if row["name"] == "python-3.12-base":
            python_identity = python_smoke_identity(manifest, approval, row, payload)
            continue
        for name, data in sorted(payload.items()):
            if data.startswith(b"\x7fELF"):
                elf = elf_summary(data, parser)
                if elf["soname"]:
                    catalog.append(
                        {
                            **elf,
                            "member": name,
                            "package": row["name"],
                            "package_version": row["version"],
                            "included_in_wheel": False,
                        }
                    )
    require(python_identity is not None, "Falta Python autenticado.")
    closure = resolve_dependencies(bundled, catalog, declared)
    inventory = {
        "schema_version": 1,
        "kind": "inventario-nativo-como-datos",
        "owner": OWNER,
        "wheel": {
            "filename": Path(spec["references"]["wheel"]["path"]).name,
            "bytes": len(refs["wheel"]),
            "sha256": WHEEL_SHA,
            **metadata,
        },
        "members": spec["wheel_members"],
        "bundled_elf": bundled,
        "source": spec["source_identity"],
        "configuration": {
            "manifest_sha256": MANIFEST_SHA,
            "files": {
                name: relocation_spec["inputs"][name]
                for name in (
                    "build/CMakeCache.txt",
                    "build/opencv2/cvconfig.h",
                    "build/opencv2/opencv_modules.hpp",
                )
            },
            "validated_by_packager_sha256": PACKAGER_SHA,
            "enabled_modules": ["core", "imgcodecs", "imgproc", "python3"],
            "gnu_build_id": "Dato heredado conservado; la identidad derivada es el SHA256 de cada archivo y el recibo de transformación.",
        },
        "relocation_receipt": spec["references"]["relocation_receipt"],
        "external_candidate_packages": packages,
        "external_elf_catalog": catalog,
        "dynamic_closure": closure,
        "sdk_provenance": {
            "approval_reference": spec["references"]["sdk_approval"],
            "coverage": approval["coverage"],
            "remaining_findings": approval["remaining_findings"],
            "db": approval["db"],
            "scope": "Aprobación histórica sólo del SDK/build offline. No aprueba wheel, importación, aplicación ni imagen de runtime.",
        },
        "smoke_requirements": python_identity,
        "coverage_gaps": [
            "Terceros estáticos/header-only potencialmente incorporados: sin mapa de enlace ni build.ninja capturados; atribución y revisión independientes pendientes.",
            "Proveedores externos sólo candidatos por archivo/SONAME; faltan bytes de algunos proveedores de base y la confirmación del runtime final.",
            "Una consulta por commit OpenCV no cubre CVE de terceros, parches de distribución, ni prueba ausencia de código vulnerable.",
        ],
        "scan_performed": False,
        "queries_performed": False,
        "security_approved": False,
        "runtime_verified": False,
        "elf_executed": False,
        "sdk_executed": False,
    }
    requests = {
        "schema_version": 1,
        "subject_sha256": WHEEL_SHA,
        "requests": [osv_request(spec["source_identity"]["commit"])],
        "queries_performed": False,
        "required_followup": "Root ejecutará y conservará todas las páginas. Respuestas vacías o cero componentes no certifican terceros/ABI ni conceden aprobación.",
    }
    documents = {
        "inventory.json": canonical(inventory),
        "wheel-source.cdx.json": canonical(cdx_document(inventory)),
        "osv-requests.json": canonical(requests),
    }
    parent = PROJECT / ".tools/backup-source-build"
    access = relocator.access_reader()
    access.check_access(parent, private=True)
    marker = relocator.read_regular(parent / ".agency-backup-build-owner", 4096)
    require(
        marker == b"agencia-digital-marketing-ai:backup-source-build:v1",
        "Raíz de publicación sin dueño acreditado.",
    )
    node = parent.lstat()
    plan = {
        "schema_version": 1,
        "owner": OWNER,
        "output_root": str(PROJECT / OUTPUT_RELATIVE),
        "controller_sha256": sha(relocator.read_regular(Path(__file__), 128 * 1024)),
        "spec_sha256": sha(raw_spec),
        "references": spec["references"],
        "parent": {
            "path": str(parent),
            "device": node.st_dev,
            "inode": node.st_ino,
            "marker_sha256": sha(marker),
        },
        "documents": {
            name: {"bytes": len(raw), "sha256": sha(raw)} for name, raw in documents.items()
        },
        "coverage": {
            "members": 10,
            "bundled_elf": 4,
            "external_apk_archives": 10,
            "external_elf_by_bytes": len(catalog),
            "external_sonames_confirmed": closure["external_confirmed"],
            "unconfirmed": closure["unconfirmed"],
            "static_third_parties_closed": False,
        },
        "smoke_requirements": python_identity,
        "scan_performed": False,
        "queries_performed": False,
        "security_approved": False,
        "runtime_verified": False,
        "elf_executed": False,
        "sdk_executed": False,
    }
    # Segunda lectura acotada de las referencias originales; no se modifica ninguna.
    for key, row in spec["references"].items():
        require(
            read_reference(relocator, row) == refs[key],
            "Una referencia cambió durante el inventario.",
        )
    return plan, documents, relocator, access


def build_plan():
    plan, _documents, _relocator, _access = prepare_documents()
    require(not os.path.lexists(plan["output_root"]), "No se adopta una publicación existente.")
    return plan


def publish_documents(output, plan, documents, access, *, reader=None):
    require(
        set(documents) == DOCUMENTS
        and plan.get("owner") == OWNER
        and plan.get("output_root") == str(output),
        "Publicación de otra identidad o archivos.",
    )
    for name, raw in documents.items():
        require(
            name in DOCUMENTS
            and safe_name(name) == name
            and type(raw) is bytes
            and len(raw) <= 2 * 1024**2,
            "Salida de datos no válida.",
        )
    if reader is None:
        reader = load_relocator().read_regular
    access.check_access(output.parent, private=True)
    os.mkdir(output, 0o700)
    access.check_access(output, private=True)
    node = output.lstat()
    marker = {
        "owner": OWNER,
        "path": str(output),
        "device": node.st_dev,
        "inode": node.st_ino,
        "plan_sha256": sha(canonical(plan)),
    }
    payloads = {".owner.json": canonical(marker), "plan.json": canonical(plan), **documents}
    for name, raw in payloads.items():
        fd = os.open(
            output / name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        access.check_access(output / name, private=True)
    for name, raw in payloads.items():
        require(
            reader(output / name, 2 * 1024**2) == raw,
            "Datos publicados no coinciden con la relectura.",
        )
    current = output.lstat()
    require(
        (current.st_dev, current.st_ino) == (node.st_dev, node.st_ino),
        "Identidad de publicación cambiada.",
    )
    receipt = {
        "success": True,
        "owner": OWNER,
        "directory": marker,
        "plan_sha256": sha(canonical(plan)),
        "files": {name: {"sha256": sha(raw), "bytes": len(raw)} for name, raw in payloads.items()},
        "scan_performed": False,
        "queries_performed": False,
        "security_approved": False,
        "runtime_verified": False,
        "elf_executed": False,
        "sdk_executed": False,
    }
    raw = canonical(receipt)
    fd = os.open(
        output / "receipt.json",
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    access.check_access(output / "receipt.json", private=True)
    return receipt


def publish(expected):
    plan, documents, relocator, access = prepare_documents()
    require(sha(canonical(plan)) == expected, "Plan diferente del pin aprobado.")
    output = Path(plan["output_root"])
    parent = output.parent.lstat()
    require(
        (parent.st_dev, parent.st_ino) == (plan["parent"]["device"], plan["parent"]["inode"]),
        "Padre privado cambió.",
    )
    result = publish_documents(output, plan, documents, access, reader=relocator.read_regular)
    for name, raw in documents.items():
        require(
            relocator.read_regular(output / name, 2 * 1024**2) == raw,
            "Publicación cambió después de escribir.",
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.publish:
            require(
                isinstance(args.expected_plan_sha256, str)
                and re.fullmatch(r"[a-f0-9]{64}", args.expected_plan_sha256),
                "Falta pin externo.",
            )
            result = publish(args.expected_plan_sha256)
        else:
            plan = build_plan()
            result = {"plan": plan, "plan_sha256": sha(canonical(plan))}
        print(canonical(result).decode())
        return 0
    except Exception as error:
        print(
            canonical(
                {
                    "success": False,
                    "error": "OPENCV_WHEEL_INVENTORY_REJECTED",
                    "error_type": type(error).__name__,
                }
            ).decode()
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
