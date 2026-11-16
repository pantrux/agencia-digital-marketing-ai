"""Plan y construcción SRT única y acotada; sin instalaciones ni escritura de bóveda."""

import argparse
import base64
import csv
import email
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath, PureWindowsPath

sys.dont_write_bytecode = True

OWNER = "agencia-digital-marketing-ai:supoclip-srt-build:v1"
MARKER = ".agency-srt-build-owner.json"
WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPTS = frozenset(
    {
        "srt",
        "srt-deduplicate",
        "srt-normalise",
        "srt-fixed-timeshift",
        "srt-linear-timeshift",
        "srt-lines-matching",
        "srt-mux",
        "srt-play",
        "srt-process",
    }
)
DIST = "srt-3.5.3.dist-info/"
RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])", re.I)
EXPECTED_HELPERS = {
    "job": {
        "path": "sre/windows_build_job.py",
        "sha256": "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44",
    },
    "extractor": {
        "path": "sre/prepare_backup_sdk.py",
        "sha256": "e5a69391db0ea7f4f2c27afa60b20ab31baaf41016d4a25492b1fa8e33393291",
    },
}


class BuildError(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise BuildError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    def constant(_value):
        raise BuildError("JSON_NONFINITE")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def plain(path, *, directory=False):
    path = Path(path).absolute()
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "INPUT_LINK",
        )
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode)
        if directory
        else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "INPUT_TYPE",
    )
    return info


def read(path, maximum):
    info = plain(path)
    return read_leaf(path, maximum, info)


def read_leaf(path, maximum, info):
    require(info.st_size <= maximum, "INPUT_SIZE")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(
            (info.st_dev, info.st_ino, info.st_size)
            == (opened.st_dev, opened.st_ino, opened.st_size),
            "INPUT_CHANGED",
        )
        raw = stream.read(maximum + 1)
    require(len(raw) == info.st_size, "INPUT_CHANGED")
    return raw


def write_new(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def json_new(path, value):
    write_new(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def safe_name(name):
    parts = name.split("/")
    require(
        name
        and not PurePosixPath(name).is_absolute()
        and all(
            part
            and part not in {".", ".."}
            and not part.endswith((".", " "))
            and not RESERVED.fullmatch(part.split(".", 1)[0])
            and not any(ord(char) < 32 or char in '<>:"\\|?*' for char in part)
            for part in parts
        ),
        "ARCHIVE_PATH",
    )
    return name


def inspect_source(raw, expected, expected_sha256):
    require(len(raw) <= 1024**2 and digest(raw) == expected_sha256, "SDIST_PIN")
    wanted = {row["path"]: row for row in expected}
    require(len(wanted) == len(expected) and expected, "SDIST_CATALOGUE")
    files, seen, total = {}, set(), 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as package:
        for index, member in enumerate(package):
            require(
                index < 128
                and (member.isfile() or member.isdir())
                and member.name.startswith("srt-3.5.3"),
                "SDIST_ENTRY",
            )
            if member.name.rstrip("/") == "srt-3.5.3":
                require(member.isdir(), "SDIST_ROOT")
                continue
            require(member.name.startswith("srt-3.5.3/"), "SDIST_ROOT")
            name = safe_name(
                member.name[len("srt-3.5.3/") :].rstrip("/")
                if member.isdir()
                else member.name[len("srt-3.5.3/") :]
            )
            require(name.casefold() not in seen, "SDIST_DUPLICATE")
            seen.add(name.casefold())
            if member.isdir():
                require(member.size == 0, "SDIST_DIRECTORY_SIZE")
                continue
            require(name in wanted and 0 <= member.size <= 1024**2, "SDIST_EXTRA_OR_SIZE")
            total += member.size
            require(total <= 4 * 1024**2, "SDIST_EXPANSION")
            body = package.extractfile(member).read(member.size + 1)
            require(
                len(body) == member.size == wanted[name]["size"]
                and digest(body) == wanted[name]["sha256"],
                "SDIST_FILE_PIN",
            )
            files[name] = body
    require(set(files) == set(wanted), "SDIST_INCOMPLETE")
    return files


def verify_wheel_bytes(raw, source):
    require(0 < len(raw) <= 1024**2, "WHEEL_SIZE")
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        entries = package.infolist()
        names = [entry.filename for entry in entries]
        require(
            len(entries) <= 64
            and len(names) == len(set(names)) == len({n.casefold() for n in names}),
            "WHEEL_DUPLICATE",
        )
        total = 0
        for entry in entries:
            safe_name(entry.filename)
            mode = entry.external_attr >> 16
            require(
                not entry.is_dir()
                and not entry.flag_bits & 1
                and stat.S_IFMT(mode) in {0, stat.S_IFREG}
                and not entry.external_attr & 0x400
                and entry.file_size <= 1024**2,
                "WHEEL_ENTRY",
            )
            total += entry.file_size
        require(total <= 4 * 1024**2, "WHEEL_EXPANSION")
        data = {name: package.read(name) for name in names}
    metadata = email.message_from_bytes(data[DIST + "METADATA"])
    require(
        metadata.get_all("Name") == ["srt"]
        and metadata.get_all("Version") == ["3.5.3"]
        and not metadata.get_all("Requires-Dist"),
        "WHEEL_METADATA",
    )
    wheel = email.message_from_bytes(data[DIST + "WHEEL"])
    tags = wheel.get_all("Tag", [])
    require(
        wheel.get_all("Root-Is-Purelib") == ["true"]
        and tags
        and len(tags) == len(set(tags))
        and set(tags) <= {"py2-none-any", "py3-none-any"}
        and "py3-none-any" in tags,
        "WHEEL_TAGS",
    )
    libraries = {"srt.py", "srt_tools/utils.py"}
    optional_init = {"srt_tools/__init__.py"} if "srt_tools/__init__.py" in data else set()
    for name in libraries | optional_init:
        require(data[name] == source[name], "WHEEL_SOURCE_CHANGED")
    licenses = set(data) & {DIST + "LICENSE", DIST + "licenses/LICENSE"}
    require(len(licenses) == 1 and data[next(iter(licenses))] == source["LICENSE"], "WHEEL_LICENSE")
    scripts = {"srt-3.5.3.data/scripts/" + name for name in SCRIPTS}
    script_line_endings = set()
    for name in scripts:
        original = source["srt_tools/" + name.rsplit("/", 1)[-1]]
        expected = original.replace(b"#!/usr/bin/env python\n", b"#!python\n", 1)
        if data.get(name) == expected:
            script_line_endings.add("LF")
        elif data.get(name) == expected.replace(b"\n", b"\r\n"):
            script_line_endings.add("CRLF")
        else:
            raise BuildError("WHEEL_SCRIPT_CHANGED_OR_MISSING")
    require(len(script_line_endings) == 1, "WHEEL_MIXED_SCRIPT_LINE_ENDINGS")
    allowed = (
        libraries
        | optional_init
        | licenses
        | scripts
        | {DIST + n for n in ["METADATA", "WHEEL", "RECORD", "top_level.txt"]}
    )
    require(
        set(data) == allowed
        and set(data[DIST + "top_level.txt"].decode().split()) == {"srt", "srt_tools"},
        "WHEEL_EXTRA_OR_MISSING",
    )
    rows = list(csv.reader(io.StringIO(data[DIST + "RECORD"].decode("utf-8"))))
    require(
        all(len(row) == 3 for row in rows)
        and len(rows) == len({row[0] for row in rows})
        and {row[0] for row in rows} == set(data),
        "WHEEL_RECORD_COVERAGE",
    )
    for name, checksum, size in rows:
        if name == DIST + "RECORD":
            require(checksum == size == "", "WHEEL_RECORD_SELF")
            continue
        expected_hash = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(data[name]).digest()
        ).decode().rstrip("=")
        require(
            checksum == expected_hash and size == str(len(data[name])), "WHEEL_RECORD_HASH_OR_SIZE"
        )
    return {
        "name": "srt",
        "version": "3.5.3",
        "tags": tags,
        "scripts": len(scripts),
        "script_line_endings": next(iter(script_line_endings)),
        "members": len(data),
        "sha256": digest(raw),
        "bytes": len(raw),
        "record_complete": True,
        "installed": False,
    }


def make_environment(root):
    root = Path(root)
    return {
        "SystemRoot": "C:\\Windows",
        "WINDIR": "C:\\Windows",
        "PATH": "C:\\Windows\\System32",
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "USERPROFILE": str(root / "profile"),
        "HOME": str(root / "profile"),
        "APPDATA": str(root / "profile/roaming"),
        "LOCALAPPDATA": str(root / "profile/local"),
        "SOURCE_DATE_EPOCH": "1704067200",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SETUPTOOLS_USE_DISTUTILS": "local",
    }


def accept_job_result(result):
    require(
        result["status"] == "completed"
        and result["exit_code"] == 0
        and result["cleanup_complete"] is True,
        "BUILD_FAILED_LOGS_PRESERVED",
    )


def load_helper(info):
    path = WORKSPACE / info["path"]
    require(digest(read(path, 1024**2)) == info["sha256"], "HELPER_PIN")
    name = "srt_fixed_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def selected_sdk_files(root, kind):
    plain(root, directory=True)
    result = []
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        directory = Path(directory)
        info = directory.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "SDK_DIRECTORY",
        )
        if kind == "base" and directory == root:
            subdirectories[:] = [name for name in subdirectories if name in {"Lib", "DLLs"}]
        if kind == "base" and directory == root / "Lib":
            subdirectories[:] = [name for name in subdirectories if name != "site-packages"]
        for child in subdirectories:
            info = (directory / child).lstat()
            require(
                stat.S_ISDIR(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & 0x400,
                "SDK_DIRECTORY",
            )
        for name in sorted(filenames):
            path = directory / name
            if kind == "base" and directory == root and path.suffix.lower() not in {".exe", ".dll"}:
                continue
            info = path.lstat()
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and not getattr(info, "st_file_attributes", 0) & 0x400,
                "SDK_FILE",
            )
            require(info.st_size <= 32 * 1024**2, "SDK_FILE_SIZE")
            result.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": info.st_size,
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "sha256": digest(read_leaf(path, 32 * 1024**2, info)),
                }
            )
    require(len(result) <= 5000, "SDK_FILE_COUNT")
    return sorted(result, key=lambda item: item["path"])


ACL_SCRIPT = r"""
$ErrorActionPreference='Stop'
$request=[Console]::In.ReadToEnd()|ConvertFrom-Json
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
try {
 $allowed=@($identity.User.Value,'S-1-5-18','S-1-5-32-544')
 foreach($row in $request.paths) {
  $item=Get-Item -LiteralPath $row.path -Force
  if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'ACL_REPARSE'}
  $acl=Get-Acl -LiteralPath $item.FullName
  if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed){throw 'ACL_OWNER'}
  foreach($ace in $acl.Access) {
   if($ace.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
    $sid=$ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if($sid -notin $allowed -and ($row.private -or ([int]$ace.FileSystemRights -band 852310))){throw 'ACL_FOREIGN'}
   }
  }
 }
 @{checked=$request.paths.Count;valid=$true}|ConvertTo-Json -Compress
}finally{$identity.Dispose()}
"""


def check_acl_batch(paths):
    require(os.name == "nt", "WINDOWS_REQUIRED")
    powershell = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    # WinSxS mantiene hardlinks del ejecutable del sistema; no es un artefacto
    # adoptable de la aplicación. La ruta literal pertenece a la frontera del SO.
    plain(powershell.parent, directory=True)
    tool_info = powershell.lstat()
    require(
        stat.S_ISREG(tool_info.st_mode) and not getattr(tool_info, "st_file_attributes", 0) & 0x400,
        "ACL_SYSTEM_TOOL",
    )
    unique = {}
    for row in paths:
        key = os.path.normcase(row["path"])
        unique[key] = {
            "path": row["path"],
            "private": row["private"] or unique.get(key, {}).get("private", False),
        }
    paths = list(unique.values())
    request = json.dumps({"paths": paths}, ensure_ascii=True).encode()
    require(len(request) <= 2 * 1024**2, "ACL_INPUT_LIMIT")
    result = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ACL_SCRIPT],
        input=request,
        capture_output=True,
        timeout=45,
        env={
            "SystemRoot": "C:\\Windows",
            "WINDIR": "C:\\Windows",
            "PSModulePath": str(powershell.parent / "Modules"),
            "PATH": "C:\\Windows\\System32",
        },
        check=False,
    )
    require(
        result.returncode == 0 and len(result.stdout) <= 65536 and len(result.stderr) <= 65536,
        "ACL_REJECTED",
    )
    value = strict_json(result.stdout)
    require(value == {"checked": len(paths), "valid": True}, "ACL_COVERAGE")


def validate_marker(root, raw, expected, expected_sha256):
    info = plain(root, directory=True)
    marker = strict_json(raw)
    require(
        marker == expected
        and marker["owner"] == OWNER
        and marker["root"] == str(root)
        and (info.st_dev, info.st_ino) == (marker["device"], marker["inode"])
        and digest(raw) == expected_sha256,
        "ROOT_IDENTITY",
    )


def require_plan_pins(dto, manifest_sha256, plan_sha256):
    require(
        manifest_sha256 == dto["manifest_sha256"] and plan_sha256 == dto["plan_sha256"],
        "EXTERNAL_PINS_REQUIRED",
    )


def validate_manifest_contract(manifest):
    require(manifest["schema"] == 1 and manifest["owner"] == OWNER, "MANIFEST_CONTRACT")
    require(
        manifest["source"]["name"] == "srt"
        and manifest["source"]["version"] == "3.5.3"
        and manifest["source"]["sha256"]
        == "4884315043a4f0740fd1f878ed6caa376ac06d70e135f306a6dc44632eed0cc0"
        and manifest["helpers"] == EXPECTED_HELPERS,
        "SOURCE_OR_HELPER_CONTRACT",
    )
    require(
        manifest["limits"]
        == {
            "seconds": 180,
            "processes": 8,
            "memory_bytes": 2 * 1024**3,
            "output_bytes": 1024**2,
            "archive_members": 128,
            "archive_file_bytes": 1024**2,
            "archive_total_bytes": 4 * 1024**2,
        },
        "LIMITS_CONTRACT",
    )
    roots = manifest["sdk"]["roots"]
    venvs = [row for row in roots if row["kind"] == "venv"]
    require(
        len(roots) == 2 and len(venvs) == 1 and sum(row["kind"] == "base" for row in roots) == 1,
        "SDK_ROOTS_CONTRACT",
    )
    venv = PureWindowsPath(venvs[0]["root"])
    require(
        venv == PureWindowsPath("C:/GPU-Bridge/immich/build-venv")
        and PureWindowsPath(manifest["sdk"]["python"]) == venv / "Scripts/python.exe",
        "SDK_EXECUTABLE_CONTRACT",
    )


def inspect_context(manifest, *, verify_sdk=True):
    validate_manifest_contract(manifest)
    require(
        os.name == "nt" and manifest["schema"] == 1 and manifest["owner"] == OWNER,
        "PLATFORM_OR_OWNER",
    )
    root = Path(manifest["root"])
    require(root == WORKSPACE / ".tools/supoclip-srt-build-v1", "ROOT_PATH")
    info = plain(root, directory=True)
    marker_raw = read(root / MARKER, 4096)
    validate_marker(root, marker_raw, manifest["root_identity"], manifest["marker_sha256"])
    archive = read(WORKSPACE / manifest["source"]["cached_path"], 1024**2)
    inspect_source(archive, manifest["source"]["files"], manifest["source"]["sha256"])
    acl = [{"path": str(root), "private": True}, {"path": str(root / MARKER), "private": True}]
    for helper in manifest["helpers"].values():
        require(digest(read(WORKSPACE / helper["path"], 1024**2)) == helper["sha256"], "HELPER_PIN")
    backend = Path(__file__).with_name("srt_build_backend.py")
    require(digest(read(backend, 65536)) == manifest["backend_sha256"], "BACKEND_PIN")
    if verify_sdk:
        for sdk in manifest["sdk"]["roots"]:
            sdk_root = Path(sdk["root"])
            st = plain(sdk_root, directory=True)
            require((st.st_dev, st.st_ino) == (sdk["device"], sdk["inode"]), "SDK_ROOT_IDENTITY")
            files = selected_sdk_files(sdk_root, sdk["kind"])
            require(files == sdk["files"], "SDK_CHANGED")
            acl.append({"path": str(sdk_root), "private": sdk["kind"] == "venv"})
            acl.extend(
                {"path": str(sdk_root / row["path"]), "private": sdk["kind"] == "venv"}
                for row in files
            )
            directories = {
                parent
                for row in files
                for parent in (sdk_root / row["path"]).parents
                if parent == sdk_root or sdk_root in parent.parents
            }
            acl.extend(
                {"path": str(parent), "private": sdk["kind"] == "venv"} for parent in directories
            )
        sdk_sca = manifest["sdk"]["sca"]
        report = read(WORKSPACE / sdk_sca["path"], 1024**2)
        require(digest(report) == sdk_sca["sha256"], "SDK_SCA_PIN")
        parsed = strict_json(report)
        require(
            len(parsed["dependencies"]) == 8
            and {r["name"]: r["version"] for r in parsed["dependencies"]}
            == manifest["sdk"]["audited_tools"]
            and all(not row["vulns"] for row in parsed["dependencies"]),
            "SDK_SCA_COVERAGE",
        )
    prepared_path = root / "prepare.json"
    prepared_sha256 = digest(read(prepared_path, 1024**2)) if prepared_path.exists() else None
    allowed = (
        {MARKER}
        if not prepared_sha256
        else {
            MARKER,
            "source",
            "wheels",
            "tmp",
            "profile",
            "backend.py",
            "prepare.json",
            "attempt.json",
            "build.stdout.log",
            "build.stderr.log",
            "build-result.json",
            "wheel-receipt.json",
        }
    )
    require({p.name for p in root.iterdir()} <= allowed, "FOREIGN_ROOT_ENTRY")
    check_acl_batch(acl)
    return {
        "root": str(root),
        "device": info.st_dev,
        "inode": info.st_ino,
        "marker_sha256": digest(marker_raw),
        "prepared_sha256": prepared_sha256,
        "attempt_exists": (root / "attempt.json").exists(),
    }


def plan(manifest_path=None):
    manifest_path = (
        Path(manifest_path) if manifest_path else Path(__file__).with_name("srt-build-inputs.json")
    )
    raw = read(manifest_path, 2 * 1024**2)
    manifest = strict_json(raw)
    context = inspect_context(manifest)
    dto = {
        "schema": 1,
        "status": "plan",
        "manifest_sha256": digest(raw),
        "context": context,
        "controller_sha256": digest(read(Path(__file__), 128 * 1024)),
        "backend_sha256": manifest["backend_sha256"],
        "source_sha256": manifest["source"]["sha256"],
        "timeout_seconds": 180,
        "output_limit": 1024**2,
        "process_limit": 8,
        "memory_bytes": 2 * 1024**3,
        "build_executed": False,
    }
    dto["plan_sha256"] = digest(json.dumps(dto, sort_keys=True, separators=(",", ":")).encode())
    return manifest, dto


def verify_prepared(manifest, root):
    prepared = strict_json(read(root / "prepare.json", 1024**2))
    require(
        prepared["owner"] == OWNER and prepared["source_sha256"] == manifest["source"]["sha256"],
        "PREPARED_IDENTITY",
    )
    actual = selected_sdk_files(root / "source", "source")
    expected = [
        {"path": row["path"], "size": row["size"], "sha256": row["sha256"]}
        for row in manifest["source"]["files"]
    ]
    require(
        [{k: row[k] for k in ("path", "size", "sha256")} for row in actual] == expected,
        "PREPARED_SOURCE_CHANGED",
    )
    require(
        digest(read(root / "backend.py", 65536)) == manifest["backend_sha256"],
        "PREPARED_BACKEND_CHANGED",
    )


def prepare(manifest):
    root = Path(manifest["root"])
    require({p.name for p in root.iterdir()} == {MARKER}, "PREPARE_NOT_EMPTY")
    extractor = load_helper(manifest["helpers"]["extractor"])
    content = read(WORKSPACE / manifest["source"]["cached_path"], 1024**2)
    inventory = extractor.extract_archive(
        content,
        "tar.gz",
        "srt-3.5.3",
        root / "source",
        max_bytes=4 * 1024**2,
        max_file=1024**2,
        max_members=128,
    )
    require(inventory == manifest["source"]["files"], "EXTRACTED_SOURCE_CHANGED")
    for name in ("wheels", "tmp", "profile", "profile/local", "profile/roaming"):
        (root / name).mkdir(mode=0o700)
    write_new(root / "backend.py", read(Path(__file__).with_name("srt_build_backend.py"), 65536))
    result = {
        "owner": OWNER,
        "source_sha256": manifest["source"]["sha256"],
        "source_files": len(inventory),
        "backend_sha256": manifest["backend_sha256"],
        "build_executed": False,
    }
    json_new(root / "prepare.json", result)
    return result


def build(manifest, plan_dto):
    root = Path(manifest["root"])
    verify_prepared(manifest, root)
    require(
        not any((root / "wheels").iterdir()) and not (root / "attempt.json").exists(),
        "BUILD_ALREADY_ATTEMPTED",
    )
    job = load_helper(manifest["helpers"]["job"])
    arguments = [
        "-I",
        "-B",
        "-X",
        "utf8",
        str(root / "backend.py"),
        str(root / "source"),
        str(root / "wheels"),
    ]
    json_new(
        root / "attempt.json",
        {
            "owner": OWNER,
            "plan_sha256": plan_dto["plan_sha256"],
            "source_sha256": manifest["source"]["sha256"],
            "arguments": arguments,
        },
    )
    result = job.run_job(
        Path(manifest["sdk"]["python"]),
        arguments,
        cwd=root,
        env=make_environment(root),
        timeout=180,
        limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
        output_limit=1024**2,
    )
    write_new(root / "build.stdout.log", result.stdout)
    write_new(root / "build.stderr.log", result.stderr)
    record = asdict(result)
    record.pop("stdout")
    record.pop("stderr")
    record.update(
        {
            "stdout_sha256": digest(result.stdout),
            "stderr_sha256": digest(result.stderr),
            "plan_sha256": plan_dto["plan_sha256"],
        }
    )
    json_new(root / "build-result.json", record)
    accept_job_result(record)
    return verify_output(manifest, root, publish=True)


def verify_output(manifest, root, *, publish=False):
    result = strict_json(read(root / "build-result.json", 65536))
    accept_job_result(result)
    paths = list((root / "wheels").iterdir())
    require(len(paths) == 1 and paths[0].name == "srt-3.5.3-py3-none-any.whl", "WHEEL_OUTPUT_SET")
    files = inspect_source(
        read(WORKSPACE / manifest["source"]["cached_path"], 1024**2),
        manifest["source"]["files"],
        manifest["source"]["sha256"],
    )
    receipt = verify_wheel_bytes(read(paths[0], 1024**2), files)
    receipt.update(
        {
            "owner": OWNER,
            "wheel_path": str(paths[0]),
            "source_sha256": manifest["source"]["sha256"],
            "plan_sha256": result["plan_sha256"],
            "sdk_cleanup_complete": True,
            "source_sdk_sca_only": True,
            "wheel_final_sca_pending": True,
        }
    )
    if publish:
        json_new(root / "wheel-receipt.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", nargs="?", choices=["plan", "prepare", "build", "verify"], default="plan"
    )
    parser.add_argument("--manifest")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        manifest, dto = plan(args.manifest)
        if args.action != "plan":
            require_plan_pins(dto, args.manifest_sha256, args.plan_sha256)
            if args.action == "prepare":
                dto = prepare(manifest)
            elif args.action == "build":
                dto = build(manifest, dto)
            else:
                dto = verify_output(manifest, Path(manifest["root"]))
        print(json.dumps(dto, sort_keys=True))
        return 0
    except (
        BuildError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        tarfile.TarError,
        zipfile.BadZipFile,
        subprocess.TimeoutExpired,
    ) as error:
        reason = str(error) if isinstance(error, BuildError) else "INPUT_IO_OR_FORMAT_REJECTED"
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "SRT_BUILD_REJECTED",
                    "reason": reason,
                    "logs_preserved": True,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
