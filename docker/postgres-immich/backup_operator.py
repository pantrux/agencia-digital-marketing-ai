"""Inicializa y respalda la base persistente de ensayo de Immich."""

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

PROJECT = "agency-media-immich-db"
OWNER = "agencia-digital-marketing-ai:postgres-immich-backup:v1"
IMAGE = "sha256:a00b7ac5ed30069abde2702a2b2b3d8321ea0f22d2ba64cc9f836c86b4c47601"
NETWORK = PROJECT + "-private-v1"
WORK = Path("/opt/media-platform/immich-db-trial-v1")
STATE = Path("/var/lib/media-platform/immich-db-trial-v1")
DATA = STATE / "pgdata"
STAGING = STATE / "capture-v1"
PASSWORD = WORK / "secrets/postgres-password"
PGPASS = WORK / "secrets/postgres-pgpass"
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
COMPOSE = [
    "compose",
    "--project-name",
    PROJECT,
    "--env-file",
    "/dev/null",
    "-f",
    str(WORK / "compose.yml"),
]
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "DOCKER_CONFIG": str(WORK / "docker-client"),
    "MEDIA_POSTGRES_IMMICH_BACKUP_DATA_PATH": str(DATA),
    "MEDIA_POSTGRES_IMMICH_BACKUP_NETWORK": NETWORK,
    "MEDIA_POSTGRES_IMMICH_BACKUP_PASSWORD_FILE": str(PASSWORD),
    "MEDIA_POSTGRES_IMMICH_BACKUP_PGPASS_FILE": str(PGPASS),
}
INIT = """BEGIN;
CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.5';
CREATE EXTENSION IF NOT EXISTS vchord VERSION '1.1.1';
CREATE EXTENSION IF NOT EXISTS cube;
CREATE EXTENSION IF NOT EXISTS earthdistance;
COMMIT;
"""
VERIFY = """
SELECT json_build_object(
  'version', current_setting('server_version_num'),
  'checksums', current_setting('data_checksums'),
  'preload', current_setting('shared_preload_libraries'),
  'cluster', (SELECT system_identifier::text FROM pg_control_system()),
  'extensions', (SELECT json_object_agg(extname, extversion)
                  FROM pg_extension WHERE extname != 'plpgsql'),
  'public_tables', (SELECT count(*) FROM pg_tables WHERE schemaname = 'public')
);
"""
PSQL = [
    "/usr/local/bin/psql",
    "-X",
    "-q",
    "-A",
    "-t",
    "-w",
    "-h",
    "127.0.0.1",
    "-p",
    "5432",
    "-U",
    "postgres",
    "-d",
    "immich",
    "--set=ON_ERROR_STOP=1",
]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(path, uid=0, gid=0, mode=0o700):
    n = path.lstat()
    assert stat.S_ISDIR(n.st_mode) and (n.st_uid, n.st_gid) == (uid, gid)
    assert mode is None or stat.S_IMODE(n.st_mode) == mode
    return {"device": n.st_dev, "inode": n.st_ino}


def mkdir(path, uid=0, gid=0):
    path.mkdir(mode=0o700)
    os.chown(path, uid, gid, follow_symlinks=False)
    return identity(path, uid, gid)


def write(path, raw, mode, uid=0, gid=0):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchown(stream.fileno(), uid, gid)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def read(path, mode, uid=0, gid=0, limit=1024**2):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        n = os.fstat(stream.fileno())
        assert stat.S_ISREG(n.st_mode) and n.st_nlink == 1
        assert (n.st_uid, n.st_gid, stat.S_IMODE(n.st_mode)) == (uid, gid, mode)
        raw = stream.read(limit + 1)
    assert len(raw) == n.st_size <= limit
    return raw, {"device": n.st_dev, "inode": n.st_ino}


def run(args, seconds=15, *, data=None, check=True):
    p = subprocess.run(args, env=ENV, input=data, capture_output=True, timeout=seconds, check=False)
    assert len(p.stdout) + len(p.stderr) < 1024**2
    if check and p.returncode:
        raise RuntimeError("COMMAND_" + Path(args[0]).name + "_" + str(p.returncode))
    return p


def docker(args, seconds=15, **kwargs):
    return run(DOCKER + args, seconds, **kwargs)


def host(req):
    assert os.geteuid() == 0 and os.uname().nodename == "vm-media-01"
    assert identity(WORK.parent, mode=None) == req["layout"][str(WORK.parent)]
    assert identity(STATE.parent) == req["layout"][str(STATE.parent)]
    fmt = '{"ID":{{json .ID}},"ServerVersion":{{json .ServerVersion}},"DockerRootDir":{{json .DockerRootDir}}}'
    assert json.loads(docker(["info", "--format", fmt]).stdout) == req["daemon"]
    image = json.loads(docker(["image", "inspect", IMAGE]).stdout)[0]
    assert image["Id"] == IMAGE and image["Architecture"] == "amd64" and image["Os"] == "linux"
    assert image["Config"]["User"] in ("", "70", "70:70")
    fs = os.statvfs(STATE.parent)
    assert fs.f_bavail * fs.f_frsize >= 5 * 1024**3
    local = run(["/usr/bin/findmnt", "-n", "-o", "FSTYPE", "--target", str(STATE.parent)])
    assert local.stdout.strip() in {b"ext4", b"xfs", b"btrfs"}


def network(net_id):
    row = json.loads(docker(["network", "inspect", net_id]).stdout)[0]
    assert row["Id"] == net_id and row["Name"] == NETWORK
    assert row["Internal"] and row["Driver"] == "bridge" and not row["EnableIPv6"]
    assert row["Labels"]["agency.owner"] == OWNER
    return row


def prepare(req, out):
    assert not os.path.lexists(WORK) and not os.path.lexists(STATE)
    assert not docker(
        ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT]
    ).stdout.strip()
    assert not docker(["network", "ls", "-q", "--filter", "name=^" + NETWORK + "$"]).stdout.strip()
    compose = base64.b64decode(req["compose"], validate=True)
    capture = base64.b64decode(req["capture"], validate=True)
    assert sha(compose) == req["compose_sha"] and sha(capture) == req["capture_sha"]
    assert b"\r" not in capture and compose.count(b"restart: unless-stopped") == 1
    password = req["password"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{48,80}", password)
    out["step"] = "staging"
    out["directories"] = {
        str(WORK): mkdir(WORK),
        str(STATE): mkdir(STATE),
        str(DATA): mkdir(DATA, 70, 70),
    }
    mkdir(WORK / "docker-client")
    mkdir(WORK / "secrets")
    write(WORK / ".agency-owner", OWNER.encode(), 0o400)
    write(STATE / ".agency-owner", OWNER.encode(), 0o400)
    trial = compose.replace(b"restart: unless-stopped", b'restart: "no"', 1)
    write(WORK / "compose.yml", trial, 0o400)
    write(WORK / "capture-backup.sh", capture, 0o400)
    write(PASSWORD, password.encode() + b"\n", 0o400, 70, 70)
    write(PGPASS, f"127.0.0.1:5432:*:postgres:{password}\n".encode(), 0o400, 70, 70)
    out["compose_trial_sha256"] = sha(trial)
    out["step"] = "compose_config"
    docker(COMPOSE + ["config", "--quiet"])
    out["step"] = "network"
    net_id = (
        docker(
            [
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                "--ipv6=false",
                "--label",
                "agency.owner=" + OWNER,
                NETWORK,
            ]
        )
        .stdout.decode()
        .strip()
    )
    out["network_id"] = net_id
    assert re.fullmatch(r"[0-9a-f]{64}", net_id)
    assert not network(net_id)["Containers"]


def staged(req):
    for p in (WORK, STATE):
        assert identity(p) == req["directories"][str(p)]
        assert read(p / ".agency-owner", 0o400)[0] == OWNER.encode()
    assert identity(DATA, 70, 70) == req["directories"][str(DATA)]
    assert sha(read(WORK / "compose.yml", 0o400)[0]) == req["compose_trial_sha256"]
    assert sha(read(WORK / "capture-backup.sh", 0o400)[0]) == req["capture_sha"]
    for p in (PASSWORD, PGPASS):
        read(p, 0o400, 70, 70)
    network(req["network_id"])


def inspect(req, cid):
    assert re.fullmatch(r"[0-9a-f]{64}", cid)
    row = json.loads(docker(["container", "inspect", cid]).stdout)[0]
    cfg, hc = row["Config"], row["HostConfig"]
    assert row["Id"] == cid and row["Image"] == IMAGE
    assert cfg["User"] == "70:70" and cfg["Labels"]["agency.owner"] == OWNER
    assert cfg["Labels"]["com.docker.compose.project"] == PROJECT
    assert cfg["Labels"]["com.docker.compose.service"] == "postgres-immich"
    assert cfg["Cmd"] == ["postgres", "-c", "shared_preload_libraries=vchord.so"]
    assert hc["NetworkMode"] == NETWORK and not hc["PortBindings"]
    assert hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"] and not hc["Privileged"]
    assert hc["SecurityOpt"] == ["no-new-privileges:true"] and hc["RestartPolicy"]["Name"] == "no"
    assert hc["PidsLimit"] == 128 and hc["Memory"] == hc["MemorySwap"] == 768 * 1024**2
    expected = {
        "/var/lib/postgresql/data": (str(DATA), True),
        "/run/secrets/postgres_password": (str(PASSWORD), False),
        "/run/secrets/postgres_pgpass": (str(PGPASS), False),
    }
    actual = {
        m["Destination"]: (m["Source"], m["RW"]) for m in row["Mounts"] if m["Type"] == "bind"
    }
    assert actual == expected
    return row


def create(req, out):
    assert not network(req["network_id"])["Containers"]
    assert not list(DATA.iterdir())
    result = docker(COMPOSE + ["create", "--pull", "never", "postgres-immich"], 45, check=False)
    out["create_exit"] = result.returncode
    cid = docker(COMPOSE + ["ps", "-a", "-q", "postgres-immich"]).stdout.decode().strip()
    if cid:
        out["cid"] = cid
    assert result.returncode == 0
    row = inspect(req, cid)
    assert row["State"]["Status"] == "created" and row["State"]["Pid"] == 0
    out["created"] = row["Created"]


def exercise(req, out):
    cid = req["cid"]
    row = inspect(req, cid)
    assert row["Created"] == req["created"] and row["State"]["Status"] == "created"
    assert not list(DATA.iterdir()) and not os.path.lexists(STAGING)
    try:
        out["step"] = "start"
        docker(["start", cid], 30)
        out["step"] = "health"
        deadline = time.monotonic() + 85
        while time.monotonic() < deadline:
            row = inspect(req, cid)
            health = row["State"]["Health"]["Status"]
            assert row["State"]["Running"] and row["RestartCount"] == 0
            if health == "healthy":
                break
            time.sleep(1)
        else:
            raise TimeoutError("PG_HEALTH")
        out["step"] = "extensions"
        cmd = ["exec", "-i", "--user", "70:70", cid, *PSQL]
        docker(cmd, 25, data=INIT.encode())
        sql = json.loads(docker(cmd, 15, data=VERIFY.encode()).stdout)
        assert (
            sql["version"] == "160015"
            and sql["checksums"] == "on"
            and sql["preload"] == "vchord.so"
        )
        assert sql["extensions"] == {
            "vector": "0.8.5",
            "vchord": "1.1.1",
            "cube": "1.5",
            "earthdistance": "1.2",
        }
        assert sql["public_tables"] == 0 and re.fullmatch(r"[0-9]+", sql["cluster"])
        out["postgres"] = sql
        out["step"] = "capture"
        p = run(
            ["/usr/bin/bash", str(WORK / "capture-backup.sh"), cid, str(DATA), str(STAGING)],
            340,
            check=False,
        )
        if p.returncode:
            out["capture_diagnostic"] = p.stderr.decode(errors="replace")[-1000:]
        assert p.returncode == 0
        capture = json.loads(p.stdout)
        path = STAGING / "postgres-immich.sql.zst"
        raw, file = read(path, 0o600)
        assert capture["cid"] == cid and capture["path"] == str(path)
        assert capture["bytes"] == len(raw) and capture["sha256"] == sha(raw)
        out["capture"] = {**capture, "file": file, "directory": identity(STAGING)}
    finally:
        state = inspect(req, cid)["State"]
        if state["Running"]:
            docker(["stop", "--time", "30", cid], 40)
        state = inspect(req, cid)["State"]
        out["stopped"] = {"status": state["Status"], "pid": state["Pid"], "exit": state["ExitCode"]}
        assert state["Status"] == "exited" and state["Pid"] == 0


def main():
    out = {"success": False, "owner": OWNER, "step": "host", "production_approved": False}
    try:
        req = json.loads(sys.stdin.buffer.read(128 * 1024))
        action = req["action"]
        out["action"] = action
        host(req)
        if action == "prepare":
            prepare(req, out)
        else:
            out["step"] = "verify_staging"
            staged(req)
            if action == "create":
                out["step"] = "create"
                create(req, out)
            else:
                assert action == "exercise"
                exercise(req, out)
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
