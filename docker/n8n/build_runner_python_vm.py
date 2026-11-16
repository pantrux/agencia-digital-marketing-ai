"""Construye en la VM el entorno Python fijado del runner de n8n."""

import base64
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
from email.parser import BytesParser
from pathlib import Path

OWNER = "agencia-digital-marketing-ai:n8n-runner-python-build:v1"
WORK = Path("/opt/media-platform/n8n-runner-python-build-v1")
CONTEXT = WORK / "context"
PAYLOAD = WORK / "payload"
BASE_DIGEST = "sha256:d76f585fb0d5107dd47143da5e61554fd9cae656a30f3470f815cb8475769b6a"
BASE = "docker.io/library/python:3.13.18-alpine3.24@" + BASE_DIGEST
MANIFEST = "a22d04112127fccac7a04981c7ef96a7fd9db53c88205ce9094170213fec07e2"
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "DOCKER_CONFIG": str(WORK / "docker-client"),
    "BUILDX_GIT_INFO": "false",
    "BUILDX_GIT_LABELS": "false",
    "BUILDX_GIT_CHECK_DIRTY": "false",
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(path):
    n = path.lstat()
    assert stat.S_ISDIR(n.st_mode) and (n.st_uid, n.st_gid) == (0, 0) and not n.st_mode & 0o022
    return {"device": n.st_dev, "inode": n.st_ino}


def run(args, seconds=15, check=True):
    result = subprocess.run(
        DOCKER + args, env=ENV, capture_output=True, timeout=seconds, check=False
    )
    assert len(result.stdout) + len(result.stderr) <= 1024**2
    if check and result.returncode:
        raise RuntimeError("DOCKER_" + args[0].upper() + "_" + str(result.returncode))
    return result


def host(req):
    assert os.geteuid() == 0 and os.uname().nodename == "vm-media-01"
    assert identity(WORK.parent) == req["parent"]
    fmt = '{"ID":{{json .ID}},"ServerVersion":{{json .ServerVersion}},"DockerRootDir":{{json .DockerRootDir}}}'
    assert json.loads(run(["info", "--format", fmt]).stdout) == req["daemon"]
    fs = os.statvfs(WORK.parent)
    assert fs.f_bavail * fs.f_frsize >= 10 * 1024**3
    assert b"v0.38.1" in run(["buildx", "version"]).stdout


def manifest(raw):
    assert sha(raw) == MANIFEST
    entries = {}
    for line in raw.decode().splitlines():
        digest, name = line.split("  ", 1)
        assert re.fullmatch(r"[A-Za-z0-9_.@/-]+", name) and name not in entries
        assert all(part not in {"", ".", "..", ".git"} for part in name.split("/"))
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        entries[name] = digest
    assert len(entries) == 57
    return entries


def read_tar(raw):
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for row in archive:
            assert row.isfile() and row.sparse is None and row.name not in files
            assert 0 <= row.size <= 3 * 1024**2 and len(files) < 58
            total += row.size
            assert total < 8 * 1024**2
            with archive.extractfile(row) as stream:
                files[row.name] = stream.read()
    expected = manifest(files["SHA256SUMS"])
    assert {name: sha(data) for name, data in files.items() if name != "SHA256SUMS"} == expected
    return files


def image():
    row = json.loads(run(["image", "inspect", BASE], 15).stdout)[0]
    assert row["Architecture"] == "amd64" and row["Os"] == "linux"
    assert any(value.split("@")[-1] == BASE_DIGEST for value in row["RepoDigests"])
    return {"id": row["Id"], "layers": row["RootFS"]["Layers"], "size": row["Size"]}


def prepare(req, out):
    raw = base64.b64decode(req["archive"], validate=True)
    assert sha(raw) == req["archive_sha256"] and len(raw) < 5 * 1024**2
    files = read_tar(raw)
    assert files["Dockerfile"].splitlines()[0] == ("FROM " + BASE + " AS build").encode()
    WORK.mkdir(mode=0o700)
    out["directory"] = identity(WORK)
    (WORK / "docker-client").mkdir(mode=0o700)
    CONTEXT.mkdir(mode=0o700)
    for name, data in files.items():
        dest = CONTEXT / name
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with dest.open("xb") as target:
            target.write(data)
    out["inputs"] = len(files)
    out["context_sha256"] = req["archive_sha256"]
    out["step"] = "pull_base"
    proc = run(["image", "pull", "--platform", "linux/amd64", BASE], 120, check=False)
    out["pull_exit"] = proc.returncode
    out["diagnostic"] = proc.stderr.decode(errors="replace")[-2000:]
    assert proc.returncode == 0
    out["base"] = image()


def check_context():
    expected = manifest((CONTEXT / "SHA256SUMS").read_bytes())
    paths = {p.relative_to(CONTEXT).as_posix(): p for p in CONTEXT.rglob("*") if not p.is_dir()}
    assert set(paths) == {*expected, "SHA256SUMS"}
    for name, digest in expected.items():
        p = paths[name]
        n = p.lstat()
        assert stat.S_ISREG(n.st_mode) and n.st_uid == n.st_gid == 0 and n.st_nlink == 1
        assert sha(p.read_bytes()) == digest


def verify_payload():
    runner = PAYLOAD / "opt/runners/task-runner-python"
    venv = runner / ".venv"
    site = venv / "lib/python3.13/site-packages"
    assert (runner / "LICENSE.md").read_bytes() == (CONTEXT / "LICENSE.md").read_bytes()
    assert (venv / "bin/python").read_bytes().startswith(b"\x7fELF")
    (extension,) = site.glob("websockets/speedups.cpython-313-*-linux-musl.so")
    assert extension.read_bytes().startswith(b"\x7fELF")
    observed = {}
    for info in site.glob("*.dist-info/METADATA"):
        metadata = BytesParser().parsebytes(info.read_bytes())
        observed[metadata["Name"].lower().replace("_", "-")] = metadata["Version"]
    assert observed == {
        "task-runner-python": "0.1.0",
        "certifi": "2026.10.23",
        "sentry-sdk": "2.35.2",
        "urllib3": "2.8.0",
        "websockets": "15.0.1",
    }
    rows = []
    total = 0
    for path in sorted(PAYLOAD.rglob("*")):
        n = path.lstat()
        name = path.relative_to(PAYLOAD).as_posix()
        if stat.S_ISLNK(n.st_mode):
            target = os.readlink(path)
            assert not Path(target).is_absolute() and path.resolve(strict=True).is_relative_to(venv)
            rows.append({"path": name, "link": target})
        elif stat.S_ISREG(n.st_mode):
            total += n.st_size
            assert total < 100 * 1024**2 and len(rows) < 4000
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            rows.append(
                {
                    "path": name,
                    "bytes": n.st_size,
                    "sha256": digest,
                    "mode": stat.S_IMODE(n.st_mode),
                }
            )
        else:
            assert stat.S_ISDIR(n.st_mode)
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    with (WORK / "payload-inventory.json").open("xb") as stream:
        stream.write(raw)
    return {
        "packages": observed,
        "inventory_sha256": sha(raw),
        "files_and_links": len(rows),
        "bytes": total,
        "native_websockets_sha256": sha(extension.read_bytes()),
    }


def build(req, out):
    assert identity(WORK) == req["directory"] and image() == req["base"]
    check_context()
    assert not os.path.lexists(PAYLOAD)
    builder = run(["buildx", "inspect", "default"]).stdout.decode()
    assert re.search(r"(?m)^Driver:\s+docker\s*$", builder)
    out["step"] = "build"
    args = [
        "buildx",
        "build",
        "--builder",
        "default",
        "--platform",
        "linux/amd64",
        "--pull=false",
        "--network=none",
        "--provenance=false",
        "--no-cache",
        "--progress=plain",
        "--target",
        "payload",
        "--output",
        f"type=local,dest={PAYLOAD}",
        str(CONTEXT),
    ]
    started = time.monotonic()
    proc = run(args, 180, check=False)
    raw = proc.stdout + proc.stderr
    with (WORK / "build.log").open("xb") as log:
        log.write(raw)
    out["build_exit"] = proc.returncode
    out["build_seconds"] = round(time.monotonic() - started, 3)
    out["build_log_sha256"] = sha(raw)
    if proc.returncode:
        out["diagnostic"] = raw.decode(errors="replace")[-4500:]
    assert proc.returncode == 0 and image() == req["base"]
    out["step"] = "payload"
    out["payload"] = verify_payload()


def main():
    out = {"success": False, "owner": OWNER, "step": "host", "production_approved": False}
    try:
        req = json.loads(sys.stdin.buffer.read(8 * 1024**2))
        action = req["action"]
        out["action"] = action
        host(req)
        if action == "prepare":
            prepare(req, out)
        else:
            assert action == "build"
            build(req, out)
        out["success"] = True
    except Exception as error:
        out["error"] = type(error).__name__
        frame = error.__traceback__
        while frame.tb_next:
            frame = frame.tb_next
        out["line"] = frame.tb_lineno
    print(json.dumps(out, sort_keys=True))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
