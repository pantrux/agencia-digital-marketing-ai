"""Plan sin bóveda/red por defecto; sólo raíz promoverá la imagen corregida."""

import argparse
import base64
import hashlib
import json
import os
import shlex
import stat
import struct
import sys
import time
import types
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LOCAL_ROOT = ROOT / ".tools/postgres-immich-security-inputs-v1"
PROFILE_PATH = LOCAL_ROOT / "security-runtime-v2.profile.json"
PREFIX = "MEDIA_POSTGRES_IMMICH_SECURITY_RUNTIME_V2_"
V7_PREFIX = "MEDIA_POSTGRES_IMMICH_SECURITY_REBUILD_V7_"
REFERENCES = {
    "connector": (
        "docker/postgres-immich/run_offline_sdk_v4.py",
        "685bb8a54fc7d65fd25710b5ea317f8eda239b57aa2978a0ff5e33b8aabba608",
    ),
    "parser": (
        "scripts/ci/check_env.py",
        "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda",
    ),
    "access": (
        "sre/backup_tools_access.py",
        "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b",
    ),
    "updater": (
        ".tools/vault_updates.py",
        "0fc75c6d082e9e25fa6a5b5146560d7373b3dfab2d701e351749dafb2183b46c",
    ),
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_source(path, maximum=256 * 1024):
    path = Path(path).absolute()
    for parent in [*reversed(path.parents), path]:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("SOURCE_LINK")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise ValueError("SOURCE_FILE")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        before = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        after, named = os.fstat(fd), path.lstat()

        def keys(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_nlink,
                info.st_mode,
            )

        if not (
            keys(info) == keys(before) == keys(after) == keys(named)
            and info.st_ctime_ns == named.st_ctime_ns
            and before.st_ctime_ns == after.st_ctime_ns
            and len(raw) == info.st_size <= maximum
        ):
            raise ValueError("SOURCE_CHANGED")
        return raw
    finally:
        os.close(fd)


def load_bytes(name, path, expected=None):
    raw = read_source(path)
    if expected is not None and digest(raw) != expected:
        raise ValueError("LOCAL_MODULE_PIN")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def load_reference(name):
    path, pin = REFERENCES[name]
    return load_bytes("security_runtime_" + name, ROOT / path, pin)


def contract(expected=None):
    return load_bytes("security_runtime_contract", HERE / "security_runtime_remote_v2.py", expected)


def sources():
    return {
        name: digest(read_source(path))
        for name, path in {
            "local": Path(__file__),
            "remote": HERE / "security_runtime_remote_v2.py",
            "dockerfile": HERE / "Dockerfile.security-runtime-v2",
        }.items()
    }


def read_values():
    load_reference("access").check_access(ROOT / ".env", private=True)
    raw = read_source(ROOT / ".env", 8 * 1024**2)
    values, errors = load_reference("parser").parse_env(raw.decode("utf-8-sig"))
    if errors:
        raise ValueError("VAULT_PARSE")
    return values


def file_references(values, remote):
    result = {}
    for name, (path, pin) in remote.FROZEN_REFS.items():
        raw = read_source(ROOT / path, 1024**2)
        if digest(raw) != pin:
            raise ValueError("HISTORY_FILE_PIN")
        result[name] = {"path": path, "sha256": pin, "size": len(raw)}
    for name in remote.DYNAMIC_REFS:
        mode = name.removeprefix("v7_").upper()
        prefix = V7_PREFIX + ("PROFILE_" if mode == "PROFILE" else mode + "_RECEIPT_")
        path, pin = values.get(prefix + "PATH"), values.get(prefix + "SHA256")
        if not path or not pin:
            raise ValueError("CATALOG_STOP_OR_EPOCH_PENDING")
        path = Path(path)
        if path.parent != LOCAL_ROOT or not path.name.startswith("security-rebuild-v7."):
            if name != "v7_profile" or path != LOCAL_ROOT / "security-rebuild-v7.profile.json":
                raise ValueError("V7_REFERENCE_NAMESPACE")
        raw = read_source(path, 1024**2)
        if digest(raw) != pin:
            raise ValueError("V7_REFERENCE_PIN")
        result[name] = {"path": str(path), "sha256": pin, "size": len(raw)}
    return result


def reference_bytes(profile):
    remote = contract(profile["sources"]["remote"])
    result = {}
    for name, row in profile["references"].items():
        path = ROOT / row["path"] if name in remote.FROZEN_REFS else Path(row["path"])
        raw = read_source(path, 1024**2)
        if digest(raw) != row["sha256"] or len(raw) != row["size"]:
            raise ValueError("REFERENCE_CHANGED")
        result[name] = raw
    return result


def build_profile(values):
    remote = contract()
    profile = {
        **remote.proposal(),
        "ready": True,
        "pending": [],
        "references": file_references(values, remote),
        "sources": sources(),
    }
    remote.validate_profile(profile)
    data = {name: remote.json_object(raw) for name, raw in reference_bytes(profile).items()}
    pure = remote.load_pure(read_source(HERE / "final_image_remote.py"))
    remote.validate_gates(data, pure)
    return profile


def bound_fields(profile):
    remote = contract(profile["sources"]["remote"])
    return {
        PREFIX + "OWNER": remote.OWNER,
        PREFIX + "CONTEXT_ROOT": remote.CONTEXT,
        PREFIX + "RECORD_ROOT": remote.RECORD,
        PREFIX + "BASE_OCI_ROOT": remote.RECORD + "/base-oci",
        PREFIX + "PROFILE_PATH": str(PROFILE_PATH),
        PREFIX + "PROFILE_SHA256": digest(remote.canonical(profile)),
        **{PREFIX + name.upper() + "_SHA256": pin for name, pin in profile["sources"].items()},
    }


def request(client, mode, profile, prepared):
    remote = contract(profile["sources"]["remote"])
    source = read_source(HERE / "security_runtime_remote_v2.py")
    payload = {
        "mode": mode,
        "profile": profile,
        "profile_sha256": digest(remote.canonical(profile)),
        "prepared": prepared,
        "references_b64": {
            name: base64.b64encode(raw).decode() for name, raw in reference_bytes(profile).items()
        },
    }
    for field, path, expected in [
        ("sdk_source_b64", HERE / "offline_sdk_remote_v4.py", remote.SDK_SHA),
        ("security_source_b64", HERE / "security_rebuild_remote_v7.py", remote.SECURITY_SHA),
        ("pure_source_b64", HERE / "final_image_remote.py", remote.PURE_SHA),
        ("layout_source_b64", ROOT / "docker/host_layout.py", remote.LAYOUT_SHA),
        (
            "dockerfile_b64",
            HERE / "Dockerfile.security-runtime-v2",
            profile["sources"]["dockerfile"],
        ),
    ]:
        raw = read_source(path)
        if digest(raw) != expected:
            raise ValueError("TRANSPORT_SOURCE_PIN")
        payload[field] = base64.b64encode(raw).decode()
    raw = remote.canonical(payload)
    if len(raw) > remote.MAX_REQUEST:
        raise ValueError("REQUEST_LIMIT")
    code = (
        "import base64,hashlib;d=base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + ");g={'__name__':'__main__','_SECURITY_RUNTIME_SOURCE_SHA256':hashlib.sha256(d).hexdigest()};exec(compile(d,'<security-runtime-fijado>','exec'),g)"
    )
    if len(code.encode()) > 96 * 1024:
        raise ValueError("BOOTSTRAP_LIMIT")
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(3)
        channel.exec_command("sudo -n /usr/bin/python3 -B -c " + shlex.quote(code))
        channel.sendall(struct.pack(">I", len(raw)) + raw)
        channel.shutdown_write()
        return load_reference("connector").collect(
            channel, time.monotonic() + 610, remote.MAX_CAPTURE
        )
    finally:
        channel.close()


def save_response(mode, response, profile, writer):
    remote = contract(profile["sources"]["remote"])
    value = response.get("result", {})
    row = value.get("receipt", {})
    if (
        value != remote.envelope(row)
        or type(row.get("success")) is not bool
        or response.get("success") is not row["success"]
        or row.get("stage") != mode
        or row.get("owner") != remote.OWNER
        or row.get("profile_sha256") != digest(remote.canonical(profile))
        or row.get("source_sha256") != profile["sources"]["remote"]
    ):
        raise ValueError("RUNTIME_RESPONSE_ENVELOPE")
    access = load_reference("access")
    access.check_access(LOCAL_ROOT, private=True)
    path = LOCAL_ROOT / ("security-runtime-v2." + mode + "." + uuid.uuid4().hex + ".json")
    raw = remote.canonical(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        access.check_access(path, private=True)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    updates = {
        PREFIX + mode.upper() + "_RECEIPT_PATH": str(path),
        PREFIX + mode.upper() + "_RECEIPT_SHA256": digest(raw),
    }
    ids = row if row["success"] else row.get("observed", {})
    for field, key in [
        ("image_id", "OBSERVED_IMAGE_ID"),
        ("config_digest", "OBSERVED_CONFIG_DIGEST"),
    ]:
        value = ids.get(field)
        if isinstance(value, str) and remote.re.fullmatch(r"sha256:[a-f0-9]{64}", value):
            updates[PREFIX + key] = value
    comments = {
        key: (
            "# Para qué: Respaldar evidencia propia de la imagen PostgreSQL corregida.",
            "# Dónde: Perfil y namespace de seguridad propios, sin secretos.",
            "# Riesgo al rotar: Conservar fallos e IDs observados; no implican aprobación SQL/SCA.",
        )
        for key in updates
    }
    writer(updates, comments=comments)
    return row["success"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "prepare", "build"), default="plan", nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        remote = contract()
        if args.mode == "plan" and args.output is None and not args.root_update_vault:
            print(
                json.dumps(
                    {
                        "success": True,
                        "proposal": remote.proposal(),
                        "sources": sources(),
                        "prefix": PREFIX,
                        "profile_path_proposed": str(PROFILE_PATH),
                        "infrastructure_executed": False,
                    }
                )
            )
            return 0
        if args.mode != "plan" and (
            not args.root_update_vault
            or args.output is not None
            or not args.expected_profile_sha256
        ):
            raise ValueError("ROOT_OPERATION_FLAGS")
        if args.mode == "plan" and (args.output is None or args.output.absolute() != PROFILE_PATH):
            raise ValueError("PROFILE_DESTINATION")
        values = read_values()
        if args.mode == "plan":
            if args.output is None or args.output.absolute() != PROFILE_PATH:
                raise ValueError("PROFILE_DESTINATION")
            profile = build_profile(values)
            raw = remote.canonical(profile)
            access = load_reference("access")
            access.check_access(LOCAL_ROOT, private=True)
            fd = os.open(
                PROFILE_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                access.check_access(PROFILE_PATH, private=True)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if args.root_update_vault:
                updates = bound_fields(profile)
                comments = {
                    key: (
                        "# Para qué: Fijar el controlador de imagen PostgreSQL corregida.",
                        "# Dónde: Perfil pequeño por referencias; context y record privados.",
                        "# Riesgo al rotar: Revisar catálogo y STOP positivos antes de prepare/build.",
                    )
                    for key in updates
                }
                load_reference("updater").update(updates, comments=comments)
            print(
                json.dumps(
                    {
                        "success": True,
                        "profile": profile,
                        "profile_sha256": digest(raw),
                        "infrastructure_executed": False,
                    }
                )
            )
            return 0
        if (
            not args.root_update_vault
            or args.output is not None
            or not args.expected_profile_sha256
        ):
            raise ValueError("ROOT_OPERATION_FLAGS")
        access = load_reference("access")
        access.check_access(LOCAL_ROOT, private=True)
        access.check_access(PROFILE_PATH, private=True)
        raw = read_source(PROFILE_PATH, 32768)
        if digest(raw) != args.expected_profile_sha256:
            raise ValueError("EXTERNAL_PROFILE_PIN")
        profile = remote.json_object(raw)
        remote = contract(profile["sources"]["remote"])
        remote.validate_profile(profile)
        if (
            profile["sources"] != sources()
            or any(values.get(key) != value for key, value in bound_fields(profile).items())
            or profile != build_profile(values)
        ):
            raise ValueError("VAULT_PROFILE_SOURCE_DRIFT")
        prepared = None
        if args.mode == "build":
            path = Path(values[PREFIX + "PREPARE_RECEIPT_PATH"])
            if path.parent != LOCAL_ROOT or not path.name.startswith(
                "security-runtime-v2.prepare."
            ):
                raise ValueError("PREPARE_RECEIPT_NAMESPACE")
            before = read_source(path, 1024**2)
            if digest(before) != values[PREFIX + "PREPARE_RECEIPT_SHA256"]:
                raise ValueError("PREPARE_RECEIPT_PIN")
            prepared = remote.json_object(before)
            remote.checked_envelope(prepared, "prepare", profile_sha=digest(raw))
        client = load_reference("connector").connect(values)
        response = request(client, args.mode, profile, prepared)
        success = save_response(args.mode, response, profile, load_reference("updater").update)
        print(json.dumps(response))
        return 0 if success else 1
    except (ValueError, OSError, KeyError, TypeError, TimeoutError):
        print(
            json.dumps(
                {"success": False, "error": "SECURITY_RUNTIME_REJECTED", "evidence_preserved": True}
            )
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
