"""Controlador local por plan respaldado; SSH sólo en apply explícito."""

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
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REMOTE = load_module(
    "component_build_contract", Path(__file__).with_name("component_build_remote.py")
)
ENV = load_module("component_build_environment", PROJECT / "scripts/ci/check_env.py")


def read_source(path, maximum=REMOTE.MAX_CONTEXT):
    if not path.is_absolute() or path.resolve(strict=True) != path.absolute():
        raise ValueError("La fuente no puede ser un enlace ni una ruta relativa.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("Se requiere fuente regular, única y acotada.")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("La fuente excede su límite.")
    return data


def decode_b64_json(value):
    data = base64.b64decode(value, validate=True)
    if len(data) > 65536:
        raise ValueError("La configuración supera su límite.")
    return json.loads(data)


def prepare_plan(values, component, workspace=PROJECT):
    if component not in REMOTE.FILES:
        raise ValueError("Componente no autorizado.")
    if any(
        values.get(key) != value
        for key, value in {
            "MEDIA_VM_IP": "192.168.0.85",
            "MEDIA_VM_SSH_USER": "jandrade",
            "MEDIA_PLATFORM_INSTALL_ROOT": "/opt/media-platform",
            "MEDIA_PLATFORM_DATA_ROOT": "/var/lib/media-platform",
            "MEDIA_PLATFORM_OWNER_MARKER": REMOTE.LAYOUT_OWNER,
        }.items()
    ):
        raise ValueError("La bóveda no fija el host y raíces autorizados.")
    specification = decode_b64_json(values["MEDIA_PLATFORM_COMPONENT_BUILDS_B64"])
    if (
        set(specification)
        != {"schema_version", "helper_sha256", "layout_helper_sha256", "components"}
        or specification["schema_version"] != 1
    ):
        raise ValueError("El esquema de builds no está implementado.")
    if not set(specification["components"]) <= set(REMOTE.FILES):
        raise ValueError("El manifiesto contiene componentes ajenos.")
    entry = specification["components"][component]
    REMOTE.validate_entry(component, entry)
    helper = read_source(PROJECT / "docker/component_build_remote.py")
    layout_source = read_source(PROJECT / "docker/host_layout.py")
    if (
        REMOTE.digest(helper) != specification["helper_sha256"]
        or REMOTE.digest(layout_source) != specification["layout_helper_sha256"]
    ):
        raise ValueError("Los helpers difieren de los hashes respaldados.")
    files = {}
    for name in REMOTE.FILES[component]:
        data = read_source(workspace / "docker" / component / name)
        if REMOTE.digest(data) != entry["files"][name]:
            raise ValueError("Una fuente no coincide con el hash respaldado.")
        files[name] = {"sha256": REMOTE.digest(data), "size": len(data)}
    plan = {
        "schema_version": 1,
        "owner": REMOTE.OWNER,
        "component": component,
        **{key: entry[key] for key in ("build_id", "tag", "base", "context_root", "logs_root")},
        **REMOTE.paths(component, entry),
        "files": files,
        "helper_sha256": specification["helper_sha256"],
        "layout_helper_sha256": specification["layout_helper_sha256"],
        "layout": decode_b64_json(values["MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64"]),
        "target": {
            "hostname": "vm-media-01",
            "ip": "192.168.0.85",
            "port": 22,
            "user": "jandrade",
            "host_fingerprint": values.get("MEDIA_VM_SSH_HOST_FINGERPRINT", ""),
        },
    }
    plan["plan_sha256"] = REMOTE.plan_hash(plan)
    REMOTE.validate_plan(plan)
    return plan


def make_payload(plan, values, workspace=PROJECT):
    current = prepare_plan(values, plan["component"], workspace)
    if current != plan:
        raise ValueError("El plan quedó obsoleto.")
    payload = {
        "plan": plan,
        "approved_plan_sha256": values.get("MEDIA_PLATFORM_BUILD_APPROVED_PLAN_SHA256"),
        "files": {
            name: base64.b64encode(
                read_source(workspace / "docker" / plan["component"] / name)
            ).decode()
            for name in REMOTE.FILES[plan["component"]]
        },
        "layout_source_b64": base64.b64encode(
            read_source(PROJECT / "docker/host_layout.py")
        ).decode(),
    }
    REMOTE.validate_payload(payload)
    return payload


def remote_command(source):
    code = (
        "import base64;exec(compile(base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + "),'<component-build>','exec'))"
    )
    return (
        "sudo -n env "
        + " ".join(key + "=" + value for key, value in REMOTE.NO_GIT.items())
        + " python3 -c "
        + shlex.quote(code)
        + " --apply"
    )


def connect_ssh(values):
    if importlib.metadata.version("paramiko") != "5.1.0":
        raise ValueError("Se requiere el entorno aislado con Paramiko 5.1.0 fijado.")
    import paramiko

    if values.get("MEDIA_VM_IP") != "192.168.0.85" or values.get("MEDIA_VM_SSH_USER") != "jandrade":
        raise ValueError("Identidad SSH fuera del destino autorizado.")
    public = values["MEDIA_VM_SSH_PUBLIC_KEY"].split()
    host_public = values["MEDIA_VM_SSH_HOST_PUBLIC_KEY"].split()
    if (
        len(public) < 2
        or len(host_public) < 2
        or public[0] != "ssh-ed25519"
        or host_public[0] != "ssh-ed25519"
    ):
        raise ValueError("Se requieren las identidades Ed25519 respaldadas, sin alternativas.")
    host_bytes = base64.b64decode(host_public[1], validate=True)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(host_bytes).digest()).decode().rstrip(
        "="
    )
    if fingerprint != values["MEDIA_VM_SSH_HOST_FINGERPRINT"]:
        raise ValueError("La pública del host no coincide con su pin respaldado.")
    private = base64.b64decode(values["MEDIA_VM_SSH_PRIVATE_KEY_B64"], validate=True)
    if len(private) > 16384:
        raise ValueError("La identidad privada supera su límite.")
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(private.decode("utf-8")))
    if key.asbytes() != base64.b64decode(public[1], validate=True):
        raise ValueError("La clave privada no corresponde a la pública respaldada.")
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


def apply_remote(plan, values, approved_hash, workspace=PROJECT):
    if (
        approved_hash != plan.get("plan_sha256")
        or values.get("MEDIA_PLATFORM_BUILD_APPROVED_PLAN_SHA256") != approved_hash
    ):
        raise ValueError("El hash aprobado debe coincidir con el plan y la bóveda.")
    payload = make_payload(plan, values, workspace)
    helper = read_source(PROJECT / "docker/component_build_remote.py")
    if REMOTE.digest(helper) != plan["helper_sha256"]:
        raise ValueError("El helper cambió antes de transferirlo.")
    client = connect_ssh(values)
    channel = None
    try:
        channel = client.get_transport().open_session(timeout=10)
        channel.settimeout(10)
        channel.exec_command(remote_command(helper))
        data = json.dumps(payload).encode()
        if len(data) > 1024 * 1024:
            raise ValueError("El payload supera el límite de transferencia.")
        channel.sendall(data)
        channel.shutdown_write()
        stdout, stderr = bytearray(), bytearray()
        deadline = time.monotonic() + 750
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("El controlador agotó su plazo.")
            while channel.recv_ready():
                stdout.extend(channel.recv(8192))
                if len(stdout) > 65536:
                    raise ValueError("La respuesta remota supera su límite.")
            while channel.recv_stderr_ready():
                stderr.extend(channel.recv_stderr(8192))
                if len(stderr) > 65536:
                    raise ValueError("El diagnóstico remoto supera su límite.")
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            time.sleep(0.025)
        if channel.recv_exit_status() != 0 or stderr:
            raise ValueError("El helper remoto rechazó el build; revisar su evidencia privada.")
        result = json.loads(stdout)
        if (
            result.get("success") is not True
            or result.get("plan_sha256") != approved_hash
            or not REMOTE.IMAGE_ID.fullmatch(result.get("image_id", ""))
            or result.get("containers_created") is not False
            or result.get("final_sca_verified") is not False
            or any(result.get(key) != value for key, value in REMOTE.result_fields(plan).items())
            or not REMOTE.SHA.fullmatch(result.get("log_sha256", ""))
            or type(result.get("changed")) is not bool
        ):
            raise ValueError("El helper no devolvió un recibo verificable.")
        return result
    finally:
        if channel is not None:
            channel.close()
        client.close()


def read_values(path):
    values, errors = ENV.parse_env(read_source(path, 2 * 1024 * 1024).decode("utf-8-sig"))
    if errors:
        raise ValueError("La bóveda no cumple el parser canónico.")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=tuple(REMOTE.FILES))
    parser.add_argument("--vault", type=Path, default=PROJECT / ".env")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args()
    if args.apply and (args.plan is None or args.expected_plan_sha256 is None):
        parser.error("apply requiere --plan y --expected-plan-sha256")
    if not args.apply and (args.plan is not None or args.expected_plan_sha256 is not None):
        parser.error("El modo plan no admite argumentos de aplicación.")
    try:
        values = read_values(args.vault)
        if args.apply:
            plan = json.loads(read_source(args.plan, 65536))
            if plan.get("component") != args.component:
                raise ValueError("El plan pertenece a otro componente.")
            result = apply_remote(plan, values, args.expected_plan_sha256)
        else:
            result = prepare_plan(values, args.component)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        print(json.dumps({"success": False, "error": "BUILD_PLAN_O_APPLY_RECHAZADO"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
