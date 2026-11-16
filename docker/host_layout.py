"""Plan y creación explícita de las dos raíces propias; no instala aplicaciones."""

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import socket
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

OWNER = "agencia-digital-marketing-ai:media-platform:v4.1"
TARGETS = {"/opt/media-platform": "code", "/var/lib/media-platform": "state"}
ANCESTORS = ("/", "/opt", "/var", "/var/lib")
MARKER = ".agencia-media-owner.json"
RECEIPT = ".agencia-media-layout-receipt.json"
SHA256 = re.compile(r"[a-f0-9]{64}")


class LayoutError(ValueError):
    """La identidad, propiedad o aprobación no permite realizar la operación."""


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def plan_hash(plan):
    return digest({key: value for key, value in plan.items() if key != "plan_sha256"})


def host_contract(identity, *, apply=False):
    if (
        identity.get("system") != "Linux"
        or identity.get("hostname") != "vm-media-01"
        or "192.168.0.85" not in identity.get("ipv4", ())
    ):
        raise LayoutError("Se requiere Linux, vm-media-01 y su IPv4 declarada.")
    if apply and identity.get("uid") != 0:
        raise LayoutError("La aplicación requiere UID efectivo 0.")
    return {"system": "Linux", "hostname": "vm-media-01", "ipv4": sorted(set(identity["ipv4"]))}


def valid_timestamp(value):
    try:
        return (
            isinstance(value, str)
            and datetime.fromisoformat(value).utcoffset().total_seconds() == 0
        )
    except (ValueError, AttributeError):
        return False


def directory_identity(node):
    return {key: node[key] for key in ("device", "inode")}


def new_marker(path, node, approved_hash, timestamp):
    return {
        "schema_version": 1,
        "owner": OWNER,
        "path": path,
        "role": TARGETS[path],
        "hostname": "vm-media-01",
        "ipv4": "192.168.0.85",
        "directory": directory_identity(node),
        "created_at_utc": timestamp,
        "creation_plan_sha256": approved_hash,
    }


def new_receipt(marker, approved_hash):
    return {
        "schema_version": 1,
        "owner": OWNER,
        "path": marker["path"],
        "directory": marker["directory"],
        "marker_sha256": digest(marker),
        "completed_with_plan_sha256": approved_hash,
        "completed_at_utc": datetime.now(UTC).isoformat(),
    }


def check_directory(node, *, own=False):
    if not isinstance(node, dict) or any(
        key not in node for key in ("kind", "uid", "gid", "mode", "device", "inode")
    ):
        raise LayoutError("El destino o un ancestro no tiene metadatos completos.")
    if (
        node["kind"] != "directory"
        or node["uid"] != 0
        or node["gid"] != 0
        or node["mode"] & 0o022
        or (own and node["mode"] != 0o700)
    ):
        raise LayoutError(
            "Un destino o ancestro es un enlace, ajeno o tiene permisos incompatibles."
        )


def target_state(path, target):
    if target is None:
        return "absent"
    check_directory(target.get("node"), own=True)
    marker = target.get("marker")
    if (
        not isinstance(marker, dict)
        or not valid_timestamp(marker.get("created_at_utc"))
        or not SHA256.fullmatch(str(marker.get("creation_plan_sha256", "")))
    ):
        raise LayoutError("El directorio existente carece de un marcador propio válido.")
    expected = new_marker(
        path, target["node"], marker["creation_plan_sha256"], marker["created_at_utc"]
    )
    if marker != expected:
        raise LayoutError("El marcador no acredita ruta, rol, host y directorio exactos.")
    receipt = target.get("receipt")
    if receipt is None:
        if target.get("extra_entries") != 0:
            raise LayoutError(
                "Un directorio incompleto contiene entradas adicionales; no se adopta."
            )
        return "partial"
    if (
        not isinstance(receipt, dict)
        or set(receipt) != set(new_receipt(marker, "0" * 64))
        or not SHA256.fullmatch(str(receipt.get("completed_with_plan_sha256", "")))
        or not valid_timestamp(receipt.get("completed_at_utc"))
    ):
        raise LayoutError("El recibo propio no es válido.")
    expected = new_receipt(marker, receipt["completed_with_plan_sha256"])
    expected["completed_at_utc"] = receipt["completed_at_utc"]
    if receipt != expected:
        raise LayoutError("El recibo no corresponde al marcador y al directorio observados.")
    return "owned"


def make_plan(inventory, identity):
    host = host_contract(identity)
    if set(inventory.get("ancestors", {})) != set(ANCESTORS) or set(
        inventory.get("targets", {})
    ) != set(TARGETS):
        raise LayoutError(
            "El inventario no contiene exclusivamente los destinos y ancestros declarados."
        )
    for node in inventory["ancestors"].values():
        check_directory(node)
    targets = []
    for path, role in TARGETS.items():
        state = target_state(path, inventory["targets"][path])
        action = {"absent": "create", "partial": "finish_receipt", "owned": "keep"}[state]
        targets.append({"path": path, "role": role, "state": state, "action": action})
    result = {
        "schema_version": 1,
        "owner": OWNER,
        "host": host,
        "inventory": inventory,
        "targets": targets,
    }
    result["plan_sha256"] = plan_hash(result)
    return result


def apply_plan(filesystem, identity, plan, approved_hash):
    host_contract(identity, apply=True)
    if (
        not isinstance(approved_hash, str)
        or not SHA256.fullmatch(approved_hash)
        or not hmac.compare_digest(plan_hash(plan), approved_hash)
        or plan.get("plan_sha256") != approved_hash
    ):
        raise LayoutError("El hash aprobado no corresponde al plan recibido.")
    current = make_plan(filesystem.inspect(), identity)
    if not hmac.compare_digest(current["plan_sha256"], approved_hash):
        raise LayoutError(
            "El plan quedó obsoleto; se requiere nueva inspección y aprobación del hash."
        )
    results = []
    for item in current["targets"]:
        path = item["path"]
        observation = make_plan(filesystem.inspect(), identity)
        if observation["inventory"]["ancestors"] != current["inventory"]["ancestors"]:
            raise LayoutError("Un ancestro cambió durante la aplicación.")
        for pending in current["targets"][len(results) :]:
            pending_path = pending["path"]
            if (
                observation["inventory"]["targets"][pending_path]
                != current["inventory"]["targets"][pending_path]
            ):
                raise LayoutError("Un destino pendiente cambió después de comprobar el plan.")
        if item["action"] == "create":
            filesystem.create_root(path, approved_hash, identity)
        if item["action"] != "keep":
            filesystem.finish_receipt(path, approved_hash, identity)
        target = filesystem.inspect()["targets"][path]
        if target_state(path, target) != "owned":
            raise LayoutError("La creación no terminó con un recibo propio comprobado.")
        results.append(
            {
                "path": path,
                "role": item["role"],
                "changed": item["action"] != "keep",
                "directory": directory_identity(target["node"]),
                "marker_sha256": digest(target["marker"]),
                "receipt_sha256": digest(target["receipt"]),
            }
        )
    return {"success": True, "owner": OWNER, "plan_sha256": approved_hash, "targets": results}


def node_data(info):
    kind = (
        "symlink"
        if stat.S_ISLNK(info.st_mode)
        else ("directory" if stat.S_ISDIR(info.st_mode) else "file")
    )
    return {
        "kind": kind,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "device": info.st_dev,
        "inode": info.st_ino,
    }


class LocalFilesystem:
    """Acceso POSIX por descriptores; no sigue enlaces ni recorre árboles de aplicaciones."""

    def __init__(self, *, apply=False):
        self.apply_enabled = apply
        self.descriptors = {}

    def __enter__(self):
        if platform.system() != "Linux":
            raise LayoutError("El acceso de directorios requiere Linux.")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            for path in ANCESTORS:
                if path == "/":
                    descriptor = os.open("/", flags)
                else:
                    parent = str(PurePosixPath(path).parent)
                    descriptor = os.open(
                        PurePosixPath(path).name, flags, dir_fd=self.descriptors[parent]
                    )
                self.descriptors[path] = descriptor
                check_directory(node_data(os.fstat(descriptor)))
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_error):
        for descriptor in reversed(list(self.descriptors.values())):
            os.close(descriptor)
        self.descriptors.clear()

    def ancestors(self):
        result = {}
        for path, descriptor in self.descriptors.items():
            info = os.fstat(descriptor)
            if path != "/":
                parent = self.descriptors[str(PurePosixPath(path).parent)]
                linked = os.stat(PurePosixPath(path).name, dir_fd=parent, follow_symlinks=False)
                if node_data(linked) != node_data(info):
                    raise LayoutError(
                        "La ruta de un ancestro dejó de corresponder al descriptor abierto."
                    )
            result[path] = node_data(info)
            check_directory(result[path])
        return result

    def open_target(self, path):
        parent = self.descriptors[str(PurePosixPath(path).parent)]
        descriptor = os.open(
            PurePosixPath(path).name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            linked = os.stat(PurePosixPath(path).name, dir_fd=parent, follow_symlinks=False)
            if node_data(os.fstat(descriptor)) != node_data(linked):
                raise LayoutError("El destino cambió al abrir su descriptor.")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def read_document(directory, name):
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > 8192
            ):
                raise LayoutError("El marcador o recibo no es un archivo privado propio.")
            content = stream.read(8193)
        try:
            value = json.loads(content)
            if len(content) > 8192 or canonical(value) != content:
                raise ValueError
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise LayoutError("El marcador o recibo no contiene JSON canónico válido.") from None

    def inspect(self):
        ancestors = self.ancestors()
        targets = {}
        for path in TARGETS:
            parent = self.descriptors[str(PurePosixPath(path).parent)]
            try:
                info = os.stat(PurePosixPath(path).name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                targets[path] = None
                continue
            check_directory(node_data(info), own=True)
            descriptor = self.open_target(path)
            try:
                targets[path] = {
                    "node": node_data(os.fstat(descriptor)),
                    "marker": self.read_document(descriptor, MARKER),
                    "receipt": self.read_document(descriptor, RECEIPT),
                    "extra_entries": sum(
                        name not in (MARKER, RECEIPT) for name in os.listdir(descriptor)
                    ),
                }
            finally:
                os.close(descriptor)
        return {"ancestors": ancestors, "targets": targets}

    def require_apply(self):
        if not self.apply_enabled or platform.system() != "Linux" or os.geteuid() != 0:
            raise LayoutError("La escritura requiere apply explícito y Linux root.")

    @staticmethod
    def write_document(directory, name, value):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(name, flags, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)

    def create_root(self, path, approved_hash, identity):
        self.require_apply()
        if path not in TARGETS:
            raise LayoutError("Destino fuera del contrato fijo.")
        observation = make_plan(self.inspect(), identity)
        if observation["inventory"]["targets"][path] is not None:
            raise LayoutError("El destino dejó de estar ausente; no se adopta.")
        parent = self.descriptors[str(PurePosixPath(path).parent)]
        os.mkdir(PurePosixPath(path).name, mode=0o700, dir_fd=parent)
        descriptor = self.open_target(path)
        try:
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, 0o700)
            marker = new_marker(
                path, node_data(os.fstat(descriptor)), approved_hash, datetime.now(UTC).isoformat()
            )
            self.write_document(descriptor, MARKER, marker)
            os.fsync(parent)
        finally:
            os.close(descriptor)

    def finish_receipt(self, path, approved_hash, identity):
        self.require_apply()
        observation = make_plan(self.inspect(), identity)
        target = observation["inventory"]["targets"][path]
        if target_state(path, target) != "partial":
            raise LayoutError(
                "Sólo se completa un directorio parcial acreditado y sin entradas ajenas."
            )
        descriptor = self.open_target(path)
        try:
            if node_data(os.fstat(descriptor)) != target["node"]:
                raise LayoutError("El directorio cambió antes de escribir su recibo.")
            if self.read_document(descriptor, MARKER) != target["marker"]:
                raise LayoutError("El marcador cambió antes de escribir el recibo.")
            self.write_document(descriptor, RECEIPT, new_receipt(target["marker"], approved_hash))
        finally:
            os.close(descriptor)


def inspect_host():
    if platform.system() != "Linux":
        raise LayoutError("El inventario de host sólo admite Linux.")
    executable = next(
        (path for path in ("/usr/sbin/ip", "/usr/bin/ip") if Path(path).is_file()), None
    )
    if executable is None:
        raise LayoutError("Falta iproute2 para comprobar la identidad IPv4 real.")
    response = subprocess.run(
        [executable, "-j", "-4", "address", "show"], check=True, capture_output=True, timeout=5
    )
    if len(response.stdout) > 65536:
        raise LayoutError("El inventario IPv4 supera el límite admitido.")
    addresses = json.loads(response.stdout)
    return {
        "system": "Linux",
        "uid": os.geteuid(),
        "hostname": socket.gethostname(),
        "ipv4": sorted(
            {
                entry["local"]
                for interface in addresses
                for entry in interface.get("addr_info", [])
                if entry.get("family") == "inet"
            }
        ),
    }


def read_plan(path):
    if not path.is_absolute() or path.resolve(strict=True) != path.absolute():
        raise LayoutError("El plan requiere un archivo absoluto sin enlaces.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            raise LayoutError("El archivo del plan no es regular, único o tiene tamaño excesivo.")
        content = stream.read(65537)
    if len(content) > 65536:
        raise LayoutError("El plan supera el tamaño admitido.")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise LayoutError("El plan requiere un objeto JSON.")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Inspección predeterminada de raíces propias de medios."
    )
    parser.add_argument("mode", nargs="?", choices=("plan", "apply"), default="plan")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-plan-sha256", default="")
    arguments = parser.parse_args()
    if arguments.mode == "apply" and (arguments.plan is None or not arguments.expected_plan_sha256):
        parser.error("apply requiere --plan y --expected-plan-sha256")
    if arguments.mode == "plan" and (arguments.plan is not None or arguments.expected_plan_sha256):
        parser.error("La inspección no admite argumentos de aplicación.")
    try:
        identity = inspect_host()
        host_contract(identity, apply=arguments.mode == "apply")
        with LocalFilesystem(apply=arguments.mode == "apply") as filesystem:
            if arguments.mode == "plan":
                result = make_plan(filesystem.inspect(), identity)
            else:
                result = apply_plan(
                    filesystem, identity, read_plan(arguments.plan), arguments.expected_plan_sha256
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LayoutError, OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({"success": False, "error": "IDENTIDAD_PROPIEDAD_O_PLAN_RECHAZADO"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
