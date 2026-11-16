"""Plan local y promociones explícitas de raíz; importar no conecta ni construye."""

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
PREFIX = "MEDIA_POSTGRES_IMMICH_RUNTIME_"
ENV_PARSER_SHA = "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def contract():
    return load("postgres_final_image_contract", HERE / "final_image_remote.py")


def sdk_local():
    remote = contract()
    path = HERE / "run_offline_sdk_v4.py"
    if remote.digest(path.read_bytes()) != remote.SDK_LOCAL:
        raise ValueError("El CLI SDK V4 difiere de la fuente congelada.")
    local = load("postgres_final_image_sdk_cli", path)
    if remote.digest(local.read_source(path)) != remote.SDK_LOCAL:
        raise ValueError("El CLI SDK cambió durante su carga.")
    return local


def read_values(local):
    parser = local.load("runtime_canonical_env", ROOT / "scripts/ci/check_env.py", ENV_PARSER_SHA)
    raw = local.read_source(ROOT / ".env", 8 * 1024**2)
    values, errors = parser.parse_env(raw.decode("utf-8-sig"))
    if errors:
        raise ValueError("La bóveda no cumple el parser canónico fijado.")
    return values


def sources(local):
    remote = contract()
    return {
        name: remote.digest(local.read_source(path))
        for name, path in {
            "local": Path(__file__),
            "remote": HERE / "final_image_remote.py",
            "dockerfile": HERE / "Dockerfile.runtime",
        }.items()
    }


def build_profile(values):
    remote, local = contract(), sdk_local()
    expected = {
        "CONTEXT_ROOT": remote.CONTEXT,
        "RECORD_ROOT": remote.RECORD,
        "BASE_OCI_ROOT": remote.RECORD + "/base-oci",
        "OWNER": remote.OWNER,
    }
    if any(values.get(PREFIX + key) != value for key, value in expected.items()):
        raise ValueError("Faltan rutas/owner runtime respaldados.")
    sdk_plan = local.decode(values[local.KEYS["plan"]])
    if sdk_plan != local.bind_plan(local.proposal(), values):
        raise ValueError("El plan SDK no coincide con los pins/layout actuales.")
    # El perfil V3 aprobado es historia literal: NO se reconstruye tras STOP.
    compile_profile = local.decode(values["MEDIA_POSTGRES_IMMICH_COMPILE_V3_PROFILE_B64"])
    if values.get("MEDIA_POSTGRES_IMMICH_COMPILE_V3_PROFILE_SHA256") != remote.COMPILE_PROFILE:
        raise ValueError("El perfil V3 no está respaldado con el SHA revisado.")
    compiled = local.decode(values["MEDIA_POSTGRES_IMMICH_COMPILE_V3_RECEIPT_B64"])
    profile = {
        **remote.proposal(),
        "sdk_plan": sdk_plan,
        "sdk_backed": local.Controller(values, sdk_plan).saved(),
        "compile_profile": compile_profile,
        "failed": compiled,
        "postverify_profile": local.decode(
            values["MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_RECOVER_PROFILE_B64"]
        ),
        "postverified": local.decode(
            values["MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_RECOVER_RECEIPT_B64"]
        ),
        "sources": sources(local),
    }
    remote.validate_profile(profile)
    return profile


def verify_pins(profile, expected, values):
    remote = contract()
    if remote.digest(remote.canonical(profile)) != expected or profile != build_profile(values):
        raise ValueError("Perfil runtime distinto del SHA externo o del estado respaldado.")
    wanted = {
        "PROFILE_SHA256": expected,
        "PROFILE_B64": base64.b64encode(remote.canonical(profile)).decode(),
        **{name.upper() + "_SHA256": value for name, value in profile["sources"].items()},
    }
    if any(values.get(PREFIX + key) != value for key, value in wanted.items()):
        raise ValueError("El perfil y las tres fuentes deben respaldarse antes de prepare/build.")


def request(client, mode, profile, prepared=None):
    local, remote = sdk_local(), contract()
    source = local.read_source(HERE / "final_image_remote.py")
    sdk = local.read_source(HERE / "offline_sdk_remote_v4.py")
    compile_source = local.read_source(HERE / "compile_remote_v3.py")
    postverify_source = local.read_source(HERE / "postverify_recover_remote.py")
    layout = local.read_source(ROOT / "docker/host_layout.py")
    dockerfile = local.read_source(HERE / "Dockerfile.runtime")
    if any(
        remote.digest(data) != expected
        for data, expected in (
            (source, profile["sources"]["remote"]),
            (sdk, remote.SDK_SOURCE),
            (compile_source, remote.COMPILE_SOURCE),
            (postverify_source, remote.POSTVERIFY_SOURCE),
            (layout, profile["sdk_plan"]["layout_helper_sha256"]),
            (dockerfile, profile["sources"]["dockerfile"]),
        )
    ):
        raise ValueError("Una fuente cambió antes de la transmisión.")
    payload = {
        "mode": mode,
        "profile": profile,
        "profile_sha256": remote.digest(remote.canonical(profile)),
        "prepared": prepared,
        "sdk_source_b64": base64.b64encode(sdk).decode(),
        "compile_source_b64": base64.b64encode(compile_source).decode(),
        "postverify_source_b64": base64.b64encode(postverify_source).decode(),
        "layout_source_b64": base64.b64encode(layout).decode(),
        "dockerfile_b64": base64.b64encode(dockerfile).decode(),
    }
    data = remote.canonical(payload)
    if len(data) > 768 * 1024:
        raise ValueError("La petición runtime excede su límite.")
    code = (
        "import base64,hashlib;d=base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + ");g={'__name__':'__main__','_RUNTIME_SOURCE_SHA256':hashlib.sha256(d).hexdigest()};exec(compile(d,'<runtime-fijado>','exec'),g)"
    )
    if len(code.encode()) > 128 * 1024:
        raise ValueError("El helper excede el límite argv Linux.")
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(3)
        channel.exec_command("sudo -n /usr/bin/python3 -B -c " + shlex.quote(code))
        channel.sendall(struct.pack(">I", len(data)) + data)
        channel.shutdown_write()
        return local.collect(channel, time.monotonic() + 610, 4 * 1024**2)
    finally:
        channel.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "prepare", "build"), nargs="?", default="plan")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        remote = contract()
        if args.mode == "plan" and args.output is None:
            # Plan consultable antes del PASS: no abre la bóveda, SSH o Docker.
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "proposal": remote.proposal(),
                        "sources": sources(sdk_local()),
                        "gates_required": [
                            "original-compile-failure",
                            "postverify-v3-pass",
                            "sdk-stop-backed",
                        ],
                        "infrastructure_executed": False,
                    }
                )
            )
            return 0
        local = sdk_local()
        values = read_values(local)
        if args.mode == "plan":
            profile = build_profile(values)
            directory = ROOT / ".tools/postgres-immich-build"
            if args.output.absolute().parent != directory or directory.resolve() != directory:
                raise ValueError(
                    "El perfil sólo puede publicarse en la raíz privada ya acreditada."
                )
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
            raise ValueError("Prepare/build exige CLI de raíz y perfil/SHA externo respaldados.")
        raw = local.read_source(args.profile, 512 * 1024)
        profile = remote.json_object(raw)
        if remote.canonical(profile) != raw:
            raise ValueError("El perfil debe ser canónico.")
        verify_pins(profile, args.expected_profile_sha256, values)
        prepared = local.decode(values.get(PREFIX + "PREPARED_B64"), optional=True)
        if args.mode == "build":
            remote.validate_prepared(
                prepared, args.expected_profile_sha256, profile["sources"]["remote"]
            )
        field = PREFIX + ("PREPARED_B64" if args.mode == "prepare" else "BUILT_B64")
        if values.get(field) not in (None, "", "CHANGE_ME"):
            raise ValueError("La etapa ya tiene recibo respaldado; no se repite ni sobrescribe.")
        client = local.connect(values)
        response = request(client, args.mode, profile, prepared)
        result = response.get("result", {})
        receipt = result.get("receipt", {})
        if (
            result != remote.envelope(receipt)
            or receipt.get("stage") != args.mode
            or receipt.get("profile_sha256") != args.expected_profile_sha256
            or receipt.get("source_sha256") != profile["sources"]["remote"]
            or type(receipt.get("success")) is not bool
        ):
            raise ValueError("El recibo runtime no acredita perfil, fuente y etapa.")
        encoded = base64.b64encode(remote.canonical(result)).decode()
        local.root_writer(profile["sdk_plan"])({field: encoded})
        if read_values(local).get(field) != encoded:
            raise ValueError("El recibo runtime no quedó respaldado durablemente.")
        print(
            json.dumps(
                {
                    "success": receipt["success"],
                    "stage": args.mode,
                    "receipt_sha256": result["receipt_sha256"],
                    "image_id": receipt.get("image_id"),
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
