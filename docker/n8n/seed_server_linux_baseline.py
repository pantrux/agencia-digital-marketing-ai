"""Siembra el store Linux musl y verifica el lock base de n8n sin ejecutar scripts."""

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import stat
import sys
import tarfile
import threading
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from receive_server_linux_baseline import MANIFEST, MIB, load_helper, need, storage  # noqa: E402

OWNER = "agencia-digital-marketing-ai:n8n-server-baseline-seed:v1"
LOOPBACK = "c8a3fde0022130f95961bef4931d16cce25155f18d7d2844290c2455c98bd36f"
INDEX = "c2cf0aed8b51a2e2c18e789e72e7f91b491700148235b30179b5e5461dd2eabf"
COMPACT = "fa4240f468159cf04efb4e530c8f9696145c727e0086bf5e0ebc4e50563f8e2c"
CONTEXT = "2dfc977abe83650bdf8756e43c876265c18254d9151b3d9830e9c794604f5aaa"
LOCK = "f9cc9a03cd857a379c8e36d4a457c797cd4f07cb71dab711c9f554273055d516"
VENDOR = "16dcd4442cb7073182c1eb2951e495c4415f7de8924d188fafb670a8798da662"
VENDOR_PATH = "vendor/wa-sqlite-1.0.9-upstream-nogit.tgz"
VENDOR_SRI = "sha512-e+HlfuBuUc8VaIawuO4yfKTzUQvCH4elMehycF431g6R7eHgo4sz9VSIwAqPevya8wjhwlXGNPn8qo5vXaoB/g=="
VENDOR_KEY = VENDOR_SRI + "\tfile:" + VENDOR_PATH
MAX_TARBALL = 96 * MIB
MIN_BYTES = 5 * 1024**3
MIN_INODES = 350_000
REQUEST_LIMIT = 12_000
STAGE_SECONDS = 900
POLICY = 3478
REGISTRY = "https://registry.npmjs.org/"
CACHE = "cache/v11/metadata-full/https%3A+registry.npmjs.org/"
NAME = re.compile(r"(?:@[a-z0-9_~.-]+/)?[A-Za-z0-9_~.-]+\Z")
VERSION = re.compile(r"[0-9][0-9A-Za-z.+-]*\Z")


def tar_route(name, version):
    need(
        NAME.fullmatch(name)
        and all(part not in {".", ".."} for part in name.split("/"))
        and VERSION.fullmatch(version),
        "IDENTIDAD",
    )
    return f"/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"


def artifact_rows(index, manifest, v5):
    need(
        index["candidate_lock_sha256"] == LOCK
        and index["schema"] == 2
        and index["scope"] == "server-reference-offline-trial"
        and index["wasm_replacements_pending"] is True
        and index["compiled"] is index["production_approved"] is False,
        "INDICE",
    )
    routes = {}
    for kind, count, inventory in (("external_v5", 1449, v5), ("contained", 1693, manifest)):
        need(len(index[kind]) == count, "CANTIDAD_TARBALLS")
        for identity, row in index[kind].items():
            name, version = identity.rsplit("@", 1)
            route = tar_route(name, version)
            leaf = f"tarballs/{name}/{version}.tgz"
            node = inventory[leaf]
            need(
                row["url"] == REGISTRY.rstrip("/") + route
                and row["path"] == leaf
                and 0 < row["bytes"] == node["bytes"] <= MAX_TARBALL
                and row["sha256"] == node["sha256"]
                and re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", row["integrity"])
                and route not in routes,
                "TARBALL_INDICE",
            )
            routes[route] = (kind, identity, row)
    return routes


@contextmanager
def open_artifact(path, row):
    need(path.resolve(strict=True) == path, "TARBALL_ENLACE")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        node = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(node.st_mode)
            and stat.S_IMODE(node.st_mode) == 0o444
            and node.st_uid == node.st_gid == 0
            and node.st_nlink == 1
            and node.st_size == row["bytes"]
            and os.fstatvfs(stream.fileno()).f_flag & os.ST_RDONLY,
            "TARBALL_FISICO_RO",
        )
        sha256, sha512, count = hashlib.sha256(), hashlib.sha512(), 0
        while chunk := stream.read(MIB):
            count += len(chunk)
            need(count <= row["bytes"], "TARBALL_TAMANO")
            sha256.update(chunk)
            sha512.update(chunk)
        need(
            count == row["bytes"]
            and sha256.hexdigest() == row["sha256"]
            and "sha512-" + base64.b64encode(sha512.digest()).decode() == row["integrity"],
            "TARBALL_SHA_SRI",
        )
        stream.seek(0)
        yield stream


class Inputs:
    def __init__(self, op, delta, v5):
        self.op, self.delta, self.v5 = op, delta, v5
        self.manifest = json.loads(op.read(delta / "transfer-manifest.json", MANIFEST, MIB))
        old = json.loads(op.read(v5 / "transfer-manifest.json", op.MANIFEST, 2 * MIB))
        index = json.loads(self.read("artifact-index.json", INDEX, 2 * MIB))
        need(index["external_v5_manifest_sha256"] == op.MANIFEST, "INDICE_V5")
        self.routes = artifact_rows(index, self.manifest, old)
        self.lock = self.read("source/pnpm-lock.yaml", LOCK, 2 * MIB)
        self.context = self.read("source/server-lock-resolver.tar.gz", CONTEXT, 2 * MIB)
        self.binary = op.read(v5 / "tool/pnpm", op.PNPM, 64 * MIB)
        need(self.binary.startswith(b"\x7fELF"), "PNPM_ELF")
        compact = json.loads(self.read("evidence/compact.json", COMPACT, 2 * MIB))
        rows = compact["rows"]
        self.metadata = {row["name"]: row for row in rows}
        need(
            len(rows) == len(self.metadata) == compact["coverage"] == 2841
            and sum(len(row["versions"]) for row in rows) == compact["versions"] == 3501,
            "PACKUMENT_COBERTURA",
        )

    def read(self, name, digest, limit):
        row = self.manifest[name]
        need(row["sha256"] == digest, "MANIFIESTO_PIN")
        raw = self.op.read(self.delta / name, digest, limit)
        need(len(raw) == row["bytes"], "MANIFIESTO_BYTES")
        return raw

    def artifact(self, route):
        kind, _, row = self.routes[route]
        return open_artifact((self.v5 if kind == "external_v5" else self.delta) / row["path"], row)

    def metadata_raw(self, name):
        row = self.metadata[name]
        return self.read(CACHE + name + ".jsonl", row["mirror_sha256"], 8 * MIB)

    def project(self, name, origin):
        original = json.loads(self.metadata_raw(name).split(b"\n", 1)[1])
        need(original["name"] == name, "PACKUMENT_NOMBRE")
        versions = {}
        for version in self.metadata[name]["versions"]:
            value = original["versions"][version]
            route = tar_route(name, version)
            dist = value["dist"]
            need(
                dist["tarball"] == REGISTRY.rstrip("/") + route
                and version in original["time"]
                and (
                    route not in self.routes
                    or dist["integrity"] == self.routes[route][2]["integrity"]
                ),
                "PACKUMENT_DIST",
            )
            versions[version] = {**value, "dist": {**dist, "tarball": origin.rstrip("/") + route}}
        return self.op.encoded(
            {
                "name": name,
                "versions": versions,
                "time": original["time"],
                "dist-tags": {
                    tag: version
                    for tag, version in original["dist-tags"].items()
                    if version in versions
                },
            }
        )


class Registry(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, inputs):
        super().__init__(("127.0.0.1", 0), Request)
        self.inputs = inputs
        self.host = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.host}/"
        self.slots = threading.BoundedSemaphore(8)
        self.lock = threading.Lock()
        self.total = self.rejected = self.failed = self.disconnects = 0
        self.served = set()

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            with self.lock:
                self.rejected += 1
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def request_path(target, headers, host):
    parsed = urlsplit(target)
    need(
        len(target) <= 512
        and target.startswith("/")
        and not target.startswith("//")
        and not any((parsed.scheme, parsed.netloc, parsed.query, parsed.fragment))
        and headers.get_all("Host") == [host]
        and all(
            header not in headers
            for header in ("Authorization", "Cookie", "Transfer-Encoding", "Content-Length")
        ),
        "HTTP_RUTA",
    )
    return unquote(parsed.path, errors="strict")


class Request(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, *_):
        pass

    def send_error(self, code, *_args, **_kwargs):
        with self.server.lock:
            self.server.rejected += 1
        self.respond(code, "text/plain", 0)

    def respond(self, code, mime, size):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self):
        server = self.server
        with server.lock:
            server.total += 1
            accepted = server.total <= REQUEST_LIMIT
        try:
            try:
                path = request_path(self.path, self.headers, server.host)
            except ValueError:
                self.send_error(400)
                return
            if not accepted:
                self.send_error(503)
            elif path in server.inputs.routes:
                with server.inputs.artifact(path) as stream:
                    self.respond(
                        200, "application/octet-stream", server.inputs.routes[path][2]["bytes"]
                    )
                    shutil.copyfileobj(stream, self.wfile, 64 * 1024)
                with server.lock:
                    server.served.add(path)
            elif path.removeprefix("/") in server.inputs.metadata:
                raw = server.inputs.project(path[1:], server.origin)
                self.respond(200, "application/json", len(raw))
                self.wfile.write(raw)
            else:
                self.send_error(404)
        except (ConnectionError, TimeoutError):
            with server.lock:
                server.disconnects += 1
        except (OSError, ValueError, KeyError):
            with server.lock:
                server.failed += 1
            self.close_connection = True


def workspace(op, inputs, dest):
    files = {}
    with tarfile.open(fileobj=io.BytesIO(inputs.context), mode="r:gz") as archive:
        for row in archive:
            path = PurePosixPath(row.name)
            need(
                len(files) < 112
                and row.isfile()
                and row.mode in (0o644, 0o755)
                and str(path) == row.name
                and not path.is_absolute()
                and not any(part in {"..", ".git", ".env", "node_modules"} for part in path.parts)
                and "\\" not in row.name
                and row.name not in files
                and row.size <= 2 * MIB,
                "WORKSPACE_TAR",
            )
            with archive.extractfile(row) as stream:
                files[row.name] = stream.read()
    listing = dict(
        (name, digest)
        for digest, name in (
            line.split("  ", 1) for line in files.pop("SHA256SUMS").decode().splitlines()
        )
    )
    need(
        len(files) == 111 and listing == {name: op.sha(raw) for name, raw in files.items()},
        "WORKSPACE_SHA",
    )
    need(op.sha(files[VENDOR_PATH]) == VENDOR, "WORKSPACE_VENDOR")
    files["pnpm-lock.yaml"] = inputs.lock
    for name, raw in files.items():
        op.write(dest / name, raw)


def command(tool, work, store, registry, offline):
    stage = ["--offline", "--config.frozen-store=true"] if offline else ["--config.offline=false"]
    return [
        str(tool),
        "--dir",
        str(work),
        "install",
        "--frozen-lockfile",
        *stage,
        "--ignore-pnpmfile",
        "--ignore-scripts",
        "--config.manage-package-manager-versions=false",
        "--config.git-branch-lockfile=false",
        "--config.fetch-retries=0",
        "--config.fetch-timeout=30000",
        "--config.network-concurrency=6",
        "--config.verify-store-integrity=true",
        "--config.package-import-method=hardlink",
        "--registry",
        registry,
        "--store-dir",
        str(store),
        "--reporter=append-only",
    ]


def parse_log(raw, offline):
    policy = re.findall(
        rb"(?m)^\xe2\x9c\x93 Lockfile passes supply-chain policies \((\d+) entries in [^\r\n]+\)\r?$",
        raw,
    )
    progress = re.findall(
        rb"(?m)^Progress: resolved (\d+), reused (\d+), downloaded (\d+), added (\d+), done\r?$",
        raw,
    )
    need(policy == [str(POLICY).encode()] and len(progress) == 1, "PNPM_EVIDENCIA")
    resolved, reused, downloaded, added = map(int, progress[0])
    need(
        0 < added <= resolved <= 3600 and (downloaded == 0 if offline else downloaded > 0),
        "PNPM_PROGRESO",
    )
    return {
        "policy_entries": POLICY,
        "resolved": resolved,
        "reused": reused,
        "downloaded": downloaded,
        "added": added,
    }


def store_keys(op, store):
    db = store / "v11/index.db"
    need(
        db.resolve(strict=True) == db
        and not any(
            db.with_name("index.db" + ext).exists() for ext in ("-wal", "-shm", "-journal")
        ),
        "STORE_INDICE",
    )
    node = db.lstat()
    need(
        stat.S_ISREG(node.st_mode) and node.st_nlink == 1 and 0 < node.st_size <= 128 * MIB,
        "STORE_TAMANO",
    )
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)) as con:
        need(con.execute("PRAGMA quick_check").fetchall() == [("ok",)], "STORE_SQLITE")
        rows = con.execute("SELECT key FROM package_index ORDER BY key LIMIT 3200").fetchall()
    return {row[0] for row in rows}, {"sha256": op.sha(db.read_bytes()), "bytes": node.st_size}


def phase(op, inputs, run, tool, tag, registry, offline):
    work = run / (tag + "-workspace")
    workspace(op, inputs, work)
    env = op.environment(run, tag)
    if offline:
        cache = Path(env["XDG_CACHE_HOME"]) / "pnpm" / CACHE.removeprefix("cache/")
        for name in inputs.metadata:
            op.write(cache / (name + ".jsonl"), inputs.metadata_raw(name))
    log = run / (tag + ".log")
    result = op.execute(
        command(tool, work, run / "store", registry, offline), work, env, log, STAGE_SECONDS
    )
    result["lock_unchanged"] = op.sha((work / "pnpm-lock.yaml").read_bytes()) == LOCK
    op.write(run / (tag + "-process.json"), op.encoded(result))
    need(
        result["status"] == "completed" and result["exit_code"] == 0 and result["lock_unchanged"],
        "PNPM_FALLO",
    )
    return result | parse_log(log.read_bytes(), offline)


def seed(op, inputs, parent):
    need(
        platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and os.geteuid() == os.getegid() == 1000
        and Path("/lib/ld-musl-x86_64.so.1").is_file()
        and {name for _, name in socket.if_nameindex()} == {"lo"},
        "LINUX_MUSL_RED_AISLADA",
    )
    info = parent.lstat()
    need(
        parent.resolve(strict=True) == parent
        and stat.S_ISDIR(info.st_mode)
        and info.st_uid == info.st_gid == 1000
        and stat.S_IMODE(info.st_mode) == 0o700,
        "WORK_PRIVADO",
    )
    need(
        all(
            path.resolve(strict=True) == path and os.statvfs(path).f_flag & os.ST_RDONLY
            for path in (inputs.delta, inputs.v5)
        ),
        "ENTRADAS_RO",
    )
    storage(parent, MIN_BYTES, MIN_INODES)
    os.umask(0o077)
    run = parent / "baseline-seed-v1"
    run.mkdir(mode=0o700)
    info = run.lstat()
    owner = {"owner": OWNER, "device": info.st_dev, "inode": info.st_ino}
    op.write(run / ".agency-owner", op.encoded(owner))
    try:
        for route in inputs.routes:
            with inputs.artifact(route):
                pass
        tool = run / "pnpm"
        op.write(tool, inputs.binary)
        tool.chmod(0o700)
        version_log = run / "version.log"
        version = op.execute(
            [str(tool), "--version"], run, op.environment(run, "version"), version_log, 10
        )
        need(
            version["status"] == "completed"
            and version["exit_code"] == 0
            and version_log.read_bytes().strip() == b"12.4.2",
            "PNPM_VERSION",
        )
        server = Registry(inputs)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            first = phase(op, inputs, run, tool, "seed", server.origin, False)
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
            op.write(
                run / "loopback.json",
                op.encoded(
                    {
                        "requests": server.total,
                        "rejected": server.rejected,
                        "failed": server.failed,
                        "disconnects": server.disconnects,
                        "served": sorted(server.served),
                    }
                ),
            )
        need(not thread.is_alive() and server.rejected == server.failed == 0, "LOOPBACK_FALLO")
        expected = {
            row["integrity"] + "\t" + identity: route
            for route, (_, identity, row) in inputs.routes.items()
        }
        before, index_before = store_keys(op, run / "store")
        registry_keys = before - {VENDOR_KEY}
        need(
            VENDOR_KEY in before
            and registry_keys
            and registry_keys <= expected.keys()
            and {expected[key] for key in registry_keys} <= server.served,
            "STORE_COBERTURA",
        )
        offline = phase(op, inputs, run, tool, "offline", REGISTRY, True)
        after, index_after = store_keys(op, run / "store")
        need(after == before and index_after == index_before, "STORE_CAMBIO_OFFLINE")
        need((run / "offline-workspace/node_modules/.pnpm").is_dir(), "INSTALL_VACIO")
        op.write(
            run / "coverage.json",
            op.encoded(
                {
                    "absent_index": sorted(expected.keys() - registry_keys),
                    "not_requested": sorted(inputs.routes.keys() - server.served),
                }
            ),
        )
        result = {
            **owner,
            "scope": "server-reference-offline-trial",
            "lock_sha256": LOCK,
            "external_v5_verified": 1449,
            "delta_verified": 1693,
            "store_rows": len(before),
            "local_wa_sqlite_cached": True,
            "index": index_after,
            "seed": first,
            "offline": offline,
            "scripts_executed": False,
            "compiled": False,
            "wasm_replacements_pending": True,
            "production_approved": False,
        }
        op.write(run / "receipt.json", op.encoded(result))
        return result
    except Exception as error:
        op.write(run / "failure.json", op.encoded({**owner, "type": type(error).__name__}))
        raise


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--seed", action="store_true")
    cli.add_argument("--loopback-helper", type=Path)
    args = cli.parse_args()
    if args.seed:
        if args.loopback_helper is None:
            cli.error("--seed requiere --loopback-helper.")
        op = load_helper(args.loopback_helper, LOOPBACK)
        result = seed(op, Inputs(op, Path("/inputs"), Path("/v5")), Path("/work"))
    else:
        result = {
            "lock_sha256": LOCK,
            "index_sha256": INDEX,
            "seeded": False,
            "min_bytes": MIN_BYTES,
            "min_inodes": MIN_INODES,
        }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
