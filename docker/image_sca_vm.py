"""Descarga el escáner fijado y revisa imágenes locales sin arrancarlas."""

import base64
import collections
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

OWNER = "agencia-digital-marketing-ai:sca-trivy:v1"
WORK = Path("/opt/media-platform/sca-trivy-v1")
CACHE = WORK / "cache"
TRIVY = WORK / "trivy"
TAR_SHA = "bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf"
RELEASE = "https://github.com/aquasecurity/trivy/releases/download/v0.76.2/trivy_0.76.2_Linux-64bit.tar.gz"
DB_SOURCE = "ghcr.io/aquasecurity/trivy-db:2"
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "DOCKER_CONFIG": str(WORK / "docker-client"),
    "XDG_CONFIG_HOME": str(WORK / "config"),
    "TMPDIR": str(WORK / "tmp"),
    "GOMEMLIMIT": "1GiB",
}
OPTIONS = [
    "--cache-dir",
    str(CACHE),
    "--skip-version-check",
    "--disable-telemetry",
    "--skip-vex-repo-update",
    "--config",
    "",
    "--ignorefile",
    "",
    "--no-progress",
]


def node(path, kind, mode=None):
    value = path.lstat()
    assert kind(value.st_mode) and value.st_uid == value.st_gid == 0
    assert mode is None or stat.S_IMODE(value.st_mode) == mode
    assert kind is not stat.S_ISREG or value.st_nlink == 1
    return {"device": value.st_dev, "inode": value.st_ino}


def sha(path):
    node(path, stat.S_ISREG)
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run(command, seconds, *, check=True):
    result = subprocess.run(command, env=ENV, timeout=seconds, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(f"COMMAND_{Path(command[0]).name}_{result.returncode}")
    return result


def host(req):
    assert os.geteuid() == 0 and os.uname().nodename == "vm-media-01"
    assert node(WORK.parent, stat.S_ISDIR) == req["parent"]
    fmt = '{"ID":{{json .ID}},"ServerVersion":{{json .ServerVersion}},"DockerRootDir":{{json .DockerRootDir}}}'
    assert json.loads(run(DOCKER + ["info", "--format", fmt], 15).stdout) == req["daemon"]
    fs = os.statvfs("/var/lib/docker")
    assert fs.f_bavail * fs.f_frsize >= 15 * 1024**3


def database():
    directory = CACHE / "db"
    node(directory, stat.S_ISDIR)
    metadata_path = directory / "metadata.json"
    db = directory / "trivy.db"
    metadata = json.loads(metadata_path.read_bytes())
    assert metadata["Version"] == 2
    now = datetime.now(UTC)
    assert datetime.fromisoformat(metadata["UpdatedAt"]) <= now
    assert now < datetime.fromisoformat(metadata["NextUpdate"])
    assert 0 < db.stat().st_size < 3 * 1024**3
    return {
        "metadata": metadata,
        "metadata_sha256": sha(metadata_path),
        "sha256": sha(db),
        "bytes": db.stat().st_size,
        "physical": node(db, stat.S_ISREG),
    }


def install(out):
    WORK.mkdir(mode=0o700)
    out["directory"] = node(WORK, stat.S_ISDIR, 0o700)
    for path in (CACHE, WORK / "tmp", WORK / "docker-client", WORK / "config"):
        path.mkdir(mode=0o700)
    with (WORK / ".agency-owner").open("x", encoding="utf-8") as stream:
        stream.write(OWNER)
    archive = WORK / "release.tar.gz"
    out["step"] = "release"
    run(
        [
            "/usr/bin/curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-time",
            "100",
            "--max-filesize",
            str(120 * 1024**2),
            "--output",
            str(archive),
            RELEASE,
        ],
        105,
    )
    assert sha(archive) == TAR_SHA
    with tarfile.open(archive, "r:gz") as tar:
        members = [member for member in tar if member.name == "trivy"]
        assert len(members) == 1 and members[0].isfile() and members[0].size <= 200 * 1024**2
        with tar.extractfile(members[0]) as source, TRIVY.open("xb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
    TRIVY.chmod(0o500)
    out["tool"] = {
        "sha256": sha(TRIVY),
        "physical": node(TRIVY, stat.S_ISREG, 0o500),
        "release_sha256": TAR_SHA,
    }
    assert b"Version: 0.76.2" in run([str(TRIVY), "--version"], 8).stdout
    out["step"] = "database"
    result = run(
        [
            str(TRIVY),
            "image",
            *OPTIONS,
            "--db-repository",
            DB_SOURCE,
            "--download-db-only",
            "--timeout",
            "2m",
        ],
        130,
        check=False,
    )
    out["db_exit"] = result.returncode
    out["db_diagnostic"] = result.stderr.decode(errors="replace")[-3000:]
    assert result.returncode == 0
    out["database"] = database()


def reference(value):
    assert re.fullmatch(
        r"(?:ghcr\.io|quay\.io|docker\.io)/[a-z0-9._/-]+@sha256:[a-f0-9]{64}", value
    )
    return value


def image(ref):
    row = json.loads(run(DOCKER + ["image", "inspect", ref], 20).stdout)[0]
    assert ref in row["RepoDigests"] and row["Architecture"] == "amd64" and row["Os"] == "linux"
    return {"id": row["Id"], "size": row["Size"], "layers": row["RootFS"]["Layers"]}


def pull(req, out):
    for ref in req["references"]:
        ref = reference(ref)
        out["reference"] = ref
        pull_cmd = DOCKER + ["image", "pull", "--platform", "linux/amd64", ref]
        proc = run(pull_cmd, 120, check=False)
        out["images"][ref] = {"exit": proc.returncode}
        if proc.returncode == 0:
            out["images"][ref]["image"] = image(ref)
        else:
            out["images"][ref]["diagnostic"] = proc.stderr.decode(errors="replace")[-1500:]
    assert all(item["exit"] == 0 for item in out["images"].values())


def scan(req, out):
    assert node(WORK, stat.S_ISDIR, 0o700) == req["directory"]
    assert (WORK / ".agency-owner").read_text(encoding="utf-8") == OWNER
    assert node(TRIVY, stat.S_ISREG, 0o500) == req["tool"]["physical"]
    assert sha(TRIVY) == req["tool"]["sha256"] and database() == req["database"]
    ref = reference(req["reference"])
    baseline = image(ref)
    assert 0 < baseline["size"] < 5 * 1024**3
    name = req["name"]
    assert re.fullmatch(r"[a-z0-9-]{3,45}", name)
    report = WORK / (name + ".json")
    assert not os.path.lexists(report)
    out["step"] = "scan"
    out["reference"] = ref
    command = [
        str(TRIVY),
        "image",
        *OPTIONS,
        "--image-src",
        "docker",
        "--platform",
        "linux/amd64",
        "--cache-backend",
        "memory",
        "--skip-db-update",
        "--skip-java-db-update",
        "--offline-scan",
        "--parallel",
        "2",
        "--max-image-size",
        "5GiB",
        "--scanners",
        "vuln",
        "--pkg-types",
        "os,library",
        "--list-all-pkgs",
        "--format",
        "json",
        "--output",
        str(report),
        "--timeout",
        "6m",
        ref,
    ]
    started = time.monotonic()
    result = run(command, 370, check=False)
    out["seconds"] = round(time.monotonic() - started, 3)
    out["scan_exit"] = result.returncode
    out["diagnostic"] = result.stderr.decode(errors="replace")[-5000:]
    assert result.returncode == 0 and image(ref) == baseline
    node(report, stat.S_ISREG)
    assert 0 < report.stat().st_size <= 20 * 1024**2
    raw = report.read_bytes()
    data = json.loads(raw)
    assert data["ArtifactType"] == "container_image" and data["ArtifactName"] == ref
    rows = data.get("Results", [])
    out["coverage"] = [
        {"class": row["Class"], "type": row["Type"], "packages": len(row.get("Packages", []))}
        for row in rows
    ]
    out["severities"] = dict(
        collections.Counter(
            item["Severity"] for row in rows for item in row.get("Vulnerabilities", [])
        )
    )
    out["report"] = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "physical": node(report, stat.S_ISREG),
        "bytes": len(raw),
        "path": str(report),
    }
    out["image"] = baseline
    out["database_sha256"] = req["database"]["sha256"]
    out["report_b64"] = base64.b64encode(raw).decode()


def main():
    out = {"owner": OWNER, "success": False, "step": "host", "production_approved": False}
    try:
        req = json.loads(sys.stdin.buffer.read(65536))
        action = req["action"]
        out["action"] = action
        host(req)
        if action == "install":
            install(out)
        elif action == "pull":
            out["images"] = {}
            pull(req, out)
        else:
            assert action == "scan"
            scan(req, out)
        out["success"] = True
    except Exception as error:
        out["error"] = type(error).__name__
        if isinstance(error, RuntimeError) and re.fullmatch(
            r"COMMAND_[a-z0-9]+_[0-9]+", str(error)
        ):
            out["error"] = str(error)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
