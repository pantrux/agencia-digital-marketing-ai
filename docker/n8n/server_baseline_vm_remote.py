"""Recibe y opera un único contenedor de ensayo baseline de n8n en Linux."""

import ast
import base64
import hashlib
import io
import json
import os
import re
import signal
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PARENT = Path("/opt/media-platform")
DELTA = PARENT / "n8n-server-linux-transfer-v2"
V5 = PARENT / "n8n-js-linux-transfer-v1"
CONTROL = DELTA / "baseline-control-v1"
OPS = CONTROL / "ops"
WORK = CONTROL / "work"
OWNER = "agencia-digital-marketing-ai:n8n-server-baseline-vm:v1"
NAME = "agency-n8n-server-baseline-v1"
IMAGE = "sha256:d76f585fb0d5107dd47143da5e61554fd9cae656a30f3470f815cb8475769b6a"
LOCK = "f9cc9a03cd857a379c8e36d4a457c797cd4f07cb71dab711c9f554273055d516"
V5_MANIFEST = "6c60011a4168ae3524fe8c657b08250936e00643060a0149dc48868ac4a9fe02"
V5_RECEIPT = "7d1ab258feaeac2e2328202a595cfbb6bfc691dc491f47f5aa381b46a7b238a6"
SOURCES = {
    "receive-n8n-js-linux-transfer-v1.py": "f3ae4889a03f90ec989cff4b9c2b74dc4331916ccdb36d25738f427082aa9b2b",
    "receive_server_linux_baseline.py": "211fcd93220ea68fe7d4b4cfe9f43c343f2a59ead575aced9bb889cb4810d451",
    "seed_server_linux_baseline.py": "8137679311a2c3b9626abb2fa0959e589ae47ec94f4491897971793310039cda",
    "n8n-js-loopback-store-v1.py": "c8a3fde0022130f95961bef4931d16cce25155f18d7d2844290c2455c98bd36f",
    "n8n_js_store_container_v1.py": "e3979955193f2449861742e4fd5700c19393bce1901ad061adf0fdca5851cfbf",
}
OPS_FILES = {
    "receive_server_linux_baseline.py",
    "seed_server_linux_baseline.py",
    "n8n-js-loopback-store-v1.py",
}
ARGV = [
    "timeout",
    "-s",
    "TERM",
    "-k",
    "10",
    "2100",
    "/usr/local/bin/python3",
    "-I",
    "-B",
    "/ops/seed_server_linux_baseline.py",
    "--loopback-helper",
    "/ops/n8n-js-loopback-store-v1.py",
    "--seed",
]
MOUNTS = {
    "/inputs": (DELTA / "inputs", False),
    "/v5": (V5 / "inputs", False),
    "/ops": (OPS, False),
    "/work": (WORK, True),
}
TMPFS = "rw,nosuid,nodev,noexec,size=64m,uid=1000,gid=1000,mode=0700"
READY = b"RECEIVE_READY\n"
CID = re.compile(r"[a-f0-9]{64}\Z")
MAX_JSON = 256 * 1024
MAX_FILE = 32 * 1024


def need(value, code):
    if not value:
        raise ValueError(code)


def canonical(data):
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pinned_module(name, raw):
    need(0 < len(raw) <= MAX_FILE and sha(raw) == SOURCES[name], "FUENTE_PIN")
    module = ModuleType(name)
    exec(compile(raw, name, "exec"), module.__dict__)
    return module


def docker_helper(raw):
    name = "n8n_js_store_container_v1.py"
    need(0 < len(raw) <= MAX_FILE and sha(raw) == SOURCES[name], "DOCKER_HELPER_PIN")
    tree = ast.parse(raw)
    nodes = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name == "docker")
        or (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"D", "ENV"}
        )
    ]
    need(len(nodes) == 3, "DOCKER_HELPER_AST")
    scope = {"subprocess": subprocess}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), name, "exec"), scope)
    return scope["docker"]


def directory(path, uid, mode):
    node = path.lstat()
    need(
        path.resolve(strict=True) == path
        and stat.S_ISDIR(node.st_mode)
        and node.st_uid == node.st_gid == uid
        and stat.S_IMODE(node.st_mode) == mode,
        "DIRECTORIO_IDENTIDAD",
    )
    return {"device": node.st_dev, "inode": node.st_ino}


def read_leaf(path, uid=0, limit=MAX_JSON):
    need(path.resolve(strict=True) == path, "ARCHIVO_RUTA")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        node = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(node.st_mode)
            and node.st_nlink == 1
            and node.st_uid == node.st_gid == uid
            and not node.st_mode & 0o022
            and 0 < node.st_size <= limit,
            "ARCHIVO_IDENTIDAD",
        )
        raw = stream.read(node.st_size + 1)
    need(len(raw) == node.st_size, "ARCHIVO_TAMANO")
    return raw


def publish(reader, path, raw):
    fd = reader.directory(None, path.parent)
    try:
        reader.checked_dir(fd, mode=0o700)
        reader.publish(fd, path.name, raw)
    finally:
        os.close(fd)


def physical_receipt(root, expected):
    raw = read_leaf(root / "receipt.json")
    observed = json.loads(raw)
    need(
        observed == expected
        and raw == canonical(observed)
        and directory(root, 0, 0o700) == {key: observed[key] for key in ("device", "inode")}
        and directory(root / "inputs", 0, 0o555)
        == {key: observed["inputs_" + key] for key in ("device", "inode")}
        and read_leaf(root / ".agency-owner")
        == canonical({key: observed[key] for key in ("owner", "device", "inode")}),
        "RECEPCION_IDENTIDAD",
    )
    return observed


def verify_v5(expected):
    actual = physical_receipt(V5, expected)
    need(
        sha(canonical(actual)[:-1]) == V5_RECEIPT
        and actual["success"] is True
        and actual["manifest_sha256"] == V5_MANIFEST,
        "V5_RECIBO",
    )


def verify_daemon(docker, expected):
    fmt = '{"ID":{{json .ID}},"ServerVersion":{{json .ServerVersion}},"DockerRootDir":{{json .DockerRootDir}}}'
    need(
        set(expected) == {"ID", "ServerVersion", "DockerRootDir"} and all(expected.values()),
        "DAEMON_DTO",
    )
    need(json.loads(docker("info", "--format", fmt).stdout) == expected, "DAEMON_IDENTIDAD")


def create_args():
    args = [
        "create",
        "--pull=never",
        "--platform=linux/amd64",
        "--name",
        NAME,
        "--cidfile",
        (CONTROL / "cid").as_posix(),
        "--label",
        "agency.owner=" + OWNER,
        "--network=none",
        "--read-only",
        "--init",
        "--cap-drop=ALL",
        "--user=1000:1000",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=128",
        "--memory=5g",
        "--memory-swap=5g",
        "--cpus=2",
        "--restart=no",
        "--workdir=/work",
        "--tmpfs",
        "/tmp:" + TMPFS,
        "--entrypoint",
        "/bin/busybox",
    ]
    for target, (path, writable) in MOUNTS.items():
        args.extend(
            [
                "--mount",
                f"type=bind,src={path.as_posix()},dst={target}" + ("" if writable else ",readonly"),
            ]
        )
    return [*args, IMAGE, *ARGV]


def inspect(docker, cid):
    need(CID.fullmatch(cid), "CID_FORMATO")
    (row,) = json.loads(docker("container", "inspect", cid).stdout)
    cfg, host = row["Config"], row["HostConfig"]
    need(
        row["Id"] == cid
        and row["Image"] == IMAGE
        and row["Name"] == "/" + NAME
        and cfg["Image"] == IMAGE
        and cfg["Labels"].get("agency.owner") == OWNER
        and cfg["User"] == "1000:1000"
        and cfg["WorkingDir"] == "/work"
        and cfg["Entrypoint"] == ["/bin/busybox"]
        and cfg["Cmd"] == ARGV,
        "CONTENEDOR_IDENTIDAD",
    )
    need(
        host["NetworkMode"] == "none"
        and host["ReadonlyRootfs"]
        and host["Init"]
        and not host["Privileged"]
        and not host["PortBindings"]
        and not host["PublishAllPorts"]
        and not host["AutoRemove"]
        and host["CapDrop"] == ["ALL"]
        and not host["CapAdd"]
        and host["SecurityOpt"] == ["no-new-privileges:true"]
        and host["PidsLimit"] == 128
        and host["Memory"] == host["MemorySwap"] == 5 * 1024**3
        and host["NanoCpus"] == 2 * 10**9
        and host["RestartPolicy"]["Name"] == "no"
        and host["Tmpfs"] == {"/tmp": TMPFS},
        "CONTENEDOR_LIMITES",
    )
    bindings = row["Mounts"]
    need(
        len(bindings) == len(MOUNTS)
        and all(item["Type"] == "bind" for item in bindings)
        and {item["Destination"]: (Path(item["Source"]), item["RW"]) for item in bindings}
        == MOUNTS,
        "CONTENEDOR_BINDS",
    )
    return row["State"]


def prepare(reader, receiver, docker, files, out):
    receiver.storage(DELTA, 5 * 1024**3, 350_000)
    (image,) = json.loads(docker("image", "inspect", IMAGE).stdout)
    need(
        (image["Id"], image["Os"], image["Architecture"]) == (IMAGE, "linux", "amd64"),
        "SDK_IDENTIDAD",
    )
    need(
        not docker("ps", "-aq", "--filter", "name=^/" + NAME + "$").stdout.strip(), "NOMBRE_OCUPADO"
    )
    CONTROL.mkdir(mode=0o700)
    out["control"] = directory(CONTROL, 0, 0o700)
    publish(reader, CONTROL / ".agency-owner", canonical({"owner": OWNER, **out["control"]}))
    OPS.mkdir(mode=0o700)
    fd = reader.directory(None, OPS)
    try:
        output = reader.Output(fd)
        for name in sorted(OPS_FILES):
            raw = files[name]
            need(output.copy((name,), io.BytesIO(raw), len(raw), False) == SOURCES[name], "OPS_SHA")
        output.seal()
    finally:
        os.close(fd)
    out["ops"] = directory(OPS, 0, 0o555)
    WORK.mkdir(mode=0o700)
    os.chown(WORK, 1000, 1000, follow_symlinks=False)
    out["work"] = directory(WORK, 1000, 0o700)
    publish(reader, CONTROL / "create-intent.json", canonical(out))
    try:
        response = docker(*create_args(), timeout=40)
    finally:
        cid_path = CONTROL / "cid"
        if cid_path.exists():
            cid = read_leaf(cid_path, limit=80).decode("ascii").strip()
            if CID.fullmatch(cid):
                out["cid"] = cid
    if "cid" not in out:
        cid = response.stdout.decode("ascii").strip()
        if CID.fullmatch(cid):
            out["cid"] = cid
    need(response.stdout.decode("ascii").strip() == out.get("cid"), "CID_CREATE")
    state = inspect(docker, out["cid"])
    need(state["Status"] == "created" and state["Pid"] == 0, "CID_YA_ACTIVO")


def observe(action, reader, receiver, docker, request, out):
    prior = request["created"]
    need(
        prior["action"] == "create"
        and prior["success"] is True
        and prior["plan_sha256"] == out["plan_sha256"]
        and prior["image"] == IMAGE
        and read_leaf(CONTROL / "create.json") == canonical(prior),
        "CREATE_NO_RESPALDADO",
    )
    for path, key, uid, mode in (
        (CONTROL, "control", 0, 0o700),
        (OPS, "ops", 0, 0o555),
        (WORK, "work", 1000, 0o700),
    ):
        need(directory(path, uid, mode) == prior[key], "CONTROL_IDENTIDAD")
    need(
        read_leaf(CONTROL / ".agency-owner") == canonical({"owner": OWNER, **prior["control"]}),
        "CONTROL_MARCA",
    )
    need(
        all(sha(read_leaf(OPS / name, limit=MAX_FILE)) == SOURCES[name] for name in OPS_FILES),
        "OPS_SHA",
    )
    cid = prior["cid"]
    out["cid"] = cid
    need(read_leaf(CONTROL / "cid", limit=80).decode("ascii").strip() == cid, "CID_FISICO")
    state = inspect(docker, cid)
    run = WORK / "baseline-seed-v1"
    if action == "start":
        need(
            state["Status"] == "created" and state["Pid"] == 0 and not os.path.lexists(run),
            "START_NO_REPLAY",
        )
        receiver.storage(WORK, 5 * 1024**3, 350_000)
        publish(reader, CONTROL / "start-intent.json", canonical(out))
        need(docker("start", cid, timeout=30).stdout.decode().strip() == cid, "START_CID")
        state = inspect(docker, cid)
    out.update(
        status=state["Status"],
        pid=state["Pid"],
        exit=state["ExitCode"],
        oom=state["OOMKilled"],
        started_at=state["StartedAt"],
        finished_at=state["FinishedAt"],
        baseline_verified=False,
    )
    if state["Status"] == "exited" and state["ExitCode"] == 0:
        proof = directory(run, 1000, 0o700)
        marker = json.loads(read_leaf(run / ".agency-owner", uid=1000))
        raw = read_leaf(run / "receipt.json", uid=1000, limit=64 * 1024)
        result = json.loads(raw)
        need(
            marker == {"owner": "agencia-digital-marketing-ai:n8n-server-baseline-seed:v1", **proof}
            and all(result[key] == marker[key] for key in marker)
            and result["scope"] == "server-reference-offline-trial"
            and result["lock_sha256"] == LOCK
            and result["external_v5_verified"] == 1449
            and result["delta_verified"] == 1693
            and result["offline"]["downloaded"] == 0
            and result["offline"]["lock_unchanged"] is True
            and result["compiled"] is result["production_approved"] is False,
            "BASELINE_RECIBO",
        )
        out.update(baseline_verified=True, baseline=result, baseline_sha256=sha(raw))
    elif state["Status"] == "exited":
        logs = docker("logs", "--tail", "24", cid, timeout=15, check=False)
        if logs.returncode == 0:
            raw = logs.stdout + logs.stderr
            need(len(raw) <= 64 * 1024, "LOG_SALIDA_LIMITE")
            out.update(
                log_sha256=sha(raw), log_bytes=len(raw), tail=raw[-3500:].decode("utf-8", "replace")
            )


def request_json(stream):
    header = stream.read(4)
    need(len(header) == 4, "PETICION_TRUNCADA")
    size = struct.unpack(">I", header)[0]
    need(0 < size <= MAX_JSON, "PETICION_LIMITE")
    raw = stream.read(size)
    need(len(raw) == size, "PETICION_TRUNCADA")
    return json.loads(raw)


def expire(*_):
    raise TimeoutError("PLAZO_REMOTO")


def main():
    action = sys.argv[1]
    out = {"action": action, "owner": OWNER, "image": IMAGE, "success": False}
    reader = None
    os.umask(0o077)
    signal.signal(signal.SIGALRM, expire)
    signal.alarm(900 if action == "receive" else 120)
    try:
        need(action in {"receive", "create", "start", "status"}, "ACCION")
        need(os.geteuid() == os.getegid() == 0 and os.uname().nodename == "vm-media-01", "HOST")
        request = request_json(sys.stdin.buffer)
        need(request["action"] == action and CID.fullmatch(request["plan_sha256"]), "PLAN")
        out["plan_sha256"] = request["plan_sha256"]
        files = {
            name: base64.b64decode(raw, validate=True) for name, raw in request["sources"].items()
        }
        need(
            set(files) == SOURCES.keys()
            and all(sha(raw) == SOURCES[name] for name, raw in files.items()),
            "FUENTES",
        )
        reader = pinned_module(
            "receive-n8n-js-linux-transfer-v1.py", files["receive-n8n-js-linux-transfer-v1.py"]
        )
        receiver = pinned_module(
            "receive_server_linux_baseline.py", files["receive_server_linux_baseline.py"]
        )
        docker = docker_helper(files["n8n_js_store_container_v1.py"])
        need(directory(PARENT, 0, 0o700) == request["parent"], "PARENT_IDENTIDAD")
        verify_v5(request["v5"])
        verify_daemon(docker, request["daemon"])
        if action == "receive":
            need(not os.path.lexists(DELTA), "RECEPCION_YA_EXISTE")
            receiver.storage(PARENT, receiver.MIN_BYTES, receiver.MIN_INODES)
            sys.stderr.buffer.write(READY)
            sys.stderr.buffer.flush()
            out["received"] = receiver.receive(reader)
        else:
            received = physical_receipt(DELTA, request["received"])
            need(
                received["received"] is True
                and received["archive_sha256"] == receiver.ARCHIVE
                and received["manifest_sha256"] == receiver.MANIFEST,
                "DELTA_RECIBO",
            )
            need(not sys.stdin.buffer.read(1), "PETICION_TRAILER")
            if action == "create":
                prepare(reader, receiver, docker, files, out)
            else:
                observe(action, reader, receiver, docker, request, out)
            verify_daemon(docker, request["daemon"])
        out["success"] = True
        if action in {"create", "start"}:
            publish(reader, CONTROL / (action + ".json"), canonical(out))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        out.update(success=False, error=type(error).__name__)
        if re.fullmatch(r"[A-Z_]{1,48}", str(error)):
            out["code"] = str(error)
        if reader is not None and action == "create" and "control" in out:
            try:
                publish(reader, CONTROL / "create-failure.json", canonical(out))
            except OSError:
                out["failure_receipt_written"] = False
    finally:
        signal.alarm(0)
    sys.stdout.buffer.write(canonical(out))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
