"""Operación Root finita; no SSH, Docker, credenciales generadas ni efectos al importar."""

import argparse
import base64
import hashlib
import json
import os
import signal
import stat
import sys
import types
from contextlib import ExitStack

CORE_SHA = "71993f702dd3a442f2b6b53cb41b5ce7d8bf75d4279f01e4f96d8376f16babe5"
LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"
MAX_REQUEST = 256 * 1024


def require(condition, code):
    if not condition:
        raise ValueError(code)


def load_raw(name, encoded, expected):
    require(type(encoded) is str and len(encoded) <= MAX_REQUEST, "SOURCE_ENCODING")
    raw = base64.b64decode(encoded, validate=True)
    require(hashlib.sha256(raw).hexdigest() == expected, "SOURCE_SHA")
    module = types.ModuleType(name)
    module.__file__ = "<" + name + ">"
    sys.modules[name] = module
    exec(compile(raw, module.__file__, "exec"), module.__dict__)
    return module


def identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
    )


def check_directory(info, uid=0):
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == info.st_gid == uid
        and stat.S_IMODE(info.st_mode) == 0o700,
        "DIRECTORY_POLICY",
    )


def create_directory(parent, name, uid, stack):
    # Nombre interno fijo; mkdir nunca adopta un nodo previo.
    require(
        type(name) is str and name and "/" not in name and name not in {".", ".."}, "DIRECTORY_NAME"
    )
    os.mkdir(name, 0o700, dir_fd=parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    stack.callback(os.close, fd)
    os.fchown(fd, uid, uid)
    os.fchmod(fd, 0o700)
    info, named = os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)
    check_directory(info, uid)
    require(stamp(info) == stamp(named), "DIRECTORY_CHANGED")
    os.fsync(parent)
    return fd


def verify_file(parent, name, expected):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == expected["uid"]
            and before.st_gid == expected["gid"]
            and stat.S_IMODE(before.st_mode) == int(expected["mode"], 8)
            and before.st_size == expected["bytes"],
            "FILE_POLICY",
        )
        raw = bytearray()
        while len(raw) <= expected["bytes"]:
            chunk = os.read(fd, min(16384, expected["bytes"] + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        require(
            stamp(before)
            == stamp(os.fstat(fd))
            == stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
            and len(raw) == expected["bytes"]
            and hashlib.sha256(raw).hexdigest() == expected["sha256"],
            "FILE_CHANGED",
        )
        return {
            "directory_entry": name,
            **identity(before),
            "uid": before.st_uid,
            "gid": before.st_gid,
            "mode": expected["mode"],
            "bytes": before.st_size,
            "sha256": expected["sha256"],
        }
    finally:
        os.close(fd)


def write_file(parent, name, raw, metadata):
    require(type(name) is str and name and "/" not in name and name not in {".", ".."}, "FILE_NAME")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        os.fchown(fd, metadata["uid"], metadata["gid"])
        os.fchmod(fd, int(metadata["mode"], 8))
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            require(written > 0, "SHORT_WRITE")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent)
    return verify_file(parent, name, metadata)


def verify_layout(layout, proof, filesystem):
    host = layout.inspect_host()
    layout.host_contract(host, apply=True)
    current = layout.make_plan(filesystem.inspect(), host)["inventory"]["targets"]
    require(
        type(proof) is dict
        and proof.get("success") is True
        and proof.get("owner") == layout.OWNER
        and type(proof.get("targets")) is list
        and len(proof["targets"]) == 2
        and {row["path"] for row in proof["targets"]}
        == {"/opt/media-platform", "/var/lib/media-platform"},
        "LAYOUT_PROOF",
    )
    for row in proof["targets"]:
        actual = current[row["path"]]
        require(
            layout.directory_identity(actual["node"]) == row["directory"]
            and layout.digest(actual["marker"]) == row["marker_sha256"]
            and layout.digest(actual["receipt"]) == row["receipt_sha256"],
            "LAYOUT_CHANGED",
        )


def prepare(core, layout, proof, plan, files, result):
    require(sys.platform == "linux" and os.geteuid() == os.getegid() == 0, "ROOT_LINUX_REQUIRED")
    core.validate_bundle(plan, files)
    checksum = core.digest(core.canonical(plan))
    with layout.LocalFilesystem() as filesystem, ExitStack() as stack:
        verify_layout(layout, proof, filesystem)
        parents = {}
        for role, path in core.TARGETS.items():
            fd = filesystem.open_target(path.rsplit("/", 1)[0])
            stack.callback(os.close, fd)
            check_directory(os.fstat(fd))
            parents[role] = fd
        # Ambas ausencias se comprueban antes de la primera escritura.
        for role in core.TARGETS:
            try:
                os.stat("kitsu-base-v1", dir_fd=parents[role], follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("TARGET_COLLISION")
        roots = {}
        for role, path in core.TARGETS.items():
            fd = create_directory(parents[role], "kitsu-base-v1", 0, stack)
            roots[role] = fd
            row = {
                "path": path,
                "directory": identity(os.fstat(fd)),
                "owner": core.OWNER,
                "plan_sha256": checksum,
                "marker_written": False,
            }
            result["targets"][role] = row
            marker = core.canonical(
                {
                    "schema": 1,
                    "role": role,
                    "path": path,
                    "owner": core.OWNER,
                    "directory": row["directory"],
                    "plan_sha256": checksum,
                }
            )
            metadata = {
                "uid": 0,
                "gid": 0,
                "mode": "0600",
                "bytes": len(marker),
                "sha256": core.digest(marker),
            }
            write_file(fd, core.MARKER, marker, metadata)
            row.update(marker_written=True, marker_sha256=metadata["sha256"])
        secrets = create_directory(roots["config"], "secrets", 0, stack)
        directories = {"": roots["data"]}
        for name, uid in core.DATA_DIRS.items():
            parent, _, leaf = name.rpartition("/")
            fd = create_directory(directories[parent], leaf, uid, stack)
            directories[name] = fd
            result["data_directories"][name] = {
                **identity(os.fstat(fd)),
                "uid": uid,
                "gid": uid,
                "mode": "0700",
            }
        for name in core.FILE_POLICY:
            parent = roots["config"] if name == "valkey.conf" else secrets
            result["files"][name] = write_file(parent, name, files[name], plan["files"][name])
        # Relectura de todos los bytes y de los padres atestados antes de éxito.
        for name in core.FILE_POLICY:
            parent = roots["config"] if name == "valkey.conf" else secrets
            require(
                verify_file(parent, name, plan["files"][name]) == result["files"][name],
                "FINAL_FILE_CHANGED",
            )
        verify_layout(layout, proof, filesystem)
        for role, fd in roots.items():
            info = os.fstat(fd)
            check_directory(info)
            require(
                identity(info) == result["targets"][role]["directory"]
                and stamp(info)
                == stamp(os.stat("kitsu-base-v1", dir_fd=parents[role], follow_symlinks=False)),
                "FINAL_ROOT_CHANGED",
            )
    result["success"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args()
    result = {
        "schema": 1,
        "success": False,
        "runtime_verified": False,
        "production_approved": False,
        "history_preserved": True,
        "targets": {},
        "data_directories": {},
        "files": {},
    }

    def deadline(*_):
        raise TimeoutError("PREPARATION_DEADLINE")

    try:
        # La operación Root tiene un plazo global; el modo plan no requiere Linux.
        if args.prepare:
            signal.signal(signal.SIGALRM, deadline)
            signal.alarm(30)
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        require(len(raw) <= MAX_REQUEST, "REQUEST_SIZE")
        req = json.loads(raw, object_pairs_hook=lambda pairs: unique(pairs))
        expected = {"core_b64", "plan"} | (
            {"layout_b64", "layout_proof", "files_b64"} if args.prepare else set()
        )
        require(type(req) is dict and set(req) == expected, "REQUEST_FIELDS")
        core = load_raw("kitsu_files_core_pinned", req["core_b64"], CORE_SHA)
        core.validate_plan(req["plan"])
        checksum = core.digest(core.canonical(req["plan"]))
        result["plan_sha256"] = checksum
        if not args.prepare:
            require(args.plan_sha256 is None, "PLAN_ARGUMENT")
            result.update(success=True, mode="plan", targets=core.TARGETS)
        else:
            require(args.plan_sha256 == checksum, "APPROVED_PLAN_SHA")
            layout = load_raw("kitsu_files_layout_pinned", req["layout_b64"], LAYOUT_SHA)
            files = core.decode_files(req["plan"], req["files_b64"])
            prepare(core, layout, req["layout_proof"], req["plan"], files, result)
        result["mode"] = "prepare" if args.prepare else "plan"
    except BaseException as error:
        result.update(success=False, error_type=type(error).__name__)
    finally:
        if args.prepare and hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return 0 if result["success"] else 1


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON_DUPLICATE")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
