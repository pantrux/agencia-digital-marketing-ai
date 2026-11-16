"""Prepara un contexto Linux reproducible de n8n con el addon Kafka acreditado."""

import argparse
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MATERIALS = REPO / ".tools/backup-source-build/n8n-server-source-v1"
APP = MATERIALS / "pnpm-12.12.0-deploy-toolchain-v1/linux-production-tree-quickjs-v1.tar.gz"
APP_SHA = "7c8e32aae45e38bfa4d10ae8cc3768b8ebccfe2763f2336404e2526b8861105e"
NPM = MATERIALS.parent / "immich-server-v336-node-update-v1"
NPM_SHA = "8ad8563b81e71b90ef405b140bccf3ba9b5e7884ab210bd49ee4e2f4bc6f61f5"
NPM_REPORT_SHA = "0a51b6d86c144dae465032ccfe8e06fa7190cac7f52ea5785079403676590b7a"
NPM_RECEIPT_SHA = "a0a8c145cdc58e81d6c7dc914fa50f09ca622548ceaa45059cbd644ad96d4ec5"
KAFKA = MATERIALS / "native-kafka-20261113-v1"
NATIVE = "4a204a0d8895658326cbcb3c18958dc26495d8b7ca0808aef6cb3088697e2bb4"
INVENTORY = "e72380673cc84a4fd6b262be93f9f431c99da733509aafc151322d3f0b0b9a32"
VERIFIED_EXPORT = "8793a95037aa00fd7c6ce4774681ca33b182712838b7fd4d9b0180139ac0bce7"
QUICKJS = (
    REPO
    / ".tools/backup-source-build/n8n-quickjs-wasm-inputs-v1/quickjs-linux-results-20261113-v8.tar"
)
QUICKJS_SHA = "05acec0c24c34c6001244aff9df28239b9376e1a78ff28ff28b5652fe4d16f2e"
PACKAGE = (
    "node_modules/.pnpm/@confluentinc+kafka-javascript@1.9.1_patch_hash="
    "c597d3e3d9d6d0d33588b7dc1e88d55f270ba15_602b6fdd5199747e8d909d98d456db49/"
    "node_modules/@confluentinc/kafka-javascript"
)
PACKAGE_SHA = "e6a4f7d285c0e0422eb200321cff341dd582a92a67fb2e45ec2a6f8690303c70"
APK_NAMES = {
    "brotli-libs",
    "c-ares",
    "gdbm",
    "libcurl",
    "libidn2",
    "libpsl",
    "libsasl",
    "libunistring",
    "lz4-libs",
    "nghttp2-libs",
    "zlib",
    "zstd-libs",
}


def need(ok, description):
    if not ok:
        raise ValueError(description)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    need(path.is_file() and not path.is_symlink(), f"Archivo inválido: {path.name}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pinned(path, expected, limit):
    need(path.is_file() and not path.is_symlink() and path.stat().st_size <= limit, path.name)
    data = path.read_bytes()
    need(len(data) <= limit and sha(data) == expected, path.name)
    return data


def inspect_app(path, expected_sha):
    need(file_sha(path) == expected_sha, "SHA del árbol n8n")
    manifest, entry = f"compiled/{PACKAGE}/package.json", "compiled/bin/n8n"
    addon = f"compiled/{PACKAGE}/build/Release/confluent-kafka-javascript.node"
    required = {
        manifest: PACKAGE_SHA,
        entry: "b057fa30179f9accebb00816a0d406b1f81196c2d540d2bf9abcf2f988c769ca",
    }
    with tarfile.open(path, "r|gz") as archive:
        for row in archive:
            need(row.name != addon, "El árbol ya contiene un addon Kafka")
            if row.name in required:
                need(
                    row.isfile() and sha(archive.extractfile(row).read()) == required.pop(row.name),
                    row.name,
                )
    need(not required, "Faltan rutas de inicio o Kafka")


def inputs():
    inventory = json.loads(pinned(KAFKA / "inputs.json", INVENTORY, 128 * 1024))
    evidence = REPO / ".tools/evidence/phase4-n8n-kafka-native-export-20261113-v1.json"
    export = json.loads(pinned(evidence, VERIFIED_EXPORT, 4096))
    need(
        export["success"] and export["verified_probe"]["loopback_mock_clients"] == 2, "Sonda Kafka"
    )
    apk_rows = {row["name"]: row for row in inventory["apk"] if row["name"] in APK_NAMES}
    need(
        apk_rows.keys() == APK_NAMES
        and sum(row["bytes"] for row in apk_rows.values()) == 2_483_601,
        "Clausura APK",
    )
    files = {}
    for row in apk_rows.values():
        source = REPO / row["path"]
        need(source.name == row["filename"], "Identidad APK")
        files[f"apk/{source.name}"] = pinned(source, row["archive_sha256"], row["bytes"])
    files["apk/SHA256SUMS"] = "".join(
        f"{sha(data)}  {Path(name).name}\n" for name, data in sorted(files.items())
    ).encode()
    native = pinned(KAFKA / "linux-kafka-build-v1.tar", NATIVE, export["bytes"])
    with tarfile.open(fileobj=io.BytesIO(native), mode="r:") as archive:
        for name, expected in export["files"].items():
            row = archive.getmember(name)
            data = archive.extractfile(row).read()
            need(row.isfile() and sha(data) == expected, name)
            target = "kafka/addon" if name.endswith(".node") else "kafka/lib"
            files[f"{target}/{name}"] = data
        for name in ("librdkafka-LICENSE", "librdkafka-LICENSES.txt"):
            row = archive.getmember(name)
            need(row.isfile() and 0 < row.size < 40 * 1024, name)
            files[f"kafka/licenses/{name}"] = archive.extractfile(row).read()
    quickjs = pinned(QUICKJS, QUICKJS_SHA, 6 * 1024**2)
    with tarfile.open(fileobj=io.BytesIO(quickjs), mode="r:") as archive:
        files["verify/quickjs-build.json"] = archive.extractfile("quickjs-build.json").read()
    npm_receipt = json.loads(pinned(NPM / "receipt.json", NPM_RECEIPT_SHA, 4096))
    need(
        npm_receipt["success"]
        and npm_receipt["npm_version"] == "11.21.2"
        and npm_receipt["npm_sha256"] == NPM_SHA
        and npm_receipt["npm_scan_sha256"] == NPM_REPORT_SHA
        and any(row["valid"] for row in npm_receipt["npm_registry"]["signatures"]),
        "Recibo de firma y escaneo de npm",
    )
    report_path = REPO / ".tools/evidence/phase4-immich-npm-tool-11.21.2-trivy-20261113-v1.json"
    npm_report = json.loads(pinned(report_path, NPM_REPORT_SHA, 1024**2))
    node_results = [row for row in npm_report["Results"] if row["Type"] == "node-pkg"]
    need(
        len(node_results) == 1
        and len(node_results[0]["Packages"]) == 144
        and all(
            issue["Severity"] not in {"HIGH", "CRITICAL"}
            for issue in node_results[0].get("Vulnerabilities", [])
        ),
        "Cobertura y umbral del escaneo de npm",
    )
    files["npm/npm-11.21.2.tgz"] = pinned(NPM / "npm-11.21.2.tgz", NPM_SHA, 4 * 1024**2)
    files["npm/SHA256SUMS"] = f"{NPM_SHA}  npm-11.21.2.tgz\n".encode()
    for name in (
        "verify-kafka-native.cjs",
        "verify-npm-runtime.cjs",
        "verify-sqlite3-native.cjs",
        "verify-server-native-image.cjs",
        "verify-quickjs-wasm-sdk.mjs",
    ):
        files[f"verify/{name}"] = (HERE / name).read_bytes()
    files["Dockerfile"] = (HERE / "Dockerfile.server-runtime").read_bytes()
    return files, apk_rows


def prepare(output, application=APP, app_sha=APP_SHA):
    need(not output.exists() and output.parent.is_dir(), "El contexto requiere una ruta nueva")
    files, apk_rows = inputs()
    inspect_app(application, app_sha)
    # Se conserva el tar del servidor y se agrega Kafka en una capa separada.
    manifest = {
        name: {"bytes": len(data), "sha256": sha(data)} for name, data in sorted(files.items())
    }
    manifest["application.tar.gz"] = {"bytes": application.stat().st_size, "sha256": app_sha}
    receipt = {
        "base": "sha256:65fb44a19eb983bc475aebef8ca3416da172e5b7b0e52418cca100997959ca6e",
        "startup": ["/usr/local/bin/node", "/opt/n8n/compiled/bin/n8n", "start"],
        "kafka_source_sha256": NATIVE,
        "kafka_inventory_sha256": INVENTORY,
        "npm_version": "11.21.2",
        "npm_scan_sha256": NPM_REPORT_SHA,
        "apk": {name: row["version"] for name, row in sorted(apk_rows.items())},
        "members": manifest,
        "docker_build_executed": False,
        "production_approved": False,
    }
    files["context.json"] = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        root = Path(temporary) / "context"
        root.mkdir()
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        shutil.copyfile(application, root / "application.tar.gz")
        need(file_sha(root / "application.tar.gz") == app_sha, "Copia de n8n")
        os.rename(root, output)
    print(
        json.dumps(
            {"context": str(output), "receipt_sha256": sha(files["context.json"]), "success": True},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--application", type=Path)
    parser.add_argument("--application-sha256")
    args = parser.parse_args()
    if (args.application is None) != (args.application_sha256 is None):
        parser.error("Indicar juntos --application y --application-sha256")
    if args.application is None:
        prepare(args.output.resolve())
    else:
        prepare(args.output.resolve(), args.application.resolve(), args.application_sha256)
