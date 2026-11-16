"""Prepara sólo datos locales de Zou; no construye, instala, conecta ni lee .env."""

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
import types
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OWNER = "agencia-digital-marketing-ai:zou-application-context:v1"
WOLFI_BASE = "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72"
PYTHON_IDENTITY = {
    "version": "3.12.16",
    "executable": "usr/bin/python3.12",
    "executable_sha256": "54bb87946dd0d8231507c0cef7a5745443a97411c406bf6f010f3ec75780a75b",
    "libpython": "usr/lib/libpython3.12.so.1.0",
    "libpython_sha256": "ad918e7753f89b9c256a6c40a17e48d080583cd25577d8634d737ab9a1e960b8",
}
GRAPH_SOURCE_SHA = "e7fba27152bdb7478474d363e1d4b8b30dc296986e8e129a08354190b568e609"
ACL_SOURCE_SHA256 = "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b"
NATIVE_SHA = "b00b1a1a1822b13ed145128209664a15f3efa781b39cd850d0d63bcc0666192b"
PACKAGER_SHA = "831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9"
REFERENCES = {
    "wheelhouse": (
        ".tools/evidence/phase4-zou-wheelhouse-audit.json",
        "cc21e186606568b1491d8d2d8fd9ee53a71080acab396256bd56e47fdee2e965",
    ),
    "libpq_plan": (
        ".tools/evidence/phase4-zou-psycopg-libpq-plan.json",
        "7a6bfecc16cb292da365af9d591b853f3aa3980a83df32169fb9198f3d8b102d",
    ),
    "libpq_bundle": (
        ".tools/backup-source-build/zou-libpq-artifacts-v1/bundle-verification.json",
        "ce08d9b49e42f133fe577ad1df954082ebfc38c406dc646331c4c597c3c3932d",
    ),
    "python_inputs": (
        "docker/python-runtime/python-runtime-inputs.json",
        "b15853067c68a6408591f6575aaf351cd426c7787468fc59ba1a9b7f7824f9c9",
    ),
    "zou_receipt": (
        ".tools/kitsu-zou-wheel-derivative-v1/derived-receipt.json",
        "660e313357177c186d5273fc78a7be70518e6a5ce11c82d7de0c4b62502ba9b8",
    ),
    "flasgger_receipt": (
        ".tools/kitsu-zou-flasgger-ui-v1/derived-receipt.json",
        "0fc3304316ed4dee2dc176136a7bcf019cadbf885e0b5e7f003663bc04ee002c",
    ),
    "opencv_receipt": (
        ".tools/backup-source-build/opencv-relocated-wheel-v1/receipt.json",
        "b88343d49decd86328a6d74d4c02b01789c83ed4d12ce7339a81d38eeada3c18",
    ),
}
REPLACEMENTS = {
    "zou": (
        ".tools/kitsu-zou-wheel-derivative-v1/zou-1.1.1+agency1-py3-none-any.whl",
        "41ead8c1a92730d43331be9095ae218f677cd17247c7d922568a0b3d988185c0",
    ),
    "flasgger": (
        ".tools/kitsu-zou-flasgger-ui-v1/flasgger-0.9.7.1+agency1-py2.py3-none-any.whl",
        "db3bb5145d3d86d82a15c3fca4cd72c665873785c3ca143466962b9747a5355e",
    ),
    "opencv-python-headless": (
        ".tools/backup-source-build/opencv-relocated-wheel-v1/wheel/opencv_python_headless-4.13.0.92+agency1-cp312-cp312-linux_x86_64.whl",
        "1c03ca69b42b10ca1ff75bdc64113ca2885d7f502edbba369b9779f2da1b51cd",
    ),
}


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def decode(raw):
    def pairs(rows):
        result = {}
        for name, value in rows:
            require(name not in result, "JSON_DUPLICATE")
            result[name] = value
        return result

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _: require(False, "JSON_CONSTANT")
    )


def plain(path, directory=False):
    path = Path(path).absolute()
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "FILE_LINK",
        )
    require(
        stat.S_ISDIR(info.st_mode)
        if directory
        else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "FILE_TYPE",
    )
    return info


def read(path, maximum, pin=None):
    before = plain(path)
    require(before.st_size <= maximum, "FILE_SIZE")
    with Path(path).open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    final = plain(path)

    def signature(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_nlink,
            stat.S_IFMT(info.st_mode),
        )

    require(
        signature(before) == signature(opened) == signature(after) == signature(final)
        and before.st_ctime_ns == final.st_ctime_ns
        and opened.st_ctime_ns == after.st_ctime_ns
        and len(raw) == before.st_size,
        "FILE_CHANGED",
    )
    require(pin is None or digest(raw) == pin, "FILE_SHA")
    return raw


def frozen_module(name, path, pin):
    raw = read(path, 256 * 1024, pin)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def safe_relative(name, *, wheel_member=False):
    require(
        isinstance(name, str)
        and name.isascii()
        and len(name) <= 240
        and re.fullmatch(r"[A-Za-z0-9_+./ ()-]+" if wheel_member else r"[A-Za-z0-9_+./-]+", name)
        and not name.startswith("/")
        and all(
            part not in {"", ".", ".."} and not part.endswith((".", " "))
            for part in name.split("/")
        ),
        "RELATIVE_PATH",
    )
    return name


def inspect_wheel(raw, filename, pin):
    """Coteja archivo, metadata y RECORD; el contenido se trata sólo como datos."""
    from packaging.utils import canonicalize_name, parse_wheel_filename

    require(digest(raw) == pin and len(raw) <= 96 * 1024**2, "WHEEL_SHA_SIZE")
    package, version, _build, tags = parse_wheel_filename(filename)
    require(
        any(tag.platform == "any" or tag.platform.endswith("_x86_64") for tag in tags),
        "WHEEL_PLATFORM",
    )
    files, seen = {}, set()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(
            not archive.comment
            and len(archive.infolist()) <= 30000
            and sum(row.file_size for row in archive.infolist()) <= 384 * 1024**2,
            "WHEEL_LIMIT",
        )
        for row in archive.infolist():
            name = row.filename.rstrip("/")
            safe_relative(name, wheel_member=True)
            require(
                name.casefold() not in seen
                and not row.flag_bits & 1
                and stat.S_IFMT(row.external_attr >> 16) in {0, stat.S_IFREG, stat.S_IFDIR},
                "WHEEL_MEMBER",
            )
            seen.add(name.casefold())
            if not row.is_dir():
                files[name] = archive.read(row)
    metadata_names = [
        name for name in files if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
    ]
    require(len(metadata_names) == 1, "WHEEL_METADATA")
    metadata_name = metadata_names[0]
    dist = str(PurePosixPath(metadata_name).parent)
    require(
        "/" not in dist and dist + "/RECORD" in files and dist + "/WHEEL" in files, "WHEEL_DIST"
    )
    message = BytesParser().parsebytes(files[metadata_name], headersonly=True)
    require(
        len(message.get_all("Name", [])) == len(message.get_all("Version", [])) == 1
        and canonicalize_name(message["Name"]) == package
        and str(version) == message["Version"],
        "WHEEL_IDENTITY",
    )
    record_name = dist + "/RECORD"
    covered = set()
    for row in csv.reader(io.StringIO(files[record_name].decode("utf-8"), newline="")):
        require(len(row) == 3 and row[0] in files and row[0] not in covered, "RECORD_PATH")
        name, checksum, size = row
        covered.add(name)
        if name == record_name:
            require(checksum == size == "", "RECORD_SELF")
        else:
            value = (
                base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=").decode()
            )
            require(checksum == "sha256=" + value and size == str(len(files[name])), "RECORD_HASH")
    require(covered == set(files), "RECORD_COVERAGE")
    return {
        "name": package,
        "version": str(version),
        "filename": filename,
        "sha256": pin,
        "bytes": len(raw),
        "metadata": files[metadata_name],
    }


def validate_closure(metadata):
    require(not {"psycopg-binary", "psycopg-c"} & metadata.keys(), "BINARY_DRIVER")
    graph = frozen_module(
        "zou_context_frozen_graph", HERE / "derive_zou_wheel.py", GRAPH_SOURCE_SHA
    )
    result = graph.validate_closure(metadata)
    require(set(result["selected"]) == set(metadata), "GRAPH_UNUSED")
    require(
        set(graph.validate_closure(metadata, extras=("prodpg",))["selected"]) == set(metadata)
        if b"Provides-Extra: prodpg" in metadata["zou"]
        else True,
        "GRAPH_PRODPG",
    )
    return result


def runtime_subject(raw, pin):
    require(
        isinstance(pin, str) and re.fullmatch(r"[a-f0-9]{64}", pin) and digest(raw) == pin,
        "CAPTURE_SHA",
    )
    data = decode(raw)
    require(data.get("kind") == "python-runtime-image-sca-inputs-v1", "CAPTURE_KIND")
    subject = data.get("subject", {})
    require(
        all(
            re.fullmatch(r"sha256:[a-f0-9]{64}", str(subject.get(key, "")))
            for key in ("manifest", "config")
        )
        and subject["manifest"] != WOLFI_BASE,
        "CAPTURE_SUBJECT",
    )
    require(
        data.get("coverage")
        == {"installed_apk": 19, "manual_apk_payloads": 14, "total_components": 33},
        "CAPTURE_COVERAGE",
    )
    require(
        all(data.get("python", {}).get(key) == value for key, value in PYTHON_IDENTITY.items()),
        "CAPTURE_PYTHON",
    )
    for path_key, hash_key in (
        ("executable", "executable_sha256"),
        ("libpython", "libpython_sha256"),
    ):
        require(
            data.get("native_files", {}).get(PYTHON_IDENTITY[path_key], {}).get("sha256")
            == PYTHON_IDENTITY[hash_key],
            "CAPTURE_ELF",
        )
    archives = subject.get("archives", [])
    require(
        len(archives) == 2
        and {row.get("name") for row in archives} == {"base.tar", "final.tar"}
        and all(
            re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256", "")))
            and type(row.get("bytes")) is int
            and row["bytes"] > 0
            for row in archives
        ),
        "CAPTURE_ARCHIVES",
    )
    return {**subject, "capture_sha256": pin, "production_approved": False}


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    require(read(path, len(raw), digest(raw)) == raw, "COPY_CHANGED")


def publish_context(target, plan, files, expected_sha256, *, check_private):
    require(
        plan.get("ready") is True and digest(canonical(plan)) == expected_sha256,
        "PLAN_NOT_PINNED_READY",
    )
    require(set(files) == set(plan.get("files", {})), "CONTEXT_COVERAGE")
    for name, raw in files.items():
        safe_relative(name)
        require(plan["files"][name] == {"sha256": digest(raw), "bytes": len(raw)}, "CONTEXT_TAMPER")
    target = Path(target).absolute()
    plain(target.parent, directory=True)
    check_private(target.parent, private=True)
    require(not target.exists(), "OUTPUT_EXISTS")
    target.mkdir(mode=0o700)
    check_private(target, private=True)
    info = target.lstat()
    write_new(
        target / ".owner.json",
        canonical(
            {
                "owner": OWNER,
                "device": info.st_dev,
                "inode": info.st_ino,
                "plan_sha256": expected_sha256,
            }
        ),
    )
    write_new(target / "plan.json", canonical(plan))
    for name, raw in sorted(files.items()):
        path = target.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        plain(path.parent, directory=True)
        write_new(path, raw)
        check_private(path, private=True)
    return {
        "success": True,
        "owner": OWNER,
        "path": str(target),
        "device": info.st_dev,
        "inode": info.st_ino,
        "plan_sha256": expected_sha256,
        "files": len(files),
        "image_built": False,
        "production_approved": False,
    }


def requirements_lock(wheels):
    seen, lines = set(), []
    for row in sorted(wheels, key=lambda item: item["name"]):
        name, version, checksum = row["name"], row["version"], row["sha256"]
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name)
            and name not in seen
            and name not in {"psycopg-binary", "psycopg-c"}
            and re.fullmatch(r"[A-Za-z0-9+_.-]+", version)
            and re.fullmatch(r"[a-f0-9]{64}", checksum),
            "LOCK_IDENTITY",
        )
        seen.add(name)
        lines.append(f"{name}=={version} --hash=sha256:{checksum}")
    return ("\n".join(lines) + "\n").encode()


def wheel_inputs(audit, references):
    rows = audit["files"]
    require(len(rows) == 142 and len({row["name"] for row in rows}) == 142, "BASELINE_WHEELS")
    require(
        references["zou_receipt"]["wheel_sha256"] == REPLACEMENTS["zou"][1]
        and references["flasgger_receipt"].get("success") is True
        and references["flasgger_receipt"]["wheel_sha256"] == REPLACEMENTS["flasgger"][1]
        and references["opencv_receipt"].get("success") is True
        and references["opencv_receipt"]["wheel"]["sha256"]
        == REPLACEMENTS["opencv-python-headless"][1],
        "DERIVATIVE_RECEIPTS",
    )
    output, metadata, selected, originals = {}, {}, [], []
    for row in rows:
        original = Path(row["cache_path"])
        info = plain(original)
        require(
            (str(info.st_dev), str(info.st_ino))
            == (row["identity"]["device"], row["identity"]["inode"]),
            "WHEEL_IDENTITY_CHANGED",
        )
        raw = read(original, 96 * 1024**2, row["sha256"])
        require(len(raw) == row["bytes"], "WHEEL_BYTES")
        originals.append({"name": row["name"], "sha256": row["sha256"]})
        if row["name"] == "psycopg-binary":
            continue
        source = original
        if row["name"] in REPLACEMENTS:
            relative, checksum = REPLACEMENTS[row["name"]]
            source = ROOT / relative
            raw = read(source, 96 * 1024**2, checksum)
        item = inspect_wheel(raw, source.name, digest(raw))
        require(item["name"] == row["name"] and item["name"] not in metadata, "WHEEL_SELECTION")
        metadata[item["name"]] = item.pop("metadata")
        item["source_path"] = str(source)
        selected.append(item)
        output["wheels/" + item["filename"]] = raw
    require(len(selected) == 141 and set(REPLACEMENTS) <= metadata.keys(), "SELECTED_WHEELS")
    return output, selected, validate_closure(metadata), originals


def apk_authenticator():
    """Sólo las funciones de datos congeladas; nunca llama al main histórico."""
    from dataclasses import dataclass
    from types import MappingProxyType

    native = frozen_module(
        "zou_app_native", ROOT / "docker/postgres-immich/native_inputs.py", NATIVE_SHA
    )
    raw = read(HERE / "package_opencv_minimal.py", 128 * 1024, PACKAGER_SHA)
    wanted = {
        "_pinned",
        "AuthenticatedIndex",
        "authenticate_index",
        "VerifiedApk",
        "verify_wolfi_apk",
        "payload_inventory",
    }
    nodes = [
        node
        for node in ast.parse(raw).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted
    ]
    require({node.name for node in nodes} == wanted, "AUTHENTICATOR_AST")
    module = types.ModuleType("zou_app_apk_auth")
    sys.modules[module.__name__] = module
    module.__dict__.update(
        {
            "hashlib": hashlib,
            "base64": base64,
            "io": io,
            "re": re,
            "tarfile": __import__("tarfile"),
            "dataclass": dataclass,
            "MappingProxyType": MappingProxyType,
            "PurePosixPath": PurePosixPath,
            "_verifier": lambda: native,
        }
    )
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]), str(HERE / "package_opencv_minimal.py"), "exec"
        ),
        module.__dict__,
    )
    return module


def apk_inputs(references):
    python = references["python_inputs"]
    source = references["libpq_plan"]
    bundle = references["libpq_bundle"]
    require(
        bundle.get("success") is True
        and len(bundle.get("artifacts", [])) == 8
        and bundle["source_plan"]["sha256"] == REFERENCES["libpq_plan"][1],
        "LIBPQ_BUNDLE",
    )
    index_ref, key_ref = python["references"]["index"], python["references"]["key"]
    index_raw = read(ROOT / index_ref["path"], 16 * 1024**2, index_ref["sha256"])
    key_raw = read(ROOT / key_ref["path"], 16384, key_ref["sha256"])
    auth = apk_authenticator()
    index = auth.authenticate_index(
        index_raw, key_raw, index_sha256=index_ref["sha256"], key_sha256=key_ref["sha256"]
    )
    inputs = {"apk-repo/x86_64/APKINDEX.tar.gz": index_raw, "keys/wolfi-signing.rsa.pub": key_raw}
    selected, seen = [], set()
    candidates = [
        {
            "name": item["name"],
            "version": item["version"],
            "path": item["path"],
            "sha256": item["sha256"],
        }
        for item in bundle["artifacts"]
    ]
    candidates.extend(
        {"name": item["name"], "version": item["version"], **item["archive"]}
        for item in python["manual_apks"]
        if item["name"] in {"gdbm", "sqlite-libs"}
    )
    require(len(candidates) == 10, "APK_CLOSURE_COUNT")
    for item in candidates:
        name, version = item["name"], item["version"]
        require(name not in seen and name in source["closure_artifacts"], "APK_CLOSURE_NAME")
        seen.add(name)
        record = dict(index.records[(name, version)])
        expected = source["closure_artifacts"][name]
        require(
            record == source["runtime_closure"]["packages"][name]
            and expected["sha256"] == item["sha256"],
            "APK_AUTHORITY",
        )
        path = Path(item["path"])
        if not path.is_absolute():
            path = ROOT / path
        raw = read(path, 16 * 1024**2, item["sha256"])
        require(len(raw) == expected["size"], "APK_SIZE")
        verified = auth.verify_wolfi_apk(raw, index, name, version, item["sha256"])
        catalog = auth.payload_inventory(verified)
        if name not in {"gdbm", "sqlite-libs"}:
            prior = next(row for row in bundle["artifacts"] if row["name"] == name)
            require(
                prior["record"] == record and prior["payload_inventory"] == catalog,
                "APK_BUNDLE_DRIFT",
            )
        filename = name + "-" + version + ".apk"
        inputs["apk-repo/x86_64/" + filename] = raw
        selected.append(
            {
                "name": name,
                "version": version,
                "filename": filename,
                "sha256": digest(raw),
                "source_path": str(path),
                "record": record,
                "payload_entries": len(catalog),
                "scripts_executed": False,
            }
        )
    require(seen == set(source["closure_artifacts"]), "APK_EXACT_CLOSURE")
    inputs["repositories"] = b"/inputs/apk-repo\n"
    return inputs, sorted(selected, key=lambda row: row["name"])


def make_plan(capture_path=None, capture_sha256=None):
    require(bool(capture_path) == bool(capture_sha256), "CAPTURE_PAIR")
    references = {
        name: decode(read(ROOT / path, 16 * 1024**2, checksum))
        for name, (path, checksum) in REFERENCES.items()
    }
    wheel_files, wheels, closure, originals = wheel_inputs(references["wheelhouse"], references)
    apk_files, apks = apk_inputs(references)
    inputs = {**wheel_files, **apk_files, "requirements.lock": requirements_lock(wheels)}
    python = references["python_inputs"]
    build_inputs = {
        "kind": "zou-application-build-inputs-v1",
        "platform": "linux/amd64",
        "base_packages": {name: row["V"] for name, row in python["base"]["packages"].items()},
        "wheels": [
            {key: row[key] for key in ("name", "version", "filename", "sha256")} for row in wheels
        ],
        "apk_packages": [
            {key: row[key] for key in ("name", "version", "filename", "sha256")} for row in apks
        ],
        "files": {
            name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in sorted(inputs.items())
        },
    }
    require(len(build_inputs["files"]) == 155, "BUILD_MANIFEST_COVERAGE")
    files = {
        **inputs,
        "build-inputs.json": canonical(build_inputs),
        "launch_zou.py": read(HERE / "launch_zou.py", 128 * 1024),
        "Dockerfile": read(HERE / "Dockerfile.application", 16384),
    }
    subject, capture_ref = None, None
    if capture_path:
        path = Path(capture_path).absolute()
        require(path.is_relative_to(ROOT / ".tools"), "CAPTURE_PATH")
        raw = read(path, 8 * 1024**2, capture_sha256)
        subject = runtime_subject(raw, capture_sha256)
        capture_ref = {"path": str(path), "sha256": capture_sha256, "bytes": len(raw)}
    plan = {
        "schema_version": 1,
        "owner": OWNER,
        "operation": "local-data-context-only",
        "ready": subject is not None,
        "pending": [] if subject else ["Root-pinned physical Python runtime capture"],
        "runtime_subject": subject,
        "runtime_capture": capture_ref,
        "named_build_context": {
            "alias": "pythonruntime",
            "manifest": subject["manifest"] if subject else None,
            "config": subject["config"] if subject else None,
            "local_oci_required": True,
        },
        "references": {
            name: {"path": path, "sha256": checksum}
            for name, (path, checksum) in REFERENCES.items()
        },
        "sources": {
            name: digest(read(HERE / name, 256 * 1024))
            for name in (
                "prepare_application_context.py",
                "launch_zou.py",
                "Dockerfile.application",
            )
        },
        "verifiers": {
            "closure": GRAPH_SOURCE_SHA,
            "apk_packager": PACKAGER_SHA,
            "native_apk": NATIVE_SHA,
            "acl": ACL_SOURCE_SHA256,
        },
        "wheels": wheels,
        "closure": closure,
        "original_wheels": originals,
        "apks": apks,
        "requirements_sha256": digest(inputs["requirements.lock"]),
        "files": {
            name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in sorted(files.items())
        },
        "build_contract": {
            "network": "none",
            "pull": False,
            "push": False,
            "git_environment": {
                "BUILDX_GIT_INFO": "false",
                "BUILDX_GIT_LABELS": "false",
                "BUILDX_GIT_CHECK_DIRTY": "false",
            },
            "apk_scripts": False,
            "apk_commit_hooks": False,
            "allow_untrusted": False,
            "ldconfig_explicit": True,
            "pip_dependencies_resolved_during_build": False,
            "expected_installed_apk_after_build": 29,
            "remaining_manual_python_components": 12,
        },
        "runtime_contract": {
            "uid": 1000,
            "gid": 1000,
            "python_isolated": True,
            "python_no_site": True,
            "site": "/opt/zou/site",
            "api_port": 5000,
            "events_port": 5001,
            "api_workers": 1,
            "events_workers": 1,
            "redis_required": True,
            "secret_paths": [
                "/run/secrets/zou_db_password",
                "/run/secrets/zou_kv_password",
                "/run/secrets/zou_secret_key",
            ],
        },
        "limits": [
            "OpenCV external ELF providers and media/FFmpeg remain separate functional acceptance.",
            "No app image, imports, libpq loader, migrations, login, Redis or browser executed by this preparation.",
        ],
        "image_built": False,
        "sdk_executed": False,
        "credentials_read": False,
        "runtime_approved": False,
        "production_approved": False,
        "sca_approved": False,
    }
    return plan, files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-capture")
    parser.add_argument("--runtime-capture-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument(
        "--output", default=str(ROOT / ".tools/backup-source-build/zou-application-context-v1")
    )
    args = parser.parse_args(argv)
    try:
        plan, files = make_plan(args.runtime_capture, args.runtime_capture_sha256)
        plan_sha256 = digest(canonical(plan))
        if args.apply:
            require(
                plan["ready"] is True and args.expected_plan_sha256 == plan_sha256,
                "PLAN_NOT_PINNED_READY",
            )
            output = Path(args.output).absolute()
            require(output.parent == ROOT / ".tools/backup-source-build", "OUTPUT_PARENT")
            read(
                output.parent / ".agency-backup-build-owner",
                128,
                "78925844fd312b9ce3be220ee9d2fe41442908101af8c14416895811110bcff6",
            )
            access = frozen_module(
                "zou_context_acl", ROOT / "sre/backup_tools_access.py", ACL_SOURCE_SHA256
            )
            result = publish_context(
                output, plan, files, plan_sha256, check_private=access.check_access
            )
            write_new(output / "receipt.json", canonical(result))
        else:
            result = {"success": True, "plan_sha256": plan_sha256, "plan": plan}
        print(canonical(result).decode())
        return 0
    except Exception as error:
        print(
            canonical(
                {
                    "success": False,
                    "ready": False,
                    "error": "ZOU_CONTEXT_REJECTED",
                    "error_type": type(error).__name__,
                    "code": str(error)[:120]
                    if isinstance(error, ValueError) and re.fullmatch(r"[A-Z_]+", str(error))
                    else None,
                }
            ).decode()
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
