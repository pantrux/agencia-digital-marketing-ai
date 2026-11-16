"""Verify the three Alpine 3.24 updates against the archived PostgreSQL image."""

import argparse
import hashlib
import importlib.util
import io
import json
import tarfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/"
BASE_ARCHIVE_SHA = "a4da25a508d05ee0f3b90511915446d81c8f75c3dfe74c8cb9b0cf1e1da8a71d"
BASE_MANIFEST = "c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a"
BASE_CONFIG = "22fdd47fd045fefc8c744ff00f801c6efe0e99cab9eb67e1751712760974316c"
BASE_DB_LAYER = "850c0285d2f320e3b3f9fafa8b38ae240ea6d2cf983ab5195a9da030b21422de"
BASE_DB_SHA = "af1be6395c8850e28ddc34c3de8839c47de7aee1611380fa75c380cb9f75a72f"
KEY_ID = "alpine-devel@lists.alpinelinux.org-6165ee59.rsa.pub"
KEY_SHA = "207e4696d3c05f7cb05966aee557307151f1f00217af4143c1bcaf33b8df733f"
INDEX_SHA = "3c7fd55bbb047333c6e835582564adfd3d84f4af8de97ce42f88e7fae4991ba9"
VERIFY_SHA = "b00b1a1a1822b13ed145128209664a15f3efa781b39cd850d0d63bcc0666192b"
UPDATES = {
    "krb5-libs": (
        "1.22.2-r1",
        "1.22.3-r0",
        "fad5b46a953e6a37a1bb61a84538fa08ecb6a07816f439eb06f2735aebfcbfbf",
    ),
    "libxslt": (
        "1.1.43-r3",
        "1.1.43-r4",
        "40e6cc7d09ce1e69421c2b37e1da4e9afef486843f21c3a79339cd6d93c1c617",
    ),
    "zlib": (
        "1.3.2-r1",
        "1.4-r0",
        "7b05bc778b8b25cd880835b40114182bd4d1581717504d0129c810c0a14291cb",
    ),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def checked(data, pin):
    if sha(data) != pin:
        raise ValueError(f"Expected SHA256 {pin}")
    return data


def load_verifier():
    path = ROOT / "docker/postgres-immich/native_inputs.py"
    checked(path.read_bytes(), VERIFY_SHA)
    spec = importlib.util.spec_from_file_location("native_inputs", path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    return verifier


def base_files(path):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != BASE_ARCHIVE_SHA:
            raise ValueError("Unexpected PostgreSQL archive")
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive:
            index = json.load(archive.extractfile("index.json"))
            [descriptor] = index["manifests"]
            if descriptor["digest"] != f"sha256:{BASE_MANIFEST}":
                raise ValueError("Unexpected OCI subject")
            manifest = json.loads(
                checked(archive.extractfile(f"blobs/sha256/{BASE_MANIFEST}").read(), BASE_MANIFEST)
            )
            if manifest["config"]["digest"] != f"sha256:{BASE_CONFIG}" or not any(
                item["digest"] == f"sha256:{BASE_DB_LAYER}" for item in manifest["layers"]
            ):
                raise ValueError("Unexpected OCI layout")
            layer = checked(
                archive.extractfile(f"blobs/sha256/{BASE_DB_LAYER}").read(), BASE_DB_LAYER
            )
    with tarfile.open(fileobj=io.BytesIO(layer)) as files:
        installed = checked(files.extractfile("lib/apk/db/installed").read(), BASE_DB_SHA)
        world = files.extractfile("etc/apk/world").read()
    return installed.decode(), world.decode().splitlines()


def verify_dependencies(verifier, before, after, world):
    affected_caps = {
        cap
        for name in UPDATES
        for row in (before[name], after[name])
        for cap, _ in verifier.capabilities(row)
    }
    if any(verifier.selector(requirement)[0] in affected_caps for requirement in world):
        raise ValueError("An existing world constraint changes")
    providers = defaultdict(list)
    for name, row in after.items():
        for cap, version in verifier.capabilities(row):
            providers[cap].append((name, version))
    reviewed = []
    for name, row in sorted(after.items()):
        for requirement in row.get("D", "").split():
            cap, operator, wanted = verifier.selector(requirement.removeprefix("!"))
            if name not in UPDATES and cap not in affected_caps:
                continue
            matched = sorted(
                provider
                for provider, version in providers[cap]
                if verifier.version_match(version, operator, wanted)
            )
            if bool(matched) == requirement.startswith("!"):
                raise ValueError(f"Unsatisfied dependency {name}: {requirement}")
            reviewed.append(
                {
                    "package": name,
                    "requires": requirement,
                    "provided_by": [f"{provider}={after[provider]['V']}" for provider in matched],
                }
            )
    if len(reviewed) != 17:
        raise ValueError("Dependency review count changed")
    return reviewed


def inventory(records):
    return "".join(f"{name}={row['V']}\n" for name, row in sorted(records.items())).encode()


def prepare(args):
    verifier = load_verifier()
    key = checked(args.key.read_bytes(), KEY_SHA)
    trusted = {KEY_ID: key}
    index_data = checked((args.inputs / "main-APKINDEX.tar.gz").read_bytes(), INDEX_SHA)
    index = verifier.verified_control(index_data, trusted)
    if index["payload"] or set(index["files"]) != {"DESCRIPTION", "APKINDEX"}:
        raise ValueError("Unexpected APKINDEX format")
    records = verifier.parse_records(index["files"]["APKINDEX"].decode())
    installed, world = base_files(args.base)
    before = verifier.parse_records(installed)
    if len(before) != 45 or before["apk-tools"]["V"] != "3.0.9-r0":
        raise ValueError("Unexpected base APK inventory")
    after = dict(before)
    receipts = {}
    payloads = {}
    for name, (old, version, pin) in UPDATES.items():
        row = records[name]
        if before[name]["V"] != old or row["V"] != version or row["A"] != "x86_64":
            raise ValueError(f"Unexpected update for {name}")
        filename = f"{name}-{version}.apk"
        raw = checked((args.inputs / filename).read_bytes(), pin)
        verification = verifier.verify_apk(raw, trusted, row)
        if (
            len(raw) != int(row["S"])
            or verification["scripts"]
            or set(verification["depends"]) != set(row.get("D", "").split())
            or set(verification["provides"]) != set(row.get("p", "").split())
        ):
            raise ValueError(f"Unexpected signed APK controls for {name}")
        after[name] = row
        receipts[name] = {
            **verification,
            "from": old,
            "url": BASE_URL + filename,
            "bytes": len(raw),
        }
        payloads[filename] = raw
    dependencies = verify_dependencies(verifier, before, after, world)
    payloads.update({"before.lock": inventory(before), "after.lock": inventory(after)})
    args.output.mkdir(exist_ok=False)
    checksums = "".join(f"{sha(raw)}  {name}\n" for name, raw in sorted(payloads.items())).encode()
    for name, data in {**payloads, "SHA256SUMS": checksums}.items():
        (args.output / name).write_bytes(data)
    receipt = {
        "base": {
            "manifest": BASE_MANIFEST,
            "config": BASE_CONFIG,
            "archive_sha256": BASE_ARCHIVE_SHA,
        },
        "index": {"url": BASE_URL + "APKINDEX.tar.gz", "sha256": INDEX_SHA, "key_sha256": KEY_SHA},
        "inputs_sha256": sha(checksums),
        "packages": receipts,
        "dependency_edges_checked": dependencies,
        "native_install_verified": False,
    }
    with (args.output.parent / "receipt.json").open("xb") as stream:
        stream.write((json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
    print(receipt["inputs_sha256"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    prepare(parser.parse_args())
