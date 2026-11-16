"""Plan local por defecto; acciones SDK sólo mediante la CLI explícita de raíz."""

import argparse
import base64
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import shlex
import stat
import struct
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
PREFIX = "MEDIA_ZOU_OPENCV_SDK_"
LOCAL_OWNER = "agencia-digital-marketing-ai:opencv-zou-minimal-sdk:v1"


def read_source(path, maximum=256 * 1024):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("La ruta contiene enlaces o reparse points.")
    expected = (info.st_dev, info.st_ino, info.st_size)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size) != expected:
            raise ValueError("Archivo cambiado antes de abrirlo.")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("Archivo no ordinario o excesivo.")
        raw = stream.read(maximum + 1)
        if len(raw) != info.st_size:
            raise ValueError("El archivo cambió durante la lectura.")
    current = path.lstat()
    if (current.st_dev, current.st_ino, current.st_size) != expected:
        raise ValueError("El nombre cambió durante la lectura.")
    return raw


def read_vault_bytes(path):
    return read_source(path, 8 * 1024**2)


def write_new(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def load(name, path, expected=None):
    raw = read_source(path)
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Helper diferente del pin respaldado.")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def contract():
    return load("opencv_zou_contract", Path(__file__).with_name("opencv_minimal_sdk_remote.py"))


KEYS = {
    "plan": PREFIX + "CONTROLLER_B64",
    "prepared": PREFIX + "PREPARED_B64",
    "container": PREFIX + "CONTAINER_B64",
    "stages": PREFIX + "STAGE_RECEIPTS_B64",
}


def decode(raw, *, optional=False):
    if optional and (not raw or raw in {"CHANGE_ME", "COMPLETAR_EN_FASE_4"}):
        return {}
    value = base64.b64decode(raw, validate=True)
    if len(value) > 2 * 1024**2:
        raise ValueError("Documento de respaldo excesivo.")
    result = contract().json_object(value)
    if type(result) is not dict:
        raise ValueError("El respaldo requiere un objeto.")
    return result


def proposal(workspace=PROJECT):
    remote = contract()
    path = Path(__file__).with_name("opencv-minimal-sdk-profile.json")
    raw = read_source(path, 2 * 1024**2)
    profile = remote.json_object(raw)
    remote.validate_profile(profile)
    remote.validate_inventory(profile["files"], profile["kit_sha256"])
    result = {
        **profile,
        "profile_sha256": remote.digest(raw),
        "controller_sha256": remote.digest(read_source(Path(__file__), 256 * 1024)),
        "remote_sha256": remote.digest(
            read_source(Path(__file__).with_name("opencv_minimal_sdk_remote.py"), 128 * 1024)
        ),
        "layout_helper_sha256": remote.digest(read_source(workspace / "docker/host_layout.py")),
        "env_helper_sha256": remote.digest(read_source(workspace / "scripts/ci/check_env.py")),
        "access_helper_sha256": remote.digest(
            read_source(workspace / "sre/backup_tools_access.py")
        ),
        "vault_helper_sha256": remote.digest(read_source(workspace / ".tools/vault_updates.py")),
        "bound_to_vault": False,
        "sdk_executed": False,
        "docker_executed": False,
    }
    if (
        result["remote_sha256"]
        != profile["files"]["code/docker/kitsu-zou/opencv_minimal_sdk_remote.py"]["sha256"]
    ):
        raise ValueError("El helper difiere del kit propuesto.")
    return result


def verify_local(plan):
    remote = contract()
    root = Path(plan["local_root"])
    marker = read_source(root / ".agency-opencv-sdk-owner.json", 8192)
    current = root.stat()
    expected = plan["local_root_identity"]
    if (
        root != PROJECT / ".tools/kitsu-zou-opencv-sdk-v1"
        or remote.json_object(marker) != expected
        or current.st_dev != expected["device"]
        or current.st_ino != expected["inode"]
        or remote.digest(marker) != plan["local_marker_sha256"]
    ):
        raise ValueError("La raíz local no conserva la identidad respaldada.")
    access = load(
        "opencv_local_access", PROJECT / "sre/backup_tools_access.py", plan["access_helper_sha256"]
    )
    access.check_access(root, private=True)
    files_root = root / ("kit-" + plan["kit_sha256"][:16])
    for name, row in plan["files"].items():
        path = files_root.joinpath(*name.split("/"))
        for parent in (*reversed(path.absolute().parents), path.absolute()):
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Input enlazado.")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != row["size"]:
                raise ValueError("Tipo/tamaño de input diferente.")
            sha = hashlib.sha256()
            remaining = row["size"]
            while remaining:
                block = stream.read(min(65536, remaining))
                if not block:
                    raise ValueError("Input truncado.")
                remaining -= len(block)
                sha.update(block)
            if stream.read(1) or sha.hexdigest() != row["sha256"]:
                raise ValueError("Input alterado.")
    return files_root


def read_values(plan, path=PROJECT / ".env"):
    access = load(
        "opencv_vault_access", PROJECT / "sre/backup_tools_access.py", plan["access_helper_sha256"]
    )
    access.check_access(path, private=True)
    parser = load(
        "opencv_env_parser", PROJECT / "scripts/ci/check_env.py", plan["env_helper_sha256"]
    )
    values, errors = parser.parse_env(read_vault_bytes(path).decode("utf-8-sig"))
    if errors:
        raise ValueError("La bóveda no cumple el parser canónico.")
    return values


def bind_plan(plan, values):
    remote = contract()
    paths = plan["paths"]
    expected = {
        "MEDIA_VM_IP": "192.168.0.85",
        "MEDIA_VM_SSH_USER": "jandrade",
        PREFIX + "OWNER": LOCAL_OWNER,
        PREFIX + "ROOT": plan["local_root"],
        PREFIX + "REMOTE_STAGING_PATH": paths["staging"],
        PREFIX + "INPUTS_PATH": paths["inputs"],
        PREFIX + "OUTPUTS_PATH": paths["outputs"],
        PREFIX + "CONTROL_PATH": paths["control"],
        PREFIX + "SOURCE_PATH": paths["source"],
        PREFIX + "OVERLAY_PATH": paths["overlay"],
        PREFIX + "NUMPY_HEADERS_PATH": paths["numpy_headers"],
        PREFIX + "PROFILE_SHA256": plan["profile_sha256"],
        PREFIX + "CONTROLLER_SHA256": plan["controller_sha256"],
        PREFIX + "REMOTE_SHA256": plan["remote_sha256"],
        PREFIX + "KIT_SHA256": plan["kit_sha256"],
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise ValueError("La bóveda no respalda rutas/identidades y fuentes actuales.")
    identity = decode(values[PREFIX + "IDENTITY_B64"])
    if any(identity.get(key) != value for key, value in plan["local_root_identity"].items()):
        raise ValueError("La raíz local no está respaldada.")
    result = {
        **plan,
        "bound_to_vault": True,
        "layout": decode(values["MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64"]),
        "host_fingerprint": values["MEDIA_VM_SSH_HOST_FINGERPRINT"],
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
        raise ValueError("Identidades SSH no admitidas.")
    host_bytes = base64.b64decode(host[1], validate=True)
    pin = "SHA256:" + base64.b64encode(hashlib.sha256(host_bytes).digest()).decode().rstrip("=")
    if pin != values["MEDIA_VM_SSH_HOST_FINGERPRINT"]:
        raise ValueError("Pin de servidor diferente.")
    private = base64.b64decode(values["MEDIA_VM_SSH_PRIVATE_KEY_B64"], validate=True)
    if len(private) > 16384:
        raise ValueError("Identidad privada excesiva.")
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(private.decode("utf-8")))
    if key.asbytes() != base64.b64decode(public[1], validate=True):
        raise ValueError("La pública cliente no coincide.")
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


def remote_command(source_sha, mode):
    allowed = {"receive", "prepare", "create", "start", "sdk", "export", "approve", "build", "stop"}
    if mode not in allowed or not contract().SHA.fullmatch(source_sha):
        raise ValueError("Etapa remota no admitida.")
    # La fuente viaja con framing binario: no supera el límite de argv Linux.
    bootstrap = (
        "import sys,struct,hashlib\n_s=sys.stdin.buffer.raw\n"
        "def _read(n):\n r=bytearray()\n while len(r)<n:\n  b=_s.read(n-len(r))\n  if not b:raise ValueError('SOURCE_TRUNCATED')\n  r.extend(b)\n return bytes(r)\n"
        "_n=struct.unpack('>I',_read(4))[0]\n"
        "if not 0<_n<=131072:raise ValueError('SOURCE_LIMIT')\n"
        "_src=_read(_n)\n_SOURCE_SHA256=hashlib.sha256(_src).hexdigest()\n"
        "if _SOURCE_SHA256!=" + repr(source_sha) + ":raise ValueError('SOURCE_PIN')\n"
        "exec(compile(_src,'<opencv-sdk-propio>','exec'))\n"
    )
    prefix = "" if mode == "receive" else "sudo -n "
    return (
        prefix
        + "/usr/bin/env -i PATH=/usr/bin:/bin HOME=/root /usr/bin/python3 -I -B -c "
        + shlex.quote(bootstrap)
        + " --mode "
        + mode
    )


def collect(channel, deadline, maximum, *, heartbeat=False):
    stdout, stderr = bytearray(), bytearray()
    next_pulse = time.monotonic()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("La sesión agotó el plazo.")
        if heartbeat and time.monotonic() >= next_pulse:
            channel.sendall(b".")
            next_pulse = time.monotonic() + 1
        for ready, receive, buffer in [
            (channel.recv_ready, channel.recv, stdout),
            (channel.recv_stderr_ready, channel.recv_stderr, stderr),
        ]:
            while ready():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Salida continua fuera de plazo.")
                buffer.extend(receive(65536))
                if len(stdout) + len(stderr) > maximum:
                    raise ValueError("Captura excesiva, no truncada.")
        if (
            channel.exit_status_ready()
            and not channel.recv_ready()
            and not channel.recv_stderr_ready()
        ):
            break
        time.sleep(0.01)
    status = channel.recv_exit_status()
    if stderr:
        raise ValueError("Diagnóstico fuera del JSON acotado.")
    result = contract().json_object(stdout)
    if type(result.get("success")) is not bool or status not in (0, 1):
        raise ValueError("Respuesta remota inválida.")
    return result


class Controller:
    def __init__(self, values, plan, *, writer, client=None):
        self.values, self.plan, self.writer, self.client = values, plan, writer, client
        self.remote = contract()

    def saved(self):
        result = {}
        for group in ("prepared", "container", "stages"):
            value = decode(self.values.get(KEYS[group]), optional=True)
            if set(result) & set(value):
                raise ValueError("Recibo duplicado entre respaldos.")
            result.update(value)
        return result

    def persist(self, stage, value):
        group = {
            "transfer": "prepared",
            "prepare": "prepared",
            "create": "container",
            "start": "container",
            "stop": "container",
            "create_unverified": "container",
            "sdk": "stages",
            "export": "stages",
            "approve": "stages",
            "build": "stages",
        }[stage]
        if stage == "transfer":
            if (
                value.get("success") is not True
                or value.get("owner") != LOCAL_OWNER
                or value.get("plan_sha256") != self.plan["plan_sha256"]
                or value.get("source_sha256") != self.plan["remote_sha256"]
                or value.get("files") != len(self.plan["files"])
                or value.get("staging") != self.plan["paths"]["staging"]
            ):
                raise ValueError("Staging no acredita plan/fuentes.")
        elif stage == "create_unverified":
            if value.get("verified") is not False or not self.remote.SHA.fullmatch(
                str(value.get("cid", ""))
            ):
                raise ValueError("CID fallido no acreditado.")
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
                raise ValueError("Recibo remoto no coincide con etapa/hash/fuente.")
        current = decode(self.values.get(KEYS[group]), optional=True)
        if stage in current and current[stage] != value:
            raise ValueError("No se sobrescribe un recibo ya respaldado.")
        current[stage] = value
        encoded = base64.b64encode(self.remote.canonical(current)).decode()
        self.writer({KEYS[group]: encoded})
        self.values[KEYS[group]] = encoded

    def request(self, mode):
        result = {
            "plan": self.plan,
            "backed": self.saved(),
            "layout_source_b64": base64.b64encode(
                read_source(PROJECT / "docker/host_layout.py")
            ).decode(),
        }
        if mode == "approve":
            proof = decode(self.values.get(PREFIX + "SDK_SCA_B64"))
            data_root = (
                Path(self.plan["local_root"]) / ("kit-" + self.plan["kit_sha256"][:16]) / "data"
            )
            original = self.remote.json_object(
                read_source(data_root / "manifest-sdk.json", 2 * 1024**2)
            )
            original["build_enabled"] = True
            original["acceptance"] = {
                **original["acceptance"],
                "native_sdk_sca_verified": True,
                "root_pins_backed_for_new_manifest": True,
            }
            raw = json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
            sha = self.remote.digest(raw)
            encoded = base64.b64encode(raw).decode()
            # Respaldo explícito previo a publicar el derivado habilitado en el host.
            self.writer(
                {
                    PREFIX + "APPROVED_MANIFEST_SHA256": sha,
                    PREFIX + "APPROVED_MANIFEST_B64": encoded,
                }
            )
            result.update(
                sdk_sca=proof, approved_manifest_sha256=sha, approved_manifest_b64=encoded
            )
        return result

    def perform(self, mode):
        root = verify_local(self.plan) if mode != "stop" else None
        request = self.request(mode)
        source = read_source(Path(__file__).with_name("opencv_minimal_sdk_remote.py"), 128 * 1024)
        if self.remote.digest(source) != self.plan["remote_sha256"]:
            raise ValueError("Fuente remota cambió desde el respaldo.")
        channel = self.client.get_transport().open_session(timeout=10)
        timeout = {"receive": 600, "prepare": 600, "sdk": 720, "build": 1920, "export": 240}.get(
            mode, 120
        )
        deadline = time.monotonic() + timeout
        try:
            channel.settimeout(10)
            channel.exec_command(remote_command(self.plan["remote_sha256"], mode))
            channel.sendall(struct.pack(">I", len(source)) + source)
            raw = self.remote.canonical(request)
            if len(raw) > 2 * 1024**2:
                raise ValueError("Petición remota excesiva.")
            channel.sendall(struct.pack(">I", len(raw)) + raw)
            if mode == "receive":
                for name, row in sorted(self.plan["files"].items()):
                    with (root / name).open("rb") as stream:
                        sha = hashlib.sha256()
                        remaining = row["size"]
                        while remaining:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Transferencia agotada.")
                            block = stream.read(min(65536, remaining))
                            if not block:
                                raise ValueError("Input truncado al transmitir.")
                            sha.update(block)
                            remaining -= len(block)
                            channel.sendall(block)
                        if stream.read(1) or sha.hexdigest() != row["sha256"]:
                            raise ValueError("Input cambió durante la transferencia.")
            if mode not in {"sdk", "build"}:
                channel.shutdown_write()
            result = collect(channel, deadline, 4 * 1024**2, heartbeat=mode in {"sdk", "build"})
        finally:
            channel.close()
        if not result["success"]:
            if self.remote.SHA.fullmatch(str(result.get("created_cid", ""))):
                self.persist(
                    "create_unverified",
                    {
                        "cid": result["created_cid"],
                        "verified": False,
                        "plan_sha256": self.plan["plan_sha256"],
                    },
                )
            if result.get("result") and mode in {"sdk", "build"}:
                self.persist(mode, result["result"])
            raise ValueError("Etapa rechazada; CID/logs/recibos propios conservados.")
        self.persist("transfer" if mode == "receive" else mode, result["result"])
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["plan", "prepare", "create", "start", "sdk", "export", "approve", "build", "stop"],
        default="plan",
    )
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        plan = proposal()
        if not args.root_update_vault:
            if args.mode != "plan":
                raise ValueError("La transición exige CLI explícita de raíz.")
            print(json.dumps(plan, sort_keys=True))
            return 0
        if args.mode != "stop":
            verify_local(plan)
        values = read_values(plan)
        plan = bind_plan(plan, values)
        updater = load(
            "opencv_root_vault_writer",
            PROJECT / ".tools/vault_updates.py",
            plan["vault_helper_sha256"],
        )
        writer = updater.update
        if args.mode == "plan":
            writer({KEYS["plan"]: base64.b64encode(contract().canonical(plan)).decode()})
            print(
                json.dumps(
                    {"success": True, "plan_sha256": plan["plan_sha256"], "bound_to_vault": True}
                )
            )
            return 0
        if decode(values[KEYS["plan"]]) != plan:
            raise ValueError("El plan ligado no es idéntico al respaldo.")
        client = connect(values)
        controller = Controller(values, plan, writer=writer, client=client)
        if args.mode == "prepare":
            saved = controller.saved()
            if "transfer" not in saved:
                controller.perform("receive")
        result = controller.perform(args.mode)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, TimeoutError):
        print(json.dumps({"success": False, "error": "OPENCV_SDK_REJECTED"}))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
