"""Prepara y arranca sólo el proyecto de colectores de la VM."""

import base64
import contextlib
import gzip
import hashlib
import ipaddress
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT = "agency-observability-nas-v1"
OWNER = "agencia-digital-marketing-ai:observability-nas:v1"
WORK = Path("/opt/media-platform/observability-nas-v1")
INCOMING = Path("/home/jandrade/agency-observability-images-v1")
TLS_SOURCE = Path("/home/jandrade/.agency-prometheus-bridge-client-v1/valid")
BRIDGE = "br-aobsoutv1"
GUARD = "AGENCY-OBS-OUT-V1"
NAS = "192.168.0.8"
VM = "192.168.0.85"
PORT = "9090"
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "COMPOSE_DISABLE_ENV_FILE": "true",
    "DOCKER_CONFIG": str(WORK / "docker-client"),
}
HEX = re.compile(r"[a-f0-9]{64}")
FILES = {
    "compose.nas.yml": "8a77429275f82e7f4a30d5b517f65bf84346052356516d39c3d2644d616da762",
    "otel-collector.nas.yml": "1932d114ecb6b421a7ca716253bb88685f46efc0d6d26c1c180758bdad5ab66b",
}
TLS = {
    "server-ca.pem": "72d5b2e0f57e609b8129dece37f3d753b10bf3c159b2f00c401c82bf17520527",
    "client.pem": "a4fc219c06cf50feeb651471cdac8b2789123560af8f3741d1b609cde0c0dfd5",
    "client.key": "ac16c2639d41e79e26498902b3c453426501c174f8107cb2df9aa0b258ff2ec1",
}
IMAGES = {
    "node-exporter": (
        "node-exporter",
        "1.12.0",
        "a5a52ec6f5bd3930fa30dfc9f305dc9a89cb07be47626ba6b0162209e0a45336",
        "b684df47e9904497f737b439f14d30bf4bd6c863da275954d9654b3f074e85e5",
        "092f05de83ff074cb2ba76d8ba9c1141439a7ce6bcaaadba1e0b7e66ec9d9050",
        "8eeb4a5a5d98d72d3c5bc74f181b78189ccc6d79b92b2ba5d9169badd4fda118",
        13466318,
        65534,
    ),
    "cadvisor": (
        "cadvisor",
        "0.60.6",
        "24b0d5e406d5d33471fbfcfa165efbad77a621035a69aba40f82529568f6d49f",
        "5e3c1171480318f3e580c4fafd1fe1559eabf2474b19cf64ef76e10f5f732322",
        "d9dca3fbe0ca16169cd5cd588f1fec62f455aea7eb83ddbe5d5d63022fe457a2",
        "5b89b4557dc2669ae5ddb19c2252c3a85529d573918a67b6dd44c010d3f7ba9e",
        20611800,
        65534,
    ),
    "otel-collector": (
        "otelcol-contrib",
        "0.165.0",
        "9242aaed61fe26dec2189d174cf9d1bc92bc9758c824b92cad05c781f9f211a8",
        "953927108e0f0e0a562d3c81923cb3f6b017c67505ea2dccd5375ae1f14bf624",
        "b90f3e7f3b31c2eaea2403a51b5e55d4f5dbe9a98f1255c9d3fc63144db29035",
        "c073ffa3a077b4b9b2318caf168482cf6ce54f6a4fc67529d0a79b792082d18d",
        120722265,
        61210,
    ),
}
LIMITS = {"node-exporter": 64, "cadvisor": 128, "otel-collector": 192}
ARCHIVES = {
    "node-exporter": (25098240, "bcceec159757f120f1e1e550ccf20d6674bc8887bdff71d41173a30311eb298e"),
    "cadvisor": (40663040, "f7bf1f0e112a9c4ff6a193ae3b137f87a40dc81eb42cea321012603e745614d5"),
    "otelcol-contrib": (
        410234880,
        "4f723455cba3c5d6effae207208de0b9d5f6152bcc936ab4d02c827c4c1e891d",
    ),
}
MOUNTS = {
    "node-exporter": {
        "/host-proc/stat": "/proc/stat",
        "/host-proc/meminfo": "/proc/meminfo",
        "/host-proc/loadavg": "/proc/loadavg",
        "/host-sys/devices/system/cpu": "/sys/devices/system/cpu",
    },
    "cadvisor": {"/sys/fs/cgroup": "/sys/fs/cgroup"},
    "otel-collector": {
        "/etc/agency-otel.yaml": str(WORK / "otel-collector.nas.yml"),
        "/run/agency-otel-tls": str(WORK / "collector-tls"),
    },
}


def require(condition, label):
    if not condition:
        raise ValueError(label)


def interrupted(_signal, _frame):
    raise ValueError("OPERACION_INTERRUMPIDA")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def run(argv, timeout=20, *, env=ENV, payload=None, stream=None, check=True):
    input_args = (
        {"input": payload} if payload is not None else {"stdin": stream or subprocess.DEVNULL}
    )
    result = subprocess.run(argv, env=env, capture_output=True, timeout=timeout, **input_args)
    require(len(result.stdout) + len(result.stderr) < 2 * 1024**2, "SALIDA_COTA")
    if check and result.returncode:
        raise ValueError("COMMAND_EXIT_" + str(result.returncode))
    return result


def docker(*args, **options):
    return run([*DOCKER, *args], **options)


def docker_json(*args):
    return json.loads(docker(*args).stdout)


def directory(path, uid, mode=None):
    node = path.lstat()
    require(
        stat.S_ISDIR(node.st_mode)
        and node.st_uid == uid
        and path.resolve(strict=True) == path
        and not node.st_mode & 0o022
        and (mode is None or stat.S_IMODE(node.st_mode) == mode),
        "DIRECTORIO",
    )
    return [node.st_dev, node.st_ino, node.st_uid]


@contextlib.contextmanager
def opened(path, uid, mode):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        node = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(node.st_mode)
            and node.st_uid == uid
            and node.st_nlink == 1
            and stat.S_IMODE(node.st_mode) == mode,
            "ARCHIVO_IDENTIDAD",
        )
        yield stream, node
        end = os.fstat(stream.fileno())
        require(
            (node.st_size, node.st_mtime_ns, node.st_ctime_ns)
            == (end.st_size, end.st_mtime_ns, end.st_ctime_ns),
            "ARCHIVO_CAMBIO",
        )


def read(path, uid, mode, maximum):
    with opened(path, uid, mode) as (stream, node):
        require(0 < node.st_size <= maximum, "ARCHIVO_COTA")
        data = stream.read(maximum + 1)
        require(len(data) == node.st_size, "ARCHIVO_LECTURA")
        return data


def write(path, raw, mode=0o600, uid=0):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchown(stream.fileno(), uid, uid)
        os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ip_route():
    rows = json.loads(run(["/usr/sbin/ip", "-j", "-4", "route", "get", NAS]).stdout)
    require(len(rows) == 1 and rows[0]["prefsrc"] == VM and not rows[0].get("gateway"), "RUTA_NAS")
    iface = rows[0]["dev"]
    require(re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", iface) and iface != BRIDGE, "RUTA_INTERFAZ")
    return iface


def host():
    require(os.geteuid() == 0 and os.uname().nodename == "vm-media-01", "HOST")
    require(Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == "1", "IP_FORWARD")
    parent = directory(WORK.parent, 0)
    info = docker_json(
        "info",
        "--format",
        '{"id":{{json .ID}},"version":{{json .ServerVersion}},"root":{{json .DockerRootDir}},"os":{{json .OSType}},"architecture":{{json .Architecture}},"cgroup_driver":{{json .CgroupDriver}},"cgroup_version":{{json .CgroupVersion}}}',
    )
    require(
        info["os"] == "linux"
        and info["architecture"] in {"x86_64", "amd64"}
        and info["cgroup_driver"] == "systemd"
        and info["cgroup_version"] == "2",
        "DAEMON",
    )
    version = docker("compose", "version", "--short").stdout.decode().strip()
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", version)
    require(match and tuple(map(int, match.groups())) >= (2, 33, 1), "COMPOSE_VERSION")
    return {"parent": parent, "daemon": info, "compose": version, "lan": ip_route()}


def disk_available(root):
    disk = os.statvfs(root)
    return disk.f_frsize * disk.f_bavail


def network_names():
    return {kind: f"{PROJECT}_{kind}" for kind in ("metrics", "egress")}


def ipam_conflicts(subnet):
    network = ipaddress.IPv4Network(subnet)
    routes = json.loads(run(["/usr/sbin/ip", "-j", "-4", "route", "show", "table", "all"]).stdout)
    existing = [row["dst"] for row in routes if row.get("dst") not in (None, "default")]
    ids = docker("network", "ls", "-q").stdout.decode().split()
    for network_info in docker_json("network", "inspect", *ids):
        existing.extend(row.get("Subnet", "") for row in network_info["IPAM"]["Config"] or [])
    return [
        value
        for value in existing
        if value
        and ":" not in value
        and ipaddress.IPv4Network(value, strict=False).overlaps(network)
    ]


def ip_config(request):
    network = ipaddress.IPv4Network(request["subnet"])
    address = ipaddress.IPv4Address(request["ip"])
    ranges = tuple(
        ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )
    require(
        any(network.subnet_of(value) for value in ranges)
        and network.prefixlen == 29
        and address in list(network.hosts())[1:]
        and not ipam_conflicts(str(network)),
        "IPAM",
    )
    return str(network), str(address)


def image_tag(info):
    return f"agency-observability/{info[0]}:{info[1]}-v1"


def saved_config(path, tag, pin, layer):
    with tarfile.open(path, "r:*") as tar:
        manifest = tar.getmember("manifest.json")
        require(manifest.isfile() and 0 < manifest.size < 32768, "IMAGEN_SAVE_MANIFEST")
        rows = json.load(tar.extractfile(manifest))
        require(
            len(rows) == 1 and tag in rows[0]["RepoTags"] and len(rows[0]["Layers"]) == 1,
            "IMAGEN_SAVE_TAG",
        )
        config = tar.getmember(rows[0]["Config"])
        require(config.isfile() and 0 < config.size < 65536, "IMAGEN_SAVE_CONFIG")
        raw = tar.extractfile(config).read()
    require(
        sha(raw) == pin and json.loads(raw)["rootfs"]["diff_ids"] == ["sha256:" + layer],
        "IMAGEN_SAVE_SHA",
    )


def inspect_image(info, missing_ok=False, proof=False):
    name, version, image_id, layer, binary, _, _, uid = info
    tag = image_tag(info)
    response = docker("image", "inspect", tag, check=False)
    if missing_ok and response.returncode:
        require(
            b"No such image" in response.stderr or b"No such object" in response.stderr,
            "IMAGEN_INSPECCION",
        )
        return None
    require(response.returncode == 0, "IMAGEN_INSPECCION")
    (row,) = json.loads(response.stdout)
    cfg = row["Config"]
    runtime = row["Id"]
    require(
        runtime.startswith("sha256:")
        and HEX.fullmatch(runtime[7:])
        and row["Architecture"] == "amd64"
        and row["Os"] == "linux"
        and row["RootFS"]["Layers"] == ["sha256:" + layer]
        and cfg["Entrypoint"] == ["/bin/" + name]
        and cfg["User"] == f"{uid}:{uid}"
        and cfg["StopSignal"] == "SIGTERM"
        and cfg["Labels"]["agency.owner"] == OWNER
        and cfg["Labels"]["agency.binary.sha256"] == binary
        and cfg["Labels"]["org.opencontainers.image.version"] == version,
        "IMAGEN_IDENTIDAD",
    )
    if runtime != "sha256:" + image_id:
        descriptor = row.get("Descriptor") or {}
        require(descriptor.get("digest") == runtime, "IMAGEN_DESCRIPTOR")
        if proof:
            with tempfile.TemporaryDirectory(prefix="image-proof-", dir=WORK) as temporary:
                save_path = Path(temporary) / "image.tar"
                docker("image", "save", "--output", str(save_path), tag, timeout=150)
                require(
                    0 < save_path.stat().st_size <= ARCHIVES[name][0] + 64 * 1024**2,
                    "IMAGEN_SAVE_COTA",
                )
                saved_config(save_path, tag, image_id, layer)
            require(inspect_image(info) == runtime, "IMAGEN_CAMBIO")
    return runtime


def import_images(out):
    directory(INCOMING, 1000, 0o700)
    for service, info in IMAGES.items():
        if inspect_image(info, missing_ok=True) is None:
            name, _, _, _, _, gzip_pin, size, _ = info
            with tempfile.TemporaryFile(dir=WORK) as staged:
                with opened(INCOMING / (name + ".docker.tar.gz"), 1000, 0o600) as (source, node):
                    require(node.st_size == size, "ARCHIVO_IMAGEN_TAMANO")
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: source.read(1024**2), b""):
                        digest.update(chunk)
                        staged.write(chunk)
                    require(
                        staged.tell() == size and digest.hexdigest() == gzip_pin,
                        "ARCHIVO_IMAGEN_SHA",
                    )
                staged.seek(0)
                size_tar, pin_tar = ARCHIVES[name]
                digest_tar = hashlib.sha256()
                total = 0
                with gzip.GzipFile(fileobj=staged) as content:
                    for chunk in iter(lambda: content.read(1024**2), b""):
                        total += len(chunk)
                        require(total <= size_tar, "ARCHIVO_TAR_TAMANO")
                        digest_tar.update(chunk)
                require(total == size_tar and digest_tar.hexdigest() == pin_tar, "ARCHIVO_TAR_SHA")
                staged.seek(0)
                docker("image", "load", "--quiet", stream=staged, timeout=150)
        out[service] = inspect_image(info, proof=True)


def compose(subnet, address, *args, **options):
    env = dict(
        ENV,
        AGENCY_OBS_EGRESS_SUBNET=subnet,
        AGENCY_OBS_EGRESS_IPV4=address,
        AGENCY_OBS_NODE_IMAGE=image_tag(IMAGES["node-exporter"]),
        AGENCY_OBS_CADVISOR_IMAGE=image_tag(IMAGES["cadvisor"]),
        AGENCY_OBS_OTEL_IMAGE=image_tag(IMAGES["otel-collector"]),
    )
    return docker(
        "compose",
        "--project-name",
        PROJECT,
        "--env-file",
        "/dev/null",
        "-f",
        str(WORK / "compose.nas.yml"),
        *args,
        env=env,
        **options,
    )


def iptables_save(family):
    binary = "/usr/sbin/iptables-save" if family == 4 else "/usr/sbin/ip6tables-save"
    raw = run([binary, "-t", "filter"]).stdout.decode()
    lines = [line for line in raw.splitlines() if line and not line.startswith("#")]
    require(lines and lines[0] == "*filter" and lines[-1] == "COMMIT", "FIREWALL_SNAPSHOT")
    chains = {}
    for line in lines[1:-1]:
        if line.startswith(":"):
            name, policy, _ = line[1:].split()
            chains[name] = (policy, [])
        else:
            command, name, *args = shlex.split(line)
            require(command == "-A" and name in chains, "FIREWALL_PARSE")
            chains[name][1].append(tuple(args))
    return chains


def firewall_rules(lan, address):
    return [
        (
            "-A",
            GUARD,
            "-s",
            address + "/32",
            "-d",
            NAS + "/32",
            "-i",
            BRIDGE,
            "-o",
            lan,
            "-p",
            "tcp",
            "-m",
            "tcp",
            "--dport",
            PORT,
            "-m",
            "conntrack",
            "--ctstate",
            "NEW,ESTABLISHED",
            "--ctdir",
            "ORIGINAL",
            "-j",
            "ACCEPT",
        ),
        (
            "-A",
            GUARD,
            "-s",
            NAS + "/32",
            "-d",
            address + "/32",
            "-i",
            lan,
            "-o",
            BRIDGE,
            "-p",
            "tcp",
            "-m",
            "tcp",
            "--sport",
            PORT,
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED",
            "--ctdir",
            "REPLY",
            "-j",
            "ACCEPT",
        ),
        ("-A", GUARD, "-j", "DROP"),
    ]


def delta(lan, address):
    return {
        4: (
            GUARD,
            firewall_rules(lan, address),
            [
                ("-i", BRIDGE, "-j", GUARD),
                ("-o", BRIDGE, "-j", GUARD),
                ("-i", BRIDGE, "-j", "DROP"),
            ],
        ),
        6: (
            None,
            [],
            [
                ("-i", BRIDGE, "-j", "DROP"),
                ("-o", BRIDGE, "-j", "DROP"),
                ("-i", BRIDGE, "-j", "DROP"),
            ],
        ),
    }


def restore_payload(family, policy, remove=False):
    guard, rules, hooks = policy
    targets = ("FORWARD", "FORWARD", "INPUT")
    rows = ["*filter"]
    if guard and not remove:
        rows.append(f":{guard} - [0:0]")
    if not remove:
        rows.extend(" ".join(row) for row in rules)
    require(len(targets) == len(hooks), "FIREWALL_PAYLOAD")
    for index in reversed(range(len(targets))):
        target, row = targets[index], hooks[index]
        action = f"-D {target}" if remove else f"-I {target} 1"
        rows.append(action + " " + " ".join(row))
    if guard and remove:
        rows.extend(" ".join(("-D", *row[1:])) for row in rules)
        rows.append(f"-X {guard}")
    return ("\n".join([*rows, "COMMIT", ""])).encode()


def verify_firewall(lan, address, present):
    observed = {}
    for family, policy in delta(lan, address).items():
        guard, rules, hooks = policy
        state = iptables_save(family)
        forward_rules, input_rules = state["FORWARD"][1], state["INPUT"][1]
        if present:
            require(
                forward_rules[:2] == hooks[:2]
                and input_rules[:1] == hooks[2:]
                and all(forward_rules.count(row) == 1 for row in hooks[:2])
                and input_rules.count(hooks[2]) == 1,
                "FIREWALL_HOOKS",
            )
            if guard:
                require(state.get(guard) == ("-", [row[2:] for row in rules]), "FIREWALL_CADENA")
        else:
            require(
                guard not in state
                and not any(row in forward_rules for row in hooks[:2])
                and hooks[2] not in input_rules,
                "FIREWALL_COLISION",
            )
        observed[family] = state
    return observed


def expected_firewall(before, policies, remove):
    expected = {}
    for family, state in before.items():
        chains = {name: (policy, list(rows)) for name, (policy, rows) in state.items()}
        guard, rules, hooks = policies[family]
        if remove:
            del chains["FORWARD"][1][:2]
            del chains["INPUT"][1][:1]
            if guard:
                del chains[guard]
        else:
            chains["FORWARD"][1][:0] = hooks[:2]
            chains["INPUT"][1][:0] = hooks[2:]
            if guard:
                chains[guard] = ("-", [row[2:] for row in rules])
        expected[family] = chains
    return expected


def change_firewall(lan, address, remove=False):
    before = verify_firewall(lan, address, remove)
    transactions = delta(lan, address)
    expected = expected_firewall(before, transactions, remove)
    for family, policy in transactions.items():
        binary = "/usr/sbin/iptables-restore" if family == 4 else "/usr/sbin/ip6tables-restore"
        run(
            [binary, "--noflush", "--wait", "5", "--test"],
            payload=restore_payload(family, policy, remove),
        )
    require(verify_firewall(lan, address, remove) == before, "FIREWALL_PREVIO_CAMBIO")
    committed = []
    try:
        for family, policy in transactions.items():
            binary = "/usr/sbin/iptables-restore" if family == 4 else "/usr/sbin/ip6tables-restore"
            run(
                [binary, "--noflush", "--wait", "5"],
                payload=restore_payload(family, policy, remove),
            )
            committed.append(family)
        require(verify_firewall(lan, address, not remove) == expected, "FIREWALL_DELTA")
    except BaseException:
        for family in reversed(committed):
            binary = "/usr/sbin/iptables-restore" if family == 4 else "/usr/sbin/ip6tables-restore"
            run(
                [binary, "--noflush", "--wait", "5"],
                payload=restore_payload(family, transactions[family], not remove),
            )
        require(verify_firewall(lan, address, remove) == before, "FIREWALL_ROLLBACK")
        raise


def inspect_container(service, cid, image_id, running=False):
    require(HEX.fullmatch(cid), "CID")
    (row,) = docker_json("container", "inspect", cid)
    cfg, hc = row["Config"], row["HostConfig"]
    info = IMAGES[service]
    uid = info[-1]
    labels = cfg["Labels"]
    expected_mounts = MOUNTS[service]
    mounts = row["Mounts"]
    require(
        row["Id"] == cid
        and row["Image"] == image_id
        and cfg["User"] == f"{uid}:{uid}"
        and labels["agency.owner"] == OWNER
        and labels["com.docker.compose.project"] == PROJECT
        and labels["com.docker.compose.service"] == service
        and hc["ReadonlyRootfs"]
        and not hc["Privileged"]
        and hc["CapDrop"] == ["ALL"]
        and not hc["CapAdd"]
        and not hc["PortBindings"]
        and hc["PidsLimit"] == 64
        and hc["RestartPolicy"]["Name"] == "no"
        and hc["Memory"] == hc["MemorySwap"] == LIMITS[service] * 1024**2
        and "no-new-privileges:true" in hc["SecurityOpt"]
        and not hc["Devices"]
        and (hc["CgroupnsMode"] == "host") == (service == "cadvisor")
        and all(m["Type"] == "bind" and not m["RW"] for m in mounts)
        and {m["Destination"]: m["Source"] for m in mounts} == expected_mounts,
        "CID_IDENTIDAD",
    )
    required_networks = {network_names()["metrics"]}
    if service == "otel-collector":
        required_networks.add(network_names()["egress"])
    require(set(row["NetworkSettings"]["Networks"]) == required_networks, "CID_REDES")
    require(
        row["State"]["Running"] == running and ((row["State"]["Pid"] > 0) == running), "CID_ESTADO"
    )
    return row


def inspect_networks(subnet, address, containers, active=False):
    names = network_names()
    rows = docker_json("network", "inspect", *names.values())
    require(len(rows) == 2, "REDES_COTA")
    results = {}
    for kind, name in names.items():
        (row,) = [r for r in rows if r["Name"] == name]
        require(
            row["Driver"] == "bridge"
            and not row["EnableIPv6"]
            and row["Internal"] == (kind == "metrics")
            and row["Labels"]["com.docker.compose.project"] == PROJECT
            and row["Labels"]["com.docker.compose.network"] == kind,
            "RED_IDENTIDAD",
        )
        members = set((row["Containers"] or {}).keys())
        allowed = set(containers.values()) if kind == "metrics" else {containers["otel-collector"]}
        require(members <= allowed and (not active or members == allowed), "RED_MIEMBROS")
        if kind == "egress":
            require(
                row["Options"]["com.docker.network.bridge.name"] == BRIDGE
                and [p["Subnet"] for p in row["IPAM"]["Config"]] == [subnet],
                "RED_EGRESO",
            )
            if active:
                require(
                    row["Containers"][containers["otel-collector"]]["IPv4Address"].split("/")[0]
                    == address,
                    "RED_DIRECCION",
                )
        results[kind] = row["Id"]
    return results


def stage_files(sources):
    require(set(sources) == set(FILES), "FUENTES")
    for name, pin in FILES.items():
        raw = base64.b64decode(sources[name], validate=True)
        require(0 < len(raw) < 32768 and sha(raw) == pin, "FUENTE_SHA")
        write(WORK / name, raw, 0o444)
    directory(TLS_SOURCE.parent, 1000, 0o700)
    directory(TLS_SOURCE, 1000, 0o700)
    tls = WORK / "collector-tls"
    tls.mkdir(mode=0o700)
    os.chown(tls, 61210, 61210)
    for name, pin in TLS.items():
        raw = read(TLS_SOURCE / name, 1000, 0o600, 4096)
        require(sha(raw) == pin, "TLS_SHA")
        write(tls / name, raw, 0o400, 61210)


def check_files():
    for name, pin in FILES.items():
        require(sha(read(WORK / name, 0, 0o444, 32768)) == pin, "FUENTE_SHA")
    directory(WORK / "collector-tls", 61210, 0o700)
    for name, pin in TLS.items():
        require(sha(read(WORK / "collector-tls" / name, 61210, 0o400, 4096)) == pin, "TLS_SHA")


def validator(out):
    mounts = []
    for target, source in MOUNTS["otel-collector"].items():
        mounts.extend(["--mount", f"type=bind,src={source},dst={target},readonly"])
    args = [
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user=61210:61210",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--pids-limit=64",
        "--memory=192m",
        "--memory-swap=192m",
        "--label=agency.owner=" + OWNER,
        *mounts,
    ]
    image = image_tag(IMAGES["otel-collector"])
    response = docker(
        "run",
        "--rm",
        *args,
        image,
        "validate",
        "--config=/etc/agency-otel.yaml",
        timeout=45,
        check=False,
    )
    out["collector_validator"] = {
        "exit": response.returncode,
        "output_sha256": sha(response.stdout + response.stderr),
    }
    if response.returncode:
        out["collector_validator"]["diagnostic"] = (response.stderr + response.stdout).decode(
            errors="replace"
        )[-800:]
    require(response.returncode == 0, "COLLECTOR_VALIDATE")


def prepare(request, observed, out):
    require(request["host"] == observed, "HOST_PLAN")
    require(disk_available(observed["daemon"]["root"]) > 2 * 1024**3, "DISCO_PREPARACION")
    subnet, address = ip_config(request)
    require(
        not WORK.exists() and not (Path("/sys/class/net") / BRIDGE).exists(), "COLISION_PROYECTO"
    )
    require(
        not docker(
            "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT
        ).stdout.strip(),
        "COLISION_CID",
    )
    for name in network_names().values():
        require(
            not docker("network", "ls", "-q", "--filter", "name=^" + name + "$").stdout.strip(),
            "COLISION_RED",
        )
    verify_firewall(observed["lan"], address, False)
    WORK.mkdir(mode=0o700)
    write(WORK / ".owner", OWNER.encode())
    (WORK / "docker-client").mkdir(mode=0o700)
    out["step"] = "stage-files"
    stage_files(request["sources"])
    check_files()
    out["step"] = "images"
    out["images"] = {}
    import_images(out["images"])
    out["step"] = "validate"
    compose(subnet, address, "config", "--quiet")
    validator(out)
    out["step"] = "create"
    create = compose(
        subnet, address, "create", "--no-build", "--pull", "never", timeout=90, check=False
    )
    out["create_exit"] = create.returncode
    ids = {}
    out["containers"] = ids
    for service in IMAGES:
        cid = compose(subnet, address, "ps", "--all", "--quiet", service).stdout.decode().strip()
        if cid:
            ids[service] = cid
            require(
                inspect_container(service, cid, out["images"][service])["State"]["Status"]
                == "created",
                "CID_CREADO",
            )
    require(create.returncode == 0 and set(ids) == set(IMAGES), "COMPOSE_CREATE")
    out["networks"] = inspect_networks(subnet, address, ids)
    out["step"] = "firewall"
    firewall = {
        "lan": observed["lan"],
        "address": address,
        "policy": delta(observed["lan"], address),
    }
    receipt = {
        "owner": OWNER,
        "host": observed,
        "directory": directory(WORK, 0, 0o700),
        "files": FILES,
        "images": out["images"],
        "containers": ids,
        "networks": out["networks"],
        "subnet": subnet,
        "address": address,
        "firewall": firewall,
        "collector_validator": out["collector_validator"],
        "services_started": False,
    }
    raw = canonical(receipt)
    write(WORK / "firewall.intent.json", raw)
    out["recovery_intent_sha256"] = sha(raw)
    change_firewall(observed["lan"], address)
    write(WORK / "prepared.json", raw)
    out.update(receipt_sha256=sha(raw), prepared=receipt, services_started=False)


def get_receipt(request, observed, allow_intent=False):
    require(HEX.fullmatch(request["receipt_sha256"]), "RECIBO_ARGUMENTO")
    directory(WORK, 0, 0o700)
    require(read(WORK / ".owner", 0, 0o600, 128) == OWNER.encode(), "RAIZ_OWNER")
    path = WORK / "prepared.json"
    if allow_intent and not path.exists():
        path = WORK / "firewall.intent.json"
    raw = read(path, 0, 0o600, 32768)
    receipt = json.loads(raw)
    require(
        sha(raw) == request["receipt_sha256"]
        and receipt["owner"] == OWNER
        and receipt["host"] == observed
        and receipt["directory"] == directory(WORK, 0, 0o700)
        and receipt["files"] == FILES,
        "RECIBO_IDENTIDAD",
    )
    check_files()
    require(
        set(receipt["containers"]) == set(IMAGES)
        and receipt["images"] == {service: inspect_image(info) for service, info in IMAGES.items()},
        "RECIBO_RECURSOS",
    )
    return receipt


def clear_firewall(lan, address):
    try:
        verify_firewall(lan, address, False)
    except ValueError as error:
        require(str(error) == "FIREWALL_COLISION", "FIREWALL_RETIRO_PREVIO")
        change_firewall(lan, address, remove=True)


def stop_owned(containers, images):
    stopped = {}
    for service in reversed(IMAGES):
        if service not in containers:
            continue
        cid = containers[service]
        (row,) = docker_json("container", "inspect", cid)
        inspect_container(service, cid, images[service], row["State"]["Running"])
        if row["State"]["Running"]:
            docker("stop", "--time", "10", cid, timeout=20)
        state = inspect_container(service, cid, images[service])["State"]
        stopped[service] = {
            "exit": state["ExitCode"],
            "status": state["Status"],
            "pid": state["Pid"],
        }
    return stopped


def start(receipt, out):
    ids = receipt["containers"]
    images = receipt["images"]
    subnet, address = receipt["subnet"], receipt["address"]
    lan = receipt["host"]["lan"]
    require(inspect_networks(subnet, address, ids) == receipt["networks"], "RED_RECIBO")
    verify_firewall(lan, address, True)
    for service, cid in ids.items():
        require(
            inspect_container(service, cid, images[service])["State"]["Status"]
            in {"created", "exited"},
            "CID_PRE_START",
        )
    started = {}
    out["running"] = started
    try:
        for service in IMAGES:
            cid = ids[service]
            started[service] = cid
            docker("start", cid, timeout=35)
            inspect_container(service, cid, images[service], True)
        require(
            inspect_networks(subnet, address, ids, active=True) == receipt["networks"], "RED_ACTIVA"
        )
        verify_firewall(lan, address, True)
        out["services_started"] = True
    except BaseException:
        out["stopped"] = stop_owned(started, images)
        raise


def main():
    out = {"success": False, "services_started": False}
    try:
        require(
            len(sys.argv) == 2 and sys.argv[1] in {"plan", "prepare", "start", "stop"}, "ACCION"
        )
        action = sys.argv[1]
        out["action"] = action
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        require(len(raw) <= 128 * 1024, "ENTRADA_COTA")
        request = json.loads(raw or b"{}")
        os.umask(0o077)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        observed = host()
        out["host"] = observed
        if action == "plan":
            require(not request or set(request) == {"subnet", "ip"}, "ENTRADA")
            if request:
                out["subnet"], out["ip"] = ip_config(request)
                verify_firewall(observed["lan"], out["ip"], False)
            out["sources"] = FILES
            out["incoming"] = str(INCOMING)
            out["docker_free_bytes"] = disk_available(observed["daemon"]["root"])
        elif action == "prepare":
            require(set(request) == {"host", "subnet", "ip", "sources"}, "ENTRADA")
            prepare(request, observed, out)
        else:
            require(set(request) == {"receipt_sha256"}, "ENTRADA")
            receipt = get_receipt(request, observed, allow_intent=action == "stop")
            if action == "start":
                out["docker_free_bytes"] = disk_available(observed["daemon"]["root"])
                require(out["docker_free_bytes"] >= 64 * 1024**2, "DISCO_ARRANQUE")
                start(receipt, out)
            else:
                out["stopped"] = stop_owned(receipt["containers"], receipt["images"])
                clear_firewall(observed["lan"], receipt["address"])
        out["success"] = True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        out["error"] = (
            str(error)
            if type(error) is ValueError and re.fullmatch(r"[A-Z0-9_]+", str(error))
            else type(error).__name__
        )
    print(canonical(out).decode(), flush=True)
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
