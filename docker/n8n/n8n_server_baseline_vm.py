"""Plan y transporte SSH explícito del ensayo baseline del servidor n8n."""

import argparse
import base64
import hashlib
import json
import os
import shlex
import stat
import struct
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
EVIDENCE = ROOT / ".tools/evidence"
ARCHIVE = ROOT / (
    ".tools/backup-source-build/n8n-server-source-v1/server-linux-transfer-v2/"
    "server-linux-x64-musl-delta-v2.tar.gz"
)
ARCHIVE_SHA = "b3405a0d625db295e62cc9cf3edd06eca25548c4c1c91e3c108b23397df406ed"
ARCHIVE_SIZE = 439_342_873
SQL_TRANSPORT = "e7a4f10379ffdcd53689c4c221ff7b84154502b37f956d33e4835221eb056c43"
SQL_CONTRACT = "114532cb83ec79974941edbc0fbed652b2f0c2700b47dcfb8e8d63863326fc5a"
REMOTE_SHA = "925fd3bdb7a24549557050d54ff0a97e2a710eff632c3d1f81efc506e58a9be6"
SSH_KEYS = {
    "MEDIA_VM_IP",
    "MEDIA_VM_SSH_USER",
    "MEDIA_VM_SSH_PUBLIC_KEY",
    "MEDIA_VM_SSH_HOST_PUBLIC_KEY",
    "MEDIA_VM_SSH_HOST_FINGERPRINT",
    "MEDIA_VM_SSH_PRIVATE_KEY_B64",
}
BOOTSTRAP = """import hashlib,struct,sys
def exact(n):
    result=sys.stdin.buffer.read(n)
    if len(result)!=n: raise ValueError('SOURCE_TRUNCATED')
    return result
size=struct.unpack('>I',exact(4))[0]
if not 0<size<=32768: raise ValueError('SOURCE_LIMIT')
source=exact(size)
if hashlib.sha256(source).hexdigest()!=sys.argv.pop(1): raise ValueError('SOURCE_PIN')
exec(compile(source,'<n8n-server-baseline-vm>','exec'))
"""


def need(value, code):
    if not value:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(name, path, raw):
    module = ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def source(path, limit=32 * 1024):
    need(path.resolve(strict=True) == path, "FUENTE_RUTA")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    need(0 < len(raw) <= limit, "FUENTE_LIMITE")
    return raw


def build_plan():
    remote_path = HERE / "server_baseline_vm_remote.py"
    remote_raw = source(remote_path)
    need(sha(remote_raw) == REMOTE_SHA, "REMOTO_PIN")
    remote = load("server_baseline_vm_remote_pinned", remote_path, remote_raw)
    v5 = source(EVIDENCE / "phase4-n8n-js-linux-transfer-v1.json")
    need(sha(v5) == remote.V5_RECEIPT, "V5_RECIBO_PIN")
    files = {}
    for name, digest in remote.SOURCES.items():
        location = HERE if name.startswith(("receive_server_", "seed_server_")) else EVIDENCE
        raw = source(location / name)
        need(sha(raw) == digest, "FUENTE_PIN")
        files[name] = raw
    control = {
        "schema": 1,
        "purpose": "server-reference-offline-trial",
        "owner": remote.OWNER,
        "target": "192.168.0.85",
        "hostname": "vm-media-01",
        "controller_sha256": sha(source(Path(__file__).resolve())),
        "remote_sha256": sha(remote_raw),
        "transport_sha256": SQL_TRANSPORT,
        "transport_contract_sha256": SQL_CONTRACT,
        "sources": remote.SOURCES,
        "archive_sha256": ARCHIVE_SHA,
        "archive_bytes": ARCHIVE_SIZE,
        "delta": remote.DELTA.as_posix(),
        "control": remote.CONTROL.as_posix(),
        "container_name": remote.NAME,
        "sdk_image": remote.IMAGE,
        "v5_receipt_sha256": remote.V5_RECEIPT,
        "lock_sha256": remote.LOCK,
        "order": ["receive", "create", "start", "status"],
        "create_args": remote.create_args(),
        "production_approved": False,
    }
    return control, remote, remote_raw, json.loads(v5), files


def checked_archive(stack):
    need(ARCHIVE.resolve(strict=True) == ARCHIVE, "CAPSULA_RUTA")
    for path in (*reversed(ARCHIVE.parents), ARCHIVE):
        node = path.lstat()
        need(
            not stat.S_ISLNK(node.st_mode) and not getattr(node, "st_file_attributes", 0) & 0x400,
            "CAPSULA_ENLACE",
        )
    need(
        stat.S_ISREG(node.st_mode) and node.st_nlink == 1 and node.st_size == ARCHIVE_SIZE,
        "CAPSULA_TAMANO",
    )
    fd = os.open(ARCHIVE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    stream = stack.enter_context(os.fdopen(fd, "rb"))
    need(os.path.samestat(node, os.fstat(stream.fileno())), "CAPSULA_SUSTITUIDA")
    need(hashlib.file_digest(stream, "sha256").hexdigest() == ARCHIVE_SHA, "CAPSULA_PIN")
    stream.seek(0)
    return stream


def framed(raw, limit):
    need(0 < len(raw) <= limit, "FRAME_LIMITE")
    return struct.pack(">I", len(raw)) + raw


def remote_command(action, digest):
    return (
        "/usr/bin/sudo -n /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C HOME=/root "
        "/usr/bin/python3 -I -B -c " + shlex.quote(BOOTSTRAP) + " " + digest + " " + action
    )


def transmit(channel, stream, deadline):
    digest, remaining = hashlib.sha256(), ARCHIVE_SIZE
    while remaining:
        need(time.monotonic() < deadline, "CAPSULA_PLAZO")
        chunk = stream.read(min(remaining, 65536))
        need(chunk, "CAPSULA_TRUNCADA")
        digest.update(chunk)
        remaining -= len(chunk)
        channel.sendall(chunk)
    need(not stream.read(1) and digest.hexdigest() == ARCHIVE_SHA, "CAPSULA_CAMBIO")


def wait_ready(channel, marker, deadline):
    ready = bytearray()
    while time.monotonic() < deadline:
        if channel.recv_stderr_ready():
            ready.extend(channel.recv_stderr(len(marker) - len(ready)))
            need(marker.startswith(ready), "PREPARACION_RESPUESTA")
            if ready == marker:
                return True
        if channel.exit_status_ready():
            need(not ready, "PREPARACION_TRUNCADA")
            return False
        time.sleep(0.01)
    raise TimeoutError("PREPARACION_PLAZO")


def operate(action, built, approved, data, stream):
    plan, remote, remote_raw, v5, files = built
    need(action in {"receive", "create", "start", "status"}, "ACCION")
    need(approved == sha(remote.canonical(plan)), "PLAN_PIN")
    fields = {"ssh", "parent", "daemon"}
    if action != "receive":
        fields.add("received")
    if action in {"start", "status"}:
        fields.add("created")
    need(set(data) == fields and set(data["ssh"]) == SSH_KEYS, "PETICION_CAMPOS")
    request = {key: value for key, value in data.items() if key != "ssh"}
    request.update(
        action=action,
        plan_sha256=approved,
        v5=v5,
        sources={name: base64.b64encode(raw).decode("ascii") for name, raw in files.items()},
    )
    packet = framed(remote.canonical(request), remote.MAX_JSON)
    sql_path = ROOT / "docker/postgres-immich/sql_acceptance_v2.py"
    sql_raw = source(sql_path, 64 * 1024)
    need(sha(sql_raw) == SQL_TRANSPORT, "TRANSPORTE_PIN")
    need(
        sha(source(sql_path.with_name("sql_acceptance_remote_v2.py"), 64 * 1024)) == SQL_CONTRACT,
        "TRANSPORTE_CONTRATO",
    )
    sdk = load("n8n_baseline_sql_transport", sql_path, sql_raw).sdk_local()
    with ExitStack() as stack:
        client = sdk.connect(data["ssh"])
        stack.callback(client.close)
        channel = client.get_transport().open_session(timeout=10)
        stack.callback(channel.close)
        channel.settimeout(10)
        deadline = time.monotonic() + (930 if action == "receive" else 140)
        channel.exec_command(remote_command(action, plan["remote_sha256"]))
        channel.sendall(framed(remote_raw, 32 * 1024))
        channel.sendall(packet)
        if stream is not None and wait_ready(channel, remote.READY, time.monotonic() + 45):
            transmit(channel, stream, deadline)
        channel.shutdown_write()
        result = sdk.collect(channel, deadline, 64 * 1024)
    need(
        result["action"] == action
        and result["plan_sha256"] == approved
        and result["owner"] == remote.OWNER
        and result["image"] == remote.IMAGE,
        "RECIBO_IDENTIDAD",
    )
    return result


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "action",
        nargs="?",
        choices=["plan", "receive", "create", "start", "status"],
        default="plan",
    )
    cli.add_argument("--plan-sha256")
    cli.add_argument("--receipt", type=Path)
    args = cli.parse_args()
    built = build_plan()
    plan, remote = built[:2]
    approved = sha(remote.canonical(plan))
    if args.action == "plan":
        print(json.dumps({**plan, "plan_sha256": approved}, sort_keys=True))
        return 0
    need(args.plan_sha256 == approved and args.receipt is not None, "PLAN_O_RECIBO")
    output = args.receipt.absolute()
    need(
        output.parent.resolve(strict=True) == output.parent and not os.path.lexists(output),
        "RECIBO_RUTA",
    )
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    need(0 < len(raw) <= 128 * 1024, "PETICION_LOCAL_LIMITE")
    data = json.loads(raw)
    with ExitStack() as stack:
        stream = checked_archive(stack) if args.action == "receive" else None
        fd = os.open(
            output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
        evidence = stack.enter_context(os.fdopen(fd, "wb"))
        try:
            result = operate(args.action, built, approved, data, stream)
        except Exception as error:
            result = {
                "action": args.action,
                "plan_sha256": approved,
                "success": False,
                "transport_error": type(error).__name__,
            }
        result_raw = remote.canonical(result)
        evidence.write(result_raw)
        evidence.flush()
        os.fsync(evidence.fileno())
    sys.stdout.buffer.write(result_raw)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
