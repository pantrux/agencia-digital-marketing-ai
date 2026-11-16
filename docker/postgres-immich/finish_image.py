"""Plan y finalización de imagen existente; importar no abre bóveda ni red."""

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
PREFIX = "MEDIA_POSTGRES_IMMICH_RUNTIME_FINISH_"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def contract():
    return load("finish_image_contract", HERE / "finish_image_remote.py")


def dependencies():
    remote = contract()
    path = HERE / "final_image.py"
    if remote.digest(path.read_bytes()) != remote.RUNTIME_LOCAL:
        raise ValueError("El CLI runtime histórico cambió.")
    runtime = load("finish_image_historic_local", path)
    sdk = runtime.sdk_local()
    if remote.digest(sdk.read_source(path)) != remote.RUNTIME_LOCAL:
        raise ValueError("El CLI runtime cambió durante su carga.")
    return runtime, sdk


def observations(sdk):
    remote = contract()
    raw = sdk.read_source(HERE / "finish-image-observations.json", 32768)
    value = sdk.contract().json_object(raw)
    if remote.canonical(value) != raw or remote.digest(raw) != remote.OBSERVATIONS_SHA:
        raise ValueError("Las observaciones no coinciden con el pin revisado.")
    return value


def sources(sdk):
    remote = contract()
    return {
        name: remote.digest(sdk.read_source(path))
        for name, path in {
            "local": Path(__file__),
            "remote": HERE / "finish_image_remote.py",
            "observations": HERE / "finish-image-observations.json",
        }.items()
    }


def build_profile(values, runtime, sdk):
    remote, obs = contract(), observations(sdk)
    historic_encoded = values["MEDIA_POSTGRES_IMMICH_RUNTIME_PROFILE_B64"]
    if len(historic_encoded) > 768 * 1024:
        raise ValueError("Perfil histórico excesivo.")
    historic_raw = base64.b64decode(historic_encoded, validate=True)
    if (
        remote.digest(historic_raw) != obs["historical_profile"]["sha256"]
        or values.get("MEDIA_POSTGRES_IMMICH_RUNTIME_PROFILE_SHA256")
        != obs["historical_profile"]["sha256"]
    ):
        raise ValueError("El perfil histórico no coincide con su respaldo durable.")
    historic = sdk.contract().json_object(historic_raw)
    runtime.contract().validate_profile(historic)
    prepared = sdk.decode(values["MEDIA_POSTGRES_IMMICH_RUNTIME_PREPARED_B64"])
    failed = sdk.decode(values["MEDIA_POSTGRES_IMMICH_RUNTIME_BUILT_B64"])
    for value, expected in (
        (prepared, obs["prepared_receipt_sha256"]),
        (failed, obs["failed_build_receipt_sha256"]),
    ):
        receipt = value.get("receipt", {})
        if value != remote.envelope(receipt) or value.get("receipt_sha256") != expected:
            raise ValueError("Prepare o fallo build no coinciden con los respaldos literales.")
    if (
        failed["receipt"].get("success") is not False
        or prepared["receipt"].get("success") is not True
        or values.get("MEDIA_POSTGRES_IMMICH_RUNTIME_OBSERVED_IMAGE_ID") != obs["image_id"]
        or values.get("MEDIA_POSTGRES_IMMICH_RUNTIME_OBSERVED_CONFIG_DIGEST")
        != obs["config_digest"]
    ):
        raise ValueError("Faltan datos observados/estados respaldados.")
    profile = {**remote.proposal(obs), "sources": sources(sdk)}
    remote.validate_profile(profile, obs)
    return profile


def verify_pins(profile, expected, values, runtime, sdk):
    remote = contract()
    if remote.digest(remote.canonical(profile)) != expected or profile != build_profile(
        values, runtime, sdk
    ):
        raise ValueError("El perfil de finalización no corresponde al SHA externo/estado actual.")
    wanted = {
        "PROFILE_SHA256": expected,
        "PROFILE_B64": base64.b64encode(remote.canonical(profile)).decode(),
        **{name.upper() + "_SHA256": value for name, value in profile["sources"].items()},
    }
    if any(values.get(PREFIX + key) != value for key, value in wanted.items()):
        raise ValueError("Perfil y fuentes deben respaldarse antes de apply.")


def request(client, profile, sdk):
    remote, obs = contract(), observations(sdk)
    source = sdk.read_source(HERE / "finish_image_remote.py")
    paths = {
        "runtime_source_b64": (HERE / "final_image_remote.py", remote.RUNTIME_SOURCE),
        "sdk_source_b64": (HERE / "offline_sdk_remote_v4.py", obs["sdk_remote_sha256"]),
        "compile_source_b64": (
            HERE / "compile_remote_v3.py",
            "e0e2bc46a65563e122a1469ac9ed5040836697748846e0408938e208e7049a3d",
        ),
        "postverify_source_b64": (
            HERE / "postverify_recover_remote.py",
            "fa397b9b4dd99300b5eb74a327fc7aa40194b91cbea923af2079f546635e6a32",
        ),
        "layout_source_b64": (ROOT / "docker/host_layout.py", obs["layout_helper_sha256"]),
    }
    if remote.digest(source) != profile["sources"]["remote"]:
        raise ValueError("El helper cambió antes de transmitir.")
    payload = {
        "profile": profile,
        "profile_sha256": remote.digest(remote.canonical(profile)),
        "observations": obs,
    }
    for name, (path, expected) in paths.items():
        raw = sdk.read_source(path)
        if remote.digest(raw) != expected:
            raise ValueError("Un helper congelado cambió antes de transmitir.")
        payload[name] = base64.b64encode(raw).decode()
    data = remote.canonical(payload)
    if len(data) > 768 * 1024:
        raise ValueError("Petición de finalización excesiva.")
    code = (
        "import base64,hashlib;d=base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + ");g={'__name__':'__main__','_FINISH_SOURCE_SHA256':hashlib.sha256(d).hexdigest()};exec(compile(d,'<finish-fijado>','exec'),g)"
    )
    if len(code.encode()) > 96 * 1024:
        raise ValueError("Helper excesivo para argv Linux.")
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(3)
        channel.exec_command("sudo -n /usr/bin/python3 -B -c " + shlex.quote(code))
        channel.sendall(struct.pack(">I", len(data)) + data)
        channel.shutdown_write()
        return sdk.collect(channel, time.monotonic() + 610, 4 * 1024**2)
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
        runtime, sdk = dependencies()
        obs = observations(sdk)
        if args.mode == "plan" and args.output is None:
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "proposal": remote.proposal(obs),
                        "sources": sources(sdk),
                        "infrastructure_executed": False,
                    }
                )
            )
            return 0
        values = runtime.read_values(sdk)
        if args.mode == "plan":
            profile = build_profile(values, runtime, sdk)
            directory = ROOT / ".tools/postgres-immich-build"
            if args.output.absolute().parent != directory or directory.resolve() != directory:
                raise ValueError("El perfil sólo puede escribirse en la raíz privada respaldada.")
            raw = remote.canonical(profile)
            with args.output.open("xb") as stream:
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
            raise ValueError("Apply exige CLI de raíz, perfil y SHA externo respaldados.")
        raw = sdk.read_source(args.profile, 128 * 1024)
        profile = sdk.contract().json_object(raw)
        if raw != remote.canonical(profile):
            raise ValueError("Perfil no canónico.")
        verify_pins(profile, args.expected_profile_sha256, values, runtime, sdk)
        field = PREFIX + "RECEIPT_B64"
        if values.get(field) not in (None, "", "CHANGE_ME"):
            raise ValueError("Hay una finalización previa respaldada; no se repite.")
        client = sdk.connect(values)
        response = request(client, profile, sdk)
        result = response.get("result", {})
        receipt = result.get("receipt", {})
        if (
            result != remote.envelope(receipt)
            or receipt.get("stage") != remote.STAGE
            or receipt.get("profile_sha256") != args.expected_profile_sha256
            or receipt.get("source_sha256") != profile["sources"]["remote"]
            or type(receipt.get("success")) is not bool
        ):
            raise ValueError("El recibo no acredita fuente/perfil/etapa de finalización.")
        # Recuperar el plan SDK ya respaldado, sin duplicarlo en el perfil nuevo.
        historic = sdk.contract().json_object(
            base64.b64decode(values["MEDIA_POSTGRES_IMMICH_RUNTIME_PROFILE_B64"], validate=True)
        )
        encoded = base64.b64encode(remote.canonical(result)).decode()
        sdk.root_writer(historic["sdk_plan"])({field: encoded})
        if runtime.read_values(sdk).get(field) != encoded:
            raise ValueError("El recibo no quedó respaldado durablemente.")
        print(
            json.dumps(
                {
                    "success": receipt["success"],
                    "receipt_sha256": result["receipt_sha256"],
                    "image_id": receipt.get("image_id"),
                    "build_reexecuted": False,
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
