"""Cliente multipart stdlib fijo de Speaches; sin modelos, proxies ni fallback."""

from __future__ import annotations

import http.client
import os
import re
import select
import socket
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from speaches_contract import (
    MAX_AUDIO_BYTES,
    MAX_MULTIPART_BYTES,
    MAX_RESPONSE_BYTES,
    SpeachesError,
    parse_verbose,
)

HOST = "192.168.0.6"
PORT = 8000
PATH = "/v1/audio/transcriptions"
MODEL = "Systran/faster-whisper-small"
CHUNK_BYTES = 64 * 1024


def _fail(code):
    raise SpeachesError(code)


def _snapshot(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_nlink,
        info.st_mtime_ns,
        # En CPython 3.12/Windows lstat y fstat aún difieren en ctime de creación.
        info.st_ctime_ns if sys.platform.startswith("linux") else None,
    )


def _open_file(path, maximum, private):
    """Ancla los ancestros con descriptores NoFollow en el runtime Linux."""
    directories = []
    fd = None
    try:
        path = Path(path).absolute()
        if ".." in path.parts or "://" in os.fspath(path):
            _fail("SPEACHES_INPUT_REJECTED")
        if sys.platform.startswith("linux"):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directories.append(os.open(path.anchor, flags))
            for part in path.parts[1:-1]:
                directories.append(os.open(part, flags, dir_fd=directories[-1]))
            leaf = os.stat(path.name, dir_fd=directories[-1], follow_symlinks=False)
        else:
            # Sólo fixtures portables. El archivo Bearer se admite únicamente en Linux.
            for parent in (*reversed(path.parents), path):
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
                ):
                    _fail("SPEACHES_INPUT_REJECTED")
            leaf = path.lstat()
        if not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1 or not 0 < leaf.st_size <= maximum:
            _fail(
                "SPEACHES_AUDIO_TOO_LARGE" if leaf.st_size > maximum else "SPEACHES_INPUT_REJECTED"
            )
        if private:
            if not sys.platform.startswith("linux"):
                _fail("SPEACHES_SECRET_FILE_UNSUPPORTED")
            if leaf.st_uid != os.geteuid() or stat.S_IMODE(leaf.st_mode) not in (0o400, 0o600):
                _fail("SPEACHES_SECRET_FILE_REJECTED")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
        )
        fd = (
            os.open(path.name, flags, dir_fd=directories[-1])
            if directories
            else os.open(path, flags)
        )
        before = os.fstat(fd)
        if _snapshot(before) != _snapshot(leaf):
            _fail("SPEACHES_INPUT_REJECTED")
        stream = os.fdopen(fd, "rb")
        fd = None
        return path, stream, before
    except SpeachesError:
        raise
    except OSError:
        raise SpeachesError("SPEACHES_INPUT_REJECTED") from None
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


@contextmanager
def _file(path, maximum, *, private=False):
    """No transforma errores del consumidor en errores de apertura del archivo."""
    path, stream, before = _open_file(path, maximum, private)
    try:
        yield stream, before
        try:
            unchanged = _snapshot(os.fstat(stream.fileno())) == _snapshot(before) and _snapshot(
                path.lstat()
            ) == _snapshot(before)
        except OSError:
            unchanged = False
        if not unchanged:
            _fail("SPEACHES_INPUT_CHANGED")
    finally:
        stream.close()


def _read_token(path):
    try:
        with _file(path, 256, private=True) as (stream, _info):
            raw = stream.read(257)
        if not 32 <= len(raw) <= 256 or re.fullmatch(rb"[A-Za-z0-9._~+/-]+={0,}", raw) is None:
            _fail("SPEACHES_SECRET_FILE_REJECTED")
        return raw.decode("ascii")
    except SpeachesError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise SpeachesError("SPEACHES_SECRET_FILE_REJECTED") from None


class _Deadline:
    """Sólo posee el socket de esta solicitud; se retira antes de devolver control."""

    def __init__(self, total, idle, cancel):
        if (
            type(total) not in (int, float)
            or type(idle) not in (int, float)
            or not 0 < idle <= 15
            or not idle <= total <= 300
        ):
            _fail("SPEACHES_BUDGET_REJECTED")
        self.until = time.monotonic() + total
        self.idle = idle
        self.cancel = cancel
        self.reason = None
        self.socket = None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._watch, daemon=True, name="speaches-deadline")
        self.thread.start()

    def _watch(self):
        while not self.stop.wait(0.02):
            if self.cancel is not None and self.cancel.is_set():
                self._abort("SPEACHES_CANCELLED")
                return
            if time.monotonic() >= self.until:
                self._abort("SPEACHES_TIMEOUT")
                return

    def _abort(self, reason):
        with self.lock:
            self.reason = self.reason or reason
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.socket.close()

    def attach(self, transport):
        with self.lock:
            self.socket = transport
        self.check()

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            self._abort("SPEACHES_CANCELLED")
        if time.monotonic() >= self.until:
            self._abort("SPEACHES_TIMEOUT")
        if self.reason:
            _fail(self.reason)
        return min(self.idle, self.until - time.monotonic())

    def finish(self):
        self.stop.set()
        self.thread.join()


class _FixedConnection(http.client.HTTPConnection):
    def __init__(self, deadline):
        super().__init__(HOST, PORT, timeout=deadline.check())
        self.deadline = deadline

    def connect(self):
        transport = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock = transport
        self.deadline.attach(transport)
        transport.settimeout(self.deadline.check())
        transport.connect((HOST, PORT))


def _connect(deadline):
    return _FixedConnection(deadline)


def _wait_first_byte(deadline, seconds):
    """Espera legibilidad sin consumir la respuesta y restaura idle antes del parseo."""
    until = min(deadline.until, time.monotonic() + seconds)
    transport = deadline.socket
    while True:
        deadline.check()
        remaining = until - time.monotonic()
        if remaining <= 0:
            _fail("SPEACHES_TIMEOUT")
        readable, _, _ = select.select([transport], [], [], min(remaining, 0.05))
        if readable:
            if time.monotonic() >= until:
                _fail("SPEACHES_TIMEOUT")
            transport.settimeout(deadline.check())
            return


def _multipart(boundary, filename, language):
    fields = [
        ("model", MODEL),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "word"),
        ("stream", "false"),
    ]
    if language is not None:
        if type(language) is not str or re.fullmatch(r"[a-z]{2,3}", language) is None:
            _fail("SPEACHES_INPUT_REJECTED")
        fields.append(("language", language))
    prefix = b""
    for name, value in fields:
        prefix += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode(
            "ascii"
        )
    prefix += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(
        "ascii"
    )
    return prefix, f"\r\n--{boundary}--\r\n".encode("ascii")


def transcribe_audio(
    audio_path,
    token_file,
    *,
    language=None,
    cancel_event=None,
    total_seconds=300,
    idle_seconds=15,
    first_byte_seconds=None,
):
    """Envía únicamente MP3/WAV preparado; nunca extrae video ni reintenta una petición."""
    if first_byte_seconds is not None and (
        type(first_byte_seconds) not in (int, float) or not 0 < first_byte_seconds <= 45
    ):
        _fail("SPEACHES_BUDGET_REJECTED")
    deadline = _Deadline(total_seconds, idle_seconds, cancel_event)
    connection = response = None
    try:
        deadline.check()
        suffix = Path(audio_path).suffix.lower()
        if suffix not in (".mp3", ".wav"):
            _fail("SPEACHES_INPUT_REJECTED")
        with _file(audio_path, MAX_AUDIO_BYTES) as (stream, info):
            token = _read_token(token_file)
            boundary = "agency-speaches-" + uuid.uuid4().hex
            prefix, suffix_body = _multipart(boundary, "audio" + suffix, language)
            length = len(prefix) + info.st_size + len(suffix_body)
            if length > MAX_MULTIPART_BYTES:
                _fail("SPEACHES_AUDIO_TOO_LARGE")
            connection = _connect(deadline)
            connection.putrequest("POST", PATH)
            connection.putheader("Authorization", "Bearer " + token)
            connection.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
            connection.putheader("Content-Length", str(length))
            connection.putheader("Connection", "close")
            connection.endheaders()
            deadline.check()
            connection.send(prefix)
            count = 0
            while True:
                deadline.check()
                block = stream.read(CHUNK_BYTES)
                if not block:
                    break
                count += len(block)
                if count > info.st_size:
                    _fail("SPEACHES_INPUT_CHANGED")
                connection.send(block)
            if count != info.st_size:
                _fail("SPEACHES_INPUT_CHANGED")
            connection.send(suffix_body)
            deadline.check()
            if first_byte_seconds is not None:
                _wait_first_byte(deadline, first_byte_seconds)
            response = connection.getresponse()
            deadline.check()
            if response.status != 200:
                _fail(
                    {
                        401: "SPEACHES_AUTH_REJECTED",
                        413: "SPEACHES_AUDIO_TOO_LARGE",
                        429: "SPEACHES_BUSY",
                    }.get(
                        response.status,
                        "SPEACHES_REDIRECT_REJECTED"
                        if 300 <= response.status < 400
                        else "SPEACHES_UPSTREAM_REJECTED",
                    )
                )
            headers = response.getheaders()
            lengths = [value for name, value in headers if name.lower() == "content-length"]
            types = [value for name, value in headers if name.lower() == "content-type"]
            if (
                len(headers) > 32
                or sum(len(name) + len(value) + 4 for name, value in headers) > 16 * 1024
                or len(lengths) != 1
                or re.fullmatch(r"[1-9][0-9]*", lengths[0]) is None
                or int(lengths[0]) > MAX_RESPONSE_BYTES
                or len(types) != 1
                or types[0].split(";", 1)[0].strip().lower() != "application/json"
                or any(
                    name.lower() in ("transfer-encoding", "content-encoding") for name, _ in headers
                )
            ):
                _fail("SPEACHES_INVALID_RESPONSE")
            expected = int(lengths[0])
            body = bytearray()
            while len(body) < expected:
                timeout = deadline.check()
                if deadline.socket is not None:
                    deadline.socket.settimeout(timeout)
                block = response.read(min(CHUNK_BYTES, expected - len(body)))
                deadline.check()
                if not block:
                    _fail("SPEACHES_RESPONSE_TRUNCATED")
                body.extend(block)
            result = parse_verbose(bytes(body))
            deadline.check()
        return result
    except SpeachesError:
        raise
    except TimeoutError:
        deadline.check()
        raise SpeachesError("SPEACHES_TIMEOUT") from None
    except (OSError, http.client.HTTPException, ValueError, TypeError):
        deadline.check()
        raise SpeachesError("SPEACHES_TRANSPORT_FAILED") from None
    finally:
        deadline.finish()
        for resource in (response, connection, deadline.socket):
            if resource is not None:
                try:
                    resource.close()
                except (OSError, http.client.HTTPException):
                    pass
