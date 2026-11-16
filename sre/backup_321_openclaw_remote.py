"""Proyecta los cinco agentes en openclaw-ia y entrega un único archivo por SSH."""

import hashlib
import importlib.util
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ROOT = Path("/home/jandrade/.openclaw-agency-backup/capture-321-v1")
CONFIG = PurePosixPath("/home/jandrade/.openclaw/openclaw.json")
INVENTORY = "memory-inventory.json"
HOST = "openclaw-ia"
USER = "jandrade"
COMMAND = "agency-backup-321-daily"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SOURCES = {
    "openclaw_backup": "59cff68674767edb5b68e940a70a19424dc8b23d107e04de26772aa2af9948fe",
    "prepare_openclaw_staging": "f8d7614e8f02f853ffa25d35a46d662ac8255b8bb0d3cbda16bd3afeb221ee7e",
}


def read_source(root_fd, name, pin):
    fd = os.open(name + ".py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or not 0 < info.st_size <= 65536
        ):
            raise ValueError("OPENCLAW_FUENTE_REMOTA")
        raw = stream.read(65537)
    if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError("OPENCLAW_FUENTE_REMOTA")
    path = ROOT / (name + ".py")
    module = importlib.util.module_from_spec(importlib.util.spec_from_file_location(name, path))
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def capture(backup, prepare, root_fd, run_id):
    import pwd

    started = datetime.now(UTC)
    try:
        raw = backup.read_file(root_fd, INVENTORY, backup.MAX_MANIFEST)
    except FileNotFoundError:
        raw = None
    config = prepare.read_source(CONFIG, prepare.MAX_CONFIG)
    entries = prepare.config_entries(config)
    home = PurePosixPath(pwd.getpwuid(os.geteuid()).pw_dir)
    if raw is None:
        inventory = prepare.discover_memory(entries, home)
        read_memory = prepare.read_workspace_memory
    else:
        inventory = prepare.load_json(raw)
        read_memory = prepare.read_memory
    manifest, files = prepare.project(entries, inventory, "agency-state", home, read_memory)
    manifest_sha256 = backup.digest(manifest)
    with tempfile.TemporaryDirectory(prefix=".daily-", dir=ROOT) as private:
        private = Path(private)
        backup.publish(private, "staging", {backup.MANIFEST: manifest, **files})
        manifest, files = backup.read_staging(private, "staging", manifest_sha256, "agency-state")
    archive = backup.pack_tar(manifest, files, "agency-state")
    frame = backup.zstd(archive)
    backup.require(backup.zstd(frame, decompress=True) == archive, "OPENCLAW_VERIFICACION_REMOTA")
    completed = datetime.now(UTC)
    receipt = {
        "success": True,
        "action": "capture",
        "scope": "agency-state",
        "synthetic": False,
        "source_attestation": "external-staging-manifest",
        "agents": len(backup.AGENTS),
        "files": len(files),
        "manifest_sha256": manifest_sha256,
        "archive_name": backup.ARCHIVE,
        "archive_sha256": backup.digest(frame),
        "archive_size": len(frame),
    }
    return backup.canonical(
        {
            "protocol": 1,
            "hostname": HOST,
            "run_id": run_id,
            "started_at_utc": started.isoformat(),
            "completed_at_utc": completed.isoformat(),
            "capture": receipt,
        }
    ), frame


def main():
    import pwd

    os.umask(0o077)
    try:
        if (
            sys.platform != "linux"
            or os.geteuid() == 0
            or pwd.getpwuid(os.geteuid()).pw_name != USER
            or socket.gethostname() != HOST
            or os.environ.get("SSH_ORIGINAL_COMMAND") != COMMAND
        ):
            raise ValueError("OPENCLAW_ORIGEN_REMOTO")
        # Abre el árbol sin enlaces antes de ejecutar las fuentes fijadas por SHA-256.
        with ExitStack() as stack:
            fd = None
            for component in ROOT.parts:
                fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            info = os.fstat(fd)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (
                os.geteuid(),
                os.getegid(),
                0o700,
            ):
                raise ValueError("OPENCLAW_RAIZ_REMOTA")
            backup = read_source(fd, "openclaw_backup", SOURCES["openclaw_backup"])
            prepare = read_source(
                fd, "prepare_openclaw_staging", SOURCES["prepare_openclaw_staging"]
            )
            raw = sys.stdin.buffer.read(1025)
            backup.require(len(raw) <= 1024, "OPENCLAW_SOLICITUD_REMOTA")
            request = prepare.load_json(raw)
            backup.require(
                set(request) == {"run_id"}
                and isinstance(request["run_id"], str)
                and RUN_ID.fullmatch(request["run_id"]),
                "OPENCLAW_SOLICITUD_REMOTA",
            )
            header, frame = capture(backup, prepare, fd, request["run_id"])
        sys.stdout.buffer.write(header)
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
    except (OSError, ValueError, KeyError, RecursionError, subprocess.SubprocessError):
        print('{"success":false,"error":"OPENCLAW_REMOTE_REJECTED"}', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
