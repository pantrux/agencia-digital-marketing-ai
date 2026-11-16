"""Materializa el espejo npm de n8n por lotes y con dos rutas de caché pnpm."""

import argparse
import http.client
import json
import re
import ssl
import tarfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from prepare_runner_urllib3 import INPUTS, INPUTS_SHA, require, sha
from stage_runner_javascript import yaml_documents

REPO = Path(__file__).resolve().parents[2]
V4 = REPO / ".tools/backup-source-build/n8n-js-frozen-lock-v4"
V4_PIN = "88f17bbed13628b577d1300a715ec1bc78b714975e9dcdb3804658af6bea548a"
CONTEXT_PIN = "2dfc977abe83650bdf8756e43c876265c18254d9151b3d9830e9c794604f5aaa"
OWNER = "agencia-digital-marketing-ai:n8n-server-mirror:v1"
HOST = "registry.npmjs.org"
NAMESPACE = Path("pnpm-cache/v11")
REGISTRY_KEY = "https%3A+registry.npmjs.org"
MIB = 1024**2
WIRE_LIMIT = 16 * MIB
BODY_LIMIT = 96 * MIB
LOT_SIZE = 180
WORKERS = 8
NEEDED_PINS = {
    "@azure/msal-node": "7.1.0",
    "@azure/msal-common": "16.14.2",
    "@intlify/core-base": "11.4.14",
    "@intlify/shared": "11.4.14",
    "@intlify/message-compiler": "11.4.14",
    "@intlify/devtools-types": "11.4.14",
    "@modelcontextprotocol/node": "2.1.0",
    "@modelcontextprotocol/sdk": "1.32.0",
    "@types/markdown-it": "14.1.2",
    "body-parser": "2.4.0",
    "follow-redirects": "1.16.2",
    "markdown-it": "14.3.2",
    "moment": "2.31.0",
    "multer": "2.5.0",
    "qs": "6.16.1",
    "toml": "5.0.1",
    "vue-i18n": "11.4.14",
}


def json_bytes(doc):
    return (
        json.dumps(doc, separators=(",", ":"), ensure_ascii=True, sort_keys=True) + "\n"
    ).encode()


def new(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(body)


def pin(path, digest):
    raw = path.read_bytes()
    require(sha(raw) == digest, "SHA de entrada inesperado.")
    return raw


def locator(home, kind, name):
    require(
        re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", name)
        and ".." not in name.split("/"),
        "Nombre npm fuera del contrato.",
    )
    return home / NAMESPACE / kind / REGISTRY_KEY / (name + ".jsonl")


def input_plan(materials):
    context = materials / "server-lock-resolver.tar.gz"
    pin(context, CONTEXT_PIN)
    with tarfile.open(context, "r:gz") as archive:
        _, lock = yaml_documents(archive.extractfile("pnpm-lock.yaml").read())
    expected = {}
    for key, node in lock["packages"].items():
        name, version = key.rsplit("@", 1)
        expected.setdefault(name, {})[version] = node["resolution"]["integrity"]
    original_count = len(expected)
    require(original_count == 2841 and len(lock["packages"]) == 3478, "Seed distinto.")
    fixes = {
        (r["name"], r["version"]): r["integrity"]
        for r in json.loads(pin(INPUTS, INPUTS_SHA))["npm_candidates"]
    }
    for name, version in [*NEEDED_PINS.items(), ("undici", "6.29.1"), ("undici", "7.30.1")]:
        expected.setdefault(name, {})[version] = fixes.get((name, version))
    receipt = json.loads(pin(V4 / "metadata-receipt.json", V4_PIN))
    v4 = {row["name"]: row for row in receipt["metadata_rows"]}
    require(len(v4) == 1469 and v4.keys() <= expected.keys(), "V4 fuera del grafo servidor.")
    return expected, v4


def validate(body, name, wanted):
    require(
        body["name"] == name
        and isinstance(body["versions"], dict)
        and isinstance(body["time"], dict),
        "Estructura de packument inesperada.",
    )
    for version, integrity in wanted.items():
        version_row = body["versions"].get(version, {})
        dist = version_row.get("dist", {})
        canonical = f"https://{HOST}/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"
        require(
            (version_row.get("name"), version_row.get("version"), dist.get("tarball"))
            == (name, version, canonical)
            and re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", dist.get("integrity", ""))
            and (integrity is None or dist["integrity"] == integrity),
            f"Identidad/SRI ausente o distinta: {name}@{version}",
        )
        published = datetime.fromisoformat(body["time"][version])
        require(published.tzinfo is not None, "Fecha de publicación sin UTC.")


def v4_record(row, wanted):
    raw = pin(locator(V4 / "home", "metadata-full", row["name"]), row["mirror_sha256"])
    require(len(raw) == row["mirror_bytes"], "Tamaño V4 inesperado.")
    _, body = raw.splitlines()
    validate(json.loads(body), row["name"], wanted)
    return raw


def download(name, wanted, deadline):
    require(time.monotonic() < deadline, "Plazo del lote agotado.")
    wire = bytearray()
    conn = http.client.HTTPSConnection(HOST, timeout=12, context=ssl.create_default_context())
    try:
        target = "/" + quote(name, safe="@")
        conn.request(
            "GET",
            target,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "Agency-n8n-registry-mirror/1",
            },
        )
        with conn.getresponse() as response:
            encoding = response.getheader("Content-Encoding")
            length = response.getheader("Content-Length")
            require(
                response.status == 200
                and encoding in (None, "gzip")
                and (length is None or int(length) <= WIRE_LIMIT),
                "Estado o framing del registro inesperado.",
            )
            while chunk := response.read1(65536):
                require(
                    time.monotonic() < deadline and len(wire) + len(chunk) <= WIRE_LIMIT,
                    "Presupuesto de registro agotado.",
                )
                wire.extend(chunk)
            require(length is None or len(wire) == int(length), "Respuesta de registro truncada.")
            header = {
                "etag": response.getheader("ETag"),
                "modified": response.getheader("Last-Modified"),
            }
        if encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body_raw = decoder.decompress(wire, BODY_LIMIT + 1)
            require(
                decoder.eof and not decoder.unused_data and len(body_raw) <= BODY_LIMIT,
                "Gzip de registro inválido o sobredimensionado.",
            )
        else:
            body_raw = bytes(wire)
        body = json.loads(body_raw)
        validate(body, name, wanted)
        raw = json_bytes(header) + json_bytes(body)
        return raw, {
            "name": name,
            "source": f"https://{HOST}{target}",
            "wire_bytes": len(wire),
            "body_sha256": sha(body_raw),
        }
    finally:
        conn.close()


def publish(root, name, raw, proof):
    home = root / "home"
    for kind in ("metadata-full", "metadata"):
        new(locator(home, kind, name), raw)
    return {**proof, "name": name, "bytes": len(raw), "mirror_sha256": sha(raw)}


def import_v4(root, expected, old):
    root.mkdir()
    identity = root.stat()
    new(
        root / ".agency-owner.json",
        json_bytes({"owner": OWNER, "device": identity.st_dev, "inode": identity.st_ino}),
    )
    reused, refresh = [], []
    for name, row in sorted(old.items()):
        try:
            raw = v4_record(row, expected[name])
        except (ValueError, KeyError):
            refresh.append(name)
            continue
        reused.append(publish(root, name, raw, {"source": "V4"}))
    pending = sorted((expected.keys() - old.keys()) | set(refresh))
    doc = {
        "owner": OWNER,
        "expected": len(expected),
        "reused": reused,
        "pending": pending,
        "refreshed": refresh,
    }
    new(root / "import-v4.json", json_bytes(doc))
    return {
        "reused": len(reused),
        "pending": len(pending),
        "refresh": refresh,
        "batches": (len(pending) + LOT_SIZE - 1) // LOT_SIZE,
    }


def check_root(root):
    info = root.lstat()
    marker = json.loads((root / ".agency-owner.json").read_bytes())
    require(
        root.is_dir()
        and not root.is_symlink()
        and not getattr(info, "st_file_attributes", 0) & 0x400
        and marker == {"owner": OWNER, "device": info.st_dev, "inode": info.st_ino},
        "Identidad del espejo inesperada.",
    )


def batch(root, expected, index):
    check_root(root)
    plan = json.loads((root / "import-v4.json").read_bytes())
    require(
        plan["owner"] == OWNER and plan["expected"] == len(expected), "Plan importado inesperado."
    )
    pending = plan["pending"]
    require(
        len(pending) == len(set(pending)) and set(pending) <= expected.keys(),
        "Cola de registro inesperada.",
    )
    names = pending[(index - 1) * LOT_SIZE : index * LOT_SIZE]
    require(index > 0 and names, "Número de lote fuera del plan.")
    claim = root / f"lot-{index:02d}.intent.json"
    new(claim, json_bytes({"names": names, "started_utc": datetime.now(UTC).isoformat()}))
    result, errors = [], {}
    deadline = time.monotonic() + 240
    decoded = wire = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        tasks = {pool.submit(download, name, expected[name], deadline): name for name in names}
        for task in as_completed(tasks):
            name = tasks.pop(task)
            try:
                raw, proof = task.result()
                decoded += len(raw)
                wire += proof["wire_bytes"]
                require(
                    decoded <= 384 * MIB and wire <= 128 * MIB,
                    "Presupuesto acumulado del lote agotado.",
                )
                result.append(publish(root, name, raw, proof))
            except (OSError, ValueError, KeyError, http.client.HTTPException) as error:
                errors[name] = {"type": type(error).__name__, "reason": str(error)[:180]}
    doc = {
        "index": index,
        "rows": sorted(result, key=lambda row: row["name"]),
        "errors": errors,
        "wire_bytes": wire,
        "decoded_bytes": decoded,
    }
    output = root / f"lot-{index:02d}.receipt.json"
    new(output, json_bytes(doc))
    return {
        "batch": index,
        "requests": len(names),
        "ok": len(result),
        "errors": errors,
        "sha256": sha(output.read_bytes()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materials", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--import-v4", action="store_true")
    action.add_argument("--batch", type=int)
    args = parser.parse_args()
    root = args.materials / "server-mirror-v1"
    expected, old = input_plan(args.materials)
    if args.import_v4:
        result = import_v4(root, expected, old)
    elif args.batch is not None:
        result = batch(root, expected, args.batch)
    else:
        result = {
            "expected": len(expected),
            "from_v4_candidates": len(old),
            "new_names": len(expected.keys() - old.keys()),
            "planned": True,
        }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
