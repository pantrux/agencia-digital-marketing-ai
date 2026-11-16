"""Recibe el contexto comprimido y construye el SDK SQLite en dos etapas."""

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import stat
import struct
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType

ROOT = Path("/opt/media-platform/n8n-wa-sqlite-sdk-build-delta-v5")
OWNER = "agencia-digital-marketing-ai:n8n-wa-sqlite-sdk-build:delta-v5"
TAG = "agency-wa-sqlite-sdk:context-07d5f806"
ARCHIVE_BYTES = 17_382
ARCHIVE_SHA = "07d5f806068128a549c267e975bbff52978f83cdc01a2af12b11779be33486e6"
CONTEXT_RECEIPT = "2b3b531c6e7f02ad36c38598dc379fecdf9cf7ff472d9ddd0915a2f92c3879af"
BASE_TAG = "agency-wa-sqlite-sdk:context-27e8c084"
BASE_MANIFEST = "sha256:18d8493cc9a2252d9442f7b4249931a4680a59d7026cb63807945b7a9d04f301"
BASE_CONFIG = "sha256:946362b8b1d3af9a62c19c46826be39fc4c0544e4d760723c6aa08cb8eb91ea3"
BASE_LAYERS = [
    "sha256:9e3967480d7a726b96738207af8cb14c1afcf45c7ba5948f39ce61e18910cb0f",
    "sha256:fbd69478d5f51cbf7f53ff4e20361fa3a85bf0a74365ea5361722a9b3cc993ef",
]
RECEIVE_READY = b"RECEIVE_READY\n"
CLIENT_SHA = "c520745f9b2fc31d33c54a19bbaf30f67203833e4521aeab9a81a07f4e9f32ce"
SOURCES = {
    "baseline": (
        "docker/n8n/server_baseline_vm_remote.py",
        "925fd3bdb7a24549557050d54ff0a97e2a710eff632c3d1f81efc506e58a9be6",
    ),
    "reader": (
        ".tools/evidence/receive-n8n-js-linux-transfer-v1.py",
        "f3ae4889a03f90ec989cff4b9c2b74dc4331916ccdb36d25738f427082aa9b2b",
    ),
    "docker": (
        ".tools/evidence/n8n_js_store_container_v1.py",
        "e3979955193f2449861742e4fd5700c19393bce1901ad061adf0fdca5851cfbf",
    ),
    "runner": (
        "docker/python-runtime/build_runtime_image_remote_v3.py",
        "d9dd9175ed9a685235699b485b18c01112c7c48970979464cf45c62c8fa1232b",
    ),
    "capture": (
        "docker/valkey/capture_image_remote_v2.py",
        "515f17990013507d254f8f1555746bcce006191292bde10cb3a6f46b6cd69002",
    ),
    "ids": (
        "docker/python-runtime/runtime_image_inventory_v3.py",
        "cd320c4280fdd7b78ffa9ecc3c1b8cf9e8450af657ee8fb109e521b56c7a63b7",
    ),
}
ENTRYPOINT = [
    "/usr/bin/busybox",
    "timeout",
    "-s",
    "TERM",
    "-k",
    "15",
    "900",
    "/usr/bin/python3.12",
    "-I",
    "-S",
    "-B",
    "/opt/wa-sqlite/run_wa_sqlite_sdk.py",
]
MAX_FRAME = 512 * 1024
RECEIVE_SECONDS, BUILD_SECONDS = 300, 180


def need(ok, code):
    if not ok:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def load(raw, expected, name, path=None):
    need(0 < len(raw) <= 64 * 1024 and sha(raw) == expected, "HELPER_PIN")
    mod = ModuleType(name)
    mod.__file__ = str(path or name)
    exec(compile(raw, mod.__file__, "exec"), mod.__dict__)
    return mod


def verify_base(docker):
    (image,) = json.loads(docker("image", "inspect", BASE_TAG).stdout)
    cfg = image["Config"]
    desc = image.get("Descriptor") or {}
    need(
        image["Id"] == BASE_MANIFEST
        and desc.get("digest", BASE_MANIFEST) == BASE_MANIFEST
        and (desc.get("annotations") or {}).get("config.digest", BASE_CONFIG) == BASE_CONFIG
        and BASE_TAG in image["RepoTags"]
        and image["Os"] == "linux"
        and image["Architecture"] == "amd64"
        and image["RootFS"]["Layers"] == BASE_LAYERS
        and cfg["Labels"]["agency.owner"]
        == "agencia-digital-marketing-ai:n8n-wa-sqlite-sdk-build:v4"
        and cfg["User"] == "1000:1000"
        and cfg["WorkingDir"] == "/work"
        and cfg["Entrypoint"] == ENTRYPOINT,
        "BASE_DAEMON_IDENTIDAD",
    )
    return BASE_LAYERS


def check_host(base, docker, daemon):
    need(
        os.geteuid() == os.getegid() == 0
        and os.uname().nodename == "vm-media-01"
        and os.uname().machine == "x86_64",
        "HOST",
    )
    need(
        base.directory(ROOT.parent, 0, 0o700) == {"device": 2050, "inode": 798035}
        and sha(base.read_leaf(ROOT.parent / ".agencia-media-owner.json"))
        == "3de6ae0f965facffe0167c6005838a1d7026531eb3cce7e9ac8a613bbcd036c0"
        and sha(base.read_leaf(ROOT.parent / ".agencia-media-layout-receipt.json"))
        == "f20ab00f2a35a9e12654e2b5caabfcfa00a9338cb124a98a14c784b6bfa8e18f",
        "PARENT_LAYOUT",
    )
    need(
        daemon
        == {
            "ID": "64fe6c9f-1992-4444-a271-78880af05d02",
            "ServerVersion": "29.9.3",
            "DockerRootDir": "/var/lib/docker",
        },
        "DAEMON_PIN",
    )
    base.verify_daemon(docker, daemon)


def free(path, minimum, inodes):
    volume = os.statvfs(path)
    need(
        volume.f_bavail * volume.f_frsize >= minimum and volume.f_favail >= inodes, "ESPACIO_LIBRE"
    )


def copy_exact(incoming, out):
    remaining = ARCHIVE_BYTES
    digest = hashlib.sha256()
    while remaining:
        block = incoming.read(min(remaining, 1024**2))
        need(bool(block), "CONTEXTO_TRUNCADO")
        out.write(block)
        digest.update(block)
        remaining -= len(block)
    need(not incoming.read(1) and digest.hexdigest() == ARCHIVE_SHA, "CONTEXTO_PIN")


def receive(base, reader, incoming, result):
    free(ROOT.parent, 16 * 1024**2, 32)
    with ExitStack() as stack:
        parent = reader.directory(None, ROOT.parent)
        stack.callback(os.close, parent)
        node = reader.checked_dir(parent, mode=0o700)
        os.mkdir(ROOT.name, mode=0o700, dir_fd=parent)
        child = reader.directory(parent, ROOT.name)
        stack.callback(os.close, child)
        observed = reader.checked_dir(child, device=node.st_dev, mode=0o700)
        identity = {"device": observed.st_dev, "inode": observed.st_ino}
        result["directory"] = identity
        reader.publish(child, ".agency-owner.json", canonical({**identity, "owner": OWNER}))
        sys.stderr.buffer.write(RECEIVE_READY)
        sys.stderr.buffer.flush()
        fd = os.open(
            "context.tar.gz",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=child,
        )
        with os.fdopen(fd, "wb") as out:
            copy_exact(incoming, out)
            out.flush()
            os.fchmod(out.fileno(), 0o444)
            os.fsync(out.fileno())
        need(base.directory(ROOT, 0, 0o700) == identity, "RECEPCION_IDENTIDAD")
        receipt = {
            "schema": 1,
            "owner": OWNER,
            "directory": identity,
            "context_bytes": ARCHIVE_BYTES,
            "context_sha256": ARCHIVE_SHA,
            "plan_sha256": result["plan_sha256"],
            "received": True,
        }
        reader.publish(child, "received.json", canonical(receipt))
        result["received"] = receipt


def verify_receive(base, receipt, plan):
    identity = base.directory(ROOT, 0, 0o700)
    need(
        receipt
        == {
            "schema": 1,
            "owner": OWNER,
            "directory": identity,
            "context_bytes": ARCHIVE_BYTES,
            "context_sha256": ARCHIVE_SHA,
            "plan_sha256": plan,
            "received": True,
        }
        and base.read_leaf(ROOT / "received.json") == canonical(receipt)
        and base.read_leaf(ROOT / ".agency-owner.json") == canonical({**identity, "owner": OWNER}),
        "RECIBO_RECEPCION",
    )
    return identity


def build_arguments(path):
    return [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "buildx",
        "build",
        "--builder",
        "default",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--pull=false",
        "--no-cache",
        "--provenance=false",
        "--sbom=false",
        "--progress",
        "plain",
        "--label",
        f"agency.owner={OWNER}",
        "--tag",
        TAG,
        "--iidfile",
        str(path / "image-id.txt"),
        "--metadata-file",
        str(path / "metadata.json"),
        "--file",
        "Dockerfile",
        "--load",
        "-",
    ]


def observe_build(base, ids, stage, result, log=""):
    metadata, iid = {}, ""
    try:
        metadata = json.loads(base.read_leaf(stage / "metadata.json", limit=1024**2))
    except (OSError, ValueError):
        pass
    try:
        iid = base.read_leaf(stage / "image-id.txt", limit=128).decode("ascii").strip()
    except (OSError, ValueError):
        pass
    result["observed_ids"] = ids.observed_build_ids(metadata, iid, log)
    return metadata, iid


def build(base, reader, docker, modules, request, result):
    result["step"] = "receive-proof"
    identity = verify_receive(base, request["received"], result["plan_sha256"])
    result["directory"] = identity
    result["step"] = "base-oci"
    prefix = verify_base(docker)
    free(Path("/var/lib/docker"), 128 * 1024**2, 4096)
    need(
        re.search(rb"(^|\s)v0\.38\.1(\s|$)", docker("buildx", "version").stdout) is not None,
        "BUILDX_VERSION",
    )
    need(
        not docker(
            "image", "ls", "--filter", f"reference={TAG}", "--format", "{{.ID}}"
        ).stdout.strip(),
        "TAG_OCUPADO",
    )
    stage = ROOT / "build"
    stage.mkdir(mode=0o700)
    result["build_directory"] = base.directory(stage, 0, 0o700)
    private = stage / "docker-client"
    private.mkdir(mode=0o700)
    base.publish(reader, private / "config.json", b"{}")
    base.publish(
        reader,
        stage / "intent.json",
        canonical(
            {
                "owner": OWNER,
                "plan_sha256": result["plan_sha256"],
                "context_sha256": ARCHIVE_SHA,
                "base": BASE_MANIFEST,
                "tag": TAG,
            }
        ),
    )
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "DOCKER_CONFIG": str(private),
        "BUILDX_GIT_INFO": "false",
        "BUILDX_GIT_LABELS": "false",
        "BUILDX_GIT_CHECK_DIRTY": "false",
    }
    result["step"] = "buildx"
    fd = os.open(ROOT / "context.tar.gz", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        node = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(node.st_mode)
            and node.st_nlink == 1
            and node.st_uid == node.st_gid == 0
            and stat.S_IMODE(node.st_mode) == 0o444
            and node.st_size == ARCHIVE_BYTES
            and node.st_dev == identity["device"]
            and hashlib.file_digest(stream, "sha256").hexdigest() == ARCHIVE_SHA,
            "CONTEXTO_FISICO",
        )
        stream.seek(0)
        try:
            outcome = modules["runner"].run(
                build_arguments(stage),
                BUILD_SECONDS,
                modules["capture"],
                input_fd=stream.fileno(),
                environment=env,
            )
        except Exception:
            observe_build(base, modules["ids"], stage, result)
            raise
    log = outcome["stdout"] + outcome["stderr"]
    base.publish(reader, stage / "build.log", log)
    result.update(build_exit=outcome["returncode"], log_sha256=sha(log), log_bytes=len(log))
    result["step"] = "image-id"
    ids = modules["ids"]
    metadata, iid = observe_build(base, ids, stage, result, log.decode("utf-8", "replace"))
    need(
        outcome["returncode"] == 0 and b"COPY --chown=0:0 --chmod=0644 building.py" in log,
        "BUILDX_RESULTADO",
    )
    result.update(ids.build_identity(metadata, iid, log.decode("utf-8", "strict")))
    (inspection,) = json.loads(docker("image", "inspect", TAG).stdout)
    config = inspection["Config"]
    descriptor = inspection.get("Descriptor") or {}
    layers = inspection["RootFS"]["Layers"]
    need(
        inspection["Id"] in {result["manifest"], result["config"]}
        and descriptor.get("digest", result["manifest"]) == result["manifest"]
        and (descriptor.get("annotations") or {}).get("config.digest", result["config"])
        == result["config"]
        and TAG in inspection["RepoTags"]
        and inspection["Os"] == "linux"
        and inspection["Architecture"] == "amd64"
        and config["Labels"]["agency.owner"] == OWNER
        and config["User"] == "1000:1000"
        and config["WorkingDir"] == "/work"
        and config["Entrypoint"] == ENTRYPOINT
        and set(config["Env"])
        == {
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin",
            "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        }
        and len(layers) == len(prefix) + 1
        and layers[:-1] == prefix
        and layers[-1] not in prefix,
        "IMAGEN_FISICA",
    )
    base.verify_daemon(docker, request["daemon"])
    result.update(
        inspect_id=inspection["Id"],
        rootfs_diff_ids=layers,
        image_bytes=inspection["Size"],
        apk_packages=40,
        tag=TAG,
    )


def exact(stream, size):
    raw = stream.read(size)
    need(len(raw) == size, "TRAMA_TRUNCADA")
    return raw


def expired(*_args):
    raise TimeoutError("PLAZO_REMOTO")


def remote(action):
    os.umask(0o077)
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(RECEIVE_SECONDS if action == "receive" else BUILD_SECONDS + 35)
    result = {
        "action": action,
        "owner": OWNER,
        "success": False,
        "sdk_executed": False,
        "sca_approved": False,
    }
    modules = {}
    incoming = sys.stdin.buffer
    try:
        size = struct.unpack(">I", exact(incoming, 4))[0]
        need(0 < size <= MAX_FRAME, "TRAMA_LIMITE")
        request = json.loads(exact(incoming, size))
        fields = {"action", "daemon", "sources", "plan_sha256"}
        need(
            request.keys() == fields | ({"received"} if action == "build" else set()),
            "TRAMA_CAMPOS",
        )
        need(
            request["action"] == action and re.fullmatch(r"[a-f0-9]{64}", request["plan_sha256"]),
            "TRAMA_ACCION",
        )
        result["plan_sha256"] = request["plan_sha256"]
        need(request["sources"].keys() == SOURCES.keys(), "TRAMA_FUENTES")
        raws = {name: base64.b64decode(request["sources"][name], validate=True) for name in SOURCES}
        for name, raw in raws.items():
            if name != "docker":
                modules[name] = load(raw, SOURCES[name][1], f"wa_sdk_{name}")
        base, reader = modules["baseline"], modules["reader"]
        docker = base.docker_helper(raws["docker"])
        check_host(base, docker, request["daemon"])
        if action == "receive":
            receive(base, reader, incoming, result)
        else:
            need(not incoming.read(1), "TRAMA_EXTRA")
            build(base, reader, docker, modules, request, result)
        result["success"] = True
    except Exception as error:
        result["error_type"] = type(error).__name__
        if isinstance(error, (ValueError, TimeoutError)) and re.fullmatch(
            r"[A-Z0-9_]+", str(error)
        ):
            result["error_code"] = str(error)
    finally:
        signal.alarm(0)
    if action == "build" and "build_directory" in result:
        try:
            base.publish(reader, ROOT / "build/result.json", canonical(result))
        except Exception:
            result.update(success=False, result_published=False)
    sys.stdout.buffer.write(canonical(result))
    return 0 if result["success"] else 1


def local_plan():
    here = Path(__file__).resolve().parent
    repo = here.parents[1]
    path = here / "n8n_server_baseline_vm.py"
    client = load(path.read_bytes(), CLIENT_SHA, "wa_sdk_transport", path)
    raws = {name: client.source(repo / path, 64 * 1024) for name, (path, _) in SOURCES.items()}
    need(all(sha(raw) == SOURCES[name][1] for name, raw in raws.items()), "PLAN_HELPERS")
    source = client.source(Path(__file__).resolve())
    location = client.ARCHIVE.parents[1] / "emscripten-toolchain-v1/wa-sqlite-sdk-context-delta-v5"
    receipt = client.source(location / "context-receipt.json")
    need(sha(receipt) == CONTEXT_RECEIPT, "PLAN_CONTEXTO")
    plan = {
        "schema": 1,
        "owner": OWNER,
        "target": "192.168.0.85",
        "root": ROOT.as_posix(),
        "tag": TAG,
        "source_sha256": sha(source),
        "context_sha256": ARCHIVE_SHA,
        "context_bytes": ARCHIVE_BYTES,
        "context_receipt_sha256": CONTEXT_RECEIPT,
        "base_manifest": BASE_MANIFEST,
        "base_config": BASE_CONFIG,
        "base_tag": BASE_TAG,
        "base_layers": BASE_LAYERS,
        "new_layers": 1,
        "sources": {name: pin for name, (_, pin) in SOURCES.items()},
        "transport_sha256": CLIENT_SHA,
        "receive_seconds": RECEIVE_SECONDS,
        "build_seconds": BUILD_SECONDS,
        "sdk_executed": False,
        "sca_approved": False,
    }
    return plan, source, raws, location, client


def send_context(channel, stream, deadline):
    digest, remaining = hashlib.sha256(), ARCHIVE_BYTES
    while remaining:
        need(time.monotonic() < deadline, "CONTEXTO_PLAZO")
        chunk = stream.read(min(remaining, 65536))
        need(bool(chunk), "CONTEXTO_TRUNCADO")
        remaining -= len(chunk)
        digest.update(chunk)
        channel.sendall(chunk)
    need(not stream.read(1) and digest.hexdigest() == ARCHIVE_SHA, "CONTEXTO_PIN")


def operate(action, dto, approved, local):
    plan, source, raws, location, client = local
    fields = {"ssh", "daemon"} | ({"received"} if action == "build" else set())
    need(dto.keys() == fields and dto["ssh"].keys() == client.SSH_KEYS, "PETICION_CAMPOS")
    request = {key: value for key, value in dto.items() if key != "ssh"}
    request.update(
        action=action,
        plan_sha256=approved,
        sources={name: base64.b64encode(raw).decode("ascii") for name, raw in raws.items()},
    )
    packet = client.framed(canonical(request), MAX_FRAME)
    sql = client.ROOT / "docker/postgres-immich/sql_acceptance_v2.py"
    need(
        sha(client.source(sql.with_name("sql_acceptance_remote_v2.py"), 64 * 1024))
        == client.SQL_CONTRACT,
        "TRANSPORTE_CONTRATO",
    )
    sdk = load(client.source(sql, 64 * 1024), client.SQL_TRANSPORT, "wa_sdk_ssh", sql).sdk_local()
    with ExitStack() as stack:
        stream = None
        if action == "receive":
            path = location / "context.tar.gz"
            need(
                path.resolve(strict=True) == path and path.stat().st_size == ARCHIVE_BYTES,
                "CONTEXTO_RUTA",
            )
            stream = stack.enter_context(path.open("rb"))
            need(hashlib.file_digest(stream, "sha256").hexdigest() == ARCHIVE_SHA, "CONTEXTO_PIN")
            stream.seek(0)
        ssh = sdk.connect(dto["ssh"])
        stack.callback(ssh.close)
        channel = ssh.get_transport().open_session(timeout=10)
        stack.callback(channel.close)
        channel.settimeout(15)
        deadline = time.monotonic() + (RECEIVE_SECONDS + 20 if stream else BUILD_SECONDS + 50)
        channel.exec_command(client.remote_command("remote-" + action, plan["source_sha256"]))
        channel.sendall(client.framed(source, 32 * 1024))
        channel.sendall(packet)
        if stream is not None and client.wait_ready(channel, RECEIVE_READY, time.monotonic() + 40):
            send_context(channel, stream, deadline)
        channel.shutdown_write()
        result = sdk.collect(channel, deadline, 64 * 1024)
    need(
        result["owner"] == OWNER
        and result["action"] == action
        and result["plan_sha256"] == approved,
        "RECIBO_IDENTIDAD",
    )
    return result


def main():
    if len(sys.argv) == 2 and sys.argv[1] in {"remote-receive", "remote-build"}:
        return remote(sys.argv[1].removeprefix("remote-"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="plan", choices=["plan", "receive", "build"])
    parser.add_argument("--plan-sha256")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    local = local_plan()
    plan = local[0]
    approved = sha(canonical(plan))
    if args.action == "plan":
        sys.stdout.buffer.write(canonical({**plan, "plan_sha256": approved}))
        return 0
    need(args.plan_sha256 == approved and args.receipt is not None, "PLAN_RECIBO")
    destination = args.receipt.absolute()
    need(destination.parent.resolve(strict=True) == destination.parent, "RECIBO_RUTA")
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    need(0 < len(raw) <= 128 * 1024, "PETICION_LIMITE")
    request = json.loads(raw)
    fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    with os.fdopen(fd, "wb") as out:
        try:
            result = operate(args.action, request, approved, local)
        except Exception as error:
            result = {
                "action": args.action,
                "owner": OWNER,
                "plan_sha256": approved,
                "success": False,
                "transport_error": type(error).__name__,
            }
        out.write(canonical(result))
        out.flush()
        os.fsync(out.fileno())
    sys.stdout.buffer.write(canonical(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
