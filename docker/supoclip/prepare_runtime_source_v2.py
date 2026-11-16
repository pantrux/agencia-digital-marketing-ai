"""Proyecta la corrección del exportador ASS sobre la fuente SupoClip V1 fijada."""

import argparse
import hashlib
import io
import json
import os
import tarfile
import types
from pathlib import Path

CODE = Path(__file__).resolve().parent
PREVIOUS = "2470847c15e73ac6ebd8ee801b39ce61bf57d62985adb0bd957b9331be30ca03"
PREVIOUS_PLAN = "2958f45d225bb1a910cec159c1cd0952c23fbf8573cd65c883b16362c6718b9f"
PREVIOUS_BACKEND = "075be34ef695acd38c9a02b27a50c18445b715ed9fa6a91fb62b7f5c98e641c8"
PREVIOUS_FRONTEND = "4af3e14ed7a614ac3e8c067d04f536d6713b4916d25c13105fb1ff99bb0fe778"
EDITOR_BEFORE = "e571bf0fc2722f323abc764f91a9e02887c818ef9bb456fc8a040bd74aff8198"
ASS_PATCHES = (("backend/src/editor_document.py", "render_document", 1),)
OWNER = "agencia-digital-marketing-ai:supoclip-runtime-source:v2"
MARKER = ".agency-supoclip-runtime-source-owner.json"


def load_previous():
    source = CODE / "prepare_runtime_source.py"
    with source.open("rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    if hashlib.sha256(raw).hexdigest() != PREVIOUS:
        raise ValueError("El preparador V1 cambió.")
    module = types.ModuleType("supoclip_runtime_source_v1")
    module.__file__ = str(source)
    exec(compile(raw, str(source), "exec"), module.__dict__)
    return module


def patch_editor(previous, raw):
    _path, function, count = ASS_PATCHES[0]
    if b'f"ass=' not in raw or b'"ffprobe"' not in raw:
        raise ValueError("La función de exportación no coincide con el contrato ASS.")
    return previous.patch_ass(raw, function, count)


def build(previous):
    v1, archives = previous.build()
    backend = "backend-source.tar"
    frontend = "frontend-font-overlay.tar"
    if (
        previous.digest(previous.canonical(v1)) != PREVIOUS_PLAN
        or previous.digest(archives[backend]) != PREVIOUS_BACKEND
        or previous.digest(archives[frontend]) != PREVIOUS_FRONTEND
    ):
        raise ValueError("La proyección V1 cambió.")
    members = v1["archives"][backend]["members"]
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archives[backend]), mode="r:") as archive:
        for item in archive:
            if not item.isfile() or item.name not in members or item.name in files:
                raise ValueError("El archivo V1 contiene miembros distintos.")
            raw = archive.extractfile(item).read(item.size + 1)
            if members[item.name] != {"bytes": len(raw), "sha256": previous.digest(raw)}:
                raise ValueError("El archivo V1 cambió.")
            files[item.name] = raw
    if set(files) != set(members):
        raise ValueError("El archivo V1 quedó incompleto.")
    path, function, count = ASS_PATCHES[0]
    if previous.digest(files[path]) != EDITOR_BEFORE:
        raise ValueError("La preimagen del exportador cambió.")
    files[path] = patch_editor(previous, files[path])
    output = previous.make_tar(files)
    archives[backend] = output
    backend_receipt = {
        "bytes": len(output),
        "sha256": previous.digest(output),
        "members": {
            **members,
            path: {"bytes": len(files[path]), "sha256": previous.digest(files[path])},
        },
    }
    plan = {
        **v1,
        "owner": OWNER,
        "previous_plan_sha256": PREVIOUS_PLAN,
        "previous_backend_sha256": PREVIOUS_BACKEND,
        "added_ass_patch": {"path": path, "function": function, "argv": count},
        "archives": {**v1["archives"], backend: backend_receipt},
    }
    return plan, archives


def publish(previous, plan, archives):
    parent = previous.PARENT
    destination = parent / "supoclip-runtime-source-v2"
    acl_path = previous.ROOT / "sre/backup_tools_access.py"
    raw = previous.checked(acl_path, previous.ACL_PIN, 64 * 1024)
    acl = types.ModuleType("supoclip_runtime_source_v2_acl")
    exec(compile(raw, str(acl_path), "exec"), acl.__dict__)
    acl.check_access(parent, private=True)
    previous.checked(parent / ".agency-backup-build-owner", previous.PARENT_PIN, 1024)
    if parent.resolve(strict=True) != parent:
        raise ValueError("La raíz privada no es canónica.")
    destination.mkdir(mode=0o700)
    acl.check_access(destination, private=True)
    info = destination.lstat()
    identity = {"owner": OWNER, "dev": info.st_dev, "ino": info.st_ino}
    entries = {
        MARKER: previous.canonical(identity),
        **archives,
        "receipt.json": previous.canonical({**plan, "identity": identity}),
    }
    for name, raw in entries.items():
        path = destination / name
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        previous.checked(path, previous.digest(raw), len(raw))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", metavar="SHA256")
    args = parser.parse_args()
    previous = load_previous()
    plan, archives = build(previous)
    pin = previous.digest(previous.canonical(plan))
    if args.publish:
        if args.publish != pin:
            raise ValueError("El SHA del plan V2 no coincide.")
        publish(previous, plan, archives)
    print(json.dumps({"plan_sha256": pin, **plan}, sort_keys=True))


if __name__ == "__main__":
    main()
