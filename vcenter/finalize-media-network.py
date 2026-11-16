"""Finaliza únicamente los perfiles conocidos de vm-media-01, sin aplicar red en vivo."""

import argparse
import copy
import json
import os
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import yaml

LEGACY_PROFILE = {
    "network": {"version": 2, "ethernets": {"ens192": {"dhcp4": True, "dhcp6": False}}}
}


class UniqueYamlLoader(yaml.SafeLoader):
    """Impide que una clave repetida o una combinación YAML oculte configuración."""


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("Clave de configuración duplicada o no textual.")
        result[key] = value
    return result


def yaml_mapping(loader, node):
    return unique_pairs(
        (loader.construct_object(key, deep=True), loader.construct_object(value, deep=True))
        for key, value in node.value
    )


UniqueYamlLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


@dataclass(frozen=True)
class NetworkPaths:
    active: Path
    legacy: Path
    cache: Path
    instance_id: Path
    instance: Path
    expected_instance: Path
    backups: Path


@dataclass(frozen=True)
class NetworkPlan:
    before: dict
    after: dict
    modes: dict
    changed: bool


def network_paths(root, instance_uuid):
    root = Path(root)
    instance = root / "var/lib/cloud/instances" / str(UUID(instance_uuid))
    return NetworkPaths(
        active=root / "etc/netplan/50-cloud-init.yaml",
        legacy=root / "etc/netplan/50-lab-dhcp.yaml",
        cache=instance / "network-config.json",
        instance_id=root / "var/lib/cloud/data/instance-id",
        instance=root / "var/lib/cloud/instance",
        expected_instance=instance,
        backups=root / "var/lib/media-platform-bootstrap/backups",
    )


def expected_network(mac_address, optional):
    return {
        "version": 2,
        "renderer": "networkd",
        "ethernets": {
            "media0": {
                "match": {"macaddress": mac_address.lower()},
                "optional": optional,
                "addresses": ["192.168.0.85/24"],
                "nameservers": {"addresses": ["192.168.0.1", "1.1.1.1"]},
                "dhcp4": False,
                "dhcp6": False,
                "set-name": "media0",
                "routes": [{"to": "0.0.0.0/0", "via": "192.168.0.1"}],
            }
        },
    }


def same_structure(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            same_structure(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            same_structure(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def regular_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Se requiere un archivo regular sin enlaces: {path.name}.")


def load_yaml(path):
    regular_file(path)
    return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=UniqueYamlLoader)


def dump_yaml(document):
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def build_plan(paths, mac_address):
    """Construye cambios solo si todos los documentos conservan la estructura autorizada."""
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac_address):
        raise ValueError("La MAC esperada no tiene formato válido.")
    profiles = set(paths.active.parent.glob("*.yaml")) | set(paths.active.parent.glob("*.yml"))
    if profiles - {paths.active, paths.legacy}:
        raise ValueError("Hay un perfil de red adicional; se conserva toda la configuración.")
    active = load_yaml(paths.active)
    regular_file(paths.cache)
    cache = json.loads(paths.cache.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs)
    valid_active = any(
        same_structure(active, {"network": expected_network(mac_address, optional)})
        for optional in (True, False)
    )
    if not valid_active:
        raise ValueError("El perfil de media0 tiene campos, MAC, IP, DNS o rutas inesperados.")
    if not any(
        same_structure(cache, expected_network(mac_address, flag)) for flag in (True, False)
    ):
        raise ValueError("La caché de red tiene campos, MAC, IP, DNS o rutas inesperados.")
    if paths.legacy.exists() or paths.legacy.is_symlink():
        if not same_structure(load_yaml(paths.legacy), LEGACY_PROFILE):
            raise ValueError(
                "El perfil heredado no es exactamente ens192 DHCP simple; no se mueve."
            )
    before = {
        paths.active: paths.active.read_bytes(),
        paths.cache: paths.cache.read_bytes(),
        paths.legacy: paths.legacy.read_bytes() if paths.legacy.exists() else None,
    }
    modes = {
        path: path.stat().st_mode & 0o777 for path, content in before.items() if content is not None
    }
    desired_active = copy.deepcopy(active)
    desired_active["network"]["ethernets"]["media0"]["optional"] = False
    desired_cache = copy.deepcopy(cache)
    desired_cache["ethernets"]["media0"]["optional"] = False
    changed = (
        before[paths.legacy] is not None
        or active["network"]["ethernets"]["media0"]["optional"]
        or cache["ethernets"]["media0"]["optional"]
    )
    after = {
        paths.active: dump_yaml(desired_active).encode("utf-8"),
        paths.cache: (json.dumps(desired_cache, indent=2) + "\n").encode("utf-8"),
    }
    return NetworkPlan(before, after, modes, bool(changed))


def validate_context(context, paths, mac_address, instance_uuid):
    """Rechaza cualquier sistema, instancia o interfaz fuera de la VM propia."""
    if (
        context["system"] != "Linux"
        or context["euid"] != 0
        or context["hostname"] != "vm-media-01"
        or context["os_id"] != "ubuntu"
        or context["os_version"] != "24.04"
    ):
        raise ValueError("Se requiere root en Ubuntu 24.04 de vm-media-01.")
    if (
        context["instance_id"].strip() != str(UUID(instance_uuid))
        or Path(context["resolved_instance"]) != paths.expected_instance.resolve()
        or paths.expected_instance.resolve() != paths.expected_instance.absolute()
    ):
        raise ValueError(
            "El UUID o el destino de la instancia cloud-init no coincide con el respaldado."
        )
    interface = context["interface"]
    addresses = [
        (item.get("local"), item.get("prefixlen"))
        for item in interface.get("addr_info", [])
        if item.get("family") == "inet"
    ]
    if (
        interface.get("ifname") != "media0"
        or interface.get("address", "").lower() != mac_address.lower()
        or addresses != [("192.168.0.85", 24)]
    ):
        raise ValueError("La interfaz media0, su MAC o la IPv4 no coinciden con la VM propia.")


def atomic_write(path, content, mode):
    temporary = path.with_name("." + path.name + ".media-finalize.tmp")
    created = False
    try:
        with temporary.open("xb") as stream:
            created = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def apply_plan(paths, plan, runner, stamp):
    """Archiva un único legado y prepara la red del próximo arranque; no aplica la red viva."""
    profiles = set(paths.active.parent.glob("*.yaml")) | set(paths.active.parent.glob("*.yml"))
    if profiles - {paths.active, paths.legacy}:
        raise ValueError("Apareció un perfil adicional después de preparar el plan; se conserva.")
    if paths.active.parent.resolve() != paths.active.parent.absolute():
        raise ValueError("El directorio netplan no puede resolverse mediante enlaces inesperados.")
    for path, original in plan.before.items():
        if original is None:
            if path.exists() or path.is_symlink():
                raise ValueError("La configuración cambió después de preparar el plan.")
        else:
            regular_file(path)
            if path.read_bytes() != original:
                raise ValueError("La configuración cambió después de preparar el plan.")
    for path in plan.after:
        temporary = path.with_name("." + path.name + ".media-finalize.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise ValueError("Hay un archivo temporal preexistente; se conserva sin modificarlo.")
    backup = None
    if plan.changed:
        if paths.backups.resolve() != paths.backups.absolute() or not re.fullmatch(
            r"[A-Za-z0-9_-]+", stamp
        ):
            raise ValueError("El destino de respaldo debe ser local y no un enlace.")
        paths.backups.mkdir(parents=True, exist_ok=True)
        backup = paths.backups / ("network-finalize-" + stamp)
        backup.mkdir(mode=0o700)
        backup.chmod(0o700)
        shutil.copyfile(paths.active, backup / paths.active.name)
        shutil.copyfile(paths.cache, backup / paths.cache.name)
        for saved in backup.iterdir():
            saved.chmod(0o600)
    try:
        if plan.changed:
            if plan.before[paths.legacy] is not None:
                # Ambos extremos son archivos exactos previamente validados; nunca se mueve un árbol.
                paths.legacy.rename(backup / paths.legacy.name)
                (backup / paths.legacy.name).chmod(0o600)
            for path, content in plan.after.items():
                atomic_write(path, content, plan.modes[path])
        for arguments in (["netplan", "generate"], ["cloud-init", "schema", "--system"]):
            if runner(arguments).returncode != 0:
                raise RuntimeError("La validación de red o cloud-init no terminó correctamente.")
    except Exception as failure:
        if plan.changed:
            for path in (paths.active, paths.cache):
                atomic_write(path, plan.before[path], plan.modes[path])
            archived = backup / paths.legacy.name
            if archived.exists():
                archived.rename(paths.legacy)
                paths.legacy.chmod(plan.modes[paths.legacy])
            regenerated = runner(["netplan", "generate"])
            if regenerated.returncode != 0:
                raise RuntimeError(
                    "Archivos restaurados, pero netplan no regeneró el estado previo."
                ) from failure
        raise RuntimeError(
            "La validación falló; se restauraron los archivos previos sin aplicar red viva."
        ) from failure
    return {
        "changed": plan.changed,
        "backup_directory": str(backup) if backup else None,
        "live_network_applied": False,
        "reboot_verification_required": True,
    }


def run_validation(arguments):
    # Se conserva solo el código de salida: schema puede leer datos de usuario con llaves respaldadas.
    return subprocess.run(arguments, capture_output=True, check=False, timeout=90)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac-address", required=True)
    parser.add_argument("--instance-uuid", required=True, type=lambda value: str(UUID(value)))
    parser.add_argument(
        "--apply", action="store_true", help="Archivar el legado y preparar el próximo arranque."
    )
    arguments = parser.parse_args()
    if platform.system() != "Linux" or os.geteuid() != 0:
        parser.error("Este comando solo puede ejecutarse como root en la VM Linux autorizada.")
    paths = network_paths(Path("/"), arguments.instance_uuid)
    release = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    address_result = subprocess.run(
        ["ip", "-j", "address", "show", "dev", "media0"], capture_output=True, check=True, text=True
    )
    interfaces = json.loads(address_result.stdout)
    if len(interfaces) != 1:
        parser.error("La interfaz media0 no es única.")
    context = {
        "system": platform.system(),
        "euid": os.geteuid(),
        "hostname": socket.gethostname(),
        "os_id": release.get("ID", "").strip('"'),
        "os_version": release.get("VERSION_ID", "").strip('"'),
        "instance_id": paths.instance_id.read_text(),
        "resolved_instance": str(paths.instance.resolve(strict=True)),
        "interface": interfaces[0],
    }
    validate_context(context, paths, arguments.mac_address, arguments.instance_uuid)
    for path in (paths.active, paths.cache, paths.instance_id):
        regular_file(path)
        if path.stat().st_uid != 0:
            raise ValueError("Los archivos de red e instancia deben pertenecer a root.")
    if paths.legacy.exists():
        regular_file(paths.legacy)
        if paths.legacy.stat().st_uid != 0:
            raise ValueError("El perfil heredado debe pertenecer a root.")
    plan = build_plan(paths, arguments.mac_address)
    result = {"changed": plan.changed, "simulation": True, "live_network_applied": False}
    if arguments.apply:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + str(os.getpid())
        result = apply_plan(paths, plan, run_validation, stamp) | {"simulation": False}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
