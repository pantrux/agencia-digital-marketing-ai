"""Inventa hipótesis de identidad de pip/vendorizados sin ejecutar sus contenidos."""

import base64
import csv
import hashlib
import io
import json
import re
import stat
import zipfile
from email.parser import BytesParser
from email.policy import compat32
from urllib.parse import quote

MAX_WHEEL = 8 * 1024**2
MAX_MEMBER = 4 * 1024**2
MAX_EXPANDED = 32 * 1024**2


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def name_normalized(value):
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)), "Nombre inválido.")
    return re.sub(r"[-_.]+", "-", value).lower()


def safe_path(value):
    require(
        isinstance(value, str)
        and 0 < len(value) <= 1024
        and not value.startswith("/")
        and not any(char in value for char in ("\\", ":", "\0"))
        and all(part not in ("", ".", "..") for part in value.split("/")),
        "Ruta ZIP inválida.",
    )
    return value


def read_zip(raw):
    require(0 < len(raw) <= MAX_WHEEL, "ZIP excesivo o vacío.")
    files, folded, expanded = {}, set(), 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for member in archive.infolist():
            name = safe_path(member.filename.rstrip("/") if member.is_dir() else member.filename)
            require(name.casefold() not in folded, "Miembro ZIP duplicado.")
            folded.add(name.casefold())
            mode = member.external_attr >> 16
            require(
                not member.flag_bits & 1
                and (stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR))
                and not mode & 0o7000
                and 0 <= member.file_size <= MAX_MEMBER
                and len(folded) <= 4096,
                "Tipo, tamaño o cifrado ZIP no admitidos.",
            )
            if member.is_dir():
                require(member.file_size == 0, "Directorio ZIP con payload.")
                continue
            expanded += member.file_size
            require(expanded <= MAX_EXPANDED, "ZIP expandido excesivo.")
            data = archive.read(member)
            require(len(data) == member.file_size, "ZIP incompleto.")
            files[name] = data
    return files


def verify_record(files, record_path):
    require(record_path in files, "RECORD ausente.")
    seen = set()
    for row in csv.reader(io.StringIO(files[record_path].decode("utf-8", "strict"), newline="")):
        require(len(row) == 3, "RECORD mal formado.")
        name, checksum, size = row
        safe_path(name)
        require(name in files and name not in seen, "RECORD duplicado o adicional.")
        seen.add(name)
        if name == record_path:
            require(checksum == size == "", "RECORD propio no vacío.")
            continue
        raw = files[name]
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        require(
            checksum == "sha256=" + encoded and size == str(len(raw)),
            "RECORD no coincide con contenido/tamaño.",
        )
    require(seen == set(files), "RECORD incompleto.")


def supplier_bom(files, vendors):
    path = "pip/_vendor/bom.cdx.json"
    if path not in files:
        return {"present": False}

    def unique(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "SBOM con clave duplicada.")
            result[key] = value
        return result

    value = json.loads(files[path].decode("utf-8", "strict"), object_pairs_hook=unique)
    require(type(value) is dict and value.get("bomFormat") == "CycloneDX", "SBOM inválido.")
    expected = {(row["name"], row["version"]) for row in vendors}
    observed, refs = [], set()
    for row in value.get("components", []):
        require(type(row) is dict, "SBOM con componente inválido.")
        if row.get("name") == "pip" and "version" not in row:
            continue
        name, version = name_normalized(row.get("name", "")), row.get("version")
        require((name, version) in expected, "SBOM con versión no declarada.")
        purl = row.get("purl", "")
        require(purl.startswith("pkg:pypi/") and "@" in purl, "SBOM sin PURL PyPI.")
        purl_name, purl_version = purl.removeprefix("pkg:pypi/").rsplit("@", 1)
        require(
            name_normalized(purl_name) == name and purl_version == version,
            "SBOM con PURL distinto.",
        )
        require(row.get("bom-ref") not in refs, "SBOM con referencia duplicada.")
        refs.add(row.get("bom-ref"))
        observed.append((name, version))
    require(len(observed) == len(expected) and set(observed) == expected, "SBOM incompleto.")
    return {
        "present": True,
        "sha256": digest(files[path]),
        "bytes": len(files[path]),
        "declarations_verified": len(observed),
        "claims_only": True,
    }


def inventory_wheel(raw):
    files = read_zip(raw)
    metadata_paths = [p for p in files if re.fullmatch(r"pip-[^/]+\.dist-info/METADATA", p)]
    require(len(metadata_paths) == 1, "METADATA pip ausente o ambiguo.")
    metadata_path = metadata_paths[0]
    metadata = BytesParser(policy=compat32).parsebytes(files[metadata_path])
    require(
        metadata.get_all("Name") == ["pip"] and len(metadata.get_all("Version", [])) == 1,
        "Identidad METADATA ambigua.",
    )
    version = metadata["Version"]
    require(
        bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:[A-Za-z0-9.+-]*)", version))
        and metadata_path == "pip-" + version + ".dist-info/METADATA",
        "Versión METADATA distinta del contenedor.",
    )
    verify_record(files, metadata_path.removesuffix("METADATA") + "RECORD")
    vendor_path = "pip/_vendor/vendor.txt"
    require(vendor_path in files, "Declaración vendor ausente.")
    vendors, seen, attributed = [], set(), set()
    for line in files[vendor_path].decode("utf-8", "strict").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(
            r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+!-]*)\s*", line
        )
        require(match is not None, "Declaración vendor no exacta.")
        name, declared_version = name_normalized(match[1]), match[2]
        require(name not in seen, "Distribución vendor duplicada.")
        seen.add(name)
        namespace = "pkg_resources" if name == "setuptools" else name.replace("-", "_")
        prefix = "pip/_vendor/" + namespace
        members = sorted(
            p for p in files if p.startswith(prefix + "/") or p in {prefix + ".py", prefix + ".pyi"}
        )
        require(members, "Falta namespace para distribución declarada: " + name)
        attributed.add(namespace)
        licenses = [
            p
            for p in members
            if re.fullmatch(r"(?:LICENSE|COPYING|NOTICE)(?:[._-].*)?", p.rsplit("/", 1)[-1], re.I)
        ]
        vendors.append(
            {
                "name": name,
                "version": declared_version,
                "namespace": namespace,
                "identity_basis": "vendor.txt del payload autenticado; hipótesis para SCA",
                "whole_distribution_verified": False,
                "files": [
                    {"path": p, "bytes": len(files[p]), "sha256": digest(files[p])} for p in members
                ],
                "license_files": licenses,
            }
        )
    require(vendors, "Declaración vendor vacía.")
    observed_namespaces = {
        (
            relative.split("/", 1)[0]
            if "/" in relative
            else relative.removesuffix(".py").removesuffix(".pyi")
        )
        for p in files
        for relative in [p.removeprefix("pip/_vendor/")]
        if p.startswith("pip/_vendor/")
        and p
        not in {
            vendor_path,
            "pip/_vendor/__init__.py",
            "pip/_vendor/README.rst",
            "pip/_vendor/bom.cdx.json",
        }
    }
    unknown = sorted(observed_namespaces - attributed)
    return {
        "schema_version": 1,
        "wheel": {"bytes": len(raw), "sha256": digest(raw)},
        "pip": {"name": "pip", "version": version, "metadata_sha256": digest(files[metadata_path])},
        "vendor_declaration_sha256": digest(files[vendor_path]),
        "supplier_sbom": supplier_bom(files, vendors),
        "supplier_notes": [
            {
                "path": p,
                "sha256": digest(files[p]),
                "bytes": len(files[p]),
                "text": files[p].decode("utf-8", "strict"),
            }
            for p in ["pip/_vendor/README.rst"]
            if p in files
        ],
        "declared_vendor_distributions": len(vendors),
        "vendors": sorted(vendors, key=lambda row: row["name"]),
        "files": [
            {"path": p, "bytes": len(data), "sha256": digest(data)}
            for p, data in sorted(files.items())
        ],
        "licenses": [
            {
                "path": p,
                "bytes": len(data),
                "sha256": digest(data),
                "text": data.decode("utf-8", "replace"),
            }
            for p, data in sorted(files.items())
            if re.fullmatch(r"(?:LICENSE|COPYING|NOTICE)(?:[._-].*)?", p.rsplit("/", 1)[-1], re.I)
        ],
        "unattributed_vendor_namespaces": unknown,
        "identity_complete": False,
        "namespace_declarations_complete": not unknown,
        "code_executed": False,
        "runtime_approved": False,
    }


def cyclonedx(inventory):
    components = []
    for row in [inventory["pip"], *inventory["vendors"]]:
        name, version = row["name"], row["version"]
        purl = "pkg:pypi/" + quote(name, safe="") + "@" + quote(version, safe="")
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "bom-ref": purl,
                "purl": purl,
                "properties": [
                    {
                        "name": "agency:identity-basis",
                        "value": "METADATA" if name == "pip" else "vendor.txt-hypothesis",
                    },
                    {"name": "agency:wheel-sha256", "value": inventory["wheel"]["sha256"]},
                ],
            }
        )
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1, "components": components}
