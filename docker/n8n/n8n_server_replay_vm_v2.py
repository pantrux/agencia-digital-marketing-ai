"""Plan y transiciones del CID que repite el baseline n8n sobre el store RO."""

import argparse
import ast
import base64
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType

CLIENT_V1 = "c520745f9b2fc31d33c54a19bbaf30f67203833e4521aeab9a81a07f4e9f32ce"
REMOTE_V1 = "925fd3bdb7a24549557050d54ff0a97e2a710eff632c3d1f81efc506e58a9be6"
REPLAY = "864a1a69356baea58ba0e0d601d7f41e2b2a7b3989994957ea35dac9a2450984"
INDEX = "9f134672f2ec03d43bfd7809e837b83154ad4f1c2ccc4e2410d5aa35a3c42d14"
CATALOG = "6c9131b3dd533604432ea361b49cae43385216e16812adb0d1d3c8d3719c3123"
DIAGNOSIS = "4be93beacf8bbeee6d537d37ad9dffefb9ebbda9ddcfea6ffd64c12e9b077136"
REPLAY_FILE = "replay_server_linux_baseline_v2.py"
STORE_LAYOUT = "8168080a8c029be9d51676f565c851c1840282136b9ac60a58fcfafb6dd01cca"
PRIOR_REPLAY_CID = "4bd2122fb92a2b99206b2fe4229095b26296cfe8c7016253c89eaa1730594246"
VENDOR_SHA = "16dcd4442cb7073182c1eb2951e495c4415f7de8924d188fafb670a8798da662"
EVIDENCE = {
    "receive": (
        "receive-v1.json",
        "0650833725c0619b157508aeacab060429f91179123c943facb73b82fdca9aa7",
    ),
    "create": (
        "create-v1.json",
        "470db2da567d8496820a9b6e4b9760576e429dd0d83185b2fc6f73e29c4fa967",
    ),
    "status": (
        "status-v1-probe-01.json",
        "dc18b802bbee10b50efba464c6a820e3bd325cbef5b9818c8b33507602b84930",
    ),
}
OWNER = "agencia-digital-marketing-ai:n8n-server-baseline-replay-v2"
NAME = "agency-n8n-server-baseline-replay-v2"


def need(ok, code):
    if not ok:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def module(raw, digest, name, path):
    need(sha(raw) == digest, "FUENTE_PIN")
    value = ModuleType(name)
    value.__file__ = str(path)
    sys.modules[name] = value
    exec(compile(raw, str(path), "exec"), value.__dict__)
    return value


def paths(orig):
    control = orig.DELTA / "baseline-replay-v2"
    store = orig.WORK / "baseline-seed-v1/store"
    mounts = {
        "/inputs": (orig.DELTA / "inputs", False),
        "/v5": (orig.V5 / "inputs", False),
        "/store": (store, False),
        "/ops": (control / "ops", False),
        "/work": (control / "work", True),
    }
    return control, store, mounts


def container_contract(orig, raw):
    control, _, mounts = paths(orig)
    namespace = {
        name: getattr(orig, name) for name in ("IMAGE", "TMPFS", "CID", "need", "json", "Path")
    }
    namespace.update(
        CONTROL=control,
        NAME=NAME,
        OWNER=OWNER,
        MOUNTS=mounts,
        ARGV=[
            "timeout",
            "-s",
            "TERM",
            "-k",
            "10",
            "1050",
            "/usr/local/bin/python3",
            "-I",
            "-B",
            "/ops/" + REPLAY_FILE,
        ],
    )
    nodes = [
        node
        for node in ast.parse(raw).body
        if isinstance(node, ast.FunctionDef) and node.name in {"create_args", "inspect"}
    ]
    need({node.name for node in nodes} == {"create_args", "inspect"}, "CONTRATO_CID")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<baseline-v1-cid>", "exec"), namespace)
    return namespace["create_args"], namespace["inspect"]


def exact_original(orig, docker, request):
    old = request["original"]
    need(
        set(old) == EVIDENCE.keys()
        and all(sha(orig.canonical(old[key])) == pin for key, (_, pin) in EVIDENCE.items()),
        "BASELINE_RECIBOS",
    )
    orig.physical_receipt(orig.DELTA, old["receive"]["received"])
    prior = old["create"]
    need(orig.read_leaf(orig.CONTROL / "create.json") == orig.canonical(prior), "BASELINE_CREATE")
    for path, key, uid, mode in (
        (orig.CONTROL, "control", 0, 0o700),
        (orig.OPS, "ops", 0, 0o555),
        (orig.WORK, "work", 1000, 0o700),
    ):
        need(orig.directory(path, uid, mode) == prior[key], "BASELINE_IDENTIDAD")
    state = orig.inspect(docker, prior["cid"])
    status = old["status"]
    need(
        prior["cid"] == status["cid"]
        and state["Status"] == "exited"
        and state["Pid"] == 0
        and state["ExitCode"] == 1
        and not state["OOMKilled"]
        and state["StartedAt"] == status["started_at"]
        and state["FinishedAt"] == status["finished_at"],
        "BASELINE_CID",
    )
    (prior_replay,) = json.loads(docker("container", "inspect", PRIOR_REPLAY_CID).stdout)
    previous = prior_replay["State"]
    need(
        prior_replay["Id"] == PRIOR_REPLAY_CID
        and prior_replay["Image"] == orig.IMAGE
        and prior_replay["Name"] == "/agency-n8n-server-baseline-replay-v1"
        and prior_replay["Config"]["Labels"].get("agency.owner")
        == "agencia-digital-marketing-ai:n8n-server-baseline-replay-v1"
        and previous["Status"] == "exited"
        and previous["Pid"] == 0
        and previous["ExitCode"] == 1
        and not previous["OOMKilled"],
        "REPLAY_V1_ESTADO",
    )
    _, store, _ = paths(orig)
    need(
        {path.name for path in store.iterdir()} == {"v11"}
        and {path.name for path in (store / "v11").iterdir()} == {"files", "index.db"},
        "STORE_ARBOL_FISICO",
    )
    index = store / "v11/index.db"
    node = index.lstat()
    need(
        (node.st_dev, node.st_ino, node.st_size) == (2050, 2890258, 49_029_120),
        "BASELINE_DB_IDENTIDAD",
    )
    need(sha(orig.read_leaf(index, uid=1000, limit=49_029_120)) == INDEX, "BASELINE_DB_SHA")
    need(
        not any(
            os.path.lexists(index.with_name(index.name + suffix))
            for suffix in ("-wal", "-shm", "-journal")
        ),
        "BASELINE_DB_WAL",
    )


def install_ops(orig, reader, control, files):
    ops = control / "ops"
    ops.mkdir(mode=0o700)
    fd = reader.directory(None, ops)
    try:
        output = reader.Output(fd)
        for name in sorted(orig.OPS_FILES | {REPLAY_FILE}):
            raw = files[name]
            need(
                output.copy((name,), io.BytesIO(raw), len(raw), False)
                == (REPLAY if name == REPLAY_FILE else orig.SOURCES[name]),
                "OPS_SHA",
            )
        output.seal()
    finally:
        os.close(fd)
    work = control / "work"
    work.mkdir(mode=0o700)
    os.chown(work, 1000, 1000, follow_symlinks=False)
    return {"ops": orig.directory(ops, 0, 0o555), "work": orig.directory(work, 1000, 0o700)}


def operation(action, orig, raw, reader, docker, request, files, out):
    control, _, _ = paths(orig)
    work = control / "work"
    create_args, inspect = container_contract(orig, raw)
    receiver = orig.pinned_module(
        "receive_server_linux_baseline.py", files["receive_server_linux_baseline.py"]
    )
    if action == "create":
        receiver.storage(orig.DELTA, 5 * 1024**3, 350_000)
        need(
            not docker("ps", "-aq", "--filter", "name=^/" + NAME + "$").stdout.strip(),
            "NOMBRE_OCUPADO",
        )
        control.mkdir(mode=0o700)
        out["control"] = orig.directory(control, 0, 0o700)
        orig.publish(
            reader, control / ".agency-owner", orig.canonical({"owner": OWNER, **out["control"]})
        )
        out.update(install_ops(orig, reader, control, files))
        orig.publish(reader, control / "create-intent.json", orig.canonical(out))
        try:
            result = docker(*create_args(), timeout=40)
        finally:
            if (control / "cid").exists():
                cid = orig.read_leaf(control / "cid", limit=80).decode().strip()
                if orig.CID.fullmatch(cid):
                    out["cid"] = cid
        need(result.stdout.decode().strip() == out.get("cid"), "CID_CREATE")
        state = inspect(docker, out["cid"])
        need(state["Status"] == "created" and state["Pid"] == 0, "CID_ESTADO")
        return
    prior = request["created"]
    need(
        prior["action"] == "create"
        and prior["success"] is True
        and prior["plan_sha256"] == out["plan_sha256"]
        and orig.read_leaf(control / "create.json") == orig.canonical(prior),
        "CREATE_NO_RESPALDADO",
    )
    for path, key, uid, mode in (
        (control, "control", 0, 0o700),
        (control / "ops", "ops", 0, 0o555),
        (work, "work", 1000, 0o700),
    ):
        need(orig.directory(path, uid, mode) == prior[key], "REPLAY_IDENTIDAD")
    need(
        orig.read_leaf(control / ".agency-owner")
        == orig.canonical({"owner": OWNER, **prior["control"]}),
        "REPLAY_MARCA",
    )
    need(
        all(
            sha(orig.read_leaf(control / "ops" / name, limit=orig.MAX_FILE))
            == (REPLAY if name == REPLAY_FILE else orig.SOURCES[name])
            for name in orig.OPS_FILES | {REPLAY_FILE}
        ),
        "REPLAY_FUENTES",
    )
    cid = prior["cid"]
    out["cid"] = cid
    need(orig.read_leaf(control / "cid", limit=80).decode().strip() == cid, "CID_FISICO")
    state = inspect(docker, cid)
    run = work / "baseline-replay-v2"
    if action == "start":
        need(
            state["Status"] == "created" and state["Pid"] == 0 and not os.path.lexists(run),
            "START_NO_REPLAY",
        )
        receiver.storage(work, 5 * 1024**3, 350_000)
        orig.publish(reader, control / "start-intent.json", orig.canonical(out))
        need(docker("start", cid, timeout=30).stdout.decode().strip() == cid, "START_CID")
        state = inspect(docker, cid)
    out.update(
        status=state["Status"],
        pid=state["Pid"],
        exit=state["ExitCode"],
        oom=state["OOMKilled"],
        baseline_verified=False,
    )
    if state["Status"] == "exited" and state["ExitCode"] == 0:
        proof = orig.directory(run, 1000, 0o700)
        marker = json.loads(orig.read_leaf(run / ".agency-owner", uid=1000))
        receipt = orig.read_leaf(run / "receipt.json", uid=1000, limit=64 * 1024)
        result = json.loads(receipt)
        need(
            marker
            == {"owner": "agencia-digital-marketing-ai:n8n-server-baseline-replay:v2", **proof}
            and all(result[key] == value for key, value in marker.items())
            and result["lock_sha256"] == orig.LOCK
            and result["index"]["sha256"] == INDEX
            and result["store_catalog_sha256"] == CATALOG
            and result["policy_entries"] == 3476
            and result["resolved"] == result["added"] == 3164
            and result["downloaded"] == 0
            and result["store_read_only"] is True
            and result["store_rows"] == 3125
            and result["vendor_sha256"] == VENDOR_SHA
            and result["process"]["lock_unchanged"] is True
            and result["compiled"] is result["production_approved"] is False,
            "REPLAY_RECIBO",
        )
        out.update(baseline_verified=True, replay=result, replay_sha256=sha(receipt))
    elif state["Status"] == "exited":
        result = docker("logs", "--tail", "24", cid, timeout=15, check=False)
        logs = result.stdout + result.stderr
        need(len(logs) <= 64 * 1024, "LOG_LIMITE")
        out.update(log_sha256=sha(logs), tail=logs[-3000:].decode("utf-8", "replace"))


def remote(action):
    os.umask(0o077)
    signal.alarm(145)
    out = {"action": action, "owner": OWNER, "success": False}
    orig = reader = None
    try:
        need(os.geteuid() == os.getegid() == 0 and os.uname().nodename == "vm-media-01", "HOST")
        from struct import unpack

        header = sys.stdin.buffer.read(4)
        need(len(header) == 4, "PETICION_TRUNCADA")
        size = unpack(">I", header)[0]
        need(0 < size <= 256 * 1024, "PETICION_LIMITE")
        packet = sys.stdin.buffer.read(size)
        need(len(packet) == size, "PETICION_TRUNCADA")
        request = json.loads(packet)
        need(not sys.stdin.buffer.read(1) and request["action"] == action, "PETICION_FRAMING")
        raw = base64.b64decode(request["remote_v1"], validate=True)
        orig = module(raw, REMOTE_V1, "n8n_replay_remote_v1", "<baseline-v1>")
        need(orig.CID.fullmatch(request["plan_sha256"]), "PLAN")
        out.update(plan_sha256=request["plan_sha256"], image=orig.IMAGE)
        files = {
            name: base64.b64decode(data, validate=True) for name, data in request["sources"].items()
        }
        pins = orig.SOURCES | {REPLAY_FILE: REPLAY}
        need(
            files.keys() == pins.keys()
            and all(sha(data) == pins[name] for name, data in files.items()),
            "FUENTES",
        )
        reader = orig.pinned_module(
            "receive-n8n-js-linux-transfer-v1.py", files["receive-n8n-js-linux-transfer-v1.py"]
        )
        docker = orig.docker_helper(files["n8n_js_store_container_v1.py"])
        need(orig.directory(orig.PARENT, 0, 0o700) == request["parent"], "PARENT_IDENTIDAD")
        orig.verify_v5(request["v5"])
        orig.verify_daemon(docker, request["daemon"])
        exact_original(orig, docker, request)
        operation(action, orig, raw, reader, docker, request, files, out)
        orig.verify_daemon(docker, request["daemon"])
        out["success"] = True
        if action in {"create", "start"}:
            orig.publish(reader, paths(orig)[0] / (action + ".json"), orig.canonical(out))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        out.update(success=False, error=type(error).__name__)
        if (
            str(error).isascii()
            and str(error).replace("_", "").isalnum()
            and str(error).isupper()
            and len(str(error)) <= 48
        ):
            out["code"] = str(error)
        if orig is not None and reader is not None and action == "create" and "control" in out:
            try:
                orig.publish(reader, paths(orig)[0] / "create-failure.json", orig.canonical(out))
            except OSError:
                out["failure_receipt_written"] = False
    finally:
        signal.alarm(0)
    print(json.dumps(out, sort_keys=True, separators=(",", ":")))
    return 0 if out["success"] else 1


def build_plan():
    here = Path(__file__).resolve().parent
    path = here / "n8n_server_baseline_vm.py"
    client = module(path.read_bytes(), CLIENT_V1, "n8n_replay_local_v1", path)
    _, orig, raw, v5, files = client.build_plan()
    original = {}
    for key, (leaf, pin) in EVIDENCE.items():
        data = client.source(client.EVIDENCE / ("phase4-n8n-server-baseline-" + leaf))
        need(sha(data) == pin, "BASELINE_PIN")
        original[key] = json.loads(data)
    need(
        sha(client.source(client.EVIDENCE / "phase4-n8n-server-baseline-replay-diagnosis-v1.json"))
        == DIAGNOSIS,
        "DIAGNOSTICO_PIN",
    )
    layout = client.source(client.EVIDENCE / "phase4-n8n-server-baseline-store-marker-read-v1.json")
    need(sha(layout) == STORE_LAYOUT, "STORE_LAYOUT_PIN")
    layout = json.loads(layout)
    need(
        layout["success"] is True
        and layout["local"] == []
        and layout["store"] == ["v11"]
        and layout["version"] == ["files", "index.db"],
        "STORE_LAYOUT",
    )
    files[REPLAY_FILE] = client.source(here / REPLAY_FILE)
    need(sha(files[REPLAY_FILE]) == REPLAY, "REPLAY_PIN")
    source = client.source(Path(__file__).resolve())
    args, _ = container_contract(orig, raw)
    plan = {
        "schema": 1,
        "owner": OWNER,
        "target": "192.168.0.85",
        "controller_sha256": sha(source),
        "transport_v1_sha256": CLIENT_V1,
        "remote_v1_sha256": REMOTE_V1,
        "replay_sha256": REPLAY,
        "diagnosis_sha256": DIAGNOSIS,
        "original_store_layout_sha256": STORE_LAYOUT,
        "prior_failed_replay_cid": PRIOR_REPLAY_CID,
        "vendor_sha256": VENDOR_SHA,
        "original_cid": original["create"]["cid"],
        "original_index_sha256": INDEX,
        "original_catalog_sha256": CATALOG,
        "sdk_image": orig.IMAGE,
        "name": NAME,
        "control": paths(orig)[0].as_posix(),
        "create_args": args(),
        "production_approved": False,
    }
    return plan, client, orig, raw, source, v5, files, original


def perform(action, approved, data, built):
    plan, client, orig, raw, source, v5, files, original = built
    allowed = {"ssh", "parent", "daemon"} | (
        {"created"} if action in {"start", "status"} else set()
    )
    need(data.keys() == allowed and data["ssh"].keys() == client.SSH_KEYS, "PETICION_CAMPOS")
    request = {key: value for key, value in data.items() if key != "ssh"}
    request.update(
        action=action,
        plan_sha256=approved,
        v5=v5,
        original=original,
        remote_v1=base64.b64encode(raw).decode(),
        sources={name: base64.b64encode(payload).decode() for name, payload in files.items()},
    )
    packet = client.framed(orig.canonical(request), 256 * 1024)
    sql_path = client.ROOT / "docker/postgres-immich/sql_acceptance_v2.py"
    need(
        sha(client.source(sql_path.with_name("sql_acceptance_remote_v2.py"), 64 * 1024))
        == client.SQL_CONTRACT,
        "TRANSPORTE_CONTRATO",
    )
    sdk = module(
        client.source(sql_path, 64 * 1024), client.SQL_TRANSPORT, "n8n_replay_transport", sql_path
    ).sdk_local()
    with ExitStack() as stack:
        ssh = sdk.connect(data["ssh"])
        stack.callback(ssh.close)
        channel = ssh.get_transport().open_session(timeout=10)
        stack.callback(channel.close)
        channel.settimeout(10)
        channel.exec_command(client.remote_command("remote-" + action, plan["controller_sha256"]))
        channel.sendall(client.framed(source, 32 * 1024))
        channel.sendall(packet)
        channel.shutdown_write()
        result = sdk.collect(channel, time.monotonic() + 155, 64 * 1024)
    need(
        result["action"] == action
        and result["plan_sha256"] == approved
        and result["owner"] == OWNER
        and result["image"] == orig.IMAGE,
        "RECIBO_IDENTIDAD",
    )
    return result


def main():
    if sys.argv[1:] in (["remote-create"], ["remote-start"], ["remote-status"]):
        return remote(sys.argv[1].removeprefix("remote-"))
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "action", nargs="?", choices=["plan", "create", "start", "status"], default="plan"
    )
    cli.add_argument("--plan-sha256")
    cli.add_argument("--receipt", type=Path)
    args = cli.parse_args()
    built = build_plan()
    plan, _, orig = built[:3]
    approved = sha(orig.canonical(plan))
    if args.action == "plan":
        print(json.dumps({**plan, "plan_sha256": approved}, sort_keys=True))
        return 0
    need(args.plan_sha256 == approved and args.receipt is not None, "PLAN_O_RECIBO")
    out = args.receipt.absolute()
    need(out.parent.resolve(strict=True) == out.parent, "RECIBO_RUTA")
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    need(0 < len(raw) <= 128 * 1024, "PETICION_LOCAL_LIMITE")
    data = json.loads(raw)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as evidence:
        try:
            result = perform(args.action, approved, data, built)
        except Exception as error:
            result = {
                "action": args.action,
                "success": False,
                "plan_sha256": approved,
                "transport_error": type(error).__name__,
            }
        evidence.write(orig.canonical(result))
        evidence.flush()
        os.fsync(evidence.fileno())
    sys.stdout.buffer.write(orig.canonical(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
