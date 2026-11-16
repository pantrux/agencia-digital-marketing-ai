"""Plan y transiciones de raíz para reanudar un SDK propio, sin replay automático."""

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
PREFIX = "MEDIA_POSTGRES_IMMICH_RESUME_V1_"


def load(name, path, expected=None):
    if expected is not None and contract().digest(path.read_bytes()) != expected:
        raise ValueError("Fuente auxiliar distinta del SHA fijado.")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def contract():
    return load("resume_contract", HERE / "resume_sdk_remote.py")


def sdk():
    return load(
        "resume_sdk_v4",
        HERE / "run_offline_sdk_v4.py",
        "685bb8a54fc7d65fd25710b5ea317f8eda239b57aa2978a0ff5e33b8aabba608",
    )


def build_profile(values=None):
    remote, local = contract(), sdk()
    values = local.read_values() if values is None else values
    previous_raw = local.read_source(ROOT / ".tools/postgres-immich-build/compile-v1-profile.json")
    previous = json.loads(previous_raw)
    if (
        previous_raw != remote.canonical(previous)
        or remote.digest(previous_raw) != remote.PROFILE_V1_SHA
    ):
        raise ValueError("El perfil compile V1 cambió.")
    if local.Controller(values, previous["sdk_plan"]).saved() != previous["backed"]:
        raise ValueError("Los recibos SDK históricos cambiaron.")
    failed = json.loads(
        local.read_source(ROOT / ".tools/evidence/phase4-postgres-immich-compile-real-result.json")
    )
    if local.decode(values.get("MEDIA_POSTGRES_IMMICH_COMPILE_RECEIPT_B64")) != failed:
        raise ValueError("El fallo compile V1 debe estar respaldado antes de reanudar.")
    profile = {
        "schema_version": 1,
        "cid": remote.CID,
        "compile_profile": previous,
        "failure": failed,
        "sources": {
            "local": remote.digest(local.read_source(Path(__file__))),
            "remote": remote.digest(local.read_source(HERE / "resume_sdk_remote.py")),
        },
    }
    remote.validate_profile(profile)
    for filename, expected in (
        ("compile_local.py", previous["sources"]["local"]),
        ("compile_remote.py", remote.COMPILE_SHA),
        ("compile-recipe.sh", previous["sources"]["recipe"]),
    ):
        if remote.digest(local.read_source(HERE / filename)) != expected:
            raise ValueError("Una fuente compile V1 dejó de estar congelada.")
    return profile


def connect(values):
    return sdk().connect(values)


def request(profile, client, action, stopped=None):
    remote, local = contract(), sdk()
    source = local.read_source(HERE / "resume_sdk_remote.py")
    fixed = local.read_source(HERE / "offline_sdk_remote_v4.py")
    compiler = local.read_source(HERE / "compile_remote.py")
    layout = local.read_source(ROOT / "docker/host_layout.py")
    if (
        remote.digest(source) != profile["sources"]["remote"]
        or remote.digest(fixed)
        != "a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed"
        or remote.digest(compiler) != remote.COMPILE_SHA
        or remote.digest(layout) != profile["compile_profile"]["sdk_plan"]["layout_helper_sha256"]
    ):
        raise ValueError("Una fuente cambió antes de transmitir.")
    payload = {
        "action": action,
        "profile": profile,
        "profile_sha256": remote.digest(remote.canonical(profile)),
        "stopped": stopped,
        **{
            key: base64.b64encode(value).decode()
            for key, value in {
                "resume_source_b64": source,
                "sdk_source_b64": fixed,
                "compile_source_b64": compiler,
                "layout_source_b64": layout,
            }.items()
        },
    }
    data = remote.canonical(payload)
    if len(data) > 512 * 1024:
        raise ValueError("Frame excesivo.")
    code = (
        "import base64,hashlib;d=base64.b64decode("
        + repr(base64.b64encode(source).decode())
        + ");g={'__name__':'__main__','_RESUME_SOURCE_SHA256':hashlib.sha256(d).hexdigest()};exec(compile(d,'<resume-fijado>','exec'),g)"
    )
    if len(code.encode()) > 96 * 1024:
        raise ValueError("Fuente excesiva para argv Linux.")
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(3)
        channel.exec_command("sudo -n /usr/bin/python3 -B -c " + shlex.quote(code))
        channel.sendall(struct.pack(">I", len(data)) + data)
        return local.collect(
            channel,
            time.monotonic() + (270 if action == "restart" else 30),
            512 * 1024,
            heartbeat=True,
        )
    finally:
        channel.close()


def verify_pins(profile, expected, values):
    remote = contract()
    wanted = {
        "PROFILE_SHA256": expected,
        "PROFILE_B64": base64.b64encode(remote.canonical(profile)).decode(),
        **{name.upper() + "_SHA256": value for name, value in profile["sources"].items()},
    }
    if (
        remote.digest(remote.canonical(profile)) != expected
        or any(values.get(PREFIX + key) != value for key, value in wanted.items())
        or profile != build_profile(values)
    ):
        raise ValueError("El perfil y fuentes deben respaldarse antes de contactar la VM.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("plan", "observe-stopped", "restart"), nargs="?", default="plan"
    )
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
                    raise ValueError("Perfil fuera de la raíz privada acreditada.")
                with args.output.open("xb") as stream:
                    stream.write(data)
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "profile_sha256": remote.digest(data),
                        "sources": profile["sources"],
                        "ssh_executed": False,
                        "sdk_executed": False,
                    }
                )
            )
            return 0
        if not args.root_update_vault or not args.profile or not args.expected_profile_sha256:
            raise ValueError("La transición exige CLI de raíz, perfil y SHA respaldados.")
        local = sdk()
        raw = local.read_source(args.profile)
        profile = json.loads(raw)
        if raw != remote.canonical(profile):
            raise ValueError("Perfil no canónico.")
        values = local.read_values()
        verify_pins(profile, args.expected_profile_sha256, values)
        stopped = None
        if args.mode == "restart":
            stopped = local.decode(values.get(PREFIX + "STOPPED_B64"))
            if (
                not stopped
                or stopped["receipt"].get("success") is not True
                or stopped["receipt"].get("stage") != "observe-stopped"
                or stopped["receipt"].get("profile_sha256") != args.expected_profile_sha256
            ):
                raise ValueError("STOPPED_B64 debe respaldarse antes de restart.")
        client = connect(values)
        response = request(profile, client, args.mode, stopped)
        value = response.get("result", {})
        receipt = value.get("receipt", {})
        if (
            value != remote.envelope(receipt)
            or receipt.get("cid") != remote.CID
            or receipt.get("profile_sha256") != args.expected_profile_sha256
            or receipt.get("source_sha256") != profile["sources"]["remote"]
            or type(receipt.get("success")) is not bool
            or receipt.get("stage") != args.mode
        ):
            raise ValueError("Recibo sin identidad, etapa o fuente acreditadas.")
        key = PREFIX + ("STOPPED_B64" if args.mode == "observe-stopped" else "RESTART_B64")
        encoded = base64.b64encode(remote.canonical(value)).decode()
        if values.get(key) not in (None, "", "CHANGE_ME", encoded):
            raise ValueError("No se sobrescribe una transición anterior.")
        local.root_writer(profile["compile_profile"]["sdk_plan"])({key: encoded})
        if local.read_values().get(key) != encoded:
            raise ValueError("El recibo no quedó respaldado durablemente.")
        print(
            json.dumps(
                {
                    "success": receipt["success"],
                    "receipt_sha256": value["receipt_sha256"],
                    "stage": args.mode,
                    "sdk_executed": False,
                }
            )
        )
        return 0 if receipt["success"] else 1
    except BaseException as error:
        cleanup = None
        if client is not None and args.mode == "restart":
            try:
                cleanup = request(profile, client, "stop")
            except BaseException as failed:
                cleanup = {"stopped": False, "error_type": type(failed).__name__}
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "cleanup": cleanup,
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
