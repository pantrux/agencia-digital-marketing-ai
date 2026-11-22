#!/usr/bin/env python3
"""Puente de texto firmado entre Studio y un OpenClaw aislado."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PATH = "/v1/sofia/chat"
PROTOCOL = "studio-sofia-v1"
UNIX_PROTOCOL = "studio-sofia-owner-unix-v1"
UNIX_PATH = "/v1/sofia/owner-turn"
UNIX_FRESHNESS = 60
AGENT = "agency-account"
BODY_LIMIT = 12 * 1024
UPSTREAM_LIMIT = 32 * 1024
UPSTREAM_DEADLINE = 30
INPUT_LIMIT = 2048
REPLY_LIMIT = 8192
FRESHNESS = 300
REPLY_TTL = 3600
REQUEST_TTL = 7 * 86400
CONCURRENCY_WINDOW = 90
MAX_ACTIVE = 1
MAX_LEDGER = 50_000
ACTOR_RATE = 6
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
HEX = re.compile(r"[0-9a-f]{64}")
TIME = re.compile(r"(?:0|[1-9][0-9]{0,15})")
SECRET = re.compile(rb"[!-~]{32,128}")


class Rejected(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(str(status))


class DelegateUnavailable(Exception):
    """El delegado no recibió bytes de este turno."""


def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(pairs)
    if len(pairs) != len(fields):
        raise ValueError("JSON duplicado")
    return fields


def encode(fields: dict[str, object]) -> bytes:
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fields_from(raw: bytes) -> dict[str, object]:
    fields = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    if not isinstance(fields, dict):
        raise ValueError("JSON de tipo inválido")
    return fields


def valid_text(value: object, limit: int) -> bool:
    if not isinstance(value, str) or len(value) > limit or not value.strip():
        return False
    for char in value:
        code = ord(char)
        if (code < 32 and code not in (9, 10, 13)) or code == 127 or 0xD800 <= code <= 0xDFFF:
            return False
    return len(value.encode("utf-8")) <= limit


def matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sign(
    key: bytes, direction: str, stamp: str, action: str, body_hash: str, *, unix: bool = False
) -> str:
    protocol, path = (UNIX_PROTOCOL, UNIX_PATH) if unix else (PROTOCOL, PATH)
    wire = "\n".join((protocol, direction, stamp, action, path, body_hash)).encode("ascii")
    return "v1=" + hmac.new(key, wire, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Turn:
    actor: str
    order: str
    request: str
    message: str
    billing_mode: str = "sandbox"
    plan_id: str = "free"

    @property
    def key(self) -> tuple[str, str, str]:
        return self.actor, self.order, self.request


def signed_turn(raw: bytes) -> Turn:
    try:
        fields = fields_from(raw)
    except (ValueError, RecursionError):
        raise Rejected(422) from None
    expected = {
        "actor_id",
        "order_id",
        "request_id",
        "agent_id",
        "billing_mode",
        "plan_id",
        "message",
    }
    if set(fields) != expected or fields["agent_id"] != AGENT:
        raise Rejected(422)
    if fields["billing_mode"] not in ("sandbox", "production") or fields["plan_id"] not in (
        "free",
        "pro",
        "ultra",
        "ilimitado",
    ):
        raise Rejected(403)
    actor, order, request, message = (
        fields["actor_id"],
        fields["order_id"],
        fields["request_id"],
        fields["message"],
    )
    if not matches(HEX, actor) or not matches(UUID, order) or not matches(UUID, request):
        raise Rejected(422)
    if not valid_text(message, INPUT_LIMIT):
        raise Rejected(422)
    return Turn(actor, order, request, message, fields["billing_mode"], fields["plan_id"])


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self.transaction() as database:
            database.execute("""
                CREATE TABLE IF NOT EXISTS turns (
                    actor TEXT NOT NULL, order_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL, started INTEGER NOT NULL,
                    finished INTEGER, text TEXT,
                    PRIMARY KEY (actor, order_id, request_id)
                )
            """)
            database.execute("CREATE INDEX IF NOT EXISTS turns_started ON turns (started)")
            database.execute("CREATE INDEX IF NOT EXISTS turns_finished ON turns (finished)")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(str(self.path), timeout=2, isolation_level=None)) as database:
            database.execute("PRAGMA secure_delete = ON")
            database.execute("BEGIN IMMEDIATE")
            with database:
                yield database

    def reserve(self, turn: Turn, body_hash: str, now: int) -> str | None:
        with self.transaction() as database:
            database.execute("DELETE FROM turns WHERE finished <= ?", (now - REQUEST_TTL,))
            database.execute(
                "UPDATE turns SET text = NULL WHERE text IS NOT NULL AND finished <= ?",
                (now - REPLY_TTL,),
            )
            previous = database.execute(
                """
                SELECT body_sha256, finished, text FROM turns
                WHERE actor = ? AND order_id = ? AND request_id = ?
            """,
                turn.key,
            ).fetchone()
            if previous is not None:
                old_hash, finished, reply = previous
                if not hmac.compare_digest(old_hash, body_hash) or (
                    finished is not None and reply is None
                ):
                    rejected = 409
                elif finished is None:
                    rejected = 429
                else:
                    return reply
            elif self.has_capacity(database, turn, now):
                database.execute(
                    """
                    INSERT INTO turns (actor, order_id, request_id, body_sha256, started)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (*turn.key, body_hash, now),
                )
                rejected = None
            else:
                rejected = 429
        if rejected is not None:
            raise Rejected(rejected)
        return None

    @staticmethod
    def has_capacity(database: sqlite3.Connection, turn: Turn, now: int) -> bool:
        locked = database.execute(
            """
            SELECT 1 FROM turns WHERE actor = ? AND order_id = ? AND finished IS NULL LIMIT 1
        """,
            (turn.actor, turn.order),
        ).fetchone()
        (active,) = database.execute(
            """
            SELECT count(*) FROM turns WHERE finished IS NULL AND started > ?
        """,
            (now - CONCURRENCY_WINDOW,),
        ).fetchone()
        (recent,) = database.execute(
            """
            SELECT count(*) FROM turns WHERE actor = ? AND started > ?
        """,
            (turn.actor, now - 60),
        ).fetchone()
        (total,) = database.execute("SELECT count(*) FROM turns").fetchone()
        return not locked and active < MAX_ACTIVE and recent < ACTOR_RATE and total < MAX_LEDGER

    def finish(self, turn: Turn, text: str, now: int) -> None:
        with self.transaction() as database:
            saved = database.execute(
                """
                UPDATE turns SET text = ?, finished = ?
                WHERE actor = ? AND order_id = ? AND request_id = ? AND finished IS NULL
            """,
                (text, now, *turn.key),
            )
            if saved.rowcount != 1:
                raise Rejected(503)

    def release_unsent(self, turn: Turn, body_hash: str) -> None:
        with self.transaction() as database:
            database.execute(
                """
                DELETE FROM turns WHERE actor = ? AND order_id = ? AND request_id = ?
                AND body_sha256 = ? AND finished IS NULL
            """,
                (*turn.key, body_hash),
            )


class Sofia:
    def __init__(
        self,
        key: bytes,
        ledger: Ledger,
        run: Callable[[Turn], str],
        owner: str,
        clock: Callable[[], float] = time.time,
        *,
        billing_mode: str = "sandbox",
    ):
        self.key = key
        self.ledger = ledger
        self.run = run
        self.owner = owner
        self.clock = clock
        self.billing_mode = billing_mode

    def process(self, raw: bytes, stamp: str, signature: str) -> tuple[bytes, dict[str, str]]:
        if len(raw) > BODY_LIMIT:
            raise Rejected(413)
        now = int(self.clock())
        if not TIME.fullmatch(stamp) or abs(now - int(stamp)) > FRESHNESS:
            raise Rejected(401)
        if not signature.startswith("v1=") or not HEX.fullmatch(signature[3:]):
            raise Rejected(401)
        fingerprint = digest(raw)
        expected = sign(self.key, "request", stamp, "POST", fingerprint)
        if not hmac.compare_digest(expected, signature):
            raise Rejected(401)
        turn = signed_turn(raw)
        if turn.billing_mode != self.billing_mode:
            raise Rejected(403)
        try:
            reply = self.ledger.reserve(turn, fingerprint, now)
            if reply is None:
                try:
                    reply = self.run(turn)
                except DelegateUnavailable:
                    self.ledger.release_unsent(turn, fingerprint)
                    raise Rejected(503) from None
                if not valid_text(reply, REPLY_LIMIT):
                    raise Rejected(503)
                self.ledger.finish(turn, reply, int(self.clock()))
        except (OSError, sqlite3.Error, http.client.HTTPException, ValueError, RecursionError):
            # Se conserva la reserva cuando la ejecución remota puede seguir viva.
            raise Rejected(503) from None
        response = encode(
            {
                "actor_id": turn.actor,
                "order_id": turn.order,
                "request_id": turn.request,
                "text": reply,
            }
        )
        response_stamp = str(int(self.clock()))
        return response, {
            "timestamp": response_stamp,
            "signature": sign(self.key, "response", response_stamp, "200", digest(response)),
        }


def interrupt_read(peer: socket.socket) -> None:
    try:
        peer.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path):
        super().__init__("localhost", timeout=UPSTREAM_DEADLINE)
        self.path = path

    def connect(self) -> None:
        try:
            require_private(self.path.parent, directory=True)
            info = self.path.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
            ):
                raise ValueError("Socket privado inválido")
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                peer.settimeout(self.timeout)
                peer.connect(str(self.path))
            except OSError:
                peer.close()
                raise
            self.sock = peer
        except (OSError, ValueError, RuntimeError):
            raise DelegateUnavailable from None


class OwnerUnix:
    def __init__(self, path: Path, key: bytes, clock: Callable[[], float] = time.time):
        self.path = path
        self.key = key
        self.clock = clock

    def __call__(self, turn: Turn) -> str:
        body = encode(
            {
                "actor_id": turn.actor,
                "order_id": turn.order,
                "request_id": turn.request,
                "billing_mode": turn.billing_mode,
                "plan_id": turn.plan_id,
                "message": turn.message,
            }
        )
        with closing(UnixHTTPConnection(self.path)) as connection:
            connection.auto_open = 0
            deadline = time.monotonic() + UPSTREAM_DEADLINE
            connection.connect()
            remaining = deadline - time.monotonic()
            if connection.sock is None or remaining <= 0:
                raise DelegateUnavailable
            timeout = threading.Timer(remaining, interrupt_read, args=(connection.sock,))
            timeout.start()
            try:
                return self.request(connection, body, turn)
            finally:
                timeout.cancel()

    def request(self, connection: UnixHTTPConnection, body: bytes, turn: Turn) -> str:
        stamp = str(int(self.clock()))
        connection.request(
            "POST",
            UNIX_PATH,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Connection": "close",
                "timestamp": stamp,
                "signature": sign(self.key, "request", stamp, "POST", digest(body), unix=True),
            },
        )
        with closing(connection.getresponse()) as response:
            stamp = response.getheader("timestamp") or ""
            signature = response.getheader("signature") or ""
            if (
                response.status != 200
                or response.getheader("Content-Type") != "application/json"
                or re.fullmatch(r"[0-9]{10}", stamp) is None
                or not signature.startswith("v1=")
                or HEX.fullmatch(signature[3:]) is None
                or abs(int(self.clock()) - int(stamp)) > UNIX_FRESHNESS
            ):
                raise Rejected(503)
            raw = response.read(UPSTREAM_LIMIT + 1)
            expected = sign(self.key, "response", stamp, "200", digest(raw), unix=True)
            if len(raw) > UPSTREAM_LIMIT or not hmac.compare_digest(signature, expected):
                raise Rejected(503)
        reply = fields_from(raw)
        ids = (reply.get("actor_id"), reply.get("order_id"), reply.get("request_id"))
        text = reply.get("text")
        if set(reply) != {"actor_id", "order_id", "request_id", "text"} or ids != turn.key:
            raise Rejected(503)
        if not valid_text(text, REPLY_LIMIT):
            raise Rejected(503)
        return text


class SofiaHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        self.request.settimeout(10)
        super().setup()
        self.read_deadline = threading.Timer(10, interrupt_read, args=(self.request,))
        self.read_deadline.start()

    def finish(self) -> None:
        self.read_deadline.cancel()
        super().finish()

    def serve(self, status: int, raw: bytes, headers: dict[str, str]) -> None:
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.serve(code, b'{"error":"SOFIA_REQUEST_REJECTED"}', {})

    def log_message(self, format: str, *args: object) -> None:
        pass

    def one_header(self, name: str) -> str:
        fields = self.headers.get_all(name, [])
        return fields[0] if len(fields) == 1 else ""

    def valid_request(self) -> bool:
        parts = self.raw_requestline.split(maxsplit=2)
        return len(parts) == 3 and parts[1] == PATH.encode("ascii")

    def do_POST(self) -> None:
        try:
            if not self.valid_request():
                raise Rejected(404)
            length = self.one_header("Content-Length")
            if self.headers.get_all("Transfer-Encoding") or not re.fullmatch(
                r"[1-9][0-9]{0,6}", length
            ):
                raise Rejected(400)
            size = int(length)
            if size > BODY_LIMIT:
                raise Rejected(413)
            if self.one_header("Content-Type").split(";", 1)[0].lower() != "application/json":
                raise Rejected(415)
            stamp, signature = self.one_header("timestamp"), self.one_header("signature")
            if not stamp or not signature:
                raise Rejected(401)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise Rejected(400)
            self.read_deadline.cancel()
            body, headers = self.server.sofia.process(raw, stamp, signature)
        except Rejected as error:
            self.read_deadline.cancel()
            self.send_error(error.status)
        else:
            self.serve(200, body, headers)

    def do_GET(self) -> None:
        self.send_error(405 if self.valid_request() else 404)

    do_HEAD = do_GET
    do_OPTIONS = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_TRACE = do_GET
    do_CONNECT = do_GET


class SofiaServer(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], sofia: Sofia):
        self.sofia = sofia
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, SofiaHandler)

    def process_request(self, request, client_address) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address) -> None:
        # BaseServer imprime el traceback, que puede contener datos del request.
        pass


def require_private(path: Path, directory: bool) -> None:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("Ruta privada inválida")
    info = path.stat()
    if directory:
        approved = stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
    else:
        approved = (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) in {0o400, 0o600}
        )
    if not approved or info.st_uid != os.geteuid():
        raise ValueError("Permisos privados inválidos")


def private_secret(path: Path) -> bytes:
    require_private(path, directory=False)
    with path.open("rb") as source:
        value = source.read(131).removesuffix(b"\n")
    if value == b"CHANGE_ME" or not SECRET.fullmatch(value):
        raise ValueError("Secreto con formato inválido")
    return value


def owner_profile(path: Path) -> tuple[str, Path, bytes, str]:
    require_private(path, directory=False)
    with path.open("rb") as source:
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("Perfil privado inválido")
    fields = fields_from(raw)
    expected = {"billingMode", "oauthProfileId", "ownerActor", "socketPath", "hmacSecret"}
    if set(fields) != expected or fields["billingMode"] not in ("sandbox", "production"):
        raise ValueError("Perfil privado inválido")
    actor, address, key = fields["ownerActor"], fields["socketPath"], fields["hmacSecret"]
    if not matches(HEX, actor) or not isinstance(address, str) or not isinstance(key, str):
        raise ValueError("Perfil privado inválido")
    address_path = Path(address)
    key_bytes = key.encode("ascii")
    if (
        not address_path.is_absolute()
        or address_path.name != "sofia-owner.sock"
        or str(address_path) != address
        or len(os.fsencode(address_path)) > 100
        or not SECRET.fullmatch(key_bytes)
    ):
        raise ValueError("Perfil privado inválido")
    return actor, address_path, key_bytes, fields["billingMode"]


def on_terminate(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hmac-file", required=True, type=Path)
    parser.add_argument("--owner-profile-file", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("El servicio requiere permisos POSIX")
    os.umask(0o077)
    try:
        key = private_secret(args.hmac_file)
        owner, socket_path, local_key, billing_mode = owner_profile(args.owner_profile_file)
        if hmac.compare_digest(key, local_key):
            raise ValueError("Credenciales de servicio distintas requeridas")
        require_private(args.state_dir, directory=True)
        state = args.state_dir / "turns.sqlite3"
        if state.exists() or state.is_symlink():
            require_private(state, directory=False)
        instance = Sofia(
            key, Ledger(state), OwnerUnix(socket_path, local_key), owner, billing_mode=billing_mode
        )
        signal.signal(signal.SIGTERM, on_terminate)
        with SofiaServer(("127.0.0.1", 3107), instance) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        print("SOFIA_START_FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
