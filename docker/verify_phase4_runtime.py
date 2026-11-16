"""Verifica por lectura el inventario operativo de la Fase 4 en vm-media-01."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

PROJECTS = {
    "agency-media": ("postgres-apps", "valkey", "zou-api", "zou-events"),
    "agency-media-ui-refresh-v1": ("kitsu-ui",),
    "agency-n8n-private": ("n8n", "javascript", "python"),
    "agency-media-immich-lan": (
        "postgres-immich",
        "immich-valkey",
        "immich-ml-auth",
        "immich-server",
    ),
    "agency-supoclip-trial-nov13": (
        "postgres",
        "valkey",
        "migrate",
        "backend",
        "worker",
        "model",
        "frontend",
        "proxy",
    ),
    "agency-moneyprinter-lan": ("api", "ui"),
    "agency-observability-nas-v1": ("otel-collector", "node-exporter", "cadvisor"),
}
NO_HEALTHCHECK = {
    "agency-supoclip-trial-nov13/migrate",
    "agency-supoclip-trial-nov13/backend",
    "agency-supoclip-trial-nov13/worker",
    "agency-supoclip-trial-nov13/proxy",
    *(f"agency-observability-nas-v1/{role}" for role in PROJECTS["agency-observability-nas-v1"]),
}
UNITS = {
    "agency-immich-lan.service": "exited",
    "agency-kitsu-zou.service": "running",
    "agency-moneyprinter-lan.service": "running",
    "agency-n8n-private.service": "exited",
    "agency-observability-nas.service": "exited",
    "agency-supoclip-private.service": "exited",
}
R2_OWNER = "agencia-digital-marketing-ai:r2-service:v1"
CID = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
MAX_PINS = 16384
INSPECT = (
    '{"id":{{json .Id}},"image":{{json .Image}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"owner":{{json (index .Config.Labels "agency.owner")}},'
    '"r2_role":{{json (index .Config.Labels "agency.role")}},'
    '"status":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},'
    '"health":{{with index .State "Health"}}{{json .Status}}{{else}}null{{end}}}'
)


class Service(NamedTuple):
    status: str = "running"
    health: bool = True
    optional: bool = False


SERVICES = {
    f"{project}/{role}": Service(
        "exited" if role == "migrate" else "running", f"{project}/{role}" not in NO_HEALTHCHECK
    )
    for project, roles in PROJECTS.items()
    for role in roles
}
SERVICES["r2/serve"] = Service(health=False)
SERVICES["r2/init"] = Service(status="exited", health=False, optional=True)


def unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Claves duplicadas.")
    return result


def read_pins(stream):
    raw = stream.read(MAX_PINS + 1)
    if len(raw) > MAX_PINS:
        raise ValueError("Manifiesto de imágenes demasiado grande.")
    spec = json.loads(raw, object_pairs_hook=unique_fields)
    if (
        not isinstance(spec, dict)
        or set(spec) != {"schema", "images"}
        or type(spec["schema"]) is not int
        or spec["schema"] != 1
    ):
        raise ValueError("Esquema de pines inválido.")
    images = spec["images"]
    if not isinstance(images, dict) or any(
        key not in SERVICES or not isinstance(value, str) or not IMAGE.fullmatch(value)
        for key, value in images.items()
    ):
        raise ValueError("Rol o imagen inválido.")
    return images


def run(*args):
    result = subprocess.run(
        args,
        env={"LC_ALL": "C"},
        capture_output=True,
        check=True,
        timeout=15,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def docker(*args):
    return run("/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args)


def docker_inventory():
    selectors = [f"com.docker.compose.project={project}" for project in PROJECTS]
    selectors.append(f"agency.owner={R2_OWNER}")
    ids = set()
    for selector in selectors:
        found = docker("ps", "-aq", "--no-trunc", "--filter", f"label={selector}").splitlines()
        if any(not CID.fullmatch(cid) for cid in found):
            raise ValueError("CID inválido.")
        ids.update(found)
    if not ids:
        return []
    data = docker("inspect", "--type", "container", "--format", INSPECT, *sorted(ids))
    rows = []
    seen = set()
    for line in data.splitlines():
        item = json.loads(line, object_pairs_hook=unique_fields)
        if item["id"] not in ids or item["id"] in seen:
            raise ValueError("Docker devolvió un CID inesperado.")
        seen.add(item["id"])
        if item["owner"] == R2_OWNER:
            key = f"r2/{item['r2_role']}" if not item["project"] and not item["service"] else None
        elif item["project"] in PROJECTS:
            key = f"{item['project']}/{item['service']}"
        else:
            key = None
        rows.append(
            {
                "key": key,
                **{
                    field: item[field] for field in ("id", "image", "status", "health", "exit_code")
                },
            }
        )
    if seen != ids:
        raise ValueError("Cambió el conjunto de CID durante la lectura.")
    return rows


def systemd_inventory():
    fields = ("ActiveState", "SubState", "UnitFileState", "Result")
    observed = {}
    for unit in UNITS:
        raw = run(
            "/usr/bin/systemctl",
            "show",
            "--no-pager",
            unit,
            *(f"--property={field}" for field in fields),
        )
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        observed[unit] = {field: values.get(field) for field in fields}
    return observed


def verify(rows, units, pins):
    errors = []
    containers = {}
    for row in rows:
        key = row["key"]
        if key not in SERVICES:
            errors.append("docker:unexpected_role")
            continue
        if key in containers:
            errors.append(f"{key}:duplicate")
            continue
        containers[key] = {
            field: row[field] for field in ("id", "image", "status", "health", "exit_code")
        }
        contract = SERVICES[key]
        if not CID.fullmatch(row["id"]) or not IMAGE.fullmatch(row["image"]):
            errors.append(f"{key}:identity")
        if key in pins and row["image"] != pins[key]:
            errors.append(f"{key}:image")
        if row["status"] != contract.status:
            errors.append(f"{key}:status")
        if contract.status == "exited" and row["exit_code"] != 0:
            errors.append(f"{key}:exit_code")
        if (contract.health or row["health"] is not None) and row["health"] != "healthy":
            errors.append(f"{key}:health")
    for key, contract in SERVICES.items():
        if key not in containers and (not contract.optional or key in pins):
            errors.append(f"{key}:missing")
    for unit, substate in UNITS.items():
        expected = {
            "ActiveState": "active",
            "SubState": substate,
            "UnitFileState": "enabled",
            "Result": "success",
        }
        for field, value in expected.items():
            if units.get(unit, {}).get(field) != value:
                errors.append(f"{unit}:{field}")
    return {
        "success": not errors,
        "scope": "phase4-runtime-inventory",
        "containers": containers,
        "units": units,
        "images_pinned": len(pins),
        "errors": sorted(errors),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pins", metavar="RUTA|-", help="Manifiesto JSON de imágenes, o - para stdin"
    )
    args = parser.parse_args(argv)
    try:
        if sys.platform != "linux":
            raise ValueError("Linux requerido.")
        if args.pins == "-":
            pins = read_pins(sys.stdin.buffer)
        elif args.pins:
            with Path(args.pins).open("rb") as stream:
                pins = read_pins(stream)
        else:
            pins = {}
        report = verify(docker_inventory(), systemd_inventory(), pins)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, subprocess.SubprocessError):
        report = {
            "success": False,
            "scope": "phase4-runtime-inventory",
            "errors": ["inventory_read_failed"],
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
