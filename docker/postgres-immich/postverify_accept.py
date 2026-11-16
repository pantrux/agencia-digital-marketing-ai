"""Plan local y postverificación explícita de raíz, sin acciones al importar."""

import argparse
import base64
import importlib.util
import json
import shlex
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PREFIX = "MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_ACCEPT_"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def contract():
    return load("pg_postverify_contract", HERE / "postverify_accept_remote.py")


def sdk_local():
    remote = contract()
    path = HERE / "run_offline_sdk_v4.py"
    if remote.digest(path.read_bytes()) != remote.SDK_LOCAL:
        raise ValueError("El CLI SDK V4 difiere del congelado.")
    local = load("pg_postverify_sdk_cli", path)
    if remote.digest(local.read_source(path)) != remote.SDK_LOCAL:
        raise ValueError("El CLI SDK cambió durante su carga.")
    return local


def sources(local):
    remote = contract()
    return {
        name: remote.digest(local.read_source(path))
        for name, path in {
            "local": Path(__file__),
            "remote": HERE / "postverify_accept_remote.py",
        }.items()
    }


def proposal():
    remote = contract()
    return {
        "schema_version": 1,
        "cid": remote.CID,
        "archive_path": remote.ARCHIVE_PATH,
        "budget_seconds": 180,
        "maximum_archive_bytes": remote.MAX_ARCHIVE,
        "sdk_executed": False,
        "compiler_executed": False,
        "sql_verified": False,
        "final_sca_verified": False,
    }


def build_profile(values):
    local, remote = sdk_local(), contract()
    sdk_plan = local.decode(values[local.KEYS["plan"]])
    if sdk_plan != local.bind_plan(local.proposal(), values):
        raise ValueError("El plan SDK actual no coincide con fuentes/layout respaldados.")
    compiled = local.decode(values["MEDIA_POSTGRES_IMMICH_COMPILE_V3_PROFILE_B64"])
    if values.get("MEDIA_POSTGRES_IMMICH_COMPILE_V3_PROFILE_SHA256") != remote.COMPILE_PROFILE:
        raise ValueError("El perfil compile V3 histórico no está respaldado.")
    profile = {
        **proposal(),
        "compile_profile": compiled,
        "sdk_plan": sdk_plan,
        "sdk_backed": local.Controller(values, sdk_plan).saved(),
        "failed": local.decode(values["MEDIA_POSTGRES_IMMICH_COMPILE_V3_RECEIPT_B64"]),
        "prior_postverify_profile": local.decode(
            values["MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_PROFILE_B64"]
        ),
        "prior_postverify_failed": local.decode(
            values["MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_RECEIPT_B64"]
        ),
        "sources": sources(local),
    }
    remote.validate_profile(profile)
    return profile


def verify_pins(profile, expected, values):
    remote = contract()
    if remote.digest(remote.canonical(profile)) != expected or profile != build_profile(values):
        raise ValueError("Perfil postverify distinto del SHA externo o del estado respaldado.")
    wanted = {
        "PROFILE_SHA256": expected,
        "PROFILE_B64": base64.b64encode(remote.canonical(profile)).decode(),
        **{name.upper() + "_SHA256": value for name, value in profile["sources"].items()},
    }
    if any(values.get(PREFIX + key) != value for key, value in wanted.items()):
        raise ValueError("Perfil y fuentes deben respaldarse antes de apply.")


def request(client, profile):
    local, remote = sdk_local(), contract()
    source = local.read_source(HERE / "postverify_accept_remote.py")
    sdk = local.read_source(HERE / "offline_sdk_remote_v4.py")
    compiler = local.read_source(HERE / "compile_remote_v3.py")
    layout = local.read_source(ROOT / "docker/host_layout.py")
    if any(
        remote.digest(raw) != sha
        for raw, sha in (
            (source, profile["sources"]["remote"]),
            (sdk, remote.SDK_SOURCE),
            (compiler, remote.COMPILE_SOURCE),
            (layout, profile["sdk_plan"]["layout_helper_sha256"]),
        )
    ):
        raise ValueError("Una fuente cambió antes de transmitir.")
    payload = {
        "profile": profile,
        "profile_sha256": remote.digest(remote.canonical(profile)),
        "sdk_source_b64": base64.b64encode(sdk).decode(),
        "compile_source_b64": base64.b64encode(compiler).decode(),
        "layout_source_b64": base64.b64encode(layout).decode(),
    }
    data = remote.canonical(payload)
    if len(data) > 512 * 1024:
        raise ValueError("Petición postverify excesiva.")
    code = (
        "import base64,hashlib;d=base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + ");g={'__name__':'__main__','_POSTVERIFY_ACCEPT_SOURCE_SHA256':hashlib.sha256(d).hexdigest()};exec(compile(d,'<postverify-fijado>','exec'),g)"
    )
    if len(code.encode()) > 96 * 1024:
        raise ValueError("El helper excede el límite argv.")
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(3)
        channel.exec_command("sudo -n /usr/bin/python3 -B -c " + shlex.quote(code))
        channel.sendall(struct.pack(">I", len(data)) + data)
        channel.shutdown_write()
        return local.collect(channel, time.monotonic() + 190, 512 * 1024)
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
        if args.mode == "plan" and args.output is None:
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "proposal": proposal(),
                        "sources": sources(sdk_local()),
                        "infrastructure_executed": False,
                    }
                )
            )
            return 0
        local = sdk_local()
        values = local.read_values()
        if args.mode == "plan":
            profile = build_profile(values)
            directory = ROOT / ".tools/postgres-immich-build"
            if args.output.absolute().parent != directory or directory.resolve() != directory:
                raise ValueError("El perfil sólo puede escribirse en la raíz privada respaldada.")
            data = remote.canonical(profile)
            with args.output.open("xb") as stream:
                stream.write(data)
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "profile_sha256": remote.digest(data),
                        "sources": profile["sources"],
                        "infrastructure_executed": False,
                    }
                )
            )
            return 0
        if not args.root_update_vault or not args.profile or not args.expected_profile_sha256:
            raise ValueError("Apply exige CLI de raíz, perfil y SHA externo respaldados.")
        raw = local.read_source(args.profile, 256 * 1024)
        profile = local.contract().json_object(raw)
        if raw != remote.canonical(profile):
            raise ValueError("Perfil no canónico.")
        verify_pins(profile, args.expected_profile_sha256, values)
        field = PREFIX + "RECEIPT_B64"
        if values.get(field) not in (None, "", "CHANGE_ME"):
            raise ValueError("Existe un recibo postverify respaldado; no se repite la etapa.")
        client = local.connect(values)
        response = request(client, profile)
        result = response.get("result", {})
        receipt = result.get("receipt", {})
        if (
            result != remote.envelope(receipt)
            or receipt.get("stage") != remote.STAGE
            or receipt.get("source_sha256") != profile["sources"]["remote"]
            or receipt.get("profile_sha256") != args.expected_profile_sha256
            or receipt.get("cid") != remote.CID
            or type(receipt.get("success")) is not bool
        ):
            raise ValueError("El recibo no acredita CID, perfil, fuente y etapa postverify.")
        encoded = base64.b64encode(remote.canonical(result)).decode()
        local.root_writer(profile["sdk_plan"])({field: encoded})
        if local.read_values().get(field) != encoded:
            raise ValueError("El recibo postverify no quedó respaldado durablemente.")
        print(
            json.dumps(
                {
                    "success": receipt["success"],
                    "receipt_sha256": result["receipt_sha256"],
                    "artifact_sha256": receipt.get("artifact_sha256"),
                    "sdk_executed": False,
                    "compiler_executed": False,
                    "sql_verified": False,
                    "final_sca_verified": False,
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
                    "partial_evidence_preserved": True,
                    "values_hidden": True,
                }
            )
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
