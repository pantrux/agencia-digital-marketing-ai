"""Factory de Root: plan local por defecto y etapas explícitas; nunca arranca el runtime."""

import argparse
import base64
import hashlib
import json
import os
import stat
import struct
import sys
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
OWNER = "agencia-digital-marketing-ai:python-runtime-image:v2"
NAMESPACE = "/var/lib/media-platform/build/python-runtime-image-v2"
LOCAL_ROOT = PROJECT / ".tools/backup-source-build/python-runtime-image-v2"
INPUT_ROOT = PROJECT / ".tools/backup-source-build/python-runtime-v1"
PREFIX = "MEDIA_PYTHON_RUNTIME_IMAGE_V2_"
ACL_SOURCE_SHA256 = "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b"
SUPPORTS = {
    "docker/valkey/capture_image_v2.py": "470c804ba086da7bfa51937ebb539e42d680d1c512a69d8dc1eb08a5773c15a1",
    "docker/valkey/capture_image_remote_v2.py": "515f17990013507d254f8f1555746bcce006191292bde10cb3a6f46b6cd69002",
    "docker/postgres-immich/final_image_remote.py": "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49",
    "docker/kitsu-zou/prepare_opencv_relocated_wheel.py": "b7b14e650a0eb900f585591077680fa91a335c0224057ad99cb24e68afd771f6",
    "docker/python-runtime/prepare_python_runtime.py": "712e0d40c549da62e0eb0edd9f29c816abaabdfc1ff28720b3ea5df81355a1ec",
    "docker/host_layout.py": "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9",
    "docker/build_component_v2.py": "9053e827d2888c194e1794657191c402a1307b272b5e37e4750ff99b44b7ad4a",
    "sre/backup_tools_access.py": ACL_SOURCE_SHA256,
    "scripts/ci/check_env.py": "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda",
    ".tools/vault_updates.py": "0fc75c6d082e9e25fa6a5b5146560d7373b3dfab2d701e351749dafb2183b46c",
}
INPUTS = {
    "context.tar",
    "inventory.json",
    "base-os19.cdx.json",
    "manual-runtime14.cdx.json",
    "runtime-image-gate.json",
}
STAGES = ("plan", "prepare", "receive", "base", "build", "final", "fetch", "gate")
COMMENTS = (
    "# Para qué: conservar identidad y evidencia de la factory Python propia por etapas.",
    "# Dónde: factory Python en vm-media-01 y recibos locales de Root.",
    "# Riesgo al rotar: cualquier cambio bloquea la siguiente etapa; no adoptar recursos existentes.",
)


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def source_module(path, expected=None):
    raw = Path(path).read_bytes()
    require(expected is None or digest(raw) == expected, "Fuente fuera del pin: " + Path(path).name)
    module = types.ModuleType("runtime_image_" + Path(path).stem)
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), vars(module))
    return module


def fixed(path):
    return source_module(PROJECT / path, SUPPORTS[path])


def reader():
    return fixed("docker/kitsu-zou/prepare_opencv_relocated_wheel.py")


def prepared_inputs():
    if not os.path.lexists(INPUT_ROOT):
        return None
    provider = reader()
    access = provider.access_reader()
    access.check_access(INPUT_ROOT, private=True)
    raw = provider.read_regular(INPUT_ROOT / "receipt.json", 1024**2)
    receipt = json.loads(raw)
    proof = receipt["directory"]
    require(
        receipt.get("success") is True
        and receipt.get("owner") == "agencia-digital-marketing-ai:python-runtime:v1"
        and all(
            receipt.get(key) is False
            for key in ("sdk_executed", "scripts_executed", "image_built", "runtime_approved")
        )
        and proof["path"] == str(INPUT_ROOT),
        "Prepare Python no acredita los inputs.",
    )
    info = INPUT_ROOT.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & 0x400
        and proof["directory"] == {"device": info.st_dev, "inode": info.st_ino},
        "Raíz preparada cambió.",
    )
    require(
        set(receipt["outputs"]) == INPUTS | {".owner.json", "plan.json"},
        "Salidas preparadas distintas.",
    )
    for name, reference in receipt["outputs"].items():
        access.check_access(INPUT_ROOT / name, private=True)
        data = provider.read_regular(INPUT_ROOT / name, 128 * 1024**2)
        require(
            len(data) == reference["bytes"] and digest(data) == reference["sha256"],
            "Output preparado cambió.",
        )
    require(
        json.loads(provider.read_regular(INPUT_ROOT / ".owner.json", 65536)) == proof,
        "Marcador preparado distinto.",
    )
    core = source_module(Path(__file__).with_name("runtime_image_inventory_v2.py"))
    context = provider.read_regular(INPUT_ROOT / "context.tar", 128 * 1024**2)
    _derived, transformation = core.derive_context(context)
    return {
        "receipt_path": str(INPUT_ROOT / "receipt.json"),
        "receipt_sha256": digest(raw),
        "directory": proof,
        "outputs": {name: receipt["outputs"][name] for name in sorted(INPUTS)},
        "derivation": transformation,
    }


def proposal():
    for path, expected in SUPPORTS.items():
        require(digest((PROJECT / path).read_bytes()) == expected, "Soporte modificado: " + path)
    sources = {
        "controller": digest(Path(__file__).read_bytes()),
        "remote": digest(Path(__file__).with_name("build_runtime_image_remote_v2.py").read_bytes()),
        "inventory": digest(Path(__file__).with_name("runtime_image_inventory_v2.py").read_bytes()),
    }
    inputs = prepared_inputs()
    profile = {
        "schema_version": 1,
        "owner": OWNER,
        "namespace": NAMESPACE,
        "local_root": str(LOCAL_ROOT),
        "target": {"ip": "192.168.0.85", "hostname": "vm-media-01", "user": "jandrade", "port": 22},
        "sources": sources,
        "supports": SUPPORTS,
        "inputs": inputs,
        "image_built": False,
        "runtime_approved": False,
        "stages": list(STAGES[1:]),
        "limits": {
            "cli_memory_hard_limit": None,
            "archive_bytes": 256 * 1024**2,
            "expanded_bytes": 512 * 1024**2,
            "log_bytes": 1024**2,
            "docker_seconds": 180,
            "remote_seconds": 240,
            "ssh_seconds": 260,
        },
    }
    return {
        "profile": profile,
        "profile_sha256": digest(canonical(profile)),
        "readiness": {
            "ready": False,
            "inputs_ready": inputs is not None,
            "pending": (
                ["Falta prepare Python físico positivo y respaldado."] if inputs is None else []
            )
            + [
                "Root debe respaldar perfil, layout, daemon y rutas antes de prepare.",
                "Faltan OCI físicos base/final, DB, colisiones, ABI y SCA; no se autoriza ejecución de runtime.",
            ],
        },
    }


def read_values():
    # Esta función sólo se invoca en operación explícita de Root, nunca durante plan.
    return fixed("docker/valkey/capture_image_v2.py").read_values()


def previous(values, stage):
    value = json.loads(
        base64.b64decode(values[PREFIX + stage.upper() + "_RECEIPT_B64"], validate=True)
    )
    remote = source_module(Path(__file__).with_name("build_runtime_image_remote_v2.py"))
    remote.unpack(value)
    return value


def check_values(values, profile, checksum):
    expected = {
        "ROOT": NAMESPACE,
        "OWNER": OWNER,
        "LOCAL_ROOT": str(LOCAL_ROOT),
        "PROFILE_SHA256": checksum,
        "CONTROLLER_SHA256": profile["sources"]["controller"],
        "REMOTE_SHA256": profile["sources"]["remote"],
        "INVENTORY_SOURCE_SHA256": profile["sources"]["inventory"],
        "INPUTS_RECEIPT_PATH": profile["inputs"]["receipt_path"],
        "INPUTS_RECEIPT_SHA256": profile["inputs"]["receipt_sha256"],
    }
    require(
        all(values.get(PREFIX + name) == value for name, value in expected.items()),
        "Pins operativos no corresponden al perfil.",
    )


def write_exclusive(path, raw):
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def request_for(stage, profile, checksum, values):
    dependencies = {
        "prepare": (),
        "receive": ("prepare",),
        "base": ("prepare", "receive"),
        "build": ("prepare", "base"),
        "final": ("prepare", "build"),
        "fetch": ("prepare", "base", "final"),
    }
    files = {
        "inventory": Path(__file__).with_name("runtime_image_inventory_v2.py"),
        "parser": PROJECT / "docker/postgres-immich/final_image_remote.py",
        "layout": PROJECT / "docker/host_layout.py",
        "capture": PROJECT / "docker/valkey/capture_image_remote_v2.py",
    }
    sources = {name: base64.b64encode(path.read_bytes()).decode() for name, path in files.items()}
    return {
        "stage": stage,
        "profile": profile,
        "profile_sha256": checksum,
        "sources": sources,
        "layout": json.loads(base64.b64decode(values[PREFIX + "LAYOUT_B64"], validate=True)),
        "daemon": json.loads(base64.b64decode(values[PREFIX + "DAEMON_B64"], validate=True)),
        "previous": {name: previous(values, name) for name in dependencies[stage]},
    }


def exchange(stage, request, values, observed):
    support = fixed("docker/valkey/capture_image_v2.py")
    remote = Path(__file__).with_name("build_runtime_image_remote_v2.py").read_bytes()
    require(digest(remote) == request["profile"]["sources"]["remote"], "Fuente remota cambió.")
    connect = support.connector_from_source((PROJECT / "docker/build_component_v2.py").read_bytes())
    client, channel = None, None
    try:
        client = connect(values)
        channel = client.get_transport().open_session(timeout=10)
        channel.settimeout(10)
        channel.exec_command(support.remote_command(digest(remote)))
        for raw in (remote, canonical(request)):
            channel.sendall(struct.pack(">I", len(raw)) + raw)
        if stage == "receive":
            provider = reader()
            for name in sorted(INPUTS):
                raw = provider.read_regular(INPUT_ROOT / name, 128 * 1024**2)
                expected = request["profile"]["inputs"]["outputs"][name]
                require(
                    len(raw) == expected["bytes"] and digest(raw) == expected["sha256"],
                    "Entrada cambió antes de transferir.",
                )
                for offset in range(0, len(raw), 65536):
                    channel.sendall(raw[offset : offset + 65536])
        stream = support.ChannelStream(channel, 260)
        if stage == "fetch":
            header = stream.frame()
            base = request["previous"]["base"]["value"]["archive"]
            final = request["previous"]["final"]["value"]["archive"]
            require(
                header == {"artifacts": [base, final]}
                and {base["name"], final["name"]} == {"base.tar", "final.tar"},
                "Cabecera fetch no coincide con respaldos.",
            )
            access = reader().access_reader()
            access.check_access(LOCAL_ROOT.parent, private=True)
            os.mkdir(LOCAL_ROOT, 0o700)
            access.check_access(LOCAL_ROOT, private=True)
            info = LOCAL_ROOT.lstat()
            marker = canonical(
                {
                    "owner": OWNER,
                    "path": str(LOCAL_ROOT),
                    "directory": {"device": info.st_dev, "inode": info.st_ino},
                    "profile_sha256": request["profile_sha256"],
                }
            )
            write_exclusive(LOCAL_ROOT / ".owner.json", marker)
            for row in (base, final):
                support.receive_archive(
                    stream, LOCAL_ROOT / row["name"], row["bytes"], row["sha256"], require_eof=False
                )
                access.check_access(LOCAL_ROOT / row["name"], private=True)
            observed["local"] = {
                "directory": {"device": info.st_dev, "inode": info.st_ino},
                "marker_sha256": digest(marker),
                "artifacts": [base, final],
            }
        result = stream.frame()
        observed["remote"] = result
        stream.finish()  # Rechaza cualquier stderr y exit distinto de0; conserva observed.
        return result
    finally:
        if channel is not None:
            channel.close()
        if client is not None:
            client.close()


def validate_local_proof(prior, marker, info, checksum):
    require(
        prior.get("success") is True
        and prior.get("stage") == "fetch"
        and prior.get("owner") == OWNER
        and prior.get("profile_sha256") == checksum
        and stat.S_ISDIR(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & 0x400,
        "Fetch local no corresponde al perfil actual.",
    )
    local = prior["local"]
    current = {"device": info.st_dev, "inode": info.st_ino}
    require(
        local["directory"] == current and digest(marker) == local["marker_sha256"],
        "Identidad local distinta.",
    )
    require(
        json.loads(marker)
        == {
            "owner": OWNER,
            "path": str(LOCAL_ROOT),
            "directory": current,
            "profile_sha256": checksum,
        },
        "Marcador local no corresponde al perfil.",
    )


def gate(profile, values):
    provider = reader()
    access = provider.access_reader()
    prior = previous(values, "fetch")["value"]
    local = prior["local"]
    access.check_access(LOCAL_ROOT, private=True)
    info = LOCAL_ROOT.lstat()
    marker = provider.read_regular(LOCAL_ROOT / ".owner.json", 65536)
    validate_local_proof(prior, marker, info, digest(canonical(profile)))
    core = source_module(
        Path(__file__).with_name("runtime_image_inventory_v2.py"), profile["sources"]["inventory"]
    )
    parser = core.load_parser(
        (PROJECT / "docker/postgres-immich/final_image_remote.py").read_bytes()
    )
    files = {}
    for row in local["artifacts"]:
        require(row["name"] in {"base.tar", "final.tar"}, "Archive local fuera del cierre.")
        access.check_access(LOCAL_ROOT / row["name"], private=True)
        raw = provider.read_regular(LOCAL_ROOT / row["name"], 256 * 1024**2)
        require(len(raw) == row["bytes"] and digest(raw) == row["sha256"], "Archive local cambió.")
        files[row["name"]] = core.read_oci(
            raw,
            core.BASE_MANIFEST if row["name"] == "base.tar" else prior["identities"]["manifest"],
            parser,
        )
    inventory_raw = provider.read_regular(INPUT_ROOT / "inventory.json", 1024**2)
    require(
        digest(inventory_raw) == profile["inputs"]["outputs"]["inventory.json"]["sha256"],
        "Inventario preparado cambió.",
    )
    inventory = core.decode(inventory_raw)
    physical = core.verify_image(files["base.tar"], files["final.tar"], inventory, parser)
    security = {
        "kind": "python-runtime-image-sca-inputs-v1",
        "subject": {
            "manifest": physical["final_manifest"],
            "config": physical["final_config"],
            "archives": local["artifacts"],
        },
        "coverage": {"installed_apk": 19, "manual_apk_payloads": 14, "total_components": 33},
        "inventory": {
            "path": str(INPUT_ROOT / "inventory.json"),
            "sha256": digest(inventory_raw),
        },
        "sboms": {
            name: {"path": str(INPUT_ROOT / name), **profile["inputs"]["outputs"][name]}
            for name in ("base-os19.cdx.json", "manual-runtime14.cdx.json")
        },
        "python": inventory["python"],
        "stdlib": inventory["stdlib"],
        "manual_components": [
            {
                "name": row["name"],
                "version": row["version"],
                "archive_sha256": row["sha256"],
                "apk_db_installed": False,
            }
            for row in inventory["manual_materials"]
        ],
        "native_files": {
            name: {
                "sha256": row["sha256"],
                "needed": row["elf"]["needed"],
                "soname": row["elf"]["soname"],
            }
            for name, row in files["final.tar"]["files"].items()
            if row.get("elf")
        },
        "gaps": inventory["gaps"] + physical["gaps"],
        "runtime_approved": False,
        "sca_approved": False,
        "binary_executed": False,
    }
    outputs = {
        "physical-verification.json": canonical(physical),
        "image-security-inputs.json": canonical(security),
    }
    for name, raw in outputs.items():
        write_exclusive(LOCAL_ROOT / name, raw)
        access.check_access(LOCAL_ROOT / name, private=True)
        require(
            provider.read_regular(LOCAL_ROOT / name, 8 * 1024**2) == raw, "Output local cambió."
        )
    return {
        "success": True,
        "stage": "gate",
        "owner": OWNER,
        "profile_sha256": digest(canonical(profile)),
        "outputs": {
            name: {"path": str(LOCAL_ROOT / name), "sha256": digest(raw), "bytes": len(raw)}
            for name, raw in outputs.items()
        },
        "runtime_approved": False,
        "sca_approved": False,
    }


def operate(stage, proposal_value, values):
    profile, checksum = proposal_value["profile"], proposal_value["profile_sha256"]
    require(profile["inputs"] is not None, "Faltan inputs preparados auténticos.")
    check_values(values, profile, checksum)
    prefix = PROJECT / ".tools/evidence" / ("phase4-python-runtime-image-v2-" + stage)
    intent, result_path = prefix.with_suffix(".intent.json"), prefix.with_suffix(".result.json")
    require(not os.path.lexists(result_path), "No repetir una etapa con resultado previo.")
    write_exclusive(intent, canonical({"stage": stage, "profile_sha256": checksum, "owner": OWNER}))
    observed = {}
    try:
        if stage == "gate":
            result = {"value": gate(profile, values)}
            result["sha256"] = digest(canonical(result["value"]))
        else:
            request = request_for(stage, profile, checksum, values)
            result = exchange(stage, request, values, observed)
            remote = source_module(
                Path(__file__).with_name("build_runtime_image_remote_v2.py"),
                profile["sources"]["remote"],
            )
            value = remote.unpack(result)
            require(
                value.get("success") is True
                and value.get("stage") == stage
                and value.get("owner") == OWNER
                and value.get("path") == NAMESPACE
                and value.get("profile_sha256") == checksum
                and value.get("runtime_approved") is False
                and value.get("binary_executed") is False,
                "Resultado remoto no corresponde al perfil.",
            )
            if stage == "fetch":
                result = remote.envelope({**value, "local": observed["local"]})
        raw = canonical(result)
        write_exclusive(result_path, raw)
    except Exception as failure:
        write_exclusive(
            result_path,
            canonical(
                {
                    "success": False,
                    "verified": False,
                    "stage": stage,
                    "error_type": type(failure).__name__,
                    "observed": observed,
                    "runtime_approved": False,
                }
            ),
        )
        raise
    updates = {
        PREFIX + stage.upper() + "_RECEIPT_PATH": str(result_path),
        PREFIX + stage.upper() + "_RECEIPT_SHA256": digest(raw),
        PREFIX + stage.upper() + "_RECEIPT_B64": base64.b64encode(raw).decode(),
    }
    fixed(".tools/vault_updates.py").update(updates, comments={name: COMMENTS for name in updates})
    return {
        "success": True,
        "stage": stage,
        "receipt_path": str(result_path),
        "receipt_sha256": digest(raw),
        "runtime_approved": False,
        "sca_approved": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", choices=STAGES, default="plan")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = proposal()
        if args.stage == "plan":
            result = plan
        else:
            require(
                args.execute
                and args.root_update_vault
                and args.expected_profile_sha256 == plan["profile_sha256"]
                and plan["readiness"]["inputs_ready"],
                "Faltan autorización explícita, perfil respaldado o inputs reales.",
            )
            result = operate(args.stage, plan, read_values())
        print(canonical(result).decode())
        return 0
    except Exception as failure:
        print(
            canonical(
                {
                    "success": False,
                    "verified": False,
                    "stage": args.stage,
                    "error_type": type(failure).__name__,
                    "error": "PYTHON_IMAGE_OPERATION_REJECTED",
                    "runtime_approved": False,
                }
            ).decode()
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
