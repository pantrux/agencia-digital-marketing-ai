"""Recibe en vm-media-01 órdenes que n8n reservó en Studio y verifica su crudo."""

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from contextlib import ExitStack, closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}")
SHA = re.compile(r"[a-f0-9]{64}")
MEDIA = Path("/mnt/producciones")
MAX_BODY = 32768
ORDER_FIELDS = {"order_id", "user_id", "plan_id", "upload_id", "mode", "media"}
OPTIONAL_ORDER_FIELDS = {"output_resolution", "output_format", "output_edit", "additional_media"}


def matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def render_resolution(order):
    resolution = order.get("output_resolution", "1080p")
    if resolution != "1080p" and not (
        resolution == "4k" and order["plan_id"] in ("ultra", "ilimitado")
    ):
        raise ValueError("Resolución fuera del plan")
    return resolution


def render_format(order):
    output_format = order.get("output_format", "mp4")
    if output_format != "mp4" and not (
        output_format == "prores4444" and order["plan_id"] == "ilimitado"
    ):
        raise ValueError("Formato fuera del plan")
    return output_format


def verify_order(request):
    fields = ORDER_FIELDS | {"attempt", "lease", "lease_expires_at"}
    if type(request) is not dict or request.keys() - OPTIONAL_ORDER_FIELDS != fields:
        raise ValueError("Orden inválida")
    if not all(matches(UUID, request[k]) for k in ("order_id", "lease", "user_id", "upload_id")):
        raise ValueError("Identidad inválida")
    if (
        type(request["attempt"]) is not int
        or request["attempt"] < 1
        or type(request["lease_expires_at"]) is not int
        or request["mode"] not in ("sandbox", "production")
        or request["plan_id"] not in ("free", "pro", "ultra", "ilimitado")
    ):
        raise ValueError("Contexto inválido")
    render_resolution(request)
    render_format(request)
    if request.get("output_edit", "full") not in ("full", "reel"):
        raise ValueError("Montaje inválido")
    job = request["media"]
    extra = request.get("additional_media")
    if extra is not None and (type(extra) is not list or not 1 <= len(extra) <= 31):
        raise ValueError("Materiales adicionales inválidos")
    jobs = [job, *(extra or [])]
    seen = set()
    for item in jobs:
        verify_media(item, request["order_id"])
        if item["asset_id"] in seen:
            raise ValueError("Material duplicado")
        seen.add(item["asset_id"])
    return job


def verify_media(job, order):
    if type(job) is not dict or set(job) != {
        "job_id",
        "asset_id",
        "project_slug",
        "extension",
        "bytes",
        "sha256",
        "object_key",
    }:
        raise ValueError("Medio inválido")
    if (
        job["job_id"] != order
        or not matches(UUID, job["asset_id"])
        or job["project_slug"] != "job-" + order.replace("-", "")
        or job["extension"] not in ("mp4", "mov", "webm")
        or not matches(SHA, job["sha256"])
        or type(job["bytes"]) is not int
        or not 0 < job["bytes"] <= 1024**3
        or job["object_key"] != f"ingest/{order}/{job['asset_id']}.{job['extension']}"
    ):
        raise ValueError("Medio inválido")


def source_matches(job):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        parent = os.open("/", flags)
        stack.callback(os.close, parent)
        for component in (*MEDIA.parts[1:], "proyectos", job["project_slug"], "01_raw"):
            parent = os.open(component, flags, dir_fd=parent)
            stack.callback(os.close, parent)
        filename = job["asset_id"] + "." + job["extension"]
        fd = os.open(
            filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
        )
        with os.fdopen(fd, "rb") as media:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != job["bytes"]
            ):
                return False
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                block = media.read(min(remaining, 1024**2))
                if not block:
                    return False
                digest.update(block)
                remaining -= len(block)
            if media.read(1):
                return False

            def identity(info):
                return (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_nlink,
                )

            return digest.hexdigest() == job["sha256"] and identity(before) == identity(
                os.fstat(fd)
            ) == identity(os.stat(filename, dir_fd=parent, follow_symlinks=False))


def receive(db, request):
    job = verify_order(request)
    order_id = request["order_id"]
    canonical = json.dumps(
        {key: request[key] for key in request.keys() & (ORDER_FIELDS | OPTIONAL_ORDER_FIELDS)},
        sort_keys=True,
        separators=(",", ":"),
    )
    previous = db.execute("SELECT payload FROM inbox WHERE order_id=?", (order_id,)).fetchone()
    if previous:
        if previous[0] != canonical:
            raise ValueError("Orden en conflicto")
        return
    if not all(source_matches(item) for item in [job, *request.get("additional_media", [])]):
        raise ValueError("Crudo distinto del confirmado")
    db.execute(
        "INSERT OR IGNORE INTO inbox(order_id,payload,received_at) VALUES(?,?,unixepoch())",
        (order_id, canonical),
    )
    saved = db.execute("SELECT payload FROM inbox WHERE order_id=?", (order_id,)).fetchone()
    if saved != (canonical,):
        raise ValueError("Orden en conflicto")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.client_address[0] != self.server.peer or self.path != "/v1/productions":
            self.send_error(404)
            return
        auth = self.headers.get_all("Authorization", [])
        if len(auth) != 1 or not hmac.compare_digest(
            auth[0].encode("latin-1"), b"Bearer " + self.server.token
        ):
            self.send_error(401)
            return
        length = self.headers.get_all("Content-Length", [])
        if (
            len(length) != 1
            or not re.fullmatch(r"[1-9][0-9]{0,4}", length[0])
            or int(length[0]) > MAX_BODY
            or self.headers.get_all("Content-Type", []) != ["application/json"]
            or self.headers.get("Transfer-Encoding")
        ):
            self.send_error(400)
            return
        try:
            request = json.loads(self.rfile.read(int(length[0])))
            with closing(sqlite3.connect(self.server.database, timeout=3)) as db, db:
                receive(db, request)
        except (OSError, ValueError, sqlite3.Error, TypeError, KeyError):
            self.send_error(422)
            return
        raw = json.dumps({"accepted": True, "order_id": request["order_id"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--listen", required=True)
    cli.add_argument("--peer", required=True)
    cli.add_argument("--token-file", required=True, type=Path)
    cli.add_argument("--state", required=True, type=Path)
    args = cli.parse_args()
    os.umask(0o077)
    if args.state.resolve(strict=True) != args.state or args.state.stat().st_uid != os.getuid():
        raise ValueError("Estado local inválido")
    fd = os.open(args.token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        token = stream.read(65)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.getuid()}
        or stat.S_IMODE(info.st_mode) not in {0o400, 0o440}
        or not re.fullmatch(rb"[a-f0-9]{64}", token)
    ):
        raise ValueError("Credencial local inválida")
    database = args.state / "inbox.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS inbox(order_id TEXT PRIMARY KEY, payload TEXT NOT NULL, received_at INTEGER NOT NULL) STRICT"
        )
    with HTTPServer((args.listen, 3160), Handler) as server:
        server.database = database
        server.peer = args.peer
        server.token = token
        server.serve_forever()


if __name__ == "__main__":
    main()
