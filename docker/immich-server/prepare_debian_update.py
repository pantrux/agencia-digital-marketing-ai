"""Prepare the sealed, five-package Trixie update for the compiled Immich 3.3.6 image."""

import argparse
import gzip
import hashlib
import io
import json
import lzma
import re
import tarfile
from pathlib import Path

BASE = "50ed464e8a0b9883dda048bdd7015508eba753065340b2f2074c22b1282dc8ba"
CONFIG = "c4eb8f57a12b78f373e396606f07421c4727091163f510ecd315b5185781c450"
BASE_TAR = "651f9b6561a29af02a9827f5c41d58ee710cc0f39172aa45c8a5a2de58275a99"
STATUS = "aefc2961708f62374f37150dae31c8e86bc8a2cde7c12538673d2caf24295aca"
INRELEASE = "511f89867ce6a293f157388d1ff6eec059a731fc092f6ab686f3b7297158decd"
RELEASE = "3a339a3ab6b68f24ad76bae6245620de5d480064d9824c038b6c6032a7d9975a"
SIGNATURE = "a09b9a8463d6eee9887f4e654f2804f8f6c41f69ce535a9d2fd244f681726024"
SECURITY_REPO = "https://deb.debian.org/debian-security/"
PACKAGES_INDEX = "main/binary-amd64/Packages.xz"
UPDATES = {
    "apt": ("3.0.3", "3.0.4"),
    "libapt-pkg7.0": ("3.0.3", "3.0.4"),
    "libbrotli1": ("1.1.0-2+b7", "1.1.0-2+deb13u1"),
    "libopenexr-3-1-30": ("3.1.13-2", "3.2.12-1~deb13u1"),
    "zlib1g": ("1:1.3.dfsg+really1.3.1-1+b1", "1:1.3.dfsg+really1.3.1-1+deb13u1"),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def read_pin(path, expected):
    raw = path.read_bytes()
    require(sha(raw) == expected, f"Unexpected SHA256 for {path.name}")
    return raw


def control_records(text):
    for block in text.strip().split("\n\n"):
        yield dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if line and not line[0].isspace() and ": " in line
        )


def base_status(path):
    with path.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == BASE_TAR, "Unexpected base OCI TAR"
        )
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive:
            [descriptor] = json.load(archive.extractfile("index.json"))["manifests"]
            require(descriptor["digest"] == "sha256:" + BASE, "Unexpected base manifest")
            manifest = json.load(archive.extractfile("blobs/sha256/" + BASE))
            require(manifest["config"]["digest"] == "sha256:" + CONFIG, "Unexpected base config")
            for layer in reversed(manifest["layers"]):
                with tarfile.open(
                    fileobj=archive.extractfile("blobs/sha256/" + layer["digest"][7:]), mode="r|*"
                ) as content:
                    for member in content:
                        if member.name.removeprefix("./") == "var/lib/dpkg/status":
                            raw = content.extractfile(member).read()
                            require(sha(raw) == STATUS, "Unexpected Debian status database")
                            return raw.decode()
    raise ValueError("No Debian status database")


def read_archive_index(root):
    read_pin(root / "InRelease", INRELEASE)
    signature = read_pin(root / "signature-verified.log", SIGNATURE).decode("utf-8-sig")
    require(
        "[GNUPG:] VALIDSIG 89C87ACEA5DD6B8E6A7068808E9F831205B4BA95 " in signature,
        "No Trixie signature",
    )
    release = read_pin(root / "Release-verified", RELEASE).decode()
    require("Codename: trixie-security" in release.splitlines(), "Unexpected repository codename")
    raw = (root / "Packages.xz").read_bytes()
    section = release.split("SHA256:\n", 1)[1].split("\nSHA512:", 1)[0]
    require(
        any(
            line.split() == [sha(raw), str(len(raw)), PACKAGES_INDEX]
            for line in section.splitlines()
        ),
        "Packages digest differs from signed Release",
    )
    return {
        row["Package"]: row
        for row in control_records(lzma.decompress(raw).decode())
        if row.get("Package") in UPDATES and row.get("Architecture") == "amd64"
    }


def inventory(rows):
    return "".join(
        sorted(
            f"{p['Package']}|{p['Architecture']}|{p['Version']}|{p['Status'].split()[-1]}\n"
            for p in rows
        )
    ).encode()


def checksums(files):
    return "".join(f"{sha(raw)}  {name}\n" for name, raw in sorted(files.items())).encode()


def prepare(args):
    installed = list(control_records(base_status(args.base)))
    indexed = read_archive_index(args.inputs)
    before = {item["Package"]: item for item in installed}
    require(len(installed) == len(before), "Duplicate installed package name")
    require(before["libdeflate0"]["Version"] == "1.23-2", "OpenEXR requires the known libdeflate0")
    payload, packages = {}, {}
    before_lock = inventory(installed)
    for name, (old, new) in UPDATES.items():
        row = indexed[name]
        existing = before[name]
        require(
            existing["Version"] == old and row["Version"] == new, f"Unexpected versions of {name}"
        )
        path = row["Filename"]
        require(
            re.fullmatch(r"pool/updates/main/[A-Za-z0-9+./_~\-]+\.deb", path) and ".." not in path,
            "Invalid repository path",
        )
        leaf = path.rsplit("/", 1)[1]
        raw = read_pin(args.inputs / leaf, row["SHA256"])
        require(len(raw) == int(row["Size"]), f"Unexpected size of {name}")
        payload[leaf] = raw
        packages[name] = {
            "old": old,
            "new": new,
            "url": SECURITY_REPO + path,
            "sha256": sha(raw),
            "bytes": len(raw),
            "depends": row["Depends"],
        }
        existing["Version"] = new
    here = Path(__file__).resolve().parent
    payload.update(
        {
            "install.sh": (here / "install_debian_update.sh").read_bytes(),
            "BEFORE.lock": before_lock,
            "AFTER.lock": inventory(installed),
        }
    )
    payload["PAYLOAD.sha256"] = checksums(payload)
    files = {
        "Dockerfile": (here / "Dockerfile.debian-update").read_bytes(),
        **{f"payload/{name}": raw for name, raw in payload.items()},
    }
    args.output.mkdir(exist_ok=False)
    archive = args.output / "context.tar.gz"
    with (
        archive.open("xb") as out,
        gzip.GzipFile(fileobj=out, mode="wb", filename="", mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w|") as tar,
    ):
        for name, raw in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.mode, member.size = 0o644, len(raw)
            tar.addfile(member, io.BytesIO(raw))
    receipt = {
        "archive_sha256": sha(archive.read_bytes()),
        "archive_bytes": archive.stat().st_size,
        "base": BASE,
        "config": CONFIG,
        "inrelease_sha256": INRELEASE,
        "verified_release_sha256": RELEASE,
        "gpgv_status_sha256": SIGNATURE,
        "packages": packages,
        "payload_sha256": sha(payload["PAYLOAD.sha256"]),
        "linux_build_pending": True,
        "production_approved": False,
    }
    (args.output / "receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("base", "inputs", "output"):
        parser.add_argument(f"--{arg}", type=Path, required=True)
    prepare(parser.parse_args())
