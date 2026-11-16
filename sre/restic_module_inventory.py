"""Inventario y cierre conservador .mod/.info. No ejecuta Go, MVS, SumDB ni ZIP."""

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path, PurePosixPath

import httpx
from go_module_semver import compare, escape, validate_module, validate_path
from install_backup_tools import read_regular

OWNER = "agencia-digital-marketing-ai:restic-modules:v1"
MARKER = ".agency-restic-modules-owner.json"
UPGRADES = {
    "github.com/klauspost/compress": "v1.18.7",
    "golang.org/x/crypto": "v0.55.0",
    "golang.org/x/net": "v0.60.0",
    "golang.org/x/text": "v0.41.0",
    "google.golang.org/grpc": "v1.83.2",
}
SOURCE_PINS = {
    "archive": "bb9b1a19040744d26d8a79be029d4e6b189c45ccc9d8831d7fe367d3c33df725",
    "go.mod": "d166913ed4897cf69069ef5be090b2d79a857fc28328bb950e12655b5a54f176",
    "go.sum": "e2d2bab6fc96eeaf53d9460af8b43e25d4c5c5de6c1aa6c1306550e6b62b26bc",
}
PROJECT = Path(__file__).resolve().parents[1]


def decode_json(data):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("JSON ambiguo.")
            out[key] = value
        return out

    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


def tokens(line):
    quoted = False
    end = len(line)
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        if not quoted and line[index : index + 2] == "//":
            end = index
            break
    line = line[:end].strip()
    if not line:
        return [], False
    result = []
    while line:
        if line.startswith('"'):
            match = re.match(r'"([^"\\]*)"', line)
            if not match:
                raise ValueError("String Go no canónico/no soportado.")
            result.append(match[1])
            line = line[match.end() :]
        else:
            match = re.match(r'[^\s"]+', line)
            if not match:
                raise ValueError("Token no soportado.")
            result.append(match[0])
            line = line[match.end() :]
        if line and not line[0].isspace():
            raise ValueError("Tokens sin separación.")
        line = line.lstrip()
    return result, False


def parse_mod(data, *, dependency=False):
    if not data or len(data) > 256 * 1024:
        raise ValueError("go.mod vacío o excesivo.")
    text = data.decode("utf-8")
    if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
        raise ValueError("Control no permitido en go.mod.")
    result = {
        "module": None,
        "go": None,
        "toolchain": None,
        "requires": [],
        "ignored_directives": [],
    }
    block = None
    seen = set()
    for original in text.splitlines():
        fields, _unused = tokens(original)
        if not fields:
            continue
        if fields == [")"]:
            if block is None:
                raise ValueError("Bloque no abierto.")
            block = None
            continue
        if block is None:
            directive, *arguments = fields
            if arguments == ["("]:
                if directive not in (
                    "require",
                    "replace",
                    "exclude",
                    "retract",
                    "tool",
                    "godebug",
                ) or (not dependency and directive in ("replace", "exclude")):
                    raise ValueError("Bloque no soportado en este módulo.")
                block = directive
                continue
        else:
            directive, arguments = block, fields
        if directive == "require":
            if len(arguments) != 2:
                raise ValueError("Requisito no canónico.")
            path, release = arguments
            validate_module(path, release)
            if path in seen:
                raise ValueError("Requisito duplicado.")
            seen.add(path)
            result["requires"].append(
                {
                    "module": path,
                    "version": release,
                    "indirect": bool(re.search(r"//\s*indirect\s*$", original)),
                }
            )
        elif directive == "module":
            if block or len(arguments) != 1 or result["module"] is not None:
                raise ValueError("Identidad module ambigua.")
            validate_path(arguments[0])
            result["module"] = arguments[0]
        elif directive in ("go", "toolchain"):
            if block or len(arguments) != 1 or result[directive] is not None:
                raise ValueError("Versión Go/toolchain ambigua.")
            result[directive] = arguments[0]
        elif directive in ("replace", "exclude", "retract", "tool", "godebug"):
            if not dependency and directive in ("replace", "exclude"):
                raise ValueError("El módulo principal no admite replace/exclude en esta unidad.")
            if directive not in result["ignored_directives"]:
                result["ignored_directives"].append(directive)
        else:
            raise ValueError("Directiva Go no soportada.")
    if block is not None or result["module"] is None:
        raise ValueError("go.mod incompleto.")
    return result


def parse_sum(data):
    if len(data) > 1024**2:
        raise ValueError("go.sum excesivo.")
    out = {}
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError("go.sum no canónico.")
        path, release, checksum = fields
        kind = "module"
        if release.endswith("/go.mod"):
            release = release[:-7]
            kind = "go.mod"
        validate_module(path, release)
        try:
            valid = (
                checksum.startswith("h1:")
                and len(base64.b64decode(checksum[3:], validate=True)) == 32
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Hash h1 inválido; no se verifica SumDB aquí.")
        key = (path, release, kind)
        if key in out and out[key] != checksum:
            raise ValueError("Checksums contradictorios.")
        out[key] = checksum
    return [
        {"module": p, "version": v, "kind": k, "declared_h1": h}
        for (p, v, k), h in sorted(out.items())
    ]


def parse_info(data, expected):
    if len(data) > 65536:
        raise ValueError("Info excesivo.")
    value = decode_json(data)
    if (
        not isinstance(value, dict)
        or value.get("Version") != expected
        or not isinstance(value.get("Time"), str)
    ):
        raise ValueError("Info no acredita la versión solicitada.")
    parsed = datetime.fromisoformat(value["Time"].replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Info debe incluir tiempo UTC.")
    return value


def make_inventory(mod, sum_data):
    parsed = parse_mod(mod)
    sums = parse_sum(sum_data)
    if parsed["module"] != "github.com/restic/restic":
        raise ValueError("La fuente no es restic.")
    present = {r["module"]: r for r in parsed["requires"]}
    if not set(UPGRADES) <= set(present):
        raise ValueError("Faltan los módulos de upgrade aprobados.")
    seeds = []
    for row in parsed["requires"]:
        candidate = UPGRADES.get(row["module"], row["version"])
        if compare(candidate, row["version"]) < 0:
            raise ValueError("No se permiten downgrades.")
        seeds.append({**row, "version": candidate})
    return {
        "source_module": parsed,
        "current_requirements": parsed["requires"],
        "proposed_requirements": seeds,
        "declared_checksums": sums,
        "upgrades": UPGRADES.copy(),
        "mvs_verified": False,
        "sumdb_verified": False,
        "sources_utf8_verified": False,
        "zip_downloaded": False,
        "security_clean": False,
        "sdk_executed": False,
    }


async def collect_metadata(
    seeds,
    client,
    write,
    *,
    cache_read=None,
    max_nodes=2048,
    max_seconds=180,
    max_bytes=16 * 1024**2,
):
    pending = deque()
    seen = set()
    nodes = []
    bytes_seen = 0
    cache_hits = 0
    http_requests = 0
    deadline = time.monotonic() + max_seconds
    semaphore = asyncio.Semaphore(4)

    def enqueue(path, release):
        validate_module(path, release)
        if (path, release) not in seen:
            if len(seen) >= max_nodes:
                raise ValueError("Cierre excede el máximo de identidades; no es completo.")
            seen.add((path, release))
            pending.append((path, release))

    for seed in seeds:
        enqueue(seed["module"], seed["version"])

    async def fetch(path, release, kind):
        nonlocal bytes_seen, cache_hits, http_requests
        relative = escape(path) + "/@v/" + escape(release) + "." + kind
        limit = 256 * 1024 if kind == "mod" else 65536
        if cache_read is not None:
            cached = cache_read(relative)
            if cached is not None:
                bytes_seen += len(cached)
                cache_hits += 1
                if len(cached) > limit or bytes_seen > max_bytes:
                    raise ValueError("Cache excede límites de metadatos.")
                return relative, cached
        async with semaphore:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Plazo de metadatos vencido.")
            http_requests += 1
            async with client.stream(
                "GET", "https://proxy.golang.org/" + relative, timeout=min(15, remaining)
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise ValueError("Respuesta de proxy rechazada.")
                length = response.headers.get("content-length")
                if length is not None and (
                    not re.fullmatch(r"[0-9]{1,9}", length) or int(length) > limit
                ):
                    raise ValueError("Longitud de metadatos rechazada.")
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=16384):
                    if time.monotonic() >= deadline:
                        raise ValueError("Plazo de metadatos vencido.")
                    body.extend(chunk)
                    bytes_seen += len(chunk)
                    if len(body) > limit or bytes_seen > max_bytes:
                        raise ValueError("Presupuesto de metadatos agotado.")
                if length is not None and len(body) != int(length):
                    raise ValueError("Metadatos truncados.")
        return relative, bytes(body)

    async def together(*coroutines):
        tasks = [asyncio.create_task(item) for item in coroutines]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def node(path, release):
        mod, info = await together(fetch(path, release, "mod"), fetch(path, release, "info"))
        try:
            parsed = parse_mod(mod[1], dependency=True)
            info_value = parse_info(info[1], release)
            if parsed["module"] != path:
                raise ValueError("go.mod declara otro módulo.")
        except ValueError as error:
            raise ValueError(path + "@" + release + ": " + str(error)) from None
        return path, release, parsed, info_value, mod, info

    async with asyncio.timeout(max_seconds):
        while pending:
            batch = [pending.popleft() for _ in range(min(4, len(pending)))]
            for path, release, parsed, info_value, mod, info in await together(
                *(node(*item) for item in batch)
            ):
                for relative, data in (mod, info):
                    write(relative, data)
                nodes.append(
                    {
                        "module": path,
                        "version": release,
                        "go": parsed["go"],
                        "requires": parsed["requires"],
                        "ignored_directives": parsed["ignored_directives"],
                        "info_time": info_value["Time"],
                        "files": [
                            {
                                "path": relative,
                                "size": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(),
                            }
                            for relative, data in (mod, info)
                        ],
                    }
                )
                for requirement in parsed["requires"]:
                    enqueue(requirement["module"], requirement["version"])
    return {
        "metadata_constraint_closure_complete": True,
        "nodes": nodes,
        "metadata_bytes": bytes_seen,
        "cache_hits": cache_hits,
        "http_requests": http_requests,
        "mvs_verified": False,
        "sumdb_verified": False,
        "sources_utf8_verified": False,
        "zip_downloaded": False,
        "security_clean": False,
        "sdk_executed": False,
    }


class MetadataStore:
    """El operador acredita ACL de raíz privada antes de usar; no crea la raíz."""

    def __init__(self, root, proxy):
        self.root, self.proxy = Path(root), Path(proxy)
        self.files = []
        self.known = {}
        if self.proxy != self.root / "proxy":
            raise ValueError("Proxy fuera de raíz propia.")
        for path in (*reversed(self.root.parents), self.root):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Ancestro enlazado o no ordinario.")
        info = self.root.stat()
        marker = decode_json(read_regular(self.root / MARKER, 8192))
        expected = {
            "schema_version": 1,
            "owner": OWNER,
            "path": str(self.root),
            "directory": {"device": info.st_dev, "inode": info.st_ino},
        }
        if marker != expected or not (self.root / "initialization-receipt.json").is_file():
            raise ValueError("Raíz no acreditada.")
        for receipt in sorted(self.root.glob("incomplete-*.json")):
            previous = decode_json(read_regular(receipt, 32 * 1024**2))
            if (
                previous.get("status") != "incomplete"
                or previous.get("source_sha256") != SOURCE_PINS
            ):
                raise ValueError("Recibo previo ajeno.")
            for row in previous.get("files", []):
                if (
                    set(row) != {"path", "size", "sha256"}
                    or type(row["size"]) is not int
                    or not 0 <= row["size"] <= 256 * 1024
                    or not re.fullmatch("[a-f0-9]{64}", row["sha256"])
                ):
                    raise ValueError("Ledger de cache inválido.")
                if row["path"] in self.known and self.known[row["path"]] != row:
                    raise ValueError("Ledger de cache contradictorio.")
                self.known[row["path"]] = row

    def get(self, relative):
        target = self.proxy.joinpath(*PurePosixPath(relative).parts)
        if relative not in self.known:
            if target.exists():
                raise ValueError("Cache existente sin ledger propio.")
            return None
        expected = self.known[relative]
        data = read_regular(target, expected["size"])
        if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise ValueError("Cache alterada; no se reutiliza.")
        return data

    def put(self, relative, data):
        parts = PurePosixPath(relative).parts
        if (
            not parts
            or PurePosixPath(relative).is_absolute()
            or ".." in parts
            or "\\" in relative
            or ":" in relative
        ):
            raise ValueError("Ruta de cache no permitida.")
        target = self.proxy.joinpath(*parts)
        for path in reversed((target.parent, *target.parent.parents)):
            if path == self.root or self.root not in path.parents:
                continue
            if path.exists():
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Cache enlazada o ajena.")
            else:
                path.mkdir(mode=0o700)
        if target.exists():
            if read_regular(target, len(data)) != data:
                raise ValueError("No se sobrescribe un metadato diferente.")
        else:
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        self.files.append(
            {"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )


def settings():
    spec = importlib.util.spec_from_file_location(
        "module_inventory_env", PROJECT / "scripts/ci/check_env.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values, errors = module.parse_env((PROJECT / ".env").read_text(encoding="utf-8-sig"))
    if errors:
        raise ValueError("Bóveda literal rechazada.")
    build = Path(values["BACKUP_BUILD_ROOT"])
    root = Path(values["BACKUP_RESTIC_MODULE_WORK_ROOT"])
    if (
        root != build / "restic-modules-v1"
        or Path(values["BACKUP_RESTIC_MODULE_PROXY_ROOT"]) != root / "proxy"
        or Path(values["BACKUP_RESTIC_MODULE_INVENTORY_PATH"]) != root / "inventory.json"
        or values["BACKUP_RESTIC_MODULE_OWNER_MARKER"] != OWNER
    ):
        raise ValueError("Rutas de módulos fuera del contrato respaldado.")
    source = Path(values["BACKUP_RESTIC_SOURCE_ROOT"])
    archive = read_regular(Path(values["BACKUP_RESTIC_SOURCE_ARCHIVE_PATH"]), 24166058)
    if (
        len(archive) != 24166058
        or hashlib.sha256(archive).hexdigest() != SOURCE_PINS["archive"]
        or values["BACKUP_RESTIC_SOURCE_ARCHIVE_SHA256"] != SOURCE_PINS["archive"]
    ):
        raise ValueError("Fuente comprimida no coincide con pin.")
    data = {name: read_regular(source / name, 1024**2) for name in ("go.mod", "go.sum")}
    if any(hashlib.sha256(value).hexdigest() != SOURCE_PINS[name] for name, value in data.items()):
        raise ValueError("Fuente restic original alterada.")
    return root, data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect-metadata", action="store_true")
    args = parser.parse_args(argv)
    try:
        root, data = settings()
        result = make_inventory(data["go.mod"], data["go.sum"])
        result.update(source_sha256=SOURCE_PINS.copy(), metadata_constraint_closure_complete=False)
        if args.collect_metadata:
            store = MetadataStore(root, root / "proxy")
            if (root / "inventory.json").exists():
                raise ValueError("Inventario anterior no se sobrescribe.")

            async def run():
                async with httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                    auth=None,
                    cookies={},
                ) as client:
                    return await collect_metadata(
                        result["proposed_requirements"], client, store.put, cache_read=store.get
                    )

            try:
                result.update(asyncio.run(run()))
            except (ValueError, httpx.HTTPError, TimeoutError) as error:
                result.update(
                    status="incomplete",
                    metadata_constraint_closure_complete=False,
                    files=store.files,
                    error="METADATA_CLOSURE_REJECTED",
                    error_type=type(error).__name__,
                    error_detail=str(error) if isinstance(error, ValueError) else None,
                )
            output = root / (
                "inventory.json"
                if result["metadata_constraint_closure_complete"]
                else "incomplete-" + uuid.uuid4().hex + ".json"
            )
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(json.dumps(result, indent=2, ensure_ascii=False).encode())
                stream.flush()
                os.fsync(stream.fileno())
        print(
            json.dumps(
                {
                    "mode": "metadata" if args.collect_metadata else "plan",
                    "current_requirements": len(result["current_requirements"]),
                    "proposed_requirements": len(result["proposed_requirements"]),
                    "nodes": len(result.get("nodes", [])),
                    "metadata_constraint_closure_complete": result[
                        "metadata_constraint_closure_complete"
                    ],
                    "sdk_executed": False,
                    "mvs_verified": False,
                    "sumdb_verified": False,
                    "inventory_path": str(
                        output if args.collect_metadata else root / "inventory.json"
                    ),
                }
            )
        )
        return (
            0 if not args.collect_metadata or result["metadata_constraint_closure_complete"] else 2
        )
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        print(json.dumps({"success": False, "sdk_executed": False, "error": "INVENTORY_REJECTED"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
