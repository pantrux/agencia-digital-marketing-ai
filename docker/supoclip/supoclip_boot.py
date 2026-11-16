"""Restaura sólo los CID acreditados del ensayo privado de SupoClip."""

import argparse
import hashlib
import itertools
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import time
from pathlib import Path

WORK = Path("/opt/media-platform/supoclip-boot-v1")
TRIAL = Path("/opt/media-platform/supoclip-trial-20261113-v1")
PROJECT = "agency-supoclip-trial-nov13"
BRIDGE = "sco24754668cc"
SPEACHES = PROJECT + "-speaches"
HOST = "192.168.0.85"
WEIGHTS = "/opt/media-platform/supoclip-model-20261113-v1/qwen2.5-1.5b-instruct-q4_k_m.gguf"
ACCEPTED = "21ec8c04123a85df99978ea9f1f74b9538608d1f21a2842ff009641eb1cfe22b"
ROLES = ("postgres", "valkey", "migrate", "backend", "worker", "model", "frontend", "proxy")
CID = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{2,30}")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
COMMENT = ("-m", "comment", "--comment", PROJECT)
OBS_FWD = (
    ("-i", "br-aobsoutv1", "-j", "AGENCY-OBS-OUT-V1"),
    ("-o", "br-aobsoutv1", "-j", "AGENCY-OBS-OUT-V1"),
)
OBS_INPUT = (("-i", "br-aobsoutv1", "-j", "DROP"),)
DOCKER_USER = ("-j", "DOCKER-USER")
MONEYPRINTER_BRIDGES = {
    "agencia-digital-marketing-ai:moneyprinter-lan:v1": "br-mpt-lan",
    "agencia-digital-marketing-ai:moneyprinter-functional:v1": "br-mpt-fn",
}
INPUT_RULES = (
    ("-i", BRIDGE, *COMMENT, "-j", "DROP"),
    (
        "-d",
        HOST + "/32",
        "!",
        "-i",
        "lo",
        "-p",
        "tcp",
        "-m",
        "tcp",
        "--dport",
        "3080",
        "-m",
        "conntrack",
        "--ctstate",
        "NEW",
        *COMMENT,
        "-j",
        "DROP",
    ),
)
USER_RULES = (
    (
        "-d",
        "192.168.0.6/32",
        "-i",
        BRIDGE,
        "-p",
        "tcp",
        "-m",
        "tcp",
        "--dport",
        "8000",
        "-m",
        "conntrack",
        "--ctstate",
        "NEW,ESTABLISHED",
        *COMMENT,
        "-j",
        "ACCEPT",
    ),
    ("-i", BRIDGE, "-m", "conntrack", "--ctstate", "INVALID,NEW,UNTRACKED", *COMMENT, "-j", "DROP"),
    (
        "-p",
        "tcp",
        "-m",
        "conntrack",
        "--ctstate",
        "NEW",
        "--ctorigdst",
        HOST,
        "--ctorigdstport",
        "3080",
        *COMMENT,
        "-j",
        "DROP",
    ),
)
INSPECT = (
    '{"id":{{json .Id}},"image":{{json .Image}},"labels":{{json .Config.Labels}},'
    '"state":{{json .State.Status}},"exit":{{json .State.ExitCode}},'
    '"finished":{{json .State.FinishedAt}},"host":{{json .HostConfig}},'
    '"networks":{{json .NetworkSettings.Networks}},"mounts":{{json .Mounts}},'
    '"health":{{with index .State "Health"}}{{json .Status}}{{else}}null{{end}}}'
)
HTTP = (
    "import json,sys,urllib.request; "
    "p=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
    "d=json.load(p.open(sys.argv[1],timeout=3)); "
    "assert d.get('status')==sys.argv[2]; "
    "assert len(sys.argv)==3 or d.get(sys.argv[3])=='connected'"
)
ERRORS = (OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt)


def need(value, error):
    if not value:
        raise ValueError(error)


def run(*args, data=None, check=True, timeout=45):
    return subprocess.run(
        args, env=ENV, input=data, capture_output=True, check=check, timeout=timeout
    )


def docker(*args, **kwargs):
    return run("/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args, **kwargs)


def secure_file(path, limit):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        node = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(node.st_mode)
            and node.st_uid == 0
            and node.st_nlink == 1
            and not node.st_mode & 0o022
            and 0 < node.st_size <= limit,
            "ARCHIVO_IDENTIDAD",
        )
        raw = stream.read(limit + 1)
        need(len(raw) == node.st_size, "ARCHIVO_TAMANO")
        return raw


def inputs():
    for directory in (WORK, TRIAL):
        st = directory.stat()
        need(
            directory.resolve() == directory and st.st_uid == 0 and not st.st_mode & 0o022,
            "DIRECTORIO_IDENTIDAD",
        )
    raw = secure_file(WORK / "accepted.json", 16384)
    need(hashlib.sha256(raw).hexdigest() == ACCEPTED, "RECIBO_SHA")
    accepted = json.loads(raw)["probe"]
    need(
        accepted["success"]
        and accepted["project"] == PROJECT
        and accepted["weights"] == WEIGHTS
        and set(accepted["containers"]) == set(ROLES),
        "RECIBO_CONTRATO",
    )
    plan = json.loads(secure_file(TRIAL / "plan.json", 32768))
    need(
        plan["project"] == PROJECT
        and plan["bridge"] == BRIDGE
        and plan["speaches"] == SPEACHES
        and plan["lan_source"] is None
        and plan["env"]["MEDIA_SUPOCLIP_TRIAL_ROOT"] == str(TRIAL),
        "PLAN_DISTINTO",
    )
    for value in (plan["app_db"], plan["env"]["MEDIA_SUPOCLIP_TRIAL_PG_ADMIN"]):
        need(IDENTIFIER.fullmatch(value), "IDENTIFICADOR_SQL")
    network = json.loads(secure_file(TRIAL / "speaches-network.json", 4096))["id"]
    need(CID.fullmatch(network), "RED_RECIBO")
    return accepted, plan, network


def snapshot():
    output = run("/usr/sbin/iptables-save", "-t", "filter").stdout.decode().splitlines()
    rows = {}
    for line in output:
        if line.startswith(":"):
            rows[line[1:].split()[0]] = []
        elif line.startswith("-A "):
            _, chain, *rule = shlex.split(line)
            rows[chain].append(tuple(rule))
    need({"INPUT", "FORWARD", "DOCKER-USER"} <= rows.keys(), "FIREWALL_CADENAS")
    return rows


def moneyprinter_prefix(rows):
    count = 0
    for rule in rows:
        owner = next((tag for tag in MONEYPRINTER_BRIDGES if tag in rule), None)
        if owner is None:
            break
        bridge = MONEYPRINTER_BRIDGES[owner]
        fields = tuple(itertools.pairwise(rule))
        by_bridge = any(
            (flag, bridge) in fields and ("!", flag) not in fields for flag in ("-i", "-o")
        )
        by_port = ("--dport", "8501") in fields and ("!", "--dport") not in fields
        need(by_bridge or by_port, "FIREWALL_MONEYPRINTER_REGLA")
        count += 1
    need(
        not any(set(rule) & MONEYPRINTER_BRIDGES.keys() for rule in rows[count:]),
        "FIREWALL_MONEYPRINTER_ORDEN",
    )
    return count


def normalized_rule(row):
    # xtables may print a single host in conntrack with or without its /32.
    return tuple(
        token.removesuffix("/32") if index > 0 and row[index - 1] == "--ctorigdst" else token
        for index, token in enumerate(row)
    )


def firewall_change(rows, *, remove=False):
    rules = {"INPUT": INPUT_RULES, "FORWARD": (), "DOCKER-USER": USER_RULES}
    owned = {chain: [row for row in entries if PROJECT in row] for chain, entries in rows.items()}
    for chain, entries in owned.items():
        allowed = (
            INPUT_RULES
            if chain == "INPUT"
            else USER_RULES
            if chain in ("FORWARD", "DOCKER-USER")
            else ()
        )
        need(
            len(entries) == len({normalized_rule(row) for row in entries})
            and all(normalized_rule(row) in allowed for row in entries),
            "FIREWALL_COLISION",
        )
    unowned = {
        chain: [row for row in entries if PROJECT not in row] for chain, entries in rows.items()
    }
    obs_present = any(row in unowned["FORWARD"] for row in OBS_FWD) or (
        OBS_INPUT[0] in unowned["INPUT"]
    )
    observed_forward = [*OBS_FWD, DOCKER_USER] if obs_present else [DOCKER_USER]
    need(unowned["FORWARD"][: len(observed_forward)] == observed_forward, "FIREWALL_ORDEN_FORWARD")
    if obs_present:
        need(unowned["INPUT"][:1] == list(OBS_INPUT), "FIREWALL_ORDEN_INPUT")
    input_offset = len(OBS_INPUT) if obs_present else 0
    offsets = {
        "INPUT": input_offset + moneyprinter_prefix(unowned["INPUT"][input_offset:]),
        "DOCKER-USER": moneyprinter_prefix(unowned["DOCKER-USER"]),
        "FORWARD": 0,
    }
    expected = {chain: list(entries) for chain, entries in unowned.items()}
    payload = ["*filter"]
    for chain in rules:
        for entry in owned[chain]:
            payload.append(" ".join(("-D", chain, *entry)))
    if not remove:
        for chain, desired in rules.items():
            offset = offsets[chain]
            expected[chain][offset:offset] = desired
            for entry in reversed(desired):
                payload.append(" ".join(("-I", chain, str(offset + 1), *entry)))
    return expected, ("\n".join([*payload, "COMMIT", ""])).encode()


def firewall(*, apply=False, remove=False):
    before = snapshot()
    expected, payload = firewall_change(before, remove=remove)
    if same_rules(before, expected):
        return True
    if not apply:
        return False
    cmd = ("/usr/sbin/iptables-restore", "--noflush", "--wait", "5")
    run(*cmd, "--test", data=payload)
    need(snapshot() == before, "FIREWALL_CAMBIO_CONCURRENTE")
    run(*cmd, data=payload)
    need(same_rules(snapshot(), expected), "FIREWALL_DELTA")
    return True


def same_rules(first, second):
    def normalized(rows):
        return {
            chain: [normalized_rule(row) if PROJECT in row else row for row in entries]
            for chain, entries in rows.items()
        }

    return normalized(first) == normalized(second)


def firewall_preflight():
    before = snapshot()
    expected, payload = firewall_change(before)
    installed = same_rules(before, expected)
    if not installed:
        run("/usr/sbin/iptables-restore", "--noflush", "--wait", "5", "--test", data=payload)
    return installed


def containers(accepted, *, policies=True):
    pinned = accepted["containers"]
    ids = (
        docker("ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={PROJECT}")
        .stdout.decode()
        .splitlines()
    )
    expected = {row["id"] for row in pinned.values()}
    need(
        len(ids) == len(ROLES) and set(ids) == expected and all(CID.fullmatch(cid) for cid in ids),
        "CID_CONJUNTO",
    )
    rows = {}
    for line in docker(
        "inspect", "--type", "container", "--format", INSPECT, *ids
    ).stdout.splitlines():
        row = json.loads(line)
        labels, host = row["labels"], row["host"]
        role = labels["com.docker.compose.service"]
        owner = "supoclip-local-model" if role == "model" else "supoclip-trial"
        need(
            role in ROLES
            and role not in rows
            and row["id"] == pinned[role]["id"]
            and row["image"] == pinned[role]["image"]
            and labels["com.docker.compose.project"] == PROJECT
            and labels["agency.owner"] == f"agencia-digital-marketing-ai:{owner}:v1",
            "CID_IDENTIDAD",
        )
        ports = {"3080/tcp": [{"HostIp": HOST, "HostPort": "3080"}]} if role == "proxy" else {}
        need(
            (host["PortBindings"] or {}) == ports
            and host["ReadonlyRootfs"]
            and not host["Privileged"]
            and host["CapDrop"] == ["ALL"],
            "CID_AISLAMIENTO",
        )
        policy = host["RestartPolicy"]["Name"]
        allowed = (
            {"no"} if policies or role not in {"worker", "model"} else {"no", "unless-stopped"}
        )
        need(policy in allowed, "CID_RESTART_POLICY")
        if role == "migrate":
            need(
                row["state"] == "exited"
                and row["exit"] == 0
                and row["finished"] == accepted["migrate_finished"],
                "MIGRADOR_DISTINTO",
            )
        else:
            need(row["state"] in {"running", "exited", "created"}, "CID_ESTADO")
        rows[role] = row
    need(set(rows) == set(ROLES), "CID_ROLES")
    worker, model = rows["worker"], rows["model"]
    need(
        model["host"]["NetworkMode"] == "container:" + worker["id"]
        and worker["host"]["Dns"] == ["127.0.0.1"],
        "CID_NAMESPACE",
    )
    bind = [mount for mount in model["mounts"] if mount["Type"] != "tmpfs"]
    need(
        len(bind) == 1
        and bind[0]["Type"] == "bind"
        and bind[0]["Source"] == WEIGHTS
        and bind[0]["Destination"] == "/run/supoclip-model/qwen.gguf"
        and not bind[0]["RW"],
        "PESOS_MONTAJE",
    )
    return rows


def network_ok(rows, pinned_network):
    networks = json.loads(
        docker(
            "network",
            "inspect",
            SPEACHES,
            *(PROJECT + "_" + role for role in ("app", "data", "ingress")),
        ).stdout
    )
    speaches, *application = networks
    need(
        speaches["Id"] == pinned_network
        and speaches["Driver"] == "bridge"
        and not speaches["EnableIPv6"]
        and speaches["Labels"].get("agency.trial") == PROJECT
        and speaches["Options"].get("com.docker.network.bridge.name") == BRIDGE
        and set(speaches["Containers"] or {}) <= {rows["worker"]["id"]},
        "RED_SPEACHES",
    )
    for row, role in zip(application, ("app", "data", "ingress"), strict=True):
        need(
            row["Name"] == PROJECT + "_" + role
            and row["Driver"] == "bridge"
            and not row["EnableIPv6"]
            and row["Internal"] == (role != "ingress")
            and row["Labels"].get("com.docker.compose.project") == PROJECT,
            "RED_COMPOSE",
        )
    for role, row in rows.items():
        need(role == "worker" or SPEACHES not in row["networks"], "RED_MIEMBROS")
    if rows["worker"]["state"] == "running":
        need(
            set(rows["worker"]["networks"]) == {SPEACHES, PROJECT + "_data"}
            and set(speaches["Containers"] or {}) == {rows["worker"]["id"]},
            "RED_WORKER",
        )


def state(cid):
    line = docker("inspect", "--type", "container", "--format", INSPECT, cid).stdout
    return json.loads(line)


def wait_for(callback, error, seconds=75):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callback():
            return
        time.sleep(2)
    raise ValueError(error)


def wait_healthy(cid, role):
    def healthy():
        current = state(cid)
        need(current["state"] == "running", f"SALUD_DETENIDO_{role.upper()}")
        return current["health"] == "healthy"

    wait_for(healthy, f"SALUD_TIEMPO_{role.upper()}")


def http(cid, url, status, *extra):
    return (
        docker(
            "exec",
            cid,
            "/usr/bin/python3.12",
            "-I",
            "-S",
            "-B",
            "-c",
            HTTP,
            url,
            status,
            *extra,
            check=False,
            timeout=8,
        ).returncode
        == 0
    )


def postgres(rows, plan, query):
    return docker(
        "exec",
        rows["postgres"]["id"],
        "psql",
        "-X",
        "-qAtw",
        "-h",
        "127.0.0.1",
        "-U",
        plan["env"]["MEDIA_SUPOCLIP_TRIAL_PG_ADMIN"],
        "-d",
        plan["app_db"],
        "--set=ON_ERROR_STOP=1",
        "-c",
        query,
        timeout=8,
    ).stdout.strip()


def local_proxy():
    return (
        run(
            "/usr/bin/curl",
            "--noproxy",
            "*",
            "--fail",
            "--silent",
            "--max-time",
            "5",
            f"http://{HOST}:3080/healthz",
            check=False,
            timeout=8,
        ).stdout
        == b"ok\n"
    )


def start(rows, plan, network, receipt):
    firewall(apply=True)
    network_ok(rows, network)
    ids = {role: row["id"] for role, row in rows.items()}
    running = {role for role, row in rows.items() if row["state"] == "running"}
    if "proxy" in running and running != set(ROLES) - {"migrate"}:
        docker("stop", "--time", "30", ids["proxy"])
        running.remove("proxy")

    def launch(role):
        if role not in running:
            docker("start", ids[role])

    launch("postgres")
    launch("valkey")
    wait_healthy(ids["postgres"], "postgres")
    wait_healthy(ids["valkey"], "valkey")
    need(
        postgres(
            rows,
            plan,
            "SELECT count(*) FROM _prisma_migrations WHERE finished_at IS NOT NULL "
            "AND rolled_back_at IS NULL",
        )
        == b"27",
        "PRISMA_MIGRACIONES",
    )
    need(
        postgres(rows, plan, "SELECT count(*) FROM schema_migrations") == b"26",
        "BACKEND_MIGRACIONES",
    )
    launch("backend")
    for endpoint, label in (("db", "database"), ("redis", "redis")):
        wait_for(
            lambda endpoint=endpoint, label=label: http(
                ids["backend"],
                "http://127.0.0.1:8000/health/" + endpoint,
                "healthy",
                label,
            ),
            "SALUD_BACKEND",
        )
    launch("worker")
    launch("model")
    wait_healthy(ids["model"], "model")
    ns_args = ("/usr/bin/readlink", "/proc/self/ns/net")
    worker_ns = docker("exec", ids["worker"], *ns_args).stdout.strip()
    model_ns = docker("exec", ids["model"], *ns_args).stdout.strip()
    backend_ns = docker("exec", ids["backend"], *ns_args).stdout.strip()
    need(
        re.fullmatch(rb"net:\[\d+\]", worker_ns) and worker_ns == model_ns != backend_ns,
        "NAMESPACE_ACTIVO",
    )
    need(http(ids["worker"], "http://127.0.0.1:18963/health", "ok"), "SALUD_MODELO_WORKER")
    worker_ip = state(ids["worker"])["networks"][PROJECT + "_data"]["IPAddress"]
    reject = (
        "import errno,socket,sys; s=socket.socket(); s.settimeout(3); "
        "assert s.connect_ex((sys.argv[1],18963))==errno.ECONNREFUSED"
    )
    docker(
        "exec",
        ids["backend"],
        "/usr/bin/python3.12",
        "-I",
        "-S",
        "-B",
        "-c",
        reject,
        worker_ip,
        timeout=8,
    )
    wait_for(
        lambda: http(ids["backend"], "http://127.0.0.1:8000/health/worker", "healthy", "worker"),
        "SALUD_COLA",
    )
    launch("frontend")
    wait_healthy(ids["frontend"], "frontend")
    need(firewall(), "FIREWALL_ANTES_PROXY")
    launch("proxy")
    wait_for(local_proxy, "SALUD_PROXY", seconds=20)
    current = containers(receipt)
    network_ok(current, network)
    need(firewall(), "FIREWALL_DESPUES_PROXY")


def stop(rows, plan, output, *, drain=True):
    running = {role for role, row in rows.items() if row["state"] == "running"}

    def halt(role):
        if role in running:
            docker("stop", "--time", "30", rows[role]["id"])
            need(state(rows[role]["id"])["state"] == "exited", f"PARADA_{role.upper()}")

    for role in ("proxy", "frontend", "backend"):
        halt(role)
    drained = False
    if drain and {"postgres", "worker"} <= running:
        try:
            wait_for(
                lambda: (
                    postgres(
                        rows,
                        plan,
                        "SELECT count(*) FROM tasks "
                        "WHERE status IN ('queued','processing','pending')",
                    )
                    == b"0"
                ),
                "COLA_DRENAJE",
                seconds=120,
            )
            drained = True
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
    for role in ("model", "worker") if drained else ("worker", "model"):
        halt(role)
    for role in ("valkey", "postgres"):
        halt(role)
    output["drained"] = drained
    output["drain_timed_out"] = drain and {"postgres", "worker"} <= running and not drained


def interrupted(*_args):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "preflight",
            "adopt",
            "start",
            "stop",
            "inspect",
            "remove-firewall",
        ),
    )
    action = parser.parse_args().action
    out = {"success": False, "action": action}
    try:
        import fcntl

        need(os.geteuid() == 0 and os.uname().nodename == "vm-media-01", "HOST")
        signal.signal(signal.SIGTERM, interrupted)
        os.umask(0o077)
        accepted, plan, network = inputs()
        fd = os.open(WORK / "systemd.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb+") as lock:
            node = os.fstat(lock.fileno())
            need(
                stat.S_ISREG(node.st_mode)
                and node.st_uid == 0
                and node.st_nlink == 1
                and stat.S_IMODE(node.st_mode) == 0o600,
                "LOCK_IDENTIDAD",
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rows = containers(accepted, policies=action not in {"preflight", "adopt"})
            if action == "preflight":
                network_ok(rows, network)
                out["firewall"] = firewall_preflight()
                out["restart_policies"] = {
                    role: row["host"]["RestartPolicy"]["Name"] for role, row in rows.items()
                }
            if action == "adopt":
                network_ok(rows, network)
                firewall(apply=True)
                for role in ("worker", "model"):
                    if rows[role]["host"]["RestartPolicy"]["Name"] != "no":
                        docker("update", "--restart=no", rows[role]["id"])
            elif action == "start":
                try:
                    start(rows, plan, network, accepted)
                except ERRORS:
                    try:
                        stop(containers(accepted), plan, out, drain=False)
                    except ERRORS:
                        out["cleanup_failed"] = True
                    raise
            elif action == "stop":
                stop(rows, plan, out)
            elif action == "remove-firewall":
                need(all(row["state"] != "running" for row in rows.values()), "CID_ACTIVOS")
                firewall(apply=True, remove=True)
            current = containers(accepted, policies=action != "preflight")
            out["containers"] = {role: row["state"] for role, row in current.items()}
            if action != "preflight":
                out["firewall"] = firewall()
            if action in {"adopt", "start", "inspect"}:
                network_ok(current, network)
                need(out["firewall"], "FIREWALL_FALTANTE")
                out["local_http"] = (
                    200 if current["proxy"]["state"] == "running" and local_proxy() else None
                )
            out["success"] = True
    except ERRORS as error:
        detail = str(error)
        out["error"] = (
            detail
            if type(error) is ValueError and re.fullmatch(r"[A-Z0-9_]+", detail)
            else type(error).__name__
        )
    print(json.dumps(out, sort_keys=True), flush=True)
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
