"""Conserva el plan local del operador WASM para la imagen SDK descartada."""

import argparse
import base64
import hashlib
import io
import json
import os
import signal
import struct
import subprocess
import sys
import tarfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType

OWNER = "agencia-digital-marketing-ai:n8n-server-wasm-sdk:v1"
ROOT = Path("/opt/media-platform/n8n-server-wasm-sdk-v1")
CLIENT_V1 = "c520745f9b2fc31d33c54a19bbaf30f67203833e4521aeab9a81a07f4e9f32ce"
REMOTE_V1 = "925fd3bdb7a24549557050d54ff0a97e2a710eff632c3d1f81efc506e58a9be6"
METADATA = "0caa3838e229b04832040b2e0a07bb903d9ca6d98a4fe641f28d2fc13c41f8ac"
REJECTION_REPORT = "5da66419"
CAPSULE = "c4e0a34f048f53bd18fa2ee06b76616dc0319bc5f24d7489cb4afa3529ecc69a"
CAPSULE_BYTES = 9_689_036
MANIFEST = "sha256:40a30b0e22be2b1fce570a5fcbf8124c4ccb22962b49850546863da07d374a51"
IMAGE = "sha256:f42d26a27107adf3340b637bd53d6bedd3f8cf55e6fa45d505d843dbdbeea00f"
REF = "docker.io/emscripten/emsdk@" + MANIFEST
INPUTS = {
    "build-wa-sqlite-wasm-offline.sh": (
        2052,
        "f87eda4204e63be35c9219e5e3aa7df29a930774d26fa8972806a8283524a674",
    ),
    "context-linux-x64.tar.gz": (
        6946122,
        "066d2e04ea3c5da743853e27a047febb932cc89d77ea3f5c08c798b151187c7d",
    ),
    "stage_server_wasm_sdk.py": (
        7685,
        "57f04b592c1facb3cfc9babe4911bf33025bc0bfba96136a608fd81bf9708470",
    ),
    "wa-sqlite-rebuild-3.54.1.tar.gz": (
        2749039,
        "e9152893ca882710fa5e4c494a7a19356391b214bc1c331f7d9b7b259785d413",
    ),
    "wasm-sdk-inputs.json": (
        727,
        "47ec7643687d48880669d844fe8fc1c646b29fc5a4ffa750b4d4518442224192",
    ),
}
STAGES = {"sqlite": 900, "quickjs": 1800}
TMPFS = "rw,nosuid,nodev,noexec,size=64m,uid=1000,gid=1000,mode=0700"
SQLITE_SOURCE_ID = (
    b"2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
)
QUICKJS_OLD = {
    "debug-asyncify": "e346c792a99a6a3a5886a1b173015b3a7839d6800a7378695eb49db1a470160c",
    "debug-sync": "4d2f9f8adf28c48ed273515b815e05ae9a698b54955f7348e951faf43b1febc3",
    "release-asyncify": "b790f3842eef48d154984cea48d35303b5c3c3696fedab4dc79f314bde005dba",
    "release-sync": "105c3bed22d457e43e3d1c3c1c6959fda62a8fe06f0fc8a985303c3a2be72232",
}

# Se ejecuta físicamente dentro del CID, antes de arrancar el stage autenticado.
BOOT = """import json, os, socket, sys
from pathlib import Path
def inspect_sdk(root):
    if root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("SDK_LAYOUT")
    def fail(error):
        raise error
    count = 0
    for _, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        count += len(dirs) + len(files)
        if count > 100000 or ".git" in dirs or ".git" in files:
            raise ValueError("SDK_GIT_O_LIMITE")
    return count
if __name__ == "__main__":
    if os.geteuid() != 1000 or os.getegid() != 1000 or {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise ValueError("SDK_AISLADO")
    nodes = inspect_sdk(Path("/emsdk/upstream/emscripten"))
    if nodes < 1:
        raise ValueError("SDK_VACIO")
    os.umask(0o077)
    with open("/work/.sdk-preflight.json", "x", encoding="ascii") as result:
        json.dump({"git_absent": True, "nodes": nodes, "stage": sys.argv[1]}, result, sort_keys=True)
        result.flush()
        os.fsync(result.fileno())
    exe = "/usr/bin/python3"
    os.execve(exe, [exe, "-I", "-B", "/inputs/stage_server_wasm_sdk.py", "--stage", sys.argv[1]], {"PATH":"/work/no-path", "LC_ALL":"C", "HOME":"/work"})
"""


def need(ok, code):
    if not ok:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(raw, digest, name, path):
    need(sha(raw) == digest, "FUENTE_PIN")
    value = ModuleType(name)
    value.__file__ = str(path)
    sys.modules[name] = value
    exec(compile(raw, str(path), "exec"), value.__dict__)
    return value


def name_for(stage):
    return "agency-n8n-wasm-" + stage + "-v1"


def paths(stage):
    control = ROOT / stage
    return control, control / "work"


def argv(stage):
    return [
        "-s",
        "TERM",
        "-k",
        "15",
        str(STAGES[stage]),
        "/usr/bin/python3",
        "-I",
        "-B",
        "-c",
        BOOT,
        stage,
    ]


def create_args(stage):
    control, work = paths(stage)
    return [
        "create",
        "--pull=never",
        "--platform=linux/amd64",
        "--name",
        name_for(stage),
        "--cidfile",
        (control / "cid").as_posix(),
        "--label",
        "agency.owner=" + OWNER,
        "--label",
        "agency.stage=" + stage,
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
        "--mount",
        f"type=bind,src={(ROOT / 'inputs').as_posix()},dst=/inputs,readonly",
        "--mount",
        f"type=bind,src={work.as_posix()},dst=/work",
        "--entrypoint",
        "/usr/bin/timeout",
        IMAGE,
        *argv(stage),
    ]


def image(docker, metadata):
    (row,) = json.loads(docker("image", "inspect", REF).stdout)
    need((row["Id"], row["Os"], row["Architecture"]) == (IMAGE, "linux", "amd64"), "SDK_IDENTIDAD")
    need(
        any(value in row["RepoDigests"] for value in (REF, REF.removeprefix("docker.io/")))
        and row["RootFS"]["Layers"] == metadata["rootfs_diff_ids"],
        "SDK_CAPAS",
    )


def inspect(base, docker, stage, cid):
    need(base.CID.fullmatch(cid), "CID_FORMATO")
    (row,) = json.loads(docker("container", "inspect", cid).stdout)
    cfg, host = row["Config"], row["HostConfig"]
    need(
        (row["Id"], row["Image"], row["Name"]) == (cid, IMAGE, "/" + name_for(stage))
        and cfg["Image"] == IMAGE
        and cfg["User"] == "1000:1000"
        and cfg["WorkingDir"] == "/work"
        and cfg["Labels"].get("agency.owner") == OWNER
        and cfg["Labels"].get("agency.stage") == stage
        and cfg["Entrypoint"] == ["/usr/bin/timeout"]
        and cfg["Cmd"] == argv(stage),
        "CID_IDENTIDAD",
    )
    need(
        host["NetworkMode"] == "none"
        and host["ReadonlyRootfs"]
        and host["Init"]
        and not (
            host["Privileged"]
            or host["PortBindings"]
            or host["PublishAllPorts"]
            or host["AutoRemove"]
            or host["CapAdd"]
        )
        and host["CapDrop"] == ["ALL"]
        and host["SecurityOpt"] == ["no-new-privileges:true"]
        and host["PidsLimit"] == 128
        and host["Memory"] == host["MemorySwap"] == 5 * 1024**3
        and host["NanoCpus"] == 2 * 10**9
        and host["RestartPolicy"]["Name"] == "no"
        and host["Tmpfs"] == {"/tmp": TMPFS},
        "CID_LIMITES",
    )
    mounts = row["Mounts"]
    need(
        len(mounts) == 2
        and all(item["Type"] == "bind" for item in mounts)
        and {item["Destination"]: (Path(item["Source"]), item["RW"]) for item in mounts}
        == {"/inputs": (ROOT / "inputs", False), "/work": (paths(stage)[1], True)},
        "CID_BINDS",
    )
    return row["State"]


def members(raw):
    need(len(raw) == CAPSULE_BYTES and sha(raw) == CAPSULE, "CAPSULA_PIN")
    result = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as archive:
        for row in archive:
            need(
                row.name in INPUTS
                and row.name not in result
                and row.type == tarfile.REGTYPE
                and row.mode == 0o644,
                "CAPSULA_MIEMBRO",
            )
            size, digest = INPUTS[row.name]
            need(row.size == size, "CAPSULA_TAMANO")
            with archive.extractfile(row) as stream:
                body = stream.read()
            need(len(body) == size and sha(body) == digest, "CAPSULA_SHA")
            result[row.name] = body
    need(result.keys() == INPUTS.keys(), "CAPSULA_COBERTURA")
    return result


def read_exact(stream, size):
    raw = stream.read(size)
    need(len(raw) == size, "ENTRADA_TRUNCADA")
    return raw


def receive(base, reader, incoming, out):
    need(not os.path.lexists(ROOT), "RECEPCION_YA_EXISTE")
    fs = os.statvfs(ROOT.parent)
    need(fs.f_bavail * fs.f_frsize >= 64 * 1024**2 and fs.f_favail >= 100, "ESPACIO_RECEPCION")
    sys.stderr.buffer.write(base.READY)
    sys.stderr.buffer.flush()
    files = members(read_exact(incoming, CAPSULE_BYTES))
    need(not incoming.read(1), "CAPSULA_TRAILER")
    ROOT.mkdir(mode=0o700)
    root_id = base.directory(ROOT, 0, 0o700)
    base.publish(reader, ROOT / ".agency-owner", base.canonical({"owner": OWNER, **root_id}))
    directory = ROOT / "inputs"
    directory.mkdir(mode=0o700)
    fd = reader.directory(None, directory)
    try:
        output = reader.Output(fd)
        for name, body in sorted(files.items()):
            need(
                output.copy((name,), io.BytesIO(body), len(body), False) == INPUTS[name][1],
                "PUBLICACION_SHA",
            )
        output.seal()
    finally:
        os.close(fd)
    proof = {
        "owner": OWNER,
        **root_id,
        "inputs": base.directory(directory, 0, 0o555),
        "archive_sha256": CAPSULE,
        "plan_sha256": out["plan_sha256"],
    }
    base.publish(reader, ROOT / "received.json", base.canonical(proof))
    out["received"] = proof


def verify_received(base, proof, plan):
    need(
        proof["owner"] == OWNER
        and proof["archive_sha256"] == CAPSULE
        and proof["plan_sha256"] == plan
        and base.read_leaf(ROOT / "received.json") == base.canonical(proof),
        "RECIBO_RECEPCION",
    )
    identity = base.directory(ROOT, 0, 0o700)
    need(
        identity == {key: proof[key] for key in ("device", "inode")}
        and base.read_leaf(ROOT / ".agency-owner") == base.canonical({"owner": OWNER, **identity})
        and base.directory(ROOT / "inputs", 0, 0o555) == proof["inputs"],
        "RECEPCION_IDENTIDAD",
    )
    need({path.name for path in (ROOT / "inputs").iterdir()} == INPUTS.keys(), "ENTRADAS_COBERTURA")
    for name, (size, digest) in INPUTS.items():
        data = base.read_leaf(ROOT / "inputs" / name, limit=size)
        need(len(data) == size and sha(data) == digest, "ENTRADA_SHA")


def output_paths(stage):
    if stage == "sqlite":
        return {
            f"wa-sqlite/dist/wa-sqlite{variant}.{ext}": None
            for variant in ("", "-async", "-jspi")
            for ext in ("mjs", "wasm")
        }
    return {
        f"workspace/packages/variant-quickjs-wasmfile-{variant}/dist/emscripten-module.{ext}": old
        if ext == "wasm"
        else None
        for variant, old in QUICKJS_OLD.items()
        for ext in (
            "wasm",
            "mjs",
            "cjs",
            "browser.mjs",
            "cloudflare.cjs",
            "d.ts",
            "browser.d.ts",
            "cloudflare.d.ts",
        )
    }


def outputs(base, stage):
    _, work = paths(stage)
    preflight = json.loads(base.read_leaf(work / ".sdk-preflight.json", uid=1000, limit=1024))
    need(
        preflight.keys() == {"git_absent", "nodes", "stage"}
        and preflight["git_absent"] is True
        and preflight["stage"] == stage
        and type(preflight["nodes"]) is int
        and 0 < preflight["nodes"] <= 100000,
        "SDK_PREFLIGHT",
    )
    wanted = output_paths(stage)
    for folder in {str(Path(name).parent) for name in wanted}:
        directory = work / folder
        need(directory.resolve(strict=True) == directory and directory.is_dir(), "SALIDA_RUTA")
        present = {entry.name for entry in directory.iterdir()}
        expected = {Path(name).name for name in wanted if str(Path(name).parent) == folder}
        extra = present - expected
        allowed = (
            {"emscripten-module.wasm.map"} if stage == "quickjs" and "-debug-" in folder else set()
        )
        need(expected <= present and extra <= allowed, "SALIDA_COBERTURA")
        for leaf in extra:
            wanted[(Path(folder) / leaf).as_posix()] = None
    results = {}
    for name, old in sorted(wanted.items()):
        raw = base.read_leaf(work / name, uid=1000, limit=8 * 1024**2)
        digest = sha(raw)
        if name.endswith(".wasm"):
            need(raw.startswith(b"\0asm\1\0\0\0") and digest != old, "WASM_SALIDA")
            if stage == "sqlite":
                need(SQLITE_SOURCE_ID + b"\0" in raw and b"3.54.1\0" in raw, "SQLITE_SALIDA")
        results[name] = {"bytes": len(raw), "sha256": digest}
    return {"sdk_preflight": preflight, "outputs": results}


def verified_create(base, stage, prior, plan):
    control, work = paths(stage)
    need(
        prior["success"] is True
        and prior["action"] == "create"
        and prior["stage"] == stage
        and prior["plan_sha256"] == plan
        and prior["image"] == IMAGE
        and base.read_leaf(control / "create.json") == base.canonical(prior),
        "CREATE_NO_RESPALDADO",
    )
    need(
        base.directory(control, 0, 0o700) == prior["control"]
        and base.directory(work, 1000, 0o700) == prior["work"]
        and base.read_leaf(control / "cid", limit=80).decode().strip() == prior["cid"],
        "CID_FISICO",
    )


def lifecycle(base, reader, docker, action, stage, request, out):
    verify_received(base, request["received"], out["plan_sha256"])
    control, work = paths(stage)
    if action == "create":
        approval = request["sdk_approval_sha256"]
        need(base.CID.fullmatch(approval), "SDK_APROBACION")
        out["sdk_approval_sha256"] = approval
        if stage == "quickjs":
            previous = request["sqlite_status"]
            prior = json.loads(base.read_leaf(paths("sqlite")[0] / "create.json"))
            verified_create(base, "sqlite", prior, out["plan_sha256"])
            state = inspect(base, docker, "sqlite", prior["cid"])
            need(
                previous["stage"] == "sqlite"
                and previous["cid"] == prior["cid"]
                and previous["wasm_built"] is True
                and previous["sdk_approval_sha256"] == prior["sdk_approval_sha256"] == approval
                and state["Status"] == "exited"
                and state["Pid"] == state["ExitCode"] == 0
                and all(previous[key] == value for key, value in outputs(base, "sqlite").items()),
                "SQLITE_PREREQUISITO",
            )
        fs = os.statvfs(ROOT)
        need(fs.f_bavail * fs.f_frsize >= 1024**3 and fs.f_favail >= 10000, "ESPACIO_INSUFICIENTE")
        need(
            not docker("ps", "-aq", "--filter", "name=^/" + name_for(stage) + "$").stdout.strip(),
            "NOMBRE_OCUPADO",
        )
        control.mkdir(mode=0o700)
        out["control"] = base.directory(control, 0, 0o700)
        work.mkdir(mode=0o700)
        os.chown(work, 1000, 1000, follow_symlinks=False)
        out["work"] = base.directory(work, 1000, 0o700)
        base.publish(reader, control / "create-intent.json", base.canonical(out))
        try:
            result = docker(*create_args(stage), timeout=45)
        finally:
            if (control / "cid").exists():
                cid = base.read_leaf(control / "cid", limit=80).decode().strip()
                if base.CID.fullmatch(cid):
                    out["cid"] = cid
        need(result.stdout.decode().strip() == out.get("cid"), "CID_CREATE")
        state = inspect(base, docker, stage, out["cid"])
        need(state["Status"] == "created" and state["Pid"] == 0, "CID_ESTADO")
        return
    prior = request["created"]
    verified_create(base, stage, prior, out["plan_sha256"])
    cid = prior["cid"]
    out.update(cid=cid, sdk_approval_sha256=prior["sdk_approval_sha256"])
    state = inspect(base, docker, stage, cid)
    if action == "start":
        need(
            state["Status"] == "created" and state["Pid"] == 0 and not list(work.iterdir()),
            "START_NO_REPLAY",
        )
        fs = os.statvfs(work)
        need(fs.f_bavail * fs.f_frsize >= 1024**3 and fs.f_favail >= 10000, "ESPACIO_INSUFICIENTE")
        base.publish(reader, control / "start-intent.json", base.canonical(out))
        need(docker("start", cid, timeout=35).stdout.decode().strip() == cid, "START_CID")
        state = inspect(base, docker, stage, cid)
    out.update(
        status=state["Status"],
        pid=state["Pid"],
        exit=state["ExitCode"],
        oom=state["OOMKilled"],
        started_at=state["StartedAt"],
        finished_at=state["FinishedAt"],
        wasm_built=False,
    )
    if state["Status"] == "exited" and state["ExitCode"] == 0:
        out.update(outputs(base, stage), wasm_built=True)
    elif state["Status"] == "exited":
        log = docker("logs", "--tail", "28", cid, timeout=15, check=False)
        raw = log.stdout + log.stderr
        need(len(raw) <= 64 * 1024, "LOG_LIMITE")
        out.update(log_sha256=sha(raw), tail=raw[-3000:].decode("utf-8", "replace"))


def expire(*_):
    raise TimeoutError("PLAZO_REMOTO")


def remote(action):
    need(False, "SDK_OFICIAL_RECHAZADO")
    os.umask(0o077)
    signal.signal(signal.SIGALRM, expire)
    signal.alarm(160)
    out = {
        "action": action,
        "owner": OWNER,
        "image": IMAGE,
        "success": False,
        "production_approved": False,
    }
    base = reader = stage = None
    incoming = sys.stdin.buffer
    try:
        need(os.geteuid() == os.getegid() == 0 and os.uname().nodename == "vm-media-01", "HOST")
        size = struct.unpack(">I", read_exact(incoming, 4))[0]
        need(0 < size <= 256 * 1024, "PETICION_LIMITE")
        request = json.loads(read_exact(incoming, size))
        stage = request["stage"]
        need(
            request["action"] == action
            and (
                (action == "receive" and stage is None) or (action != "receive" and stage in STAGES)
            ),
            "ACCION",
        )
        base = load(
            base64.b64decode(request["base"], validate=True),
            REMOTE_V1,
            "n8n_wasm_remote_base",
            "<n8n-baseline-remote>",
        )
        need(base.CID.fullmatch(request["plan_sha256"]), "PLAN")
        out.update(stage=stage, plan_sha256=request["plan_sha256"])
        reader = base.pinned_module(
            "receive-n8n-js-linux-transfer-v1.py",
            base64.b64decode(request["reader"], validate=True),
        )
        docker = base.docker_helper(base64.b64decode(request["docker"], validate=True))
        metadata = base64.b64decode(request["metadata"], validate=True)
        need(sha(metadata) == METADATA, "METADATA_PIN")
        need(base.directory(base.PARENT, 0, 0o700) == request["parent"], "PARENT_IDENTIDAD")
        base.verify_daemon(docker, request["daemon"])
        if action == "receive":
            receive(base, reader, incoming, out)
        else:
            need(not incoming.read(1), "PETICION_TRAILER")
            image(docker, json.loads(metadata))
            lifecycle(base, reader, docker, action, stage, request, out)
            base.verify_daemon(docker, request["daemon"])
        out["success"] = True
        if action in {"create", "start"}:
            base.publish(reader, paths(stage)[0] / (action + ".json"), base.canonical(out))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        out.update(success=False, error=type(error).__name__)
        code = str(error)
        if (
            0 < len(code) <= 48
            and code.isascii()
            and code.isupper()
            and code.replace("_", "").isalnum()
        ):
            out["code"] = code
        if base is not None and reader is not None and action == "create" and "control" in out:
            try:
                base.publish(reader, paths(stage)[0] / "create-failure.json", base.canonical(out))
            except OSError:
                out["failure_receipt_written"] = False
    finally:
        signal.alarm(0)
    print(json.dumps(out, sort_keys=True, separators=(",", ":")))
    return 0 if out["success"] else 1


def local_plan():
    here = Path(__file__).resolve().parent
    path = here / "n8n_server_baseline_vm.py"
    client = load(path.read_bytes(), CLIENT_V1, "n8n_wasm_client_base", path)
    base_path = here / "server_baseline_vm_remote.py"
    raw = client.source(base_path)
    base = load(raw, REMOTE_V1, "n8n_wasm_client_remote", base_path)
    metadata = client.source(client.EVIDENCE / "phase4-n8n-emscripten-sdk-metadata-v1.json")
    need(sha(metadata) == METADATA, "METADATA_PIN")
    source = client.source(Path(__file__).resolve())
    helpers = {
        key: client.source(client.EVIDENCE / filename)
        for key, filename in (
            ("reader", "receive-n8n-js-linux-transfer-v1.py"),
            ("docker", "n8n_js_store_container_v1.py"),
        )
    }
    plan = {
        "schema": 1,
        "target": "192.168.0.85",
        "owner": OWNER,
        "remote_root": ROOT.as_posix(),
        "source_sha256": sha(source),
        "metadata_sha256": METADATA,
        "manifest": MANIFEST,
        "image": IMAGE,
        "capsule_sha256": CAPSULE,
        "capsule_bytes": CAPSULE_BYTES,
        "transport_client_sha256": CLIENT_V1,
        "transport_remote_sha256": REMOTE_V1,
        "inputs": INPUTS,
        "stages": STAGES,
        "ready": False,
        "sdk_rejection_report_sha256_prefix": REJECTION_REPORT,
        "production_approved": False,
    }
    payload = {
        key: base64.b64encode(value).decode()
        for key, value in {"base": raw, "metadata": metadata, **helpers}.items()
    }
    return plan, client, base, source, payload


def apply(action, stage, request, plan_sha, local):
    need(False, "SDK_OFICIAL_RECHAZADO")
    plan, client, base, source, payload = local
    fields = {"ssh", "parent", "daemon"}
    if action != "receive":
        fields.add("received")
    if action == "create":
        fields.add("sdk_approval_sha256")
        if stage == "quickjs":
            fields.add("sqlite_status")
    if action in {"start", "status"}:
        fields.add("created")
    need(request.keys() == fields and request["ssh"].keys() == client.SSH_KEYS, "PETICION_CAMPOS")
    capsule = None
    if action == "receive":
        path = client.ARCHIVE.parents[1] / "n8n-server-wasm-sdk-v1.tar.gz"
        capsule = client.source(path, CAPSULE_BYTES)
        members(capsule)
    dto = {key: value for key, value in request.items() if key != "ssh"}
    dto.update(payload, action=action, stage=stage, plan_sha256=plan_sha)
    packet = client.framed(base.canonical(dto), 256 * 1024)
    sql = client.ROOT / "docker/postgres-immich/sql_acceptance_v2.py"
    need(
        sha(client.source(sql.with_name("sql_acceptance_remote_v2.py"), 64 * 1024))
        == client.SQL_CONTRACT,
        "TRANSPORTE_CONTRATO",
    )
    sdk = load(client.source(sql, 64 * 1024), client.SQL_TRANSPORT, "n8n_wasm_ssh", sql).sdk_local()
    with ExitStack() as stack:
        ssh = sdk.connect(request["ssh"])
        stack.callback(ssh.close)
        channel = ssh.get_transport().open_session(timeout=10)
        stack.callback(channel.close)
        channel.settimeout(10)
        deadline = time.monotonic() + 175
        channel.exec_command(client.remote_command("remote-" + action, plan["source_sha256"]))
        channel.sendall(client.framed(source, 32 * 1024))
        channel.sendall(packet)
        if capsule is not None and client.wait_ready(channel, base.READY, time.monotonic() + 45):
            for offset in range(0, len(capsule), 65536):
                need(time.monotonic() < deadline, "TRANSFER_PLAZO")
                channel.sendall(capsule[offset : offset + 65536])
        channel.shutdown_write()
        result = sdk.collect(channel, deadline, 64 * 1024)
    need(
        result["action"] == action
        and result["stage"] == stage
        and result["plan_sha256"] == plan_sha
        and result["owner"] == OWNER
        and result["image"] == IMAGE,
        "RECIBO_IDENTIDAD",
    )
    return result


def main():
    if len(sys.argv) == 2 and sys.argv[1] in {
        "remote-receive",
        "remote-create",
        "remote-start",
        "remote-status",
    }:
        return remote(sys.argv[1].removeprefix("remote-"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=["plan", "receive", "create", "start", "status"],
        default="plan",
    )
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    local = local_plan()
    plan, _, base = local[:3]
    plan_sha = sha(base.canonical(plan))
    if args.action == "plan":
        print(json.dumps({**plan, "plan_sha256": plan_sha}, sort_keys=True))
        return 0
    parser.error("SDK_OFICIAL_RECHAZADO")
    need(
        args.plan_sha256 == plan_sha
        and args.receipt is not None
        and (args.stage is None) == (args.action == "receive"),
        "PLAN_ETAPA_RECIBO",
    )
    path = args.receipt.absolute()
    need(path.parent.resolve(strict=True) == path.parent, "RECIBO_RUTA")
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    need(0 < len(raw) <= 128 * 1024, "PETICION_LOCAL_LIMITE")
    request = json.loads(raw)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as out:
        try:
            result = apply(args.action, args.stage, request, plan_sha, local)
        except Exception as error:
            result = {
                "action": args.action,
                "stage": args.stage,
                "plan_sha256": plan_sha,
                "success": False,
                "transport_error": type(error).__name__,
            }
        out.write(base.canonical(result))
        out.flush()
        os.fsync(out.fileno())
    sys.stdout.buffer.write(base.canonical(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
