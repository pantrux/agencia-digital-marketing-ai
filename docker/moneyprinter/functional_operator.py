"""Prepara y opera la prueba privada de MoneyPrinterTurbo en la VM de medios."""

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

OWNER = "agencia-digital-marketing-ai:moneyprinter-functional:v1"
NAME = "agency-moneyprinter-functional"
NETWORK = NAME + "-speaches"
BRIDGE = "br-mpt-fn"
IMAGE = "sha256:2547b9b5ba2577bd00e1508ef52497226c776b56f344767466d795b7c0ea4c1d"
REF = "ghcr.io/harry0703/moneyprinterturbo@" + IMAGE
INPUT = Path("/opt/media-platform/moneyprinter-functional-v1")
STATE = Path("/var/lib/media-platform/moneyprinter-functional-v1")
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
IPT = "/usr/sbin/iptables"
IP6T = "/usr/sbin/ip6tables"
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "DOCKER_CONFIG": str(INPUT / "docker-client"),
    "MEDIA_MONEYPRINTER_FUNCTIONAL_INPUT_ROOT": str(INPUT),
    "MEDIA_MONEYPRINTER_FUNCTIONAL_STATE_ROOT": str(STATE),
    "MEDIA_MONEYPRINTER_FUNCTIONAL_NETWORK_NAME": NETWORK,
}
OVERLAY = {
    "LICENSE.agency-speaches.txt",
    "app/services/agency_speaches.py",
    "app/services/subtitle.py",
    "app/services/task.py",
    "speaches_client.py",
    "speaches_contract.py",
    "webui/Main.py",
}
COMPOSE = [
    "compose",
    "--project-name",
    NAME,
    "--env-file",
    "/dev/null",
    "--file",
    str(INPUT / "compose.yml"),
]


def run(args, seconds=15, check=True):
    p = subprocess.run(args, env=ENV, capture_output=True, timeout=seconds, check=False)
    if check and p.returncode:
        raise RuntimeError("COMMAND_" + Path(args[0]).name + "_" + str(p.returncode))
    return p


def docker(args, seconds=15, check=True):
    return run(DOCKER + args, seconds, check)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def directory(path, mode, uid=0, gid=0):
    path.mkdir(mode=mode)
    os.chown(path, uid, gid, follow_symlinks=False)
    verify_dir(path, mode, uid, gid)


def verify_dir(path, mode, uid=0, gid=0):
    node = path.lstat()
    assert stat.S_ISDIR(node.st_mode) and stat.S_IMODE(node.st_mode) == mode
    assert (node.st_uid, node.st_gid) == (uid, gid)
    return {"device": node.st_dev, "inode": node.st_ino}


def write(path, raw, mode, uid=0, gid=0):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchown(stream.fileno(), uid, gid)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        node = os.fstat(stream.fileno())
    assert stat.S_ISREG(node.st_mode) and stat.S_IMODE(node.st_mode) == mode
    assert node.st_nlink == 1 and node.st_size == len(raw)
    assert path.read_bytes() == raw
    return {"device": node.st_dev, "inode": node.st_ino}


def layout(req):
    assert os.geteuid() == 0 and Path("/etc/hostname").read_text().strip() == "vm-media-01"
    for path, physical in (
        (INPUT.parent, req["layout"][str(INPUT.parent)]),
        (STATE.parent, req["layout"][str(STATE.parent)]),
    ):
        node = path.lstat()
        assert stat.S_ISDIR(node.st_mode) and node.st_uid == 0 and not node.st_mode & 0o022
        assert (node.st_dev, node.st_ino) == (physical["device"], physical["inode"])
    fmt = "{{json .ID}} {{json .ServerVersion}} {{json .DockerRootDir}}"
    info = docker(["info", "--format", fmt]).stdout.decode().split()
    assert (
        dict(zip(("ID", "ServerVersion", "DockerRootDir"), map(json.loads, info), strict=True))
        == req["daemon"]
    )
    image = json.loads(docker(["image", "inspect", REF]).stdout)[0]
    assert image["Id"] == IMAGE and REF in image["RepoDigests"] and image["Architecture"] == "amd64"


def policy():
    tag = ["-m", "comment", "--comment", OWNER]
    established = ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"]
    return [
        (
            IPT,
            "DOCKER-USER",
            [
                "-i",
                BRIDGE,
                "!",
                "-o",
                BRIDGE,
                "-p",
                "tcp",
                "-d",
                "192.168.0.6/32",
                "--dport",
                "8000",
                *tag,
                "-j",
                "RETURN",
            ],
        ),
        (IPT, "DOCKER-USER", ["-i", BRIDGE, "!", "-o", BRIDGE, *tag, "-j", "DROP"]),
        (IPT, "DOCKER-USER", ["!", "-i", BRIDGE, "-o", BRIDGE, *established, *tag, "-j", "RETURN"]),
        (IPT, "DOCKER-USER", ["!", "-i", BRIDGE, "-o", BRIDGE, *tag, "-j", "DROP"]),
        (IPT, "INPUT", ["-i", BRIDGE, *established, *tag, "-j", "RETURN"]),
        (IPT, "INPUT", ["-i", BRIDGE, *tag, "-j", "DROP"]),
        (IP6T, "DOCKER-USER", ["-i", BRIDGE, *tag, "-j", "DROP"]),
        (IP6T, "DOCKER-USER", ["-o", BRIDGE, *tag, "-j", "DROP"]),
        (IP6T, "INPUT", ["-i", BRIDGE, *tag, "-j", "DROP"]),
    ]


def check_policy():
    for binary in (IPT, IP6T):
        forward = run([binary, "-w", "3", "-S", "FORWARD"]).stdout.splitlines()
        assert forward[1] == b"-A FORWARD -j DOCKER-USER"
        for chain in ("DOCKER-USER", "INPUT"):
            expected = [
                args for program, name, args in policy() if (program, name) == (binary, chain)
            ]
            actual = run([binary, "-w", "3", "-S", chain]).stdout.splitlines()[1:]
            assert sum(OWNER.encode() in row for row in actual) == len(expected)
            assert all(OWNER.encode() in row for row in actual[: len(expected)])
            for args in expected:
                run([binary, "-w", "3", "-C", chain, *args])


def network(net_id):
    row = json.loads(docker(["network", "inspect", net_id]).stdout)[0]
    assert row["Name"] == NETWORK and row["Driver"] == "bridge" and not row["EnableIPv6"]
    assert row["Id"] == net_id and row["Labels"].get("agency.owner") == OWNER
    assert row["Options"]["com.docker.network.bridge.name"] == BRIDGE
    return row


def inspect(cid, svc, net_id):
    assert re.fullmatch(r"[0-9a-f]{64}", cid)
    row = json.loads(docker(["container", "inspect", cid]).stdout)[0]
    assert row["Id"] == cid and row["Image"] == IMAGE
    config, host = row["Config"], row["HostConfig"]
    assert config["User"] == "33:33" and config["Labels"]["agency.owner"] == OWNER
    assert config["Labels"]["com.docker.compose.project"] == NAME
    assert config["Labels"]["com.docker.compose.service"] == svc
    assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"] and host["Init"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["PidsLimit"] == 96 and host["Memory"] == host["MemorySwap"] == 1024**3
    assert host["Dns"] == ["127.0.0.1"]
    assert host["NetworkMode"] == NETWORK
    endpoint = row["NetworkSettings"]["Networks"][NETWORK]
    assert endpoint["NetworkID"] in ("", net_id)
    ports = {"8501/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8501"}]} if svc == "ui" else {}
    assert (host["PortBindings"] or {}) == ports
    mounts = {m["Destination"]: (m["Source"], m["RW"]) for m in row["Mounts"]}
    expected = {
        "/MoneyPrinterTurbo/" + name: (str(INPUT / "overlay" / name), False)
        for name in OVERLAY
        if name != "webui/Main.py" or svc == "ui"
    }
    expected["/run/secrets/moneyprinter-speaches"] = (str(INPUT / "secrets/speaches.bearer"), False)
    expected["/run/mpt"] = (str(STATE / svc), True)
    assert {k: v for k, v in mounts.items() if k not in host["Tmpfs"]} == expected
    return row


def prepare(req, out):
    assert not os.path.lexists(INPUT) and not os.path.lexists(STATE)
    assert not Path("/sys/class/net", BRIDGE).exists()
    assert not docker(["network", "ls", "-q", "--filter", "name=^" + NETWORK + "$"]).stdout.strip()
    assert not docker(
        ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + NAME]
    ).stdout.strip()
    for binary, chain in {(binary, chain) for binary, chain, _ in policy()}:
        assert OWNER.encode() not in run([binary, "-w", "3", "-S", chain]).stdout
    assert set(req["overlay"]) == OVERLAY
    source = base64.b64decode(req["compose"], validate=True)
    assert digest(source) == req["compose_sha"]
    api_key, bearer = req["api_key"], req["bearer"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,80}", api_key)
    assert re.fullmatch(r"[!-~]{16,256}", bearer)
    template = base64.b64decode(req["template"], validate=True).decode("utf-8")
    assert (
        template.count('api_key = ""') >= 1
        and template.count('subtitle_speaches_token_file = ""') == 1
    )
    template = (
        template.replace('log_level = "DEBUG"', 'log_level = "INFO"', 1)
        .replace(
            'subtitle_speaches_token_file = ""',
            'subtitle_speaches_token_file = "/run/secrets/moneyprinter-speaches"',
        )
        .replace('video_source = "pexels"', 'video_source = "local"', 1)
    )
    out["step"] = "private_files"
    directory(INPUT, 0o700)
    directory(STATE, 0o700)
    out["directories"] = {str(p): verify_dir(p, 0o700) for p in (INPUT, STATE)}
    for p in (
        "docker-client",
        "secrets",
        "overlay",
        "overlay/app",
        "overlay/app/services",
        "overlay/webui",
    ):
        directory(INPUT / p, 0o700)
    for p in (INPUT, STATE):
        write(p / ".agency-owner", OWNER.encode(), 0o400)
    write(INPUT / "compose.yml", source, 0o400)
    for name, entry in req["overlay"].items():
        raw = base64.b64decode(entry["content"], validate=True)
        assert digest(raw) == entry["sha256"]
        write(INPUT / "overlay" / name, raw, 0o444)
    write(INPUT / "secrets/speaches.bearer", bearer.encode(), 0o400, 33, 33)
    for svc in ("ui", "api"):
        directory(STATE / svc, 0o700, 33, 33)
        config = (
            template
            if svc == "ui"
            else template.replace('api_key = ""', 'api_key = "' + api_key + '"', 1)
        )
        app = tomllib.loads(config)["app"]
        assert app["api_key"] == (api_key if svc == "api" else "")
        assert app["subtitle_provider"] == "speaches" and not app["enable_redis"]
        write(STATE / svc / "config.toml", config.encode(), 0o600, 33, 33)
    out["step"] = "compose_config"
    docker(COMPOSE + ["config", "--quiet"])
    out["step"] = "network_policy"
    for binary, chain, args in reversed(policy()):
        run([binary, "-w", "3", "-I", chain, "1", *args])
    check_policy()
    out["step"] = "network_create"
    net_id = (
        docker(
            [
                "network",
                "create",
                "--driver",
                "bridge",
                "--ipv6=false",
                "--label",
                "agency.owner=" + OWNER,
                "--opt",
                "com.docker.network.bridge.name=" + BRIDGE,
                NETWORK,
            ]
        )
        .stdout.decode()
        .strip()
    )
    out["network_id"] = net_id
    assert re.fullmatch(r"[0-9a-f]{64}", net_id)
    network(net_id)
    out["step"] = "compose_create"
    create = docker(COMPOSE + ["create", "--pull", "never", "ui", "api"], 60, check=False)
    out["create_exit"] = create.returncode
    out["services"] = {}
    for svc in ("ui", "api"):
        cid = docker(COMPOSE + ["ps", "-a", "-q", svc]).stdout.decode().strip()
        if cid:
            out["services"][svc] = {"cid": cid}
            row = inspect(cid, svc, net_id)
            assert row["State"]["Status"] == "created" and row["State"]["Pid"] == 0
            out["services"][svc]["verified_created"] = True
    assert create.returncode == 0 and set(out["services"]) == {"ui", "api"}


def operate(req, out):
    for p in (INPUT, STATE):
        assert verify_dir(p, 0o700) == req["directories"][str(p)]
        assert (p / ".agency-owner").read_bytes() == OWNER.encode()
    net_id = req["network_id"]
    assert set(req["services"]) == {"ui", "api"}
    check_policy()
    network(net_id)
    action = req["action"]
    out["services"] = {}
    for svc, item in req["services"].items():
        cid = item["cid"]
        row = inspect(cid, svc, net_id)
        out["step"] = svc
        if action == "start":
            assert row["State"]["Status"] == "created"
            docker(["start", cid], 30)
        elif action == "stop" and row["State"]["Running"]:
            docker(["stop", "--time", "15", cid], 25)
        row = inspect(cid, svc, net_id)
        status = row["State"]
        out["services"][svc] = {
            "cid": cid,
            "status": status["Status"],
            "pid": status["Pid"],
            "exit": status["ExitCode"],
            "restarts": row["RestartCount"],
            "ip": row["NetworkSettings"]["Networks"][NETWORK]["IPAddress"],
        }
    if action == "start":
        out["step"] = "http"
        expected = {
            "ui": "http://127.0.0.1:8501/_stcore/health",
            "api": "http://" + out["services"]["api"]["ip"] + ":8080/openapi.json",
        }
        out["http"] = {}
        deadline = time.monotonic() + 35
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for svc, url in expected.items():
            while time.monotonic() < deadline:
                try:
                    with opener.open(url, timeout=2) as response:
                        body = response.read(512 * 1024)
                        result = {"status": response.status}
                    if svc == "api":
                        result["routes"] = sorted(json.loads(body)["paths"])
                    out["http"][svc] = result
                    break
                except OSError:
                    time.sleep(0.5)
        for svc, item in out["services"].items():
            row = inspect(item["cid"], svc, net_id)
            item["status"] = row["State"]["Status"]
            item["exit"] = row["State"]["ExitCode"]
        assert set(out["http"]) == {"ui", "api"}


def main():
    out = {"owner": OWNER, "success": False, "production_approved": False}
    try:
        req = json.loads(sys.stdin.buffer.read(2 * 1024**2))
        action = req["action"]
        assert action in {"prepare", "start", "status", "stop"}
        out["action"] = action
        out["step"] = "host"
        layout(req)
        if action == "prepare":
            prepare(req, out)
        else:
            operate(req, out)
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
