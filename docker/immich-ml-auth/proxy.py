"""Proxy privado y acotado del protocolo ML Immich 3.3.6; sólo biblioteca estándar."""

import argparse
import http.client
import json
import os
import re
import select
import socket
import stat
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def load_secret(path: Path) -> str:
    """Lee un archivo regular absoluto; los errores nunca incluyen ruta ni contenido."""
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path.absolute():
            raise ValueError
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            value = stream.read(4097)
        if len(value) > 4096:
            raise ValueError
        token = value.removesuffix(b"\n").removesuffix(b"\r").decode("ascii")
        if not re.fullmatch(r"[!-~]{32,4096}", token):
            raise ValueError
        if any(marker in token.upper() for marker in ("CHANGE_ME", "PENDIENTE", "REPLACE_ME")):
            raise ValueError
        return token
    except (OSError, ValueError, UnicodeError):
        raise ValueError("El archivo secreto no está disponible o no es válido.") from None


@dataclass(frozen=True)
class Limits:
    """Topes operativos comunes, independientes de planes comerciales."""

    request_bytes: int = 32 * 1024 * 1024
    response_bytes: int = 8 * 1024 * 1024
    connections: int = 8
    predictions: int = 2
    header_seconds: float = 5.0
    transfer_seconds: float = 120.0
    health_seconds: float = 5.0
    idle_seconds: float = 5.0


CHUNK = 64 * 1024
HEADER_BYTES = 16 * 1024
LINE_BYTES = 4096
BOUNDARY = re.compile(
    r"""multipart/form-data\s*;\s*boundary=(?:"[0-9A-Za-z'()+_,./:=?-]{1,70}"|[0-9A-Za-z'()+_,./:=?-]{1,70})\s*""",
    re.IGNORECASE,
)


def open_upstream(timeout: float) -> http.client.HTTPConnection:
    """El destino literal nunca procede de configuración, URL o cabeceras entrantes."""
    return http.client.HTTPConnection("192.168.0.6", 3003, timeout=timeout)


def close_socket(connection, direction=socket.SHUT_RDWR):
    try:
        connection.shutdown(direction)
    except OSError:
        pass


class HeaderReader:
    """Acota las líneas y el conjunto de cabeceras antes del parser estándar."""

    def __init__(self, stream):
        self.stream = stream
        self.remaining = HEADER_BYTES
        self.complete = False

    def readline(self, _size=-1):
        line = self.stream.readline(min(self.remaining + 1, LINE_BYTES + 1))
        self.remaining -= len(line)
        if len(line) > LINE_BYTES or self.remaining < 0:
            raise http.client.LineTooLong("cabeceras")
        if line == b"\r\n":
            self.complete = True
        return line


class ClientDisconnected(Exception):
    """El cliente cerró la conexión antes de completar su transacción."""


class TransferGuard:
    """Cierra el socket upstream por plazo total o desconexión; no deja hilos huérfanos."""

    def __init__(self, client, upstream, seconds):
        self.client, self.upstream = client, upstream
        self.deadline = time.monotonic() + seconds
        self.body_complete = False
        self.reply_started = False
        self.transport = None
        self.timed_out = self.disconnected = False
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def watch(self):
        while not self.finished.wait(0.025):
            if time.monotonic() >= self.deadline:
                self.timed_out = True
                if not self.body_complete:
                    close_socket(self.client, socket.SHUT_RD)
                elif self.reply_started:
                    close_socket(self.client)
                break
            if self.body_complete:
                try:
                    if select.select([self.client], [], [], 0)[0]:
                        if not self.client.recv(1, socket.MSG_PEEK):
                            self.disconnected = True
                            break
                except OSError:
                    self.disconnected = True
                    break
        if not self.finished.is_set() and self.transport is not None:
            # HTTPConnection puede soltar sock al recibir Connection: close;
            # el cuerpo aún conserva ese mismo transporte hasta terminar.
            close_socket(self.transport)

    def check(self):
        if self.timed_out:
            raise TimeoutError
        if self.disconnected:
            raise ClientDisconnected

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_error):
        self.finished.set()
        self.thread.join()
        self.upstream.close()


class ProxyServer(ThreadingHTTPServer):
    """Número fijo máximo de conexiones y predicciones; ninguna cola de trabajo sin límite."""

    request_queue_size = 8
    allow_reuse_address = True

    def __init__(self, address, secret, limits):
        self._secret = secret
        self.limits = limits
        self.connections = threading.BoundedSemaphore(limits.connections)
        self.predictions = threading.BoundedSemaphore(limits.predictions)
        super().__init__(address, ProxyHandler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            try:
                request.settimeout(0.2)
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def shutdown_request(self, request):
        # Enviar FIN de escritura y drenar un remanente pequeño evita que un
        # cierre con entrada pendiente descarte el rechazo HTTP mediante RST.
        close_socket(request, socket.SHUT_WR)
        deadline = time.monotonic() + 0.1
        remaining = CHUNK
        try:
            while remaining and time.monotonic() < deadline:
                request.settimeout(max(0.001, deadline - time.monotonic()))
                block = request.recv(remaining)
                if not block:
                    break
                remaining -= len(block)
        except OSError:
            pass
        finally:
            super().shutdown_request(request)

    def handle_error(self, *_arguments):
        # Nunca publicar excepciones, rutas, cabeceras ni datos de inferencia.
        pass


class ProxyHandler(BaseHTTPRequestHandler):
    """Una petición por conexión; sólo se copian cabeceras expresamente admitidas."""

    protocol_version = "HTTP/1.0"

    def log_message(self, *_arguments):
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply(code, b'{"error":"PETICION_RECHAZADA"}')

    def reply(self, status, body, content_type="application/json"):
        self.response_started = True
        self.send_response_only(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        for offset in range(0, len(body), CHUNK):
            self.wfile.write(body[offset : offset + CHUNK])

    def handle(self):
        self.close_connection = True
        self.request_version = "HTTP/1.0"
        self.command = None
        self.response_started = False
        self.connection.settimeout(self.server.limits.idle_seconds)
        header_deadline = time.monotonic() + self.server.limits.header_seconds
        timer = threading.Timer(self.server.limits.header_seconds, close_socket, (self.connection,))
        timer.daemon = True
        timer.start()
        try:
            self.raw_requestline = self.rfile.readline(LINE_BYTES + 1)
            if not self.raw_requestline:
                return
            if len(self.raw_requestline) > LINE_BYTES:
                self.send_error(414)
                return
            stream = self.rfile
            reader = HeaderReader(stream)
            self.rfile = reader
            try:
                valid = self.parse_request()
            finally:
                self.rfile = stream
            timer.cancel()
            if valid:
                if not reader.complete or time.monotonic() >= header_deadline:
                    self.send_error(400)
                    return
                if self.request_version not in ("HTTP/1.0", "HTTP/1.1"):
                    self.request_version = "HTTP/1.0"
                    self.send_error(400)
                    return
                self.dispatch()
        except (OSError, http.client.HTTPException, ClientDisconnected):
            pass
        finally:
            timer.cancel()
            timer.join()

    def dispatch(self):
        # BaseHTTPRequestHandler normaliza //; comprobar también el destino original.
        target = self.raw_requestline.split()[1]
        methods = {b"/ping": "GET", b"/healthz": "GET", b"/predict": "POST"}
        if target not in methods:
            self.send_error(404)
            return
        if self.command != methods[target]:
            self.send_error(405)
            return
        if any("\r" in value or "\n" in value for value in self.headers.values()):
            self.send_error(400)
            return
        if "Expect" in self.headers:
            self.send_error(417)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if "Transfer-Encoding" in self.headers or len(lengths) > 1:
            self.send_error(400)
            return
        if lengths and not re.fullmatch(r"[0-9]{1,12}", lengths[0]):
            self.send_error(400)
            return
        length = int(lengths[0]) if lengths else 0
        if self.command == "GET":
            if length:
                self.send_error(400)
            else:
                self.forward("/health", 0, health=True)
            return
        if not lengths:
            self.send_error(411)
            return
        if length > self.server.limits.request_bytes:
            self.send_error(413)
            return
        types = self.headers.get_all("Content-Type", [])
        if length == 0 or len(types) != 1 or not BOUNDARY.fullmatch(types[0]):
            self.send_error(415)
            return
        if not self.server.predictions.acquire(blocking=False):
            self.reply(429, b'{"error":"CONCURRENCIA_AGOTADA"}')
            return
        try:
            self.forward("/predict", length, content_type=types[0])
        finally:
            self.server.predictions.release()

    def forward(self, path, length, *, health=False, content_type=None):
        seconds = (
            self.server.limits.health_seconds if health else self.server.limits.transfer_seconds
        )
        guard = None
        response = None
        try:
            upstream = open_upstream(min(3.0, seconds))
            with TransferGuard(self.connection, upstream, seconds) as guard:
                upstream.connect()
                guard.transport = upstream.sock
                guard.check()
                upstream.sock.settimeout(seconds)
                upstream.putrequest("GET" if health else "POST", path, skip_accept_encoding=True)
                upstream.putheader("Authorization", "Bearer " + self.server._secret)
                upstream.putheader("Connection", "close")
                if not health:
                    upstream.putheader("Content-Type", content_type)
                    upstream.putheader("Content-Length", str(length))
                upstream.endheaders()
                remaining = length
                while remaining:
                    guard.check()
                    block = self.rfile.read1(min(CHUNK, remaining))
                    if not block:
                        raise ClientDisconnected
                    upstream.send(block)
                    remaining -= len(block)
                guard.body_complete = True
                response = upstream.getresponse()
                guard.check()
                body = bytearray()
                maximum = (
                    min(self.server.limits.response_bytes, 64 * 1024)
                    if health
                    else self.server.limits.response_bytes
                )
                while block := response.read1(min(CHUNK, maximum + 1 - len(body))):
                    body.extend(block)
                    if len(body) > maximum:
                        raise http.client.HTTPException
                    guard.check()
                if response.length not in (None, 0):
                    raise http.client.IncompleteRead(body)
                status = response.status
                response_type = response.getheader("Content-Type", "application/octet-stream")
                if "\r" in response_type or "\n" in response_type:
                    raise http.client.HTTPException
                if health and status == 200:
                    try:
                        data = json.loads(body)
                        ready = (
                            data.get("ready") is True
                            and data.get("cuda_available") is True
                            and data.get("service") == "immich-ml"
                            and data.get("status") == "ok"
                        )
                    except (ValueError, AttributeError):
                        ready = False
                    if not ready:
                        status = 503
                guard.check()
                guard.reply_started = True
                self.reply(status, body, response_type)
        except ClientDisconnected:
            return
        except TimeoutError:
            if not self.response_started:
                self.reply(504, b'{"error":"PLAZO_UPSTREAM_AGOTADO"}')
        except (OSError, http.client.HTTPException):
            if guard is not None and guard.disconnected:
                return
            if not self.response_started:
                if guard is not None and guard.timed_out:
                    self.reply(504, b'{"error":"PLAZO_UPSTREAM_AGOTADO"}')
                else:
                    self.reply(502, b'{"error":"UPSTREAM_NO_DISPONIBLE"}')
        finally:
            if response is not None:
                response.close()


def create_server(secret_file: Path, address=("0.0.0.0", 3003), limits=None):
    """Falla antes de escuchar si falta el secreto; no acepta configuración de upstream."""
    return ProxyServer(
        address, load_secret(secret_file), limits if limits is not None else Limits()
    )


def main():
    parser = argparse.ArgumentParser(
        description="Proxy ML privado con destino fijo y secreto de archivo."
    )
    parser.add_argument("--secret-file", type=Path, default=Path("/run/secrets/gpu_bridge_bearer"))
    parser.add_argument("--healthcheck", action="store_true")
    arguments = parser.parse_args()
    if arguments.healthcheck:
        connection = http.client.HTTPConnection("127.0.0.1", 3003, timeout=6)
        try:
            connection.request("GET", "/healthz")
            return 0 if connection.getresponse().status == 200 else 1
        except (OSError, http.client.HTTPException):
            return 1
        finally:
            connection.close()
    try:
        with create_server(arguments.secret_file) as server:
            server.serve_forever()
    except ValueError:
        parser.exit(1, "El secreto requerido no está disponible o no es válido.\n")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
