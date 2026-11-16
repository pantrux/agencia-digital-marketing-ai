"""Gestiona únicamente las reglas de Alertmanager propio en el NAS."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import signal
import smtplib
import socket
import ssl
import stat
import subprocess
import types
from pathlib import Path

CHECKER = Path(__file__).resolve().with_name("nas_filter_check.py")
CHECKER_SHA = "7a6ff8a68cabddff23b82a69507061440073b60b1478e1d717327833c5d55253"
PIN = re.compile(r"[0-9a-f]{64}")
JOURNAL_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
NAME = "agency-alertmanager-nas-v1"
OWNER = "agencia-digital-marketing-ai:alertmanager-nas:v1"
IMAGE = "sha256:6adddcd22a4a4f683f234216875a16e639b407cfc2ca451c41d1fe0abdd07abc"
ADDRESS, GATEWAY, SUBNET = "172.31.254.10", "172.31.254.9", "172.31.254.8/29"
BRIDGE, LAN = "br-aamv1", "eth0"
SMTP_HOST, SMTP_PORT, AM_PORT = "smtp.gmail.com", "587", "9093"
SMTP_CA = "/etc/ssl/certs/ca-certificates.crt"
SMTP_TIMEOUT = 5
POSTROUTING = "DEFAULT_POSTROUTING"
TABLE_ORDER = ("6:filter", "4:nat", "4:filter")
COMMAND = [
    "--config.file=/etc/alertmanager/alertmanager.yml",
    "--web.config.file=/etc/alertmanager/web.yml",
    "--storage.path=/alertmanager",
    f"--web.listen-address={ADDRESS}:{AM_PORT}",
    "--cluster.listen-address=",
]
OPTIONS = {
    "com.docker.network.bridge.name": BRIDGE,
    "com.docker.network.bridge.enable_icc": "false",
    "com.docker.network.bridge.enable_ip_masquerade": "false",
}
ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    AttributeError,
    subprocess.SubprocessError,
    KeyboardInterrupt,
)


def require(condition, code):
    if not condition:
        raise ValueError(code)


def modules():
    with os.fdopen(os.open(CHECKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        raw = stream.read(65537)
    require(
        stat.S_ISREG(info.st_mode)
        and (info.st_uid, info.st_gid) == (0, 0)
        and not info.st_mode & 0o022
        and info.st_nlink == 1
        and len(raw) == info.st_size,
        "AM_CHECKER_IDENTIDAD",
    )
    checker = types.ModuleType("nas_filter_check")
    checker.__file__ = str(CHECKER)
    require(hashlib.sha256(raw).hexdigest() == CHECKER_SHA, "AM_CHECKER_SHA")
    exec(compile(raw, str(CHECKER), "exec"), checker.__dict__)
    return checker, checker.source()


def smtp_address(value):
    ip = ipaddress.IPv4Address(value)
    require(
        str(ip) == value and ip.is_global and not ip.is_multicast and not ip.is_reserved,
        "AM_SMTP_IP_PUBLICA",
    )
    return value


def smtp_preflight(address):
    class PinnedSMTP(smtplib.SMTP):
        def _get_socket(self, host, port, timeout):
            # El cliente conserva el hostname de Gmail para SNI y verificación TLS.
            require(host == SMTP_HOST and port == int(SMTP_PORT), "AM_SMTP_DESTINO")
            return socket.create_connection((address, port), timeout)

    context = ssl.create_default_context(cafile=SMTP_CA)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        smtp = PinnedSMTP(
            SMTP_HOST, int(SMTP_PORT), local_hostname="localhost", timeout=SMTP_TIMEOUT
        )
        try:
            require(smtp.ehlo()[0] == 250 and smtp.has_extn("starttls"), "AM_SMTP_STARTTLS")
            require(smtp.starttls(context=context)[0] == 220, "AM_SMTP_STARTTLS")
            require(smtp.ehlo()[0] == 250, "AM_SMTP_EHLO_TLS")
        finally:
            smtp.close()
    except ssl.SSLCertVerificationError:
        raise ValueError("AM_SMTP_CERTIFICADO") from None
    except (OSError, smtplib.SMTPException):
        raise ValueError("AM_SMTP_TRANSPORTE") from None


def flow(repair, source, destination, ingress, egress, port):
    ct = ("-m", "conntrack")
    original = (
        "--ctproto",
        "6",
        "--ctorigsrc",
        source,
        "--ctorigdst",
        destination,
        "--ctorigdstport",
        port,
    )
    end = ("-j", "ACCEPT")
    return (
        (
            "-s",
            source + "/32",
            "-d",
            destination + "/32",
            "-i",
            ingress,
            "-o",
            egress,
            *repair.TCP,
            "--dport",
            port,
            *ct,
            "--ctstate",
            "NEW,ESTABLISHED",
            *original,
            "--ctdir",
            "ORIGINAL",
            *end,
        ),
        (
            "-s",
            destination + "/32",
            "-d",
            source + "/32",
            "-i",
            egress,
            "-o",
            ingress,
            *repair.TCP,
            "--sport",
            port,
            *ct,
            "--ctstate",
            "ESTABLISHED",
            *original,
            "--ctdir",
            "REPLY",
            *end,
        ),
    )


def changes(repair, smtp):
    result = {}
    for family in (4, 6):
        flows = (
            (
                *flow(repair, repair.ADDRESS, ADDRESS, repair.BRIDGE, BRIDGE, AM_PORT),
                *flow(repair, ADDRESS, smtp, BRIDGE, LAN, SMTP_PORT),
            )
            if family == 4
            else ()
        )
        result[f"{family}:filter"] = {
            repair.owned_chain("INPUT"): (
                len(repair.own_rules(family, "INPUT")),
                (("-i", BRIDGE, "-j", "DROP"),),
            ),
            repair.owned_chain("OUTPUT"): (
                len(repair.own_rules(family, "OUTPUT")),
                (("-o", BRIDGE, "-j", "DROP"),),
            ),
            repair.owned_chain("FORWARD"): (
                len(repair.TRANSIT) if family == 4 else 0,
                (
                    *flows,
                    ("-i", BRIDGE, "-j", "DROP"),
                    ("-o", BRIDGE, "-j", "DROP"),
                ),
            ),
        }
    result["4:nat"] = {
        POSTROUTING: (
            0,
            (
                (
                    "-s",
                    ADDRESS + "/32",
                    "-d",
                    smtp + "/32",
                    "-o",
                    LAN,
                    *repair.TCP,
                    "--dport",
                    SMTP_PORT,
                    "-j",
                    "MASQUERADE",
                ),
            ),
        ),
    }
    return result


def amend(rows, groups, enable):
    changed = list(rows)
    for chain, (offset, rules) in groups.items():
        require(("CHAIN", chain, "-") in changed, "AM_CADENA_FISICA")
        indices = [i for i, row in enumerate(changed) if row[:2] == ("-A", chain)]
        additions = [("-A", chain, *rule) for rule in rules]
        if enable:
            require(
                len(indices) >= offset and all(row not in changed for row in additions),
                "AM_REGLA_EXISTENTE",
            )
            # Las huellas del reconciliador conservan el orden de cada cadena.
            index = (
                indices[offset]
                if offset < len(indices)
                else (indices[-1] + 1 if indices else len(changed))
            )
            changed[index:index] = additions
        else:
            require(
                len(indices) >= offset + len(additions)
                and all(changed.count(row) == 1 for row in additions)
                and [changed[i] for i in indices[offset : offset + len(additions)]] == additions,
                "AM_REGLA_PROPIA",
            )
            for row in additions:
                changed.remove(row)
    return tuple(changed)


def base_state(checker, repair, tables, reference):
    proof = checker.evaluate(repair, tables, reference)
    nat = repair.chains(tables["4:nat"])
    require(
        nat["POSTROUTING"]["rules"] == [("-j", POSTROUTING)]
        and checker.references(nat, POSTROUTING) == ["POSTROUTING"],
        "AM_POSTROUTING_TOPOLOGIA",
    )
    v4 = repair.chains(tables["4:filter"])
    require(
        v4["PPPOE_FORWARD"]["rules"] == [("-d", "192.168.0.0/24", "-i", "tun0", "-j", "DROP")],
        "AM_PPPOE_TOPOLOGIA",
    )
    return proof


def expected_extension(checker, repair, tables, reference, smtp):
    base_state(checker, repair, tables, reference)
    specs = changes(repair, smtp)
    return {key: amend(tables[key], specs[key], True) for key in TABLE_ORDER}


def payload(key, groups, enable):
    _, table = key.split(":")
    lines = []
    for chain, (offset, rules) in groups.items():
        if enable:
            lines.extend(
                f"-I {chain} {i} " + " ".join(rule) for i, rule in enumerate(rules, offset + 1)
            )
        else:
            lines.extend(f"-D {chain} " + " ".join(rule) for rule in reversed(rules))
    return ("*" + table + "\n" + "\n".join(lines) + "\nCOMMIT\n").encode("ascii")


def restore(repair, key, groups, enable, *, test):
    verb = "ip6tables-restore" if key.startswith("6:") else "iptables-restore"
    argv = [str(repair.MULTI), verb, "--noflush", "--wait", "5"]
    if test:
        argv.append("--test")
    result = subprocess.run(
        argv,
        env=repair.ENV,
        input=payload(key, groups, enable),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    require(result.returncode == 0, "AM_RESTORE_TEST" if test else "AM_RESTORE")


def docker_inspect(repair, kind, identifier):
    argv = (
        repair.DOCKER,
        "--host",
        "unix:///var/run/docker.sock",
        "inspect",
        "--type",
        kind,
        identifier,
    )
    [row] = json.loads(repair.command(argv))
    return row


def route(repair, address, interface, *, gateway=None):
    row = (
        repair.command(("/sbin/ip", "-4", "route", "get", address)).decode().splitlines()[0].split()
    )
    require(
        row[0] == address
        and "dev" in row
        and row[row.index("dev") + 1] == interface
        and ("via" in row) == (gateway is not None)
        and (gateway is None or row[row.index("via") + 1] == gateway),
        "AM_RUTA",
    )


def alertmanager_host(repair, cid, network_id, smtp, *, stopped):
    net = docker_inspect(repair, "network", network_id)
    require(
        net["Id"] == network_id
        and net["Name"] == NAME
        and net["Driver"] == "bridge"
        and net["Scope"] == "local"
        and not net["Internal"]
        and not net["EnableIPv6"]
        and net["Labels"].get("agency.owner") == OWNER
        and net["Options"] == OPTIONS
        and net["IPAM"]["Config"] == [{"Subnet": SUBNET, "Gateway": GATEWAY}],
        "AM_RED_IDENTIDAD",
    )
    row = docker_inspect(repair, "container", cid)
    host, config, state = row["HostConfig"], row["Config"], row["State"]
    running = state["Running"]
    require(
        row["Id"] == cid
        and row["Name"] == "/" + NAME
        and row["Image"] == IMAGE
        and config["Labels"].get("agency.owner") == OWNER
        and config["User"] == "61211:61211"
        and config["Cmd"] == COMMAND
        and config["Entrypoint"] == ["/bin/alertmanager"]
        and not config.get("ExposedPorts")
        and not host["PortBindings"]
        and not host["PublishAllPorts"]
        and not host["Privileged"]
        and host["ReadonlyRootfs"]
        and host["CapDrop"] == ["ALL"]
        and not host["CapAdd"]
        and "no-new-privileges:true" in host["SecurityOpt"]
        and host["RestartPolicy"]["Name"] in ("", "no")
        and host["Dns"] == ["127.0.0.1"]
        and host["ExtraHosts"] == [f"{SMTP_HOST}:{smtp}"],
        "AM_CONTENEDOR_IDENTIDAD",
    )
    require(
        type(running) is bool
        and type(state["Pid"]) is int
        and (
            (running and state["Pid"] > 0 and state["Status"] == "running")
            or (not running and state["Pid"] == 0 and state["Status"] in ("created", "exited"))
        )
        and (not stopped or not running),
        "AM_CONTENEDOR_ESTADO",
    )
    image = docker_inspect(repair, "image", IMAGE)
    require(
        image["Id"] == IMAGE
        and image["Os"] == "linux"
        and image["Architecture"] == "amd64"
        and image["Config"]["Labels"].get("cl.e-scale.agencia.owner") == OWNER,
        "AM_IMAGEN_IDENTIDAD",
    )
    networks = row["NetworkSettings"]["Networks"]
    require(
        host["NetworkMode"] in (NAME, network_id) and set(networks) == {NAME}, "AM_RED_CONTENEDOR"
    )
    endpoint = networks[NAME]
    members = net.get("Containers") or {}
    require(
        endpoint["NetworkID"] in ((network_id,) if running else ("", network_id))
        and endpoint["IPAMConfig"]["IPv4Address"] == ADDRESS
        and not endpoint["GlobalIPv6Address"]
        and endpoint["IPAddress"] in ((ADDRESS,) if running else ("", ADDRESS))
        and set(members) == ({cid} if running else set())
        and (not running or members[cid]["IPv4Address"] == ADDRESS + "/29"),
        "AM_ENDPOINT_IDENTIDAD",
    )
    require(Path("/sys/class/net", BRIDGE, "bridge").is_dir(), "AM_BRIDGE_IDENTIDAD")
    route(repair, ADDRESS, BRIDGE)
    route(repair, repair.ADDRESS, repair.BRIDGE)
    # La salida SMTP debe seguir por la interfaz LAN, con un gateway físico IPv4.
    wan = repair.command(("/sbin/ip", "-4", "route", "get", smtp)).decode().splitlines()[0].split()
    require(
        wan[0] == smtp
        and "dev" in wan
        and wan[wan.index("dev") + 1] == LAN
        and "via" in wan
        and "src" in wan
        and wan[wan.index("src") + 1] == repair.NAS,
        "AM_SMTP_RUTA",
    )
    return {
        "cid": cid,
        "image": IMAGE,
        "network": network_id,
        "running": running,
        "pid": state["Pid"],
        "started_at": state["StartedAt"],
    }


def private_dir(path):
    info = path.lstat()
    require(
        path.resolve(strict=True) == path
        and stat.S_ISDIR(info.st_mode)
        and (info.st_uid, info.st_gid) == (0, 0)
        and stat.S_IMODE(info.st_mode) == 0o700,
        "AM_JOURNAL_IDENTIDAD",
    )


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def record(repair, journal, name, data):
    with (journal / name).open("xb") as stream:
        stream.write(repair.encode(data) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync(journal)


def propose(checker, repair, args):
    expected, reference = checker.journal(repair, args.baseline_plan_sha256)
    require(repair.host() == expected, "AM_PROMETHEUS_EPOCA")
    smtp = smtp_address(args.smtp_ip)
    am = alertmanager_host(repair, args.am_cid, args.network_id, smtp, stopped=True)
    smtp_preflight(smtp)
    before = repair.snapshot()
    after = expected_extension(checker, repair, before, reference, smtp)
    proposal = {
        "baseline_plan_sha256": args.baseline_plan_sha256,
        "journal_id": args.journal_id,
        "prometheus": expected,
        "alertmanager": am,
        "smtp_ipv4": smtp,
        "before": repair.hashes(before),
        "after": repair.hashes(after),
        "reference": reference,
    }
    require(repair.hashes(repair.snapshot()) == proposal["before"], "AM_CAMBIO_CONCURRENTE")
    attest(checker, repair, proposal, stopped=True)
    return proposal


def attest(checker, repair, proposal, *, stopped):
    expected = proposal["alertmanager"]
    require(
        repair.host() == proposal["prometheus"]
        and checker.journal(repair, proposal["baseline_plan_sha256"])
        == (proposal["prometheus"], proposal["reference"]),
        "AM_PROMETHEUS_EPOCA",
    )
    am = alertmanager_host(
        repair,
        expected["cid"],
        expected["network"],
        proposal["smtp_ipv4"],
        stopped=stopped,
    )
    if stopped:
        require(am == expected, "AM_EPOCA_CAMBIO")
    return am


def expected_hashes(repair, tables, key, groups, enable):
    updated = dict(tables)
    updated[key] = amend(tables[key], groups, enable)
    return repair.hashes(updated)


def transition(repair, key, groups, enable, tables, *, commit=True):
    before = repair.hashes(tables)
    after = expected_hashes(repair, tables, key, groups, enable)
    require(repair.hashes(repair.snapshot()) == before, "AM_CAMBIO_CONCURRENTE")
    restore(repair, key, groups, enable, test=True)
    require(repair.hashes(repair.snapshot()) == before, "AM_CAMBIO_CONCURRENTE")
    if commit:
        restore(repair, key, groups, enable, test=False)
        require(repair.hashes(repair.snapshot()) == after, "AM_DERIVA_APLICACION")


def reverse(checker, repair, proposal):
    # Una interrupción puede dejar sólo algunas de las tres tablas aplicadas.
    specs = changes(repair, proposal["smtp_ipv4"])
    live = repair.snapshot()
    restored = dict(live)
    touched = []
    for key in TABLE_ORDER:
        rows = live[key]
        additions = [
            ("-A", chain, *rule) for chain, (_, rules) in specs[key].items() for rule in rules
        ]
        present = sum(row in rows for row in additions)
        require(present in (0, len(additions)), "AM_REVERSION_ESTADO")
        if present:
            restored[key] = amend(rows, specs[key], False)
            touched.append(key)
    base_state(checker, repair, restored, proposal["reference"])
    for key in reversed(touched):
        transition(repair, key, specs[key], False, live)
        live = {**live, key: restored[key]}
    hashes = repair.hashes(repair.snapshot())
    require(hashes == repair.hashes(restored), "AM_REVERSION_SHA")
    return hashes


def read_proposal(checker, repair, args, journal):
    proposal = json.loads(checker.read_file(journal / "intent.json", (0o600,)))
    require(
        repair.sha(repair.encode(proposal)) == args.plan_sha256
        and proposal["baseline_plan_sha256"] == args.baseline_plan_sha256
        and proposal["journal_id"] == args.journal_id,
        "AM_JOURNAL_PIN",
    )
    instance, reference = checker.journal(repair, args.baseline_plan_sha256)
    require(
        instance == proposal["prometheus"] and reference == proposal["reference"], "AM_JOURNAL_BASE"
    )
    smtp_address(proposal["smtp_ipv4"])
    return proposal


def verified_extension(checker, repair, tables, proposal):
    specs = changes(repair, proposal["smtp_ipv4"])
    original = {key: amend(tables[key], specs[key], False) for key in TABLE_ORDER}
    base_state(checker, repair, original, proposal["reference"])
    return repair.hashes(tables)


def operation(checker, repair, args, out):
    root = repair.JOURNAL.parent
    private_dir(root)
    journal = root / ("firewall-alertmanager-" + args.journal_id)
    if args.action in ("plan", "test"):
        require(not os.path.lexists(journal), "AM_JOURNAL_EXISTE")
        proposal = propose(checker, repair, args)
        digest = repair.sha(repair.encode(proposal))
        specs = changes(repair, proposal["smtp_ipv4"])
        if args.action == "test":
            require(digest == args.plan_sha256, "AM_PLAN_SHA")
            before = repair.snapshot()
            require(repair.hashes(before) == proposal["before"], "AM_CAMBIO_CONCURRENTE")
            for key in TABLE_ORDER:
                transition(repair, key, specs[key], True, before, commit=False)
            attest(checker, repair, proposal, stopped=True)
            out["test_verified"] = True
        out.update(plan_sha256=digest, plan=proposal)
        return
    if args.action == "check":
        private_dir(journal)
        proposal = read_proposal(checker, repair, args, journal)
        result = json.loads(checker.read_file(journal / "result.json", (0o600,)))
        require(
            result == {"tables": proposal["after"]}
            and not os.path.lexists(journal / "revert.json"),
            "AM_JOURNAL_RESULTADO",
        )
        am = attest(checker, repair, proposal, stopped=False)
        first = verified_extension(checker, repair, repair.snapshot(), proposal)
        require(
            verified_extension(checker, repair, repair.snapshot(), proposal) == first
            and attest(checker, repair, proposal, stopped=False) == am
            and not os.path.lexists(journal / "revert.json"),
            "AM_CAMBIO_CONCURRENTE",
        )
        out.update(
            tables=first,
            rules_verified=13,
            alertmanager_running=am["running"],
            global_drift=sorted(key for key in first if first[key] != proposal["after"][key]),
        )
        return
    with repair.lock(root / ".firewall-filter-reconcile.lock", os.O_CREAT):
        if args.action == "revert":
            private_dir(journal)
            proposal = read_proposal(checker, repair, args, journal)
            require(repair.host() == proposal["prometheus"], "AM_PROMETHEUS_EPOCA")
            alertmanager_host(
                repair,
                proposal["alertmanager"]["cid"],
                proposal["alertmanager"]["network"],
                proposal["smtp_ipv4"],
                stopped=True,
            )
            tables = reverse(checker, repair, proposal)
            if not os.path.lexists(journal / "revert.json"):
                record(repair, journal, "revert.json", {"tables": tables})
            out.update(reverted=True, tables=tables)
            return
        proposal = propose(checker, repair, args)
        require(repair.sha(repair.encode(proposal)) == args.plan_sha256, "AM_PLAN_SHA")
        specs = changes(repair, proposal["smtp_ipv4"])
        journal.mkdir(mode=0o700)
        record(repair, journal, "intent.json", proposal)
        sync(root)
        try:
            current = repair.snapshot()
            require(repair.hashes(current) == proposal["before"], "AM_CAMBIO_CONCURRENTE")
            for key in TABLE_ORDER:
                attest(checker, repair, proposal, stopped=True)
                transition(repair, key, specs[key], True, current)
                current = {**current, key: amend(current[key], specs[key], True)}
            attest(checker, repair, proposal, stopped=True)
            require(
                verified_extension(checker, repair, repair.snapshot(), proposal)
                == proposal["after"],
                "AM_DERIVA_APLICACION",
            )
            record(repair, journal, "result.json", {"tables": proposal["after"]})
        except ERRORS:
            try:
                tables = reverse(checker, repair, proposal)
                record(repair, journal, "revert.json", {"tables": tables})
                out["automatic_revert"] = True
            except ERRORS:
                out["automatic_revert"] = False
            raise
        out.update(rules_verified=13, tables=proposal["after"])


def interrupted(_signum, _frame):
    raise ValueError("AM_INTERRUMPIDO")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "test", "apply", "check", "revert"))
    parser.add_argument("--baseline-plan-sha256", required=True)
    parser.add_argument("--journal-id", required=True)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--smtp-ip")
    parser.add_argument("--am-cid")
    parser.add_argument("--network-id")
    args = parser.parse_args(argv)
    out = {"action": args.action, "success": False}
    try:
        require(os.geteuid() == os.getegid() == 0, "AM_ROOT")
        require(
            PIN.fullmatch(args.baseline_plan_sha256)
            and JOURNAL_ID.fullmatch(args.journal_id)
            and (args.action == "plan" or PIN.fullmatch(args.plan_sha256 or ""))
            and (
                (
                    args.action in ("check", "revert")
                    and not any((args.smtp_ip, args.am_cid, args.network_id))
                )
                or (
                    args.action in ("plan", "test", "apply")
                    and args.smtp_ip
                    and all(PIN.fullmatch(value or "") for value in (args.am_cid, args.network_id))
                )
            ),
            "AM_ARGUMENTOS",
        )
        os.umask(0o077)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        checker, repair = modules()
        operation(checker, repair, args, out)
        out["success"] = True
    except ERRORS as error:
        code = str(error) if type(error) is ValueError else ""
        out["error"] = code if re.fullmatch(r"[A-Z0-9_]+", code) else "AM_FILTRO_RECHAZADO"
    print(json.dumps(out, sort_keys=True, separators=(",", ":")))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
