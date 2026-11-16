"""Acciones R2 explícitas sobre recursos propios; no ejecuta comandos o SDK."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import signal
import socket
import stat
import sys
import time
import types
from contextlib import ExitStack, contextmanager
from pathlib import Path

import r2_service_contract as C

LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"


def identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


@contextmanager
def directory(path):
    """Retiene ancestros NoFollow y rechaza sustituciones de nombres al salir."""
    path = Path(path)
    C.require(path.is_absolute() and ".." not in path.parts)
    bindings = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        fd = os.open("/", flags)
        stack.callback(os.close, fd)
        for name in path.parts[1:]:
            parent = fd
            fd = os.open(name, flags, dir_fd=parent)
            stack.callback(os.close, fd)
            bindings.append((parent, name, fd))
        yield fd
        for parent, name, held in bindings:
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            C.require(stat.S_ISDIR(linked.st_mode) and identity(linked) == identity(os.fstat(held)))


def read_at(fd, name, limit, *, uid=None, mode=None):
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    try:
        before = os.fstat(opened)
        C.require(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= limit
        )
        C.require(uid is None or before.st_uid == uid)
        C.require(mode is None or stat.S_IMODE(before.st_mode) == mode)
        raw = os.read(opened, limit + 1)
        after = os.fstat(opened)
        linked = os.stat(name, dir_fd=fd, follow_symlinks=False)

        def snap(i):
            return (
                i.st_dev,
                i.st_ino,
                i.st_size,
                i.st_uid,
                i.st_gid,
                i.st_mode,
                i.st_nlink,
                i.st_mtime_ns,
                i.st_ctime_ns,
            )

        C.require(len(raw) == before.st_size and snap(before) == snap(after) == snap(linked))
        return raw, after
    finally:
        os.close(opened)


def put_at(fd, name, raw, *, uid=None, mode=0o600):
    opened = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=fd
    )
    try:
        if uid is not None:
            os.fchown(opened, uid, uid)
        os.fchmod(opened, mode)  # Archivo todavía vacío antes de recibir secretos.
        with os.fdopen(os.dup(opened), "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(opened)
    os.fsync(fd)


def absent(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def private_directory(fd, uid=65532):
    info = os.fstat(fd)
    C.require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == uid
        and info.st_gid == uid
        and stat.S_IMODE(info.st_mode) == 0o700
    )
    return identity(info)


def file_record(info):
    return {
        **identity(info),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def mount_kind(path):
    info = Path(path).stat()
    device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
    found = []
    raw = Path("/proc/self/mountinfo").read_bytes()
    C.require(len(raw) <= 2 * 1024**2)
    for line in raw.decode("utf-8", errors="strict").splitlines():
        before, _, after = line.partition(" - ")
        parts = before.split()
        tail = after.split()
        C.require(len(parts) >= 6 and len(tail) >= 3)
        point = parts[4]
        if parts[2] == device and (
            str(path) == point or str(path).startswith(point.rstrip("/") + "/")
        ):
            found.append((len(point), point, tail))
    C.require(bool(found))
    return max(found, key=lambda row: row[0])[1:]


class Filesystem:
    """Rutas inyectables sólo para fixtures; la CLI utiliza las constantes cerradas."""

    def __init__(self, local=None, media=None):
        self.local = Path(local or C.ROOT)
        self.media = Path(media or C.MEDIA)

    def owner(self, spec):
        return {
            "owner": C.OWNER,
            "deployment_id": spec["deployment_id"],
            "core_sha256": C.core_hash(spec),
        }

    def preflight_media(self, spec):
        point, tail = mount_kind(self.media)
        C.require(
            point == str(self.media)
            and tail[0] == "nfs4"
            and tail[1] == "192.168.0.8:/volume2/producciones"
            and {"rw", "vers=4.1"} <= set(tail[2].split(","))
        )
        with directory(self.media) as fd:
            C.require(identity(os.fstat(fd)) == spec["media_before"])
            raw, _info = read_at(fd, ".probe", 170)
            C.require(len(raw) == 170 and hashlib.sha256(raw).hexdigest() == C.PROBE_SHA)

    def preflight_prepare(self, spec):
        self.preflight_media(spec)
        C.require(not self.local.exists() and not self.local.is_symlink())
        with directory(self.local.parent) as fd:
            info = os.fstat(fd)
            C.require(
                info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == 0o700
                and absent(fd, self.local.name)
            )
        _point, tail = mount_kind(self.local.parent)
        C.require(tail[0] in {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs"})
        with directory(self.media) as fd:
            C.require(absent(fd, C.MEDIA_MARKER))
            if not absent(fd, "proyectos"):
                with directory(self.media / "proyectos") as projects:
                    C.require(absent(projects, C.PROJECT))

    def prepare(self, spec, secrets):
        if spec["prepared"] is not None:
            self.verify(spec)
            return spec["prepared"]
        C.require(type(secrets) is dict and set(secrets) == set(C.SECRET_NAMES))
        decoded = {name: base64.b64decode(value, validate=True) for name, value in secrets.items()}
        C.require(len(decoded["hmac.key"]) == 64 and decoded["hmac.key"].isascii())
        C.require(
            0 < len(decoded["r2-access.key"]) <= 128
            and all(chr(c).isalnum() and c < 128 for c in decoded["r2-access.key"])
        )
        C.require(
            0 < len(decoded["r2-secret.key"]) <= 256
            and all(33 <= c <= 126 for c in decoded["r2-secret.key"])
        )
        self.preflight_prepare(spec)  # Todos los destinos se comprueban antes de mutar.
        marker = C.canonical(self.owner(spec))
        with directory(self.media) as fd:
            put_at(fd, C.MEDIA_MARKER, marker)
            media_proof = {
                **identity(os.fstat(fd)),
                "marker_name": C.MEDIA_MARKER,
                "marker_sha256": hashlib.sha256(marker).hexdigest(),
            }
            if absent(fd, "proyectos"):
                os.mkdir("proyectos", 0o770, dir_fd=fd)
        with directory(self.media / "proyectos") as fd:
            os.mkdir(C.PROJECT, 0o770, dir_fd=fd)
        with directory(self.media / "proyectos" / C.PROJECT) as fd:
            project = identity(os.fstat(fd))
            put_at(fd, ".owner.json", marker)
            os.mkdir("01_raw", 0o770, dir_fd=fd)
        with directory(self.media / "proyectos" / C.PROJECT / "01_raw") as fd:
            raw = identity(os.fstat(fd))
        with directory(self.local.parent) as fd:
            os.mkdir(self.local.name, 0o700, dir_fd=fd)
        with directory(self.local) as fd:
            os.fchown(fd, 65532, 65532)
            os.fchmod(fd, 0o700)
            root = private_directory(fd)
            put_at(fd, ".owner.json", marker, uid=65532)
            os.mkdir("state", 0o700, dir_fd=fd)
        with directory(self.local / "state") as fd:
            os.fchown(fd, 65532, 65532)
            os.fchmod(fd, 0o700)
            state_marker = C.canonical({**self.owner(spec), "directory": private_directory(fd)})
            put_at(fd, ".owner.json", state_marker, uid=65532)
            state_proof = {
                **identity(os.fstat(fd)),
                "marker_name": ".owner.json",
                "marker_sha256": hashlib.sha256(state_marker).hexdigest(),
            }
        result = {
            "schema_version": 1,
            **self.owner(spec),
            "root": root,
            "state_proof": state_proof,
            "media_proof": media_proof,
            "project": project,
            "raw": raw,
            "files": {},
            "config_sha256": "",
        }
        config = C.canonical(C.runtime_config(spec, result))
        result["config_sha256"] = hashlib.sha256(config).hexdigest()
        with directory(self.local) as fd:
            for name, data in {**decoded, "runtime.json": config}.items():
                put_at(fd, name, data, uid=65532, mode=0o400)
                _raw, info = read_at(fd, name, 16384, uid=65532, mode=0o400)
                result["files"][name] = file_record(info)
            put_at(fd, "prepare-receipt.json", C.canonical(result), uid=65532)
        C.validate_prepared(result, spec)
        return result

    def verify_local_control(self, spec):
        C.validate_spec(spec)
        C.require(spec["prepared"] is not None)
        expected = spec["prepared"]
        with directory(self.local) as fd:
            C.require(private_directory(fd) == expected["root"])
            marker, _ = read_at(fd, ".owner.json", 65536, uid=65532, mode=0o600)
            C.require(json.loads(marker) == self.owner(spec))
            receipt, _ = read_at(fd, "prepare-receipt.json", 65536, uid=65532, mode=0o600)
            C.require(json.loads(receipt) == expected)

    def verify(self, spec):
        self.verify_local_control(spec)
        expected = spec["prepared"]
        self.preflight_media(spec)
        with directory(self.local) as fd:
            for name, wanted in expected["files"].items():
                raw, info = read_at(fd, name, 16384, uid=65532, mode=0o400)
                C.require(file_record(info) == wanted)
                if name == "runtime.json":
                    C.require(
                        hashlib.sha256(raw).hexdigest() == expected["config_sha256"]
                        and json.loads(raw) == C.runtime_config(spec, expected)
                    )
        for path, proof in (
            (self.local / "state", expected["state_proof"]),
            (self.media, expected["media_proof"]),
        ):
            with directory(path) as fd:
                C.require(identity(os.fstat(fd)) == {k: proof[k] for k in ("device", "inode")})
                if path == self.local / "state":
                    private_directory(fd)
                raw, _ = read_at(
                    fd,
                    proof["marker_name"],
                    65536,
                    uid=65532 if path == self.local / "state" else None,
                    mode=0o600 if path == self.local / "state" else None,
                )
                C.require(hashlib.sha256(raw).hexdigest() == proof["marker_sha256"])
        for path, key in (
            (self.media / "proyectos" / C.PROJECT, "project"),
            (self.media / "proyectos" / C.PROJECT / "01_raw", "raw"),
        ):
            with directory(path) as fd:
                C.require(identity(os.fstat(fd)) == expected[key])
                if key == "project":
                    raw, _ = read_at(fd, ".owner.json", 65536)
                    C.require(json.loads(raw) == self.owner(spec))

    def read_record(self, name):
        with directory(self.local) as fd:
            if absent(fd, name):
                return None
            raw, _ = read_at(fd, name, 65536, uid=65532, mode=0o600)
            return json.loads(raw)

    def record(self, name, value):
        with directory(self.local) as fd:
            put_at(fd, name, C.canonical(value), uid=65532)

    def database(self, *, exists):
        with directory(self.local / "state") as fd:
            if not exists:
                C.require(absent(fd, "sync-ledger.sqlite3"))
                return
            opened = os.open(
                "sync-ledger.sqlite3", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            try:
                info = os.fstat(opened)
                C.require(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == 65532
                    and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_nlink == 1
                    and os.read(opened, 16) == b"SQLite format 3\x00"
                )
            finally:
                os.close(opened)


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/run/docker.sock")


class Engine:
    def __init__(self):
        info = Path("/run/docker.sock").lstat()
        C.require(
            stat.S_ISSOCK(info.st_mode)
            and info.st_uid == 0
            and not stat.S_IMODE(info.st_mode) & 0o002
        )
        self.deadline = time.monotonic() + 100

    def request(self, method, path, body=None, *, allow_missing=False, binary=False):
        C.require(method in ("GET", "POST") and path.startswith("/"))
        left = self.deadline - time.monotonic()
        C.require(left > 0)
        connection = UnixConnection("localhost", timeout=min(20, left))
        try:
            raw = None if body is None else C.canonical(body)
            connection.request(
                method,
                path,
                body=raw,
                headers={} if raw is None else {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            data = response.read(1024 * 1024 + 1)
            C.require(len(data) <= 1024 * 1024 and time.monotonic() < self.deadline)
            if allow_missing and response.status == 404:
                return None
            C.require(response.status in (200, 201, 204, 304))
            return data if binary else json.loads(data) if data else None
        finally:
            connection.close()

    def version(self):
        value = self.request("GET", "/version")
        C.require(
            value["Version"] == "29.9.3"
            and tuple(map(int, value["MinAPIVersion"].split(".")))
            <= (1, 54)
            <= tuple(map(int, value["ApiVersion"].split(".")))
        )

    def inspect(self, cid):
        C.require(C.SHA.fullmatch(cid) is not None)
        return self.request("GET", "/v1.54/containers/" + cid + "/json")

    def named(self, name):
        return self.request("GET", "/v1.54/containers/" + name + "/json", allow_missing=True)

    def create(self, spec, role):
        return self.request(
            "POST",
            "/v1.54/containers/create?platform=linux%2Famd64&name=" + C.container_name(spec, role),
            C.container_config(spec, role),
        )

    def start(self, cid):
        self.request("POST", "/v1.54/containers/" + cid + "/start")

    def port_free(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("192.168.0.85", 3010))
        finally:
            probe.close()

    def stop(self, cid):
        self.request("POST", "/v1.54/containers/" + cid + "/stop?t=10")

    def initialized(self, cid):
        until = time.monotonic() + 30
        while time.monotonic() < until:
            value = self.inspect(cid)
            if not value["State"]["Running"]:
                C.require(
                    value["State"]["Status"] == "exited"
                    and value["State"]["ExitCode"] == 0
                    and not value["State"].get("OOMKilled")
                    and not value["State"].get("Error")
                )
                raw = self.request(
                    "GET",
                    "/v1.54/containers/" + cid + "/logs?stdout=1&stderr=1&tail=10",
                    binary=True,
                )
                C.require(len(raw) <= 8192)
                stdout = bytearray()
                while raw:
                    C.require(len(raw) >= 8 and raw[0] == 1 and raw[1:4] == b"\x00" * 3)
                    size = int.from_bytes(raw[4:8], "big")
                    C.require(size <= len(raw) - 8)
                    stdout.extend(raw[8 : 8 + size])
                    raw = raw[8 + size :]
                C.require(json.loads(stdout) == {"status": "initialized"})
                return
            time.sleep(0.1)
        self.stop(cid)
        raise ValueError("R2_INIT_TIMEOUT")

    def health(self):
        until = time.monotonic() + 30
        while time.monotonic() < until:
            connection = http.client.HTTPConnection("192.168.0.85", 3010, timeout=2)
            try:
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                data = response.read(257)
                if response.status == 200 and len(data) <= 256:
                    value = json.loads(data)
                    if (
                        type(value) is dict
                        and set(value) == {"ready", "busy"}
                        and all(type(v) is bool for v in value.values())
                        and value["ready"] != value["busy"]
                    ):
                        return value
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.2)
        raise ValueError("R2_SERVICE_HEALTH_TIMEOUT")


class CreatedUnverified(ValueError):
    def __init__(self, action, role, cid, spec):
        super().__init__("R2_CREATED_CID_REQUIRES_REVIEW")
        self.result = {
            "success": False,
            "action": action,
            "created_cid": cid,
            "verified": False,
            "role": role,
            "core_sha256": C.core_hash(spec),
            "intent_saved": True,
            "requires_cid_backup_before_any_action": True,
            "error": "R2_CREATED_CID_REQUIRES_REVIEW",
        }


def execute(plan, approved, store, engine=None, *, secrets=None):
    C.verify_plan(plan, approved)
    spec = plan["spec"]
    action = plan["action"]
    if action == "prepare":
        return {
            "success": True,
            "action": action,
            "prepared": store.prepare(spec, secrets),
            "containers_started": False,
        }
    if action == "stop":
        store.verify_local_control(spec)
    else:
        store.verify(spec)
    engine.version()
    role = "init" if action.endswith("init") else "serve"
    cid = spec["init_container_id" if role == "init" else "service_container_id"]
    record_name = role + "-created.json"
    intent_name = role + "-intent.json"
    intent = {
        "name": C.container_name(spec, role),
        "role": role,
        "core_sha256": C.core_hash(spec),
        "container_config_sha256": C.digest(C.container_config(spec, role)),
    }
    if action.startswith("create-"):
        previous = store.read_record(record_name)
        if previous is not None:
            C.require(store.read_record(intent_name) == intent)
            C.require(previous["core_sha256"] == C.core_hash(spec) and previous["role"] == role)
            if cid is None:
                raise CreatedUnverified(action, role, previous["container_id"], spec)
            candidate = {
                **spec,
                ("init_container_id" if role == "init" else "service_container_id"): previous[
                    "container_id"
                ],
            }
            try:
                C.verify_container(candidate, role, engine.inspect(previous["container_id"]))
            except Exception:
                raise CreatedUnverified(action, role, previous["container_id"], spec) from None
            C.require(cid is None or cid == previous["container_id"])
            C.require(not previous["warnings_present"])
            verified = store.read_record(role + "-verified.json")
            if verified is None:
                store.record(role + "-verified.json", previous)
            else:
                C.require(verified == previous)
            return {
                "success": True,
                "action": action,
                **previous,
                "changed": False,
                "requires_cid_backup_before_start": True,
            }
        C.require(
            cid is None
            and store.read_record(intent_name) is None
            and engine.named(C.container_name(spec, role)) is None
        )
        if role == "init":
            store.database(exists=False)
        else:
            C.require(store.read_record("init-completed.json") == spec["init_result"])
            store.database(exists=True)
            engine.port_free()
        store.record(intent_name, intent)
        created = engine.create(spec, role)
        C.require(type(created["Id"]) is str and C.SHA.fullmatch(created["Id"]) is not None)
        record = {
            "container_id": created["Id"],
            "role": role,
            "core_sha256": C.core_hash(spec),
            "warnings_present": bool(created.get("Warnings")),
        }
        try:
            store.record(record_name, record)
            C.require(not record["warnings_present"])
            candidate = {
                **spec,
                ("init_container_id" if role == "init" else "service_container_id"): created["Id"],
            }
            C.verify_container(candidate, role, engine.inspect(created["Id"]))
            store.record(role + "-verified.json", record)
        except Exception:
            raise CreatedUnverified(action, role, created["Id"], spec) from None
        return {
            "success": True,
            "action": action,
            **record,
            "changed": True,
            "requires_cid_backup_before_start": True,
        }
    info = engine.inspect(cid)
    C.verify_container(spec, role, info)
    created = store.read_record(record_name)
    C.require(
        type(created) is dict
        and set(created) == {"container_id", "role", "core_sha256", "warnings_present"}
        and created["container_id"] == cid
        and created["role"] == role
        and created["core_sha256"] == C.core_hash(spec)
        and type(created["warnings_present"]) is bool
    )
    C.require(store.read_record(intent_name) == intent)
    if action == "stop":
        changed = bool(info["State"]["Running"])
        if changed:
            engine.stop(cid)
        after = engine.inspect(cid)
        C.verify_container(spec, role, after)
        C.require(not after["State"]["Running"])
        return {
            "success": True,
            "action": action,
            "container_id": cid,
            "changed": changed,
            "removed": False,
        }
    C.require(
        not created["warnings_present"] and store.read_record(role + "-verified.json") == created
    )
    if role == "init":
        previous = store.read_record("init-completed.json")
        if previous is not None:
            C.require(previous == spec["init_result"])
            store.database(exists=True)
            return {"success": True, "action": action, "init_result": previous, "changed": False}
        store.database(exists=False)
        C.require(info["State"]["Status"] == "created" and not info["State"]["Running"])
        engine.start(cid)
        engine.initialized(cid)
        C.verify_container(spec, role, engine.inspect(cid))
        store.database(exists=True)
        result = {"container_id": cid, "exit_code": 0, "core_sha256": C.core_hash(spec)}
        store.record("init-completed.json", result)
        return {
            "success": True,
            "action": action,
            "init_result": result,
            "changed": True,
            "removed": False,
        }
    C.require(store.read_record("init-completed.json") == spec["init_result"])
    store.database(exists=True)
    changed = not info["State"]["Running"]
    if changed:
        engine.port_free()
        engine.start(cid)
    C.verify_container(spec, role, engine.inspect(cid))
    return {
        "success": True,
        "action": action,
        "container_id": cid,
        "changed": changed,
        "health": engine.health(),
    }


def main():
    def expired(_signal, _frame):
        raise TimeoutError("R2_SERVICE_DEADLINE")

    if sys.platform.startswith("linux"):
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 110)
    try:
        C.require(
            sys.platform.startswith("linux")
            and os.geteuid() == 0
            and socket.gethostname() == "vm-media-01"
        )
        raw = sys.stdin.buffer.read(256 * 1024 + 1)
        C.require(len(raw) <= 256 * 1024)
        payload = json.loads(raw)
        C.require(set(payload) == {"plan", "approved", "layout_source_b64", "secrets"})
        C.verify_plan(payload["plan"], payload["approved"])
        observation = payload["plan"]["observation"]
        source = base64.b64decode(payload["layout_source_b64"], validate=True)
        C.require(hashlib.sha256(source).hexdigest() == LAYOUT_SHA == observation["layout_sha256"])
        layout = types.ModuleType("r2_service_layout_pinned")
        exec(compile(source, "<layout-fijado>", "exec"), layout.__dict__)
        host = layout.inspect_host()
        layout.host_contract(host, apply=True)
        with layout.LocalFilesystem() as fs:
            actual = layout.make_plan(fs.inspect(), host)["inventory"]["targets"]
        for expected in observation["layout"]["targets"]:
            current = actual[expected["path"]]
            C.require(
                layout.directory_identity(current["node"]) == expected["directory"]
                and layout.digest(current["marker"]) == expected["marker_sha256"]
                and layout.digest(current["receipt"]) == expected["receipt_sha256"]
            )
        os.umask(0o077)
        result = execute(
            payload["plan"],
            payload["approved"],
            Filesystem(),
            None if payload["plan"]["action"] == "prepare" else Engine(),
            secrets=payload["secrets"],
        )
        print(json.dumps(result))
        return 0
    except CreatedUnverified as error:
        print(json.dumps(error.result))
        return 2
    except Exception:
        print('{"success":false,"error":"R2_SERVICE_ACTION_REJECTED"}')
        return 1
    finally:
        if sys.platform.startswith("linux"):
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    raise SystemExit(main())
