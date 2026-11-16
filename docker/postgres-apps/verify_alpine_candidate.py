"""Verify the exported PostgreSQL image and its offline Trivy scan."""

import argparse
import gzip
import hashlib
import json
import tarfile
from pathlib import Path

BASE_CONFIG = "sha256:22fdd47fd045fefc8c744ff00f801c6efe0e99cab9eb67e1751712760974316c"
BASE_MANIFEST = "sha256:c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a"
BASE_SHA = "a4da25a508d05ee0f3b90511915446d81c8f75c3dfe74c8cb9b0cf1e1da8a71d"
DB_SHA = "15982ae74263f22790b348c89b181c9edfd21d70b08b2364aedcf0f7ef8fab29"
DB_METADATA_SHA = "4532c5b0decd082090096a44391fddc9a314fd33bceb0ab51dfb489a12808cce"
APK_LOCK_SHA = "fe1975092efcf87cf6620b126835df41d122e87821a468a221f8c5a90cc7928d"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def descriptor_blob(archive, descriptor):
    algorithm, pin = descriptor["digest"].split(":")
    if algorithm != "sha256" or len(pin) != 64:
        raise ValueError("Unexpected OCI digest")
    member = archive.getmember(f"blobs/sha256/{pin}")
    if not member.isfile() or member.size != descriptor["size"]:
        raise ValueError("Unexpected OCI blob size or type")
    with archive.extractfile(member) as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin:
            raise ValueError(f"OCI blob hash mismatch: {pin}")
        if descriptor["mediaType"].endswith("+json") or "config" in descriptor["mediaType"]:
            stream.seek(0)
            return json.load(stream)
    return None


def image(path):
    with tarfile.open(path) as archive:
        if len(archive.getnames()) != len(set(archive.getnames())):
            raise ValueError("Duplicate OCI archive members")
        [root] = json.load(archive.extractfile("index.json"))["manifests"]
        manifest = descriptor_blob(archive, root)
        config = descriptor_blob(archive, manifest["config"])
        layers = manifest["layers"]
        diff_ids = config["rootfs"]["diff_ids"]
        for layer, diff_id in zip(layers, diff_ids, strict=True):
            descriptor_blob(archive, layer)
            if not layer["mediaType"].endswith("+gzip"):
                raise ValueError("Expected a gzip-compressed OCI layer")
            name = f"blobs/sha256/{layer['digest'].removeprefix('sha256:')}"
            with archive.extractfile(name) as raw, gzip.GzipFile(fileobj=raw) as stream:
                if f"sha256:{hashlib.file_digest(stream, 'sha256').hexdigest()}" != diff_id:
                    raise ValueError(f"OCI uncompressed layer hash mismatch: {diff_id}")
    if config["os"] != "linux" or config["architecture"] != "amd64":
        raise ValueError("Unexpected image platform")
    return {
        "archive_sha256": digest(path),
        "manifest": root["digest"],
        "config": manifest["config"]["digest"],
        "rootfs": config["rootfs"]["diff_ids"],
        "runtime": config["config"],
    }


def verify(args):
    base, candidate = image(args.base), image(args.candidate)
    if (base["archive_sha256"], base["manifest"], base["config"]) != (
        BASE_SHA,
        BASE_MANIFEST,
        BASE_CONFIG,
    ):
        raise ValueError("Unexpected archived base identity")
    if candidate["runtime"] != base["runtime"] or candidate["rootfs"][:-1] != base["rootfs"]:
        raise ValueError("The candidate changes the runtime configuration or base layers")
    if candidate["rootfs"][-1] in base["rootfs"]:
        raise ValueError("Expected one newly applied layer")
    if args.candidate_id.read_text().strip() not in {candidate["manifest"], candidate["config"]}:
        raise ValueError("The exported image does not match the Linux build receipt")
    if digest(args.db) != DB_SHA or digest(args.db_metadata) != DB_METADATA_SHA:
        raise ValueError("Unexpected Trivy database or metadata")

    report = json.loads(args.report.read_bytes())
    if (
        report["Trivy"]["Version"] != "0.76.2"
        or report["Metadata"]["ImageID"] != candidate["config"]
    ):
        raise ValueError("Trivy report is for a different image or scanner")
    [os_result] = [row for row in report["Results"] if row.get("Class") == "os-pkgs"]
    packages = os_result["Packages"]
    package_lock = sorted(f"{item['Name']}={item['Version']}" for item in packages)
    if (
        os_result["Type"] != "alpine"
        or len(packages) != 45
        or digest(args.expected) != APK_LOCK_SHA
        or package_lock != sorted(args.expected.read_text().splitlines())
    ):
        raise ValueError("Trivy package coverage differs from the post-build inventory")
    counts = dict.fromkeys(("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"), 0)
    for row in report["Results"]:
        for vulnerability in row.get("Vulnerabilities") or []:
            counts[vulnerability.get("Severity", "UNKNOWN")] += 1
    result = {
        "schema": 1,
        "base_manifest": base["manifest"],
        "candidate_manifest": candidate["manifest"],
        "candidate_config": candidate["config"],
        "candidate_archive_sha256": candidate["archive_sha256"],
        "candidate_layer": candidate["rootfs"][-1],
        "trivy_report_sha256": digest(args.report),
        "trivy_db_sha256": DB_SHA,
        "trivy_db_metadata_sha256": DB_METADATA_SHA,
        "apk_count": len(packages),
        "findings": counts,
        "os_high_critical_clear": counts["HIGH"] == counts["CRITICAL"] == 0,
        "sql_acceptance_verified": False,
        "production_approved": False,
    }
    with args.receipt.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    if not result["os_high_critical_clear"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "base",
        "candidate",
        "candidate-id",
        "report",
        "db",
        "db-metadata",
        "expected",
        "receipt",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    verify(parser.parse_args())
