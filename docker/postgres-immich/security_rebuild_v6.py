"""Plan pequeño por referencias y CLI operacional explícita de Root."""

import argparse
import base64
import hashlib
import json
import os
import shlex
import stat
import struct
import time
import types
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
LOCAL_ROOT = BASE / ".tools/postgres-immich-security-inputs-v1"
PROFILE_PATH = LOCAL_ROOT / "security-rebuild-v6.profile.json"
PREFIX = "MEDIA_POSTGRES_IMMICH_SECURITY_REBUILD_V6_"
OPERATION_MODES = (
    "receive",
    "prepare",
    "restart",
    "materialize",
    "graph",
    "regression",
    "build",
    "catalog",
    "stop",
)
REFS = {
    "normal": (
        "docker/postgres-immich/offline_sdk_remote_v4.py",
        "a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed",
    ),
    "compiler": (
        "docker/postgres-immich/compile_remote_v3.py",
        "e0e2bc46a65563e122a1469ac9ed5040836697748846e0408938e208e7049a3d",
    ),
    "augment": (
        "docker/postgres-immich/augment_sdk_remote.py",
        "997877a7ae20478b7c6cb6afd3f7f2432a325c7aa319d2004508fffdbf3724dc",
    ),
    "old_profile": (
        ".tools/postgres-immich-build/compile-v3-profile.json",
        "bffefb81179fd767d640faf106965ba7d465e90c3a789b4e7800b988a0152153",
    ),
    "recover": (
        ".tools/evidence/phase4-postgres-immich-postverify-v3-recover-real-result.json",
        "df3913b08f92f41c85243f5621db65b12562a31160da1562f0cdd48add794108",
    ),
    "layout": (
        "docker/host_layout.py",
        "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9",
    ),
    "connector": (
        "docker/postgres-immich/run_offline_sdk_v4.py",
        "685bb8a54fc7d65fd25710b5ea317f8eda239b57aa2978a0ff5e33b8aabba608",
    ),
    "data_preparer": (
        "docker/postgres-immich/prepare_security_vendor.py",
        "490a7173c7abdf1156badd287846606cccd3a826f26522a34b8185ad214566a2",
    ),
    "env_parser": (
        "scripts/ci/check_env.py",
        "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda",
    ),
    "access": (
        ".tools/backup-source-build/vault-controller-v1/vault-r2-state.py",
        "aa8fc9a38f1707f9a27289b4fe1efb6b6643eddd0ed672deb6b4502b13215df7",
    ),
    "access_core": (
        ".tools/backup-source-build/vault-controller-v1/verify-restic-auth-v2-r2.py",
        "d8fa9ed6e2f8ad7f44227ee1d4bd3890d9dbf813d5fd39db49c9c3ed61cb5c7c",
    ),
    "updater": (
        ".tools/vault_updates.py",
        "0fc75c6d082e9e25fa6a5b5146560d7373b3dfab2d701e351749dafb2183b46c",
    ),
    "derived": (
        ".tools/postgres-immich-security-inputs-v1/derived-v1/receipt.json",
        "a47255902ebf0c0d92dfb730a1fea864dda12a257cbe58c698d0d9bb346e7c43",
    ),
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def regular(path, maximum=128 * 1024):
    path = Path(path).absolute()
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Ancestro no canónico.")
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & 0x400
        or before.st_size > maximum
    ):
        raise ValueError("Archivo no ordinario o excesivo.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = path.lstat()
        if (
            any(
                (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                for row in (info, after)
            )
            or len(raw) != before.st_size
        ):
            raise ValueError("Archivo cambiado durante lectura.")
    return raw


def module_raw(name, raw, filename):
    module = types.ModuleType(name)
    module.__dict__["__file__"] = str(filename)
    exec(compile(raw, str(filename), "exec"), module.__dict__)
    return module


def reference_bytes(name):
    relative, pin = REFS[name]
    raw = regular(BASE / relative, 128 * 1024)
    if digest(raw) != pin:
        raise ValueError("Referencia congelada cambió: " + name)
    return raw


def load_reference(name):
    return module_raw("rebuild_fixed_" + name, reference_bytes(name), BASE / REFS[name][0])


def remote_contract(expected=None):
    path = HERE / "security_rebuild_remote_v6.py"
    raw = regular(path)
    if expected is not None and digest(raw) != expected:
        raise ValueError("El ejecutor remoto cambió antes de cargarlo.")
    return module_raw("rebuild_contract", raw, path)


def file_paths():
    derived = LOCAL_ROOT / "derived-v1"
    return {
        **{
            name: derived / name
            for name in (
                "Cargo.lock",
                "vendor.tar.gz",
                "vendor-lock.json",
                "build-script-audit.json",
            )
        },
        **{
            filename: BASE / REFS[key][0]
            for key, filename in [
                ("normal", "normal.py"),
                ("compiler", "compiler.py"),
                ("augment", "augment.py"),
                ("old_profile", "old-profile.json"),
                ("recover", "recover.json"),
            ]
        },
        "recipe.sh": HERE / "security-rebuild-recipe-v6.sh",
        "verifier.py": HERE / "security_rebuild_remote_v6.py",
    }


def proposal():
    remote = remote_contract()
    references = {}
    for name, (relative, pin) in REFS.items():
        raw = reference_bytes(name)
        references[name] = {"path": relative, "sha256": pin, "size": len(raw)}
    derived = json.loads(reference_bytes("derived"))
    if (
        derived.get("success") is not True
        or derived.get("owner")
        != "agencia-digital-marketing-ai:postgres-immich-security-derived:v1"
    ):
        raise ValueError("El derivado no acredita su recepción real.")
    info = (LOCAL_ROOT / "derived-v1").lstat()
    if (
        derived.get("device") != info.st_dev
        or derived.get("inode") != info.st_ino
        or not stat.S_ISDIR(info.st_mode)
    ):
        raise ValueError("El derivado cambió de identidad.")
    inventory = {}
    for name, path in file_paths().items():
        raw = regular(path, 48 * 1024**2)
        if name in derived["outputs"] and digest(raw) != derived["outputs"][name]:
            raise ValueError("Output derivado distinto del respaldo.")
        inventory[name] = {"sha256": digest(raw), "size": len(raw)}
    old = json.loads(reference_bytes("old_profile"))
    birth = old["backed"]["create"]["receipt"]["container"]
    profile = {
        "schema_version": 1,
        "owner": remote.OWNER,
        "cid": remote.CID,
        "image": remote.IMAGE,
        "original_container": {k: birth[k] for k in ("cid", "created", "image_id")},
        "paths": {
            "staging": remote.STAGING,
            "control": remote.CONTROL,
            "work": remote.WORK,
            "output": remote.OUTPUT,
        },
        "sources": {
            "local": digest(regular(Path(__file__))),
            "remote": inventory["verifier.py"]["sha256"],
            "recipe": inventory["recipe.sh"]["sha256"],
        },
        "references": references,
        "files": inventory,
        "budgets": remote.BUDGETS,
        "environment_sha256": remote.digest(remote.canonical(remote.build_environment())),
        "regression_inputs_sha256": remote.digest(
            remote.canonical(
                {k: remote.digest(v) for k, v in remote.regression_inputs()[0].items()}
            )
        ),
        "local_root": str(LOCAL_ROOT),
        "sdk_security_policy": remote.SDK_SECURITY_POLICY,
    }
    remote.validate_plan(profile)
    return profile


def access():
    helper = load_reference("data_preparer")
    config = {
        "helpers": [
            {"path": REFS[name][0], "sha256": REFS[name][1]} for name in ("access", "access_core")
        ]
    }
    return helper.native_access(config)


def read_values(plan, path=None, *, acl=None, parser=None):
    path = BASE / ".env" if path is None else Path(path)
    acl = access() if acl is None else acl
    acl.check(path)
    if plan["references"]["env_parser"]["sha256"] != REFS["env_parser"][1]:
        raise ValueError("Parser de bóveda ajeno.")
    parser = load_reference("env_parser") if parser is None else parser
    values, errors = parser.parse_env(regular(path, 8 * 1024**2).decode("utf-8-sig"))
    if errors:
        raise ValueError("La bóveda no cumple el parser canónico.")
    return values


def bound_fields(plan):
    remote = remote_contract(plan["sources"]["remote"])
    return {
        PREFIX + "OWNER": remote.OWNER,
        PREFIX + "CID": remote.CID,
        PREFIX + "PROFILE_PATH": str(PROFILE_PATH),
        PREFIX + "PROFILE_SHA256": digest(remote.canonical(plan)),
        **{PREFIX + key.upper() + "_SOURCE_SHA256": pin for key, pin in plan["sources"].items()},
        **{PREFIX + key.upper() + "_PATH": path for key, path in plan["paths"].items()},
    }


def saved(values, mode, plan):
    remote = remote_contract(plan["sources"]["remote"])
    names = {"prepare"} if mode == "stop" else remote.MODES - {"stop"}
    result = {}
    for name in names:
        prefix = PREFIX + name.upper() + "_RECEIPT_"
        path, pin = values.get(prefix + "PATH"), values.get(prefix + "SHA256")
        if not path and not pin:
            continue
        path = Path(path)
        if path.parent != LOCAL_ROOT or not path.name.startswith(
            "security-rebuild-v6." + name + "."
        ):
            raise ValueError("Puntero de recibo fuera del namespace propio.")
        raw = regular(path, 1024 * 1024)
        if digest(raw) != pin:
            raise ValueError("Recibo local distinto de su SHA respaldado.")
        value = json.loads(raw)
        remote.verified_saved(
            {"backed": {name: value}, "profile_sha256": digest(remote.canonical(plan))}
        )
        result[name] = value
    return result


def collect(channel, deadline, maximum, *, heartbeat):
    output, diagnostic = bytearray(), bytearray()
    pulse = time.monotonic()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Se agotó el presupuesto remoto.")
        if heartbeat and time.monotonic() >= pulse:
            channel.sendall(b".")
            pulse = time.monotonic() + 1
        for ready, read, buffer in [
            (channel.recv_ready, channel.recv, output),
            (channel.recv_stderr_ready, channel.recv_stderr, diagnostic),
        ]:
            while ready():
                if time.monotonic() >= deadline:
                    raise TimeoutError("La captura continua agotó el plazo.")
                buffer.extend(read(65536))
                if len(output) + len(diagnostic) > maximum:
                    raise ValueError("Salida excesiva; no se trunca.")
        if (
            channel.exit_status_ready()
            and not channel.recv_ready()
            and not channel.recv_stderr_ready()
        ):
            break
        time.sleep(0.01)
    status = channel.recv_exit_status()
    if diagnostic or status not in (0, 1):
        raise ValueError("Respuesta fuera del protocolo JSON.")
    value = json.loads(output)
    if type(value.get("success")) is not bool or value["success"] != (status == 0):
        raise ValueError("Exit code y resultado remoto no coinciden.")
    return value


def sdk_approval(values, plan, mode):
    remote = remote_contract(plan["sources"]["remote"])
    if mode not in remote.SDK_GATED_MODES:
        return None
    if values.get(PREFIX + "SDK_SCA_APPROVED") != "true":
        raise ValueError("Root no ha aprobado SCA vigente del SDK reutilizado.")
    path = Path(values.get(PREFIX + "SDK_SCA_RECEIPT_PATH", ""))
    if not path.is_absolute() or not path.is_relative_to(BASE / ".tools") or path.suffix != ".json":
        raise ValueError("El recibo SCA debe ser JSON propio bajo .tools.")
    raw = regular(path, 65536)
    pin = values.get(PREFIX + "SDK_SCA_RECEIPT_SHA256", "")
    approval = {"raw_b64": base64.b64encode(raw).decode(), "sha256": pin}
    remote.validate_sdk_security(approval, plan)
    return approval


def request(client, plan, mode, backed, reviewed, approval=None):
    remote = remote_contract(plan["sources"]["remote"])
    if mode not in remote.MODES:
        raise ValueError("Modo no permitido.")
    deadline = time.monotonic() + remote.BUDGETS[mode] + 10
    source = regular(HERE / "security_rebuild_remote_v6.py")
    if digest(source) != plan["sources"]["remote"]:
        raise ValueError("Ejecutor remoto cambió.")
    support = {"normal": base64.b64encode(reference_bytes("normal")).decode()}
    if mode != "stop":
        support.update(
            {
                name: base64.b64encode(reference_bytes(name)).decode()
                for name in ("compiler", "augment", "old_profile", "recover", "layout")
            }
        )
    payload = {
        "action": mode,
        "profile": plan,
        "profile_sha256": digest(remote.canonical(plan)),
        "backed": backed,
        "support": support,
        "reviewed": reviewed,
        "sdk_approval": approval,
    }
    encoded = remote.canonical(payload)
    if len(encoded) > 1024 * 1024:
        raise ValueError("Frame excesivo.")
    code = (
        "import base64,hashlib;d=base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + ");g={'__name__':'__main__','_REBUILD_SOURCE_SHA256':hashlib.sha256(d).hexdigest()};exec(compile(d,'<rebuild-fijado>','exec'),g)"
    )
    if len(code.encode()) > 96 * 1024:
        raise ValueError("El helper excede argv seguro.")
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(5)
        channel.exec_command(
            ("" if mode == "receive" else "sudo -n ")
            + "/usr/bin/python3 -B -c "
            + shlex.quote(code)
        )
        channel.sendall(struct.pack(">I", len(encoded)) + encoded)
        if mode == "receive":
            for name, path in sorted(file_paths().items()):
                raw = regular(path, 48 * 1024**2)
                if (
                    len(raw) != plan["files"][name]["size"]
                    or digest(raw) != plan["files"][name]["sha256"]
                ):
                    raise ValueError("Una entrada cambió antes de transmitir.")
                for offset in range(0, len(raw), 65536):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("La transferencia agotó el plazo global.")
                    channel.settimeout(min(5, max(0.1, deadline - time.monotonic())))
                    channel.sendall(raw[offset : offset + 65536])
            channel.shutdown_write()
        return collect(channel, deadline, 1024 * 1024, heartbeat=mode != "receive")
    finally:
        channel.close()


def persist(mode, response, plan, writer):
    remote = remote_contract(plan["sources"]["remote"])
    if response.get("success") is not True:
        raise ValueError("Etapa remota rechazada; evidencias propias preservadas.")
    value = response["result"]
    remote.verified_saved(
        {"backed": {mode: value}, "profile_sha256": digest(remote.canonical(plan))}
    )
    filename = "security-rebuild-v6." + mode + "." + uuid.uuid4().hex + ".json"
    path = LOCAL_ROOT / filename
    access().check(LOCAL_ROOT)
    raw = remote.canonical(value)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    access().check(path)
    comments = {
        PREFIX + mode.upper() + "_RECEIPT_" + key: (
            "# Para qué: Respaldar recibo e identidad de la reconstrucción de seguridad de VectorChord.",
            "# Dónde: CLI de Root y control SSD propio; sin credenciales en el recibo.",
            "# Riesgo al rotar: Verificar SHA, perfil y CID antes de una siguiente operación.",
        )
        for key in ("PATH", "SHA256")
    }
    writer(
        {
            PREFIX + mode.upper() + "_RECEIPT_PATH": str(path),
            PREFIX + mode.upper() + "_RECEIPT_SHA256": digest(raw),
        },
        comments=comments,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["plan", *OPERATION_MODES], nargs="?", default="plan")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        if args.mode == "plan":
            remote = remote_contract()
            profile = proposal()
            raw = remote.canonical(profile)
            if args.output:
                if args.output.absolute() != PROFILE_PATH:
                    raise ValueError("El perfil sólo se publica en el destino privado fijo.")
                access().check(LOCAL_ROOT)
                with PROFILE_PATH.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            if args.root_update_vault:
                if regular(PROFILE_PATH, 32768) != raw:
                    raise ValueError(
                        "El perfil físico debe existir y coincidir antes de respaldarlo."
                    )
                read_values(profile)
                updates = bound_fields(profile)
                comments = {
                    key: (
                        "# Para qué: Fijar la reconstrucción aislada de VectorChord.",
                        "# Dónde: Perfil pequeño por referencias, sin duplicar historiales.",
                        "# Riesgo al rotar: Revisar identidad y fuentes antes de ejecutar SDK.",
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
                        "sdk_executed": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if (
            not args.root_update_vault
            or args.output
            or args.profile.absolute() != PROFILE_PATH
            or not args.expected_profile_sha256
        ):
            raise ValueError("Operación exige flags y perfil fijo de Root.")
        raw = regular(PROFILE_PATH, 32768)
        if digest(raw) != args.expected_profile_sha256:
            raise ValueError("Perfil distinto del SHA externo.")
        profile = json.loads(raw)
        if profile["sources"]["local"] != digest(regular(Path(__file__))):
            raise ValueError("Controlador local cambió.")
        remote = remote_contract(profile["sources"]["remote"])
        remote.validate_plan(profile)
        values = read_values(profile)
        if any(values.get(key) != value for key, value in bound_fields(profile).items()):
            raise ValueError("El perfil no coincide con los pins respaldados.")
        if args.mode != "stop" and profile != proposal():
            raise ValueError("Las referencias o datos cambiaron desde el respaldo.")
        previous = saved(values, args.mode, profile)
        reviewed = values.get(PREFIX + "GRAPH_REVIEW_STATUS", "")
        remote.require_previous(
            args.mode,
            {key: value["receipt"] for key, value in previous.items()},
            reviewed=reviewed == remote.REVIEWED,
        )
        approval = sdk_approval(values, profile, args.mode)
        client = load_reference("connector").connect(values)
        response = request(client, profile, args.mode, previous, reviewed, approval)
        persist(args.mode, response, profile, load_reference("updater").update)
        print(json.dumps(response, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, TimeoutError):
        print(
            json.dumps(
                {
                    "success": False,
                    "error": "SECURITY_REBUILD_REJECTED",
                    "owned_evidence_preserved": True,
                }
            )
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
