"""Plan, preparación local y lectura remota explícita de cinco proveedores OpenCV."""

import argparse
import base64
import hashlib
import json
import os
import shlex
import struct
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = "agencia-digital-marketing-ai:opencv-base-providers:v1"
OUTPUT = ROOT / ".tools/backup-source-build/opencv-base-providers-v1"
REMOTE = ROOT / "docker/kitsu-zou/capture_opencv_runtime_materials_remote.py"
READER = (
    "docker/kitsu-zou/recover_opencv_sdk_scan_v2.py",
    "b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c",
)
REFS = {
    "audit_profile": (
        ".tools/evidence/phase4-zou-opencv-sdk-audit-v1-profile.json",
        "747a33d04bf5238ccc374cc3ac7cd3b1f49050bf9df5cb668d4191cc851001e1",
    ),
    "previous_capture": (
        ".tools/evidence/phase4-zou-opencv-sdk-installed-755ea032b52a767f-v1.capture.json",
        "fbdb485a006e697b498a26a57981e8909daa59b67df427a785998763dff659d3",
    ),
    "auditor_source": (
        "docker/kitsu-zou/opencv_sdk_audit_remote.py",
        "4d46898d6700d5858bc778a3ddb68acc0c0f2d44b923a35c9c681a789fa18dd2",
    ),
    "controller_source": (
        "docker/kitsu-zou/opencv_minimal_sdk_remote.py",
        "77f442612a9c14df8f0685474ce5e3f47c8c0da16e828888ee8250dc1d7e002b",
    ),
    "layout_source": (
        "docker/host_layout.py",
        "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9",
    ),
    "inventory": (
        ".tools/backup-source-build/opencv-wheel-inventory-v1/inventory.json",
        "81bfa3187026ee88f96b6fd5cd281a29f1ff40851325cbba9d9a6fea9f522e16",
    ),
    "parser": (
        "docker/postgres-immich/final_image_remote.py",
        "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49",
    ),
    "access": (
        "sre/backup_tools_access.py",
        "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b",
    ),
    "transport": (
        "docker/postgres-immich/security_rebuild_v6.py",
        "0b70c1b5a69467ad1f94d9989b36d894b48af905904c793e3b507d97ab3f23c4",
    ),
    "transport_profile": (
        ".tools/postgres-immich-security-inputs-v1/security-rebuild-v6.profile.json",
        "5f9564883e60d3e95e56dadc866448d369852942cd89ddefd571dabc9bb38331",
    ),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def module_raw(name, path, raw):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def reader():
    path = ROOT / READER[0]
    raw = path.read_bytes()
    require(len(raw) < 128 * 1024 and digest(raw) == READER[1], "Lector de datos alterado.")
    return module_raw("opencv_base_reader_fixed", path, raw)


def context():
    safe = reader()
    raw = {key: safe.read_exact(ROOT / path, 2 * 1024**2, sha) for key, (path, sha) in REFS.items()}
    source = safe.read_exact(REMOTE, 128 * 1024)
    remote = module_raw("opencv_base_remote_pure", REMOTE, source)
    inventory = (
        safe.json_object(raw["inventory"])
        if hasattr(safe, "json_object")
        else json.loads(raw["inventory"])
    )
    require(
        {r["soname"] for r in inventory["dynamic_closure"]["unconfirmed"]} == set(remote.TARGETS),
        "Proveedores pendientes distintos del contrato.",
    )
    access = module_raw("opencv_base_access_fixed", ROOT / REFS["access"][0], raw["access"])
    access.check_access(OUTPUT.parent, private=True)
    marker = safe.read_exact(OUTPUT.parent / ".agency-backup-build-owner", 4096)
    require(
        marker == b"agencia-digital-marketing-ai:backup-source-build:v1",
        "Raíz de datos no acreditada.",
    )
    info = OUTPUT.parent.lstat()
    plan = {
        "schema_version": 1,
        "owner": OWNER,
        "root": str(OUTPUT),
        "source_sha256": digest(safe.read_exact(Path(__file__), 128 * 1024)),
        "remote_sha256": digest(source),
        "reader_sha256": READER[1],
        "references": {key: {"path": path, "sha256": sha} for key, (path, sha) in REFS.items()},
        "parent": {"device": info.st_dev, "inode": info.st_ino, "marker_sha256": digest(marker)},
        "targets": remote.TARGETS,
        "remote_seconds": 150,
        "parent_seconds": 175,
        "maximum_response_bytes": 32 * 1024**2,
        "sdk_executed": False,
        "elf_executed": False,
        "runtime_approved": False,
    }
    return plan, raw, source, safe, access


def write_new(path, raw, access):
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as output:
        access.check_access(path, private=True)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def prepare(plan, access):
    os.mkdir(OUTPUT, 0o700)
    access.check_access(OUTPUT, private=True)
    info = OUTPUT.lstat()
    proof = {
        "owner": OWNER,
        "root": str(OUTPUT),
        "device": info.st_dev,
        "inode": info.st_ino,
        "plan_sha256": digest(canonical(plan)),
    }
    write_new(OUTPUT / ".owner.json", canonical(proof), access)
    write_new(OUTPUT / "plan.json", canonical(plan), access)
    write_new(OUTPUT / "prepared.json", canonical(proof), access)
    return proof


def validate_root(plan, expected, safe, access):
    access.check_access(OUTPUT, private=True)
    proof_raw = safe.read_exact(OUTPUT / "prepared.json", 4096, expected)
    proof = json.loads(proof_raw)
    info = OUTPUT.lstat()
    require(
        proof
        == {
            "owner": OWNER,
            "root": str(OUTPUT),
            "device": info.st_dev,
            "inode": info.st_ino,
            "plan_sha256": digest(canonical(plan)),
        },
        "Identidad del namespace alterada.",
    )
    require(
        safe.read_exact(OUTPUT / ".owner.json", 4096) == proof_raw
        and safe.read_exact(OUTPUT / "plan.json", 65536) == canonical(plan),
        "Marcador/plan de otra preparación.",
    )
    require(
        {p.name for p in OUTPUT.iterdir()} == {".owner.json", "plan.json", "prepared.json"},
        "No adoptar intento previo ni archivos ajenos.",
    )
    return proof


def fetch(raw, source, plan):
    transport = module_raw(
        "opencv_base_transport_fixed", ROOT / REFS["transport"][0], raw["transport"]
    )
    values = transport.read_values(json.loads(raw["transport_profile"]))
    client = transport.load_reference("connector").connect(values)
    channel = None
    try:
        request = {
            key + "_b64": base64.b64encode(raw[key]).decode()
            for key in (
                "audit_profile",
                "previous_capture",
                "auditor_source",
                "controller_source",
                "layout_source",
            )
        }
        body = canonical(request)
        require(len(body) <= 3 * 1024**2, "Petición de captura excesiva.")
        bootstrap = (
            "import sys,struct,hashlib\ns=sys.stdin.buffer\n"
            "def read(n):\n b=bytearray()\n while len(b)<n:\n  p=s.read(n-len(b))\n  if not p:raise ValueError('Peticion truncada')\n  b.extend(p)\n return bytes(b)\n"
            "n=struct.unpack('>I',read(4))[0]\n"
            "if not 0<n<=131072:raise ValueError('Fuente excesiva')\n"
            "d=read(n)\nif hashlib.sha256(d).hexdigest()!="
            + repr(plan["remote_sha256"])
            + ":raise ValueError('Fuente alterada')\n"
            "exec(compile(d,'<lector-bibliotecas-fijado>','exec'),{'__name__':'__main__'})"
        )
        channel = client.get_transport().open_session(timeout=5)
        channel.settimeout(5)
        channel.exec_command(
            "sudo -n /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root /usr/bin/python3 -I -B -c "
            + shlex.quote(bootstrap)
        )
        channel.sendall(
            struct.pack(">I", len(source)) + source + struct.pack(">I", len(body)) + body
        )
        channel.shutdown_write()
        out, err = bytearray(), bytearray()
        deadline = time.monotonic() + plan["parent_seconds"]
        while (
            not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready()
        ):
            require(time.monotonic() < deadline, "Captura remota agotó el presupuesto.")
            if channel.recv_ready():
                out.extend(channel.recv(65536))
            if channel.recv_stderr_ready():
                err.extend(channel.recv_stderr(65536))
            require(
                len(out) <= plan["maximum_response_bytes"] and len(err) <= 16384,
                "Respuesta remota excesiva.",
            )
            time.sleep(0.01)
        code = channel.recv_exit_status()
        response = json.loads(out)
        require(
            code == 0 and not err and response.get("success") is True,
            "Captura remota rechazada: " + str(response.get("detail", "sin detalle")),
        )
        return response
    finally:
        if channel is not None:
            channel.close()
        client.close()


def capture(plan, raw, source, safe, access, expected):
    proof = validate_root(plan, expected, safe, access)
    write_new(
        OUTPUT / "capture-intent.json",
        canonical({"plan_sha256": digest(canonical(plan)), "prepared_sha256": expected}),
        access,
    )
    try:
        result = fetch(raw, source, plan)
        require(
            set(result["files"]) == set(plan["targets"])
            and all(
                result[k] is False for k in ("elf_executed", "sdk_executed", "container_mutated")
            ),
            "Respuesta de otro alcance.",
        )
        parser = module_raw("opencv_base_elf_fixed", ROOT / REFS["parser"][0], raw["parser"])
        directory = OUTPUT / "libraries"
        os.mkdir(directory, 0o700)
        access.check_access(directory, private=True)
        catalog = {}
        for name, row in result["files"].items():
            data = base64.b64decode(row["data_b64"], validate=True)
            require(
                0 < len(data) <= 8 * 1024**2
                and len(data) == row["bytes"]
                and digest(data) == row["sha256"],
                "Bytes de biblioteca alterados.",
            )
            elf = parser.elf_info(data)
            require(elf["soname"] == name, "SONAME distinto del objetivo cerrado.")
            write_new(directory / name, data, access)
            require(
                safe.read_exact(directory / name, 8 * 1024**2, row["sha256"]) == data,
                "La biblioteca publicada cambió.",
            )
            catalog[name] = {
                **{k: v for k, v in row.items() if k != "data_b64"},
                "elf": elf,
                "runtime_installed": False,
            }
        current = OUTPUT.lstat()
        require(
            (current.st_dev, current.st_ino) == (proof["device"], proof["inode"]),
            "Namespace cambió durante captura.",
        )
        receipt = {
            "success": True,
            "owner": OWNER,
            "directory": proof,
            "plan_sha256": digest(canonical(plan)),
            "remote": {k: v for k, v in result.items() if k != "files"},
            "providers": catalog,
            "elf_executed": False,
            "sdk_executed": False,
            "runtime_approved": False,
        }
        write_new(OUTPUT / "receipt.json", canonical(receipt), access)
        return {
            "success": True,
            "receipt_sha256": digest(canonical(receipt)),
            "providers": len(catalog),
            "runtime_approved": False,
        }
    except BaseException as error:
        write_new(
            OUTPUT / "failure.json",
            canonical(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "detail": str(error)[:240],
                    "sdk_executed": False,
                    "elf_executed": False,
                }
            ),
            access,
        )
        raise


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("mode", nargs="?", choices=("plan", "prepare", "capture"), default="plan")
    cli.add_argument("--expected-source-sha256")
    cli.add_argument("--expected-plan-sha256")
    cli.add_argument("--expected-root-proof-sha256")
    args = cli.parse_args(argv)
    plan, raw, source, safe, access = context()
    if args.mode == "plan":
        result = {"plan": plan, "plan_sha256": digest(canonical(plan))}
    else:
        require(
            args.expected_source_sha256 == plan["source_sha256"]
            and args.expected_plan_sha256 == digest(canonical(plan)),
            "Faltan pins externos de fuente/plan.",
        )
        if args.mode == "prepare":
            result = prepare(plan, access)
        else:
            require(
                isinstance(args.expected_root_proof_sha256, str)
                and len(args.expected_root_proof_sha256) == 64,
                "Falta pin de identidad local.",
            )
            result = capture(plan, raw, source, safe, access, args.expected_root_proof_sha256)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
