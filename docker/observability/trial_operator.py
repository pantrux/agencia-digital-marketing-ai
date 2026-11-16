"""Crea un ensayo interno y verifica dos muestras antes de detenerlo."""

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = "agency-observability-trial-v1"
OWNER = "agencia-digital-marketing-ai:observability-trial:v1"
WORK = Path("/opt/media-platform/observability-trial-v1")
NETWORK = PROJECT + "_metrics"
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
COMPOSE = [
    "compose",
    "--project-name",
    PROJECT,
    "--env-file",
    "/dev/null",
    "-f",
    str(WORK / "compose.trial.yml"),
]
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "DOCKER_CONFIG": str(WORK / "docker-client"),
    "COMPOSE_DISABLE_ENV_FILE": "true",
}
SERVICES = ("node-exporter", "cadvisor", "otel-collector")
FILENAMES = {"compose.trial.yml", "otel-collector.trial.yml"}


def run(args, seconds=15, check=True):
    result = subprocess.run(
        DOCKER + args, env=ENV, capture_output=True, timeout=seconds, check=False
    )
    if check and result.returncode:
        raise RuntimeError("DOCKER_" + args[0].upper() + "_" + str(result.returncode))
    return result


def json_output(args):
    return json.loads(run(args).stdout)


def directory(path):
    n = path.lstat()
    assert stat.S_ISDIR(n.st_mode) and n.st_uid == n.st_gid == 0 and not n.st_mode & 0o022
    return {"device": n.st_dev, "inode": n.st_ino}


def secure_source(name, sha):
    path = WORK / name
    n = path.lstat()
    assert stat.S_ISREG(n.st_mode) and n.st_nlink == 1 and n.st_uid == n.st_gid == 0
    assert stat.S_IMODE(n.st_mode) == 0o444
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha


def host(req):
    assert os.geteuid() == 0 and os.uname().nodename == "vm-media-01"
    assert directory(WORK.parent) == req["parent"]
    fmt = '{"ID":{{json .ID}},"ServerVersion":{{json .ServerVersion}},"DockerRootDir":{{json .DockerRootDir}}}'
    assert json_output(["info", "--format", fmt]) == req["daemon"]
    assert (
        run(["info", "--format", "{{.CgroupDriver}}|{{.CgroupVersion}}"]).stdout.strip()
        == b"systemd|2"
    )
    assert set(req["images"]) == set(SERVICES)
    for info in req["images"].values():
        row = json_output(["image", "inspect", info["ref"]])[0]
        assert row["Id"] == info["id"] and info["ref"] in row["RepoDigests"]
        assert row["Architecture"] == "amd64" and row["Os"] == "linux"


def inspect(cid, svc, req):
    assert re.fullmatch(r"[0-9a-f]{64}", cid)
    row = json_output(["container", "inspect", cid])[0]
    cfg, hc = row["Config"], row["HostConfig"]
    assert row["Id"] == cid and cfg["User"] == "65534:65534"
    assert hc["ReadonlyRootfs"] and not hc["Privileged"] and hc["CapDrop"] == ["ALL"]
    assert not hc["PortBindings"] and hc["PidsLimit"] == 64
    assert hc["Memory"] == hc["MemorySwap"] and hc["Memory"] <= 192 * 1024**2
    assert all(not m["RW"] for m in row["Mounts"])
    if svc == "validate":
        assert row["Image"] == req["images"]["otel-collector"]["id"]
        assert cfg["Labels"]["agency.owner"] == OWNER and hc["NetworkMode"] == "none"
        assert len(row["Mounts"]) == 1
    else:
        assert row["Image"] == req["images"][svc]["id"]
        assert cfg["Labels"]["com.docker.compose.project"] == PROJECT
        assert cfg["Labels"]["com.docker.compose.service"] == svc
        assert hc["NetworkMode"] == NETWORK
        assert hc["CgroupnsMode"] == ("host" if svc == "cadvisor" else "private")
    return row


def network():
    row = json_output(["network", "inspect", NETWORK])[0]
    assert row["Internal"] and row["Driver"] == "bridge" and not row["EnableIPv6"]
    assert row["Labels"]["com.docker.compose.project"] == PROJECT
    return row


def prepare(req, out):
    assert not os.path.lexists(WORK)
    assert not run(
        ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT]
    ).stdout.strip()
    assert not run(["network", "ls", "-q", "--filter", "name=^" + NETWORK + "$"]).stdout.strip()
    assert set(req["sources"]) == FILENAMES
    WORK.mkdir(mode=0o700)
    out["directory"] = directory(WORK)
    (WORK / "docker-client").mkdir(mode=0o700)
    for name, entry in req["sources"].items():
        path = WORK / name
        with path.open("xb") as stream:
            stream.write(base64.b64decode(entry["content"], validate=True))
        path.chmod(0o444)
        secure_source(name, entry["sha256"])
    out["step"] = "compose_config"
    run(COMPOSE + ["config", "--quiet"])
    out["step"] = "collector_validator"
    mounts = f"type=bind,src={WORK}/otel-collector.trial.yml,dst=/etc/agency-otel.yaml,readonly"
    cid = (
        run(
            [
                "create",
                "--name",
                PROJECT + "-validate",
                "--label",
                "agency.owner=" + OWNER,
                "--pull=never",
                "--network=none",
                "--read-only",
                "--user",
                "65534:65534",
                "--cap-drop=ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--cpus=0.50",
                "--memory=192m",
                "--memory-swap=192m",
                "--pids-limit=64",
                "--mount",
                mounts,
                req["images"]["otel-collector"]["ref"],
                "validate",
                "--config=/etc/agency-otel.yaml",
            ]
        )
        .stdout.decode()
        .strip()
    )
    out["containers"] = {"validate": {"cid": cid}}
    row = inspect(cid, "validate", req)
    assert row["State"]["Status"] == "created"
    out["containers"]["validate"]["created"] = row["Created"]
    out["step"] = "compose_create"
    created = run(COMPOSE + ["create", "--no-build", "--pull", "never"], 50, check=False)
    out["create_exit"] = created.returncode
    if created.returncode:
        out["diagnostic"] = created.stderr.decode(errors="replace")[-2000:]
    for svc in SERVICES:
        cid = run(COMPOSE + ["ps", "-a", "-q", svc]).stdout.decode().strip()
        if cid:
            out["containers"][svc] = {"cid": cid}
            row = inspect(cid, svc, req)
            assert row["State"]["Status"] == "created" and row["State"]["Pid"] == 0
            out["containers"][svc]["created"] = row["Created"]
    out["network"] = network()["Id"]
    assert created.returncode == 0 and len(out["containers"]) == 4


def sample(ip, observed):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{ip}:8889/metrics", timeout=4) as response:
        data = response.read(2 * 1024**2 + 1)
    assert len(data) <= 2 * 1024**2
    up, timestamps, counts = {}, {}, {}
    for line in data.decode().splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.rsplit(" ", 2)
        metric = fields[0].split("{", 1)[0]
        counts[metric] = counts.get(metric, 0) + 1
        if metric == "up" and len(fields) >= 2:
            for job in ("agency-node", "agency-cgroups"):
                if f'job="{job}"' in fields[0]:
                    up[job] = float(fields[1]) == 1
        if len(fields) != 3 or not fields[-1].isdigit():
            continue
        if metric == "node_memory_MemAvailable_bytes" and float(fields[1]) > 0:
            timestamps[metric] = int(fields[-1])
        if (
            metric in {"container_cpu_usage_seconds_total", "container_memory_usage_bytes"}
            and observed in fields[0]
        ):
            timestamps[metric] = int(fields[-1])
    return {"bytes": len(data), "up": up, "timestamps": timestamps, "metric_counts": counts}


def exercise(req, out):
    assert directory(WORK) == req["directory"] and network()["Id"] == req["network"]
    assert set(req["sources"]) == FILENAMES
    for name, sha in req["sources"].items():
        secure_source(name, sha)
    assert set(req["containers"]) == {"validate", *SERVICES}
    for svc, item in req["containers"].items():
        row = inspect(item["cid"], svc, req)
        expected = "exited" if req["action"] == "retry" else "created"
        assert row["State"]["Status"] == expected and row["Created"] == item["created"]
        assert row["State"]["Pid"] == 0
        if req["action"] == "retry" and svc != "validate":
            assert row["State"]["ExitCode"] == req["prior_stopped"][svc]["exit"]
    validator = req["containers"]["validate"]["cid"]
    out["step"] = "validate"
    if req["action"] == "exercise":
        command = run(["start", "--attach", validator], 35, check=False)
        assert command.returncode == 0
    valid = inspect(validator, "validate", req)["State"]
    out["validator"] = {
        "exit": valid["ExitCode"],
        "status": valid["Status"],
    }
    assert valid["ExitCode"] == valid["Pid"] == 0 and valid["Status"] == "exited"
    started = []
    try:
        for svc in SERVICES:
            out["step"] = "start-" + svc
            cid = req["containers"][svc]["cid"]
            started.append((svc, cid))
            run(["start", cid], 30)
            assert inspect(cid, svc, req)["State"]["Running"]
        out["step"] = "metrics"
        cid = req["containers"]["otel-collector"]["cid"]
        row = inspect(cid, "otel-collector", req)
        endpoint = row["NetworkSettings"]["Networks"][NETWORK]
        assert endpoint["NetworkID"] == req["network"]
        ip = endpoint["IPAddress"]
        assert re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", ip)
        observed = "/system.slice/docker-" + req["containers"]["node-exporter"]["cid"] + ".scope"
        deadline = time.monotonic() + 60
        out["samples"] = []
        while time.monotonic() < deadline:
            try:
                current = sample(ip, observed)
                out["last_sample"] = current
            except OSError:
                time.sleep(2)
                continue
            if (
                len(current["timestamps"]) == 3
                and len(current["up"]) == 2
                and all(current["up"].values())
            ):
                if not out["samples"] or all(
                    value > out["samples"][0]["timestamps"][key]
                    for key, value in current["timestamps"].items()
                ):
                    out["samples"].append(current)
                if len(out["samples"]) == 2:
                    break
            time.sleep(2)
        assert len(out["samples"]) == 2
    finally:
        out["stopped"] = {}
        for svc, cid in reversed(started):
            try:
                row = inspect(cid, svc, req)
                if row["State"]["Running"]:
                    run(["stop", "--timeout", "10", cid], 22)
                state = inspect(cid, svc, req)["State"]
                out["stopped"][svc] = {
                    "pid": state["Pid"],
                    "exit": state["ExitCode"],
                    "status": state["Status"],
                }
                if out["step"] != "metrics" or len(out.get("samples", [])) != 2:
                    log = run(["logs", "--tail", "12", cid], 8, check=False)
                    out["stopped"][svc]["diagnostic"] = (log.stdout + log.stderr).decode(
                        errors="replace"
                    )[-1600:]
            except Exception as error:
                out["stopped"][svc] = {"error": type(error).__name__}
        assert all(
            item.get("pid") == 0 and item.get("status") == "exited"
            for item in out["stopped"].values()
        )


def main():
    out = {"success": False, "owner": OWNER, "production_approved": False, "step": "host"}
    try:
        req = json.loads(sys.stdin.buffer.read(65536))
        action = req["action"]
        out["action"] = action
        host(req)
        if action == "prepare":
            prepare(req, out)
        else:
            assert action in {"exercise", "retry"}
            exercise(req, out)
        out["success"] = True
    except Exception as error:
        out["error"] = type(error).__name__
        if isinstance(error, RuntimeError) and re.fullmatch(r"DOCKER_[A-Z]+_[0-9]+", str(error)):
            out["error"] = str(error)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
