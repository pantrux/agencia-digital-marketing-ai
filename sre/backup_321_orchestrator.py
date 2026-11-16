"""Copy one sealed, complete backup set to the NAS and R2 repositories."""

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path("/var/lib/media-platform")
EXECUTOR = Path(__file__).with_name("sre-backup-321.sh")
EXECUTOR_SHA = "c12b68e28bdf9d8b7ca70ec440eefcad2f487aeec45627b6735f6d9f09a19578"
MAX_STAGING_BYTES = 2 * 1024**3
MIN_JOURNAL_FREE_BYTES = 64 * 1024**2
FILES = ("postgres-apps.sql.zst", "postgres-immich.sql.zst", "d1.sql.zst", "openclaw.tar.zst")
D1_FILES = ("d1.sql.zst",)
OPENCLAW_FILES = ("openclaw.tar.zst",)
SCOPE_FILES = {"complete": FILES, "d1-clock": D1_FILES, "openclaw-clock": OPENCLAW_FILES}
SCOPE_RESULT = {
    "complete": "backup_set_complete",
    "d1-clock": "d1_backup_complete",
    "openclaw-clock": "openclaw_backup_complete",
}
STAGING_FILES = {*FILES, "SHA256SUMS"}
TARGETS = ("nas", "r2")
SHA256 = re.compile(r"[a-f0-9]{64}")
COMMAND_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def private(path, modes, *, directory=False):
    info = path.lstat()
    allowed_type = (
        stat.S_ISDIR(info.st_mode)
        if directory
        else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    )
    require(
        allowed_type
        and (info.st_uid, info.st_gid) == (0, 0)
        and stat.S_IMODE(info.st_mode) in modes,
        "IDENTIDAD_PRIVADA",
    )
    return info


def private_directory(path):
    require(
        path.is_absolute() and path.is_relative_to(ROOT) and path.resolve(strict=True) == path,
        "DIRECTORIO_STAGING",
    )
    for parent in (path, *path.parents):
        private(parent, (0o700,), directory=True)
        if parent == ROOT:
            return


def read_verified(path, *, maximum=None, modes=(0o600,)):
    expected = identity(private(path, modes))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        require(identity(os.fstat(fd)) == expected, "ARCHIVO_CAMBIO")
        if maximum is None:
            result = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            result = stream.read(maximum + 1)
            require(0 < len(result) <= maximum and len(result) == expected[6], "ARCHIVO_COTA")
        require(identity(os.fstat(fd)) == identity(path.lstat()) == expected, "ARCHIVO_CAMBIO")
    return result, expected


def check_staging(staging, manifest_pin, files=FILES):
    private_directory(staging)
    require(staging != ROOT, "STAGING_RAIZ")
    names = {path.name for path in staging.iterdir()}
    if files == FILES:
        require("openclaw.tar.zst" in names, "OPENCLAW_FALTANTE")
    require(names == {*files, "SHA256SUMS"}, "STAGING_INCOMPLETO_O_CON_EXTRAS")
    require(
        sum(private(staging / name, (0o600,)).st_size for name in files) <= MAX_STAGING_BYTES,
        "STAGING_COTA",
    )
    manifest, manifest_identity = read_verified(staging / "SHA256SUMS", maximum=1024)
    require(hashlib.sha256(manifest).hexdigest() == manifest_pin, "SHA_MANIFIESTO")
    hashes = {}
    for row in manifest.splitlines(keepends=True):
        match = re.fullmatch(rb"([a-f0-9]{64})  ([a-z0-9.-]+)\n", row)
        require(match is not None, "MANIFIESTO_INVALIDO")
        digest, name = (part.decode("ascii") for part in match.groups())
        require(name in files and name not in hashes, "MANIFIESTO_NO_EXACTO")
        hashes[name] = digest
    require(set(hashes) == set(files), "MANIFIESTO_INCOMPLETO")
    identities = {"SHA256SUMS": manifest_identity}
    for name, pin in hashes.items():
        digest, info = read_verified(staging / name)
        require(info[6] > 0 and digest == pin, "SHA_STAGING")
        identities[name] = info
    check_unchanged(staging, identities)
    return identities


def check_unchanged(staging, identities):
    private_directory(staging)
    require(
        {path.name for path in staging.iterdir()} == set(identities)
        and all(identity((staging / name).lstat()) == info for name, info in identities.items()),
        "STAGING_CAMBIO",
    )


def request_settings(request, action):
    expected = {"staging", "manifest_sha256"}
    if action == "run":
        expected |= {"run_id", "journal", "restic", "restic_sha256", "targets", "timeout_seconds"}
    require(
        isinstance(request, dict)
        and set(request) == expected
        and isinstance(request["staging"], str)
        and isinstance(request["manifest_sha256"], str)
        and SHA256.fullmatch(request["manifest_sha256"]),
        "SOLICITUD",
    )
    if action == "plan":
        return
    require(
        all(isinstance(request[k], str) for k in ("run_id", "journal", "restic", "restic_sha256"))
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", request["run_id"])
        and Path(request["restic"]).is_absolute()
        and SHA256.fullmatch(request["restic_sha256"])
        and type(request["timeout_seconds"]) is int
        and 30 <= request["timeout_seconds"] <= 3600
        and isinstance(request["targets"], dict)
        and set(request["targets"]) == set(TARGETS),
        "SOLICITUD_RUN",
    )
    for name, target in request["targets"].items():
        require(
            isinstance(target, dict)
            and set(target) == {"repository", "id", "password_file"}
            and all(isinstance(value, str) for value in target.values())
            and SHA256.fullmatch(target["id"])
            and Path(target["password_file"]).is_absolute(),
            "SOLICITUD_DESTINO",
        )
        pattern = (
            r"/mnt/producciones/_backups(/[A-Za-z0-9][A-Za-z0-9._-]*)+"
            if name == "nas"
            else r"s3:https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com/studio-backups-bucket(/[A-Za-z0-9][A-Za-z0-9._-]*)+"
        )
        require(re.fullmatch(pattern, target["repository"]), "REPOSITORIO")
    require(
        request["targets"]["nas"]["id"] != request["targets"]["r2"]["id"], "REPOSITORIOS_DISTINTOS"
    )


def write_record(path, value):
    with path.open("xb") as output:
        output.write(encode(value) + b"\n")
        output.flush()
        os.fsync(output.fileno())
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def exclusive():
    import fcntl

    private_directory(ROOT)
    lock = ROOT / ".agency-backup-321.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        require(identity(os.fstat(fd)) == identity(private(lock, (0o600,))), "LOCK_IDENTIDAD")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("CORRIDA_EN_CURSO") from error
        yield
    finally:
        os.close(fd)


def output_code(error):
    return (
        str(error)
        if type(error) is ValueError and re.fullmatch(r"[A-Z0-9_]+", str(error))
        else type(error).__name__
    )


def terminate_group(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def invoke(journal, name, arguments, script, env, timeout, *, prefix=()):
    path = journal / f"{name}.stdout"
    with path.open("xb") as output, (journal / f"{name}.stderr").open("xb") as errors:
        child = subprocess.Popen(
            [*prefix, "/usr/bin/bash", "/dev/stdin", *arguments],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=errors,
            env=env,
            start_new_session=True,
        )
        try:
            child.communicate(script, timeout=timeout)
        except BaseException:
            terminate_group(child)
            raise
    return child.returncode, path


def execute_target(request, journal, name, staging, script, env, scope):
    target = request["targets"][name]
    child_receipts = journal / name
    binding = {"repository_id": target["id"], "manifest_sha256": request["manifest_sha256"]}
    if scope != "complete":
        binding["scope"] = scope
    write_record(journal / f"{name}.intent.json", binding)
    result = {"success": False, "reconciliation_required": True}
    arguments = [
        name,
        str(staging),
        request["run_id"],
        request["restic"],
        request["restic_sha256"],
        target["repository"],
        target["id"],
        str(child_receipts),
    ]
    if scope != "complete":
        arguments.append(f"--{scope}")
    try:
        exit_code, stdout_path = invoke(
            journal, name, arguments, script, env, request["timeout_seconds"]
        )
        result["exit_code"] = exit_code
        raw, _ = read_verified(stdout_path, maximum=16384)
        child = json.loads(raw)
        require(
            isinstance(child, dict)
            and child.get("destination") == name
            and child.get("run_id") == request["run_id"]
            and all(child.get(key) == value for key, value in binding.items()),
            "RECIBO_DESTINO",
        )
        snapshot = child.get("snapshot_id")
        if isinstance(snapshot, str) and SHA256.fullmatch(snapshot):
            result["snapshot_id"] = snapshot
        if exit_code == 0 and child.get("success") is True:
            require(
                "snapshot_id" in result
                and child.get("backup_attempted") is True
                and child.get("reconciliation_required") is False,
                "RECIBO_SNAPSHOT",
            )
            saved, _ = read_verified(child_receipts / "result.json", maximum=16384)
            require(json.loads(saved) == child, "RECIBO_PERSISTIDO")
            result.update(
                success=True,
                reconciliation_required=False,
                receipt_sha256=hashlib.sha256(saved).hexdigest(),
            )
        else:
            result["reconciliation_required"] = child.get("reconciliation_required") is not False
            if isinstance(child.get("error_code"), str) and re.fullmatch(
                r"[A-Z0-9_]+", child["error_code"]
            ):
                result["error"] = child["error_code"]
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        result["error"] = output_code(error)
    write_record(journal / f"{name}.result.json", result)
    return result


def credentials(request):
    output = {}
    for name, target in request["targets"].items():
        password = Path(target["password_file"])
        require(password.resolve(strict=True) == password, "PASSFILE_RUTA")
        require(not password.is_relative_to(Path(request["staging"])), "PASSFILE_EN_STAGING")
        private(password, (0o400, 0o600))
        env = {**COMMAND_ENV, "RESTIC_PASSWORD_FILE": str(password)}
        if name == "nas":
            proof = os.environ.get("BACKUP_NAS_DSM_RECEIPT_FILE")
            if proof:
                env["BACKUP_NAS_DSM_RECEIPT_FILE"] = proof
        else:
            for key in ("R2_PLATFORM_ACCESS_KEY_ID", "R2_PLATFORM_SECRET_ACCESS_KEY"):
                value = os.environ.get(key, "")
                require(
                    re.fullmatch(r"[!-~]{1,256}", value) and value != "CHANGE_ME", "CREDENCIAL_R2"
                )
                env[key] = value
        output[name] = env
    return output


def run_backup(request, out, *, scope="complete"):
    require(scope in SCOPE_FILES, "ALCANCE_RESPALDO")
    staging = Path(request["staging"])
    journal_root = Path(request["journal"])
    private_directory(journal_root)
    require(not journal_root.is_relative_to(staging), "JOURNAL_EN_STAGING")
    with exclusive():
        stage_identity = check_staging(staging, request["manifest_sha256"], SCOPE_FILES[scope])
        disk = os.statvfs(journal_root)
        require(disk.f_frsize * disk.f_bavail >= MIN_JOURNAL_FREE_BYTES, "DISCO_RECIBOS")
        script, _ = read_verified(
            EXECUTOR.absolute(),
            maximum=32768,
            modes=(0o400, 0o444, 0o500, 0o555, 0o600, 0o644, 0o700, 0o755),
        )
        require(hashlib.sha256(script).hexdigest() == EXECUTOR_SHA, "EJECUTOR_SHA")
        environments = credentials(request)
        restic = Path(request["restic"])
        require(restic.resolve(strict=True) == restic, "RESTIC_RUTA")
        restic_pin, _ = read_verified(restic, modes=(0o500, 0o555, 0o700, 0o755))
        require(restic_pin == request["restic_sha256"], "SHA_RESTIC")
        journal = journal_root / request["run_id"]
        journal.mkdir(mode=0o700)
        sync_directory(journal_root)
        out["run_id"] = request["run_id"]
        out["manifest_sha256"] = request["manifest_sha256"]
        write_record(
            journal / "intent.json",
            {
                **out,
                "staging": str(staging),
                "executor_sha256": EXECUTOR_SHA,
                "targets": {name: request["targets"][name]["id"] for name in TARGETS},
            },
        )
        out["destinations"] = {}
        try:
            for name in TARGETS:
                check_unchanged(staging, stage_identity)
                out["pending_destination"] = name
                result = execute_target(
                    request, journal, name, staging, script, environments[name], scope
                )
                out["destinations"][name] = result
                del out["pending_destination"]
                require(result["success"], "DESTINO_FALLO")
            check_unchanged(staging, stage_identity)
            out.update(success=True, backup_set_complete=scope == "complete")
            out[SCOPE_RESULT[scope]] = True
        except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            out["error"] = output_code(error)
        write_record(journal / "result.json", out)


def interrupted(_signal, _frame):
    raise ValueError("CORRIDA_INTERRUMPIDA")


def unique_fields(pairs):
    fields = dict(pairs)
    require(len(fields) == len(pairs), "SOLICITUD_CLAVE_REPETIDA")
    return fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run"))
    args = parser.parse_args()
    out = {
        "success": False,
        "action": args.action,
        "backup_set_complete": False,
        "restoration_verified": False,
    }
    try:
        require(
            os.uname().sysname == "Linux"
            and os.uname().nodename == "vm-media-01"
            and os.geteuid() == os.getegid() == 0,
            "HOST_O_IDENTIDAD",
        )
        os.umask(0o077)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        raw = sys.stdin.buffer.read(32769)
        require(0 < len(raw) <= 32768, "SOLICITUD_COTA")
        request = json.loads(raw, object_pairs_hook=unique_fields)
        request_settings(request, args.action)
        if args.action == "plan":
            check_staging(Path(request["staging"]), request["manifest_sha256"])
            out.update(success=True, manifest_sha256=request["manifest_sha256"], files=list(FILES))
        else:
            run_backup(request, out)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        out.update(success=False, backup_set_complete=False, error=output_code(error))
    if out.get("error") == "OPENCLAW_FALTANTE":
        out["missing"] = ["openclaw.tar.zst"]
    print(encode(out).decode(), flush=True)
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
