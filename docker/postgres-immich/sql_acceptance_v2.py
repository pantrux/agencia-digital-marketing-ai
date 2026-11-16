"""Plan y etapas SQL aisladas; importar o pedir el plan no accede a la bóveda ni red."""

import argparse
import ast
import base64
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shlex
import stat
import struct
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PREFIX = "MEDIA_POSTGRES_IMMICH_SQL_FUNCTIONAL_V2_"
LOCAL_ROOT = ROOT / ".tools/backup-source-build/postgres-sql-functional-v2-inputs"
NATIVE_ACL_SHA256 = "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b"
PARENT_MARKER_SHA = "78925844fd312b9ce3be220ee9d2fe41442908101af8c14416895811110bcff6"
RUNTIME_FILES = {
    "prepare": ROOT
    / ".tools/postgres-immich-security-inputs-v1/security-runtime-v3.prepare.2991994f50484581ae7fe13e895786ad.json",
    "build": ROOT
    / ".tools/postgres-immich-security-inputs-v1/security-runtime-v3.build.f22671b0e0c14d108dc3a148b7480b2e.json",
}


def load(name, path, expected=None):
    """Compilar exactamente los bytes cotejados; nunca ejecutar caché .pyc."""
    raw = read_source(path)
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("El módulo no coincide con su hash antes de ejecutarlo.")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def contract():
    return load("postgres_sql_contract_v2", HERE / "sql_acceptance_remote_v2.py")


def fixed_contract():
    return load(
        "postgres_sql_sdk_data_only", HERE / "offline_sdk_remote_v4.py", contract().SDK_SOURCE
    )


def sdk_local():
    """Cierre AST exacto de transporte SDK685b; no mutar/importar el SDK completo."""
    path = HERE / "run_offline_sdk_v4.py"
    raw = read_source(path)
    if hashlib.sha256(raw).hexdigest() != contract().SDK_LOCAL:
        raise ValueError("El transporte SDK V4 no corresponde a la fuente fijada.")
    nodes = []
    for node in ast.parse(raw).body:
        if isinstance(node, ast.FunctionDef) and node.name in {"connect", "collect"}:
            nodes.append(node)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"PREFIX", "KEYS"}
        ):
            nodes.append(node)
    if {n.name for n in nodes if isinstance(n, ast.FunctionDef)} != {"connect", "collect"} or {
        n.targets[0].id for n in nodes if isinstance(n, ast.Assign)
    } != {"PREFIX", "KEYS"}:
        raise ValueError("El cierre del transporte SDK no es el conjunto exacto.")
    module = types.ModuleType("sql_v2_sdk_transport_closed")
    module.__file__ = str(path)
    module.__dict__.update(
        base64=base64,
        hashlib=hashlib,
        io=io,
        time=time,
        importlib=importlib,
        contract=fixed_contract,
        load=load,
        read_source=read_source,
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), module.__dict__)
    return module


def read_source(path, maximum=256 * 1024):
    """NoFollow y metadata antes/después, con ctime separado por API en Windows."""
    path = Path(path).absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("Ruta local no canónica.")
    for node in [*reversed(path.parents), path]:
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Enlace/reparse local no permitido.")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 <= info.st_size <= maximum:
        raise ValueError("Archivo local no ordinario, enlazado o excesivo.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        before = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(maximum + 1)
        after, named = os.fstat(fd), path.lstat()

        def key(v):
            return (v.st_dev, v.st_ino, v.st_mode, v.st_size, v.st_mtime_ns, v.st_nlink)

        if not (
            key(info) == key(before) == key(after) == key(named)
            and info.st_ctime_ns == named.st_ctime_ns
            and before.st_ctime_ns == after.st_ctime_ns
            and len(data) == info.st_size
        ):
            raise ValueError("Archivo local cambió durante la lectura.")
        return data
    finally:
        os.close(fd)


def access(sdk):
    return sdk.load("sql_v2_native_access", ROOT / "sre/backup_tools_access.py", NATIVE_ACL_SHA256)


def private_inputs(sdk):
    parent = ROOT / ".tools/backup-source-build"
    access(sdk).check_access(parent, private=True)
    raw = read_source(parent / ".agency-backup-build-owner", 256)
    if (
        contract().digest(raw) != PARENT_MARKER_SHA
        or raw != b"agencia-digital-marketing-ai:backup-source-build:v1"
    ):
        raise ValueError("El padre privado no acredita su marcador literal.")
    if LOCAL_ROOT.parent != parent or LOCAL_ROOT.resolve(strict=True) != LOCAL_ROOT:
        raise ValueError("Raíz de entradas SQL V2 no canónica.")
    info = LOCAL_ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("La raíz de entradas debe ser un directorio propio precreado.")
    access(sdk).check_access(LOCAL_ROOT, private=True)


def private_read(path, sdk, maximum=512 * 1024):
    private_inputs(sdk)
    path = Path(path)
    if not path.is_absolute() or path.parent != LOCAL_ROOT:
        raise ValueError("Aprobación/perfil fuera de la raíz privada SQL V2.")
    access(sdk).check_access(path, private=True)
    return read_source(path, maximum)


def write_private(path, data, sdk):
    private_inputs(sdk)
    path = Path(path)
    if path.parent != LOCAL_ROOT:
        raise ValueError("Salida fuera de la raíz privada SQL V2.")
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    access(sdk).check_access(path, private=True)


def read_values(sdk):
    parser = sdk.load(
        "sql_canonical_env", ROOT / "scripts/ci/check_env.py", contract().ENV_PARSER_SHA
    )
    access(sdk).check_access(ROOT / ".env", private=True)
    raw = read_source(ROOT / ".env", 8 * 1024**2)
    values, errors = parser.parse_env(raw.decode("utf-8-sig"))
    if errors:
        raise ValueError("La bóveda no cumple el parser canónico fijado.")
    return values


def decode(sdk, text, *, optional=False):
    if optional and text in (None, "", "CHANGE_ME"):
        return {}
    raw = base64.b64decode(text, validate=True)
    if len(raw) > 512 * 1024:
        raise ValueError("Documento SQL de bóveda excesivo.")
    value = sdk.contract().json_object(raw)
    if not isinstance(value, dict):
        raise ValueError("Se requiere un objeto SQL en la bóveda.")
    return value


def sources(sdk):
    return {
        name: contract().digest(sdk.read_source(path))
        for name, path in {
            "local": Path(__file__),
            "remote": HERE / "sql_acceptance_remote_v2.py",
            "sql": HERE / "acceptance.sql",
        }.items()
    }


def runtime_receipts(approval, sdk):
    values = {}
    for stage, expected in contract().RUNTIME_PINS.items():
        reference = approval["runtime"][stage]
        path = Path(reference["path"])
        if path != RUNTIME_FILES[stage] or not path.is_absolute():
            raise ValueError("Ruta Prepare/Build diferente de la evidencia física revisada.")
        raw = read_source(path, 1024**2)
        if contract().digest(raw) != expected or reference["sha256"] != expected:
            raise ValueError("Prepare/Build físico cambió desde la revisión.")
        values[stage] = sdk.contract().json_object(raw)
    contract().validate_runtime_receipts(values["prepare"], values["build"])
    return values


def approved(values, sdk):
    path = Path(values[PREFIX + "APPROVAL_PATH"])
    if path != LOCAL_ROOT / "approval.json":
        raise ValueError("Ruta de autorización funcional no respaldable.")
    raw = private_read(path, sdk)
    if contract().digest(raw) != values[PREFIX + "APPROVAL_SHA256"]:
        raise ValueError("La autorización funcional difiere de su SHA externo.")
    value = sdk.contract().json_object(raw)
    contract().validate_approval(value, contract().IMAGE_ID)
    runtime_receipts(value, sdk)
    return value, contract().digest(contract().canonical(value))


def root_writer(sdk, plan):
    """Sólo el CLI explícito de raíz usa el actualizador canónico por SHA."""
    pin = "0fc75c6d082e9e25fa6a5b5146560d7373b3dfab2d701e351749dafb2183b46c"
    if plan.get("vault_helper_sha256") != pin:
        raise ValueError("Actualizador de bóveda distinto del helper canónico.")
    helper = sdk.load("sql_v2_root_writer", ROOT / ".tools/vault_updates.py", pin)

    def write(updates):
        comments = {
            key: (
                "# Para qué: Respaldar etapas de aceptación SQL funcional aislada; no aprobación de producción.",
                "# Dónde: Perfil privado SQL V2 y CID propio en vm-media-01.",
                "# Riesgo al rotar: Revisar recibos e identidad antes de continuar; nunca repetir una etapa.",
            )
            for key in updates
        }
        return helper.update(updates, comments=comments)

    return write


def backed_paths(values, parent):
    expected = {
        "ROOT": parent + "/sql-functional-v2",
        "DATA_PATH": parent + "/sql-functional-v2/data",
        "SECRETS_PATH": parent + "/sql-functional-v2/secrets",
        "OWNER": contract().OWNER,
    }
    if any(values.get(PREFIX + key) != value for key, value in expected.items()):
        raise ValueError("Las rutas/owner SQL deben respaldarse antes de preparar.")
    return expected


def build_profile(values, sdk):
    remote = contract()
    approval, approval_sha = approved(values, sdk)
    physical = runtime_receipts(approval, sdk)
    parent = physical["prepare"]["receipt"]["directories"]["record"]
    paths = backed_paths(values, parent["path"])
    password = values.get(PREFIX + "PASSWORD", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", password):
        raise ValueError("Falta la contraseña SQL persistida antes de su uso.")
    profile = {
        "schema_version": 2,
        "owner": remote.OWNER,
        "target": {"hostname": "vm-media-01", "ip": "192.168.0.85", "user": "jandrade", "port": 22},
        "parent": parent,
        "root": paths["ROOT"],
        "data": paths["DATA_PATH"],
        "secrets": paths["SECRETS_PATH"],
        "name": values[PREFIX + "CONTAINER_NAME"],
        "image": approval["image"],
        "password_sha256": remote.digest(password.encode()),
        "approval_sha256": approval_sha,
        "sources": sources(sdk),
        "budgets": remote.BUDGETS,
        "sdk_plan": decode(sdk, values[sdk.KEYS["plan"]]),
    }
    remote.validate_profile(profile)
    return profile


def verify_pins(profile, expected, values, sdk, *, stop=False):
    remote = contract()
    remote.validate_profile(profile)
    if remote.digest(remote.canonical(profile)) != expected or profile["sources"] != sources(sdk):
        raise ValueError("El perfil o sus fuentes SQL cambiaron desde la revisión.")
    wanted = {
        "PROFILE_B64": base64.b64encode(remote.canonical(profile)).decode(),
        "PROFILE_SHA256": expected,
        **{name.upper() + "_SHA256": sha for name, sha in profile["sources"].items()},
    }
    if any(values.get(PREFIX + key) != value for key, value in wanted.items()):
        raise ValueError("El perfil/fuentes SQL deben quedar respaldados antes de cada etapa.")
    backed_paths(values, profile["parent"]["path"])
    if values.get(PREFIX + "CONTAINER_NAME") != profile["name"]:
        raise ValueError("El nombre SQL no coincide con el respaldo.")
    if not stop and profile != build_profile(values, sdk):
        raise ValueError("El perfil no coincide con secreto/aprobación/estado respaldados.")


def validate_result(value, mode, profile):
    remote = contract()
    receipt = value.get("receipt", {})
    if (
        value != remote.envelope(receipt)
        or receipt.get("stage") != mode
        or receipt.get("profile_sha256") != remote.digest(remote.canonical(profile))
        or receipt.get("source_sha256") != profile["sources"]["remote"]
        or type(receipt.get("success")) is not bool
        or receipt.get("owner") != remote.OWNER
        or receipt.get("authorization_scope") != "functional_test_only"
        or receipt.get("production_approved") is not False
        or receipt.get("coverage_complete") is not False
        or receipt.get("global_high_critical", "ausente") is not None
    ):
        raise ValueError("El recibo SQL no acredita fuente, perfil y etapa revisados.")
    if mode == "create" and receipt["success"] and not remote.sha(receipt.get("cid")):
        raise ValueError("Create SQL no acredita un CID completo.")
    return receipt


def request(client, mode, profile, backed, values, sdk):
    remote = contract()
    source = read_source(HERE / "sql_acceptance_remote_v2.py")
    sql = read_source(HERE / "acceptance.sql")
    fixed = sdk.read_source(HERE / "offline_sdk_remote_v4.py")
    layout = sdk.read_source(ROOT / "docker/host_layout.py")
    for data, expected in (
        (source, profile["sources"]["remote"]),
        (sql, profile["sources"]["sql"]),
        (fixed, remote.SDK_SOURCE),
        (layout, profile["sdk_plan"]["layout_helper_sha256"]),
    ):
        if remote.digest(data) != expected:
            raise ValueError("Una fuente cambió antes de la transmisión SQL.")
    payload = {
        "mode": mode,
        "profile": profile,
        "profile_sha256": remote.digest(remote.canonical(profile)),
        "backed": backed,
        "sdk_source_b64": base64.b64encode(fixed).decode(),
        "layout_source_b64": base64.b64encode(layout).decode(),
    }
    # Stop conserva pruebas físicas del runtime; no lee aprobación ni contraseña.
    refs = {k: {"path": str(RUNTIME_FILES[k]), "sha256": v} for k, v in remote.RUNTIME_PINS.items()}
    payload["runtime_receipts"] = runtime_receipts({"runtime": refs}, sdk)
    if mode not in ("stop1", "stop2"):
        payload["approval"] = approved(values, sdk)[0]
    if mode == "prepare":
        payload["password"] = values[PREFIX + "PASSWORD"]
        payload["sql_source_b64"] = base64.b64encode(sql).decode()
    data = remote.canonical(payload)
    if len(data) > 512 * 1024:
        raise ValueError("Petición SQL excesiva.")
    code = (
        "import base64,hashlib;d=base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + ");g={'__name__':'__main__','_SQL_SOURCE_SHA256':hashlib.sha256(d).hexdigest()};exec(compile(d,'<sql-fijado>','exec'),g)"
    )
    if len(code.encode()) > 96 * 1024:
        raise ValueError("Helper SQL excesivo para argv Linux.")
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(3)
        channel.exec_command("sudo -n /usr/bin/python3 -B -c " + shlex.quote(code))
        channel.sendall(struct.pack(">I", len(data)) + data)
        return sdk.collect(
            channel, time.monotonic() + remote.BUDGETS[mode] + 5, 4 * 1024**2, heartbeat=True
        )
    finally:
        # EOF hace fallar el watchdog del ejecutor y activa stop del CID propio.
        channel.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", *contract().STAGES), nargs="?", default="plan")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        remote = contract()
        if args.mode == "plan" and args.output is None:
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "infrastructure_executed": False,
                        "stages": remote.STAGES,
                        "budgets": remote.BUDGETS,
                        "required_gates": [
                            "runtime-v3-physical-build-prepare-and-abi",
                            "root-functional-test-only-approval-1h",
                            "credentials-and-cid-backed",
                        ],
                        "sql_verified": False,
                        "production_approved": False,
                        "coverage_complete": False,
                        "global_high_critical": None,
                        "final_profile_ready": False,
                        "local_inputs_root": str(LOCAL_ROOT),
                        "remote_parent": remote.PARENT_PROOF,
                        "image_id": remote.IMAGE_ID,
                        "config_digest": remote.CONFIG_DIGEST,
                        "script_sha256": remote.SCRIPT_SHA256,
                        "required_fields_prefix": PREFIX,
                    }
                )
            )
            return 0
        sdk = sdk_local()
        values = read_values(sdk)
        if args.mode == "plan":
            if not args.root_update_vault:
                raise ValueError("Escribir el perfil requiere invocación explícita de raíz.")
            profile = build_profile(values, sdk)
            output = args.output.absolute()
            directory = LOCAL_ROOT
            if output.parent != directory or directory.resolve() != directory:
                raise ValueError("El perfil sólo puede escribirse en la raíz privada respaldada.")
            raw = remote.canonical(profile)
            write_private(output, raw, sdk)
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "profile_sha256": remote.digest(raw),
                        "sources": profile["sources"],
                        "infrastructure_executed": False,
                    }
                )
            )
            return 0
        if not args.root_update_vault or not args.profile or not args.expected_profile_sha256:
            raise ValueError("Cada etapa exige CLI de raíz y perfil/SHA externo respaldados.")
        if Path(values.get(PREFIX + "PROFILE_PATH", "")) != args.profile.absolute():
            raise ValueError("La ruta del perfil no coincide con el respaldo de raíz.")
        raw = private_read(args.profile, sdk)
        profile = sdk.contract().json_object(raw)
        if raw != remote.canonical(profile):
            raise ValueError("El perfil SQL debe ser JSON canónico.")
        stop = args.mode in ("stop1", "stop2")
        verify_pins(profile, args.expected_profile_sha256, values, sdk, stop=stop)
        backed = decode(sdk, values.get(PREFIX + "RECEIPTS_B64"), optional=True)
        if args.mode in backed:
            raise ValueError("Esta etapa ya tiene recibo; no se permite sobrescribir ni repetir.")
        if args.mode != "prepare":
            remote.require_backup(args.mode, backed, backed)
        write_private(
            LOCAL_ROOT / ("." + args.mode + "-started"),
            remote.canonical({"profile_sha256": args.expected_profile_sha256, "mode": args.mode}),
            sdk,
        )
        client = sdk.connect(values)
        response = request(client, args.mode, profile, backed, values, sdk)
        result = response.get("result", {})
        receipt = validate_result(result, args.mode, profile)
        if response.get("success") != receipt["success"]:
            raise ValueError("El framing SQL y el recibo discrepan.")
        write_private(LOCAL_ROOT / (args.mode + ".result.json"), remote.canonical(result), sdk)
        updated = {**backed, args.mode: result}
        encoded = base64.b64encode(remote.canonical(updated)).decode()
        # CAS lógico antes de escribir; sólo esta invocación explícita de raíz actualiza la bóveda.
        current = read_values(sdk)
        if current.get(PREFIX + "RECEIPTS_B64") != values.get(PREFIX + "RECEIPTS_B64"):
            raise ValueError("Los recibos cambiaron durante la etapa; conservar evidencia remota.")
        root_writer(sdk, profile["sdk_plan"])({PREFIX + "RECEIPTS_B64": encoded})
        if read_values(sdk).get(PREFIX + "RECEIPTS_B64") != encoded:
            raise ValueError("El recibo SQL no quedó respaldado duraderamente.")
        print(
            json.dumps(
                {
                    "success": receipt["success"],
                    "stage": args.mode,
                    "receipt_sha256": result["receipt_sha256"],
                    "cid": receipt.get("cid"),
                    "functional_verified": receipt["success"]
                    and args.mode in ("verify1", "verify2"),
                    "production_approved": False,
                    "coverage_complete": False,
                    "global_high_critical": None,
                }
            )
        )
        return 0 if receipt["success"] else 1
    except BaseException as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "values_hidden": True,
                    "partial_evidence_preserved": True,
                }
            )
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
