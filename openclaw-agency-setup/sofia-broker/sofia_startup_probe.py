"""Confirma el socket y el perfil nativo antes de iniciar el broker."""

import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import stat
import sys
import time
from pathlib import Path
from uuid import uuid4

ROUTE = "/v1/sofia/owner-readiness"
PROTOCOL = "studio-sofia-owner-unix-v1"


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Archivo de preparación inválido")
        if stat.S_IMODE(info.st_mode) not in {0o400, 0o600} or not 0 < info.st_size <= 4096:
            raise ValueError("Permisos o tamaño inválidos")
        return json.load(source)


def sign(key, direction, stamp, action, body):
    data = "\n".join((PROTOCOL, direction, stamp, action, ROUTE, hashlib.sha256(body).hexdigest()))
    return "v1=" + hmac.new(key, data.encode("ascii"), hashlib.sha256).hexdigest()


def check(sock, key, payload):
    now = str(int(time.time()))
    raw = json.dumps(payload, separators=(",", ":")).encode("ascii")
    with socket.socket(socket.AF_UNIX) as unix:
        unix.settimeout(4)
        unix.connect(str(sock))
        connection = http.client.HTTPConnection("localhost", timeout=4)
        connection.auto_open = False
        connection.sock = unix
        try:
            connection.request(
                "POST",
                ROUTE,
                body=raw,
                headers={
                    "Content-Type": "application/json",
                    "Connection": "close",
                    "timestamp": now,
                    "signature": sign(key, "request", now, "POST", raw),
                },
            )
            with connection.getresponse() as response:
                if response.status != 200:
                    return False
                stamp = response.getheader("timestamp", "")
                body = response.read(4097)
                if (
                    len(body) > 4096
                    or not re.fullmatch(r"[0-9]{10}", stamp)
                    or abs(time.time() - int(stamp)) > 60
                ):
                    return False
                expected = sign(key, "response", stamp, "200", body)
                if not hmac.compare_digest(response.getheader("signature", ""), expected):
                    return False
                return json.loads(body) == {
                    **{field: payload[field] for field in ("actor_id", "order_id", "request_id")},
                    "text": "ready",
                }
        finally:
            connection.close()


def main():
    profile = read_private(Path(sys.argv[1]))
    identifiers = read_private(Path(sys.argv[2]))
    sock = Path(profile["socketPath"])
    if sock != Path(f"/run/user/{os.getuid()}/agency-sofia-owner/sofia-owner.sock"):
        raise ValueError("Socket fuera del directorio esperado")
    key = profile["hmacSecret"].encode("ascii")
    payload = {
        "actor_id": profile["ownerActor"],
        "order_id": identifiers["order_id"],
        "request_id": str(uuid4()),
        "billing_mode": profile.get("billingMode", "sandbox"),
        "plan_id": "free",
        "message": "startup-readiness",
    }
    deadline = time.monotonic() + 28
    while time.monotonic() < deadline:
        try:
            info = sock.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                return 1
            return 0 if check(sock, key, payload) else 1
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.3)
        except (OSError, http.client.HTTPException, ValueError):
            return 1
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (IndexError, KeyError, OSError, TypeError, ValueError):
        sys.exit(1)
