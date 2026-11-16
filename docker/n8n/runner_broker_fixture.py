"""Broker local de ensayo del launcher 1.5.1 y el runner Python de n8n 2.46.6."""

import argparse
import asyncio
import hmac
import json
import os
import re
import secrets
import stat
from collections import deque
from http import HTTPStatus
from importlib.metadata import version
from pathlib import Path

from websockets.datastructures import Headers
from websockets.frames import Frame, Opcode
from websockets.http11 import Request
from websockets.protocol import OPEN
from websockets.server import ServerProtocol

HTTP_LIMIT = 8192
AUTH_LIMIT = 512
FRAME_LIMIT = 4096
TASK_ID = "fixture-python-001"
TASK_CODE = 'return [{"json": {"value": _items[0]["json"]["value"] * 2}}]'


class BadRequest(ValueError):
    """Rechazo HTTP con estado fijo y sin detalles del cliente."""

    def __init__(self, status=400):
        self.status = status


class ProtocolError(ValueError):
    """El cliente no cumplió el protocolo de la prueba."""


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Claves JSON duplicadas.")
    return result


def reject_constant(_):
    raise ValueError("Constante JSON no válida.")


def json_object(raw):
    result = json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant
    )
    if not isinstance(result, dict):
        raise ValueError("Se esperaba un objeto JSON.")
    return result


async def read_request(reader):
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError:
        raise BadRequest(431) from None
    if len(raw) > HTTP_LIMIT:
        raise BadRequest(431)
    try:
        first, *fields = raw[:-4].decode("ascii").split("\r\n")
        method, path, http_version = first.split(" ")
        if http_version != "HTTP/1.1" or not re.fullmatch(r"/[!-~]*", path):
            raise BadRequest()
        headers = Headers()
        for field in fields:
            name, value = field.split(":", 1)
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or not re.fullmatch(
                r"[ -~]*", value
            ):
                raise BadRequest()
            headers[name] = value.strip()
    except (UnicodeError, ValueError):
        raise BadRequest() from None
    if any(
        len(headers.get_all(key)) > 1
        for key in ("Host", "Authorization", "Content-Length", "Content-Type")
    ):
        raise BadRequest()
    if "Transfer-Encoding" in headers or "Expect" in headers:
        raise BadRequest()
    return method, path, headers


async def reply(writer, status, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    header = (
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
    ).encode()
    writer.write(header + raw)
    await writer.drain()


class WebSocket:
    """Transporta el protocolo Sans-I/O sobre la misma conexión del broker HTTP."""

    def __init__(self, protocol, reader, writer):
        self.protocol = protocol
        self.reader = reader
        self.writer = writer
        self.pending = deque()
        self.messages = 0

    async def flush(self):
        for chunk in self.protocol.data_to_send():
            self.writer.write(chunk)
        await self.writer.drain()

    async def send(self, payload):
        self.protocol.send_text(json.dumps(payload, separators=(",", ":")).encode())
        await self.flush()

    async def receive(self):
        while not self.pending:
            chunk = await self.reader.read(HTTP_LIMIT)
            if not chunk:
                raise ProtocolError()
            self.protocol.receive_data(chunk)
            await self.flush()
            if self.protocol.state is not OPEN:
                raise ProtocolError()
            for frame in self.protocol.events_received():
                if not isinstance(frame, Frame) or frame.opcode not in (
                    Opcode.TEXT,
                    Opcode.PING,
                    Opcode.PONG,
                ):
                    raise ProtocolError()
                if frame.opcode is Opcode.TEXT:
                    if not frame.fin or len(self.pending) >= 8:
                        raise ProtocolError()
                    self.pending.append(frame.data)
        self.messages += 1
        if self.messages > 32:
            raise ProtocolError()
        try:
            return json_object(self.pending.popleft())
        except (UnicodeError, ValueError, RecursionError):
            raise ProtocolError() from None

    async def expect(self, message_type, *, allow_python_offers=False):
        while True:
            message = await self.receive()
            if message.get("type") == message_type:
                return message
            if not allow_python_offers:
                raise ProtocolError()
            read_offer(message, launcher=False)

    async def close(self, code):
        self.protocol.send_close(code)
        await self.flush()


def read_offer(message, *, launcher):
    offer = message.get("offerId")
    valid = message.get("validFor")
    if (
        message.get("type") != "runner:taskoffer"
        or message.get("taskType") != "python"
        or not isinstance(offer, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", offer)
        or type(valid) is not int
        or not (valid == -1 if launcher else 0 < valid <= 10000)
    ):
        raise ProtocolError()
    return offer


class BrokerFixture:
    """Registra un launcher, un runner Python y una sola tarea sintética fija."""

    def __init__(self, auth_token, *, port=0, connection_seconds=25):
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", auth_token):
            raise ValueError("El token sintético debe tener de 24 a 128 caracteres ASCII.")
        self._auth_token = auth_token.encode()
        self.port = port
        self.connection_seconds = connection_seconds
        self._stage = "new"
        self._grant = None
        self._grants_issued = 0
        self._grants_consumed = 0
        self._events = []
        self._error = None
        self._finished = asyncio.Event()
        self._connections = set()

    async def __aenter__(self):
        self._server = await asyncio.start_server(
            self._connect, "127.0.0.1", self.port, limit=HTTP_LIMIT, backlog=8
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self._server.close()
        pending = tuple(self._connections)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self._server.wait_closed()
        self._grant = None

    def _connect(self, reader, writer):
        if len(self._connections) >= 8:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections.add(task)
        task.add_done_callback(self._connections.discard)

    def result(self):
        return {
            "passed": self._stage == "complete",
            "events": list(self._events),
            "grants_issued": self._grants_issued,
            "grants_consumed": self._grants_consumed,
            "error": self._error,
        }

    async def wait(self, seconds):
        try:
            await asyncio.wait_for(self._finished.wait(), seconds)
        except TimeoutError:
            return False
        return self._stage == "complete"

    async def _handle(self, reader, writer):
        try:
            async with asyncio.timeout(self.connection_seconds):
                try:
                    await self._route(reader, writer)
                except BadRequest as error:
                    await reply(writer, error.status, {"error": "fixture_request_rejected"})
        except (TimeoutError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (TimeoutError, OSError):
                pass

    async def _route(self, reader, writer):
        method, path, headers = await read_request(reader)
        if headers.get("Host") not in {"127.0.0.1", f"127.0.0.1:{self.port}"}:
            raise BadRequest()
        if path == "/runners/auth":
            if method != "POST":
                raise BadRequest(405)
            await self._authorize(reader, writer, headers)
            return
        if "Content-Length" in headers and headers["Content-Length"] != "0":
            raise BadRequest()
        if method == "GET" and path == "/healthz":
            await reply(writer, 200, {"status": "ok"})
        elif method == "GET" and re.fullmatch(r"/runners/_ws\?id=[A-Za-z0-9_-]{1,64}", path):
            await self._upgrade(reader, writer, path, headers)
        else:
            raise BadRequest(404)

    async def _authorize(self, reader, writer, headers):
        length = headers.get("Content-Length", "")
        if not re.fullmatch(r"[1-9][0-9]{0,3}", length):
            raise BadRequest()
        if int(length) > AUTH_LIMIT:
            raise BadRequest(413)
        if headers.get("Content-Type") != "application/json":
            raise BadRequest()
        raw = await reader.readexactly(int(length))
        try:
            data = json_object(raw)
        except (UnicodeError, ValueError, RecursionError):
            raise BadRequest() from None
        if data.keys() != {"token"} or not isinstance(data["token"], str):
            raise BadRequest()
        if not data["token"].isascii() or not hmac.compare_digest(
            data["token"].encode(), self._auth_token
        ):
            raise BadRequest(401)
        if self._stage not in {"new", "deferred"}:
            raise BadRequest(409)
        role = "launcher" if self._stage == "new" else "python"
        token = secrets.token_urlsafe(24)
        self._grant = (role, token)
        self._stage = role + "_granted"
        self._grants_issued += 1
        await reply(writer, 200, {"data": {"token": token}})

    async def _upgrade(self, reader, writer, path, headers):
        auth = headers.get("Authorization", "")
        if self._grant is None or not hmac.compare_digest(auth, "Bearer " + self._grant[1]):
            raise BadRequest(403)
        protocol = ServerProtocol(origins=[None], max_size=FRAME_LIMIT)
        protocol.receive_data(f"GET {path} HTTP/1.1\r\n".encode() + headers.serialize())
        requests = protocol.events_received()
        if len(requests) != 1 or not isinstance(requests[0], Request):
            raise BadRequest()
        response = protocol.accept(requests[0])
        if response.status_code != 101:
            raise BadRequest()
        role, _ = self._grant
        self._grant = None
        self._grants_consumed += 1
        self._stage = role + "_active"
        ws = WebSocket(protocol, reader, writer)
        protocol.send_response(response)
        try:
            await ws.flush()
            await self._conversation(ws, role)
        except (ProtocolError, OSError, TimeoutError):
            self._stage = "failed"
            self._error = "protocol_" + role
            self._finished.set()
        else:
            try:
                await ws.close(1000)
            except OSError:
                # El launcher original corta su TCP al terminar la transferencia.
                pass

    async def _conversation(self, ws, role):
        launcher = role == "launcher"
        await ws.send({"type": "broker:inforequest"})
        identity = await ws.expect("runner:info")
        if identity.get("name") != (
            "launcher-python" if launcher else "Python Task Runner"
        ) or identity.get("types") != ["python"]:
            raise ProtocolError()
        self._events.append(role + "_registered")
        await ws.send({"type": "broker:runnerregistered"})
        offer = read_offer(await ws.expect("runner:taskoffer"), launcher=launcher)
        await ws.send({"type": "broker:taskofferaccept", "offerId": offer, "taskId": TASK_ID})
        if launcher:
            message = await ws.expect("runner:taskdeferred")
            if message.get("taskId") != TASK_ID:
                raise ProtocolError()
            self._events.append("launcher_deferred")
            self._stage = "deferred"
            return
        accepted = await ws.expect("runner:taskaccepted", allow_python_offers=True)
        if accepted.get("taskId") != TASK_ID:
            raise ProtocolError()
        self._events.append("python_task_accepted")
        await ws.send(
            {
                "type": "broker:tasksettings",
                "taskId": TASK_ID,
                "settings": {
                    "code": TASK_CODE,
                    "nodeMode": "runOnceForAllItems",
                    "items": [{"json": {"value": 21}}],
                },
            }
        )
        done = await ws.expect("runner:taskdone", allow_python_offers=True)
        expected = {"result": [{"json": {"value": 42}}]}
        if done.get("taskId") != TASK_ID or json.dumps(
            done.get("data"), sort_keys=True
        ) != json.dumps(expected, sort_keys=True):
            raise ProtocolError()
        await ws.send({"type": "broker:drain"})
        self._events.append("python_task_done")
        self._stage = "complete"
        self._finished.set()


async def serve(token, port, deadline):
    async with BrokerFixture(token, port=port, connection_seconds=deadline) as broker:
        print(json.dumps({"event": "ready", "port": broker.port}), flush=True)
        passed = await broker.wait(deadline)
        result = broker.result()
        if not passed and result["error"] is None:
            result["error"] = "deadline"
        print(json.dumps({"event": "result", **result}), flush=True)
    return 0 if passed else 1


def read_token(path):
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("Archivo sintético no regular o enlazado.")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        current = os.fstat(stream.fileno())
        if not os.path.samestat(before, current) or current.st_nlink != 1:
            raise ValueError("Cambió el archivo sintético.")
        if os.name == "posix" and (current.st_uid != os.geteuid() or current.st_mode & 0o077):
            raise ValueError("El archivo sintético debe ser privado y propio.")
        raw = stream.read(131)
    if not re.fullmatch(rb"[A-Za-z0-9_-]{24,128}(?:\r?\n)?", raw):
        raise ValueError("El archivo debe contener sólo un token sintético ASCII.")
    return raw.decode("ascii").rstrip("\r\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-token-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--deadline-seconds", type=int, default=40)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535 or not 1 <= args.deadline_seconds <= 90:
        parser.error("Puerto o plazo fuera del contrato.")
    if version("websockets") != "15.0.1":
        parser.error("La prueba requiere websockets 15.0.1.")
    try:
        token = read_token(args.auth_token_file)
    except (OSError, ValueError):
        parser.error("El archivo del token sintético no cumple el contrato.")
    raise SystemExit(asyncio.run(serve(token, args.port, args.deadline_seconds)))


if __name__ == "__main__":
    main()
