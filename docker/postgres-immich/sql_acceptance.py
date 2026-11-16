"""Plan y etapas SQL aisladas; importar o pedir el plan no accede a la bóveda ni red."""

import argparse
import base64
import importlib.util
import json
import re
import shlex
import struct
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PREFIX = "MEDIA_POSTGRES_IMMICH_SQL_"
FINAL_PREFIX = "MEDIA_POSTGRES_IMMICH_FINAL_"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contract():
    return load("postgres_sql_contract", HERE / "sql_acceptance_remote.py")


def sdk_local():
    remote = contract()
    path = HERE / "run_offline_sdk_v4.py"
    if remote.digest(path.read_bytes()) != remote.SDK_LOCAL:
        raise ValueError("El CLI SDK V4 no coincide con la fuente congelada.")
    module = load("postgres_sql_sdk", path)
    if remote.digest(module.read_source(path)) != remote.SDK_LOCAL:
        raise ValueError("El CLI SDK cambió durante su lectura.")
    return module


def read_values(sdk):
    parser = sdk.load(
        "sql_canonical_env", ROOT / "scripts/ci/check_env.py", contract().ENV_PARSER_SHA
    )
    raw = sdk.read_source(ROOT / ".env", 8 * 1024**2)
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
            "remote": HERE / "sql_acceptance_remote.py",
            "sql": HERE / "acceptance.sql",
        }.items()
    }


def verify_reports(approval, *, root=None, sdk=None):
    """Cotejar bytes de reportes privados; una bandera clean aislada no acredita cobertura."""
    root = ROOT / ".tools/evidence" if root is None else Path(root)
    sdk = sdk_local() if sdk is None else sdk
    remote = contract()
    for report in approval["reports"]:
        path = Path(report["path"])
        if not path.is_absolute() or not path.is_relative_to(root) or path.resolve() != path:
            raise ValueError("El reporte SCA debe estar en la raíz privada de evidencia.")
        data = sdk.read_source(path, 16 * 1024**2)
        if remote.digest(data) != report["sha256"]:
            raise ValueError("Un reporte SCA cambió desde la aprobación respaldada.")
        # Rechazar informes vacíos o JSON malformado, sin inventar un esquema Trivy/OSV común.
        value = sdk.contract().json_object(data)
        if not isinstance(value, (dict, list)) or not value:
            raise ValueError("Un reporte SCA está vacío o no es JSON verificable.")


def approved(values, sdk):
    remote = contract()
    path = Path(values[FINAL_PREFIX + "SCA_RECEIPT_PATH"])
    if not path.is_absolute() or not path.is_relative_to(ROOT / ".tools/evidence"):
        raise ValueError("La aprobación final debe tener ruta privada respaldada.")
    raw = sdk.read_source(path, 512 * 1024)
    if remote.digest(raw) != values[FINAL_PREFIX + "SCA_RECEIPT_SHA256"]:
        raise ValueError("La aprobación final difiere del hash respaldado.")
    value = sdk.contract().json_object(raw)
    remote.validate_approval(value, values[FINAL_PREFIX + "IMAGE_ID"])
    verify_reports(value, sdk=sdk)
    final = value["finalization"]
    field = final.get("vault_field", "")
    if not re.fullmatch(r"MEDIA_POSTGRES_IMMICH_[A-Z0-9_]+_RECEIPT_B64", field):
        raise ValueError("La finalización debe referir a un recibo duradero de este rol.")
    saved = decode(sdk, values[field])
    receipt = saved.get("receipt", {})
    if (
        saved != remote.envelope(receipt)
        or saved.get("receipt_sha256") != final["receipt_sha256"]
        or receipt.get("success") is not True
        or receipt.get("image_id") != value["image"]["id"]
        or receipt.get("config_digest") != value["image"]["config_digest"]
    ):
        raise ValueError("El subject SCA no coincide con la finalización respaldada.")
    return value, remote.digest(remote.canonical(value))


def backed_paths(values, parent):
    expected = {
        "ROOT": parent + "/sql-acceptance-v1",
        "DATA_PATH": parent + "/sql-acceptance-v1/data",
        "SECRETS_PATH": parent + "/sql-acceptance-v1/secrets",
        "OWNER": contract().OWNER,
    }
    if any(values.get(PREFIX + key) != value for key, value in expected.items()):
        raise ValueError("Las rutas/owner SQL deben respaldarse antes de preparar.")
    return expected


def build_profile(values, sdk):
    remote = contract()
    approval, approval_sha = approved(values, sdk)
    prepared = decode(sdk, values["MEDIA_POSTGRES_IMMICH_RUNTIME_PREPARED_B64"])
    receipt = prepared.get("receipt", {})
    if prepared != remote.envelope(receipt) or receipt.get("success") is not True:
        raise ValueError("Falta el padre runtime preparado y respaldado.")
    parent = receipt["directories"]["record"]
    paths = backed_paths(values, parent["path"])
    password = values.get(PREFIX + "PASSWORD", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", password):
        raise ValueError("Falta la contraseña SQL persistida antes de su uso.")
    profile = {
        "schema_version": 1,
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
    ):
        raise ValueError("El recibo SQL no acredita fuente, perfil y etapa revisados.")
    if mode == "create" and receipt["success"] and not remote.sha(receipt.get("cid")):
        raise ValueError("Create SQL no acredita un CID completo.")
    return receipt


def request(client, mode, profile, backed, values, sdk):
    remote = contract()
    source = sdk.read_source(HERE / "sql_acceptance_remote.py")
    sql = sdk.read_source(HERE / "acceptance.sql")
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
                            "final-image-finalization",
                            "os-cargo-sqlite-sca-approved",
                            "credentials-and-cid-backed",
                        ],
                        "sql_verified": False,
                        "final_profile_ready": False,
                    }
                )
            )
            return 0
        sdk = sdk_local()
        values = read_values(sdk)
        if args.mode == "plan":
            profile = build_profile(values, sdk)
            output = args.output.absolute()
            directory = ROOT / ".tools/postgres-immich-build"
            if output.parent != directory or directory.resolve() != directory:
                raise ValueError("El perfil sólo puede escribirse en la raíz privada respaldada.")
            raw = remote.canonical(profile)
            with output.open("xb") as stream:
                stream.write(raw)
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
        raw = sdk.read_source(args.profile, 512 * 1024)
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
        client = sdk.connect(values)
        response = request(client, args.mode, profile, backed, values, sdk)
        result = response.get("result", {})
        receipt = validate_result(result, args.mode, profile)
        if response.get("success") != receipt["success"]:
            raise ValueError("El framing SQL y el recibo discrepan.")
        updated = {**backed, args.mode: result}
        encoded = base64.b64encode(remote.canonical(updated)).decode()
        # CAS lógico antes de escribir; sólo esta invocación explícita de raíz actualiza la bóveda.
        current = read_values(sdk)
        if current.get(PREFIX + "RECEIPTS_B64") != values.get(PREFIX + "RECEIPTS_B64"):
            raise ValueError("Los recibos cambiaron durante la etapa; conservar evidencia remota.")
        sdk.root_writer(profile["sdk_plan"])({PREFIX + "RECEIPTS_B64": encoded})
        if read_values(sdk).get(PREFIX + "RECEIPTS_B64") != encoded:
            raise ValueError("El recibo SQL no quedó respaldado duraderamente.")
        print(
            json.dumps(
                {
                    "success": receipt["success"],
                    "stage": args.mode,
                    "receipt_sha256": result["receipt_sha256"],
                    "cid": receipt.get("cid"),
                    "sql_verified": receipt["success"] and args.mode in ("verify1", "verify2"),
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
