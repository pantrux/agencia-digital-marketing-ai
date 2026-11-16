"""Exporta las seis salidas físicas del ensayo SQLite terminado."""

import argparse
import base64
import hashlib
import json
import os
import signal
import struct
import sys
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType

HERE = Path(globals().get("__file__", "/opt/media-platform/docker/n8n/export.py")).resolve().parent
REPO = HERE.parents[1]
EVIDENCE = REPO / ".tools/evidence"
ACCEPTANCE = "1f02114ffb158f8a1f9b126ed9eb1cd5ec72b3cb1e6d9d5f8a6d77ee150dd987"
TERMINAL = "9bd8dd7fa555a18bfd283114cc153b31412c05f03a79de4b9fd3f0bf9d3a776d"
APPROVAL = "78e96f070bd8c2a7b76413bb3a789125dabcd5ee42dad46d978df44cfa538eb9"
OUTPUTS = frozenset(
    f"wa-sqlite{variant}.{ext}" for variant in ("", "-async", "-jspi") for ext in ("mjs", "wasm")
)
MAX_FILE = 4 * 1024**2
PREFIX = b"SQLITE_DIST_V1\n"


def need(ok, code):
    if not ok:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def load(raw, pin, name, path="<operator>"):
    need(sha(raw) == pin, "FUENTE_PIN")
    module = ModuleType(name)
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def exact(read, size):
    body = bytearray()
    while len(body) < size:
        chunk = read(min(65536, size - len(body)))
        need(chunk, "TRAMA_TRUNCADA")
        body.extend(chunk)
    return bytes(body)


def checked_terminal(status):
    need(
        sha(canonical(status)) == TERMINAL
        and status["success"] is status["functional_verified"] is True
        and status["action"] == "status"
        and status["state"]["Status"] == "exited"
        and status["state"]["ExitCode"] == status["state"]["Pid"] == 0
        and status["state"]["Running"] is status["state"]["OOMKilled"] is False
        and status["functional"]["outputs"].keys() == OUTPUTS,
        "TERMINAL_PIN",
    )
    return status["functional"]["outputs"]


def remote():
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(2))
    signal.alarm(90)
    read = sys.stdin.buffer.read
    size = struct.unpack(">I", exact(read, 4))[0]
    need(0 < size <= 256 * 1024, "TRAMA_LIMITE")
    data = json.loads(exact(read, size))
    need(not read(1), "TRAMA_EXTRA")
    raw = {key: base64.b64decode(value, validate=True) for key, value in data["sources"].items()}
    status = data["status"]
    outputs = checked_terminal(status)
    accepted = load(raw["acceptance"], ACCEPTANCE, "wa_accepted_operator")
    builder = load(raw["builder"], accepted.BUILDER, "wa_accepted_builder")
    base = load(raw["baseline"], builder.SOURCES["baseline"][1], "wa_accepted_baseline")
    docker = base.docker_helper(raw["docker"])
    builder.check_host(base, docker, data["daemon"])
    image = accepted.checked_image(builder, base, docker, data["build"], data["approval"], APPROVAL)
    need(sha(raw["probe"]) == accepted.PROBE_SHA, "PROBE_PIN")
    proof = accepted.verify_created(
        base,
        image,
        {**data, "approval_sha256": APPROVAL, "plan_sha256": status["plan_sha256"]},
        raw["probe"],
    )
    cid = proof["cid"]
    state = accepted.inspect(docker, cid, image)
    need(
        cid == status["cid"] and all(state[k] == value for k, value in status["state"].items()),
        "ESTADO_PIN",
    )
    need(accepted.functional(base) == status["functional"], "FUNCIONAL_PIN")
    dist = accepted.ROOT / "work/wa-sqlite/dist"
    files = {}
    for name, expected in sorted(outputs.items()):
        body = base.read_leaf(dist / name, uid=1000, limit=MAX_FILE)
        need(len(body) == expected["bytes"] and sha(body) == expected["sha256"], "SALIDA_PIN")
        files[name] = body
    need(accepted.inspect(docker, cid, image) == state, "ESTADO_CAMBIO")
    out = sys.stdout.buffer
    out.write(PREFIX)
    for body in files.values():
        out.write(body)
    out.flush()


def read_json(name, pin):
    raw = (EVIDENCE / name).read_bytes()
    need(sha(raw) == pin, "RECIBO_PIN")
    return json.loads(raw)


def local():
    path = HERE / "n8n_wa_sqlite_sdk_acceptance_vm_delta_v5.py"
    source = path.read_bytes()
    accepted = load(source, ACCEPTANCE, "wa_acceptance_local", path)
    acceptance, _, sources, client = accepted.local_plan()
    sources = {name: body for name, body in sources.items() if name != "reader"}
    sources["acceptance"] = source
    stem = "phase4-n8n-wa-sqlite-sdk-"
    status = read_json(stem + "acceptance-status-delta-v5.json", TERMINAL)
    need(sha(canonical(acceptance)) == status["plan_sha256"], "ACEPTACION_PLAN")
    outputs = checked_terminal(status)
    build = read_json(stem + "build-delta-v5.json", accepted.BUILD_RECEIPT)
    approval = read_json(stem + "functional-approval-delta-v5.json", APPROVAL)
    accepted.approval_for(build, approval, APPROVAL)
    create_path = EVIDENCE / (stem + "acceptance-create-delta-v5.json")
    create = json.loads(create_path.read_bytes())
    created = create["created"]
    need(
        create["success"]
        and created["cid"] == status["cid"]
        and created["image"] == status["image"]
        and created["plan_sha256"] == status["plan_sha256"]
        and created["approval_sha256"] == APPROVAL,
        "CREATE_PIN",
    )
    raw = client.source(Path(__file__).resolve())
    request = {"status": status, "build": build, "approval": approval, "created": created}
    plan = {
        "schema": 1,
        "terminal_sha256": TERMINAL,
        "approval_sha256": APPROVAL,
        "controller_sha256": sha(raw),
        "source_sha256": {name: sha(body) for name, body in sources.items()},
        "cid": status["cid"],
        "outputs": outputs,
    }
    request["sources"] = {name: base64.b64encode(body).decode() for name, body in sources.items()}
    return plan, request, client, raw


def transfer(client, code, request, ssh, outputs):
    sql = REPO / "docker/postgres-immich/sql_acceptance_v2.py"
    sdk = load(
        client.source(sql, 64 * 1024), client.SQL_TRANSPORT, "wa_export_ssh", sql
    ).sdk_local()
    need(
        sha(client.source(sql.with_name("sql_acceptance_remote_v2.py"), 64 * 1024))
        == client.SQL_CONTRACT,
        "SSH_CONTRATO",
    )
    with ExitStack() as stack:
        connection = sdk.connect(ssh)
        stack.callback(connection.close)
        channel = connection.get_transport().open_session(timeout=10)
        stack.callback(channel.close)
        channel.settimeout(30)
        channel.exec_command(client.remote_command("remote-export", sha(code)))
        channel.sendall(client.framed(code, 32 * 1024))
        channel.sendall(client.framed(canonical(request), 256 * 1024))
        channel.shutdown_write()
        need(exact(channel.recv, len(PREFIX)) == PREFIX, "TRAMA_CABECERA")
        files = {}
        for name, meta in sorted(outputs.items()):
            need(0 < meta["bytes"] <= MAX_FILE, "SALIDA_LIMITE")
            raw = exact(channel.recv, meta["bytes"])
            need(sha(raw) == meta["sha256"], "SALIDA_PIN")
            files[name] = raw
        need(not channel.recv(1) and channel.recv_exit_status() == 0, "TRAMA_CIERRE")
    return files


def publish(path, files, plan):
    need(path.parent.resolve(strict=True) == path.parent, "DESTINO_RUTA")
    acl = REPO / "sre/backup_tools_access.py"
    access = load(
        acl.read_bytes(),
        "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b",
        "wa_export_acl",
        acl,
    )
    access.check_access(path.parent, private=True)
    path.mkdir(mode=0o700)
    access.check_access(path, private=True)
    dist = path / "dist"
    dist.mkdir(mode=0o700)
    for name, raw in files.items():
        with (dist / name).open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    result = {**plan, "success": True}
    with (path / "receipt.json").open("xb") as stream:
        stream.write(canonical(result))
        stream.flush()
        os.fsync(stream.fileno())
    return result


def main():
    if sys.argv[1:] == ["remote-export"]:
        return remote()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("plan", "export"), default="plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    plan, request, client, raw = local()
    digest = sha(canonical(plan))
    if args.action == "plan":
        print(canonical({**plan, "plan_sha256": digest}).decode(), end="")
        return
    need(args.plan_sha256 == digest and args.output is not None, "PLAN_PIN")
    data_raw = sys.stdin.buffer.read(128 * 1024 + 1)
    need(0 < len(data_raw) <= 128 * 1024, "PETICION_LIMITE")
    data = json.loads(data_raw)
    need(
        data.keys() == {"ssh", "daemon"} and data["ssh"].keys() == client.SSH_KEYS,
        "PETICION_CAMPOS",
    )
    files = transfer(
        client, raw, {**request, "daemon": data["daemon"]}, data["ssh"], plan["outputs"]
    )
    result = publish(args.output.absolute(), files, {**plan, "plan_sha256": digest})
    print(canonical(result).decode(), end="")


if __name__ == "__main__":
    main()
