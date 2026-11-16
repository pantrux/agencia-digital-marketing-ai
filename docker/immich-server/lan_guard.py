"""Verifica el despliegue Immich y conserva su aislamiento al reactivarlo."""

import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

CONTROL = Path("/opt/media-platform/immich-lan-v1")
STATE = Path("/var/lib/media-platform/immich-lan-v1")
MOUNT = Path("/mnt/producciones")
PROJECT = "agency-media-immich-lan"
OWNER = "agencia-digital-marketing-ai:immich-lan:v1"
PG_OWNER = "agencia-digital-marketing-ai:postgres-immich-lan:v1"
HOST, NIC, LAN = "192.168.0.85", "media0", "192.168.0.0/24"
GPU, GPU_PORT, PORT = "192.168.0.6", "3003", "2283"
BR_LAN, BR_GPU = "br-immich-lan", "br-immich-eg"
NETWORKS = {
    "data": (
        "data",
        "172.31.241.0/28",
        None,
        ("postgres-immich", "immich-valkey", "immich-server"),
    ),
    "ml": ("ml", "172.31.241.16/28", None, ("immich-ml-auth", "immich-server")),
    "ml_egress": ("ml-egress", "172.31.241.32/28", BR_GPU, ("immich-ml-auth",)),
    "lan": ("ingress", "172.31.241.48/28", BR_LAN, ("immich-server",)),
}
IMAGES = {
    "postgres-immich": ("4e2f960dcbc4c5fecf8c8b8a07231700c2f8920350539220ef6e8c6053c78dae", 70),
    "immich-valkey": ("de6fbe9626fea9f6f297b6867d2c8cf8f0394ee5ed171613166fc093cb49d0a6", 1000),
    "immich-ml-auth": ("077a815fb7f45eedee7fdb799d52519de21266884eb0bcdd6b38d0b78ca0f723", 65532),
    "immich-server": ("9379466497dbddf101825a04ffb90e06ff5a2ebb49250a0c3810944c6b67392c", 1000),
}
NEIGHBORS = {
    "agencia-digital-marketing-ai:moneyprinter-functional:v1": ("br-mpt-fn", "8501"),
    "agencia-digital-marketing-ai:moneyprinter-lan:v1": ("br-mpt-lan", "8501"),
    "agency-supoclip-trial-nov13": ("sco24754668cc", "3080"),
    "agencia-digital-marketing-ai:n8n-private:v1": ("br-ag-n8n-in", "5678"),
}
OBS = "br-aobsoutv1"
N8N_INBOX = (
    "-s",
    "172.31.240.2/32",
    "-d",
    "172.31.240.1/32",
    "-i",
    "br-ag-n8n-in",
    "-p",
    "tcp",
    "-m",
    "tcp",
    "--dport",
    "3160",
    "-m",
    "comment",
    "--comment",
    "agencia-digital-marketing-ai:n8n-production-inbox:v1",
    "-j",
    "ACCEPT",
)
CHAINS = ("INPUT", "DOCKER-USER")
DOCKER_CHAINS = {
    "DOCKER",
    "DOCKER-BRIDGE",
    "DOCKER-CT",
    "DOCKER-FORWARD",
    "DOCKER-INTERNAL",
    "DOCKER-ISOLATION-STAGE-1",
    "DOCKER-ISOLATION-STAGE-2",
}
DIGEST = re.compile(r"[a-f0-9]{64}")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"}


def require(ok, code):
    if not ok:
        raise ValueError(code)


def run(*args, payload=None, timeout=15, include_stderr=False):
    proc = subprocess.run(
        args, input=payload, capture_output=True, env=ENV, check=False, timeout=timeout
    )
    output = proc.stdout + proc.stderr if include_stderr else proc.stdout
    require(proc.returncode == 0 and len(output) <= 4 * 1024**2, "OPERACION_RECHAZADA")
    return output


def docker(*args, timeout=15, include_stderr=False):
    return run(
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        *args,
        timeout=timeout,
        include_stderr=include_stderr,
    )


def compose(*args, timeout=270):
    return docker(
        "compose",
        "--ansi",
        "never",
        "--project-name",
        PROJECT,
        "--project-directory",
        str(CONTROL),
        "--env-file",
        str(CONTROL / "compose.env"),
        "--file",
        str(CONTROL / "compose.yml"),
        *args,
        timeout=timeout,
    )


def unique(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "JSON_DUPLICADO")
    return result


def json_load(raw):
    return json.loads(raw, object_pairs_hook=unique)


def private_file(path, owner=0):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        node = os.fstat(file.fileno())
        require(
            stat.S_ISREG(node.st_mode)
            and node.st_nlink == 1
            and node.st_uid == owner
            and node.st_gid == owner
            and node.st_mode & 0o022 == 0
            and 0 < node.st_size <= 65536,
            "ARCHIVO_CONTROL",
        )
        raw = file.read(65537)
        current = os.fstat(file.fileno())
        stable = (
            "st_dev",
            "st_ino",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_mode",
            "st_nlink",
        )
        require(
            len(raw) == node.st_size
            and all(getattr(node, field) == getattr(current, field) for field in stable),
            "ARCHIVO_CAMBIO",
        )
    return raw, stat.S_IMODE(node.st_mode)


def directory(path):
    node = path.lstat()
    require(stat.S_ISDIR(node.st_mode) and path.resolve(strict=True) == path, "DIRECTORIO_RUTA")
    return node


def load_plan():
    require(
        sys.platform == "linux" and os.geteuid() == 0 and socket.gethostname() == "vm-media-01",
        "HOST_IDENTIDAD",
    )
    ctl = directory(CONTROL)
    require((ctl.st_uid, ctl.st_gid, stat.S_IMODE(ctl.st_mode)) == (0, 0, 0o700), "CONTROL_MODO")
    raw, mode = private_file(CONTROL / "guard-plan.json")
    require(mode == 0o600, "PLAN_MODO")
    plan = json_load(raw)
    require(
        set(plan)
        == {
            "networks",
            "containers",
            "library_path",
            "data_path",
            "compose_sha256",
            "compose_env_sha256",
            "config_sha256",
        },
        "PLAN_CAMPOS",
    )
    for key, names in (("networks", NETWORKS), ("containers", IMAGES)):
        items = plan[key]
        require(
            set(items) == set(names)
            and len(set(items.values())) == len(names)
            and all(DIGEST.fullmatch(value) for value in items.values()),
            "PLAN_IDENTIDADES",
        )
    for key, name in (
        ("compose_sha256", "compose.yml"),
        ("compose_env_sha256", "compose.env"),
        ("config_sha256", "immich.lan.json"),
    ):
        raw, mode = private_file(CONTROL / name)
        require(hashlib.sha256(raw).hexdigest() == plan[key], "CONTROL_SHA")
        require(name != "compose.env" or mode == 0o600, "REFERENCIAS_MODO")
    return plan


def network_host(plan):
    rows = json_load(run("/usr/sbin/ip", "-j", "-4", "addr", "show", "dev", NIC))
    require(
        any(
            row["ifname"] == NIC
            and any(
                address.get("local") == HOST and address.get("prefixlen") == 24
                for address in row["addr_info"]
            )
            for row in rows
        ),
        "HOST_LAN",
    )
    mounts = [
        line.split(" - ", 1) for line in Path("/proc/self/mountinfo").read_text().splitlines()
    ]
    targets = [(left.split(), right.split()) for left, right in mounts]
    nas = [(left, right) for left, right in targets if left[4] == str(MOUNT)]
    require(len(nas) == 1, "NAS_MONTAJE")
    left, right = nas[0]
    require(
        left[3] == "/"
        and right[:2] == ["nfs4", "192.168.0.8:/volume2/producciones"]
        and {"rw", "vers=4.1", "proto=tcp"} <= set(right[2].split(",")),
        "NAS_ORIGEN",
    )
    lib, data = Path(plan["library_path"]), Path(plan["data_path"])
    library_root = MOUNT / "_biblioteca_global"
    data_root = MOUNT / "_apps/immich-lan-v1"
    require(lib != library_root and lib.is_relative_to(library_root), "NAS_BIBLIOTECA")
    require(
        data == STATE / "data" or (data != data_root and data.is_relative_to(data_root)),
        "IMMICH_DATA_RUTA",
    )
    dev = os.makedev(*map(int, left[2].split(":")))
    require(directory(lib).st_dev == directory(MOUNT).st_dev == dev, "NAS_DISPOSITIVO")
    if data.is_relative_to(data_root):
        require(directory(data).st_dev == dev, "NAS_DATA_DISPOSITIVO")
    local = directory(STATE).st_dev
    require(local != dev, "NVME_DISPOSITIVO")
    for path, uid in ((STATE / "postgres", 70), (STATE / "valkey", 1000), (data, 1000)):
        node = directory(path)
        if not path.is_relative_to(data_root):
            require(
                (node.st_dev, node.st_uid, node.st_gid, stat.S_IMODE(node.st_mode))
                == (local, uid, uid, 0o700),
                "NVME_PERMISOS",
            )
    require(
        not any((path := Path(row[4])).is_relative_to(lib) for row, _ in targets), "NAS_SUBMONTAJE"
    )


def check_containers(plan):
    cids = plan["containers"]
    observed = docker(
        "ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}"
    )
    require(set(observed.decode().split()) == set(cids.values()), "DOCKER_PROYECTO")
    rows = json_load(docker("container", "inspect", *cids.values()))
    require({row["Id"] for row in rows} == set(cids.values()), "DOCKER_CID")
    observed_state = {}
    for service, cid in cids.items():
        row = next(item for item in rows if item["Id"] == cid)
        config, host, current = row["Config"], row["HostConfig"], row["State"]
        image, uid = IMAGES[service]
        labels = config["Labels"]
        expected_networks = {
            f"agency-immich-lan-{role}-v1": plan["networks"][key]
            for key, (role, _, _, members) in NETWORKS.items()
            if service in members
        }
        networks = row["NetworkSettings"]["Networks"]
        expected_ports = (
            {"2283/tcp": [{"HostIp": HOST, "HostPort": PORT}]} if service == "immich-server" else {}
        )
        require(
            row["Image"] == "sha256:" + image
            and config["User"] == f"{uid}:{uid}"
            and labels["agency.owner"] == (PG_OWNER if service == "postgres-immich" else OWNER)
            and labels["com.docker.compose.project"] == PROJECT
            and labels["com.docker.compose.service"] == service
            and host["ReadonlyRootfs"]
            and not host["Privileged"]
            and host["CapDrop"] == ["ALL"]
            and host["RestartPolicy"]["Name"] == "no"
            and not host["PublishAllPorts"]
            and (host["PortBindings"] or {}) == expected_ports
            and set(networks) == set(expected_networks)
            and all(
                networks[key]["NetworkID"] == pin
                or (not current["Running"] and networks[key]["NetworkID"] == "")
                for key, pin in expected_networks.items()
            ),
            "DOCKER_CONTRATO",
        )
        mounts = {item["Destination"]: item for item in row["Mounts"] if item["Type"] == "bind"}
        required = {
            "postgres-immich": {"/var/lib/postgresql/data": (str(STATE / "postgres"), True)},
            "immich-valkey": {"/data": (str(STATE / "valkey"), True)},
            "immich-ml-auth": {},
            "immich-server": {
                "/data": (plan["data_path"], True),
                "/external/producciones": (plan["library_path"], False),
                "/run/immich/lan.json": (str(CONTROL / "immich.lan.json"), False),
            },
        }[service]
        require(
            all(
                target in mounts and (mounts[target]["Source"], mounts[target]["RW"]) == expected
                for target, expected in required.items()
            ),
            "DOCKER_MOUNTS",
        )
        secrets = {
            "postgres-immich": ("postgres_password", "postgres_pgpass"),
            "immich-valkey": ("immich_redis_acl", "immich_redis_password"),
            "immich-ml-auth": ("gpu_bridge_bearer",),
            "immich-server": ("immich_db_password", "immich_redis_password"),
        }[service]
        secret_paths = {f"/run/secrets/{name}" for name in secrets}
        require(
            set(mounts) == set(required) | secret_paths
            and all(not mounts[target]["RW"] for target in secret_paths)
            and all(item["Type"] in {"bind", "tmpfs"} for item in row["Mounts"]),
            "DOCKER_MOUNTS_AJENOS",
        )
        observed_state[service] = current
    for key, (role, subnet, bridge, members) in NETWORKS.items():
        (row,) = json_load(docker("network", "inspect", f"agency-immich-lan-{role}-v1"))
        require(
            row["Id"] == plan["networks"][key]
            and row["Name"] == f"agency-immich-lan-{role}-v1"
            and row["Driver"] == "bridge"
            and row["Internal"] is (bridge is None)
            and not row["EnableIPv6"]
            and [item["Subnet"] for item in row["IPAM"]["Config"]] == [subnet]
            and (
                bridge is None
                or row["IPAM"]["Config"][0].get("Gateway")
                == str(ipaddress.ip_network(subnet).network_address + 1)
            )
            and (bridge is None or row["Options"].get("com.docker.network.bridge.name") == bridge)
            and set(row["Containers"] or {}) <= {cids[member] for member in members},
            "DOCKER_RED",
        )
    return observed_state


def rules(family):
    mark = ("-m", "comment", "--comment", OWNER)
    input_rows = [("-i", bridge, *mark, "-j", "DROP") for bridge in (BR_LAN, BR_GPU)]
    forward_rows = [
        (direction, bridge, *mark, "-j", "DROP")
        for bridge in (BR_LAN, BR_GPU)
        for direction in ("-i", "-o")
    ]
    if family == 6:
        return {"INPUT": input_rows, "DOCKER-USER": forward_rows}

    local = ("-d", HOST, "-p", "tcp", "-m", "tcp", "--dport", PORT)
    original = ("-m", "conntrack", "--ctstate", "NEW,ESTABLISHED", "--ctdir", "ORIGINAL")
    reply = ("-m", "conntrack", "--ctstate", "ESTABLISHED", "--ctdir", "REPLY")
    published = ("--ctorigdst", HOST, "--ctorigdstport", PORT)
    tcp = ("-p", "tcp", "-m", "tcp")
    ingress = NETWORKS["lan"][1]
    gateway = str(ipaddress.ip_network(ingress).network_address + 1)
    input_rows[:0] = [
        (
            "-i",
            BR_LAN,
            "-s",
            ingress,
            "-d",
            gateway,
            *tcp,
            "--sport",
            PORT,
            *reply,
            *mark,
            "-j",
            "ACCEPT",
        ),
    ]
    input_rows.extend(
        (
            ("-i", NIC, "-s", LAN, *local, *mark, "-j", "ACCEPT"),
            ("!", "-i", "lo", *local, *mark, "-j", "DROP"),
        )
    )
    forward_rows[:0] = [
        (
            "-i",
            NIC,
            "-o",
            BR_LAN,
            "-s",
            LAN,
            *tcp,
            "--dport",
            PORT,
            *original,
            *published,
            *mark,
            "-j",
            "RETURN",
        ),
        (
            "-i",
            BR_LAN,
            "-o",
            NIC,
            "-d",
            LAN,
            *tcp,
            "--sport",
            PORT,
            *reply,
            *published,
            *mark,
            "-j",
            "RETURN",
        ),
        (
            "-i",
            BR_GPU,
            "-o",
            NIC,
            "-d",
            GPU,
            *tcp,
            "--dport",
            GPU_PORT,
            *original,
            *mark,
            "-j",
            "RETURN",
        ),
        (
            "-i",
            NIC,
            "-o",
            BR_GPU,
            "-s",
            GPU,
            *tcp,
            "--sport",
            GPU_PORT,
            *reply,
            *mark,
            "-j",
            "RETURN",
        ),
    ]
    return {"INPUT": input_rows, "DOCKER-USER": forward_rows}


def shape(row):
    fields, inverse, tokens = [], False, iter(row)
    for option in tokens:
        if option == "!":
            inverse = True
            continue
        value = next(tokens)
        if option in {"-s", "-d", "--ctorigdst"}:
            value = str(ipaddress.ip_network(value))
        if option == "--ctstate":
            value = ",".join(sorted(value.split(",")))
        fields.append((option, value, inverse))
        inverse = False
    return sorted(fields)


def parse_snapshot(raw):
    policies, rows = {}, {}
    for line in raw.decode().splitlines():
        if line.startswith(":"):
            chain, policy, _ = line[1:].split()
            require(chain not in rows, "FIREWALL_SNAPSHOT")
            policies[chain], rows[chain] = policy, []
        elif line.startswith("-A "):
            _, chain, *args = shlex.split(line)
            rows[chain].append(tuple(args))
        elif line and line not in {"*filter", "COMMIT"}:
            require(line.startswith("#"), "FIREWALL_SNAPSHOT")
    require({"INPUT", "FORWARD", "DOCKER-USER"} <= rows.keys(), "FIREWALL_CADENAS")
    return policies, rows


def snapshot(family):
    binary = "/usr/sbin/iptables-save" if family == 4 else "/usr/sbin/ip6tables-save"
    return parse_snapshot(run(binary, "-t", "filter"))


def prefix(rows, family):
    inputs = rows["INPUT"]
    # El receptor de producción inserta su regla privada antes del colector.
    inbox_offset = int(family == 4 and inputs[:1] == [N8N_INBOX])
    require(not inbox_offset or inputs.count(N8N_INBOX) == 1, "FIREWALL_N8N_DUPLICADO")
    obs_in = ("-i", OBS, "-j", "DROP")
    jump = "AGENCY-OBS-OUT-V1" if family == 4 else "DROP"
    obs_forward = [(flag, OBS, "-j", jump) for flag in ("-i", "-o")]
    has_obs = obs_in in rows["INPUT"] or any(row in rows["FORWARD"] for row in obs_forward)
    expected = [*obs_forward, ("-j", "DOCKER-USER")] if has_obs else [("-j", "DOCKER-USER")]
    require(rows["FORWARD"][: len(expected)] == expected, "FIREWALL_FORWARD")
    require(
        not has_obs
        or (
            inputs[inbox_offset : inbox_offset + 1] == [obs_in]
            and inputs.count(obs_in) == 1
            and all(rows["FORWARD"].count(row) == 1 for row in obs_forward)
        ),
        "FIREWALL_OBSERVABILIDAD",
    )
    offsets = {}
    for chain in CHAINS:
        offset = int(has_obs) + inbox_offset if chain == "INPUT" else 0
        entries = rows[chain]
        for row in entries[offset:]:
            tags = set(row) & NEIGHBORS.keys()
            if not tags:
                break
            require(len(tags) == 1, "FIREWALL_VECINO_ETIQUETA")
            tag = tags.pop()
            bridge, port = NEIGHBORS[tag]
            fields = shape(row)
            require(
                ("--comment", tag, False) in fields
                and (
                    any((flag, bridge, False) in fields for flag in ("-i", "-o"))
                    or any((flag, port, False) in fields for flag in ("--dport", "--ctorigdstport"))
                ),
                "FIREWALL_VECINO_REGLA",
            )
            offset += 1
        require(
            not any(set(row) & NEIGHBORS.keys() for row in entries[offset:]),
            "FIREWALL_VECINO_ORDEN",
        )
        offsets[chain] = offset
    return offsets


def owns(row):
    return any(row[index : index + 2] == ("--comment", OWNER) for index in range(len(row) - 1))


def desired_snapshot(current, family, remove=False):
    policies, rows = current
    expected = {chain: [] for chain in rows}
    desired = rules(family)
    for chain, entries in rows.items():
        allowed = [shape(row) for row in desired.get(chain, ())]
        present = []
        for row in entries:
            if owns(row):
                form = shape(row)
                require(form in allowed and form not in present, "FIREWALL_REGLA_PROPIA")
                present.append(form)
                continue
            require(
                chain in DOCKER_CHAINS or not (set(row) & {BR_LAN, BR_GPU}), "FIREWALL_BRIDGE_AJENO"
            )
            expected[chain].append(row)
    if not remove:
        for chain, offset in prefix(expected, family).items():
            expected[chain][offset:offset] = desired[chain]
    return policies, expected


def same_snapshot(left, right):
    left_policies, left_rows = left
    right_policies, right_rows = right
    if left_policies != right_policies:
        return False
    for chain, rows in left_rows.items():
        other = right_rows[chain]
        if len(rows) != len(other):
            return False
        for index, row in enumerate(rows):
            match = other[index]
            if row != match and (not owns(row) or not owns(match) or shape(row) != shape(match)):
                return False
    return True


def quote(field):
    require(re.fullmatch(r"[-a-zA-Z0-9_!.,/:]+", field), "FIREWALL_TOKEN")
    return field


def delta(before, after):
    payload = ["*filter"]
    for chain in CHAINS:
        payload.extend(
            " ".join(map(quote, ("-D", chain, *row))) for row in before[1][chain] if owns(row)
        )
        payload.extend(
            " ".join(map(quote, ("-I", chain, str(index), *row)))
            for index, row in enumerate(after[1][chain], 1)
            if owns(row)
        )
    return ("\n".join([*payload, "COMMIT", ""])).encode()


def restore(family, payload, test=False):
    binary = "/usr/sbin/iptables-restore" if family == 4 else "/usr/sbin/ip6tables-restore"
    flags = ("--test",) if test else ()
    run(binary, "--wait", "5", "--noflush", *flags, payload=payload)


def rollback(family, before):
    current = snapshot(family)
    if same_snapshot(current, before):
        return
    current_clean = desired_snapshot(current, family, remove=True)
    prior_clean = desired_snapshot(before, family, remove=True)
    if same_snapshot(current_clean, prior_clean):
        target = before
    elif same_snapshot(before, prior_clean):
        target = current_clean
    else:
        raise ValueError("FIREWALL_ROLLBACK_CONCURRENTE")
    payload = delta(current, target)
    restore(family, payload, test=True)
    require(snapshot(family) == current, "FIREWALL_ROLLBACK_CONCURRENTE")
    restore(family, payload)
    require(same_snapshot(snapshot(family), target), "FIREWALL_ROLLBACK")


def firewall(action):
    families = (4, 6)
    before = {family: snapshot(family) for family in families}
    expected = {
        family: desired_snapshot(before[family], family, remove=action == "remove")
        for family in families
    }
    changes = {
        family: delta(before[family], expected[family])
        for family in families
        if not same_snapshot(before[family], expected[family])
    }
    require(action != "check" or not changes, "FIREWALL_PENDIENTE")
    for family, payload in changes.items():
        restore(family, payload, test=True)
    applied = []
    try:
        for family, payload in changes.items():
            require(snapshot(family) == before[family], "FIREWALL_CAMBIO_CONCURRENTE")
            applied.append(family)
            restore(family, payload)
        for family in families:
            require(same_snapshot(snapshot(family), expected[family]), "FIREWALL_DERIVA")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        for family in reversed(applied):
            rollback(family, before[family])
        raise
    return {
        str(family): hashlib.sha256(repr(snapshot(family)).encode()).hexdigest()
        for family in families
    }


def stop(plan, verify_proxy=True):
    check_containers(plan)
    compose("stop", timeout=335)
    states = check_containers(plan)
    require(all(not row["Running"] and row["Pid"] == 0 for row in states.values()), "DOCKER_PARADA")
    proxy = states["immich-ml-auth"]
    require(not verify_proxy or (proxy["ExitCode"] == 0 and not proxy["OOMKilled"]), "PROXY_PARADA")
    return states


def wait_for_microservices(cid, started_at, timeout=300):
    require(DIGEST.fullmatch(cid), "WORKER_CID")
    require(
        re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", started_at),
        "WORKER_INICIO",
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = docker("logs", "--since", started_at, cid, include_stderr=True)
        require(
            b"Metadata service init failed" not in logs
            and b"Unable to initialize reverse geocoding" not in logs,
            "WORKER_METADATOS",
        )
        if b"Immich Microservices is running [v3.3.6]" in logs:
            return
        time.sleep(2)
    raise ValueError("WORKER_TIMEOUT")


def gpu_bearer():
    raw, _ = private_file(CONTROL / "compose.env")
    variable = b"MEDIA_IMMICH_LAN_GPU_BEARER_FILE="
    (reference,) = [
        line.removeprefix(variable) for line in raw.splitlines() if line.startswith(variable)
    ]
    path = Path(reference.decode("utf-8").strip(" \t\"'"))
    require(path.is_absolute() and path.resolve(strict=True) == path, "GPU_ML_SECRETO_RUTA")
    raw, mode = private_file(path, owner=IMAGES["immich-ml-auth"][1])
    token = raw.removesuffix(b"\n").removesuffix(b"\r").decode("ascii")
    require(mode in (0o400, 0o600) and re.fullmatch(r"[!-~]{32,4096}", token), "GPU_ML_SECRETO")
    return token


def check_gpu_ready():
    token = gpu_bearer()
    conn = http.client.HTTPConnection(GPU, int(GPU_PORT), timeout=4, source_address=(HOST, 0))
    try:
        conn.request("GET", "/health", headers={"Authorization": "Bearer " + token})
        response = conn.getresponse()
        require(response.status == 200, "GPU_ML_NO_DISPONIBLE")
        body = response.read(65537)
        require(len(body) <= 65536, "GPU_ML_NO_DISPONIBLE")
        data = json_load(body)
        require(
            isinstance(data, dict)
            and data.get("ready") is True
            and data.get("cuda_available") is True
            and data.get("service") == "immich-ml"
            and data.get("status") == "ok",
            "GPU_ML_NO_DISPONIBLE",
        )
    except (OSError, ValueError, http.client.HTTPException):
        raise ValueError("GPU_ML_NO_DISPONIBLE") from None
    finally:
        conn.close()


def start(plan):
    network_host(plan)
    states = check_containers(plan)
    if not states["immich-server"]["Running"]:
        check_gpu_ready()
    try:
        if any(states[service]["Running"] for service in ("immich-server", "immich-ml-auth")):
            firewall("check")
        firewall("apply")
        check_containers(plan)
        compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "235",
            "--no-recreate",
            "--no-build",
            "--pull",
            "never",
        )
        states = check_containers(plan)
        require(
            all(row["Running"] and row["Health"]["Status"] == "healthy" for row in states.values()),
            "DOCKER_SALUD",
        )
        started_at = states["immich-server"]["StartedAt"]
        wait_for_microservices(plan["containers"]["immich-server"], started_at)
        worker = check_containers(plan)["immich-server"]
        require(worker["Running"] and worker["StartedAt"] == started_at, "WORKER_REINICIADO")
        return firewall("check")
    except (OSError, ValueError, subprocess.TimeoutExpired, KeyError, TypeError):
        states = check_containers(plan)
        if any(row["Running"] for row in states.values()):
            stop(plan, verify_proxy=False)
        raise


def execute(action):
    plan = load_plan()
    lock = os.open("/run/agency-immich-lan.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    with os.fdopen(lock, "rb+") as file:
        node = os.fstat(file.fileno())
        require(
            stat.S_ISREG(node.st_mode)
            and node.st_uid == node.st_gid == 0
            and node.st_nlink == 1
            and stat.S_IMODE(node.st_mode) == 0o600,
            "LOCK_MODO",
        )
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = {"action": action, "success": True, "project": PROJECT}
        if action == "start":
            receipt["filter_sha256"] = start(plan)
        elif action == "stop":
            states = stop(plan)
            receipt["proxy_exit"] = states["immich-ml-auth"]["ExitCode"]
        else:
            states = check_containers(plan)
            if action == "remove":
                require(
                    all(not row["Running"] and row["Pid"] == 0 for row in states.values()),
                    "FIREWALL_PROCESOS_ACTIVOS",
                )
            else:
                network_host(plan)
                if action == "apply" and any(
                    states[service]["Running"] for service in ("immich-server", "immich-ml-auth")
                ):
                    firewall("check")
            receipt["filter_sha256"] = firewall(action)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "start", "stop", "remove"))
    args = parser.parse_args()
    try:
        receipt = execute(args.action)
    except (
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
        KeyError,
        TypeError,
        StopIteration,
    ) as error:
        code = error.args[0] if type(error) is ValueError and error.args else "OPERACION_FALLIDA"
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "OPERACION_FALLIDA"
        print(json.dumps({"action": args.action, "success": False, "error": code}, sort_keys=True))
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
