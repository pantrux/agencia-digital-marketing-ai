"""Controlador local R2: plan por defecto; cada acción requiere hash respaldado."""

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
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


C = load("r2_service_contract", Path(__file__).with_name("r2_service_contract.py"))
ENV = load("r2_service_environment", PROJECT / "scripts/ci/check_env.py")


def read_source(path, maximum=256 * 1024):
    path = Path(path)
    C.require(path.is_absolute() and path.resolve(strict=True) == path.absolute())
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        C.require(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= maximum
        )
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        C.require(
            len(raw) == before.st_size
            and (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
        )
        return raw


def unique(pairs):
    result = {}
    for key, value in pairs:
        C.require(key not in result)
        result[key] = value
    return result


def parse_json(raw):
    def invalid(_value):
        raise ValueError("R2_SERVICE_JSON_REJECTED")

    return json.loads(
        raw.decode("utf-8", errors="strict"), object_pairs_hook=unique, parse_constant=invalid
    )


def decode(value):
    raw = base64.b64decode(value, validate=True)
    C.require(len(raw) <= 65536)
    return parse_json(raw)


def read_values(path):
    values, errors = ENV.parse_env(read_source(path, 2 * 1024**2).decode("utf-8-sig"))
    C.require(not errors)
    return values


def sources():
    return {
        key: read_source(PROJECT / "docker" / filename)
        for key, filename in (
            ("controller", "r2_service.py"),
            ("remote", "r2_service_remote.py"),
            ("contract", "r2_service_contract.py"),
            ("layout", "host_layout.py"),
        )
    }


def prepare_plan(values, action):
    source = sources()
    for role in ("controller", "remote", "contract"):
        C.require(
            hashlib.sha256(source[role]).hexdigest()
            == values.get(
                "MEDIA_R2_SERVICE_"
                + ("SOURCE" if role == "controller" else role.upper())
                + "_SHA256"
            )
        )
    C.require(
        values.get("MEDIA_VM_IP") == "192.168.0.85"
        and values.get("MEDIA_VM_SSH_USER") == "jandrade"
        and values.get("MEDIA_R2_SERVICE_ROOT") == C.ROOT
        and values.get("MEDIA_R2_SERVICE_MEDIA_ROOT") == C.MEDIA
        and values.get("MEDIA_R2_SERVICE_PORT") == "3010"
    )
    spec = decode(values["MEDIA_R2_SERVICE_SPEC_B64"])
    C.validate_action(spec, action)
    C.require(
        spec["account_id"] == values.get("CF_ACCOUNT_ID")
        and str(spec["max_object_bytes"]) == values.get("MEDIA_R2_SYNC_MAX_OBJECT_BYTES")
    )
    observation = {
        **{role + "_sha256": hashlib.sha256(raw).hexdigest() for role, raw in source.items()},
        "layout": decode(values["MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64"]),
    }
    return C.make_plan(spec, action, observation)


def remote_command(source):
    # Sólo código público fijado en la orden; credenciales exclusivamente en stdin.
    code = (
        'import base64,sys,types;m=types.ModuleType("r2_service_contract");sys.modules[m.__name__]=m;exec(compile(base64.b64decode('
        + repr(base64.b64encode(source["contract"]).decode())
        + '),"<r2-contrato>","exec"),m.__dict__);exec(compile(base64.b64decode('
        + repr(base64.b64encode(source["remote"]).decode())
        + '),"<r2-remoto>","exec"))'
    )
    return (
        "sudo -n env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 python3 -I -B -S -c "
        + shlex.quote(code)
    )


def connect_ssh(values):
    C.require(importlib.metadata.version("paramiko") == "5.1.0")
    import paramiko

    C.require(
        values.get("MEDIA_VM_IP") == "192.168.0.85"
        and values.get("MEDIA_VM_SSH_USER") == "jandrade"
    )
    public = values["MEDIA_VM_SSH_PUBLIC_KEY"].split()
    host = values["MEDIA_VM_SSH_HOST_PUBLIC_KEY"].split()
    C.require(len(public) >= 2 and len(host) >= 2 and public[0] == host[0] == "ssh-ed25519")
    host_raw = base64.b64decode(host[1], validate=True)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(host_raw).digest()).decode().rstrip(
        "="
    )
    C.require(fingerprint == values["MEDIA_VM_SSH_HOST_FINGERPRINT"])
    raw = base64.b64decode(values["MEDIA_VM_SSH_PRIVATE_KEY_B64"], validate=True)
    C.require(len(raw) <= 16384)
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(raw.decode("utf-8")))
    C.require(key.asbytes() == base64.b64decode(public[1], validate=True))
    client = paramiko.SSHClient()
    client.get_host_keys().add(
        "192.168.0.85", "ssh-ed25519", paramiko.PKey.from_type_string("ssh-ed25519", host_raw)
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


def secret_payload(values):
    raw = {
        name: values[key].encode("ascii", errors="strict")
        for name, key in zip(
            C.SECRET_NAMES,
            (
                "STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET",
                "R2_PLATFORM_ACCESS_KEY_ID",
                "R2_PLATFORM_SECRET_ACCESS_KEY",
            ),
            strict=True,
        )
    }
    C.require(len(raw["hmac.key"]) == 64 and all(33 <= byte <= 126 for byte in raw["hmac.key"]))
    C.require(
        0 < len(raw["r2-access.key"]) <= 128
        and raw["r2-access.key"].isalnum()
        and 0 < len(raw["r2-secret.key"]) <= 256
        and all(33 <= byte <= 126 for byte in raw["r2-secret.key"])
    )
    return {name: base64.b64encode(data).decode("ascii") for name, data in raw.items()}


def apply_remote(plan, values, approved):
    C.verify_plan(plan, approved)
    C.require(
        approved == values.get("MEDIA_R2_SERVICE_APPROVED_PLAN_SHA256")
        and plan == prepare_plan(values, plan["action"])
    )
    source = sources()
    payload = {
        "plan": plan,
        "approved": approved,
        "layout_source_b64": base64.b64encode(source["layout"]).decode(),
        "secrets": secret_payload(values)
        if plan["action"] == "prepare" and plan["spec"]["prepared"] is None
        else None,
    }
    raw = C.canonical(payload)
    C.require(len(raw) <= 256 * 1024)
    client = connect_ssh(values)
    channel = None
    try:
        channel = client.get_transport().open_session(timeout=10)
        channel.settimeout(10)
        channel.exec_command(remote_command(source))
        channel.sendall(raw)
        channel.shutdown_write()
        stdout, stderr = bytearray(), bytearray()
        deadline = time.monotonic() + 120
        while True:
            C.require(time.monotonic() < deadline)
            while channel.recv_ready():
                stdout.extend(channel.recv(8192))
                C.require(len(stdout) <= 65536)
            while channel.recv_stderr_ready():
                stderr.extend(channel.recv_stderr(8192))
                C.require(len(stderr) <= 8192)
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            time.sleep(0.02)
        exit_code = channel.recv_exit_status()
        C.require(not stderr)
        result = parse_json(bytes(stdout))
        if result.get("success") is False and result.get("created_cid") is not None:
            C.require(
                set(result)
                == {
                    "success",
                    "action",
                    "created_cid",
                    "verified",
                    "role",
                    "core_sha256",
                    "intent_saved",
                    "requires_cid_backup_before_any_action",
                    "error",
                }
                and plan["action"].startswith("create-")
                and result["action"] == plan["action"]
                and result["role"] == plan["action"].split("-")[1]
                and C.SHA.fullmatch(result["created_cid"]) is not None
                and result["verified"] is False
                and result["core_sha256"] == C.core_hash(plan["spec"])
                and result["intent_saved"] is True
                and result["requires_cid_backup_before_any_action"] is True
                and result["error"] == "R2_CREATED_CID_REQUIRES_REVIEW"
            )
            return result
        C.require(exit_code == 0)
        C.require(result.get("success") is True and result.get("action") == plan["action"])
        if plan["action"] == "prepare":
            C.validate_prepared(result["prepared"], plan["spec"])
            C.require(result["containers_started"] is False)
        elif plan["action"].startswith("create-"):
            C.require(
                C.SHA.fullmatch(result["container_id"]) is not None
                and result["core_sha256"] == C.core_hash(plan["spec"])
                and result["requires_cid_backup_before_start"] is True
            )
        elif plan["action"] == "start-init":
            C.validate_spec({**plan["spec"], "init_result": result["init_result"]})
        else:
            C.require(result["container_id"] == plan["spec"]["service_container_id"])
        return result
    finally:
        if channel is not None:
            channel.close()
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=C.ACTIONS)
    parser.add_argument("--vault", type=Path, default=PROJECT / ".env")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    if args.apply and (args.plan is None or args.expected_plan_sha256 is None):
        parser.error("apply requiere plan privado y hash respaldado")
    if not args.apply and (args.plan is not None or args.expected_plan_sha256 is not None):
        parser.error("el modo plan no admite argumentos apply")
    try:
        values = read_values(args.vault)
        if args.apply:
            plan = parse_json(read_source(args.plan, 65536))
            C.require(plan["action"] == args.action)
            result = apply_remote(plan, values, args.expected_plan_sha256)
        else:
            result = prepare_plan(values, args.action)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("success", True) else 2
    except Exception:
        print('{"success":false,"error":"R2_SERVICE_PLAN_OR_ACTION_REJECTED"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
