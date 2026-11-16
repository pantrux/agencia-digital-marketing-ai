"""Consumidor Linux: configuración privada, worker fijo y supervisión por PIDFD."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import stat
import sys
import time
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

CODE_DIR = Path(__file__).absolute().parent
HELPER_PINS = {
    "sync_storage.py": "b28b137b38039309bc86f3b4e4b7afd74d1f8515cc0b8241a15d9de0a7c45023",
    "sync_ledger.py": "5759d30fe8ff94f64f3e030ddbddfc7c419859806c50f3045991bf31a181094e",
    "sync_protocol.py": "4839968c238d50bb84fb7e71dd0693399cb8af9b050587bec349aa5ea02209a8",
    "r2_client.py": "aa1f2da7b959e72941a94dade981bad6d5f567449b62dfff29175003b3ea5377",
}
CODE_FILES = frozenset((*HELPER_PINS, "sync_runtime.py", "sync_http.py"))
MAX_ENVELOPE = 8192
MAX_OUTPUT = 8192
SHA = re.compile(r"[0-9a-f]{64}")


class RuntimeErrorCode(ValueError):
    """Sólo códigos fijos; no conserva excepciones, cabeceras o secretos."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class WorkerCancelled(BaseException):
    pass


def guard_code(directory=CODE_DIR):
    if not sys.platform.startswith("linux"):
        raise RuntimeErrorCode("UNSUPPORTED")
    directory = Path(directory)
    for parent in (*reversed(directory.parents), directory):
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid not in (0, os.geteuid())
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise RuntimeErrorCode("CONFIG_INVALID")
    info = directory.lstat()
    if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o222:
        raise RuntimeErrorCode("CONFIG_INVALID")
    if {path.name for path in directory.iterdir()} != CODE_FILES:
        raise RuntimeErrorCode("CONFIG_INVALID")
    for name in CODE_FILES:
        path = directory / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in (0, os.geteuid())
            or stat.S_IMODE(info.st_mode) & 0o222
            or not 0 < info.st_size <= 1024 * 1024
        ):
            raise RuntimeErrorCode("CONFIG_INVALID")
        if (
            name in HELPER_PINS
            and hashlib.sha256(path.read_bytes()).hexdigest() != HELPER_PINS[name]
        ):
            raise RuntimeErrorCode("CONFIG_INVALID")


# Con -I el directorio del script no se importa implícitamente. Sólo se añade
# nuestra carpeta fija después de acreditar sus seis archivos en la entrada CLI.
if __name__ == "__main__" or (sys.flags.isolated and sys.flags.no_site):
    try:
        if not sys.flags.isolated or not sys.flags.no_site:
            raise RuntimeErrorCode("CONFIG_INVALID")
        guard_code()
    except (OSError, RuntimeErrorCode):
        sys.stderr.write("CONFIG_INVALID\n")
        raise SystemExit(2) from None
sys.path.insert(0, str(CODE_DIR))
from r2_client import R2Credentials, R2Error, download  # noqa: E402
from sync_ledger import Ledger, LedgerError, validate_publication  # noqa: E402
from sync_ledger import Reservation as Reservation  # noqa: E402
from sync_protocol import ProtocolError, verify_request  # noqa: E402
from sync_storage import Publication as Publication  # noqa: E402
from sync_storage import RootProof, StorageError, SyncStorage  # noqa: E402


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeErrorCode("CONFIG_INVALID")
        result[key] = value
    return result


def _json(data):
    def invalid(_value):
        raise RuntimeErrorCode("CONFIG_INVALID")

    try:
        return json.loads(
            data.decode("utf-8", errors="strict"), object_pairs_hook=_unique, parse_constant=invalid
        )
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeErrorCode("CONFIG_INVALID") from None


def _path(value):
    if (
        type(value) is not str
        or not value.startswith("/")
        or value.startswith("//")
        or "\x00" in value
        or any(p in ("", ".", "..") for p in value.split("/")[1:])
    ):
        raise RuntimeErrorCode("CONFIG_INVALID")
    return value


def private_file(path, maximum):
    """Lee un archivo regular privado manteniendo anclas NoFollow de sus padres."""
    _path(os.fspath(path))
    if not sys.platform.startswith("linux"):
        raise RuntimeErrorCode("UNSUPPORTED")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    bindings = []
    with ExitStack() as stack:
        parent = os.open("/", flags)
        stack.callback(os.close, parent)
        parts = os.fspath(path).split("/")[1:]
        for name in parts[:-1]:
            child = os.open(name, flags, dir_fd=parent)
            stack.callback(os.close, child)
            bindings.append((parent, name, child))
            parent = child
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
        )
        stack.callback(os.close, fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) not in (0o400, 0o600)
            or not 0 < before.st_size <= maximum
        ):
            raise RuntimeErrorCode("CONFIG_INVALID")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(fd, min(16384, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)

        def snap(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_nlink,
                info.st_uid,
                info.st_mode,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        linked = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (
            len(data) != before.st_size
            or len(data) > maximum
            or snap(before) != snap(after)
            or snap(linked) != snap(after)
        ):
            raise RuntimeErrorCode("CONFIG_INVALID")
        for parent_fd, name, held in bindings:
            linked, actual = os.stat(name, dir_fd=parent_fd, follow_symlinks=False), os.fstat(held)
            if not stat.S_ISDIR(linked.st_mode) or (linked.st_dev, linked.st_ino) != (
                actual.st_dev,
                actual.st_ino,
            ):
                raise RuntimeErrorCode("CONFIG_INVALID")
        return bytes(data)


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeConfig:
    media_root: str
    media_proof: RootProof
    state_root: str
    state_proof: RootProof
    account_id: str
    max_object_bytes: int
    hmac_secret_file: str
    r2_access_key_file: str
    r2_secret_key_file: str
    listen_host: str
    listen_port: int
    provision_on_nas: bool = False

    def __repr__(self):
        return "RuntimeConfig(<redactado>)"


def parse_config(raw):
    import ipaddress

    value = _json(raw)
    fields = {"schema_version", *RuntimeConfig.__dataclass_fields__}
    if (
        type(value) is not dict
        or set(value) not in (fields, fields - {"provision_on_nas"})
        or type(value["schema_version"]) is not int
        or value["schema_version"] not in (1, 2)
        or (value["schema_version"] == 1 and "provision_on_nas" in value)
        or (value["schema_version"] == 2 and value.get("provision_on_nas") is not True)
    ):
        raise RuntimeErrorCode("CONFIG_INVALID")
    data = dict(value)
    data.pop("schema_version")
    for name in (
        "media_root",
        "state_root",
        "hmac_secret_file",
        "r2_access_key_file",
        "r2_secret_key_file",
    ):
        _path(data[name])
    for name in ("media_proof", "state_proof"):
        if type(data[name]) is not dict or set(data[name]) != {
            "device",
            "inode",
            "marker_name",
            "marker_sha256",
        }:
            raise RuntimeErrorCode("CONFIG_INVALID")
        try:
            data[name] = RootProof(**data[name])
        except (StorageError, TypeError):
            raise RuntimeErrorCode("CONFIG_INVALID") from None
    try:
        address = ipaddress.IPv4Address(data["listen_host"])
        if str(address) != data["listen_host"]:
            raise ValueError
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise RuntimeErrorCode("CONFIG_INVALID") from None
    if (
        type(data["listen_port"]) is not int
        or not 1024 <= data["listen_port"] <= 65535
        or type(data["max_object_bytes"]) is not int
        or not 0 < data["max_object_bytes"] <= 2**53 - 1
        or type(data["account_id"]) is not str
        or re.fullmatch(r"[0-9a-f]{32}", data["account_id"]) is None
    ):
        raise RuntimeErrorCode("CONFIG_INVALID")
    return RuntimeConfig(**data)


@dataclass(frozen=True, slots=True)
class Runtime:
    config: RuntimeConfig = field(repr=False)
    config_path: str = field(repr=False)
    hmac_secret: bytes = field(repr=False)
    credentials: R2Credentials = field(repr=False)
    snapshot: str = field(repr=False)

    @property
    def max_object_bytes(self):
        return self.config.max_object_bytes

    def ledger(self):
        return Ledger(self.config.state_root, self.config.state_proof)

    def storage(self):
        return SyncStorage(
            self.config.media_root, self.config.media_proof, max_object_bytes=self.max_object_bytes
        )


def load_runtime(config_path):
    try:
        raw = private_file(config_path, 16384)
        config = parse_config(raw)
        secret = private_file(config.hmac_secret_file, 128)
        access = private_file(config.r2_access_key_file, 128)
        r2_secret = private_file(config.r2_secret_key_file, 256)
        if not 32 <= len(secret) <= 128:
            raise RuntimeErrorCode("CONFIG_INVALID")
        credentials = R2Credentials(
            config.account_id, access.decode("ascii"), r2_secret.decode("ascii")
        )
        snapshot = hashlib.sha256(
            b"".join(hashlib.sha256(v).digest() for v in (raw, secret, access, r2_secret))
        ).hexdigest()
        return Runtime(config, os.fspath(config_path), secret, credentials, snapshot)
    except (OSError, ValueError, UnicodeError, TypeError):
        raise RuntimeErrorCode("CONFIG_INVALID") from None


def encode_envelope(body, timestamp, signature, snapshot):
    value = {
        "body": base64.b64encode(body).decode("ascii"),
        "timestamp": timestamp,
        "signature": signature,
        "snapshot": snapshot,
    }
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(raw) > MAX_ENVELOPE:
        raise RuntimeErrorCode("UNAUTHORIZED")
    return raw


def decode_envelope(raw, runtime, now_seconds):
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_ENVELOPE:
            raise ValueError
        value = _json(raw)
        if (
            type(value) is not dict
            or set(value) != {"body", "timestamp", "signature", "snapshot"}
            or type(value["body"]) is not str
            or type(value["snapshot"]) is not str
            or SHA.fullmatch(value["snapshot"]) is None
            or value["snapshot"] != runtime.snapshot
        ):
            raise ValueError
        body = base64.b64decode(value["body"], validate=True)
        if base64.b64encode(body).decode("ascii") != value["body"]:
            raise ValueError
        return verify_request(
            body,
            value["timestamp"],
            value["signature"],
            runtime.hmac_secret,
            runtime.max_object_bytes,
            now_seconds,
        )
    except (ValueError, TypeError, ProtocolError, KeyError):
        raise RuntimeErrorCode("UNAUTHORIZED") from None


def public_receipt(publication, job):
    receipt = validate_publication(publication, job)
    return {
        "status": "completed",
        "job_id": receipt.job_id,
        "asset_id": receipt.asset_id,
        "relative_path": receipt.relative_path,
        "bytes": receipt.bytes,
        "sha256": receipt.sha256,
    }


def prepare_project(runtime, raw, verified, storage):
    # La orden que llegó al worker sigue firmada por Edge y se vuelve a validar en DSM.
    envelope = _json(raw)
    wire = json.dumps(
        {key: envelope[key] for key in ("body", "timestamp", "signature")}, separators=(",", ":")
    ).encode("ascii")
    conn = http.client.HTTPConnection("192.168.0.8", 3148, timeout=8)
    try:
        conn.request(
            "POST", "/v1/provision", body=wire, headers={"Content-Type": "application/json"}
        )
        with conn.getresponse() as response:
            body = response.read(4097)
            if (
                response.status != 200
                or len(body) > 4096
                or response.getheader("Content-Type") != "application/json"
            ):
                raise RuntimeErrorCode("FAILED")
            message = (
                b"studio-nas-provision-v1\n" + envelope["timestamp"].encode("ascii") + b"\n" + body
            )
            expected = "v1=" + hmac.digest(runtime.hmac_secret, message, "sha256").hex()
            if not hmac.compare_digest(expected, response.getheader("Signature", "")):
                raise RuntimeErrorCode("FAILED")
        result = _json(body)
        if (
            set(result) != {"fingerprint", "inode", "created"}
            or result["fingerprint"] != verified.fingerprint
            or type(result["inode"]) is not int
            or result["inode"] <= 0
            or type(result["created"]) is not bool
        ):
            raise RuntimeErrorCode("FAILED")
        storage.require_project_inode(verified.job, result["inode"])
    except (OSError, ValueError, http.client.HTTPException):
        raise RuntimeErrorCode("FAILED") from None
    finally:
        conn.close()


def process_envelope(
    runtime,
    raw,
    *,
    ledger=None,
    storage_factory=None,
    downloader=download,
    now_seconds=lambda: int(time.time()),
):
    verified = decode_envelope(raw, runtime, now_seconds)
    ledger = runtime.ledger() if ledger is None else ledger
    storage_factory = runtime.storage if storage_factory is None else storage_factory
    value = asdict(verified.job)
    try:
        with ledger.operation(
            value, verified.fingerprint, max_object_bytes=runtime.max_object_bytes, wait_seconds=0
        ) as operation:
            reservation = operation.reserve()
            if reservation.state == "completed":
                return public_receipt(reservation.receipt, verified.job)
            storage = None
            if getattr(getattr(runtime, "config", None), "provision_on_nas", False):
                storage = storage_factory()
                prepare_project(runtime, raw, verified, storage)
            with closing(
                downloader(
                    value,
                    runtime.max_object_bytes,
                    runtime.credentials,
                    total_seconds=280,
                    idle_seconds=15,
                )
            ) as chunks:
                publication = (storage if storage is not None else storage_factory()).publish(
                    value, chunks
                )
            if not reservation.is_new:
                operation.reconcile(publication)
            return public_receipt(operation.complete(publication), verified.job)
    except LedgerError as error:
        raise RuntimeErrorCode(
            "BUSY"
            if error.code == "LOCK_TIMEOUT"
            else "CONFLICT"
            if error.code == "JOB_FINGERPRINT_CONFLICTO"
            else "FAILED"
        ) from None
    except (StorageError, R2Error, OSError, ValueError):
        raise RuntimeErrorCode("FAILED") from None


PYTHON = Path(sys.executable).resolve()


def worker_command(config_path):
    _path(config_path)
    return [
        str(PYTHON),
        "-I",
        "-B",
        "-S",
        str(CODE_DIR / "sync_runtime.py"),
        "worker",
        "--config",
        config_path,
    ]


def worker_environment():
    return {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def _pidfd_ready(pidfd, timeout=0):
    import select

    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(max(0, int(timeout * 1000))))


def _signal_owned(pidfd, value):
    import signal

    try:
        signal.pidfd_send_signal(pidfd, value, None, 0)
    except ProcessLookupError:
        pass


def _stop_owned(pidfd):
    import signal

    if _pidfd_ready(pidfd):
        return True
    _signal_owned(pidfd, signal.SIGTERM)
    if _pidfd_ready(pidfd, 2):
        return True
    _signal_owned(pidfd, signal.SIGKILL)
    return _pidfd_ready(pidfd, 2)


def _watch_owned(process, pidfd, raw, cancel, *, client=None, timeout=300):
    """Sólo vigila el proceso ya creado por el supervisor, nunca un PID entrante."""
    import selectors
    import socket

    started = time.monotonic()
    deadline = started + timeout
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    pending = memoryview(raw)
    eof = set()
    reason = None
    with selectors.DefaultSelector() as selector:
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(
                stream, selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ, name
            )
        selector.register(pidfd, selectors.EVENT_READ, "process")
        if client is not None:
            selector.register(client, selectors.EVENT_READ, "client")
        while True:
            if cancel.is_set():
                reason = "CANCELLED"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "TIMEOUT"
                break
            if _pidfd_ready(pidfd) and eof == {"stdout", "stderr"}:
                break
            for key, _mask in selector.select(min(0.05, remaining)):
                name = key.data
                if name == "process":
                    selector.unregister(pidfd)
                    continue
                if name == "client":
                    try:
                        data = client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                    except BlockingIOError:
                        continue
                    except OSError:
                        data = b""
                    reason = "CANCELLED" if not data else "BAD_REQUEST"
                    break
                stream = key.fileobj
                try:
                    if name == "stdin":
                        written = os.write(stream.fileno(), pending)
                        pending = pending[written:]
                        if not pending:
                            selector.unregister(stream)
                            stream.close()
                    else:
                        capacity = MAX_OUTPUT - sum(len(v) for v in buffers.values())
                        chunk = os.read(stream.fileno(), min(4096, capacity + 1))
                        if not chunk:
                            selector.unregister(stream)
                            eof.add(name)
                        elif len(chunk) > capacity:
                            reason = "WORKER_FAILED"
                            break
                        else:
                            buffers[name].extend(chunk)
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError):
                    reason = "WORKER_FAILED"
                    break
            if reason:
                break
    dead = _stop_owned(pidfd) if reason else _pidfd_ready(pidfd)
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), reason, dead


@dataclass(slots=True)
class _Active:
    process: object
    pidfd: int | None
    watching: bool = True


class WorkerSupervisor:
    """Un solo worker fijo; no proporciona ejecución genérica ni mata por PID."""

    def __init__(self, runtime):
        import signal
        import threading

        if (
            not sys.platform.startswith("linux")
            or not hasattr(os, "pidfd_open")
            or not hasattr(signal, "pidfd_send_signal")
        ):
            raise RuntimeErrorCode("UNSUPPORTED")
        guard_code(CODE_DIR)
        info = PYTHON.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (0, os.geteuid())
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise RuntimeErrorCode("CONFIG_INVALID")
        probe = os.pidfd_open(os.getpid(), 0)
        os.close(probe)
        self.runtime = runtime
        self._gate = threading.Lock()
        self._state = threading.Lock()
        self._active = None
        self.cancel = threading.Event()

    def _release_if_dead(self):
        with self._state:
            active = self._active
            if active is None or active.watching:
                return
            dead = (
                _pidfd_ready(active.pidfd)
                if active.pidfd is not None
                else active.process.poll() is not None
            )
            if not dead:
                return
            active.process.wait(timeout=0.1)
            for name in ("stdin", "stdout", "stderr"):
                stream = getattr(active.process, name)
                if stream is not None and not stream.closed:
                    stream.close()
            if active.pidfd is not None:
                os.close(active.pidfd)
            self._active = None
            self._gate.release()

    @property
    def busy(self):
        self._release_if_dead()
        return self._gate.locked()

    def run(self, raw, verified, *, client=None):
        import subprocess

        if len(raw) > MAX_ENVELOPE:
            raise RuntimeErrorCode("UNAUTHORIZED")
        self._release_if_dead()
        if self.cancel.is_set() or not self._gate.acquire(blocking=False):
            raise RuntimeErrorCode("BUSY")
        active = None
        try:
            process = subprocess.Popen(
                worker_command(self.runtime.config_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                close_fds=True,
                shell=False,
                cwd=str(CODE_DIR),
                env=worker_environment(),
            )
            active = _Active(process, None)
            with self._state:
                self._active = active
            try:
                active.pidfd = os.pidfd_open(process.pid, 0)
            except OSError:
                # Sin PIDFD no se envía entrada ni señales por PID. El worker fijo
                # rechaza EOF; si no termina se retienen proceso y slot para revisión.
                process.stdin.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                raise RuntimeErrorCode("WORKER_FAILED") from None
            stdout, stderr, reason, dead = _watch_owned(
                process, active.pidfd, raw, self.cancel, client=client
            )
            if reason or not dead:
                raise RuntimeErrorCode(reason or "WORKER_FAILED")
            process.wait(timeout=0.1)
            if stderr:
                raise RuntimeErrorCode("WORKER_FAILED")
            value = _json(stdout)
            if (
                type(value) is dict
                and set(value) == {"status", "error"}
                and value["status"] == "error"
                and value["error"] in {"UNAUTHORIZED", "BUSY", "CONFLICT", "FAILED", "CANCELLED"}
            ):
                raise RuntimeErrorCode(value["error"])
            expected = {
                "status": "completed",
                "job_id": verified.job.job_id,
                "asset_id": verified.job.asset_id,
                "relative_path": verified.job.relative_path,
                "bytes": verified.job.bytes,
                "sha256": verified.job.sha256,
            }
            if (
                process.returncode != 0
                or value != expected
                or any(type(value[k]) is not type(v) for k, v in expected.items())
            ):
                raise RuntimeErrorCode("WORKER_FAILED")
            return expected
        except RuntimeErrorCode:
            raise
        except Exception:
            if active is not None and active.pidfd is not None:
                _stop_owned(active.pidfd)
            raise RuntimeErrorCode("WORKER_FAILED") from None
        finally:
            if active is None:
                self._gate.release()
            else:
                active.watching = False
                self._release_if_dead()


def worker_limits(max_object_bytes):
    import resource

    desired = {
        resource.RLIMIT_AS: 512 * 1024**2,
        resource.RLIMIT_NOFILE: 64,
        resource.RLIMIT_CPU: 240,
        resource.RLIMIT_CORE: 0,
        resource.RLIMIT_FSIZE: max(max_object_bytes, 128 * 1024**2),
        resource.RLIMIT_NPROC: 0,
    }
    for kind, limit in desired.items():
        soft, hard = resource.getrlimit(kind)
        new_hard = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        new_soft = new_hard if soft == resource.RLIM_INFINITY else min(soft, new_hard)
        resource.setrlimit(kind, (new_soft, new_hard))


def _cancel_worker(_signal, _frame):
    raise WorkerCancelled()


def main(argv=None):
    import argparse
    import signal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("worker", "init-db"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        guard_code(CODE_DIR)
        runtime = load_runtime(args.config)
        if args.mode == "init-db":
            runtime.ledger().initialize()
            sys.stdout.write('{"status":"initialized"}\n')
            return 0
        signal.signal(signal.SIGTERM, _cancel_worker)
        signal.signal(signal.SIGINT, _cancel_worker)
        worker_limits(runtime.max_object_bytes)
        raw = sys.stdin.buffer.read(MAX_ENVELOPE + 1)
        result = process_envelope(runtime, raw)
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        return 0
    except WorkerCancelled:
        sys.stdout.write('{"status":"error","error":"CANCELLED"}\n')
        return 75
    except BaseException as error:
        code = (
            error.code
            if isinstance(error, RuntimeErrorCode)
            and error.code in {"UNAUTHORIZED", "BUSY", "CONFLICT"}
            else "FAILED"
        )
        sys.stdout.write(
            json.dumps({"status": "error", "error": code}, separators=(",", ":")) + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
