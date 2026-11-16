"""Recibe y verifica la captura de OpenClaw para la corrida diaria 3-2-1."""

import argparse
import base64
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__:
    from . import openclaw_backup as backup
    from . import prepare_openclaw_staging as prepare
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import openclaw_backup as backup
    import prepare_openclaw_staging as prepare


HOSTNAME = "openclaw-ia"
ADDRESS = "192.168.0.7"
USER = "jandrade"
PORT = 22
COMMAND = "agency-backup-321-daily"
INGRESS = Path("/var/lib/media-platform/backup-321-openclaw-ingress-v1")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
FILE_PATH = re.compile(r"/[a-zA-Z0-9._/-]+")
HOST_KEYS = {
    "ssh-ed25519": "ssh-ed25519",
    "ecdsa-sha2-nistp256": "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384": "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521": "ecdsa-sha2-nistp521",
    "ssh-rsa": "rsa-sha2-512,rsa-sha2-256",
}
CAPTURE_FIELDS = {
    "success",
    "action",
    "scope",
    "synthetic",
    "source_attestation",
    "agents",
    "files",
    "manifest_sha256",
    "archive_name",
    "archive_sha256",
    "archive_size",
}
HEADER_FIELDS = {"protocol", "hostname", "run_id", "started_at_utc", "completed_at_utc", "capture"}
MAX_HEADER = 8192
TIMEOUT = 180
CLOCK_SKEW = timedelta(seconds=5)


def checked_path(value):
    backup.require(isinstance(value, str) and FILE_PATH.fullmatch(value), "OPENCLAW_RUTA_PERFIL")
    path = Path(value)
    backup.require(
        path.anchor == "/" and str(path) == value and ".." not in path.parts, "OPENCLAW_RUTA_PERFIL"
    )
    return path


def read_pin(parent, name, pin, maximum):
    backup.require(backup.valid_pin(pin), "OPENCLAW_SHA_PERFIL")
    raw = backup.read_file(parent, name, maximum)
    backup.require(raw and backup.digest(raw) == pin, "OPENCLAW_SHA_PERFIL")
    return raw


def load_profile(path, pin):
    path = checked_path(path)
    with backup.private_root(path.parent) as parent:
        profile = prepare.load_json(read_pin(parent, path.name, pin, 8192))
        backup.require(
            set(profile)
            == {"identity_file", "identity_sha256", "known_hosts_file", "known_hosts_sha256"},
            "OPENCLAW_PERFIL",
        )
        identity = checked_path(profile["identity_file"])
        hosts = checked_path(profile["known_hosts_file"])
        backup.require(
            len({identity, hosts, path}) == 3 and identity.parent == hosts.parent == path.parent,
            "OPENCLAW_RUTA_PERFIL",
        )
        private_key = read_pin(parent, identity.name, profile["identity_sha256"], 8192)
        known_hosts = read_pin(parent, hosts.name, profile["known_hosts_sha256"], 8192)
    backup.require(
        private_key.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----\n"), "OPENCLAW_FORMATO_LLAVE"
    )
    rows = known_hosts.decode("ascii").splitlines()
    backup.require(len(rows) == 1, "OPENCLAW_HOST_KEY")
    host, algorithm, blob, *_ = rows[0].split()
    backup.require(host == ADDRESS and algorithm in HOST_KEYS, "OPENCLAW_HOST_KEY")
    key = base64.b64decode(blob, validate=True)
    name = algorithm.encode("ascii")
    backup.require(key.startswith(len(name).to_bytes(4, "big") + name), "OPENCLAW_HOST_KEY")
    return identity, hosts, HOST_KEYS[algorithm]


def ssh_argv(identity, known_hosts, host_algorithm):
    options = (
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "CertificateFile=none",
        "PreferredAuthentications=publickey",
        "PubkeyAcceptedAlgorithms=ssh-ed25519",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "GSSAPIAuthentication=no",
        "HostbasedAuthentication=no",
        "StrictHostKeyChecking=yes",
        "CheckHostIP=yes",
        "VerifyHostKeyDNS=no",
        "UpdateHostKeys=no",
        "GlobalKnownHostsFile=/dev/null",
        f"UserKnownHostsFile={known_hosts}",
        f"HostKeyAlgorithms={host_algorithm}",
        "ProxyCommand=none",
        "ProxyJump=none",
        "ControlMaster=no",
        "ClearAllForwardings=yes",
        "ForwardAgent=no",
        "ForwardX11=no",
        "PermitLocalCommand=no",
        "ConnectTimeout=10",
        "ConnectionAttempts=1",
        "ServerAliveInterval=10",
        "ServerAliveCountMax=2",
    )
    return [
        "/usr/bin/ssh",
        "-4",
        "-T",
        "-F",
        "/dev/null",
        "-S",
        "none",
        "-p",
        str(PORT),
        "-i",
        str(identity),
        *[value for option in options for value in ("-o", option)],
        f"{USER}@{ADDRESS}",
        COMMAND,
    ]


def receive(argv, request, timeout=TIMEOUT):
    limit = MAX_HEADER + 1 + backup.MAX_FRAME
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        close_fds=True,
    ) as client:
        try:
            client.stdin.write(backup.canonical(request))
            client.stdin.close()
            payload = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(client.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    backup.require(
                        remaining > 0 and selector.select(remaining), "OPENCLAW_SSH_TIMEOUT"
                    )
                    block = os.read(client.stdout.fileno(), min(65536, limit + 1 - len(payload)))
                    if not block:
                        break
                    payload.extend(block)
                    backup.require(len(payload) <= limit, "OPENCLAW_SSH_TAMANO")
            backup.require(
                client.wait(timeout=max(0, deadline - time.monotonic())) == 0, "OPENCLAW_SSH_FALLO"
            )
            return payload
        finally:
            if client.poll() is None:
                client.kill()
                client.wait()


def verify_response(raw, run_id, sent, received):
    header, separator, frame = raw.partition(b"\n")
    backup.require(
        separator and len(header) <= MAX_HEADER and 0 < len(frame) <= backup.MAX_FRAME,
        "OPENCLAW_PROTOCOLO",
    )
    response = prepare.load_json(header)
    backup.require(
        set(response) == HEADER_FIELDS
        and type(response["protocol"]) is int
        and response["protocol"] == 1
        and response["hostname"] == HOSTNAME
        and response["run_id"] == run_id,
        "OPENCLAW_PROTOCOLO",
    )
    start = datetime.fromisoformat(response["started_at_utc"])
    end = datetime.fromisoformat(response["completed_at_utc"])
    backup.require(
        start.utcoffset() == end.utcoffset() == timedelta(0)
        and sent - CLOCK_SKEW <= start <= end <= received + CLOCK_SKEW,
        "OPENCLAW_VENTANA",
    )
    row = response["capture"]
    backup.require(
        isinstance(row, dict)
        and set(row) == CAPTURE_FIELDS
        and row["success"] is True
        and row["action"] == "capture"
        and row["scope"] == "agency-state"
        and row["synthetic"] is False
        and row["source_attestation"] == "external-staging-manifest"
        and row["archive_name"] == backup.ARCHIVE
        and type(row["agents"]) is int
        and row["agents"] == len(backup.AGENTS)
        and type(row["files"]) is int
        and len(backup.AGENTS) <= row["files"] <= backup.MAX_FILES
        and type(row["archive_size"]) is int
        and row["archive_size"] == len(frame)
        and frame.startswith(backup.ZSTD_MAGIC)
        and backup.valid_pin(row["manifest_sha256"])
        and backup.digest(frame) == row["archive_sha256"],
        "OPENCLAW_RECIBO_REMOTO",
    )
    manifest, files = backup.unpack_tar(backup.zstd(bytes(frame), decompress=True), "agency-state")
    backup.require(
        backup.digest(manifest) == row["manifest_sha256"] and len(files) == row["files"],
        "OPENCLAW_MANIFIESTO",
    )
    for agent, expected in prepare.AGENTS.items():
        config = prepare.load_json(files[f"{agent}/config.json"])
        backup.require(config == {"id": agent, "model": expected}, "OPENCLAW_PROYECCION")
    return row, bytes(frame), start.isoformat(), end.isoformat()


def wait_until_received(end):
    remaining = end - datetime.now(UTC)
    backup.require(remaining <= CLOCK_SKEW, "OPENCLAW_VENTANA")
    if remaining > timedelta(0):
        time.sleep(remaining.total_seconds())
        backup.require(end <= datetime.now(UTC), "OPENCLAW_VENTANA")


def write_private(path, content):
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def transport(request, profile, profile_pin, *, ingress=INGRESS, exchange=receive):
    import fcntl

    backup.require(
        set(request) == {"run_id"}
        and isinstance(request["run_id"], str)
        and RUN_ID.fullmatch(request["run_id"]),
        "OPENCLAW_SOLICITUD",
    )
    run_id = request["run_id"]
    argv = ssh_argv(*load_profile(profile, profile_pin))
    with backup.private_root(ingress) as parent:
        fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        backup.require(run_id not in os.listdir(parent), "OPENCLAW_CORRIDA_EXISTENTE")
        sent = datetime.now(UTC)
        raw = exchange(argv, request)
        row, frame, start, end = verify_response(raw, run_id, sent, datetime.now(UTC))
        # El ensamblador exige una captura finalizada según el reloj local.
        wait_until_received(datetime.fromisoformat(end))
        receipt = backup.canonical(row)
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=ingress) as pending:
            pending = Path(pending)
            write_private(pending / "capture.json", receipt)
            write_private(pending / backup.ARCHIVE, frame)
            with backup.directory(pending.name, parent) as directory:
                os.fsync(directory)
            backup.require(run_id not in os.listdir(parent), "OPENCLAW_CORRIDA_EXISTENTE")
            os.rename(pending.name, run_id, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
    return {
        "receipt": str(ingress / run_id / "capture.json"),
        "sha256": backup.digest(receipt),
        "manifest_sha256": row["manifest_sha256"],
        "hostname": HOSTNAME,
        "started_at_utc": start,
        "completed_at_utc": end,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profile-sha256", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        backup.require(
            sys.platform == "linux" and os.geteuid() == os.getegid() == 0, "OPENCLAW_OPERADOR"
        )
        raw = sys.stdin.buffer.read(1025)
        backup.require(len(raw) <= 1024, "OPENCLAW_SOLICITUD")
        result = transport(prepare.load_json(raw), args.profile, args.profile_sha256)
    except (OSError, TypeError, ValueError, KeyError, RecursionError, subprocess.SubprocessError):
        print('{"success":false,"error":"OPENCLAW_TRANSPORT_REJECTED"}', file=sys.stderr)
        return 1
    print(backup.canonical(result).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
