"""Captura, sella y copia una vez por fecha de Santiago en vm-media-01."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__:
    from . import backup_321_orchestrator as core
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_orchestrator as core


DEFAULT_CONFIG = Path("/var/lib/media-platform/backup-321-daily-config-v1")
RUNTIME = Path(__file__).resolve().parent
PYTHON = "/usr/bin/python3"
ROOT_NAMES = ("daily", "postgres", "d1", "openclaw", "staging", "journal")
PG = ("postgres-apps", "postgres-immich")
TASKS = {
    "postgres": ("backup_321_capture_postgres.py", "capture", 900),
    "d1": ("backup_321_capture_d1.py", "capture", 360),
    "openclaw": ("backup_321_openclaw_transport.py", None, 1200),
    "assemble": ("backup_321_staging.py", "assemble", 600),
    "plan": ("backup_321_orchestrator.py", "plan", 120),
    "run": ("backup_321_orchestrator.py", "run", None),
}
PINS = {script for script, _, _ in TASKS.values()} | {
    "capture-postgres-backup.sh",
    "capture-immich-backup.sh",
    "openclaw_backup.py",
    "d1_export.py",
    "prepare_openclaw_staging.py",
    "sre-backup-321.sh",
}
RETENTION_SCRIPT = "backup_321_daily_retention.py"
RETENTION_PINS = {
    RETENTION_SCRIPT: "7bec2fc7dc0344685e4b432c5dbf694b92a6bc0c2e955c8307521e3398eec8d8",
    "backup_321_retention.py": "cfcb6be1311b874f632409ba43fdbd3a8667c61468d9827b93a1cdddaecbb956",
    "backup_321_verify_r2_restore.py": "eae8368634da8518beb1c585c586d483b529f46337a56666239aaeb741b0f025",
}
RETENTION_TIMEOUT_SECONDS = 2700
READ_MODES = (0o400, 0o444, 0o500, 0o555, 0o600, 0o644, 0o700, 0o755)
FILE_LIMIT = 32768
R2_FIELDS = {
    "access_key_id_file": "R2_PLATFORM_ACCESS_KEY_ID",
    "secret_access_key_file": "R2_PLATFORM_SECRET_ACCESS_KEY",
}
BOOTSTRAP = (
    "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
    'runpy.run_path(sys.argv.pop(1),run_name="__main__")'
)
OPENCLAW_FIELDS = {
    "receipt",
    "sha256",
    "manifest_sha256",
    "hostname",
    "started_at_utc",
    "completed_at_utc",
}
PROFILE_FIELDS = {
    "schema",
    "roots",
    "pins",
    "sources",
    "d1",
    "openclaw",
    "restic",
    "restic_sha256",
    "targets",
    "r2",
    "nas_dsm_receipt_file",
    "timeout_seconds",
    "vendor_manifest_sha256",
}
sync_directory = core.sync_directory


def daily_id(now):
    try:
        zone = ZoneInfo("America/Santiago")
    except ZoneInfoNotFoundError as error:
        raise ValueError("ZONA_SANTIAGO") from error
    return "daily-" + now.astimezone(zone).date().isoformat()


def fields(value, keys, code):
    core.require(isinstance(value, dict) and set(value) == set(keys), code)


def pin(value):
    return isinstance(value, str) and core.SHA256.fullmatch(value) is not None


def document(raw):
    value = json.loads(raw, object_pairs_hook=core.unique_fields)
    core.require(isinstance(value, dict), "RECIBO_JSON")
    return value


def read_json(path, modes=(0o600,)):
    raw, _ = core.read_verified(path, maximum=FILE_LIMIT, modes=modes)
    return document(raw), hashlib.sha256(raw).hexdigest()


def verified_pin(path, expected, modes=READ_MODES):
    core.require(pin(expected), "PIN_FORMATO")
    core.require(path.resolve(strict=True) == path, "ARCHIVO_RUTA")
    digest, _ = core.read_verified(path, modes=modes)
    core.require(digest == expected, "ARCHIVO_SHA")


def trusted_install_dir(path):
    core.require(path.is_absolute() and path.resolve(strict=True) == path, "RUNTIME_RUTA")
    for parent in (path, *path.parents):
        core.private(parent, (0o700, 0o750, 0o755), directory=True)


def verified_runtime(profile):
    trusted_install_dir(RUNTIME)
    fields(profile["pins"], PINS, "PERFIL_PINES")
    for basename, digest in profile["pins"].items():
        verified_pin(RUNTIME / basename, digest)
    vendor = RUNTIME / "vendor"
    manifest = vendor / "SHA256SUMS"
    raw, _ = core.read_verified(manifest, maximum=1024**2, modes=READ_MODES)
    core.require(hashlib.sha256(raw).hexdigest() == profile["vendor_manifest_sha256"], "VENDOR_SHA")
    names = []
    for row in raw.splitlines(keepends=True):
        match = re.fullmatch(r"([a-f0-9]{64})  ([^\r\n\\]+)\n", row.decode("utf-8"))
        core.require(match is not None, "VENDOR_MANIFIESTO")
        digest, name = match.groups()
        relative = PurePosixPath(name)
        core.require(
            not relative.is_absolute()
            and ".." not in relative.parts
            and name == relative.as_posix()
            and name != "SHA256SUMS",
            "VENDOR_RUTA",
        )
        names.append(name)
        verified_pin(vendor / name, digest)
    seen = set()
    for directory, children, files in os.walk(vendor, followlinks=False):
        root = Path(directory)
        trusted_install_dir(root)
        for child in children:
            core.private(root / child, (0o700, 0o750, 0o755), directory=True)
        seen.update((root / name).relative_to(vendor).as_posix() for name in files)
    core.require(
        names == sorted(set(names)) and set(names) | {"SHA256SUMS"} == seen, "VENDOR_INVENTARIO"
    )
    return vendor


def secret_file(value, roots):
    path = Path(value)
    core.private_directory(path.parent)
    core.require(not any(path.is_relative_to(root) for root in roots), "CREDENCIAL_EN_STAGE")
    core.private(path, (0o400, 0o600))
    return path


def r2_environment(profile, roots):
    env = {}
    fields(profile["r2"], R2_FIELDS, "PERFIL_R2")
    for key, name in R2_FIELDS.items():
        path = secret_file(profile["r2"][key], roots)
        raw, _ = core.read_verified(path, maximum=258, modes=(0o400, 0o600))
        token = raw.removesuffix(b"\r\n") if raw.endswith(b"\r\n") else raw.removesuffix(b"\n")
        core.require(
            re.fullmatch(rb"[!-~]{1,256}", token) and token != b"CHANGE_ME", "CREDENCIAL_R2"
        )
        env[name] = token.decode("ascii")
    proof = secret_file(profile["nas_dsm_receipt_file"], roots)
    env["BACKUP_NAS_DSM_RECEIPT_FILE"] = str(proof)
    return env


def preflight(profile):
    fields(profile, PROFILE_FIELDS, "PERFIL_CAMPOS")
    core.require(type(profile["schema"]) is int and profile["schema"] == 1, "PERFIL_VERSION")
    fields(profile["roots"], ROOT_NAMES, "PERFIL_RAICES")
    roots = [Path(profile["roots"][name]) for name in ROOT_NAMES]
    for root in roots:
        core.private_directory(root)
        core.require(sum(root.is_relative_to(other) for other in roots) == 1, "RAICES_SOLAPADAS")
    vendor = verified_runtime(profile)
    sys.path.insert(0, str(vendor))
    sys.path.insert(0, str(RUNTIME))
    import httpx
    from backup_321_capture_d1 import check_tools
    from backup_321_capture_d1 import validate_request as validate_d1
    from backup_321_capture_postgres import observed_container
    from backup_321_capture_postgres import validate_request as validate_pg
    from backup_321_openclaw_transport import INGRESS
    from backup_321_openclaw_transport import load_profile as check_transport_profile

    core.require(
        Path(httpx.__file__).resolve().is_relative_to(vendor) and httpx.__version__ == "0.28.1",
        "VENDOR_HTTPX",
    )
    fields(profile["sources"], PG, "PERFIL_FUENTES")
    fields(profile["d1"], {"account_id", "database_id", "token_file"}, "PERFIL_D1")
    validate_pg(postgres_request(profile, "preflight"))
    validate_d1(d1_request(profile, "preflight"))
    check_tools()
    fields(profile["openclaw"], {"profile", "profile_sha256"}, "PERFIL_OPENCLAW")
    core.require(Path(profile["roots"]["openclaw"]) == INGRESS, "PERFIL_OPENCLAW_INGRESO")
    oc_profile = secret_file(profile["openclaw"]["profile"], roots)
    check_transport_profile(str(oc_profile), profile["openclaw"]["profile_sha256"])
    secret_file(profile["d1"]["token_file"], roots)
    verified_pin(Path(profile["restic"]), profile["restic_sha256"], (0o500, 0o555, 0o700, 0o755))
    backup = backup_request(
        profile, "preflight", Path(profile["roots"]["staging"]) / "preflight", "0" * 64
    )
    core.request_settings(backup, "run")
    for target in profile["targets"].values():
        secret_file(target["password_file"], roots)
    env = r2_environment(profile, roots)
    for name, expected in profile["sources"].items():
        observed_container(name, expected, "production")
    return vendor, env


def load_profile(path, pin_file):
    for file in (path, pin_file):
        core.private_directory(file.parent)
    raw, _ = core.read_verified(pin_file, maximum=65)
    core.require(re.fullmatch(rb"[a-f0-9]{64}\n?", raw), "PERFIL_PIN_FORMATO")
    profile, digest = read_json(path)
    core.require(digest.encode() == raw.rstrip(b"\n"), "PERFIL_SHA")
    return profile, digest


@contextmanager
def daily_lock(root):
    import fcntl

    path = root / ".daily.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        core.require(
            core.identity(os.fstat(fd)) == core.identity(core.private(path, (0o600,))),
            "LOCK_DIARIO",
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("CORRIDA_DIARIA_EN_CURSO") from error
        yield
    finally:
        os.close(fd)


def terminate(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=10)
    except ProcessLookupError:
        child.wait()
    except subprocess.TimeoutExpired:
        core.terminate_group(child)


def runner(profile, vendor, credentials):
    def invoke(name, request):
        script, action, timeout = TASKS[name]
        if name == "openclaw":
            oc = profile["openclaw"]
            command = [
                PYTHON,
                "-I",
                "-S",
                "-B",
                str(RUNTIME / script),
                "--profile",
                oc["profile"],
                "--profile-sha256",
                oc["profile_sha256"],
            ]
        else:
            command = [
                PYTHON,
                "-I",
                "-B",
                "-c",
                BOOTSTRAP,
                str(vendor),
                str(RUNTIME / script),
                action,
            ]
        env = {**core.COMMAND_ENV, **credentials} if name == "run" else core.COMMAND_ENV
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if name == "openclaw" else subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        ) as child:
            try:
                raw, errors = child.communicate(
                    core.encode(request), timeout=timeout or profile["timeout_seconds"] * 2 + 90
                )
            except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
                terminate(child)
                raise
        if child.returncode != 0:
            code = "ETAPA_FALLO"
            failure = raw or errors
            if failure and len(failure) <= FILE_LIMIT:
                try:
                    candidate = document(failure).get("error")
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", candidate):
                        code = candidate
                except ValueError:
                    pass
            raise ValueError(code)
        core.require(0 < len(raw) <= FILE_LIMIT, "RECIBO_COTA")
        return document(raw)

    return invoke


def postgres_request(profile, run_id):
    return {
        "run_id": run_id,
        "root": profile["roots"]["postgres"],
        "scope": "production",
        **profile["sources"],
    }


def d1_request(profile, run_id):
    return {"run_id": run_id, "root": profile["roots"]["d1"], **profile["d1"]}


def backup_request(profile, run_id, staging, manifest):
    return {
        "run_id": run_id,
        "staging": str(staging),
        "manifest_sha256": manifest,
        "journal": profile["roots"]["journal"],
        "restic": profile["restic"],
        "restic_sha256": profile["restic_sha256"],
        "targets": profile["targets"],
        "timeout_seconds": profile["timeout_seconds"],
    }


def verify_saved_result(path, row):
    core.private_directory(path.parent)
    saved, digest = read_json(path)
    core.require(saved == row and saved.get("success") is True, "RECIBO_PERSISTIDO")
    return digest


def verify_openclaw(path, row, started):
    fields(row, OPENCLAW_FIELDS, "OPENCLAW_RECIBO")
    core.require(row["receipt"] == str(path), "OPENCLAW_RECIBO_RUTA")
    core.require(
        row["hostname"] == "openclaw-ia" and pin(row["sha256"]) and pin(row["manifest_sha256"]),
        "OPENCLAW_RECIBO_IDENTIDAD",
    )
    begin = datetime.fromisoformat(row["started_at_utc"])
    end = datetime.fromisoformat(row["completed_at_utc"])
    core.require(
        begin.utcoffset() == end.utcoffset() == timedelta(0)
        and started - timedelta(seconds=60) <= begin <= end <= datetime.now(UTC),
        "OPENCLAW_VENTANA",
    )
    core.private_directory(path.parent)
    saved, digest = read_json(path)
    core.require(
        digest == row["sha256"]
        and saved.get("success") is True
        and saved.get("manifest_sha256") == row["manifest_sha256"],
        "OPENCLAW_RECIBO_SHA",
    )
    return row


def run_daily(profile, run_id, invoke):
    roots = {name: Path(path) for name, path in profile["roots"].items()}
    day = roots["daily"] / run_id
    with daily_lock(roots["daily"]):
        core.require(
            not any(os.path.lexists(path / run_id) for path in roots.values()),
            "CORRIDA_DIARIA_EXISTENTE",
        )
        day.mkdir(mode=0o700)
        sync_directory(day.parent)
        started = datetime.now(UTC)
        result = {
            "run_id": run_id,
            "success": False,
            "backup_set_complete": False,
            "restoration_verified": False,
            "stages": {},
            "started_at_utc": started.isoformat(),
        }
        core.write_record(day / "intent.json", result)

        def call(name, request):
            result["pending_stage"] = name
            core.write_record(day / f"{name}.intent.json", {"run_id": run_id, "stage": name})
            return invoke(name, request)

        try:
            refs = {}
            for name, request in (
                ("postgres", postgres_request(profile, run_id)),
                ("d1", d1_request(profile, run_id)),
            ):
                row = call(name, request)
                parent = roots[name] / run_id
                core.require(
                    row.get("run_id") == run_id
                    and row.get("step") == "captured"
                    and row.get("run_directory") == str(parent),
                    "RECIBO_CAPTURA",
                )
                receipt = parent / "result.json"
                refs[name] = {"receipt": str(receipt), "sha256": verify_saved_result(receipt, row)}
                result["stages"][name] = refs[name]
            oc = call("openclaw", {"run_id": run_id})
            refs["openclaw"] = verify_openclaw(
                roots["openclaw"] / run_id / "capture.json", oc, started
            )
            result["stages"]["openclaw"] = refs["openclaw"]
            stage_parent = roots["staging"] / run_id
            stage = stage_parent / "staging"
            row = call(
                "assemble",
                {
                    "run_id": run_id,
                    "root": str(roots["staging"]),
                    **refs,
                    "sources": {**profile["sources"], "d1_account_id": profile["d1"]["account_id"]},
                },
            )
            manifest = row.get("manifest_sha256")
            core.require(
                row.get("run_id") == run_id
                and row.get("step") == "staged"
                and row.get("staging") == str(stage)
                and pin(manifest),
                "RECIBO_STAGING",
            )
            result["stages"]["assemble"] = {
                "manifest_sha256": manifest,
                "sha256": verify_saved_result(stage_parent / "result.json", row),
            }
            plan = call("plan", {"staging": str(stage), "manifest_sha256": manifest})
            core.require(
                plan.get("success") is True and plan.get("manifest_sha256") == manifest,
                "RECIBO_PLAN",
            )
            result["stages"]["plan"] = {"manifest_sha256": manifest}
            row = call("run", backup_request(profile, run_id, stage, manifest))
            core.require(
                row.get("run_id") == run_id
                and row.get("manifest_sha256") == manifest
                and row.get("backup_set_complete") is True
                and row.get("restoration_verified") is False,
                "RECIBO_COPIA",
            )
            destinations = row.get("destinations")
            fields(destinations, core.TARGETS, "RECIBO_DESTINOS")
            snapshots = {}
            for name, target in destinations.items():
                core.require(
                    target.get("success") is True
                    and pin(target.get("snapshot_id"))
                    and pin(target.get("receipt_sha256")),
                    "RECIBO_SNAPSHOT",
                )
                snapshots[name] = target["snapshot_id"]
            result["stages"]["run"] = {
                "snapshots": snapshots,
                "sha256": verify_saved_result(roots["journal"] / run_id / "result.json", row),
            }
            del result["pending_stage"]
            result.update(success=True, backup_set_complete=True)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            subprocess.SubprocessError,
            KeyboardInterrupt,
        ) as error:
            result["error"] = core.output_code(error)
        result["completed_at_utc"] = datetime.now(UTC).isoformat()
        core.write_record(day / "result.json", result)
        return result


def run_retention(run_id, mode, profile_path, profile_pin):
    command = [
        PYTHON,
        "-I",
        "-B",
        str(RUNTIME / RETENTION_SCRIPT),
        "--run-id",
        run_id,
        "--mode",
        mode,
        "--profile",
        str(profile_path),
        "--profile-pin",
        str(profile_pin),
    ]
    try:
        for basename, expected in RETENTION_PINS.items():
            verified_pin(RUNTIME / basename, expected)
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=core.COMMAND_ENV,
            start_new_session=True,
        ) as child:
            try:
                raw, _ = child.communicate(timeout=RETENTION_TIMEOUT_SECONDS)
            except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
                terminate(child)
                raise
        core.require(0 < len(raw) <= FILE_LIMIT, "RETENCION_RECIBO_COTA")
        result = document(raw)
        core.require(result.get("success") is (child.returncode == 0), "RETENCION_RECIBO_ESTADO")
        return result
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        return {"success": False, "error": core.output_code(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_CONFIG / "profile.json")
    parser.add_argument("--profile-pin", type=Path, default=DEFAULT_CONFIG / "profile.sha256")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--retention", choices=("off", "audit", "apply"), default="off")
    args = parser.parse_args()
    try:
        host = os.uname() if hasattr(os, "uname") else None
        core.require(
            host is not None
            and host.sysname == "Linux"
            and host.nodename == "vm-media-01"
            and os.geteuid() == os.getegid() == 0,
            "HOST_O_IDENTIDAD",
        )
        os.umask(0o077)
        signal.signal(signal.SIGTERM, core.interrupted)
        signal.signal(signal.SIGHUP, core.interrupted)
        profile, digest = load_profile(args.profile, args.profile_pin)
        vendor, credentials = preflight(profile)
        if args.check and args.retention != "off":
            for basename, expected in RETENTION_PINS.items():
                verified_pin(RUNTIME / basename, expected)
        run_id = daily_id(datetime.now(UTC))
        result = {"success": True, "action": "check", "profile_sha256": digest, "run_id": run_id}
        if args.check and args.retention != "off":
            result["retention_mode"] = args.retention
        if not args.check:
            result = run_daily(profile, run_id, runner(profile, vendor, credentials))
            if result["success"] and result["backup_set_complete"] and args.retention != "off":
                result["retention"] = run_retention(
                    run_id, args.retention, args.profile, args.profile_pin
                )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        result = {"success": False, "backup_set_complete": False, "error": core.output_code(error)}
    print(core.encode(result).decode(), flush=True)
    return 0 if result["success"] and result.get("retention", {}).get("success", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
