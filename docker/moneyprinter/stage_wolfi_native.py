"""Empaqueta el código y los APK fijados para el build Wolfi de MoneyPrinter."""

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / ".tools"
NATIVE = TOOLS / "moneyprinter-remediation/wolfi-ffmpeg-native-v1"
STAGE = TOOLS / "backup-source-build/moneyprinter-wolfi-app-tree-v1"
RUNTIME = "sha256:ba69f696b21955483e5e47e46eab2d7a9e556a3cf5fd4bd40817554901781af8"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_file(path, expected, size=None):
    before = path.lstat()
    assert stat.S_ISREG(before.st_mode) and before.st_nlink == 1
    assert not getattr(before, "st_file_attributes", 0) & 0x400
    assert 0 <= before.st_size <= 32 * 1024**2
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        assert os.path.samestat(before, opened)
        raw = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
    assert os.path.samestat(opened, after)
    assert before.st_size == after.st_size == len(raw)
    assert before.st_mtime_ns == after.st_mtime_ns
    assert size is None or len(raw) == size
    assert sha(raw) == expected
    return raw


def pinned_json(path, expected):
    return json.loads(read_file(path, expected))


def entries():
    app = pinned_json(
        STAGE / "app-tree-inventory.json",
        "805fb244246bb9436a98ef9edc15d58b78f1977351339e728290e212076f982e",
    )
    assert len(app) == 83
    files = {
        "app-tree/" + name: (STAGE / "app-tree" / name, row["sha256"], row["bytes"])
        for name, row in app.items()
    }
    receipt = pinned_json(
        NATIVE / "receipt.json", "5d8278025697ade7642e9dc655f7c56e43c9a452300f956823ae429e1c3a18f7"
    )
    plan = pinned_json(
        NATIVE / "plan.json", "7ae0fe0fbd144997787783e0c39ad9275b46df0cf5ca40383f658f7e309a8c14"
    )
    physical = pinned_json(
        TOOLS / "backup-source-build/python-runtime-lib64-recovery-v1/inventory.json",
        "eb771205202ecc94b632288c46b14806aa7139938f7dcc3a51ceb2e63b803b00",
    )
    assert receipt["runtime_manifest"] == plan["runtime_manifest"] == RUNTIME
    assert receipt["package_count"] == len(receipt["packages"]) == 58
    base = {name: row["V"] for name, row in physical["base"]["packages"].items()}
    assert len(base) == 19
    repo = "apk-repo/x86_64/"
    apks = {}
    for row in receipt["packages"]:
        assert row["success"] and row["architecture"] == "x86_64"
        name, filename = row["name"], row["filename"]
        assert filename == name + "-" + row["version"] + ".apk"
        apks[name] = row["version"]
        files[repo + filename] = (
            NATIVE / "repo/x86_64" / filename,
            row["authentication"]["archive_sha256"],
            row["bytes"],
        )
    (terminfo,) = (
        row for row in physical["manual_materials"] if row["name"] == "ncurses-terminfo-base"
    )
    for row in [*plan["physical_only_reused"], terminfo]:
        name, data = row["name"], row["archive"]
        assert name not in apks and not row["apk_db_installed"]
        path = ROOT / data["path"]
        apks[name] = row["version"]
        files[repo + path.name] = (path, data["sha256"], data["bytes"])
    assert len(apks) == 65 and not (apks.keys() & base.keys())
    files[repo + "APKINDEX.tar.gz"] = (
        NATIVE / "repo/x86_64/APKINDEX.tar.gz",
        receipt["index_sha256"],
        None,
    )
    files["keys/wolfi-signing.rsa.pub"] = (
        NATIVE / "keys/wolfi-signing.rsa.pub",
        receipt["key_sha256"],
        None,
    )
    return base, apks, files


def add(archive, name, raw):
    assert re.fullmatch(r"[a-zA-Z0-9_.+/-]+", name)
    assert all(p not in ("", ".", "..", ".git") for p in name.split("/"))
    item = tarfile.TarInfo(name)
    item.mode = 0o444
    item.size = len(raw)
    archive.addfile(item, io.BytesIO(raw))


def main():
    base, apks, files = entries()
    path = STAGE / "native-inputs-v1.tar"
    records = {}
    with path.open("xb") as target:
        with tarfile.open(fileobj=target, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for name, (local, expected, size) in sorted(files.items()):
                data = read_file(local, expected, size)
                records[name] = {"bytes": len(data), "sha256": expected}
                add(archive, name, data)
            repositories = b"/inputs/apk-repo\n"
            add(archive, "repositories", repositories)
            records["repositories"] = {"bytes": len(repositories), "sha256": sha(repositories)}
        target.flush()
        os.fsync(target.fileno())
    assert len(records) == 151 and path.stat().st_size <= 96 * 1024**2
    with tarfile.open(path, "r:") as archive:
        observed = {}
        for row in archive:
            assert row.isfile() and row.name not in observed
            with archive.extractfile(row) as stream:
                observed[row.name] = {
                    "bytes": row.size,
                    "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                }
        assert observed == records
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    result = {
        "schema": 1,
        "kind": "moneyprinter-wolfi-native-inputs-v1",
        "success": True,
        "runtime_manifest": RUNTIME,
        "base_packages": base,
        "apk_packages": apks,
        "files": records,
        "archive": str(path),
        "archive_bytes": path.stat().st_size,
        "archive_sha256": digest,
    }
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    with (STAGE / "native-inputs-v1.json").open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    print(
        json.dumps(
            {
                "archive_sha256": digest,
                "archive_bytes": path.stat().st_size,
                "manifest_sha256": sha(raw),
                "files": len(records),
                "success": True,
            }
        )
    )


if __name__ == "__main__":
    main()
