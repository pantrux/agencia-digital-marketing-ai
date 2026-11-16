"""Overlay Speaches fijado: plan por defecto; publicación nueva sólo por hash."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import stat
from pathlib import Path

from speaches_client import _file
from speaches_contract import SpeachesError

CODE = Path(__file__).absolute().parent
OWNER = "agencia-digital-marketing-ai:supoclip-speaches-overlay:v1"
COMMIT = "f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38"
PINS = {
    "backend/src/config.py": "1f5fe8f0f78568429c8e58c2cadce251adb612538ddd0ee4e58388af9c1694ca",
    "backend/src/media/common.py": "50f14a1ccac20833c253e09d250fbc8675660800346b9ddfff6a4982f96f3713",
    "backend/src/media/transcription.py": "19c7dc73a165f51d7c2bd96fcbdeee5db7e1be1629caf471794fefa37189cae2",
}
HELPERS = ("speaches_client.py", "speaches_contract.py", "speaches_provider.py")


class PatchError(ValueError):
    def __init__(self, code, created_root=None):
        super().__init__(code)
        self.code = code
        self.created_root = created_root


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PatchError("PATCH_MANIFEST_INVALID")
        result[key] = value
    return result


def _read(path, maximum=256 * 1024):
    try:
        with _file(path, maximum) as (stream, info):
            return stream.read(maximum + 1), {
                "dev": info.st_dev,
                "ino": info.st_ino,
                "bytes": info.st_size,
            }
    except (SpeachesError, OSError, ValueError, TypeError):
        raise PatchError("PATCH_INPUT_REJECTED") from None


def manifest():
    raw, _identity = _read(CODE / "source-patches.json")
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if (
            set(value) != {"schema", "upstream", "license", "files", "helpers", "ffmpeg_reference"}
            or value["schema"] != 1
            or value["upstream"] != {"repository": "FujiwaraChoki/supoclip", "commit": COMMIT}
            or len(value["files"]) != 3
            or {x["path"] for x in value["files"]} != set(PINS)
            or [x["source"] for x in value["helpers"]] != list(HELPERS)
        ):
            raise ValueError("schema")
        for item in value["files"]:
            source = item["source_text"].encode("utf-8")
            blob = hashlib.sha1(
                b"blob " + str(len(source)).encode() + b"\0" + source, usedforsecurity=False
            ).hexdigest()
            if (
                digest(source) != PINS[item["path"]]
                or item["before_sha256"] != PINS[item["path"]]
                or blob != item["blob"]
            ):
                raise ValueError("pin")
        if (
            digest(value["license"]["text"].encode("utf-8"))
            != "2d57b88304ae76aa40722515c6f740ee8f6473e9d057c3e29b8ba08eeebd4e38"
        ):
            raise ValueError("license")
        return value, digest(raw)
    except PatchError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise PatchError("PATCH_MANIFEST_INVALID") from None


def patch_source(raw, item):
    try:
        if digest(raw) != PINS[item["path"]]:
            raise ValueError("before")
        text = raw.decode("utf-8")
        for change in item["patches"]:
            if not change["before"] or text.count(change["before"]) != 1:
                raise ValueError("count")
            text = text.replace(change["before"], change["after"], 1)
        result = text.encode("utf-8")
        if digest(result) != item["after_sha256"]:
            raise ValueError("after")
        ast.parse(text)
        return result
    except (ValueError, KeyError, UnicodeError, SyntaxError):
        raise PatchError("PATCH_SOURCE_MISMATCH") from None


def _destination(value):
    path = Path(value).absolute()
    if ".." in path.parts or path.exists() or path.is_symlink():
        raise PatchError("PATCH_DESTINATION_REJECTED")
    try:
        for parent in reversed(path.parents):
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise PatchError("PATCH_DESTINATION_REJECTED")
        parent = path.parent.stat()
        if os.name == "posix" and (
            parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise PatchError("PATCH_DESTINATION_REJECTED")
        return path
    except OSError:
        raise PatchError("PATCH_DESTINATION_REJECTED") from None


def make_plan(source_root, destination):
    source_root = Path(source_root).absolute()
    destination = _destination(destination)
    if destination.is_relative_to(source_root) or source_root.is_relative_to(destination):
        raise PatchError("PATCH_DESTINATION_REJECTED")
    value, manifest_hash = manifest()
    outputs, inputs = {}, []
    for item in value["files"]:
        raw, identity = _read(source_root / item["path"])
        outputs[item["path"]] = patch_source(raw, item)
        inputs.append({"path": item["path"], "sha256": digest(raw), **identity})
    for name, item in zip(HELPERS, value["helpers"], strict=True):
        raw, _identity = _read(CODE / name)
        if item["target"] != "backend/" + name or digest(raw) != item["sha256"]:
            raise PatchError("PATCH_HELPER_MISMATCH")
        outputs[item["target"]] = raw
    outputs["LICENSE-SupoClip-AGPL-3.0.txt"] = value["license"]["text"].encode("utf-8")
    plan = {
        "schema": 1,
        "owner": OWNER,
        "commit": COMMIT,
        "source_root": str(source_root),
        "destination": str(destination),
        "manifest_sha256": manifest_hash,
        "patcher_sha256": digest(Path(__file__).read_bytes()),
        "inputs": inputs,
        "outputs": [
            {"path": name, "sha256": digest(raw), "bytes": len(raw), "mode_posix": "0444"}
            for name, raw in sorted(outputs.items())
        ],
    }
    return plan, outputs


def apply_overlay(source_root, destination, expected_plan_hash):
    plan, outputs = make_plan(source_root, destination)
    if digest(canonical(plan)) != expected_plan_hash:
        raise PatchError("PATCH_PLAN_STALE")
    root = Path(plan["destination"])
    try:
        root.mkdir(mode=0o700)
    except OSError:
        raise PatchError("PATCH_DESTINATION_REJECTED") from None
    info = root.lstat()
    identity = {"path": str(root), "dev": info.st_dev, "ino": info.st_ino, "owner": OWNER}
    try:
        marker = root / ".agency-supoclip-speaches-overlay"
        with marker.open("xb") as stream:
            stream.write((OWNER + "\n").encode())
        marker.chmod(0o600)
        for name, raw in outputs.items():
            path = root / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o444)
            if digest(path.read_bytes()) != digest(raw):
                raise OSError("publication")
        receipt = {**identity, "plan_sha256": expected_plan_hash, "outputs": plan["outputs"]}
        with (root / "overlay-receipt.json").open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, sort_keys=True, indent=2)
            stream.write("\n")
        (root / "overlay-receipt.json").chmod(0o600)
        return {
            "status": "created",
            **identity,
            "plan_sha256": expected_plan_hash,
            "files": len(outputs),
        }
    except (OSError, ValueError):
        # Se conserva la raíz nueva acreditada para revisión; nunca borrar por patrón.
        raise PatchError("PATCH_PUBLICATION_FAILED", created_root=identity) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            result = apply_overlay(args.source_root, args.destination, args.plan_sha256)
        else:
            plan, _outputs = make_plan(args.source_root, args.destination)
            result = {"status": "plan", "plan_sha256": digest(canonical(plan)), "plan": plan}
        print(json.dumps(result, sort_keys=True))
        return 0
    except PatchError as error:
        print(
            json.dumps({"status": "error", "error": error.code, "created_root": error.created_root})
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
