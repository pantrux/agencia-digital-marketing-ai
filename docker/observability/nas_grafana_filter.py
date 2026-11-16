"""Amplía únicamente la cadena IPv4 de Prometheus para el datasource de Grafana."""

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import types
from pathlib import Path

CHECKER = Path(__file__).resolve().with_name("nas_filter_check.py")
CHECKER_SHA = "7a6ff8a68cabddff23b82a69507061440073b60b1478e1d717327833c5d55253"
AM_SOURCE = CHECKER.with_name("nas_alertmanager_filter.py")
AM_SHA = "14cfd5865931e255657bcae78f5a5c01454300f39130a1a45e7a7051e0c8d774"
CID = "be83c1d7d445576affb23d31fd32d1806dcb2b434affcd7528a6f22b9b7c60c9"
NETWORK = "dae7c42b9e43a477c84567926b72af08928c98eed7e5a274a3584c2393c8aa84"
NETWORK_NAME = "monitoring-stack_monitoring_network"
GRAFANA, GRAFANA_BRIDGE = "192.168.48.3", "docker-dae7c42b"
PROMETHEUS, PROMETHEUS_BRIDGE = "172.31.254.2", "br-apromv1"
CHAIN = "APROM_V1_FORWARD"
FILTER = "4:filter"
INDEX = 2
PIN = re.compile(r"[0-9a-f]{64}")
JOURNAL_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
ERRORS = (
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    AttributeError,
    RecursionError,
    subprocess.SubprocessError,
    KeyboardInterrupt,
)


def require(condition, code):
    if not condition:
        raise ValueError(code)


def modules():
    with os.fdopen(os.open(CHECKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and (info.st_uid, info.st_gid) == (0, 0)
            and not info.st_mode & 0o022
            and info.st_nlink == 1,
            "GRAFANA_FUENTE",
        )
        raw = stream.read(65537)
    require(
        len(raw) == info.st_size and hashlib.sha256(raw).hexdigest() == CHECKER_SHA,
        "GRAFANA_FUENTE",
    )
    checker = types.ModuleType("nas_filter_check")
    checker.__file__ = str(CHECKER)
    exec(compile(raw, str(CHECKER), "exec"), checker.__dict__)
    return checker, checker.source()


def load_alertmanager(checker):
    raw = checker.read_file(AM_SOURCE, (0o400, 0o444, 0o600, 0o644))
    require(hashlib.sha256(raw).hexdigest() == AM_SHA, "GRAFANA_AM_FUENTE")
    module = types.ModuleType("nas_alertmanager_filter")
    module.__file__ = str(AM_SOURCE)
    exec(compile(raw, str(AM_SOURCE), "exec"), module.__dict__)
    return module


def alertmanager_context(checker, repair, baseline, journal_id, pin):
    require(JOURNAL_ID.fullmatch(journal_id) and PIN.fullmatch(pin), "GRAFANA_AM_PIN")
    module = load_alertmanager(checker)
    journal = repair.JOURNAL.parent / ("firewall-alertmanager-" + journal_id)
    private_directory(journal)
    args = types.SimpleNamespace(
        baseline_plan_sha256=baseline, journal_id=journal_id, plan_sha256=pin
    )
    plan = module.read_proposal(checker, repair, args, journal)
    return module, plan


def alertmanager_attest(checker, repair, alertmanager):
    module, plan = alertmanager
    journal = repair.JOURNAL.parent / ("firewall-alertmanager-" + plan["journal_id"])
    private_directory(journal)
    result = json.loads(checker.read_file(journal / "result.json", (0o600,)))
    current = json.loads(checker.read_file(journal / "intent.json", (0o600,)))
    require(
        current == plan
        and result == {"tables": plan["after"]}
        and not os.path.lexists(journal / "revert.json"),
        "GRAFANA_AM_JOURNAL",
    )
    return module.attest(checker, repair, plan, stopped=False)


def alertmanager_binding(repair, alertmanager):
    _, plan = alertmanager
    return {"journal_id": plan["journal_id"], "plan_sha256": repair.sha(repair.encode(plan))}


def grafana_host(repair):
    template = (
        '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Config.Image}},'
        '"running":{{json .State.Running}},"pid":{{json .State.Pid}},'
        '"started_at":{{json .State.StartedAt}},"ports":{{json .HostConfig.PortBindings}},'
        '"networks":{{json .NetworkSettings.Networks}}}'
    )
    docker = (repair.DOCKER, "--host", "unix:///var/run/docker.sock")
    row = json.loads(
        repair.command((*docker, "inspect", "--type", "container", "--format", template, CID))
    )
    require(
        row["id"] == CID
        and row["name"] == "/grafana"
        and row["image"] == "grafana/grafana:latest"
        and row["running"] is True
        and type(row["pid"]) is int
        and row["pid"] > 0
        and isinstance(row["started_at"], str)
        and row["started_at"]
        and not row["ports"]
        and set(row["networks"]) == {NETWORK_NAME},
        "GRAFANA_CONTENEDOR",
    )
    endpoint = row["networks"][NETWORK_NAME]
    require(endpoint["NetworkID"] == NETWORK and endpoint["IPAddress"] == GRAFANA, "GRAFANA_RED")
    for address, bridge in ((GRAFANA, GRAFANA_BRIDGE), (PROMETHEUS, PROMETHEUS_BRIDGE)):
        require(Path("/sys/class/net", bridge, "bridge").is_dir(), "GRAFANA_BRIDGE")
        route = (
            repair.command(("/sbin/ip", "-4", "route", "get", address))
            .decode()
            .splitlines()[0]
            .split()
        )
        require(
            route[0] == address
            and "dev" in route
            and route[route.index("dev") + 1] == bridge
            and "via" not in route,
            "GRAFANA_RUTA",
        )
    return {
        "cid": CID,
        "pid": row["pid"],
        "started_at": row["started_at"],
        "network": NETWORK,
        "ip": GRAFANA,
        "bridge": GRAFANA_BRIDGE,
    }


def rules(repair):
    conntrack = (
        "--ctproto",
        "6",
        "--ctorigsrc",
        GRAFANA,
        "--ctorigdst",
        PROMETHEUS,
        "--ctorigdstport",
        "9090",
    )
    return (
        (
            "-s",
            GRAFANA + "/32",
            "-d",
            PROMETHEUS + "/32",
            "-i",
            GRAFANA_BRIDGE,
            "-o",
            PROMETHEUS_BRIDGE,
            *repair.TCP,
            "--dport",
            "9090",
            "-m",
            "conntrack",
            "--ctstate",
            "NEW,ESTABLISHED",
            *conntrack,
            "--ctdir",
            "ORIGINAL",
            "-j",
            "ACCEPT",
        ),
        (
            "-s",
            PROMETHEUS + "/32",
            "-d",
            GRAFANA + "/32",
            "-i",
            PROMETHEUS_BRIDGE,
            "-o",
            GRAFANA_BRIDGE,
            *repair.TCP,
            "--sport",
            "9090",
            "-m",
            "conntrack",
            "--ctstate",
            "ESTABLISHED",
            *conntrack,
            "--ctdir",
            "REPLY",
            "-j",
            "ACCEPT",
        ),
    )


def insert_offset(repair, alertmanager):
    if alertmanager is None:
        return INDEX
    module, plan = alertmanager
    offset, rows = module.changes(repair, plan["smtp_ipv4"])[FILTER][CHAIN]
    require(offset == INDEX, "GRAFANA_AM_ORDEN")
    return offset + len(rows)


def verify_baseline(checker, repair, tables, reference, alertmanager):
    if alertmanager is None:
        proof = checker.evaluate(repair, tables, reference)
        require(not proof["global_drift"], "GRAFANA_BASE_DERIVA")
        v4 = repair.chains(tables[FILTER])
        require(
            v4["FORWARD"]["rules"] == [("-j", "PPPOE_FORWARD"), ("-j", CHAIN)]
            and v4[CHAIN]["rules"] == list(repair.own_rules(4, "FORWARD"))
            and tuple(v4[CHAIN]["rules"][:INDEX]) == repair.TRANSIT,
            "GRAFANA_ORDEN",
        )
    else:
        module, plan = alertmanager
        require(plan["reference"] == reference, "GRAFANA_AM_BASE")
        module.verified_extension(checker, repair, tables, plan)
    # El DNAT existente sólo coincide con el flujo de .85 por eth0; el flujo nuevo es directo.
    nat = repair.chains(tables["4:nat"])
    require(
        nat["PREROUTING"]["rules"] == [("-j", repair.NAT_CHAIN)]
        and nat[repair.NAT_CHAIN]["rules"] == [repair.DNAT],
        "GRAFANA_NAT_ORDEN",
    )


def expected_extension(checker, repair, tables, reference, alertmanager=None):
    verify_baseline(checker, repair, tables, reference, alertmanager)
    offset = insert_offset(repair, alertmanager)
    additions = tuple(("-A", CHAIN, *rule) for rule in rules(repair))
    rows = tables[FILTER]
    require(all(row not in rows for row in additions), "GRAFANA_REGLA_EXISTENTE")
    indices = [index for index, row in enumerate(rows) if row[:2] == ("-A", CHAIN)]
    insert = indices[offset]
    return {**tables, FILTER: rows[:insert] + additions + rows[insert:]}


def stripped_extension(repair, tables, offset):
    rows = tables[FILTER]
    additions = tuple(("-A", CHAIN, *rule) for rule in rules(repair))
    chain_rows = tuple(row for row in rows if row[:2] == ("-A", CHAIN))
    require(
        chain_rows[offset : offset + len(additions)] == additions
        and all(rows.count(row) == 1 for row in additions),
        "GRAFANA_REGLAS_PROPIAS",
    )
    return {**tables, FILTER: tuple(row for row in rows if row not in additions)}


def payload(repair, enable, *, offset=INDEX):
    specifications = rules(repair)
    if enable:
        lines = [
            f"-I {CHAIN} {position} " + " ".join(rule)
            for position, rule in enumerate(specifications, start=offset + 1)
        ]
    else:
        lines = [f"-D {CHAIN} " + " ".join(rule) for rule in reversed(specifications)]
    return ("*filter\n" + "\n".join(lines) + "\nCOMMIT\n").encode("ascii")


def restore(repair, enable, *, test, offset=INDEX):
    argv = [str(repair.MULTI), "iptables-restore", "--noflush", "--wait", "5"]
    if test:
        argv.append("--test")
    result = subprocess.run(
        argv,
        env=repair.ENV,
        input=payload(repair, enable, offset=offset),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    require(result.returncode == 0, "GRAFANA_RESTORE_TEST" if test else "GRAFANA_RESTORE")


def propose(checker, repair, baseline, journal_id, *, alertmanager=None):
    container, reference = checker.journal(repair, baseline)
    if alertmanager is None:
        proof = checker.check(repair, baseline)
        require(
            not proof["global_drift"] and not proof["table_change_during_check"],
            "GRAFANA_BASE_DERIVA",
        )
        am_before = None
    else:
        am_before = alertmanager_attest(checker, repair, alertmanager)
        require(am_before["running"], "GRAFANA_AM_DETENIDO")
    require(repair.host() == container, "GRAFANA_PROMETHEUS_EPOCA")
    grafana = grafana_host(repair)
    before = repair.snapshot()
    after = expected_extension(checker, repair, before, reference, alertmanager)
    proposal = {
        "baseline_plan_sha256": baseline,
        "journal_id": journal_id,
        "prometheus": container,
        "grafana": grafana,
        "before": repair.hashes(before),
        "after": repair.hashes(after),
        "chain": CHAIN,
        "insert_at": insert_offset(repair, alertmanager) + 1,
        "rules": rules(repair),
    }
    if alertmanager is not None:
        proposal["alertmanager"] = alertmanager_binding(repair, alertmanager)
        require(
            alertmanager_attest(checker, repair, alertmanager) == am_before, "GRAFANA_AM_CAMBIO"
        )
    require(
        repair.hashes(repair.snapshot()) == proposal["before"]
        and repair.host() == container
        and grafana_host(repair) == grafana
        and checker.journal(repair, baseline) == (container, reference),
        "GRAFANA_CAMBIO_CONCURRENTE",
    )
    return proposal


def private_directory(path):
    info = path.lstat()
    require(
        path.resolve(strict=True) == path
        and stat.S_ISDIR(info.st_mode)
        and (info.st_uid, info.st_gid) == (0, 0)
        and stat.S_IMODE(info.st_mode) == 0o700,
        "GRAFANA_JOURNAL_RAIZ",
    )


def write_record(repair, journal, name, value):
    with (journal / name).open("xb") as stream:
        stream.write(repair.encode(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(journal, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def restore_planned(repair, proposal, enable, *, test):
    offset = proposal.get("insert_at", INDEX + 1) - 1
    if offset == INDEX:
        restore(repair, enable, test=test)
    else:
        restore(repair, enable, test=test, offset=offset)


def change(repair, proposal, enable):
    expected = proposal["before" if enable else "after"]
    final = proposal["after" if enable else "before"]
    # El bloqueo del proyecto abarca ambas verificaciones. DSM exige que iptables-restore
    # adquiera su propio bloqueo global; por eso se verifica la tabla antes y después del COMMIT.
    require(repair.hashes(repair.snapshot()) == expected, "GRAFANA_CAMBIO_CONCURRENTE")
    restore_planned(repair, proposal, enable, test=True)
    require(repair.hashes(repair.snapshot()) == expected, "GRAFANA_CAMBIO_CONCURRENTE")
    restore_planned(repair, proposal, enable, test=False)
    require(repair.hashes(repair.snapshot()) == final, "GRAFANA_DERIVA_APLICACION")


def reverse(repair, proposal):
    current = repair.hashes(repair.snapshot())
    require(current in (proposal["before"], proposal["after"]), "GRAFANA_REVERSION_ESTADO")
    if current == proposal["after"]:
        change(repair, proposal, False)
    return proposal["before"]


def reverse_combined(checker, repair, proposal, alertmanager):
    module, am_plan = alertmanager
    alertmanager_attest(checker, repair, alertmanager)
    live = repair.snapshot()
    additions = tuple(("-A", CHAIN, *rule) for rule in rules(repair))
    present = sum(row in live[FILTER] for row in additions)
    require(present in (0, len(additions)), "GRAFANA_REVERSION_ESTADO")
    restored = stripped_extension(repair, live, proposal["insert_at"] - 1) if present else live
    module.verified_extension(checker, repair, restored, am_plan)
    before, after = repair.hashes(restored), repair.hashes(live)
    if present:
        change(repair, {**proposal, "before": before, "after": after}, False)
    return before


def read_proposal(checker, repair, journal, baseline, journal_id, pin, alertmanager=None):
    proposal = json.loads(checker.read_file(journal / "intent.json", (0o600,)))
    require(
        repair.sha(repair.encode(proposal)) == pin
        and proposal["baseline_plan_sha256"] == baseline
        and proposal["journal_id"] == journal_id,
        "GRAFANA_JOURNAL_PIN",
    )
    instance, reference = checker.journal(repair, baseline)
    require(
        instance == proposal["prometheus"]
        and proposal["insert_at"] == insert_offset(repair, alertmanager) + 1,
        "GRAFANA_JOURNAL_BASE",
    )
    if alertmanager is None:
        require(
            "alertmanager" not in proposal and reference == proposal["before"],
            "GRAFANA_JOURNAL_BASE",
        )
    else:
        _, am_plan = alertmanager
        require(
            proposal.get("alertmanager") == alertmanager_binding(repair, alertmanager)
            and reference == am_plan["reference"],
            "GRAFANA_AM_BASE",
        )
    return proposal


def check_combined(checker, repair, proposal, alertmanager):
    _, am_plan = alertmanager
    state = alertmanager_attest(checker, repair, alertmanager)
    identity = grafana_host(repair)
    tables = repair.snapshot()
    pure_am = stripped_extension(repair, tables, proposal["insert_at"] - 1)
    verify_baseline(checker, repair, pure_am, am_plan["reference"], alertmanager)
    live = repair.hashes(tables)
    require(
        identity == proposal["grafana"]
        and grafana_host(repair) == identity
        and alertmanager_attest(checker, repair, alertmanager) == state
        and repair.hashes(repair.snapshot()) == live,
        "GRAFANA_CAMBIO_CONCURRENTE",
    )
    return {
        "tables": live,
        "alertmanager_running": state["running"],
        "alertmanager_rules_verified": 13,
        "prometheus_rules_verified": 10,
        "dnat_verified": True,
        "global_drift": sorted(key for key in live if live[key] != proposal["after"][key]),
    }


def operation(checker, repair, action, baseline, journal_id, pin, out, *, alertmanager=None):
    kwargs = {} if alertmanager is None else {"alertmanager": alertmanager}

    def revert(proposal):
        if alertmanager is None:
            return reverse(repair, proposal)
        return reverse_combined(checker, repair, proposal, alertmanager)

    if action in ("plan", "test"):
        proposal = propose(checker, repair, baseline, journal_id, **kwargs)
        digest = repair.sha(repair.encode(proposal))
        if action == "test":
            require(digest == pin, "GRAFANA_PLAN_SHA")
            require(
                repair.hashes(repair.snapshot()) == proposal["before"], "GRAFANA_CAMBIO_CONCURRENTE"
            )
            restore_planned(repair, proposal, True, test=True)
            require(
                repair.hashes(repair.snapshot()) == proposal["before"], "GRAFANA_CAMBIO_CONCURRENTE"
            )
            if alertmanager is not None:
                require(
                    alertmanager_attest(checker, repair, alertmanager)["running"],
                    "GRAFANA_AM_DETENIDO",
                )
            out["test_verified"] = True
        out.update(plan_sha256=digest, plan=proposal)
        return

    root = repair.JOURNAL.parent
    private_directory(root)
    journal = root / ("firewall-grafana-" + journal_id)
    if action == "check":
        private_directory(journal)
        proposal = read_proposal(checker, repair, journal, baseline, journal_id, pin, **kwargs)
        applied = json.loads(checker.read_file(journal / "result.json", (0o600,)))
        require(
            applied == {"tables": proposal["after"]}
            and not os.path.lexists(journal / "revert.json"),
            "GRAFANA_JOURNAL_RESULTADO",
        )
        if alertmanager is None:
            require(
                repair.hashes(repair.snapshot()) == proposal["after"]
                and repair.host() == proposal["prometheus"]
                and grafana_host(repair) == proposal["grafana"]
                and repair.hashes(repair.snapshot()) == proposal["after"],
                "GRAFANA_DERIVA_APLICACION",
            )
            out["tables"] = proposal["after"]
        else:
            out.update(check_combined(checker, repair, proposal, alertmanager))
            require(not os.path.lexists(journal / "revert.json"), "GRAFANA_JOURNAL_RESULTADO")
        out.update(rules_verified=2, plan_sha256=pin)
        return

    with repair.lock(root / ".firewall-filter-reconcile.lock", os.O_CREAT):
        if action == "revert":
            private_directory(journal)
            proposal = read_proposal(checker, repair, journal, baseline, journal_id, pin, **kwargs)
            # La reversión sigue siendo válida si Grafana ya se detuvo o se recreó.
            tables = revert(proposal)
            if not os.path.lexists(journal / "revert.json"):
                write_record(repair, journal, "revert.json", {"tables": tables})
            out.update(reverted=True, tables=tables)
            return

        proposal = propose(checker, repair, baseline, journal_id, **kwargs)
        require(repair.sha(repair.encode(proposal)) == pin, "GRAFANA_PLAN_SHA")
        journal.mkdir(mode=0o700)
        write_record(repair, journal, "intent.json", proposal)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if alertmanager is not None:
                require(
                    alertmanager_attest(checker, repair, alertmanager)["running"],
                    "GRAFANA_AM_DETENIDO",
                )
            change(repair, proposal, True)
            require(
                repair.host() == proposal["prometheus"]
                and grafana_host(repair) == proposal["grafana"],
                "GRAFANA_CONTENEDOR_CAMBIO",
            )
            if alertmanager is not None:
                proof = check_combined(checker, repair, proposal, alertmanager)
                require(
                    proof["alertmanager_running"] and not proof["global_drift"], "GRAFANA_AM_CAMBIO"
                )
            write_record(repair, journal, "result.json", {"tables": proposal["after"]})
        except ERRORS:
            try:
                tables = revert(proposal)
                write_record(repair, journal, "revert.json", {"tables": tables})
                out["automatic_revert"] = True
            except ERRORS:
                out["automatic_revert"] = False
            raise
        out.update(rules_verified=2, plan_sha256=pin, tables=proposal["after"])


def interrupted(_signal, _frame):
    raise ValueError("GRAFANA_INTERRUMPIDA")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "test", "apply", "check", "revert"))
    parser.add_argument("--baseline-plan-sha256", required=True)
    parser.add_argument("--journal-id", required=True)
    parser.add_argument("--plan-sha256")
    parser.add_argument("--am-journal-id")
    parser.add_argument("--am-plan-sha256")
    args = parser.parse_args(argv)
    out = {"action": args.action, "success": False}
    try:
        require(os.geteuid() == os.getegid() == 0, "GRAFANA_ROOT")
        require(
            PIN.fullmatch(args.baseline_plan_sha256)
            and JOURNAL_ID.fullmatch(args.journal_id)
            and (args.action == "plan" or PIN.fullmatch(args.plan_sha256 or "")),
            "GRAFANA_PIN",
        )
        require((args.am_journal_id is None) == (args.am_plan_sha256 is None), "GRAFANA_AM_PIN")
        os.umask(0o077)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        checker, repair = modules()
        alertmanager = None
        if args.am_journal_id is not None:
            alertmanager = alertmanager_context(
                checker, repair, args.baseline_plan_sha256, args.am_journal_id, args.am_plan_sha256
            )
        operation(
            checker,
            repair,
            args.action,
            args.baseline_plan_sha256,
            args.journal_id,
            args.plan_sha256,
            out,
            alertmanager=alertmanager,
        )
        out["success"] = True
    except ERRORS as error:
        code = str(error) if type(error) is ValueError else ""
        out["error"] = code if re.fullmatch(r"[A-Z0-9_]+", code) else "GRAFANA_FILTRO_RECHAZADO"
    print(json.dumps(out, sort_keys=True, separators=(",", ":")))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
