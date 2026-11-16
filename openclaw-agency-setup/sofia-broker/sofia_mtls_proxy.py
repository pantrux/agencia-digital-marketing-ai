#!/usr/bin/env python3
"""Recibe sólo el POST firmado de NPM y lo entrega al broker local de Sofía."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import signal
import socket
import ssl
import stat
import sys
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIND = ("192.168.0.7", 3147)
NPM_IP = "192.168.0.8"
BROKER = ("127.0.0.1", 3107)
CREDENTIALS_ROOT = Path("/run/credentials")
MANAGED_UNIT = "agency-sofia-mtls-v1.service"
ROUTE = "/v1/sofia/chat"
LIMIT = 12288
RESPONSE_LIMIT = 24 * 1024
HEADER_LIMIT = 8192
UPSTREAM_TIMEOUT = 37
RAW_LENGTH = re.compile(r"[1-9][0-9]{0,4}")
STAMP = re.compile(r"[0-9]{10}")
SIGNATURE = re.compile(r"v1=[0-9a-f]{64}")
PIN = re.compile(r"[0-9a-f]{64}")
DNS = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
REJECTED = b'{"error":"SOFIA_MTLS_REJECTED"}'


class Refused(Exception):
    def __init__(self, status):
        self.status = status


def profile(value):
    if (
        type(value) is not dict
        or set(value) != {"server_name", "npm_client_dns", "npm_client_sha256"}
        or not all(isinstance(item, str) for item in value.values())
        or not DNS.fullmatch(value["server_name"])
        or not DNS.fullmatch(value["npm_client_dns"])
        or value["server_name"] == value["npm_client_dns"]
        or not PIN.fullmatch(value["npm_client_sha256"])
    ):
        raise ValueError("SOFIA_TLS_PROFILE")
    return value


def context(credentials: Path, settings: dict) -> ssl.SSLContext:
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.verify_mode = ssl.CERT_REQUIRED
    tls.options |= ssl.OP_NO_TICKET
    tls.num_tickets = 0
    tls.post_handshake_auth = False
    tls.set_alpn_protocols(["http/1.1"])
    tls.load_verify_locations(cafile=str(credentials / "client-ca.pem"))
    if tls.cert_store_stats()["x509_ca"] != 1:
        raise ValueError("SOFIA_TLS_CLIENT_CA")
    tls.load_cert_chain(str(credentials / "server.pem"), str(credentials / "server.key"))

    def sni(_sock, call_name, _ctx):
        return (
            None
            if call_name == settings["server_name"]
            else ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        )

    tls.sni_callback = sni
    return tls


def check_credentials(root: Path) -> None:
    if not root.is_absolute() or root != root.resolve(strict=True):
        raise ValueError("SOFIA_TLS_CREDENTIAL_DIR")
    directory = root.lstat()
    effective_uid = os.geteuid()
    managed = root.is_relative_to(CREDENTIALS_ROOT)
    if managed:
        valid_directory = (
            root == CREDENTIALS_ROOT / MANAGED_UNIT
            and directory.st_uid == directory.st_gid == 0
            and stat.S_IMODE(directory.st_mode) == 0o550
        )
        file_uid, file_modes = 0, {0o440}
    else:
        valid_directory = (
            directory.st_uid == effective_uid and stat.S_IMODE(directory.st_mode) == 0o700
        )
        file_uid, file_modes = effective_uid, {0o400, 0o600}
    if (
        not stat.S_ISDIR(directory.st_mode)
        or not valid_directory
        or not os.access(root, os.R_OK | os.X_OK)
    ):
        raise ValueError("SOFIA_TLS_CREDENTIAL_DIR")
    for name in ("profile.json", "server.pem", "server.key", "client-ca.pem"):
        path = root / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != file_uid
            or (managed and info.st_gid != 0)
            or stat.S_IMODE(info.st_mode) not in file_modes
            or not os.access(path, os.R_OK)
            or not 0 < info.st_size <= (4096 if name == "profile.json" else 65536)
        ):
            raise ValueError("SOFIA_TLS_CREDENTIAL_FILE")


def peer_allowed(connection: ssl.SSLSocket, settings: dict) -> bool:
    certificate = connection.getpeercert(binary_form=True)
    if not certificate:
        return False
    actual = hashlib.sha256(certificate).hexdigest()
    if not hmac.compare_digest(actual, settings["npm_client_sha256"]):
        return False
    parsed = connection.getpeercert()
    return parsed is not None and ("DNS", settings["npm_client_dns"]) in parsed.get(
        "subjectAltName", ()
    )


def interrupt(peer: socket.socket) -> None:
    try:
        peer.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def one(headers, name):
    values = headers.get_all(name) or ()
    return values[0] if len(values) == 1 else ""


def forward(raw, stamp, signature):
    with closing(http.client.HTTPConnection(*BROKER, timeout=UPSTREAM_TIMEOUT)) as conn:
        deadline = time.monotonic() + UPSTREAM_TIMEOUT
        conn.connect()
        if conn.sock is None:
            raise OSError("SOFIA_TLS_UPSTREAM")
        conn.auto_open = 0
        until = threading.Timer(max(0, deadline - time.monotonic()), interrupt, args=(conn.sock,))
        until.start()
        try:
            conn.request(
                "POST",
                ROUTE,
                body=raw,
                headers={
                    "content-type": "application/json",
                    "content-length": str(len(raw)),
                    "timestamp": stamp,
                    "signature": signature,
                    "connection": "close",
                },
            )
            with closing(conn.getresponse()) as response:
                status = response.status
                if status in {400, 401, 403, 404, 405, 409, 413, 415, 422, 429, 503}:
                    return status, REJECTED, {}
                length = one(response.headers, "Content-Length")
                stamp = one(response.headers, "timestamp")
                signature = one(response.headers, "signature")
                if (
                    status != 200
                    or one(response.headers, "Content-Type") != "application/json"
                    or response.headers.get_all("Transfer-Encoding")
                    or not RAW_LENGTH.fullmatch(length)
                    or int(length) > RESPONSE_LIMIT
                    or not STAMP.fullmatch(stamp)
                    or not SIGNATURE.fullmatch(signature)
                ):
                    raise Refused(502)
                answer = response.read(int(length) + 1)
                if len(answer) != int(length):
                    raise Refused(502)
                return 200, answer, {"timestamp": stamp, "signature": signature}
        finally:
            until.cancel()


class LimitedHttpInput:
    """Acota la lectura agregada del request line y headers del parser estándar."""

    def __init__(self, source):
        self.source = source
        self.remaining = HEADER_LIMIT

    def readline(self, limit=-1):
        read_limit = self.remaining + 1 if limit < 0 else min(limit, self.remaining + 1)
        data = self.source.readline(read_limit)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise OSError("SOFIA_TLS_HEADER_LIMIT")
        return data

    def read(self, length):
        return self.source.read(length)

    def close(self):
        self.source.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.rfile = LimitedHttpInput(self.rfile)
        self.deadline = threading.Timer(6, interrupt, args=(self.connection,))
        self.deadline.start()

    def finish(self):
        self.deadline.cancel()
        super().finish()

    def handle(self):
        self.close_connection = True
        self.handle_one_request()

    def send_error(self, code, message=None, explain=None):
        self.serve(code, REJECTED, {})

    def log_message(self, format, *args):
        pass

    def handle_expect_100(self):
        self.send_error(417)
        return False

    def serve(self, status, raw, headers):
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

    def exact_route(self):
        parts = self.raw_requestline.split()
        return (
            self.request_version == "HTTP/1.1"
            and len(parts) == 3
            and parts[1] == ROUTE.encode("ascii")
        )

    def do_POST(self):
        try:
            if not self.exact_route():
                raise Refused(404)
            if one(self.headers, "Host") != self.server.settings["server_name"]:
                raise Refused(421)
            length = one(self.headers, "Content-Length")
            if self.headers.get_all("Transfer-Encoding") or not RAW_LENGTH.fullmatch(length):
                raise Refused(400)
            count = int(length)
            if count > LIMIT:
                raise Refused(413)
            if one(self.headers, "Content-Type") != "application/json":
                raise Refused(415)
            stamp = one(self.headers, "timestamp")
            signature = one(self.headers, "signature")
            if not STAMP.fullmatch(stamp) or not SIGNATURE.fullmatch(signature):
                raise Refused(401)
            raw = self.rfile.read(count)
            if len(raw) != count:
                raise Refused(400)
            self.deadline.cancel()
            self.connection.settimeout(UPSTREAM_TIMEOUT)
            status, data, headers = self.server.forward(raw, stamp, signature)
        except Refused as exc:
            self.send_error(exc.status)
        except (OSError, http.client.HTTPException, ValueError):
            self.send_error(502)
        else:
            self.serve(status, data, headers)

    def do_GET(self):
        self.send_error(405 if self.exact_route() else 404)

    do_HEAD = do_OPTIONS = do_PUT = do_PATCH = do_DELETE = do_TRACE = do_CONNECT = do_GET


class EdgeServer(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 8

    def __init__(
        self, tls, settings, *, forward_request=forward, address=BIND, allowed_peer=NPM_IP
    ):
        self.tls = tls
        self.settings = settings
        self.forward = forward_request
        self.allowed_peer = allowed_peer
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def verify_request(self, request, peer):
        return peer[0] == self.allowed_peer

    def process_request(self, request, peer):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, peer)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, peer):
        tls = request
        try:
            tls = self.tls.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
            tls.settimeout(5)
            tls.do_handshake()
            if peer_allowed(tls, self.settings):
                self.finish_request(tls, peer)
        except (OSError, ValueError):
            pass
        finally:
            self.shutdown_request(tls)
            self.slots.release()

    def handle_error(self, request, client_address):
        pass


def main():
    try:
        credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
        check_credentials(credentials)
        settings = profile(json.loads((credentials / "profile.json").read_bytes()))
        tls = context(credentials, settings)
        signal.signal(signal.SIGTERM, terminate)
        with EdgeServer(tls, settings) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (KeyError, OSError, ValueError, TypeError):
        print("SOFIA_MTLS_START_FAILED", file=sys.stderr)
        return 1
    return 0


def terminate(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    raise SystemExit(main())
