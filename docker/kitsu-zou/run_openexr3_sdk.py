"""Lanzador de raíz: plan local por defecto, sin conexiones al importar."""

import argparse
import base64
import hashlib
import io
import json
import os
import shlex
import stat
import struct
import sys
import tarfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
PREFIX = "MEDIA_ZOU_OPENCV_OPENEXR3_SDK_"
ACL_HELPER_SUM256 = "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b"
PARSER_SHA = "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda"
LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"
UPDATER_SHA = "0fc75c6d082e9e25fa6a5b5146560d7373b3dfab2d701e351749dafb2183b46c"
LOCAL_SHA = "222e30e5d78dd494482a61c54c906846c13156eb3031466990df5ecb538958bf"


def read_source(path, maximum=256 * 1024):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Ruta con enlace o punto de reanálisis.")
    before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        first = os.fstat(stream.fileno())
        if (
            (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) != before
            or not stat.S_ISREG(first.st_mode)
            or first.st_nlink != 1
            or not 0 <= first.st_size <= maximum
        ):
            raise ValueError("Fuente irregular, cambiada o excesiva.")
        raw = stream.read(maximum + 1)
        last = os.fstat(stream.fileno())
        if len(raw) != first.st_size or (
            first.st_dev,
            first.st_ino,
            first.st_size,
            first.st_mtime_ns,
            first.st_ctime_ns,
        ) != (last.st_dev, last.st_ino, last.st_size, last.st_mtime_ns, last.st_ctime_ns):
            raise ValueError("Fuente cambió durante la lectura.")
    end = path.lstat()
    if (end.st_dev, end.st_ino, end.st_size, end.st_mtime_ns) != before or end.st_nlink != 1:
        raise ValueError("Nombre de fuente cambiado.")
    return raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def module_bytes(name, raw, path, expected=None):
    if expected is not None and digest(raw) != expected:
        raise ValueError("Fuente auxiliar distinta del pin.")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def contract():
    path = HERE / "openexr3_sdk_remote.py"
    return module_bytes("openexr3_contract", read_source(path), path)


def checked_reference(path, expected, maximum=256 * 1024):
    raw = read_source(path, maximum)
    if digest(raw) != expected:
        raise ValueError("Referencia pública cambió respecto al pin.")
    return raw


def proposal():
    r = contract()
    archive = ROOT / ".tools/kitsu-zou-openexr3-inputs-v1/overlay.tar"
    receipt = archive.with_name("receipt.json")
    raw = checked_reference(archive, r.PREFIX_SHA, 16 * 1024**2)
    checked_reference(receipt, r.PREFIX_RECEIPT_SHA)
    audit_path = ROOT / ".tools/evidence/phase4-zou-opencv-sdk-audit-v1-profile.json"
    audit_raw = checked_reference(audit_path, r.AUDIT_SHA, 2 * 1024**2)
    audit = r.json_object(audit_raw)
    original = audit["sdk_plan"]
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for item in tar:
            if item.name in entries:
                raise ValueError("Prefijo con duplicados.")
            if item.issym():
                entries[item.name] = {"kind": "link", "target": item.linkname}
            elif item.isreg():
                with tar.extractfile(item) as stream:
                    data = stream.read(item.size + 1)
                entries[item.name] = {"kind": "file", "size": len(data), "sha256": digest(data)}
            else:
                raise ValueError("El prefijo preparado sólo admite archivos y enlaces fijados.")
    if len(entries) != 248:
        raise ValueError("Prefijo incompleto.")
    r.archive_payloads(raw, entries)
    files = {}
    for name, path, expected, maximum in [
        ("prefix.tar", archive, r.PREFIX_SHA, 16 * 1024**2),
        ("prefix-receipt.json", receipt, r.PREFIX_RECEIPT_SHA, 256 * 1024),
        ("audit.json", audit_path, r.AUDIT_SHA, 2 * 1024**2),
        ("normal.py", HERE / "opencv_minimal_sdk_remote.py", r.NORMAL_SHA, 256 * 1024),
        (
            "host-proof.json",
            ROOT / ".tools/evidence/phase4-zou-opencv-inputs-host-verification-real-v1.json",
            r.HOST_SHA,
            256 * 1024,
        ),
        (
            "input-sca.json",
            ROOT / ".tools/evidence/phase4-zou-opencv-openexr3-inputs-current-sca-v1.receipt.json",
            r.INPUT_SCA_SHA,
            256 * 1024,
        ),
        ("launcher.py", HERE / "openexr3_sdk_remote.py", None, 256 * 1024),
    ]:
        data = read_source(path, maximum)
        if expected is not None and digest(data) != expected:
            raise ValueError("Artefacto de transferencia distinto del respaldo.")
        files[name] = {"path": str(path), "size": len(data), "sha256": digest(data)}
    root_info = archive.parent.lstat()
    if (root_info.st_dev, root_info.st_ino) != (8121725096129312908, 1407374884211355):
        raise ValueError("Identidad del prefijo preparado distinta del respaldo.")
    return {
        **r.minimum_profile(),
        "kind": "openexr3-sdk-controller-v1",
        "schema": 1,
        "prefix_archive_bytes": r.PREFIX_BYTES,
        "prefix_receipt_sha256": r.PREFIX_RECEIPT_SHA,
        "prefix_entries": entries,
        "transfer_files": files,
        "original_plan_sha256": original["plan_sha256"],
        "sdk_entrypoints": [
            {"path": row["path"], "sha256": row["sha256"]} for row in original["sdk_entrypoints"]
        ],
        "sources": {
            "local": digest(read_source(Path(__file__))),
            "remote": files["launcher.py"]["sha256"],
            "layout": LAYOUT_SHA,
            "normal": r.NORMAL_SHA,
            "acl_helper": ACL_HELPER_SUM256,
            "env_parser": PARSER_SHA,
            "updater": UPDATER_SHA,
            "connector": LOCAL_SHA,
        },
        "limits": {
            "sdk_seconds": 600,
            "build_seconds": 1800,
            "cleanup_reserve_seconds": 20,
            "log_bytes": 4 * 1024**2,
            "cpu": 2,
            "memory_bytes": 6 * 1024**3,
            "pids": 128,
        },
        "sdk_executed": False,
        "bound_to_vault": False,
        "external_sca_approval_required": True,
    }


def decode(raw):
    value = contract().json_object(base64.b64decode(raw, validate=True))
    if not isinstance(value, dict):
        raise ValueError("Respaldo no es objeto JSON.")
    return value


def read_values(plan):
    access_path, parser_path = ROOT / "sre/backup_tools_access.py", ROOT / "scripts/ci/check_env.py"
    access = module_bytes(
        "openexr3_access", checked_reference(access_path, ACL_HELPER_SUM256), access_path
    )
    access.check_access(ROOT / ".env", private=True)
    parser = module_bytes("openexr3_env", checked_reference(parser_path, PARSER_SHA), parser_path)
    values, errors = parser.parse_env(read_source(ROOT / ".env", 8 * 1024**2).decode("utf-8-sig"))
    if errors:
        raise ValueError("Bóveda ambigua.")
    return values


def bind_plan(plan, values):
    r = contract()
    expected = {
        "OWNER": r.OWNER,
        "CODE_PATH": r.PATHS["code"],
        "PREFIX_PATH": r.PATHS["prefix"],
        "CONTROL_PATH": r.PATHS["control"],
        "OUTPUTS_PATH": r.PATHS["outputs"],
        "WORK_PATH": r.PATHS["outputs"] + "/openexr3-attempt-v2",
        "REMOTE_STAGING_PATH": r.PATHS["staging"],
        "LOCAL_SOURCE_SHA256": plan["sources"]["local"],
        "REMOTE_SOURCE_SHA256": plan["sources"]["remote"],
        "PREFIX_TAR_PATH": plan["transfer_files"]["prefix.tar"]["path"],
        "PREFIX_TAR_SHA256": r.PREFIX_SHA,
        "PREFIX_RECEIPT_SHA256": r.PREFIX_RECEIPT_SHA,
        "PROFILE_SHA256": digest(r.canonical(plan)),
    }
    if any(values.get(PREFIX + key) != value for key, value in expected.items()):
        raise ValueError("Rutas o pins del lanzador no respaldados.")
    result = {
        **plan,
        "bound_to_vault": True,
        "layout": decode(values["MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64"]),
    }
    result["plan_sha256"] = digest(r.canonical(result))
    return result


def connect(values):
    path = HERE / "run_opencv_minimal_sdk.py"
    old = module_bytes("openexr3_ssh", checked_reference(path, LOCAL_SHA), path)
    return old.connect(values)


def write_new(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def saved(values):
    result = {}
    for group in ("PREPARED_B64", "CONTAINER_B64", "STAGE_RECEIPTS_B64"):
        data = decode(values.get(PREFIX + group, "e30="))
        if set(result) & set(data):
            raise ValueError("Etapa duplicada entre grupos respaldados.")
        result.update(data)
    return result


def persist(values, plan, stage, value, writer):
    r = contract()
    group = (
        "PREPARED_B64"
        if stage in {"transfer", "prepare"}
        else "CONTAINER_B64"
        if stage in {"create", "create_unverified", "start", "stop"}
        else "STAGE_RECEIPTS_B64"
    )
    if stage == "create_unverified":
        if (
            value.get("verified") is not False
            or value.get("plan_sha256") != plan["plan_sha256"]
            or not r.SHA.fullmatch(str(value.get("cid", "")))
        ):
            raise ValueError("Intención CID incompleta.")
    else:
        r.receipt_context(value, stage, plan)
    current = decode(values.get(PREFIX + group, "e30="))
    if stage in current:
        raise ValueError("No se sustituye una etapa ya respaldada.")
    checksum = digest(r.canonical(value))
    evidence = (
        ROOT
        / ".tools/evidence"
        / ("phase4-zou-opencv-openexr3-sdk-v1." + stage + "." + checksum[:16] + ".json")
    )
    write_new(evidence, r.canonical(value))
    current[stage] = value
    encoded = base64.b64encode(r.canonical(current)).decode()
    writer({PREFIX + group: encoded})
    values[PREFIX + group] = encoded
    return str(evidence)


def collect(channel, deadline, maximum, *, heartbeat):
    out, err = bytearray(), bytearray()
    pulse = time.monotonic()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Plazo de transporte agotado.")
        if heartbeat and time.monotonic() >= pulse:
            channel.sendall(b".")
            pulse = time.monotonic() + 1
        for ready, read, target in (
            (channel.recv_ready, channel.recv, out),
            (channel.recv_stderr_ready, channel.recv_stderr, err),
        ):
            while ready():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Salida continua fuera de plazo.")
                target.extend(read(65536))
                if len(out) + len(err) > maximum:
                    raise ValueError("Captura excesiva, sin truncar.")
        if (
            channel.exit_status_ready()
            and not channel.recv_ready()
            and not channel.recv_stderr_ready()
        ):
            break
        time.sleep(0.01)
    code = channel.recv_exit_status()
    value = contract().json_object(out)
    if (
        err
        or type(value.get("success")) is not bool
        or code not in (0, 1)
        or value["success"] != (code == 0)
    ):
        raise ValueError("Framing/estado remoto incoherentes.")
    return value


def remote_command(checksum, mode):
    r = contract()
    if mode not in r.MODES or not r.SHA.fullmatch(checksum):
        raise ValueError("Etapa o ejecutor remoto inválidos.")
    bootstrap = (
        "import sys,struct,hashlib\ns=sys.stdin.buffer.raw\n"
        "def read(n):\n b=bytearray()\n while len(b)<n:\n  x=s.read(n-len(b))\n  if not x:raise ValueError('SOURCE_TRUNCATED')\n  b.extend(x)\n return bytes(b)\n"
        "n=struct.unpack('>I',read(4))[0]\nif not 0<n<=262144:raise ValueError('SOURCE_LIMIT')\n"
        "source=read(n)\n_SOURCE_SHA256=hashlib.sha256(source).hexdigest()\nif _SOURCE_SHA256!="
        + repr(checksum)
        + ":raise ValueError('SOURCE_PIN')\n"
        "exec(compile(source,'<openexr3-sdk-fijado>','exec'))"
    )
    return (
        ("" if mode == "receive" else "sudo -n ")
        + "/usr/bin/env -i PATH=/usr/bin:/bin HOME=/root /usr/bin/python3 -I -B -c "
        + shlex.quote(bootstrap)
        + " --mode "
        + mode
    )


def request_remote(client, mode, plan, values):
    r = contract()
    source = checked_reference(HERE / "openexr3_sdk_remote.py", plan["sources"]["remote"])
    normal = checked_reference(HERE / "opencv_minimal_sdk_remote.py", r.NORMAL_SHA)
    request = {
        "mode": mode,
        "plan": plan,
        "backed": saved(values),
        "layout_b64": base64.b64encode(
            checked_reference(ROOT / "docker/host_layout.py", LAYOUT_SHA)
        ).decode(),
    }
    if mode in {"approve", "build"}:
        path = Path(values[PREFIX + "SDK_SCA_RECEIPT_PATH"])
        checksum = values[PREFIX + "SDK_SCA_RECEIPT_SHA256"]
        proof = checked_reference(path, checksum, 1024**2)
        request.update(
            approval_b64=base64.b64encode(proof).decode(), approval_source_sha256=checksum
        )
    encoded = r.canonical(request)
    if len(encoded) > 2 * 1024**2:
        raise ValueError("Petición excesiva.")
    channel = client.get_transport().open_session(timeout=10)
    deadline = time.monotonic() + (
        1840 if mode == "build" else 640 if mode in {"receive", "prepare", "sdk"} else 120
    )
    try:
        channel.settimeout(10)
        channel.exec_command(remote_command(plan["sources"]["remote"], mode))
        for raw in (source, normal, encoded):
            channel.sendall(struct.pack(">I", len(raw)) + raw)
        if mode == "receive":
            for _name, row in sorted(plan["transfer_files"].items()):
                raw = checked_reference(Path(row["path"]), row["sha256"], 16 * 1024**2)
                if len(raw) != row["size"]:
                    raise ValueError("Input de transferencia cambió de tamaño.")
                for start in range(0, len(raw), 65536):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Transferencia fuera de plazo.")
                    channel.sendall(raw[start : start + 65536])
        heartbeat = mode in {"sdk", "build"}
        if not heartbeat:
            channel.shutdown_write()
        return collect(channel, deadline, 4 * 1024**2, heartbeat=heartbeat)
    finally:
        channel.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        default="plan",
        choices=["plan", "prepare", "create", "start", "sdk", "approve", "build", "stop"],
    )
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        r = contract()
        if args.mode == "stop" and args.root_update_vault:
            # La parada no depende del TAR local, del SDK, de SCA ni del kit.
            values = read_values(None)
            plan = decode(values[PREFIX + "CONTROLLER_B64"])
            r.check_plan(plan)
            if plan["sources"]["local"] != digest(read_source(Path(__file__))) or plan["sources"][
                "remote"
            ] != digest(read_source(HERE / "openexr3_sdk_remote.py")):
                raise ValueError("Fuentes de parada distintas del respaldo.")
        else:
            proposed = proposal()
            if not args.root_update_vault:
                if args.mode != "plan":
                    raise ValueError("La transición requiere CLI explícita de raíz.")
                print(r.canonical(proposed).decode())
                return 0
            values = read_values(proposed)
            plan = bind_plan(proposed, values)
        updater = module_bytes(
            "openexr3_vault_writer",
            checked_reference(ROOT / ".tools/vault_updates.py", UPDATER_SHA),
            ROOT / ".tools/vault_updates.py",
        )
        if args.mode == "plan":
            updater.update(
                {PREFIX + "CONTROLLER_B64": base64.b64encode(r.canonical(plan)).decode()}
            )
            print(
                json.dumps(
                    {"success": True, "plan_sha256": plan["plan_sha256"], "runtime_applied": False}
                )
            )
            return 0
        if decode(values[PREFIX + "CONTROLLER_B64"]) != plan:
            raise ValueError("Plan actual distinto del respaldo durable.")
        client = connect(values)
        if args.mode == "prepare" and "transfer" not in saved(values):
            result = request_remote(client, "receive", plan, values)
            if not result["success"]:
                raise ValueError("Transferencia rechazada.")
            persist(values, plan, "transfer", result["result"], updater.update)
        result = request_remote(client, args.mode, plan, values)
        if r.SHA.fullmatch(str(result.get("created_cid", ""))):
            persist(
                values,
                plan,
                "create_unverified",
                {
                    "cid": result["created_cid"],
                    "verified": False,
                    "plan_sha256": plan["plan_sha256"],
                },
                updater.update,
            )
        if result.get("result") is not None:
            persist(values, plan, args.mode, result["result"], updater.update)
        print(r.canonical(result).decode())
        return 0 if result["success"] else 1
    except (ValueError, OSError, KeyError, TypeError, TimeoutError) as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": "OPENEXR3_SDK_REJECTED",
                    "error_type": type(error).__name__,
                }
            )
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
