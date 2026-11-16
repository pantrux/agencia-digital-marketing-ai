"""Plan compile local y aplicación exclusiva del CLI de raíz, sin efectos al importar."""

import argparse
import base64
import importlib.util
import json
import shlex
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PREFIX = "MEDIA_POSTGRES_IMMICH_COMPILE_V2_"
FEATURE_KEY = "MEDIA_POSTGRES_IMMICH_SDK_V4_FEATURE_REVIEW_STATUS"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def contract():
    return load("pg_compile_contract_v2", HERE / "compile_remote_v2.py")


def sdk_local():
    remote = contract()
    path = HERE / "run_offline_sdk_v4.py"
    raw = path.read_bytes()
    if remote.digest(raw) != remote.V4_LOCAL_SHA:
        raise ValueError("Controlador SDK V4 distinto del congelado.")
    module = load("pg_compile_sdk_local", path)
    if remote.digest(module.read_source(path)) != remote.V4_LOCAL_SHA:
        raise ValueError("El controlador SDK cambió durante la carga.")
    return module


def build_profile(values=None):
    remote, local = contract(), sdk_local()
    values = local.read_values() if values is None else values
    plan = local.decode(values[local.KEYS["plan"]])
    if plan != local.bind_plan(local.proposal(), values):
        raise ValueError("El plan SDK activo no coincide con fuentes/layout actuales.")
    if values.get(FEATURE_KEY) != remote.FEATURE_STATUS:
        raise ValueError("Falta la revisión literal de features y enlace custom.")
    proof = local.read_source(
        ROOT / ".tools/evidence/phase4-postgres-immich-target-features-real.json"
    )
    remote.validate_feature_wrapper(proof)
    resume_raw = local.read_source(ROOT / ".tools/postgres-immich-build/resume-v1-profile.json")
    if (
        remote.digest(resume_raw) != remote.RESUME_PROFILE_SHA
        or remote.digest(local.read_source(HERE / "resume_sdk_remote.py"))
        != remote.RESUME_SOURCE_SHA
    ):
        raise ValueError("La unidad de reanudación dejó de coincidir con su freeze.")
    restart = local.decode(values.get("MEDIA_POSTGRES_IMMICH_RESUME_V1_RESTART_B64"))
    profile = {
        "schema_version": 1,
        "cid": remote.CID,
        "sdk_plan": plan,
        "backed": local.Controller(values, plan).saved(),
        "feature_status": values[FEATURE_KEY],
        "feature_wrapper_b64": base64.b64encode(proof).decode(),
        "settings": remote.settings(),
        "resume": {"profile_sha256": remote.RESUME_PROFILE_SHA, "restart": restart},
        "sources": {
            name: remote.digest(local.read_source(path))
            for name, path in {
                "local": Path(__file__),
                "remote": HERE / "compile_remote_v2.py",
                "recipe": HERE / "compile-recipe-v2.sh",
            }.items()
        },
    }
    remote.validate_profile(profile)
    return profile


def connect(values):
    return sdk_local().connect(values)


def verify_pins(profile, expected, values):
    remote = contract()
    if remote.digest(remote.canonical(profile)) != expected or profile != build_profile(values):
        raise ValueError("Perfil, fuentes o estado SDK distintos del SHA externo.")
    wanted = {
        "PROFILE_SHA256": expected,
        "PROFILE_B64": base64.b64encode(remote.canonical(profile)).decode(),
        **{name.upper() + "_SHA256": value for name, value in profile["sources"].items()},
    }
    if any(values.get(PREFIX + key) != value for key, value in wanted.items()):
        raise ValueError("El perfil y sus fuentes deben quedar respaldados antes de apply.")


def request(profile, client, *, action="compile"):
    remote, local = contract(), sdk_local()
    source = local.read_source(HERE / "compile_remote_v2.py")
    recipe = local.read_source(HERE / "compile-recipe-v2.sh")
    sdk = local.read_source(HERE / "offline_sdk_remote_v4.py")
    layout = local.read_source(ROOT / "docker/host_layout.py")
    if (
        remote.digest(source) != profile["sources"]["remote"]
        or remote.digest(recipe) != profile["sources"]["recipe"]
        or remote.digest(sdk) != remote.V4_REMOTE_SHA
        or remote.digest(layout) != profile["sdk_plan"]["layout_helper_sha256"]
    ):
        raise ValueError("Una fuente cambió antes de transmitir.")
    payload = {
        "action": action,
        "profile": profile,
        "profile_sha256": remote.digest(remote.canonical(profile)),
        "sdk_source_b64": base64.b64encode(sdk).decode(),
        "layout_source_b64": base64.b64encode(layout).decode(),
        "remote_source_b64": base64.b64encode(source).decode(),
        "recipe_source_b64": base64.b64encode(recipe).decode(),
    }
    encoded = remote.canonical(payload)
    if len(encoded) > 512 * 1024:
        raise ValueError("Petición compile excesiva.")
    code = (
        "import base64,hashlib;d=base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + ");g={'__name__':'__main__','_COMPILE_SOURCE_SHA256':hashlib.sha256(d).hexdigest()};exec(compile(d,'<compile-fijado>','exec'),g)"
    )
    if len(code.encode()) > 96 * 1024:
        raise ValueError("Helper compile demasiado grande para argv Linux.")
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(3)
        channel.exec_command("sudo -n /usr/bin/python3 -B -c " + shlex.quote(code))
        channel.sendall(struct.pack(">I", len(encoded)) + encoded)
        # Mantener stdin abierto permite detectar EOF y cinco segundos sin latidos.
        return local.collect(
            channel,
            time.monotonic() + (1810 if action == "compile" else 25),
            512 * 1024,
            heartbeat=True,
        )
    finally:
        channel.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply"), nargs="?", default="plan")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        remote = contract()
        if args.mode == "plan":
            profile = build_profile()
            data = remote.canonical(profile)
            if args.output:
                directory = ROOT / ".tools/postgres-immich-build"
                if args.output.absolute().parent != directory or directory.resolve() != directory:
                    raise ValueError(
                        "El perfil sólo puede escribirse en la raíz privada ya acreditada."
                    )
                with args.output.open("xb") as stream:
                    stream.write(data)
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "profile_sha256": remote.digest(data),
                        "sources": profile["sources"],
                        "settings": profile["settings"],
                        "ssh_executed": False,
                        "sdk_executed": False,
                    }
                )
            )
            return 0
        if not args.root_update_vault or not args.profile or not args.expected_profile_sha256:
            raise ValueError("Apply exige CLI de raíz, perfil y SHA externo respaldados.")
        local = sdk_local()
        raw = local.read_source(args.profile)
        profile = local.contract().json_object(raw)
        if remote.canonical(profile) != raw:
            raise ValueError("El perfil no es canónico.")
        values = local.read_values()
        verify_pins(profile, args.expected_profile_sha256, values)
        client = connect(values)
        response = request(profile, client)
        value = response.get("result", {})
        receipt = value.get("receipt", {})
        if (
            value
            != {"receipt": receipt, "receipt_sha256": remote.digest(remote.canonical(receipt))}
            or receipt.get("cid") != remote.CID
            or receipt.get("profile_sha256") != args.expected_profile_sha256
            or receipt.get("source_sha256") != profile["sources"]["remote"]
            or type(receipt.get("success")) is not bool
        ):
            raise ValueError("El recibo compile no acredita CID, perfil y fuente.")
        # Campo independiente: no altera los B64 del SDK ni avanza hacia SQL o runtime.
        field = PREFIX + "RECEIPT_B64"
        encoded = base64.b64encode(remote.canonical(value)).decode()
        if values.get(field) not in (None, "", "CHANGE_ME", encoded):
            raise ValueError("Existe un recibo compile anterior; no se sobrescribe.")
        local.root_writer(profile["sdk_plan"])({field: encoded})
        if local.read_values().get(field) != encoded:
            raise ValueError("El recibo no quedó respaldado durablemente.")
        print(
            json.dumps(
                {
                    "success": receipt["success"],
                    "receipt_sha256": value["receipt_sha256"],
                    "profile_sha256": args.expected_profile_sha256,
                    "cid": remote.CID,
                    "sql_verified": False,
                    "final_sca_verified": False,
                }
            )
        )
        return 0 if receipt["success"] else 1
    except BaseException as error:
        # También un recibo inválido o un respaldo fallido cierra el CID propio.
        if client is not None:
            try:
                error.cleanup = request(profile, client, action="stop")
            except BaseException as failed:
                error.cleanup = {"stopped": False, "error_type": type(failed).__name__}
        result = {
            "success": False,
            "error_type": type(error).__name__,
            "partial_evidence_preserved": True,
            "values_hidden": True,
        }
        if hasattr(error, "cleanup"):
            result["cleanup"] = error.cleanup
        print(json.dumps(result))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
