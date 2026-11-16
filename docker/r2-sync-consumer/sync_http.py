"""HTTP/1.1 cerrado del consumidor; no registra cabeceras, cuerpos o credenciales."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import sys
import threading
import time
from pathlib import Path

CODE_DIR = Path(__file__).absolute().parent
if __name__ == "__main__":
    try:
        if not sys.flags.isolated or not sys.flags.no_site or not sys.platform.startswith("linux"):
            raise ValueError
        # Antes de importar código vecino se acredita su ubicación y permisos.
        for parent in (*reversed(CODE_DIR.parents), CODE_DIR):
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise ValueError
        for path in (CODE_DIR, CODE_DIR / "sync_runtime.py"):
            info = path.lstat()
            if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o222:
                raise ValueError
        info = (CODE_DIR / "sync_runtime.py").lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError
    except (OSError, ValueError):
        sys.stderr.write("CONFIG_INVALID\n")
        raise SystemExit(2) from None
sys.path.insert(0, str(CODE_DIR))
import sync_runtime as R  # noqa: E402

MAX_HEADER_BYTES = 8192
MAX_HEADER_LINES = 64
HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
STATUS = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Content Too Large",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


class RequestError(ValueError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


class ReadDeadline:
    """El temporizador sólo posee este socket y se retira antes del trabajo."""

    def __init__(self, connection, seconds):
        if type(seconds) not in (int, float) or not 0 < seconds <= 10:
            raise ValueError("Presupuesto inválido.")
        self.connection = connection
        self.expired = False
        self._lock = threading.Lock()
        self._active = True
        self.timer = threading.Timer(seconds, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self):
        with self._lock:
            if not self._active:
                return
            self.expired = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

    def cancel(self):
        with self._lock:
            self._active = False
        self.timer.cancel()
        self.timer.join()


def read_request(connection):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) >= MAX_HEADER_BYTES:
            raise RequestError(400, "BAD_REQUEST")
        chunk = connection.recv(min(4096, MAX_HEADER_BYTES - len(data)))
        if not chunk:
            raise RequestError(400, "BAD_REQUEST")
        data.extend(chunk)
    head, buffered = bytes(data).split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    if len(lines) > MAX_HEADER_LINES or len(lines[0]) > 1024:
        raise RequestError(400, "BAD_REQUEST")
    try:
        method, path, version = lines[0].decode("ascii").split(" ")
    except (UnicodeError, ValueError):
        raise RequestError(400, "BAD_REQUEST") from None
    if version != "HTTP/1.1" or not method or not path:
        raise RequestError(400, "BAD_REQUEST")
    headers = {}
    for line in lines[1:]:
        if b":" not in line:
            raise RequestError(400, "BAD_REQUEST")
        name, value = line.split(b":", 1)
        if not HEADER_NAME.fullmatch(name) or any(
            c < 32 and c != 9 or c == 127 or c > 126 for c in value
        ):
            raise RequestError(400, "BAD_REQUEST")
        name = name.decode("ascii").lower()
        headers.setdefault(name, []).append(value.decode("ascii").strip(" \t"))
    if (
        any(name in headers for name in ("transfer-encoding", "expect"))
        or len(headers.get("host", [])) != 1
        or not headers["host"][0]
    ):
        raise RequestError(400, "BAD_REQUEST")
    if any(len(headers.get(name, [])) > 1 for name in ("content-length", "timestamp", "signature")):
        raise RequestError(400, "BAD_REQUEST")
    if path not in ("/v1/sync", "/healthz"):
        raise RequestError(404, "NOT_FOUND")
    if method != ("POST" if path == "/v1/sync" else "GET"):
        raise RequestError(405, "METHOD_NOT_ALLOWED")
    lengths = headers.get("content-length", [])
    if path == "/healthz":
        if lengths not in ([], ["0"]) or buffered:
            raise RequestError(400, "BAD_REQUEST")
        return path, b"", None, None
    if len(lengths) != 1 or re.fullmatch(r"(?:0|[1-9][0-9]{0,15})", lengths[0]) is None:
        raise RequestError(400, "BAD_REQUEST")
    length = int(lengths[0])
    if length > 4096:
        raise RequestError(413, "TOO_LARGE")
    if (
        not length
        or len(headers.get("timestamp", [])) != 1
        or len(headers.get("signature", [])) != 1
        or len(buffered) > length
    ):
        raise RequestError(400, "BAD_REQUEST")
    body = bytearray(buffered)
    while len(body) < length:
        chunk = connection.recv(length - len(body))
        if not chunk:
            raise RequestError(400, "BAD_REQUEST")
        body.extend(chunk)
    return path, bytes(body), headers["timestamp"][0], headers["signature"][0]


def send_json(connection, status, value, *, retry=False):
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    headers = [
        f"HTTP/1.1 {status} {STATUS[status]}",
        "Content-Type: application/json",
        f"Content-Length: {len(data)}",
        "Connection: close",
        "Cache-Control: no-store",
    ]
    if retry:
        headers.append("Retry-After: 1")
    connection.settimeout(3)
    connection.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + data)


class SyncService:
    def __init__(
        self, runtime, *, supervisor=None, ledger=None, now_seconds=lambda: int(time.time())
    ):
        self.runtime = runtime
        self.supervisor = supervisor if supervisor is not None else R.WorkerSupervisor(runtime)
        self.ledger = ledger if ledger is not None else runtime.ledger()
        self.now_seconds = now_seconds
        self._lock = threading.Lock()
        self._active = None
        self._result = None
        self._thread = None

    def health(self):
        if self._active is not None or self.supervisor.busy:
            return 200, {"ready": False, "busy": True}
        try:
            self.ledger.check()
            return 200, {"ready": True, "busy": False}
        except R.LedgerError as error:
            return (
                (200, {"ready": False, "busy": True})
                if error.code == "LOCK_TIMEOUT"
                else (503, {"ready": False, "busy": False})
            )
        except Exception:
            return 503, {"ready": False, "busy": False}

    def sync(self, body, timestamp, signature):
        try:
            verified = R.verify_request(
                body,
                timestamp,
                signature,
                self.runtime.hmac_secret,
                self.runtime.max_object_bytes,
                self.now_seconds,
            )
        except R.ProtocolError:
            raise R.RuntimeErrorCode("UNAUTHORIZED") from None
        key = (verified.job.job_id, verified.fingerprint)
        with self._lock:
            for known in (self._active, self._result and self._result[0]):
                if known is not None and known[0] == key[0] and known != key:
                    raise R.RuntimeErrorCode("CONFLICT")
            if self._result is not None and self._result[0] == key:
                _, receipt, error = self._result
                if error is None:
                    return 200, receipt
                self._result = None
                raise R.RuntimeErrorCode(error)
            if self._active == key:
                return 202, {"status": "processing"}
            if self._active is not None or self.supervisor.busy or self.supervisor.cancel.is_set():
                raise R.RuntimeErrorCode("BUSY")
            try:
                self.ledger.check()
            except R.LedgerError as error:
                raise R.RuntimeErrorCode(
                    "BUSY" if error.code == "LOCK_TIMEOUT" else "FAILED"
                ) from None
            envelope = R.encode_envelope(body, timestamp, signature, self.runtime.snapshot)
            self._active = key
            self._thread = threading.Thread(
                target=self._execute, args=(key, envelope, verified), daemon=True
            )
            try:
                self._thread.start()
            except RuntimeError:
                self._active, self._thread = None, None
                raise R.RuntimeErrorCode("FAILED") from None
            return 202, {"status": "processing"}

    def _execute(self, key, envelope, verified):
        receipt, error = None, None
        try:
            receipt = self.supervisor.run(envelope, verified)
        except R.RuntimeErrorCode as failure:
            error = failure.code
        except Exception:
            error = "FAILED"
        finally:
            with self._lock:
                self._result = (key, receipt, error)
                self._active = None

    def wait_for_worker(self, deadline):
        if self._thread is not None:
            self._thread.join(max(0, deadline - time.monotonic()))
        return self._thread is not None and self._thread.is_alive()


def handle_connection(connection, service, *, read_seconds=10):
    deadline = ReadDeadline(connection, read_seconds)
    try:
        try:
            request = read_request(connection)
        finally:
            deadline.cancel()
        if deadline.expired:
            return
        path, body, timestamp, signature = request
        if path == "/healthz":
            status, value = service.health()
            send_json(connection, status, value)
        else:
            status, value = service.sync(body, timestamp, signature)
            send_json(connection, status, value)
    except RequestError as error:
        if not deadline.expired:
            try:
                send_json(connection, error.status, {"error": error.code})
            except OSError:
                pass
    except R.RuntimeErrorCode as error:
        status, code = {
            "UNAUTHORIZED": (401, "UNAUTHORIZED"),
            "CONFLICT": (409, "CONFLICT"),
            "BUSY": (503, "BUSY"),
            "TIMEOUT": (504, "TIMEOUT"),
            "BAD_REQUEST": (400, "BAD_REQUEST"),
        }.get(error.code, (503, "SYNC_FAILED"))
        try:
            send_json(connection, status, {"error": code}, retry=status == 503)
        except OSError:
            pass
    except OSError:
        pass
    except Exception:
        try:
            send_json(connection, 503, {"error": "SYNC_FAILED"}, retry=True)
        except OSError:
            pass
    finally:
        deadline.cancel()
        connection.close()


def serve(runtime, *, stop=None):
    """Sólo CLI explícita; no crea estado, secretos ni configuración de red."""
    R.guard_code(CODE_DIR)
    ledger = runtime.ledger()
    try:
        ledger.check()
    except R.LedgerError as error:
        if error.code != "LOCK_TIMEOUT":
            raise
    service = SyncService(runtime, ledger=ledger)
    stop = stop or threading.Event()
    gate = threading.BoundedSemaphore(8)
    owned, threads = set(), set()
    mutex = threading.Lock()

    def handle(connection):
        try:
            handle_connection(connection, service)
        finally:
            with mutex:
                owned.discard(connection)
                threads.discard(threading.current_thread())
            gate.release()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((runtime.config.listen_host, runtime.config.listen_port))
        listener.listen(8)
        listener.settimeout(0.25)
        while not stop.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            if not gate.acquire(blocking=False):
                try:
                    send_json(connection, 503, {"error": "BUSY"}, retry=True)
                except OSError:
                    pass
                finally:
                    connection.close()
                continue
            with mutex:
                owned.add(connection)
                thread = threading.Thread(target=handle, args=(connection,), daemon=True)
                threads.add(thread)
            thread.start()
    finally:
        listener.close()
        service.supervisor.cancel.set()
        with mutex:
            for connection in owned:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            waiting = tuple(threads)
        until = time.monotonic() + 5
        for thread in waiting:
            thread.join(max(0, until - time.monotonic()))
        worker_alive = service.wait_for_worker(until)
        if any(thread.is_alive() for thread in waiting) or worker_alive or service.supervisor.busy:
            raise R.RuntimeErrorCode("BUSY")


def main(argv=None):
    import argparse
    import signal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve",))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    signal.signal(signal.SIGINT, lambda *_args: stop.set())
    try:
        serve(R.load_runtime(args.config), stop=stop)
        return 0
    except Exception:
        sys.stderr.write("SERVICE_FAILED\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
