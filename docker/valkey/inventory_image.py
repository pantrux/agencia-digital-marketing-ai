"""Plan local predeterminado y publicación explícita de Root; sin scanner/red."""

import argparse
import hashlib
import json
import os
import re
import stat
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = ROOT / ".tools/evidence/phase4-valkey-inventory-v1"
OWNER = "agencia-digital-marketing-ai:valkey-inventory:v1"
READER = "docker/kitsu-zou/recover_opencv_sdk_scan_v2.py"
READER_SHA = "b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c"
PARSER = "docker/postgres-immich/final_image_remote.py"
PARSER_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
CORE_SHA = "2d1f335fbb458d34c80b3a2c0fa993d75bc5cf0858377ac9bb1a25966976ee6d"
ARCHIVE = ".tools/cache/phase4-built-images/valkey-de6fbe9626fea9f6-v2.tar"
ARCHIVE_SHA = "046dc47ec1d369d3d916190ff3b62e91a45585833faf1fddb141372813bb904e"
REFERENCES = {
    "candidate": (
        ".tools/evidence/phase4-valkey-alpine-candidate.json",
        "74decf8f583aaf3ba0ca6f4c2aa708c6147c863fb1f5ab2f75fc25ac5e813096",
    ),
    "freeze": (
        ".tools/evidence/phase4-valkey-oci-capture-v2-final-freeze.json",
        "743580caecd753b88a4080bbff9eff18354029e4dae24dae8156ec1992185831",
    ),
    "profile": (
        ".tools/evidence/phase4-valkey-oci-capture-v2-profile.json",
        "79fa154319aff2a94300ab976c949cfd0f5028c841a46feb0278a2ad08558278",
    ),
    "capture": (
        ".tools/evidence/phase4-valkey-oci-capture-v2-capture.result.json",
        "cc207619ec3e86f050cfec18aed0429ac8b7eb49a4f2550fc199dd18d0c38953",
    ),
    "fetch": (
        ".tools/evidence/phase4-valkey-oci-capture-v2-fetch.result.json",
        "1ae9e1e9c11832987ef83ea887ad751e948ac97ccaf4ccdb987c6079a7440476",
    ),
}


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def module_raw(name, raw, path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def pinned_module(reader, name, path, pin):
    raw = reader.read_exact(path, 128 * 1024, pin)
    require(digest(raw) == pin, "HELPER_BYTES")
    return module_raw(name, raw, path), raw


def checked_directory(path):
    path = Path(path).absolute()
    require(path.resolve(strict=True) == path, "DIRECTORY_CANONICAL")
    for node in (*reversed(path.parents), path):
        info = node.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "DIRECTORY_LINK",
        )
    return path


def load_reader():
    path = ROOT / READER
    checked_directory(path.parent)
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and not getattr(info, "st_file_attributes", 0) & 0x400
        and info.st_size <= 128 * 1024,
        "READER_TYPE",
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(128 * 1024 + 1)
        after = path.lstat()
    require(
        (info.st_dev, info.st_ino, info.st_size)
        == (opened.st_dev, opened.st_ino, opened.st_size)
        == (after.st_dev, after.st_ino, after.st_size)
        and digest(raw) == READER_SHA,
        "READER_BYTES",
    )
    return module_raw("valkey_fixed_reader", raw, path)


def envelope(wrapper, stage):
    require(type(wrapper) is dict and set(wrapper) == {"value", "sha256"}, "RECEIPT_FIELDS")
    value = wrapper["value"]
    require(
        type(value) is dict
        and digest(canonical(value)) == wrapper["sha256"]
        and value.get("success") is True
        and value.get("stage") == stage
        and value.get("binary_accepted") is False,
        "RECEIPT_IDENTITY",
    )
    return value


def prepare():
    reader = load_reader()
    parser, _ = pinned_module(reader, "valkey_frozen_pure_oci", ROOT / PARSER, PARSER_SHA)
    core_path = Path(__file__).with_name("inventory_core.py")
    core, core_raw = pinned_module(reader, "valkey_inventory_core_fixed_bytes", core_path, CORE_SHA)
    references, documents = {}, {}
    for key, (relative, pin) in REFERENCES.items():
        raw = reader.read_exact(ROOT / relative, 1024**2, pin)
        documents[key] = parser.json_object(raw)
        references[key] = {"path": relative, "sha256": pin, "bytes": len(raw)}
    candidate, freeze, profile = (documents[key] for key in ("candidate", "freeze", "profile"))
    require(
        profile["candidate"] == candidate
        and freeze["profile"]["sha256"] == REFERENCES["profile"][1],
        "PROFILE_BINDING",
    )
    for relative, row in freeze["implementation_files"].items():
        raw = reader.read_exact(ROOT / relative, 128 * 1024, row["sha256"])
        require(len(raw) == row["bytes"], "FROZEN_SOURCE_SIZE")
    require(
        profile["source_sha256"]
        == freeze["implementation_files"]["docker/valkey/capture_image_v2.py"]["sha256"]
        and profile["remote_sha256"]
        == freeze["implementation_files"]["docker/valkey/capture_image_remote_v2.py"]["sha256"],
        "CAPTURE_SOURCE_BINDING",
    )
    capture, fetch = (
        envelope(documents["capture"], "capture"),
        envelope(documents["fetch"], "fetch"),
    )
    require(
        fetch["capture_receipt_sha256"] == documents["capture"]["sha256"]
        and all(
            capture[key] == fetch[key]
            for key in ("owner", "path", "directory", "marker_sha256", "profile_sha256")
        ),
        "CAPTURE_FETCH_CHAIN",
    )
    require(
        capture["owner"] == profile["owner"]
        and capture["path"] == profile["namespace"]
        and capture["profile_sha256"] == REFERENCES["profile"][1],
        "CAPTURE_PROFILE",
    )
    require(
        capture["oci"]["manifest_digest"] == candidate["platform_digest"]
        and capture["oci"]["config_digest"] == candidate["manifest"]["config"]["digest"]
        and capture["oci"]["layers_verified"] == len(candidate["manifest"]["layers"]) == 7
        and capture["oci"]["sca_executed"] is False,
        "CAPTURE_OCI",
    )
    archive = reader.hash_file(ROOT / ARCHIVE, core.MAX_ARCHIVE)
    raw = reader.read_exact(ROOT / ARCHIVE, core.MAX_ARCHIVE, ARCHIVE_SHA)
    local = fetch["local_archive"]
    require(
        Path(local["path"]) == ROOT / ARCHIVE
        and local["sha256"]
        == fetch["archive_sha256"]
        == capture["archive"]["sha256"]
        == ARCHIVE_SHA
        and local["size"]
        == fetch["archive_bytes"]
        == capture["archive"]["size"]
        == len(raw)
        == 18843648,
        "ARCHIVE_CHAIN",
    )
    # El campo histórico 'directory' contiene la identidad del archivo, no una carpeta.
    require(
        local["directory"] == {key: archive["fstat"][key] for key in ("device", "inode")},
        "ARCHIVE_FILE_IDENTITY",
    )
    parsed = core.parse_oci(raw, parser, candidate)
    value = core.inventory(parsed, parser)
    require(
        value["os"]["identity"]["VERSION_ID"] == "3.24.3"
        and len(value["os"]["packages"]) == 23
        and len(value["binaries"]) == 3,
        "FROZEN_INVENTORY_COUNTS",
    )
    provenance = {
        "references": references,
        "archive": archive,
        "archive_path": ARCHIVE,
        "capture_receipt_sha256": documents["capture"]["sha256"],
        "fetch_receipt_sha256": documents["fetch"]["sha256"],
        "candidate_index_digest_declared": candidate["index_digest"],
        "index_link_independently_verified": False,
        "manifest_digest": parsed["manifest_digest"],
        "config_digest": parsed["config_digest"],
        "config_document": parsed["config"],
        "layers": parsed["layers"],
        "parser_sha256": PARSER_SHA,
        "binary_to_source_binding_verified": False,
    }
    binary_materials = {
        key: value[key]
        for key in (
            "source",
            "binaries",
            "aliases",
            "embedded_candidates",
            "dynamic_link_structure",
            "shipped_spdx",
            "shipped_spdx_sha256",
            "licenses",
            "gaps",
            "binary_sca_verified",
            "embedded_libraries_complete",
            "binary_accepted",
        )
    }
    outputs = {
        "inventory.json": core.canonical(value),
        "file-catalog.json": core.canonical(core.file_catalog(parsed)),
        "os-packages.cdx.json": core.canonical(core.os_sbom(value)),
        "binary-materials.json": core.canonical(binary_materials),
        "provenance.json": core.canonical(provenance),
    }
    source = reader.read_exact(Path(__file__), 128 * 1024)
    plan = {
        "schema_version": 1,
        "kind": "valkey-inventory-plan",
        "owner": OWNER,
        "run_root": str(RUN_ROOT),
        "sources": {
            "local": digest(source),
            "core": digest(core_raw),
            "parser": PARSER_SHA,
            "reader": READER_SHA,
        },
        "references": references,
        "archive": archive,
        "outputs": {
            name: {"sha256": digest(data), "bytes": len(data)} for name, data in outputs.items()
        },
        "counts": {
            "layers": len(parsed["layers"]),
            "expanded_bytes": parsed["expanded_bytes"],
            "nodes": len(parsed["files"]),
            "apk": len(value["os"]["packages"]),
            "native_binaries": len(value["binaries"]),
            "aliases": len(value["aliases"]),
        },
        "limits": {
            "archive_bytes": core.MAX_ARCHIVE,
            "expanded_bytes": core.MAX_EXPANDED,
            "layer_bytes": core.MAX_LAYER,
            "member_bytes": core.MAX_MEMBER,
            "nodes": core.MAX_NODES,
        },
        "sca_executed": False,
        "binary_accepted": False,
        "runtime_executed": False,
    }
    require(reader.hash_file(ROOT / ARCHIVE, core.MAX_ARCHIVE) == archive, "ARCHIVE_CHANGED")
    return plan, outputs


def write_new(path, raw):
    require(type(raw) is bytes and len(raw) <= 16 * 1024**2, "OUTPUT_LIMIT")
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def publish(target, plan, outputs):
    checked_directory(target.parent)
    os.mkdir(target, 0o700)
    info = target.lstat()
    identity = {"device": info.st_dev, "inode": info.st_ino}
    try:
        require(
            outputs
            and all(
                re.fullmatch(r"[a-z][a-z0-9-]*\.json", name)
                and name not in {"receipt.json", "failure.json"}
                for name in outputs
            ),
            "OUTPUT_NAMES",
        )
        require(
            plan.get("outputs")
            == {name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in outputs.items()},
            "OUTPUT_PLAN",
        )
        write_new(
            target / ".owner.json",
            canonical(
                {
                    "owner": OWNER,
                    "path": str(target),
                    "directory": identity,
                    "plan_sha256": digest(canonical(plan)),
                }
            ),
        )
        write_new(target / "plan.json", canonical(plan))
        reader = load_reader()
        for name, raw in outputs.items():
            write_new(target / name, raw)
            require(
                reader.read_exact(target / name, 16 * 1024**2, digest(raw)) == raw, "OUTPUT_CHANGED"
            )
        require(
            (target.lstat().st_dev, target.lstat().st_ino) == (info.st_dev, info.st_ino),
            "PUBLICATION_DIRECTORY_CHANGED",
        )
        receipt = {
            "schema_version": 1,
            "owner": OWNER,
            "directory": identity,
            "plan_sha256": digest(canonical(plan)),
            "artifacts_published": True,
            "outputs": plan["outputs"],
            "binary_accepted": False,
            "sca_executed": False,
            "runtime_executed": False,
        }
        write_new(target / "receipt.json", canonical(receipt))
        return receipt
    except BaseException:
        write_new(
            target / "failure.json",
            canonical(
                {
                    "artifacts_published": False,
                    "binary_accepted": False,
                    "error": "INVENTORY_PUBLICATION_INCOMPLETE",
                }
            ),
        )
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    if args.publish:
        require(
            all(
                re.fullmatch(r"[0-9a-f]{64}", value or "")
                for value in (args.expected_source_sha256, args.expected_plan_sha256)
            ),
            "ROOT_PINS_REQUIRED",
        )
    plan, outputs = prepare()
    if not args.publish:
        print(
            json.dumps(
                {
                    "plan": plan,
                    "plan_sha256": digest(canonical(plan)),
                    "artifacts_published": False,
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    require(
        plan["sources"]["local"] == args.expected_source_sha256
        and digest(canonical(plan)) == args.expected_plan_sha256,
        "EXTERNAL_PLAN_PIN",
    )
    print(json.dumps(publish(RUN_ROOT, plan, outputs), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "artifacts_published": False,
                    "binary_accepted": False,
                    "error": "INVENTORY_REJECTED",
                }
            )
        )
        raise SystemExit(1) from None
