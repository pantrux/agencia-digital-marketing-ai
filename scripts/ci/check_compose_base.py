"""Contrato cerrado de la base Compose; config usa sólo entradas sintéticas."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ("postgres-apps", "valkey", "zou-api", "zou-events")
IMAGES = dict(
    zip(
        SERVICES,
        (
            "MEDIA_POSTGRES_APPS_IMAGE_ID",
            "MEDIA_VALKEY_OCI_CAPTURE_V2_MANIFEST_DIGEST",
            "MEDIA_ZOU_APPLICATION_IMAGE_MANIFEST",
            "MEDIA_ZOU_APPLICATION_IMAGE_MANIFEST",
        ),
        strict=True,
    )
)
SECRET_FILES = {
    "postgres_password": "MEDIA_POSTGRES_APPS_PASSWORD_FILE",
    "postgres_pgpass": "MEDIA_POSTGRES_APPS_PGPASS_FILE",
    "valkey_password": "MEDIA_VALKEY_PASSWORD_FILE",
    "zou_db_password": "MEDIA_ZOU_DB_PASSWORD_FILE",
    "zou_kv_password": "MEDIA_ZOU_KV_PASSWORD_FILE",
    "zou_secret_key": "MEDIA_ZOU_SECRET_KEY_FILE",
}
PG_HEALTH = (
    "exec psql -X -q -A -t -w -h 127.0.0.1 -p 5432 "
    '-U "$${POSTGRES_USER}" -d "$${POSTGRES_DB}" '
    '--set=ON_ERROR_STOP=1 -c "SELECT 1"'
)
VALKEY_HEALTH = (
    "export REDISCLI_AUTH; IFS= read -r REDISCLI_AUTH < /run/secrets/valkey_password; "
    "exec valkey-cli --no-auth-warning -e -h 127.0.0.1 -p 6379 ping"
)
TMP = "/tmp:rw,nosuid,nodev,noexec,size={size}m,mode=1777"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def ref(name: str) -> str:
    return "${" + name + ":?Falta " + name + "}"


def load_model(raw: str) -> dict:
    """SafeLoader con rechazo de claves repetidas, también en mapas anidados."""

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            require(isinstance(key, str) and key not in result, "Clave YAML repetida o inválida")
            result[key] = loader.construct_object(value_node)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        model = yaml.load(raw, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise ValueError("YAML inválido") from exc
    require(isinstance(model, dict), "Se requiere objeto Compose")
    return model


def http_health(port: int, kind: str) -> list[str]:
    prefix = (
        "import json,urllib.request; "
        "r=urllib.request.build_opener(urllib.request.ProxyHandler({})).open("
        f"'http://127.0.0.1:{port}/',timeout=2); "
        "b=r.read(4097);r.close();d=json.loads(b); "
        "assert r.status==200 and len(b)<=4096 and isinstance(d,dict) and "
    )
    identity = (
        "isinstance(d.get('api'),str) and bool(d['api']) and "
        "isinstance(d.get('version'),str) and bool(d['version'])"
    )
    suffix = (
        identity
        if kind == "api"
        else (
            "((" + identity + ") or "
            "(isinstance(d.get('name'),str) and d['name'].endswith(' Event stream')))"
        )
    )
    return ["CMD", "/usr/bin/python3.12", "-I", "-S", "-B", "-c", prefix + suffix]


def bind(source: str, target: str, *, readonly: bool = False) -> dict:
    return {
        "type": "bind",
        "source": ref(source),
        "target": target,
        "read_only": readonly,
        "bind": {"create_host_path": False},
    }


def environment(name: str) -> dict:
    if name == "postgres-apps":
        return {
            "PGDATA": "/var/lib/postgresql/data",
            "POSTGRES_USER": ref("MEDIA_POSTGRES_APPS_ADMIN_USER"),
            "POSTGRES_DB": ref("MEDIA_POSTGRES_APPS_ADMIN_DATABASE"),
            "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres_password",
            "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256",
            "POSTGRES_INITDB_ARGS": (
                "--data-checksums --auth-host=scram-sha-256 --auth-local=scram-sha-256"
            ),
            "PGPASSFILE": "/run/secrets/postgres_pgpass",
        }
    return {
        "DB_HOST": "postgres-apps",
        "DB_PORT": "5432",
        "DB_USERNAME": ref("MEDIA_ZOU_DATABASE_USER"),
        "DB_DATABASE": ref("MEDIA_ZOU_DATABASE_NAME"),
        "KV_HOST": "valkey",
        "KV_PORT": "6379",
        "DOMAIN_NAME": ref("MEDIA_ZOU_DOMAIN_NAME"),
        "DOMAIN_PROTOCOL": ref("MEDIA_ZOU_DOMAIN_PROTOCOL"),
    }


def validate_model(model: dict) -> None:
    """Rechaza ampliaciones de acceso, secretos, descargas y servicios ocultos."""
    require(set(model) == {"name", "services", "networks", "secrets"}, "Ámbito Compose ampliado")
    require(model["name"] == ref("MEDIA_COMPOSE_PROJECT_NAME"), "Proyecto obligatorio")
    require(set(model["services"]) == set(SERVICES), "Se requieren cuatro servicios explícitos")
    require(
        model["networks"]
        == {
            "data": {"external": True, "name": ref("MEDIA_PLATFORM_DATA_NETWORK_NAME")},
            "app": {"external": True, "name": ref("MEDIA_PLATFORM_APP_NETWORK_NAME")},
        },
        "Redes externas obligatorias sin valores predeterminados",
    )
    require(
        model["secrets"] == {k: {"file": ref(v)} for k, v in SECRET_FILES.items()},
        "Los secretos deben ser sólo archivos explícitos",
    )
    for name in SERVICES:
        s = model["services"][name]
        pg, cache = name == "postgres-apps", name == "valkey"
        app = not (pg or cache)
        expected = {
            "image": ref(IMAGES[name]),
            "pull_policy": "never",
            "platform": "linux/amd64",
            "user": "70:70" if pg else "1000:1000",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "restart": "no" if app else "unless-stopped",
            "cpus": "1.0" if pg or name == "zou-api" else "0.5",
            "mem_limit": "768m" if pg or name == "zou-api" else "256m",
            "pids_limit": 128 if pg else 64,
            "stop_grace_period": "30s",
            "networks": ["data", "app"] if app else ["data"],
            "logging": {"driver": "local", "options": {"max-size": "10m", "max-file": "2"}},
            "tmpfs": [TMP.format(size=64 if pg or name == "zou-api" else 32)],
        }
        expected["memswap_limit"] = expected["mem_limit"]
        if pg:
            expected.update(
                {
                    "shm_size": "128m",
                    "environment": environment(name),
                    "secrets": ["postgres_password", "postgres_pgpass"],
                    "volumes": [bind("MEDIA_POSTGRES_APPS_DATA_PATH", "/var/lib/postgresql/data")],
                }
            )
            expected["tmpfs"].append(
                "/var/run/postgresql:rw,nosuid,nodev,noexec,size=16m,uid=70,gid=70,mode=0775"
            )
        elif cache:
            expected.update(
                {
                    "command": ["valkey-server", "/etc/valkey/valkey.conf"],
                    "secrets": ["valkey_password"],
                    "volumes": [
                        bind("MEDIA_VALKEY_DATA_PATH", "/data"),
                        bind("MEDIA_VALKEY_CONFIG_FILE", "/etc/valkey/valkey.conf", readonly=True),
                    ],
                }
            )
        else:
            expected.update(
                {
                    "command": [
                        "/usr/bin/python3.12",
                        "-I",
                        "-S",
                        "-B",
                        "/opt/zou/launch_zou.py",
                        "api" if name == "zou-api" else "events",
                    ],
                    "environment": environment(name),
                    "secrets": ["zou_db_password", "zou_kv_password", "zou_secret_key"],
                    "volumes": [
                        bind("MEDIA_ZOU_STATE_PATH", "/var/lib/zou"),
                        bind("MEDIA_ZOU_PREVIEWS_PATH", "/var/lib/zou/previews"),
                    ],
                    "depends_on": {
                        "postgres-apps": {"condition": "service_healthy"},
                        "valkey": {"condition": "service_healthy"},
                    },
                }
            )
            if name == "zou-api":
                expected["ports"] = ["127.0.0.1:5000:5000"]
        health = (
            ["CMD-SHELL", PG_HEALTH if pg else VALKEY_HEALTH]
            if not app
            else http_health(
                5000 if name == "zou-api" else 5001, "api" if name == "zou-api" else "events"
            )
        )
        expected["healthcheck"] = {
            "test": health,
            "interval": "15s",
            "timeout": "5s",
            "retries": 4,
            "start_period": "60s" if pg else "20s" if cache else "40s",
        }
        require(s == expected, f"Contrato de servicio alterado: {name}")


def required_variables(raw: str) -> set[str]:
    matches = re.findall(r"(?<!\$)\$\{([A-Z][A-Z0-9_]*):\?Falta \1\}", raw)
    remaining = re.sub(r"(?<!\$)\$\{([A-Z][A-Z0-9_]*):\?Falta \1\}", "", raw)
    require("${" not in remaining.replace("$${", ""), "Interpolación no obligatoria")
    return set(matches)


def synthetic_values(path: Path, raw: str) -> dict[str, str]:
    data = path.read_bytes()
    require(len(data) <= 16384, "Entorno sintético demasiado grande")
    values = {}
    for line in data.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        require(
            bool(re.fullmatch(r"[A-Z][A-Z0-9_]*=[A-Za-z0-9_./:-]+", line)),
            "Entorno sintético literal inválido",
        )
        key, value = line.split("=", 1)
        require(key not in values, "Variable sintética repetida")
        values[key] = value
    require(set(values) == required_variables(raw), "Cobertura sintética distinta del Compose")
    for key, value in values.items():
        if key in set(IMAGES.values()):
            digit = {
                "MEDIA_POSTGRES_APPS_IMAGE_ID": "1",
                "MEDIA_VALKEY_OCI_CAPTURE_V2_MANIFEST_DIGEST": "2",
                "MEDIA_ZOU_APPLICATION_IMAGE_MANIFEST": "3",
            }[key]
            require(value == "sha256:" + digit * 64, "ID sintético requerido")
        else:
            require("synthetic" in value or value == "http", "Valor no sintético")
            if key.endswith(("_PATH", "_FILE")):
                if key == "MEDIA_ZOU_PREVIEWS_PATH":
                    prefix = "/mnt/producciones/ci-synthetic/"
                else:
                    prefix = (
                        "/var/lib/media-platform/"
                        if key.endswith("_PATH")
                        else "/opt/media-platform/"
                    )
                require(value.startswith(prefix) and ".." not in value, "Ruta sintética ajena")
    return values


def config_environment(directory: Path) -> dict[str, str]:
    # Nunca hereda credenciales, MEDIA_*, DOCKER_HOST ni la configuración del operador.
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update(
        {
            "HOME": str(directory),
            "USERPROFILE": str(directory),
            "TMP": str(directory),
            "TEMP": str(directory),
            "TMPDIR": str(directory),
            "DOCKER_CONFIG": str(directory),
            "PATH": os.defpath,
            "COMPOSE_DISABLE_ENV_FILE": "true",
        }
    )
    return env


def verify_compose(compose: Path, synthetic: Path) -> None:
    docker = shutil.which("docker")
    require(docker is not None, "Docker Compose no disponible; config --quiet sigue pendiente")
    with tempfile.TemporaryDirectory(prefix="agency-compose-config-") as folder:
        directory = Path(folder)
        env = config_environment(directory)
        env["PATH"] = str(Path(docker).parent) + os.pathsep + env["PATH"]
        command = [
            docker,
            "compose",
            "--project-directory",
            str(directory),
            "--env-file",
            str(synthetic.resolve()),
            "-f",
            str(compose.resolve()),
            "config",
            "--quiet",
        ]
        # config no consulta el daemon; no imprime la configuración expandida.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(
                command,
                cwd=directory,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=30,
                check=False,
            )
            sizes = stdout.tell(), stderr.tell()
        require(result.returncode == 0 and max(sizes) <= 65536, "Compose config --quiet falló")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="Sólo contrato; declara config Compose pendiente",
    )
    args = parser.parse_args()
    try:
        compose, synthetic = (
            ROOT / "docker/docker-compose.yml",
            ROOT / "docker/compose.synthetic.env",
        )
        raw = compose.read_text(encoding="utf-8")
        validate_model(load_model(raw))
        synthetic_values(synthetic, raw)
        if not args.structure_only:
            verify_compose(compose, synthetic)
        print(
            json.dumps(
                {
                    "structure_verified": True,
                    "compose_config_verified": not args.structure_only,
                    "runtime_verified": False,
                    "production_approved": False,
                }
            )
        )
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"verified": False, "error": str(exc), "runtime_verified": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
