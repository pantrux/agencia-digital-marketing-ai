"""Prepara el delta sin pip sobre el OCI Linux verificado del runner Python V1."""

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_SHA = "69e3bddde2baa272588e2db0e759611154b5a3ea40d782c8fc365cb70cdc19e1"
MANIFEST = "sha256:f26b05501da64d8abbc2ce65c056b69a2f26c3c6c25461eb10491885206938a0"
CONFIG = "sha256:303ec8db904899c5f785e77a44be690fbb54ee0fdff4b977c9ac905ef53ea5c7"
SOURCE_BYTES = 25_914_368
BASE = "sha256:d76f585fb0d5107dd47143da5e61554fd9cae656a30f3470f815cb8475769b6a"
PAYLOAD = "sha256:1bd806f92942be8e02d991e8c547c647f494e7fd08aaa8531b3706f190794741"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    require(path.is_file() and not path.is_symlink(), path.name)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(source):
    require(source.stat().st_size == SOURCE_BYTES and digest(source) == SOURCE_SHA, "OCI V1")
    with tarfile.open(source, "r:") as archive:

        def blob(pin):
            path = "blobs/sha256/" + pin.removeprefix("sha256:")
            row = archive.getmember(path)
            require(row.isfile() and 0 < row.size <= 1024**2, path)
            raw = archive.extractfile(row).read()
            require("sha256:" + hashlib.sha256(raw).hexdigest() == pin, path)
            return json.loads(raw)

        index = json.load(archive.extractfile("index.json"))
        (descriptor,) = index["manifests"]
        if descriptor["digest"] != MANIFEST:
            (descriptor,) = blob(descriptor["digest"])["manifests"]
        require(descriptor["digest"] == MANIFEST, "Manifiesto V1")
        manifest = blob(MANIFEST)
        require(manifest["config"]["digest"] == CONFIG, "Config V1")
        config = blob(CONFIG)
    root = config["config"]
    diff_ids = config["rootfs"]["diff_ids"]
    require(
        (config["os"], config["architecture"]) == ("linux", "amd64")
        and (root["User"], root["WorkingDir"], root["Entrypoint"], root["Cmd"])
        == ("1000:1000", "/home/runner", ["/usr/local/bin/task-runner-launcher"], ["python"])
        and len(diff_ids) == len(manifest["layers"]) == 12
        and diff_ids[5] == PAYLOAD,
        "Contrato runtime V1",
    )
    return diff_ids


def prepare(source, output):
    require(not output.exists() and output.parent.is_dir(), "Salida nueva requerida")
    layers = verify(source)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        root = Path(temp) / "context"
        root.mkdir()
        files = ("Dockerfile", "verify-runner-python-runtime.py")
        shutil.copyfile(HERE / "Dockerfile.runner-python-runtime-v2", root / files[0])
        shutil.copyfile(HERE / files[1], root / files[1])
        hashes = {name: digest(root / name) for name in files}
        (root / "SHA256SUMS").write_bytes(
            "".join(f"{pin}  {name}\n" for name, pin in hashes.items()).encode()
        )
        receipt = {
            "success": True,
            "source_sha256": SOURCE_SHA,
            "base": BASE,
            "parent_manifest": MANIFEST,
            "parent_config": CONFIG,
            "parent_layers": layers,
            "payload_diff_id": PAYLOAD,
            "members": hashes,
            "platform": "linux/amd64",
            "built": False,
        }
        raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        (root / "context.json").write_bytes(raw)
        os.rename(root, output)
    print(
        json.dumps(
            {"context_sha256": hashlib.sha256(raw).hexdigest(), "context": str(output), **receipt},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-oci", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.source_oci, args.output)
