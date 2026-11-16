"""Verify the Immich Linux export and project the server runtime build context."""

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
from collections import Counter
from pathlib import Path

SOURCE_SHA = "93a0ddc32aa515f7a44bc06f98c5cd5a98070872e3bb96731b256839f440e62b"
MAIN_SHA = "7894328add69a1fe0a44f7d8c84cd527efeafb9bc6dc01b3b0280d236f010861"
BASE_MANIFEST = "sha256:c0a8e008ad9befe8af0e3a515e3b3661e98d38c37eadb6efee18a6d5098136c1"
BASE_CONFIG = "sha256:fb0c7c1a1b2a553c1c60e4bbb3d999d3d1483c9a223807e0856cd42d30b9cbea"
GROUPS = {"server/dist/": 1378, "packages/plugin-sdk/dist/": 7, "packages/sdk/build/": 6}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def select(raw):
    if len(raw) != 1163476 or sha(raw) != SOURCE_SHA:
        raise ValueError("Unexpected Immich Linux export")
    selected, inventory = {}, {}
    counts = Counter()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            name = member.name
            group = next((prefix for prefix in GROUPS if name.startswith(prefix)), None)
            if (
                group is None
                or name in inventory
                or not re.fullmatch(r"[A-Za-z0-9_.+@/-]+", name)
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or not member.isfile()
                or (member.uid, member.gid, member.mode) != (0, 0, 0o644)
                or member.size > 2 * 1024 * 1024
            ):
                raise ValueError(f"Unexpected Immich member: {name}")
            data = archive.extractfile(member).read()
            inventory[name] = sha(data)
            counts[group] += 1
            if group != "packages/sdk/build/":
                selected[name] = data
    if counts != GROUPS or inventory["server/dist/main.js"] != MAIN_SHA:
        raise ValueError("Unexpected compiled Immich inventory")
    return selected, inventory


def sums(files):
    return "".join(f"{pin}  {name}\n" for name, pin in sorted(files.items())).encode()


def prepare(source, output):
    raw = source.read_bytes()
    selected, inventory = select(raw)
    package = Path(__file__).resolve().parent
    runtime = sums({name: inventory[name] for name in selected})
    source_manifest = sums(inventory)
    installer = (package / "install_compiled_server.sh").read_bytes()
    payload = {
        **selected,
        "install.sh": installer,
        "RUNTIME.sha256": runtime,
        "SOURCE-MANIFEST.txt": source_manifest,
        "SOURCE-MANIFEST.sha256": sums({"SOURCE-MANIFEST.txt": sha(source_manifest)}),
    }
    payload["PAYLOAD.sha256"] = sums({name: sha(data) for name, data in payload.items()})
    recipe = (package / "Dockerfile.compiled-server").read_bytes()
    files = {"Dockerfile": recipe, **{f"payload/{name}": data for name, data in payload.items()}}
    output.mkdir(exist_ok=False)
    transport = output / "context.tar.gz"
    with (
        transport.open("xb") as target,
        gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as zipped,
        tarfile.open(fileobj=zipped, mode="w|") as archive,
    ):
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    receipt = {
        "archive": {"sha256": sha(transport.read_bytes()), "bytes": transport.stat().st_size},
        "source": {"sha256": SOURCE_SHA, "bytes": len(raw), "files": len(inventory)},
        "base_manifest": BASE_MANIFEST,
        "base_config": BASE_CONFIG,
        "main_js_sha256": MAIN_SHA,
        "runtime_files": len(selected),
        "sdk_files_retained_in_source_receipt": GROUPS["packages/sdk/build/"],
        "payload_sha256": sha(payload["PAYLOAD.sha256"]),
        "dockerfile_sha256": sha(recipe),
        "runtime_check_pending": True,
        "production_approved": False,
    }
    (output / "receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(args.source, args.output)
