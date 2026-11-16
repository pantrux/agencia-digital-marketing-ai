"""Staging de datos GoProxy: plan predeterminado, sin Go, extracción o bóveda."""

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from backup_tools_access_background import check_access
from go_module_semver import escape, validate_module
from restic_archive_inputs import MAX_ZIP, ArchiveError, inspect_module_zip

PROJECT = Path(__file__).resolve().parents[1]
PARENT = PROJECT / ".tools/backup-source-build/restic-security-v2"
ROOT = PARENT / "module-archives-v1"
OWNER = "agencia-digital-marketing-ai:restic-module-archives:v1"
MARKER = ".agency-restic-module-archives-owner.json"
CACHE = PROJECT / ".tools/backup-source-build/runs/restic-mvs-v1/module-cache"
INVENTORY = PROJECT / ".tools/evidence/restic-security-v2-selected-verified.json"
OSV = PROJECT / ".tools/evidence/restic-security-v2-selected-osv.json"
CACHE_INDEX = PARENT / "mvs-online-v2.cache-inventory.json"
PINS = {
    str(INVENTORY): "9e528979108edce274e45127ab60626cc9ccec003a2063162dce122b6e567029",
    str(OSV): "90f6a068317789a6b36c7ec0783de29051ed43f0224e2e76ba54b74e4c456836",
    str(CACHE_INDEX): "f3a7432ed4922615ddbe3494edce607059f82227a0dc68f1fa1af1d7e16d9b42",
    str(PARENT / "go.sum"): "3de5391b0dce172a76a8e551b3f3cdec85b3188454bcf4bd88682e708ad9a33d",
    str(
        PROJECT / ".tools/evidence/backup-go-sdk-file-hashes.json"
    ): "736791e23b9f17337997d57b6261357593fbadc16ce98113b359653c7bc94fd5",
}
COMPRESSED_TOTAL = 3 * 1024**3
EXPANDED_TOTAL = 8 * 1024**3


class StageError(ValueError):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise StageError("JSON_DUPLICATE")
            out[key] = value
        return out

    return json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=unique,
        parse_constant=lambda _value: (_ for _ in ()).throw(StageError("JSON_NONFINITE")),
    )


def regular(path, maximum=8 * 1024**2):
    path = Path(path)
    for parent in (*reversed(path.absolute().parents), path.absolute()):
        node = parent.lstat()
        if stat.S_ISLNK(node.st_mode) or getattr(node, "st_file_attributes", 0) & 0x400:
            raise StageError("LINK_OR_REPARSE")
    node = path.lstat()
    if not stat.S_ISREG(node.st_mode) or node.st_nlink != 1 or node.st_size > maximum:
        raise StageError("FILE_TYPE_OR_LIMIT")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        held = os.fstat(stream.fileno())
        if (held.st_dev, held.st_ino, held.st_size) != (node.st_dev, node.st_ino, node.st_size):
            raise StageError("FILE_CHANGED")
        raw = stream.read(maximum + 1)
        if len(raw) != node.st_size or len(raw) > maximum:
            raise StageError("FILE_CHANGED")
        return raw


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def allowed_url(url):
    try:
        p = urllib.parse.urlsplit(url)
        if (
            p.scheme != "https"
            or p.hostname not in ("proxy.golang.org", "storage.googleapis.com")
            or p.username
            or p.password
            or p.port not in (None, 443)
            or p.fragment
        ):
            raise ValueError
    except ValueError:
        raise StageError("REDIRECT_REJECTED") from None
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_zip(module, version, path, *, max_bytes=MAX_ZIP, open_url=None):
    validate_module(module, version)
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_ZIP:
        raise StageError("DOWNLOAD_LIMIT")
    original = "https://proxy.golang.org/" + escape(module) + "/@v/" + escape(version) + ".zip"
    current, redirects = original, 0
    if open_url is None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            NoRedirect(),
        )
        open_url = opener.open
    started = time.monotonic()
    while True:
        allowed_url(current)
        request = urllib.request.Request(
            current,
            headers={"Accept-Encoding": "identity", "User-Agent": "agency-static-module-stage/1"},
        )
        try:
            response = open_url(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308) and redirects < 3:
                location = error.headers.get("Location")
                error.close()
                if not isinstance(location, str):
                    raise StageError("REDIRECT_REJECTED") from None
                current = allowed_url(urllib.parse.urljoin(current, location))
                redirects += 1
                continue
            status = error.code
            error.close()
            raise StageError("HTTP_" + str(status)) from None
        except (OSError, ValueError):
            raise StageError("HTTP_FAILED") from None
        with response:
            if (
                response.status != 200
                or response.headers.get("Content-Encoding", "identity").lower() != "identity"
            ):
                raise StageError("HTTP_RESPONSE")
            length = response.headers.get("Content-Length")
            if length is not None and (
                not re.fullmatch(r"[0-9]{1,12}", length) or int(length) > max_bytes
            ):
                raise StageError("DOWNLOAD_LIMIT")
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
            count, digest = 0, hashlib.sha256()
            with os.fdopen(fd, "wb") as stream:
                try:
                    while chunk := response.read1(65536):
                        count += len(chunk)
                        if count > max_bytes or time.monotonic() - started > 300:
                            raise StageError("DOWNLOAD_LIMIT")
                        stream.write(chunk)
                        digest.update(chunk)
                except (OSError, http.client.HTTPException):
                    raise StageError("HTTP_FAILED") from None
                stream.flush()
                os.fsync(stream.fileno())
            if not count or length is not None and int(length) != count:
                raise StageError("DOWNLOAD_TRUNCATED")
        return {
            "url": original,
            "redirects": redirects,
            "compressed_bytes": count,
            "sha256": digest.hexdigest(),
        }


def valid_h1(value):
    try:
        return value.startswith("h1:") and len(base64.b64decode(value[3:], validate=True)) == 32
    except (ValueError, AttributeError):
        return False


def make_plan():
    loaded = {}
    for name, expected in PINS.items():
        raw = regular(name)
        if sha(raw) != expected:
            raise StageError("INPUT_PIN_CHANGED")
        loaded[name] = raw
    source_manifest = {
        r["path"]: r
        for r in decode(loaded[str(PROJECT / ".tools/evidence/backup-go-sdk-file-hashes.json")])
    }
    sdk_sources = {}
    for suffix, expected in {
        "sumdb/dirhash/hash.go": "24de6a735c1605e39b6381e6962787faa5c59adab896fb66dbc1ac8b6afcb297",
        "zip/zip.go": "3a0fd2c978e20233b6bd904a9c510dd746a7a50825c35a2e4ab1d8618f9de77b",
        "module/module.go": "aa2b672b6d0bbc701d46556d7d46399b2ef6ce1fb918687bd067bf8d5efd7741",
    }.items():
        relative = "src/cmd/vendor/golang.org/x/mod/" + suffix
        raw = regular(PROJECT / ".tools/backup-source-build/sdk/go1.27.3/go" / relative)
        if (
            sha(raw) != expected
            or source_manifest[relative]["sha256"] != expected
            or len(raw) != source_manifest[relative]["size"]
        ):
            raise StageError("SDK_HASH_SOURCE_CHANGED")
        sdk_sources[relative] = expected
    inventory, osv, cache_index = (decode(loaded[str(p)]) for p in (INVENTORY, OSV, CACHE_INDEX))
    modules = inventory["modules"]
    selected = [(row["Path"], row["Version"]) for row in modules if row.get("Main") is not True]
    expected_osv = set(selected) | {("github.com/restic/restic", "v0.19.1"), ("stdlib", "1.27.3")}
    if (
        len(modules) != 285
        or len(selected) != 284
        or len(set(selected)) != 284
        or inventory["verification"].get("success") is not True
        or osv.get("requested") != 286
        or osv.get("covered") != 286
        or osv.get("pagination_exhausted") is not True
        or {(row["name"], row["version"]) for row in osv["results"]} != expected_osv
        or len(osv["results"]) != 286
        or any(row["vulnerability_ids"] for row in osv["results"])
    ):
        raise StageError("SELECTION_OR_OSV_COVERAGE")
    cache_rows = {row["path"]: row for row in cache_index["files"]}
    declared = {}
    for line in loaded[str(PARENT / "go.sum")].decode("utf-8").splitlines():
        path, version, checksum = line.split()
        if not valid_h1(checksum):
            raise StageError("SUM_FORMAT")
        if not version.endswith("/go.mod"):
            if (path, version) in declared and declared[path, version] != checksum:
                raise StageError("SUM_CONTRADICTION")
            declared[path, version] = checksum
    rows = []
    for index, (module, version) in enumerate(sorted(selected), 1):
        validate_module(module, version)
        prefix = escape(module) + "/@v/" + escape(version)
        metadata = {}
        for kind in ("mod", "info"):
            relative = "cache/download/" + prefix + "." + kind
            cached = cache_rows.get(relative)
            if cached is None:
                raise StageError("SELECTED_METADATA_MISSING")
            raw = regular(CACHE / relative, 262144 if kind == "mod" else 65536)
            if len(raw) != cached["size"] or sha(raw) != cached["sha256"]:
                raise StageError("SELECTED_METADATA_CHANGED")
            text = raw.decode("utf-8", errors="strict")
            if kind == "info" and decode(raw).get("Version") != version:
                raise StageError("INFO_VERSION")
            if kind == "mod" and re.findall(
                r'(?m)^\s*module\s+"?([^"\s]+)"?\s*(?://[^\n]*)?$', text
            ) != [module]:
                raise StageError("MOD_IDENTITY")
            metadata[kind] = {
                "path": str(CACHE / relative),
                "sha256": sha(raw),
                "bytes": len(raw),
                "target": "proxy/" + prefix + "." + kind,
            }
        checksums = []
        if (module, version) in declared:
            checksums.append({"source": "go.sum declarado", "h1": declared[module, version]})
        lookup = (
            "cache/download/sumdb/sum.golang.org/lookup/" + escape(module) + "@" + escape(version)
        )
        if lookup in cache_rows:
            raw = regular(CACHE / lookup)
            if sha(raw) != cache_rows[lookup]["sha256"]:
                raise StageError("LOOKUP_CHANGED")
            for line in raw.decode("utf-8", errors="strict").splitlines():
                parts = line.split()
                if len(parts) == 3 and parts[:2] == [module, version] and valid_h1(parts[2]):
                    checksums.append(
                        {
                            "source": "lookup cacheado; firma/Merkle no verificados por Python",
                            "h1": parts[2],
                            "sha256": sha(raw),
                        }
                    )
        if len({row["h1"] for row in checksums}) > 1:
            raise StageError("H1_DECLARATIONS_CONTRADICT")
        rows.append(
            {
                "index": index,
                "module": module,
                "version": version,
                "metadata": metadata,
                "target": "proxy/" + prefix + ".zip",
                "checksums": checksums,
                "expected_h1": checksums[0]["h1"] if checksums else None,
            }
        )
    return {
        "schema_version": 1,
        "owner": OWNER,
        "root": str(ROOT),
        "inputs": PINS,
        "controller_sha256": sha(regular(Path(__file__))),
        "validator_sha256": sha(regular(Path(__file__).with_name("restic_archive_inputs.py"))),
        "hashzip_hash1_sdk_sources": sdk_sources,
        "modules": rows,
        "compressed_limit": COMPRESSED_TOTAL,
        "expanded_limit": EXPANDED_TOTAL,
        "sdk_executed": False,
        "extraction_permitted": False,
        "sumdb_verified_by_python": False,
    }


def parent_proof():
    check_access(PARENT, private=True)
    raw = regular(PARENT / ".agency-restic-security-owner.json", 8192)
    marker, info = decode(raw), PARENT.stat()
    if (
        marker.get("owner") != "agencia-digital-marketing-ai:restic-security:v2"
        or marker.get("path") != str(PARENT)
        or (marker.get("device"), marker.get("inode")) != (info.st_dev, info.st_ino)
    ):
        raise StageError("PARENT_OWNER")


def prepare(plan):
    parent_proof()
    os.mkdir(ROOT, 0o700)
    check_access(ROOT, private=True)
    info = ROOT.stat()
    marker = {
        "schema_version": 1,
        "owner": OWNER,
        "path": str(ROOT),
        "device": info.st_dev,
        "inode": info.st_ino,
    }
    write_new(ROOT / MARKER, encoded(marker))
    write_new(ROOT / "staging-plan.json", encoded(plan))
    return {
        "prepared": True,
        "root": str(ROOT),
        "marker": marker,
        "plan_sha256": sha(encoded(plan)),
        "sdk_executed": False,
    }


def guard_root():
    parent_proof()
    check_access(ROOT, private=True)
    marker = decode(regular(ROOT / MARKER, 8192))
    info = ROOT.stat()
    if marker != {
        "schema_version": 1,
        "owner": OWNER,
        "path": str(ROOT),
        "device": info.st_dev,
        "inode": info.st_ino,
    }:
        raise StageError("ROOT_OWNER")
    return marker


def mkdirs(relative):
    current = ROOT
    for part in Path(relative).parts:
        if part in ("", ".", "..") or ":" in part or "\\" in part:
            raise StageError("TARGET_PATH")
        current /= part
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise StageError("TARGET_TYPE") from None
    return current


def file_row(relative):
    if (
        type(relative) is not str
        or "\\" in relative
        or ":" in relative
        or relative.startswith("/")
        or any(p in ("", ".", "..") for p in relative.split("/"))
    ):
        raise StageError("TARGET_PATH")
    path = ROOT / relative
    for parent in (*reversed(path.parents), path):
        item = parent.lstat()
        if stat.S_ISLNK(item.st_mode) or getattr(item, "st_file_attributes", 0) & 0x400:
            raise StageError("LINK_OR_REPARSE")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_ZIP:
        raise StageError("FILE_TYPE_OR_LIMIT")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest, count = hashlib.sha256(), 0
    with os.fdopen(fd, "rb") as stream:
        held = os.fstat(stream.fileno())
        if (held.st_dev, held.st_ino, held.st_size) != (info.st_dev, info.st_ino, info.st_size):
            raise StageError("FILE_CHANGED")
        while chunk := stream.read(65536):
            count += len(chunk)
            if count > info.st_size:
                raise StageError("FILE_CHANGED")
            digest.update(chunk)
        after = path.lstat()
        if count != info.st_size or (after.st_dev, after.st_ino, after.st_size) != (
            info.st_dev,
            info.st_ino,
            info.st_size,
        ):
            raise StageError("FILE_CHANGED")
    return {
        "path": relative,
        "bytes": count,
        "sha256": digest.hexdigest(),
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def records(plan, plan_sha):
    result = {}
    directory = ROOT / "receipts"
    if not directory.exists():
        return result
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise StageError("RECEIPT_DIRECTORY")
    for path in sorted(directory.iterdir()):
        if not re.fullmatch(r"[0-9]{4}\.json", path.name):
            raise StageError("FOREIGN_RECEIPT")
        row = decode(regular(path))
        index = row["index"]
        if (
            type(index) is not int
            or not 1 <= index <= len(plan["modules"])
            or path.name != f"{index:04d}.json"
        ):
            raise StageError("RECEIPT_IDENTITY")
        expected = plan["modules"][index - 1]
        if (
            index in result
            or row.get("plan_sha256") != plan_sha
            or (row.get("module"), row.get("version")) != (expected["module"], expected["version"])
        ):
            raise StageError("RECEIPT_IDENTITY")
        permitted = {
            expected["target"],
            *(m["target"] for m in expected["metadata"].values()),
            f"quarantine/{index:04d}.zip",
        }
        if (
            row.get("status") not in ("staged", "failed")
            or not isinstance(row.get("files"), list)
            or any(
                not isinstance(item, dict)
                or type(item.get("path")) is not str
                or item["path"] not in permitted
                for item in row["files"]
            )
        ):
            raise StageError("RECEIPT_FILES")
        if row["status"] == "staged" and {item["path"] for item in row["files"]} != permitted - {
            f"quarantine/{index:04d}.zip"
        }:
            raise StageError("RECEIPT_FILES")
        for item in row["files"]:
            if file_row(item["path"]) != item:
                raise StageError("STAGED_FILE_CHANGED")
        result[index] = row
    return result


def stage(plan, plan_sha, limit):
    guard_root()
    lock = ROOT / ".stage-lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    held = os.fstat(fd)
    try:
        prior = records(plan, plan_sha)
        compressed = sum(row["compressed_charge"] for row in prior.values())
        expanded = sum(row["expanded_charge"] for row in prior.values())
        mkdirs("receipts")
        mkdirs("quarantine")
        processed = 0
        for entry in plan["modules"]:
            if entry["index"] in prior:
                continue
            if processed >= limit:
                break
            guard_root()
            index = entry["index"]
            relative = "quarantine/" + f"{index:04d}.zip"
            destination = ROOT / relative
            if os.path.lexists(destination):
                raise StageError("FOREIGN_DESTINATION")
            row = {
                "index": index,
                "module": entry["module"],
                "version": entry["version"],
                "plan_sha256": plan_sha,
                "status": "failed",
                "files": [],
                "compressed_charge": 0,
                "expanded_charge": 0,
            }
            budget_stop = False
            try:
                if compressed >= COMPRESSED_TOTAL:
                    raise StageError("TOTAL_COMPRESSED_LIMIT")
                network = download_zip(
                    entry["module"],
                    entry["version"],
                    destination,
                    max_bytes=min(MAX_ZIP, COMPRESSED_TOTAL - compressed),
                )
                try:
                    with zipfile.ZipFile(destination) as archive:
                        charge = min(MAX_ZIP, sum(info.file_size for info in archive.infolist()))
                except (zipfile.BadZipFile, UnicodeError):
                    charge = MAX_ZIP
                row["expanded_charge"] = charge
                if expanded + charge > EXPANDED_TOTAL:
                    budget_stop = True
                    row["expanded_charge"] = 0
                    row["expanded_rejected_bytes"] = charge
                    raise StageError("TOTAL_EXPANDED_LIMIT")
                checked = inspect_module_zip(
                    destination, entry["module"], entry["version"], expected_h1=entry["expected_h1"]
                )
                for metadata in entry["metadata"].values():
                    raw = regular(metadata["path"], 262144)
                    if sha(raw) != metadata["sha256"]:
                        raise StageError("METADATA_CHANGED")
                    target = ROOT / metadata["target"]
                    mkdirs(target.relative_to(ROOT).parent)
                    write_new(target, raw)
                    row["files"].append(file_row(metadata["target"]))
                target = ROOT / entry["target"]
                os.link(destination, target, follow_symlinks=False)
                os.unlink(destination)
                row["files"].append(file_row(entry["target"]))
                row.update(status="staged", archive=checked, network=network)
            except (ArchiveError, StageError, OSError, ValueError) as error:
                row.update(
                    error=getattr(
                        error,
                        "code",
                        str(error) if isinstance(error, StageError) else "INPUT_REJECTED",
                    ),
                    member=getattr(error, "member", None),
                )
            if destination.exists():
                row["files"].append(file_row(relative))
            row["compressed_charge"] = sum(
                item["bytes"] for item in row["files"] if item["path"].endswith(".zip")
            )
            compressed += row["compressed_charge"]
            expanded += row["expanded_charge"]
            write_new(ROOT / "receipts" / f"{index:04d}.json", encoded(row))
            prior[index] = row
            processed += 1
            print(
                json.dumps({"index": index, "status": row["status"], "error": row.get("error")}),
                flush=True,
            )
            if budget_stop:
                break
        ledger = {
            "owner": OWNER,
            "plan_sha256": plan_sha,
            "processed": len(prior),
            "staged": sum(row["status"] == "staged" for row in prior.values()),
            "failed": sum(row["status"] != "staged" for row in prior.values()),
            "compressed_bytes": compressed,
            "expanded_bytes_charged": expanded,
            "receipts": [
                {"index": i, "sha256": sha(regular(ROOT / "receipts" / f"{i:04d}.json"))}
                for i in sorted(prior)
            ],
            "complete": len(prior) == 284
            and all(row["status"] == "staged" for row in prior.values()),
            "sdk_executed": False,
            "extracted": False,
            "sumdb_verified_by_python": False,
        }
        name = ROOT / f"ledger-{len(prior):04d}.json"
        if not name.exists():
            write_new(name, encoded(ledger))
        return ledger
    finally:
        os.close(fd)
        info = lock.lstat()
        if (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino):
            os.unlink(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--limit", type=int, default=284)
    args = parser.parse_args(argv)
    try:
        plan = make_plan()
        if args.prepare:
            report = prepare(plan)
        elif args.apply:
            raw = regular(ROOT / "staging-plan.json")
            if args.plan_sha256 != sha(raw) or decode(raw) != plan or not 1 <= args.limit <= 284:
                raise StageError("EXTERNAL_PLAN")
            report = stage(plan, sha(raw), args.limit)
        else:
            report = {
                "plan_only": True,
                "modules": len(plan["modules"]),
                "root": str(ROOT),
                "sdk_executed": False,
                "network_used": False,
            }
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, IndexError):
        print(json.dumps({"success": False, "error": "STAGING_REJECTED", "sdk_executed": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
