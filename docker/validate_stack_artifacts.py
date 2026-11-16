"""Valida identidad y alcance de candidatos; no audita ni acredita producción."""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_MANIFEST = Path(__file__).with_name("stack-artifacts.json")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
REVISION = re.compile(r"^[a-f0-9]{40}$")
# Cada rol fija su procedencia y versión revisadas. Una actualización requiere
# cambiar explícitamente este contrato junto al inventario y volver a auditar.
POLICIES = {
    "postgres-apps": ("16.15", "postgres/postgres", "PostgreSQL", "docker.io/library/postgres"),
    "postgres-immich-base": (
        "16.15",
        "postgres/postgres",
        "PostgreSQL",
        "docker.io/library/postgres",
    ),
    "immich-server": (
        "3.3.6",
        "immich-app/immich",
        "AGPL-3.0-only",
        "ghcr.io/immich-app/immich-server",
    ),
    "valkey": ("9.1.3", "valkey-io/valkey", "BSD-3-Clause", "docker.io/valkey/valkey"),
    "n8n": ("2.46.6", "n8n-io/n8n", "LicenseRef-Sustainable-Use", "docker.io/n8nio/n8n"),
    "n8n-runners": (
        "2.46.6",
        "n8n-io/n8n",
        "LicenseRef-Sustainable-Use",
        "docker.io/n8nio/runners",
    ),
    "moneyprinter": (
        "1.3.9",
        "harry0703/MoneyPrinterTurbo",
        "MIT",
        "ghcr.io/harry0703/moneyprinterturbo",
    ),
    "otel-collector": (
        "0.164.0",
        "open-telemetry/opentelemetry-collector-releases",
        "Apache-2.0",
        "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib",
    ),
    "node-exporter": (
        "1.12.1",
        "prometheus/node_exporter",
        "Apache-2.0",
        "quay.io/prometheus/node-exporter",
    ),
    "cadvisor": ("0.60.6", "google/cadvisor", "Apache-2.0", "ghcr.io/google/cadvisor"),
    "kitsu-ui": ("1.0.77", "cgwire/kitsu", "AGPL-3.0-only", None),
    "zou-api": ("1.1.1", "cgwire/zou", "AGPL-3.0-only", None),
    "supoclip": ("unreleased-20261105", "FujiwaraChoki/supoclip", "AGPL-3.0-only", None),
    "vectorchord-extension": ("1.1.1", "supervc-stack/VectorChord", "AGPL-3.0-only", None),
    "pgvector-extension": ("0.8.5", "pgvector/pgvector", "PostgreSQL", None),
}
FALSE_CLAIMS = ("security_clean", "runtime_verified", "production_ready", "deployment_enabled")
# Las dos bases verificadas tienen funciones distintas. El .deb GNU de
# VectorChord no puede recibir la base musl ni sus bytes bajo un tag Debian.
POSTGRES_PINS = {
    "postgres-apps": (
        "16.15-alpine3.24",
        "sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139",
        "sha256:ac838714b7d794185a866e5a6b479b954e7d8a070568aa0acac6fe07c3070bf5",
    ),
    "postgres-immich-base": (
        "16.15-trixie",
        "sha256:933463a6c8eba2b9f5cd414138738cd8d5519cd2c471214c222652f547a66a32",
        "sha256:43f9275df91e012c3da3d86bea5d7bb5e67a473296cc906c5f9e80b8176b1a37",
    ),
}
TOP_FIELDS = {
    "schema_version",
    "status",
    "as_of_utc",
    "platform",
    "target",
    "artifacts",
    "postgres_immich_build",
    "audit_status",
    "observations",
    *FALSE_CLAIMS,
}


def load_json(path: Path) -> object:
    """Lee un documento acotado sin evaluar expresiones ni aceptar JSON ambiguo."""
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("El inventario excede el límite permitido.")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("El inventario contiene claves JSON duplicadas.")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("El inventario contiene un número JSON no finito.")

    return json.loads(
        path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=unique,
        parse_constant=reject_constant,
    )


def plain_https(value: object) -> bool:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and bool(url.hostname)
            and not url.username
            and not url.password
            and not url.port
            and not url.query
            and not url.fragment
            and "%" not in url.path
            and ".." not in url.path.split("/")
        )
    except ValueError:
        return False


def source_valid(source: object, repository: str) -> bool:
    if not isinstance(source, dict) or set(source) != {
        "repository",
        "revision",
        "license_url",
        "license_sha256",
    }:
        return False
    revision = source.get("revision")
    if (
        source.get("repository") != "https://github.com/" + repository
        or not isinstance(revision, str)
        or not REVISION.fullmatch(revision)
        or not isinstance(source.get("license_sha256"), str)
        or not SHA256.fullmatch(source["license_sha256"])
    ):
        return False
    url = source.get("license_url")
    prefix = f"https://raw.githubusercontent.com/{repository}/{revision}/"
    return (
        plain_https(url)
        and url.startswith(prefix)
        and url[len(prefix) :]
        in {
            "LICENSE",
            "LICENSE.md",
            "COPYRIGHT",
            "COPYING",
        }
    )


def license_valid(value: object, role: str, selected: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "spdx",
        "declared_expression",
        "classification",
        "use",
        "scope",
    }:
        return False
    internal = role in {"n8n", "n8n-runners"}
    expression = "AGPL-3.0-only OR Elastic-2.0" if role == "vectorchord-extension" else selected
    return value == {
        "spdx": selected,
        "declared_expression": expression,
        "classification": "restricted" if internal else "osi",
        "use": "agency-internal" if internal else "application-use",
        # La licencia del proyecto no se atribuye a todos los componentes de una imagen.
        "scope": "upstream-project-only",
    }


def download_valid(entry: dict) -> bool:
    download = entry.get("download")
    if not isinstance(download, dict) or set(download) != {"url", "sha256", "size"}:
        return False
    if (
        not plain_https(download.get("url"))
        or not isinstance(download.get("sha256"), str)
        or not SHA256.fullmatch(download["sha256"])
        or type(download.get("size")) is not int
        or not 0 < download["size"] <= 128 * 1024**2
    ):
        return False
    role, version = entry["role"], entry["version"]
    repo = POLICIES[role][1]
    revision = entry["source"]["revision"]
    url = download["url"]
    if role == "kitsu-ui":
        return url == f"https://github.com/{repo}/releases/download/v{version}/kitsu-{version}.tgz"
    if role == "zou-api":
        parsed = urlsplit(url)
        return (
            parsed.hostname == "files.pythonhosted.org"
            and parsed.path.endswith(f"/zou-{version}-py3-none-any.whl")
            and parsed.path.startswith("/packages/")
        )
    if role == "vectorchord-extension":
        return url == (
            f"https://github.com/{repo}/releases/download/{version}/"
            f"postgresql-16-vchord_{version}-1_amd64.deb"
        )
    return url == f"https://codeload.github.com/{repo}/tar.gz/{revision}"


def image_valid(entry: dict, repository: str) -> bool:
    image = entry.get("image")
    if not isinstance(image, dict) or set(image) != {
        "repository",
        "tag",
        "index_digest",
        "platform_digest",
        "observed_versions",
    }:
        return False
    if not isinstance(entry.get("version"), str):
        return False
    tag = image.get("tag")
    expected_tags = {entry["version"], "v" + entry["version"]}
    postgres_pin = POSTGRES_PINS.get(entry["role"])
    if postgres_pin:
        if (
            tuple(image.get(key) for key in ("tag", "index_digest", "platform_digest"))
            != postgres_pin
        ):
            return False
        expected_tags = {postgres_pin[0]}
    if (
        image.get("repository") != repository
        or not isinstance(tag, str)
        or tag not in expected_tags
        or any(
            not isinstance(image.get(key), str) or not DIGEST.fullmatch(image[key])
            for key in ("index_digest", "platform_digest")
        )
    ):
        return False
    observed = image.get("observed_versions")
    return (
        isinstance(observed, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in observed.items())
        and (not entry["role"].startswith("postgres-") or observed == {"postgresql": "16.15"})
    )


def validate_inventory(document: object) -> list[str]:
    """Control puro: no abre red, procesos, bóvedas, imágenes ni recursos del destino."""
    if not isinstance(document, dict):
        return ["El inventario debe ser un objeto JSON."]
    errors = []
    if set(document) != TOP_FIELDS:
        errors.append("El inventario debe conservar los campos del contrato candidato.")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        errors.append("La versión de esquema no está implementada.")
    if document.get("status") != "candidate" or document.get("audit_status") != "not_verified":
        errors.append("Una declaración de identidad no puede acreditar una auditoría.")
    if any(document.get(key) is not False for key in FALSE_CLAIMS):
        errors.append("No se permite autodeclarar seguridad, runtime, producción o CD aprobado.")
    try:
        timestamp = document.get("as_of_utc")
        if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
            raise ValueError
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        errors.append("La revisión de fuentes requiere fecha UTC válida.")
    if document.get("platform") != "linux/amd64" or document.get("target") != {
        "hostname": "vm-media-01",
        "ip": "192.168.0.85",
        "application_root": "/opt/media-platform",
        "data_root": "/var/lib/media-platform",
        "media_root": "/mnt/producciones",
        "media_source": "192.168.0.8:/volume2/producciones",
    }:
        errors.append("La plataforma o el destino no corresponden al alcance autorizado.")
    observations = document.get("observations")
    if not isinstance(observations, list) or any(
        not isinstance(item, str) for item in observations
    ):
        errors.append("Las observaciones deben ser una lista de textos sin efectos.")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list):
        return errors + ["El inventario requiere la lista completa de candidatos."]
    roles = []
    for entry in artifacts:
        if not isinstance(entry, dict) or not isinstance(entry.get("role"), str):
            errors.append("Un candidato carece de rol válido.")
            continue
        role = entry["role"]
        if role not in POLICIES:
            errors.append("Se declara un rol no revisado; no se ejecuta su contenido.")
            continue
        roles.append(role)
        version, repo, selected, image_repository = POLICIES[role]
        kind = "image" if image_repository else "download"
        if set(entry) != {"role", "kind", "version", "platform", "source", "license", kind}:
            errors.append(f"El candidato {role} contiene campos no admitidos o incompletos.")
        if entry.get("version") != version or entry.get("kind") != kind:
            errors.append(f"El pin o tipo de {role} difiere de la versión revisada.")
        if entry.get("platform") != "linux/amd64":
            errors.append(f"El candidato {role} no corresponde a linux/amd64.")
        source_ok = source_valid(entry.get("source"), repo)
        if not source_ok:
            errors.append(f"La procedencia o licencia fijada de {role} no es válida.")
        if not license_valid(entry.get("license"), role, selected):
            errors.append(f"La licencia o el alcance de uso de {role} difiere del contrato.")
        if image_repository:
            if not image_valid(entry, image_repository):
                errors.append(f"La imagen de {role} requiere repositorio, tag y digests exactos.")
        elif source_ok and entry.get("version") == version and not download_valid(entry):
            errors.append(f"El archivo HTTPS de {role} carece de identidad fijada.")
    if len(roles) != len(set(roles)) or set(roles) != set(POLICIES):
        errors.append("No se pueden omitir, duplicar o sustituir los roles obligatorios.")
    build = document.get("postgres_immich_build")
    if (
        not isinstance(build, dict)
        or build.get("runtime_verified") is not False
        or build
        != {
            "base_role": "postgres-immich-base",
            "postgresql": "16.15",
            "extension_roles": ["pgvector-extension", "vectorchord-extension"],
            "preload": ["vchord.so"],
            "built_image_digest": None,
            "runtime_verified": False,
        }
    ):
        errors.append("La imagen PostgreSQL Immich derivada sigue siendo un plan no ejecutado.")
    return errors


def assess(document: object) -> dict:
    errors = validate_inventory(document)
    return {
        "schema_version": 1,
        "structure_valid": not errors,
        "errors": errors,
        "audit_status": "not_verified",
        "security_clean": False,
        "runtime_verified": False,
        "production_ready": False,
        "deployment_enabled": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    try:
        result = assess(load_json(args.manifest))
    except (OSError, ValueError, UnicodeError, RecursionError):
        # No publicar entradas, rutas aportadas, tokens ni excepciones de carga.
        result = assess(None)
        result["errors"] = ["No se pudo leer un inventario JSON válido y acotado."]
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["structure_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
