"""Carga, reinstala y construye SupoClip con el SDK Linux fijado."""

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

INPUTS = Path("/inputs")
WORK = Path("/work")
NODE = Path("/usr/bin/node")
UID = 1000
LOCK = "d3d98cbe5f080c30a77cc16e7f6e128b2d3d52cb08a9ffa609f80b7ffb8bc2fe"
SOURCE = "9a59806c0b1211f33c9f1debb714aaae8ec974f7d3401e5adf3620afe30e3c9c"
SOURCE_RECEIPT = "171a128655d160fa9c56f700f629813de293c578adff5d6e4bc2abc8752d7ccc"
NPM_RECEIPT = "b30eed332c6d0cec35fc409a56b60f725c2710e92d3135ecc528b2a4f4e679d0"
PNPM = "d3c7c3d12d87d177e32f01748d5994fb20cd5becf11670ee60530c374ffeabb3"
ENGINE = "5d42b181631fd20bb0ecc5abcdba72575e7f467a0d52f4d5ef1ff28f0c74e6e9"
ENGINE_REV = "c2990dca591cba766e3b7ef5d9e8a84796e47ab7"
MAX_ARCHIVE = 256 * 1024**2
MAX_FILE = 96 * 1024**2
MAX_LOG = 8 * 1024**2
SEED = WORK / "frontend-seed-v2"
REPLAY = WORK / "frontend-replay-v3"
BUILD = WORK / "frontend-build-v3"
BUILD_REPAIR = WORK / "private/frontend-build-repair-v1"
CHANGELOG_PIN = "97f07d61268bff8c534a78a2b67cb92b6adaac0d240a03b4178f464aba782ad7"
ESLINT_ORIGINAL = "25b74e440ef41b3c74998ab2fed9e81089681ce0f1f124104787408487bb686b"
ESLINT_REPAIR = "bd19f745a0b4ce4d88bbacb52eb39e8e2be8838326ace325c0c83d48ab06b00d"
DB_FIXTURE = "postgresql://supoclip_build@127.0.0.1:1/supoclip_build"
AUTH_FIXTURE = "supoclip-build-only-placeholder-for-static-compilation"
WORKSPACE = b"""supportedArchitectures:
  os: [linux]
  cpu: [x64]
  libc: [glibc]
fetchRetries: 0
fetchTimeout: 60000
networkConcurrency: 4
verifyStoreIntegrity: true
virtualStoreDirMaxLength: 45
"""


def require(value, code):
    if not value:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(name):
    path = PurePosixPath(name)
    return (
        path.as_posix() == name
        and not path.is_absolute()
        and path.parts
        and not {"..", "."}.intersection(path.parts)
        and not any(char in name for char in ("\\", ":", "\x00"))
    )


def public_origin(value):
    require(
        isinstance(value, str) and re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", value),
        "ORIGEN_PUBLICO",
    )
    parsed = urlsplit(value)
    require(parsed.hostname and (parsed.port is None or parsed.port > 0), "ORIGEN_PUBLICO")
    return value


def tarball_route(key):
    match = re.fullmatch(
        r"((?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+)@([0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9_.+-]+)?)",
        key,
    )
    require(match is not None, "IDENTIDAD_NPM")
    name, version = match.groups()
    require(safe_path(name), "RUTA_NPM")
    return f"/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"


def regular(path, maximum):
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 <= info.st_size <= maximum,
        "ARCHIVO_REGULAR",
    )
    return info


def read(path, maximum, pin=None):
    info = regular(path, maximum)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) == info.st_size and (pin is None or sha(raw) == pin), "ARCHIVO_SHA")
    return raw


def put(path, raw, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(mode)


def save(path, data):
    put(path, (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode())


def extract(path, pin, members, destination, *, select=None, prefix=""):
    regular(path, MAX_ARCHIVE)
    with path.open("rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == pin, "TAR_SHA")
        stream.seek(0)
        seen = set()
        with tarfile.open(fileobj=stream, mode="r:*") as archive:
            for item in archive:
                name = item.name
                require(
                    item.isfile()
                    and name not in seen
                    and safe_path(name)
                    and name.startswith(prefix)
                    and 0 <= item.size <= MAX_FILE,
                    "TAR_MIEMBRO",
                )
                if members is not None:
                    require(
                        name in members
                        and item.size == members[name]["bytes"]
                        and item.mode == 0o444,
                        "TAR_INVENTARIO",
                    )
                seen.add(name)
                if select is None or name in select:
                    body = archive.extractfile(item).read(item.size + 1)
                    require(
                        len(body) == item.size
                        and (members is None or sha(body) == members[name]["sha256"]),
                        "TAR_CONTENIDO",
                    )
                    put(destination / name.removeprefix(prefix), body)
        require(
            seen == set(members) if members is not None else len(seen) == 1068,
            "TAR_COBERTURA",
        )


def catalog():
    receipt = json.loads(read(INPUTS / "transport-receipt.json", 1024**2, NPM_RECEIPT))
    require(receipt["lock_sha256"] == LOCK and receipt["reachable_tarballs"] == 796, "NPM_CATALOGO")
    packages = {}
    for number, archive in enumerate(receipt["archives"], 1):
        require(archive["path"] == f"linux-npm-part-{number}.tar", "NPM_ARCHIVO")
        for path, row in archive["members"].items():
            key = row["identity"]
            name, version = key.rsplit("@", 1)
            require(
                path == f"tarballs/{name}/{version}.tgz" and key not in packages,
                "NPM_RUTA",
            )
            require(
                row["integrity"].startswith("sha512-")
                and len(base64.b64decode(row["integrity"][7:], validate=True)) == 64,
                "NPM_SRI",
            )
            packages[key] = {"path": path, "route": tarball_route(key), **row}
    require(len(receipt["archives"]) == 2 and len(packages) == 796, "NPM_COBERTURA")
    return receipt["archives"], packages


class Mirror(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, packages):
        super().__init__(("127.0.0.1", 0), Request)
        self.origin = f"127.0.0.1:{self.server_port}"
        self.routes = {row["route"]: row for row in packages.values()}
        self.served = set()
        self.rejected = 0
        self.total = 0
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(8)


class Request(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, *_):
        pass

    def do_GET(self):
        server = self.server
        with server.slots:
            parsed = urlsplit(self.path)
            row = server.routes.get(unquote(parsed.path, errors="strict"))
            with server.lock:
                valid = (
                    row is not None
                    and len(self.path) <= 512
                    and not any((parsed.netloc, parsed.scheme, parsed.query, parsed.fragment))
                    and self.headers.get_all("Host") == [server.origin]
                    and all(k not in self.headers for k in ("Authorization", "Cookie", "Range"))
                    and row["identity"] not in server.served
                    and server.total + row["bytes"] <= 330 * 1024**2
                )
                if valid:
                    server.served.add(row["identity"])
                    server.total += row["bytes"]
                else:
                    server.rejected += 1
            if not valid:
                self.send_error(404)
                return
            raw = read(SEED / row["path"], row["bytes"], row["sha256"])
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(raw)


def command(argv, cwd, env, log, timeout):
    with log.open("xb") as stream:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        try:
            while True:
                left = deadline - time.monotonic()
                require(left > 0, "COMANDO_PLAZO")
                try:
                    status = process.wait(timeout=min(1, left))
                    break
                except subprocess.TimeoutExpired:
                    require(os.fstat(stream.fileno()).st_size <= MAX_LOG, "COMANDO_LOG_LIMITE")
        except BaseException:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
            raise
    require(status == 0 and log.stat().st_size <= MAX_LOG, "COMANDO_FALLO_" + log.stem.upper())
    return read(log, MAX_LOG)


def environment(stage, registry, *, offline):
    home = stage / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    state = stage / "cache"
    state.mkdir(mode=0o700, exist_ok=True)
    config = state / "empty.npmrc"
    if not config.exists():
        put(config, b"")
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(state),
        "XDG_CACHE_HOME": str(state),
        "XDG_DATA_HOME": str(state),
        "TMPDIR": str(state),
        "PNPM_HOME": str(state),
        "npm_config_registry": registry,
        "npm_config_userconfig": str(config),
        "npm_config_globalconfig": str(config),
        "npm_config_ignore_scripts": "true",
        "npm_config_ignore_pnpmfile": "true",
        "npm_config_manage_package_manager_versions": "false",
        "npm_config_offline": str(offline).lower(),
        "npm_config_strict_ssl": "true",
        "CHECKPOINT_DISABLE": "1",
        "NEXT_TELEMETRY_DISABLED": "1",
        "PRISMA_GENERATE_SKIP_AUTOINSTALL": "1",
    }


def source(stage, *, lock_only=False):
    data = json.loads(read(INPUTS / "frontend-source-receipt.json", 1024**2, SOURCE_RECEIPT))
    require(data["lock_sha256"] == LOCK and len(data["members"]) == 597, "FUENTE_RECIBO")
    extract(
        INPUTS / "frontend-source.tar",
        SOURCE,
        data["members"],
        stage,
        select={"frontend/pnpm-lock.yaml", "frontend/package.json"} if lock_only else None,
    )
    frontend = stage / "frontend"
    read(frontend / "pnpm-lock.yaml", 1024**2, LOCK)
    put(frontend / "pnpm-workspace.yaml", WORKSPACE)
    return frontend


def verify_sources(frontend):
    data = json.loads(read(INPUTS / "frontend-source-receipt.json", 1024**2, SOURCE_RECEIPT))
    expected = {
        name.removeprefix("frontend/"): pin
        for name, pin in data["members"].items()
        if name.startswith("frontend/")
    }
    observed = set()
    for parent, dirs, files in os.walk(frontend, followlinks=False):
        path = Path(parent)
        if path == frontend:
            require(
                "node_modules" in dirs and not (path / "node_modules").is_symlink(),
                "FUENTE_NODE_MODULES",
            )
            dirs.remove("node_modules")
            require("pnpm-workspace.yaml" in files, "FUENTE_WORKSPACE")
            files.remove("pnpm-workspace.yaml")
        for dirname in dirs:
            require(not (path / dirname).is_symlink(), "FUENTE_ENLACE")
        for name in files:
            child = path / name
            relative = child.relative_to(frontend).as_posix()
            require(relative in expected, "FUENTE_RUTA")
            pin = expected[relative]
            body = read(child, pin["bytes"], pin["sha256"])
            require(len(body) == pin["bytes"], "FUENTE_TAMANO")
            observed.add(relative)
    require(observed == set(expected), "FUENTE_COBERTURA")
    require(read(frontend / "pnpm-workspace.yaml", 4096) == WORKSPACE, "FUENTE_WORKSPACE")


def pnpm(*args):
    return [
        NODE,
        SEED / "pnpm/bin/pnpm.cjs",
        *args,
        "--ignore-scripts",
        "--ignore-pnpmfile",
        "--store-dir",
        SEED / "store",
        "--reporter=append-only",
    ]


def store_indexes(packages):
    rows = {}
    root = SEED / "store/v10/index"
    for path in root.glob("*/*.json"):
        raw = read(path, 8 * 1024**2)
        metadata = json.loads(raw)
        identity = metadata["name"] + "@" + metadata["version"]
        require(identity in packages and identity not in rows, "STORE_IDENTIDAD")
        sri = base64.b64decode(packages[identity]["integrity"][7:]).hex()
        require(
            path.relative_to(root).as_posix()
            == f"{sri[:2]}/{sri[2:64]}-{identity.replace('/', '+')}.json",
            "STORE_INDICE",
        )
        rows[identity] = sha(raw)
    require(set(rows) == set(packages), "STORE_COBERTURA")
    return rows


def seed(archives, packages):
    SEED.mkdir(mode=0o700)
    for item in archives:
        extract(INPUTS / item["path"], item["sha256"], item["members"], SEED)
    extract(INPUTS / "pnpm-10.27.0.tgz", PNPM, None, SEED / "pnpm", prefix="package/")
    frontend = source(SEED, lock_only=True)
    with Mirror(packages) as mirror:
        thread = threading.Thread(target=mirror.serve_forever)
        thread.start()
        try:
            env = environment(SEED, f"http://{mirror.origin}/", offline=False)
            version = command(
                [NODE, SEED / "pnpm/bin/pnpm.cjs", "--version"],
                frontend,
                env,
                SEED / "pnpm-version.log",
                15,
            )
            require(version.strip() == b"10.27.0", "PNPM_VERSION")
            command(pnpm("fetch"), frontend, env, SEED / "fetch.log", 420)
        finally:
            mirror.shutdown()
            thread.join()
            save(
                SEED / "mirror.json",
                {
                    "served": sorted(mirror.served),
                    "rejected": mirror.rejected,
                    "bytes": mirror.total,
                },
            )
        require(mirror.served == set(packages) and mirror.rejected == 0, "MIRROR_COBERTURA")
    read(frontend / "pnpm-lock.yaml", 1024**2, LOCK)
    save(SEED / "seed.json", {"success": True, "lock": LOCK, "indexes": store_indexes(packages)})


def replay(packages):
    result = json.loads(read(SEED / "seed.json", 256 * 1024))
    require(result["success"] is True and result["lock"] == LOCK, "SEMILLA_RECIBO")
    require(result["indexes"] == store_indexes(packages), "STORE_CAMBIO")
    REPLAY.mkdir(mode=0o700)
    frontend = source(REPLAY)
    env = environment(REPLAY, "https://registry.npmjs.org/", offline=True)
    command(
        pnpm("install", "--offline", "--frozen-lockfile", "--package-import-method=copy"),
        frontend,
        env,
        REPLAY / "install.log",
        420,
    )
    read(frontend / "pnpm-lock.yaml", 1024**2, LOCK)
    require(result["indexes"] == store_indexes(packages), "STORE_CAMBIO")
    verify_sources(frontend)
    save(REPLAY / "replay.json", {"success": True, "lock": LOCK, "packages": len(packages)})


def output_files(root):
    """Valida symlinks internos y busca los valores sintéticos de build por bloques."""
    pins, links = {}, {}
    root = root.resolve(strict=True)
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                require(
                    not os.path.isabs(target) and path.resolve(strict=True).is_relative_to(root),
                    "STANDALONE_ENLACE",
                )
                links[relative] = target
                continue
            if path.is_dir():
                continue
            require(not name.startswith(".env"), "STANDALONE_ENTORNO")
            info = path.lstat()
            require(
                stat.S_ISREG(info.st_mode) and info.st_size <= 256 * 1024**2, "STANDALONE_ARCHIVO"
            )
            digest, tail = hashlib.sha256(), b""
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    data = tail + chunk
                    require(
                        not any(marker.encode() in data for marker in (DB_FIXTURE, AUTH_FIXTURE)),
                        "STANDALONE_VALOR_DE_BUILD",
                    )
                    tail = data[-128:]
                    digest.update(chunk)
            pins[relative] = {"bytes": info.st_size, "sha256": digest.hexdigest()}
    return {"files": pins, "symlinks": links}


def build(app_origin, api_origin):
    result = json.loads(read(REPLAY / "replay.json", 4096))
    require(result == {"success": True, "lock": LOCK, "packages": 796}, "REPLAY_RECIBO")
    frontend = REPLAY / "frontend"
    read(frontend / "pnpm-lock.yaml", 1024**2, LOCK)
    verify_sources(frontend)
    BUILD.mkdir(mode=0o700)
    changelog = read(BUILD_REPAIR / "CHANGELOG.md", 1024**2, CHANGELOG_PIN)
    put(frontend / "CHANGELOG.md", changelog)
    eslint = frontend / "eslint.config.mjs"
    read(eslint, 4096, ESLINT_ORIGINAL)
    put(BUILD / "eslint.config.original.mjs", eslint.read_bytes())
    eslint_candidate = frontend / "eslint.config.pending.mjs"
    put(eslint_candidate, read(BUILD_REPAIR / "eslint.config.mjs", 4096, ESLINT_REPAIR))
    eslint_candidate.replace(eslint)
    engine = BUILD / "schema-engine"
    put(engine, read(INPUTS / "schema-engine", 24 * 1024**2, ENGINE), 0o500)
    env = environment(BUILD, "https://registry.npmjs.org/", offline=True)
    env.update(
        NODE_OPTIONS="--max-old-space-size=4096",
        PRISMA_SCHEMA_ENGINE_BINARY=str(engine),
        DATABASE_URL=DB_FIXTURE,
        BETTER_AUTH_SECRET=AUTH_FIXTURE,
        BETTER_AUTH_URL=app_origin,
        NEXT_PUBLIC_APP_URL=app_origin,
        NEXT_PUBLIC_API_URL=api_origin,
        NEXT_PUBLIC_SELF_HOST="true",
        BACKEND_INTERNAL_URL="http://127.0.0.1:1",
    )
    version = command([engine, "--version"], frontend, env, BUILD / "engine.log", 15)
    require(ENGINE_REV.encode() in version, "PRISMA_MOTOR_VERSION")
    modules = frontend / "node_modules"
    command(
        [NODE, modules / "prisma/build/index.js", "generate", "--schema", "prisma/schema.prisma"],
        frontend,
        env,
        BUILD / "generate.log",
        120,
    )
    generated = frontend / "src/generated/prisma"
    require(
        (generated / "index.js").is_file() and any(generated.rglob("*.wasm")), "PRISMA_GENERADO"
    )
    command(
        [NODE, modules / "typescript/bin/tsc", "--noEmit", "--incremental", "false"],
        frontend,
        env,
        BUILD / "typecheck.log",
        240,
    )
    next_log = command(
        [NODE, modules / "next/dist/bin/next", "build"], frontend, env, BUILD / "next.log", 900
    )
    require(b"ESLint:" not in next_log, "NEXT_ESLINT")
    standalone = frontend / ".next/standalone"
    require((standalone / "server.js").is_file(), "NEXT_STANDALONE")
    shutil.copytree(frontend / ".next/static", standalone / ".next/static", symlinks=True)
    shutil.copytree(frontend / "public", standalone / "public", symlinks=True)
    if (standalone / "CHANGELOG.md").exists():
        read(standalone / "CHANGELOG.md", 1024**2, CHANGELOG_PIN)
    else:
        put(standalone / "CHANGELOG.md", changelog)
    save(
        BUILD / "build.json",
        {
            "success": True,
            "lock": LOCK,
            "standalone": str(standalone),
            "public_app_origin": app_origin,
            "public_api_origin": api_origin,
            **output_files(standalone),
        },
    )


def preflight():
    require(
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and os.geteuid() == os.getegid() == UID,
        "SDK_LINUX_UID",
    )
    require(
        INPUTS.resolve(strict=True) == INPUTS
        and bool(os.statvfs(INPUTS).f_flag & os.ST_RDONLY)
        and WORK.resolve(strict=True) == WORK,
        "SDK_MONTAJES",
    )
    mode = WORK.lstat()
    require(mode.st_uid == mode.st_gid == UID and stat.S_IMODE(mode.st_mode) == 0o700, "SDK_WORK")
    require(not any(Path(path).exists() for path in ("/usr/bin/git", "/bin/git")), "SDK_VCS")
    require(set(os.listdir("/sys/class/net")) == {"lo"}, "SDK_RED")
    os.umask(0o077)
    result = subprocess.run(
        [NODE, "--version"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=True,
        timeout=10,
    )
    require(result.stdout.strip() == b"v24.22.2", "NODE_VERSION")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("seed", "replay", "build"))
    parser.add_argument("--public-app-origin")
    parser.add_argument("--public-api-origin")
    args = parser.parse_args()
    preflight()
    if args.stage == "build":
        build(public_origin(args.public_app_origin), public_origin(args.public_api_origin))
    else:
        require(
            args.public_app_origin is None and args.public_api_origin is None, "ARGUMENTOS_ETAPA"
        )
        archives, packages = catalog()
        if args.stage == "seed":
            seed(archives, packages)
        else:
            replay(packages)
    print(json.dumps({"success": True, "stage": args.stage, "lock": LOCK}))


if __name__ == "__main__":
    main()
