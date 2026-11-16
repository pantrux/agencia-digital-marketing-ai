"""Comprueba el despliegue MoneyPrinter y protege su red antes del arranque."""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
from pathlib import Path

INPUT = Path("/opt/media-platform/moneyprinter-lan-v1")
STATE = Path("/var/lib/media-platform/moneyprinter-lan-v1")
MOUNT = Path("/mnt/producciones")
NAS = MOUNT / "_moneyprinter"
PROJECT = "agency-moneyprinter-lan"
NETWORK = "agency-moneyprinter-lan-speaches"
OWNER = "agencia-digital-marketing-ai:moneyprinter-lan:v1"
OLD_OWNER = "agencia-digital-marketing-ai:moneyprinter-functional:v1"
BRIDGE, OLD_BRIDGE = "br-mpt-lan", "br-mpt-fn"
OBS_BRIDGE, OBS_GUARD = "br-aobsoutv1", "AGENCY-OBS-OUT-V1"
NIC, IP, LAN = "media0", "192.168.0.85", "192.168.0.0/24"
SPEACHES = "192.168.0.6"
ROLES = ("api", "ui")
CHAINS = ("DOCKER-USER", "INPUT")
TABLES = ("ip6tables", "iptables")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def command(argv, code, payload=None):
    result = subprocess.run(
        argv,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=ENV,
        timeout=12,
        check=False,
    )
    require(result.returncode == 0 and len(result.stdout) <= 4 * 1024**2, code)
    return result.stdout


def unique_object(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "JSON_DUPLICADO")
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique_object)


def docker_json(*argv):
    return decode(
        command(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *argv], "DOCKER")
    )


def only_row(rows):
    require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict), "INSPECT")
    return rows[0]


def directory(path, private=False):
    node = path.lstat()
    require(stat.S_ISDIR(node.st_mode) and path.resolve(strict=True) == path, "DIRECTORIO")
    if private:
        require((node.st_uid, node.st_gid) == (0, 0) and node.st_mode & 0o022 == 0, "RAIZ_PRIVADA")
    return node


def file_at(path):
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")


def read_regular(stream, limit):
    node = os.fstat(stream.fileno())
    require(stat.S_ISREG(node.st_mode) and node.st_nlink == 1 and node.st_size <= limit, "ARCHIVO")
    raw = stream.read(limit + 1)
    after = os.fstat(stream.fileno())
    require(
        len(raw) == node.st_size
        and all(
            getattr(node, field) == getattr(after, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_uid",
                "st_gid",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        ),
        "ARCHIVO_CAMBIO",
    )
    return raw, node


def host():
    require(sys.platform == "linux" and os.geteuid() == 0, "HOST_PRIVILEGIO")
    require(socket.gethostname() == "vm-media-01", "HOST_NOMBRE")
    addresses = only_row(
        decode(command(["/usr/sbin/ip", "-j", "-4", "addr", "show", "dev", NIC], "HOST_IP"))
    )
    require(
        addresses["ifname"] == NIC
        and any(
            entry["local"] == IP and entry["prefixlen"] == 24 for entry in addresses["addr_info"]
        ),
        "HOST_IP",
    )
    for path in (INPUT.parent, INPUT, STATE.parent, STATE):
        directory(path, private=True)


def read_plan(stream):
    raw, node = read_regular(stream, 64 * 1024)
    require((node.st_uid, node.st_gid, stat.S_IMODE(node.st_mode)) == (0, 0, 0o600), "PLAN_MODO")
    plan = decode(raw)
    require(
        set(plan) == {"daemon", "image", "network_id", "containers", "nas", "compose_sha256"},
        "PLAN_CAMPOS",
    )
    require(
        set(plan["daemon"]) == {"ID", "ServerVersion", "DockerRootDir"}
        and all(
            isinstance(value, str) and 0 < len(value) <= 256 for value in plan["daemon"].values()
        ),
        "PLAN_DAEMON",
    )
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", plan["image"]), "PLAN_IMAGEN")
    require(
        DIGEST.fullmatch(plan["network_id"]) and DIGEST.fullmatch(plan["compose_sha256"]),
        "PLAN_SHA",
    )
    cids = plan["containers"]
    require(
        set(cids) == set(ROLES)
        and len(set(cids.values())) == 2
        and all(DIGEST.fullmatch(cid) for cid in cids.values()),
        "PLAN_CID",
    )
    with file_at(INPUT / "compose.yml") as compose:
        raw, node = read_regular(compose, 64 * 1024)
    require((node.st_uid, node.st_gid) == (0, 0) and node.st_mode & 0o022 == 0, "COMPOSE_MODO")
    require(hashlib.sha256(raw).hexdigest() == plan["compose_sha256"], "COMPOSE_SHA")
    return plan


def nas_layout(proof):
    expected = {
        ".",
        *ROLES,
        *(f"{role}/{kind}" for role in ROLES for kind in ("tasks", "local_videos")),
    }
    require(
        set(proof) == {"success", "path", "owner", "directories", "marker"}
        and proof["success"] is True
        and proof["owner"] == OWNER
        and proof["path"] == "/volume2/producciones/_moneyprinter"
        and set(proof["directories"]) == expected,
        "NAS_RECIBO",
    )
    rows = [line.split(" - ", 1) for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    mounts = [
        (left.split(), right.split()) for left, right in rows if left.split()[4] == str(MOUNT)
    ]
    require(len(mounts) == 1, "NAS_MONTAJE")
    ((left, right),) = mounts
    require(
        left[3] == "/"
        and "rw" in left[5].split(",")
        and right[:2] == ["nfs4", "192.168.0.8:/volume2/producciones"]
        and {"rw", "vers=4.1"} <= set(right[2].split(",")),
        "NAS_NFS41",
    )
    dev = os.makedev(*map(int, left[2].split(":")))
    mount = directory(MOUNT)
    require((mount.st_dev, mount.st_ino) == (dev, 105971713), "NAS_SHARE_IDENTIDAD")

    def same_node(node, record, mode):
        require(
            set(record) == {"inode", "mode", "uid", "gid"}
            and record["mode"] == oct(mode)
            and (record["uid"], record["gid"]) == (1024, 100)
            and (node.st_dev, node.st_ino, node.st_uid, node.st_gid)
            == (dev, record["inode"], record["uid"], record["gid"])
            and stat.S_IMODE(node.st_mode) in {mode, 0o777},
            "NAS_NODO",
        )

    for name, record in proof["directories"].items():
        same_node(directory(NAS / name), record, 0o700)
    with file_at(NAS / ".agency-owner") as stream:
        raw, node = read_regular(stream, 512)
    same_node(node, proof["marker"], 0o400)
    require(raw == OWNER.encode(), "NAS_MARCADOR")


def containers(plan):
    info = docker_json("info", "--format", "{{json .}}")
    require({key: info.get(key) for key in plan["daemon"]} == plan["daemon"], "DOCKER_DAEMON")
    image = only_row(docker_json("image", "inspect", plan["image"]))
    image_id = image["Id"]
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        and image["Os"] == "linux"
        and image["Architecture"] == "amd64",
        "DOCKER_IMAGEN",
    )
    net_id = plan["network_id"]
    network = only_row(docker_json("network", "inspect", net_id))
    require(
        network["Id"] == net_id
        and network["Name"] == NETWORK
        and network["Driver"] == "bridge"
        and network["EnableIPv6"] is False
        and network["Internal"] is False
        and network["Options"].get("com.docker.network.bridge.name") == BRIDGE
        and network["Labels"].get("agency.owner") == OWNER
        and set(network.get("Containers") or {}) <= set(plan["containers"].values()),
        "DOCKER_RED",
    )
    bearer = INPUT / "speaches"
    node = bearer.lstat()
    require(
        stat.S_ISREG(node.st_mode)
        and node.st_nlink == 1
        and (node.st_uid, node.st_gid, stat.S_IMODE(node.st_mode)) == (65532, 65532, 0o400),
        "DOCKER_BEARER",
    )
    for role, cid in plan["containers"].items():
        row = only_row(docker_json("container", "inspect", cid))
        config, settings, host_config = row["Config"], row["NetworkSettings"], row["HostConfig"]
        labels = config["Labels"]
        require(
            row["Id"] == cid
            and row["Image"] == image_id
            and config["User"] == "65532:65532"
            and config["Cmd"] == [role]
            and labels["agency.owner"] == OWNER
            and labels["com.docker.compose.project"] == PROJECT
            and labels["com.docker.compose.service"] == role,
            "DOCKER_CID",
        )
        require(
            host_config["RestartPolicy"] == {"Name": "no", "MaximumRetryCount": 0}
            and not host_config["PublishAllPorts"]
            and host_config["NetworkMode"] == NETWORK,
            "DOCKER_SUPERVISION",
        )
        ports = {"8501/tcp": [{"HostIp": IP, "HostPort": "8501"}]} if role == "ui" else {}
        require((host_config["PortBindings"] or {}) == ports, "DOCKER_PUERTOS")
        require(set(settings["Networks"]) == {NETWORK}, "DOCKER_CID_RED")
        endpoint = settings["Networks"][NETWORK]["NetworkID"]
        require(
            endpoint == net_id
            or (endpoint == "" and row["State"]["Status"] in {"created", "exited"}),
            "DOCKER_CID_RED_ID",
        )
        mounts = {
            mount["Destination"]: mount for mount in row["Mounts"] if mount["Type"] != "tmpfs"
        }
        secret = "/run/secrets/moneyprinter-speaches"
        sources = {
            "/run/mpt": STATE / role,
            "/run/mpt/storage/tasks": NAS / role / "tasks",
            "/run/mpt/storage/local_videos": NAS / role / "local_videos",
        }
        require(
            len(mounts)
            == len(row["Mounts"]) - sum(mount["Type"] == "tmpfs" for mount in row["Mounts"])
            and set(mounts) == {secret, *sources}
            and all(mount["Type"] == "bind" for mount in mounts.values()),
            "DOCKER_BINDS",
        )
        require(
            mounts[secret]["RW"] is False
            and mounts[secret]["Source"] == str(bearer)
            and all(
                mounts[target]["Source"] == str(src) and mounts[target]["RW"] is True
                for target, src in sources.items()
            ),
            "DOCKER_BINDS_ORIGEN",
        )


def tag(owner):
    return ["-m", "comment", "--comment", owner]


def action(args, owner, verdict):
    return [*args, *tag(owner), "-j", verdict]


def old_rules(table, chain):
    old = OLD_BRIDGE
    established = ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"]
    if table == "ip6tables":
        drops = [["-i", old]] if chain == "INPUT" else [["-i", old], ["-o", old]]
        return [action(args, OLD_OWNER, "DROP") for args in drops]
    if chain == "INPUT":
        return [
            action(["-i", old, *established], OLD_OWNER, "RETURN"),
            action(["-i", old], OLD_OWNER, "DROP"),
        ]
    return [
        action(
            [
                "-i",
                old,
                "!",
                "-o",
                old,
                "-p",
                "tcp",
                "-m",
                "tcp",
                "-d",
                SPEACHES,
                "--dport",
                "8000",
            ],
            OLD_OWNER,
            "RETURN",
        ),
        action(["-i", old, "!", "-o", old], OLD_OWNER, "DROP"),
        action(["!", "-i", old, "-o", old, *established], OLD_OWNER, "RETURN"),
        action(["!", "-i", old, "-o", old], OLD_OWNER, "DROP"),
    ]


def new_rules(table, chain):
    if table == "ip6tables":
        drops = [["-i", BRIDGE]] if chain == "INPUT" else [["-i", BRIDGE], ["-o", BRIDGE]]
        return [action(args, OWNER, "DROP") for args in drops]
    tcp = ["-p", "tcp", "-m", "tcp"]
    if chain == "INPUT":
        port = ["-d", IP, *tcp, "--dport", "8501"]
        return [
            action(args, OWNER, "DROP")
            for args in (
                ["-i", BRIDGE],
                ["!", "-i", NIC, *port],
                ["-i", NIC, "!", "-s", LAN, *port],
            )
        ]
    outbound = ["-i", BRIDGE, "-o", NIC, *tcp]
    inbound = ["-i", NIC, "-o", BRIDGE, *tcp]
    original = ["-m", "conntrack", "--ctstate", "NEW,ESTABLISHED", "--ctdir", "ORIGINAL"]
    reply = ["-m", "conntrack", "--ctstate", "ESTABLISHED", "--ctdir", "REPLY"]
    ui = ["--ctorigdst", IP, "--ctorigdstport", "8501"]
    whisper = ["--ctorigdst", SPEACHES, "--ctorigdstport", "8000"]
    permits = (
        [*outbound, "-d", SPEACHES, "--dport", "8000", *original, *whisper],
        [*inbound, "-s", SPEACHES, "--sport", "8000", *reply, *whisper],
        [*inbound, "-s", LAN, "--dport", "8501", *original, *ui],
        [*outbound, "-d", LAN, "--sport", "8501", *reply, *ui],
    )
    return [
        *[action(args, OWNER, "RETURN") for args in permits],
        action(["-i", BRIDGE], OWNER, "DROP"),
        action(["-o", BRIDGE], OWNER, "DROP"),
    ]


def rule_shape(tokens):
    """Normaliza el orden y la representación que imprime xtables en -S."""
    fields = []
    pending = iter(tokens)
    for option in pending:
        inverse = option == "!"
        if inverse:
            option = next(pending)
        require(
            option
            in {
                "-i",
                "-o",
                "-p",
                "-d",
                "-s",
                "-j",
                "-m",
                "--comment",
                "--ctstate",
                "--ctdir",
                "--ctorigdst",
                "--ctorigdstport",
                "--sport",
                "--dport",
            },
            "REGLA_FORMA",
        )
        value = next(pending)
        if option in {"-s", "-d", "--ctorigdst"}:
            value = str(ipaddress.ip_network(value, strict=True))
        if option == "--ctstate":
            value = ",".join(sorted(value.split(",")))
        fields.append((option, inverse, value))
    return sorted(fields)


def firewall_command(table, *args):
    return command([f"/usr/sbin/{table}", "-w", "3", *args], "FIREWALL")


def chain_rows(table, chain):
    output = firewall_command(table, "-S", chain).decode().splitlines()
    rows = [shlex.split(line) for line in output]
    require(
        rows
        and rows[0][:2] in (["-P", chain], ["-N", chain])
        and all(row[:2] == ["-A", chain] for row in rows[1:]),
        "FIREWALL_CADENA",
    )
    return [row[2:] for row in rows[1:]]


def table_state(table):
    forward = chain_rows(table, "FORWARD")
    inputs = chain_rows(table, "INPUT")
    # Observability's hooks select only its own bridge, before Docker's general jump.
    obs_target = OBS_GUARD if table == "iptables" else "DROP"
    obs_forward = [[direction, OBS_BRIDGE, "-j", obs_target] for direction in ("-i", "-o")]
    obs_input = ["-i", OBS_BRIDGE, "-j", "DROP"]
    obs_installed = forward[:2] == obs_forward
    docker_index = len(obs_forward) if obs_installed else 0
    require(forward[docker_index : docker_index + 1] == [["-j", "DOCKER-USER"]], "FIREWALL_FORWARD")
    require(
        (inputs[:1] == [obs_input]) == obs_installed
        and inputs.count(obs_input) == int(obs_installed)
        and all(forward.count(rule) == int(obs_installed) for rule in obs_forward),
        "FIREWALL_OBSERVABILIDAD",
    )
    statements, suffixes, states, old_states = [], {}, [], []
    for chain in CHAINS:
        old, new = old_rules(table, chain), new_rules(table, chain)
        offset = int(obs_installed) if chain == "INPUT" else 0
        rows = inputs if chain == "INPUT" else chain_rows(table, chain)
        count = sum(OWNER in row for row in rows)
        old_count = sum(OLD_OWNER in row for row in rows)
        require(
            count in {0, len(new)} and old_count in {0, len(old)},
            "FIREWALL_PROPIEDAD",
        )
        prefix = (old if old_count else []) + (new if count else [])
        stop = offset + len(prefix)
        require(
            [rule_shape(args) for args in rows[offset:stop]]
            == [rule_shape(args) for args in prefix],
            "FIREWALL_PRIORIDAD",
        )
        for args in prefix:
            firewall_command(table, "-C", chain, *args)
        suffixes[chain] = rows[:offset] + rows[stop:]
        require(all(BRIDGE not in rule for rule in rows[stop:]), "FIREWALL_PUENTE_AJENO")
        states.append(bool(count))
        old_states.append(bool(old_count))
        if not count:
            for index, args in enumerate(new, start=offset + old_count + 1):
                statements.append(" ".join(["-I", chain, str(index), *args]))
    require(len(set(states)) == 1, "FIREWALL_FAMILIA_PARCIAL")
    require(len(set(old_states)) == 1, "FIREWALL_ANTERIOR_PARCIAL")
    return statements, suffixes, old_states[0]


def restore(table, statements, test):
    payload = ("*filter\n" + "\n".join(statements) + "\nCOMMIT\n").encode()
    args = [f"/usr/sbin/{table}-restore", "--wait", "3", "--noflush"]
    command([*args, "--test"] if test else args, "FIREWALL_TRANSACCION", payload)


def firewall(verify_only):
    initial = {table: table_state(table) for table in TABLES}
    require(len({old for _, _, old in initial.values()}) == 1, "FIREWALL_ANTERIOR_PARCIAL")
    if verify_only:
        require(all(not statements for statements, _, _ in initial.values()), "FIREWALL_AUSENTE")
        return False
    for table, (statements, _, _) in initial.items():
        if statements:
            restore(table, statements, test=True)
    for table, (statements, suffixes, old) in initial.items():
        if statements:
            require(table_state(table) == (statements, suffixes, old), "FIREWALL_CAMBIO")
            restore(table, statements, test=False)
        require(table_state(table) == ([], suffixes, old), "FIREWALL_RESULTADO")
    return any(statements for statements, _, _ in initial.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        host()
        with file_at(INPUT / "guard-plan.json") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            plan = read_plan(stream)
            nas_layout(plan["nas"])
            containers(plan)
            changed = firewall(args.verify_only)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        StopIteration,
        subprocess.TimeoutExpired,
    ) as error:
        code = (
            str(error)
            if type(error) is ValueError and re.fullmatch(r"[A-Z0-9_]+", str(error))
            else type(error).__name__
        )
        print(json.dumps({"ok": False, "code": code}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "firewall_changed": changed, "verify_only": args.verify_only}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
