"""Acepta la compilación SQLite en un CID propio del SDK derivado."""

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import struct
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType

ROOT = Path("/opt/media-platform/n8n-wa-sqlite-acceptance-v4")
OWNER = "agencia-digital-marketing-ai:n8n-wa-sqlite-acceptance:v4"
NAME = "agency-n8n-wa-sqlite-acceptance-v4"
LEAF = "wa-sqlite-rebuild-3.54.1.tar.gz"
SOURCE_SHA = "e9152893ca882710fa5e4c494a7a19356391b214bc1c331f7d9b7b259785d413"
SOURCE_BYTES = 2_749_039
BUILDER = "4af74dbdec17159c7b0b7ba03910e12ae79e822c42e99e5b03f010a44475ac75"
BUILD_PLAN = "4ffaad455409a8aeeebdc0e12440fedb2b0fe6313333ac309c73a82e944affa6"
BUILD_RECEIPT = "e599b9c7b106d2c16ebec87aba47b4d768542b9147fe49ee631a3301aa79f7b5"
PROBE = "verify-wa-sqlite-sdk.mjs"
PROBE_SHA = "3da7d34969dd660aa0c3142dc70463fda30dc24d13ad24c2f23b099a4c707ea3"
NODE_SHA = "eaad0c458723e978cac0bd4393ca63d8b88255a5b7ee8b77a0731218d8ce53eb"
READY = b"RECEIVE_READY\n"
TMPFS = "rw,nosuid,nodev,noexec,size=64m,uid=1000,gid=1000,mode=0700"
VARIANTS = ("wa-sqlite", "wa-sqlite-async", "wa-sqlite-jspi")
SOURCE_ID = "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
STAGE = (
    "/usr/bin/python3.12 -I -S -B /opt/wa-sqlite/run_wa_sqlite_sdk.py"
    " && exec /usr/bin/node --experimental-wasm-jspi /inputs/" + PROBE
)
COMMAND = ["timeout", "-s", "TERM", "-k", "15", "900", "/bin/sh", "-eu", "-c", STAGE]
MOUNTS = {"/inputs": (ROOT / "inputs", False), "/work": (ROOT / "work", True)}
HEX = re.compile(r"[a-f0-9]{64}\Z")


def need(ok, code):
    if not ok:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def load(raw, digest, name, path=None):
    need(0 < len(raw) <= 64 * 1024 and sha(raw) == digest, "FUENTE_PIN")
    mod = ModuleType(name)
    mod.__file__ = str(path or name)
    exec(compile(raw, mod.__file__, "exec"), mod.__dict__)
    return mod


def approval_for(build, approval, approval_sha):
    subject = {name: build[name] for name in ("manifest", "config", "inspect_id")}
    need(
        build["success"] is True
        and build["action"] == "build"
        and build["plan_sha256"] == BUILD_PLAN
        and sha(canonical(build)) == BUILD_RECEIPT
        and build["apk_packages"] == 40
        and build["sdk_executed"] is False
        and all(re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in subject.values())
        and subject["inspect_id"] in {subject["manifest"], subject["config"]},
        "BUILD_IDENTIDAD",
    )
    need(
        isinstance(approval, dict)
        and approval.keys()
        == subject.keys()
        | {
            "schema",
            "build_sha256",
            "report_sha256",
            "coverage_sha256",
            "unresolved_high_critical_unknown",
            "authorization",
        }
        and type(approval["schema"]) is int
        and approval["schema"] == 1
        and all(approval[name] == value for name, value in subject.items())
        and approval["build_sha256"] == BUILD_RECEIPT
        and all(HEX.fullmatch(approval[name]) for name in ("report_sha256", "coverage_sha256"))
        and type(approval["unresolved_high_critical_unknown"]) is int
        and approval["unresolved_high_critical_unknown"] == 0
        and approval["authorization"] == "functional_test_only"
        and sha(canonical(approval)) == approval_sha,
        "SCA_APROBACION",
    )
    return subject


def checked_image(builder, base, docker, build, approval, approval_sha):
    subject = approval_for(build, approval, approval_sha)
    need(
        build["owner"] == builder.OWNER
        and build["tag"] == builder.TAG
        and base.directory(builder.ROOT, 0, 0o700) == build["directory"]
        and base.directory(builder.ROOT / "build", 0, 0o700) == build["build_directory"]
        and base.read_leaf(builder.ROOT / ".agency-owner.json")
        == canonical({**build["directory"], "owner": builder.OWNER})
        and base.read_leaf(builder.ROOT / "build/result.json") == canonical(build),
        "BUILD_RECIBO_FISICO",
    )
    (image,) = json.loads(docker("image", "inspect", subject["inspect_id"]).stdout)
    cfg = image["Config"]
    desc = image.get("Descriptor") or {}
    need(
        image["Id"] == subject["inspect_id"]
        and image["Os"] == "linux"
        and image["Architecture"] == "amd64"
        and builder.TAG in image["RepoTags"]
        and desc.get("digest", subject["manifest"]) == subject["manifest"]
        and (desc.get("annotations") or {}).get("config.digest", subject["config"])
        == subject["config"]
        and image["RootFS"]["Layers"] == build["rootfs_diff_ids"]
        and cfg["Labels"]["agency.owner"] == builder.OWNER
        and cfg["User"] == "1000:1000"
        and cfg["WorkingDir"] == "/work"
        and cfg["Entrypoint"] == builder.ENTRYPOINT,
        "SDK_DAEMON_IDENTIDAD",
    )
    return subject["inspect_id"]


def create_arguments(image):
    arguments = [
        "create",
        "--pull=never",
        "--platform",
        "linux/amd64",
        "--name",
        NAME,
        "--cidfile",
        str(ROOT / "cid"),
        "--label",
        f"agency.owner={OWNER}",
        "--network",
        "none",
        "--read-only",
        "--init",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "1000:1000",
        "--workdir",
        "/work",
        "--cpus",
        "2",
        "--memory",
        "5g",
        "--memory-swap",
        "5g",
        "--pids-limit",
        "128",
        "--restart",
        "no",
        "--tmpfs",
        f"/tmp:{TMPFS}",
        "--entrypoint",
        "/usr/bin/busybox",
    ]
    for target, (source, writable) in MOUNTS.items():
        arguments += [
            "--mount",
            f"type=bind,src={source},dst={target}" + ("" if writable else ",readonly"),
        ]
    return arguments + [image, *COMMAND]


def inspect(docker, cid, image):
    need(HEX.fullmatch(cid), "CID_PIN")
    (row,) = json.loads(docker("container", "inspect", cid).stdout)
    cfg, host, state = row["Config"], row["HostConfig"], row["State"]
    mounts = {x["Destination"]: (x["Type"], x["Source"], x["RW"]) for x in row["Mounts"]}
    need(
        row["Id"] == cid
        and row["Name"] == "/" + NAME
        and row["Image"] == image
        and cfg["Image"] == image
        and cfg["Labels"]["agency.owner"] == OWNER
        and cfg["User"] == "1000:1000"
        and cfg["WorkingDir"] == "/work"
        and cfg["Entrypoint"] == ["/usr/bin/busybox"]
        and cfg["Cmd"] == COMMAND
        and host["NetworkMode"] == "none"
        and not host["PortBindings"]
        and not host["PublishAllPorts"]
        and not host["AutoRemove"]
        and host["ReadonlyRootfs"] is True
        and host["Init"] is True
        and host["Privileged"] is False
        and host["CapDrop"] == ["ALL"]
        and not host.get("CapAdd")
        and host["SecurityOpt"] == ["no-new-privileges:true"]
        and host["RestartPolicy"]["Name"] == "no"
        and host["PidsLimit"] == 128
        and host["NanoCpus"] == 2_000_000_000
        and host["Memory"] == host["MemorySwap"] == 5 * 1024**3
        and host["Tmpfs"] == {"/tmp": TMPFS}
        and len(row["Mounts"]) == len(mounts) == 2
        and mounts == {target: ("bind", str(path), rw) for target, (path, rw) in MOUNTS.items()},
        "CONTENEDOR_IDENTIDAD",
    )
    return state


def exact(stream, size):
    data = stream.read(size)
    need(len(data) == size, "TRAMA_TRUNCADA")
    return data


def create(base, reader, docker, image, incoming, probe, request, out):
    need(
        not docker("container", "ls", "-aq", "--filter", f"name=^/{NAME}$").stdout.strip(),
        "NOMBRE_OCUPADO",
    )
    volume = os.statvfs(ROOT.parent)
    need(volume.f_bavail * volume.f_frsize >= 1024**3 and volume.f_favail >= 4096, "ESPACIO")
    ROOT.mkdir(mode=0o700)
    identity = base.directory(ROOT, 0, 0o700)
    out["directory"] = identity
    base.publish(reader, ROOT / ".agency-owner.json", canonical({**identity, "owner": OWNER}))
    sys.stderr.buffer.write(READY)
    sys.stderr.buffer.flush()
    source = exact(incoming, SOURCE_BYTES)
    need(not incoming.read(1) and sha(source) == SOURCE_SHA, "CONTEXTO_PIN")
    inputs, work = ROOT / "inputs", ROOT / "work"
    inputs.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    for name, raw in ((LEAF, source), (PROBE, probe)):
        path = inputs / name
        base.publish(reader, path, raw)
        path.chmod(0o444)
    inputs.chmod(0o555)
    os.chown(work, 1000, 1000)
    proof = {
        "schema": 1,
        "owner": OWNER,
        "directory": identity,
        "image": image,
        "inputs": base.directory(inputs, 0, 0o555),
        "work": base.directory(work, 1000, 0o700),
        "approval_sha256": request["approval_sha256"],
        "plan_sha256": request["plan_sha256"],
        "build_sha256": request["approval"]["build_sha256"],
        "source_sha256": SOURCE_SHA,
        "probe_sha256": sha(probe),
    }
    base.publish(reader, ROOT / "create-intent.json", canonical(proof))
    cid = docker(*create_arguments(image), timeout=35).stdout.decode("ascii").strip()
    need(HEX.fullmatch(cid), "CREATE_CID")
    out["observed_cid"] = cid
    need(base.read_leaf(ROOT / "cid", limit=128).decode().strip() == cid, "CID_FISICO")
    state = inspect(docker, cid, image)
    need(
        state["Status"] == "created" and state["Pid"] == 0 and state["Running"] is False,
        "CID_DETENIDO",
    )
    proof["cid"] = cid
    base.publish(reader, ROOT / "created.json", canonical(proof))
    out["created"] = proof


def verify_created(base, image, request, probe):
    proof = request["created"]
    identity = base.directory(ROOT, 0, 0o700)
    need(
        proof
        == {
            "schema": 1,
            "owner": OWNER,
            "directory": identity,
            "image": image,
            "inputs": base.directory(ROOT / "inputs", 0, 0o555),
            "work": base.directory(ROOT / "work", 1000, 0o700),
            "approval_sha256": request["approval_sha256"],
            "plan_sha256": request["plan_sha256"],
            "build_sha256": request["approval"]["build_sha256"],
            "source_sha256": SOURCE_SHA,
            "probe_sha256": sha(probe),
            "cid": proof["cid"],
        }
        and base.read_leaf(ROOT / "created.json") == canonical(proof)
        and base.read_leaf(ROOT / ".agency-owner.json") == canonical({**identity, "owner": OWNER})
        and base.read_leaf(ROOT / "cid", limit=128).decode().strip() == proof["cid"]
        and sha(base.read_leaf(ROOT / "inputs" / LEAF, limit=SOURCE_BYTES)) == SOURCE_SHA
        and base.read_leaf(ROOT / "inputs" / PROBE, limit=32 * 1024) == probe,
        "CREATE_RECIBO",
    )
    return proof


def functional(base):
    work = ROOT / "work"
    raw = base.read_leaf(work / ".sqlite-functional.json", uid=1000, limit=16 * 1024)
    probe = json.loads(raw)
    preflight = json.loads(base.read_leaf(work / ".sdk-preflight.json", uid=1000, limit=4096))
    need(
        preflight["stage"] == "sqlite"
        and preflight["git_entries"] == 0
        and type(preflight["walked_entries"]) is int
        and 0 < preflight["walked_entries"] <= 10_000
        and preflight["uid"] == preflight["gid"] == 1000
        and preflight["network"] == ["lo"]
        and preflight["root_read_only"] is preflight["inputs_read_only"] is True
        and preflight["source_sha256"] == SOURCE_SHA
        and preflight["node_sha256"] == NODE_SHA
        and probe["schema"] == 1
        and probe["version"] == "3.54.1"
        and probe["source_id"] == SOURCE_ID
        and probe["node"] == "v24.22.3"
        and probe["variants"].keys() == set(VARIANTS)
        and probe["browser_verified"] is probe["opfs_verified"] is False,
        "PRUEBA_RECIBO",
    )
    dist = work / "wa-sqlite/dist"
    base.directory(dist, 1000, 0o700)
    need(
        {p.name for p in dist.iterdir()}
        == {f"{v}.{ext}" for v in VARIANTS for ext in ("mjs", "wasm")},
        "DIST_LISTA",
    )
    outputs = {}
    for variant, item in probe["variants"].items():
        need(
            item.keys()
            == {
                "imports",
                "exports",
                "import_abi_sha256",
                "export_abi_sha256",
                "js_sha256",
                "wasm_sha256",
                "async_vfs_opens",
            }
            and all(type(item[k]) is int and 0 < item[k] <= 2048 for k in ("imports", "exports"))
            and all(HEX.fullmatch(item[k]) for k in ("import_abi_sha256", "export_abi_sha256"))
            and type(item["async_vfs_opens"]) is int
            and (
                item["async_vfs_opens"] == 0
                if variant == "wa-sqlite"
                else item["async_vfs_opens"] > 0
            ),
            "PRUEBA_ABI",
        )
        for ext, key in (("mjs", "js_sha256"), ("wasm", "wasm_sha256")):
            name = f"{variant}.{ext}"
            body = base.read_leaf(dist / name, uid=1000, limit=4 * 1024**2)
            need(sha(body) == item[key], "DIST_SHA")
            if ext == "wasm":
                need(
                    body.startswith(b"\0asm\1\0\0\0")
                    and SOURCE_ID.encode() + b"\0" in body
                    and b"3.54.1\0" in body,
                    "DIST_WASM",
                )
            outputs[name] = {"bytes": len(body), "sha256": item[key]}
    return {
        "functional_sha256": sha(raw),
        "probe": probe,
        "preflight": preflight,
        "outputs": outputs,
    }


def lifecycle(action, base, reader, docker, image, request, probe, out):
    proof = verify_created(base, image, request, probe)
    cid = proof["cid"]
    out["cid"] = cid
    state = inspect(docker, cid, image)
    if action == "start":
        need(
            state["Status"] == "created"
            and state["Pid"] == 0
            and state["Running"] is False
            and not any((ROOT / "work").iterdir()),
            "START_NO_REPLAY",
        )
        base.publish(reader, ROOT / "start-intent.json", canonical({"cid": cid, "owner": OWNER}))
        need(docker("start", cid, timeout=30).stdout.decode().strip() == cid, "START_CID")
        state = inspect(docker, cid, image)
        base.publish(
            reader, ROOT / "started.json", canonical({"cid": cid, "started_at": state["StartedAt"]})
        )
    out["state"] = {
        k: state[k]
        for k in ("Status", "Running", "Pid", "ExitCode", "StartedAt", "FinishedAt", "OOMKilled")
    }
    if action == "status" and state["Status"] == "exited":
        need(
            state["Pid"] == 0 and state["ExitCode"] == 0 and state["OOMKilled"] is False,
            "SDK_SALIDA",
        )
        need(
            base.read_leaf(ROOT / "started.json")
            == canonical({"cid": cid, "started_at": state["StartedAt"]}),
            "START_RECIBO",
        )
        out["functional"] = functional(base)
        need(inspect(docker, cid, image) == state, "ESTADO_CAMBIO")
        out["functional_verified"] = True


def expired(*_args):
    raise TimeoutError("PLAZO_REMOTO")


def remote(action):
    os.umask(0o077)
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(150)
    incoming = sys.stdin.buffer
    out = {
        "action": action,
        "owner": OWNER,
        "success": False,
        "functional_verified": False,
        "production_approved": False,
        "browser_verified": False,
        "opfs_verified": False,
    }
    base = None
    try:
        size = struct.unpack(">I", exact(incoming, 4))[0]
        need(0 < size <= 256 * 1024, "TRAMA_LIMITE")
        request = json.loads(exact(incoming, size))
        expected = {
            "action",
            "plan_sha256",
            "approval_sha256",
            "daemon",
            "build",
            "approval",
            "sources",
        }
        need(
            request.keys() == expected | (set() if action == "create" else {"created"}),
            "TRAMA_CAMPOS",
        )
        need(request["action"] == action and HEX.fullmatch(request["plan_sha256"]), "TRAMA_ACCION")
        out["plan_sha256"] = request["plan_sha256"]
        raws = {k: base64.b64decode(v, validate=True) for k, v in request["sources"].items()}
        need(raws.keys() == {"builder", "baseline", "reader", "docker", "probe"}, "TRAMA_FUENTES")
        builder = load(raws["builder"], BUILDER, "wa_builder")
        base = load(raws["baseline"], builder.SOURCES["baseline"][1], "wa_baseline")
        reader = load(raws["reader"], builder.SOURCES["reader"][1], "wa_reader")
        docker = base.docker_helper(raws["docker"])
        probe = raws["probe"]
        need(0 < len(probe) <= 32 * 1024 and sha(probe) == PROBE_SHA, "PROBE_PIN")
        builder.check_host(base, docker, request["daemon"])
        image = checked_image(
            builder, base, docker, request["build"], request["approval"], request["approval_sha256"]
        )
        out.update(image=image, approval_sha256=request["approval_sha256"])
        if action == "create":
            create(base, reader, docker, image, incoming, probe, request, out)
        else:
            need(not incoming.read(1), "TRAMA_EXTRA")
            lifecycle(action, base, reader, docker, image, request, probe, out)
        out["success"] = True
    except Exception as error:
        out["error_type"] = type(error).__name__
        if isinstance(error, (ValueError, TimeoutError)) and re.fullmatch(
            r"[A-Z0-9_]+", str(error)
        ):
            out["error_code"] = str(error)
        if action == "create" and "directory" in out:
            try:
                cid = base.read_leaf(ROOT / "cid", limit=128).decode().strip()
                if HEX.fullmatch(cid):
                    out["observed_cid"] = cid
            except (OSError, ValueError):
                pass
            try:
                base.publish(reader, ROOT / "create-failure.json", canonical(out))
            except (OSError, ValueError):
                pass
    finally:
        signal.alarm(0)
    sys.stdout.buffer.write(canonical(out))
    return 0 if out["success"] else 1


def local_plan():
    here = Path(__file__).resolve().parent
    path = here / "n8n_wa_sqlite_sdk_build_vm_v4.py"
    raw = path.read_bytes()
    builder = load(raw, BUILDER, "wa_builder_local", path)
    previous, _source, helpers, _location, client = builder.local_plan()
    need(sha(builder.canonical(previous)) == BUILD_PLAN, "BUILD_PLAN")
    probe = client.source(here / PROBE)
    need(sha(probe) == PROBE_SHA, "PROBE_PIN")
    source = client.source(Path(__file__).resolve())
    plan = {
        "schema": 1,
        "owner": OWNER,
        "root": ROOT.as_posix(),
        "name": NAME,
        "target": "192.168.0.85",
        "source_sha256": sha(source),
        "input_sha256": SOURCE_SHA,
        "input_bytes": SOURCE_BYTES,
        "builder_sha256": BUILDER,
        "build_plan_sha256": BUILD_PLAN,
        "probe_sha256": PROBE_SHA,
        "build_receipt_sha256": BUILD_RECEIPT,
        "sdk_image_requires_approved_build": True,
        "production_approved": False,
    }
    sources = {
        "builder": raw,
        "probe": probe,
        **{k: helpers[k] for k in ("baseline", "reader", "docker")},
    }
    return plan, source, sources, client


def operate(action, dto, plan_sha, approval_sha, local):
    plan, source, sources, client = local
    expected = {"ssh", "daemon", "build", "approval"}
    need(dto.keys() == expected | (set() if action == "create" else {"created"}), "PETICION_CAMPOS")
    need(dto["ssh"].keys() == client.SSH_KEYS, "SSH_CAMPOS")
    approval_for(dto["build"], dto["approval"], approval_sha)
    request = {k: v for k, v in dto.items() if k != "ssh"}
    request.update(
        action=action,
        plan_sha256=plan_sha,
        approval_sha256=approval_sha,
        sources={k: base64.b64encode(v).decode("ascii") for k, v in sources.items()},
    )
    sql = client.ROOT / "docker/postgres-immich/sql_acceptance_v2.py"
    need(
        sha(client.source(sql.with_name("sql_acceptance_remote_v2.py"), 64 * 1024))
        == client.SQL_CONTRACT,
        "SSH_CONTRATO",
    )
    sdk = load(
        client.source(sql, 64 * 1024), client.SQL_TRANSPORT, "wa_acceptance_ssh", sql
    ).sdk_local()
    data = None
    if action == "create":
        data = client.source(client.ARCHIVE.parents[1] / LEAF, SOURCE_BYTES)
        need(len(data) == SOURCE_BYTES and sha(data) == SOURCE_SHA, "CONTEXTO_PIN")
    with ExitStack() as stack:
        ssh = sdk.connect(dto["ssh"])
        stack.callback(ssh.close)
        channel = ssh.get_transport().open_session(timeout=10)
        stack.callback(channel.close)
        channel.settimeout(15)
        deadline = time.monotonic() + 170
        channel.exec_command(client.remote_command("remote-" + action, plan["source_sha256"]))
        channel.sendall(client.framed(source, 32 * 1024))
        channel.sendall(client.framed(canonical(request), 256 * 1024))
        if data is not None and client.wait_ready(channel, READY, time.monotonic() + 45):
            for offset in range(0, len(data), 65536):
                need(time.monotonic() < deadline, "CONTEXTO_PLAZO")
                channel.sendall(data[offset : offset + 65536])
        channel.shutdown_write()
        result = sdk.collect(channel, deadline, 64 * 1024)
    need(
        result["owner"] == OWNER
        and result["action"] == action
        and result["plan_sha256"] == plan_sha,
        "RECIBO_IDENTIDAD",
    )
    return result


def main():
    if len(sys.argv) == 2 and sys.argv[1] in {"remote-create", "remote-start", "remote-status"}:
        return remote(sys.argv[1].removeprefix("remote-"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", nargs="?", choices=("plan", "create", "start", "status"), default="plan"
    )
    parser.add_argument("--plan-sha256")
    parser.add_argument("--approval-sha256")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    local = local_plan()
    plan = local[0]
    digest = sha(canonical(plan))
    if args.action == "plan":
        sys.stdout.buffer.write(canonical({**plan, "plan_sha256": digest}))
        return 0
    need(
        args.plan_sha256 == digest
        and args.receipt is not None
        and HEX.fullmatch(args.approval_sha256 or ""),
        "PLAN_RECIBO",
    )
    dest = args.receipt.absolute()
    need(dest.parent.resolve(strict=True) == dest.parent, "RECIBO_RUTA")
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    need(0 < len(raw) <= 128 * 1024, "PETICION_LIMITE")
    dto = json.loads(raw)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        try:
            result = operate(args.action, dto, digest, args.approval_sha256, local)
        except Exception as error:
            result = {
                "action": args.action,
                "plan_sha256": digest,
                "owner": OWNER,
                "success": False,
                "transport_error": type(error).__name__,
            }
        f.write(canonical(result))
        f.flush()
        os.fsync(f.fileno())
    sys.stdout.buffer.write(canonical(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
