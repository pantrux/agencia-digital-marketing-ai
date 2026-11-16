"""Plan local predeterminado; cada transición remota y respaldo requiere CLI de raíz."""

import argparse
import base64
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import shlex
import signal
import stat
import struct
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
PREFIX = "MEDIA_POSTGRES_IMMICH_SDK_"
INVENTORY_SHA = "dc23c9100b38b36f9973319ab045976fa627aeabbbf80c1618b976d2a643ad3e"
KEYS = {
    "plan": PREFIX + "CONTROLLER_B64",
    "prepared": PREFIX + "PREPARED_B64",
    "container": PREFIX + "CONTAINER_B64",
    "stages": PREFIX + "STAGE_RECEIPTS_B64",
}


def read_source(path, maximum=256 * 1024):
    path = Path(path)
    if path.resolve(strict=True) != path.absolute():
        raise ValueError("La fuente contiene enlaces o rutas no canónicas.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > maximum
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Fuente no ordinaria o excesiva.")
        data = stream.read(maximum + 1)
        if len(data) != info.st_size:
            raise ValueError("La fuente cambió de tamaño.")
        return data


def load(name, path, expected=None):
    data = read_source(path)
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Un helper difiere del hash respaldado.")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contract():
    return load("pg_sdk_contract", Path(__file__).with_name("offline_sdk_remote.py"))


def decode(value, *, optional=False):
    if optional and (not value or value in {"CHANGE_ME", "COMPLETAR_EN_FASE_4"}):
        return {}
    data = base64.b64decode(value, validate=True)
    if len(data) > 256 * 1024:
        raise ValueError("Documento de bóveda excesivo.")
    result = contract().json_object(data)
    if not isinstance(result, dict):
        raise ValueError("La bóveda requiere un objeto JSON.")
    return result


def proposal(workspace=PROJECT):
    remote = contract()
    historical = read_source(
        workspace / ".tools/evidence/phase4-postgres-immich-offline-container-plan.json", 65536
    )
    inventory_data = read_source(
        workspace / ".tools/evidence/phase4-postgres-immich-offline-inputs.json", 256 * 1024
    )
    if (
        remote.digest(historical) != remote.HISTORICAL_SHA
        or remote.digest(inventory_data) != INVENTORY_SHA
    ):
        raise ValueError("El plan histórico o el inventario de 78 archivos fueron alterados.")
    inventory = remote.json_object(inventory_data)
    files = {
        name: {key: item[key] for key in ("sha256", "size")}
        for name, item in inventory["files"].items()
    }
    remote.validate_inventory(files)
    if inventory["checksum_manifest_sha256"] != remote.KIT_SHA:
        raise ValueError("El pin del kit difiere.")
    args = remote.derive_create_arguments(remote.json_object(historical)["create_arguments"])
    return {
        "schema_version": 1,
        "plan_revision": 2,
        "owner": remote.OWNER,
        "build_id": remote.BUILD_ID,
        "staging": remote.STAGING,
        "inputs": remote.INPUTS,
        "outputs": remote.OUTPUTS,
        "control": remote.CONTROL,
        "historical_sha256": remote.HISTORICAL_SHA,
        "historical_plan_b64": base64.b64encode(historical).decode(),
        "kit_sha256": remote.KIT_SHA,
        "files": files,
        "create_arguments": args,
        "controller_sha256": remote.digest(read_source(Path(__file__).resolve())),
        "remote_sha256": remote.digest(
            read_source(Path(__file__).with_name("offline_sdk_remote.py"))
        ),
        "layout_helper_sha256": remote.digest(read_source(workspace / "docker/host_layout.py")),
        "env_helper_sha256": remote.digest(read_source(workspace / "scripts/ci/check_env.py")),
        "vault_helper_sha256": remote.digest(read_source(workspace / ".tools/vault_updates.py")),
        "target": {"hostname": "vm-media-01", "ip": "192.168.0.85", "port": 22, "user": "jandrade"},
        "budget_seconds": {"sdk": 600, "graph": 180},
        "capture_limit_bytes": remote.MAX_OUTPUT,
        "sdk_executed": False,
        "docker_executed": False,
        "bound_to_vault": False,
        "input_bytes_verified": False,
    }


def verify_local_input(path, expected):
    if path.resolve(strict=True) != path.absolute():
        raise ValueError("Un archivo local usa enlaces o una ruta ambigua.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != expected["size"]
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("El archivo local no cumple su tipo o tamaño.")
        sha, remaining = hashlib.sha256(), expected["size"]
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise ValueError("El archivo local se truncó durante la verificación.")
            remaining -= len(chunk)
            sha.update(chunk)
        if stream.read(1) or sha.hexdigest() != expected["sha256"]:
            raise ValueError("Un archivo local fue alterado antes de autenticar.")


def read_values(path=PROJECT / ".env"):
    helper = load("pg_sdk_env", PROJECT / "scripts/ci/check_env.py")
    values, errors = helper.parse_env(read_source(path, 2 * 1024**2).decode("utf-8-sig"))
    if errors:
        raise ValueError("La bóveda no cumple el parser canónico.")
    return values


def bind_plan(plan, values):
    remote = contract()
    expected = {
        "MEDIA_VM_IP": "192.168.0.85",
        "MEDIA_VM_SSH_USER": "jandrade",
        "MEDIA_PLATFORM_INSTALL_ROOT": "/opt/media-platform",
        "MEDIA_PLATFORM_DATA_ROOT": "/var/lib/media-platform",
        "MEDIA_PLATFORM_OWNER_MARKER": "agencia-digital-marketing-ai:media-platform:v4.1",
        PREFIX + "REMOTE_STAGING_PATH": remote.STAGING,
        PREFIX + "INPUTS_PATH": remote.INPUTS,
        PREFIX + "OUTPUTS_PATH": remote.OUTPUTS,
        PREFIX + "CONTROL_PATH": remote.CONTROL,
        PREFIX + "OWNER": remote.OWNER,
    }
    if any(values.get(k) != v for k, v in expected.items()):
        raise ValueError("La bóveda no fija host, raíces y propietario autorizados.")
    result = {
        **plan,
        "layout": decode(values["MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64"]),
        "host_fingerprint": values["MEDIA_VM_SSH_HOST_FINGERPRINT"],
        "bound_to_vault": True,
    }
    result["plan_sha256"] = remote.digest(remote.canonical(result))
    return result


def connect(values):
    if importlib.metadata.version("paramiko") != "5.1.0":
        raise ValueError("Se requiere Paramiko 5.1.0 aislado.")
    import paramiko

    if values.get("MEDIA_VM_IP") != "192.168.0.85" or values.get("MEDIA_VM_SSH_USER") != "jandrade":
        raise ValueError("Destino SSH no autorizado.")
    public = values["MEDIA_VM_SSH_PUBLIC_KEY"].split()
    host = values["MEDIA_VM_SSH_HOST_PUBLIC_KEY"].split()
    if len(public) < 2 or len(host) < 2 or public[0] != "ssh-ed25519" or host[0] != "ssh-ed25519":
        raise ValueError("Se requieren las identidades Ed25519 respaldadas, sin fallback.")
    host_bytes = base64.b64decode(host[1], validate=True)
    pin = "SHA256:" + base64.b64encode(hashlib.sha256(host_bytes).digest()).decode().rstrip("=")
    if pin != values["MEDIA_VM_SSH_HOST_FINGERPRINT"]:
        raise ValueError("El pin del host difiere de la pública respaldada.")
    private = base64.b64decode(values["MEDIA_VM_SSH_PRIVATE_KEY_B64"], validate=True)
    if len(private) > 16384:
        raise ValueError("Identidad SSH excesiva.")
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(private.decode()))
    if key.asbytes() != base64.b64decode(public[1], validate=True):
        raise ValueError("La identidad cliente no coincide con su pública.")
    client = paramiko.SSHClient()
    client.get_host_keys().add(
        "192.168.0.85", "ssh-ed25519", paramiko.PKey.from_type_string("ssh-ed25519", host_bytes)
    )
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            "192.168.0.85",
            port=22,
            username="jandrade",
            pkey=key,
            allow_agent=False,
            look_for_keys=False,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
        )
        return client
    except BaseException:
        client.close()
        raise


def remote_command(source, mode):
    if mode not in {"receive", "prepare", "create", "start", "sdk", "graph", "stop"}:
        raise ValueError("Modo remoto no autorizado.")
    encoded = base64.b64encode(source).decode()
    code = (
        "import base64,hashlib;_source=base64.b64decode("
        + repr(encoded)
        + ");_SOURCE_SHA256=hashlib.sha256(_source).hexdigest();exec(compile(_source,'<pg-sdk-propio>','exec'))"
    )
    if len(code.encode()) > 96 * 1024:
        raise ValueError("El helper supera el límite seguro de argv Linux.")
    prefix = "" if mode == "receive" else "sudo -n "
    return prefix + "/usr/bin/python3 -c " + shlex.quote(code) + " --mode " + mode


def collect(channel, deadline, maximum, *, heartbeat=False):
    stdout, stderr = bytearray(), bytearray()
    next_pulse = time.monotonic()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("El controlador agotó el plazo remoto.")
        if heartbeat and time.monotonic() >= next_pulse:
            channel.sendall(b".")
            next_pulse = time.monotonic() + 1
        for ready, read, buffer in (
            (channel.recv_ready, channel.recv, stdout),
            (channel.recv_stderr_ready, channel.recv_stderr, stderr),
        ):
            while ready():
                if time.monotonic() >= deadline:
                    raise TimeoutError("La salida continua agotó el plazo.")
                buffer.extend(read(65536))
                if len(stdout) + len(stderr) > maximum:
                    raise ValueError("La captura excede 4 MiB; se rechaza truncarla.")
        if (
            channel.exit_status_ready()
            and not channel.recv_ready()
            and not channel.recv_stderr_ready()
        ):
            break
        time.sleep(0.01)
    status = channel.recv_exit_status()
    if stderr:
        raise ValueError("El ejecutor produjo diagnóstico fuera del informe JSON.")
    result = contract().json_object(stdout)
    if type(result.get("success")) is not bool or status not in (0, 1):
        raise ValueError("Respuesta remota no verificable.")
    return result


class Controller:
    def __init__(self, values, plan, *, client=None, writer=None):
        self.values, self.plan, self.client, self.writer = values, plan, client, writer
        self.remote = contract()
        self.sources_verified = False

    def saved(self):
        result = {}
        for key in ("prepared", "container", "stages"):
            value = decode(self.values.get(KEYS[key]), optional=True)
            if set(result) & set(value):
                raise ValueError("Recibos duplicados entre campos de bóveda.")
            result.update(value)
        return result

    def persist(self, group, stage, value):
        expected_group = {
            "transfer": "prepared",
            "prepare": "prepared",
            "create": "container",
            "start": "container",
            "stop": "container",
            "create_unverified": "container",
            "sdk": "stages",
            "graph": "stages",
        }
        if expected_group.get(stage) != group:
            raise ValueError("Transición y campo de bóveda no corresponden.")
        if stage == "transfer":
            if (
                value.get("success") is not True
                or value.get("stage") != "transfer"
                or value.get("owner") != self.remote.OWNER
                or value.get("staging") != self.remote.STAGING
                or value.get("plan_sha256") != self.plan["plan_sha256"]
                or value.get("source_sha256") != self.plan["remote_sha256"]
                or value.get("files") != 78
                or not self.remote.SHA.fullmatch(str(value.get("marker_sha256", "")))
                or any(
                    type(value.get("directory", {}).get(k)) is not int or value["directory"][k] <= 0
                    for k in ("device", "inode")
                )
            ):
                raise ValueError("El recibo de transferencia no acredita su plan y directorio.")
        elif stage == "create_unverified":
            if value.get("verified") is not False or not self.remote.SHA.fullmatch(
                str(value.get("cid", ""))
            ):
                raise ValueError("El CID no verificado carece de formato válido.")
        else:
            receipt = value.get("receipt", {})
            if (
                set(value) != {"receipt", "receipt_sha256"}
                or receipt.get("stage") != stage
                or receipt.get("plan_sha256") != self.plan["plan_sha256"]
                or receipt.get("source_sha256") != self.plan["remote_sha256"]
                or type(receipt.get("success")) is not bool
                or value["receipt_sha256"] != self.remote.digest(self.remote.canonical(receipt))
            ):
                raise ValueError("El recibo remoto no coincide con su etapa, fuente, plan y hash.")
        existing = decode(self.values.get(KEYS[group]), optional=True)
        if stage in existing and existing[stage] != value:
            raise ValueError("No se sobrescribe una transición ya respaldada.")
        existing[stage] = value
        encoded = base64.b64encode(self.remote.canonical(existing)).decode()
        self.writer({KEYS[group]: encoded})
        self.values[KEYS[group]] = encoded

    def request(self, mode, *, transfer=False):
        if mode != "stop" and not self.sources_verified:
            data = read_source(
                PROJECT / ".tools/evidence/phase4-postgres-immich-offline-inputs.json", 256 * 1024
            )
            if self.remote.digest(data) != INVENTORY_SHA:
                raise ValueError("El mapa de fuentes fue alterado antes de autenticar.")
            inventory = self.remote.json_object(data)
            for name, expected in self.plan["files"].items():
                verify_local_input(Path(inventory["files"][name]["source"]), expected)
            self.sources_verified = True
        if self.client is None:
            self.client = connect(self.values)
        source = read_source(Path(__file__).with_name("offline_sdk_remote.py"))
        if self.remote.digest(source) != self.plan["remote_sha256"]:
            raise ValueError("El ejecutor cambió antes de enviarlo.")
        channel = self.client.get_transport().open_session(timeout=10)
        budget = {"sdk": 600, "graph": 180}.get(mode, 600 if mode in {"receive", "prepare"} else 60)
        deadline = time.monotonic() + budget
        try:
            channel.settimeout(2)
            channel.exec_command(remote_command(source, mode))
            layout = read_source(PROJECT / "docker/host_layout.py")
            if self.remote.digest(layout) != self.plan["layout_helper_sha256"]:
                raise ValueError("El helper de layout cambió.")
            request = {
                "plan": self.plan,
                "backed": self.saved(),
                "layout_source_b64": base64.b64encode(layout).decode(),
            }
            if mode in {"sdk", "graph"}:
                request["budget"] = budget - 5
            header = self.remote.canonical(request)
            if len(header) > 256 * 1024:
                raise ValueError("Petición remota excesiva.")
            channel.sendall(struct.pack(">I", len(header)) + header)
            if transfer:
                self.send_files(channel, deadline)
            heartbeat = mode in {"sdk", "graph"}
            if not heartbeat:
                channel.shutdown_write()
            return collect(channel, deadline, self.remote.MAX_OUTPUT, heartbeat=heartbeat)
        finally:
            channel.close()

    def send_files(self, channel, deadline):
        data = read_source(
            PROJECT / ".tools/evidence/phase4-postgres-immich-offline-inputs.json", 256 * 1024
        )
        if self.remote.digest(data) != INVENTORY_SHA:
            raise ValueError("El mapa de archivos locales cambió.")
        inventory = self.remote.json_object(data)
        for name, expected in sorted(self.plan["files"].items()):
            source = Path(inventory["files"][name]["source"])
            if source.resolve(strict=True) != source.absolute():
                raise ValueError("Ruta local enlazada antes de transferir.")
            descriptor = os.open(
                source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            )
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size != expected["size"]
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    raise ValueError("Fuente no ordinaria o tamaño alterado.")
                sha, count = hashlib.sha256(), 0
                while chunk := stream.read(65536):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Transferencia fuera del presupuesto.")
                    count += len(chunk)
                    if count > expected["size"]:
                        raise ValueError("La fuente creció durante la transferencia.")
                    sha.update(chunk)
                    channel.sendall(chunk)
                if count != expected["size"] or sha.hexdigest() != expected["sha256"]:
                    raise ValueError("La fuente transferida no cumple el hash fijado.")

    def perform(self, mode):
        saved = self.saved()
        self.remote.require_backup(mode, saved, saved)
        try:
            if mode == "prepare":
                uploaded = self.request("receive", transfer=True)
                if uploaded.get("success") is not True:
                    raise ValueError("La transferencia fue rechazada.")
                self.persist("prepared", "transfer", uploaded["result"])
            result = self.request(mode)
            if "result" in result:
                group = (
                    "prepared"
                    if mode == "prepare"
                    else "container"
                    if mode in {"create", "start", "stop"}
                    else "stages"
                )
                self.persist(group, mode, result["result"])
            elif result.get("created_cid"):
                self.persist(
                    "container",
                    "create_unverified",
                    {"cid": result["created_cid"], "verified": False},
                )
            if mode in {"sdk", "graph"} and result.get("success") is not True:
                stopped = self.request("stop")
                result["cleanup"] = stopped
                if "result" in stopped:
                    self.persist("container", "stop", stopped["result"])
            return result
        except BaseException as error:
            if mode in {"sdk", "graph"}:
                previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    stopped = self.request("stop")
                    error.cleanup = stopped
                    if "result" in stopped:
                        self.persist("container", "stop", stopped["result"])
                except BaseException as failure:
                    error.cleanup = {"success": False, "error_type": type(failure).__name__}
                finally:
                    signal.signal(signal.SIGINT, previous)
            raise

    def close(self):
        if self.client is not None:
            self.client.close()


def root_writer(plan):
    helper = load("pg_sdk_vault", PROJECT / ".tools/vault_updates.py", plan["vault_helper_sha256"])

    def write(updates):
        comments = {
            key: (
                "# Para qué: Respaldar el contrato y las transiciones del constructor SDK PostgreSQL Immich.",
                "# Dónde: Controlador local y namespace propio de vm-media-01, sin secretos del SDK.",
                "# Riesgo al rotar: Detener la operación y revisar recibos e identidades antes de continuar.",
            )
            for key in updates
        }
        return helper.update(updates, comments=comments)

    return write


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("plan", "prepare", "create", "start", "sdk", "graph", "stop"),
        nargs="?",
        default="plan",
    )
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    controller = None
    try:
        candidate = proposal()
        if args.mode == "plan" and not args.root_update_vault:
            print(json.dumps(candidate, sort_keys=True))
            return 0
        if not args.root_update_vault:
            raise ValueError("La transición requiere el CLI de raíz con respaldo de bóveda.")
        values = read_values()
        bound = bind_plan(candidate, values)
        if args.mode == "plan":
            existing = decode(values.get(KEYS["plan"]), optional=True)
            if existing and existing != bound:
                raise ValueError("Existe otro plan respaldado; requiere revisión de raíz.")
            writer = root_writer(bound)
            writer({KEYS["plan"]: base64.b64encode(contract().canonical(bound)).decode()})
            result = {
                "success": True,
                "mode": "plan",
                "plan_sha256": bound["plan_sha256"],
                "sdk_executed": False,
            }
        else:
            saved = decode(values[KEYS["plan"]])
            if saved != bound:
                raise ValueError("Fuentes, plan o identidades no coinciden con la bóveda.")
            writer = root_writer(bound)
            controller = Controller(values, saved, writer=writer)
            result = controller.perform(args.mode)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["success"] else 1
    except BaseException as error:
        result = {
            "success": False,
            "mode": args.mode,
            "error_type": type(error).__name__,
            "values_hidden": True,
        }
        if hasattr(error, "cleanup"):
            result["cleanup"] = error.cleanup
        print(json.dumps(result))
        return 1
    finally:
        if controller is not None:
            controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
