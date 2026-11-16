"""Recupera reglas de observabilidad perdidas tras una recarga de DSM."""

import argparse
import ctypes
import hashlib
import ipaddress
import json
import os
import re
import struct
import subprocess
import types
from pathlib import Path

ROOT = Path("/volume2/agency-prometheus-private-v1/filter-operator-v1")
PROFILE = ROOT / "firewall-recovery-profile.json"
GRAFANA = ROOT / "nas_grafana_filter.py"
GRAFANA_SHA = "d3bc0fa21227bd0610faa24ca07a887818a6dec8bc2697f4496fb9f1066d7432"


def require(condition, code):
    if not condition:
        raise ValueError(code)


def missing_rules(live, desired):
    missing = []
    index = 0
    for position, rule in enumerate(desired, 1):
        if index < len(live) and live[index] == rule:
            index += 1
        else:
            missing.append((position, rule))
    require(index == len(live), "REGLA_PROPIA_INESPERADA")
    return missing


def startup_target(repair, am, smtp, observed, approved):
    """Conserva el estado de Docker y recupera únicamente las reglas aprobadas."""
    rebased = {key: repair.chains(rows) for key, rows in observed.items()}
    for key in ("4:filter", "6:filter"):
        state = rebased[key]
        original = repair.chains(approved[key])
        for hook in repair.BUILTINS:
            name = repair.owned_chain(hook)
            for chain, node in state.items():
                if chain == hook:
                    continue
                require(
                    all(not rule or rule[-1] != name for rule in node["rules"]),
                    "REFERENCIA_AJENA",
                )
            state[hook] = original[hook]
            state[name] = original[name]
    state = rebased["4:nat"]
    original = repair.chains(approved["4:nat"])
    prerouting = state["PREROUTING"]
    intended = original["PREROUTING"]
    require(
        intended["rules"] == [("-j", repair.NAT_CHAIN)]
        and prerouting["policy"] == intended["policy"]
        and prerouting["rules"] in ([], intended["rules"]),
        "PREROUTING_INESPERADO",
    )
    for name, node in state.items():
        if name != "PREROUTING":
            require(
                all(not rule or rule[-1] != repair.NAT_CHAIN for rule in node["rules"]),
                "REFERENCIA_AJENA",
            )
    state["PREROUTING"] = intended
    state[repair.NAT_CHAIN] = original[repair.NAT_CHAIN]
    masq = am.changes(repair, smtp)["4:nat"][am.POSTROUTING][1]
    require(tuple(original[am.POSTROUTING]["rules"][: len(masq)]) == masq, "PERFIL_POSTROUTING")
    postrouting = state[am.POSTROUTING]
    retained = [rule for rule in postrouting["rules"] if rule not in masq]
    postrouting["rules"] = [*masq, *retained]
    return {
        key: tuple(("CHAIN", name, node["policy"]) for name, node in table.items())
        + tuple(("-A", name, *rule) for name, node in table.items() for rule in node["rules"])
        for key, table in rebased.items()
    }


def recovery_plan(repair, am, smtp, observed, expected, *, startup=False):
    keys = ("4:filter", "6:filter", "4:nat")
    require(set(observed) == set(expected) == set(keys), "TABLAS_INCOMPLETAS")
    live = {key: repair.chains(observed[key]) for key in keys}
    desired = {key: repair.chains(expected[key]) for key in keys}
    extended_nat = am.changes(repair, smtp)["4:nat"][am.POSTROUTING][1]
    nat_rules = {repair.NAT_CHAIN: (repair.DNAT,), am.POSTROUTING: extended_nat}
    steps = []

    def state():
        return {key: repair.sha(repair.encode(table)) for key, table in live.items()}

    for key in keys:
        family, table = key.split(":")
        before = state()
        current, target = live[key], desired[key]
        own = {repair.owned_chain(hook) for hook in repair.BUILTINS} if table == "filter" else set()
        hooks = set(repair.BUILTINS) if table == "filter" else set()
        selected = set(nat_rules) if table == "nat" else set()
        excluded = own | hooks | selected
        nat_hook_missing = (
            startup and table == "nat" and current["PREROUTING"] != target["PREROUTING"]
        )
        if nat_hook_missing:
            excluded.add("PREROUTING")
            require(
                current["PREROUTING"]["rules"] == []
                and target["PREROUTING"]["rules"] == [("-j", repair.NAT_CHAIN)],
                "PREROUTING_INESPERADO",
            )
        require(
            {chain: content for chain, content in current.items() if chain not in excluded}
            == {chain: content for chain, content in target.items() if chain not in excluded},
            "TOPOLOGIA_DSM",
        )

        missing_hooks = []
        for hook in repair.BUILTINS if table == "filter" else ():
            jump = ("-j", repair.owned_chain(hook))
            wanted = target[hook]
            require(wanted["rules"][-1:] == [jump], "PERFIL_SALTO")
            if current[hook] != wanted:
                require(
                    current[hook] == {"policy": wanted["policy"], "rules": wanted["rules"][:-1]},
                    "TOPOLOGIA_DSM",
                )
                missing_hooks.append(hook)
        require(
            len(missing_hooks) in (0, len(repair.BUILTINS)),
            "SALTOS_PARCIALES",
        )

        lines = []
        for chain in sorted(own | selected):
            wanted = target[chain]
            if chain not in current:
                require(chain in own or (startup and chain == repair.NAT_CHAIN), "TOPOLOGIA_DSM")
                current[chain] = {"policy": "-", "rules": []}
                lines.append("-N " + chain)
            present = current[chain]
            require(present["policy"] == wanted["policy"] == "-", "TOPOLOGIA_DSM")
            additions = missing_rules(present["rules"], wanted["rules"])
            for position, rule in additions:
                require(chain in own or rule in nat_rules[chain], "REGLA_AJENA_AUSENTE")
                require(
                    all(re.fullmatch(r"[a-zA-Z0-9_.,:/+=-]+", token) for token in rule),
                    "REGLA_FORMATO",
                )
                lines.append(f"-I {chain} {position} " + " ".join(rule))
            current[chain] = wanted

        if lines:
            steps.append(
                {
                    "action": "restore",
                    "table": key,
                    "before": before,
                    "after": state(),
                    "payload": "\n".join(("*" + table, *lines, "COMMIT", "")),
                }
            )
        if missing_hooks:
            before = state()
            for hook in missing_hooks:
                current[hook] = target[hook]
            steps.append(
                {
                    "action": "hooks",
                    "table": key,
                    "before": before,
                    "after": state(),
                    "family": int(family),
                }
            )
        if nat_hook_missing:
            before = state()
            current["PREROUTING"] = target["PREROUTING"]
            steps.append({"action": "nat_hook", "table": key, "before": before, "after": state()})
        require(current == target, "PLAN_INCOMPLETO")
    return steps


def restore(repair, step, *, test):
    verb = "ip6tables-restore" if step["table"].startswith("6:") else "iptables-restore"
    command = [str(repair.MULTI), verb, "--noflush", "--wait", "5"]
    if test:
        command.append("--test")
    result = subprocess.run(
        command,
        env=repair.ENV,
        input=step["payload"].encode("ascii"),
        capture_output=True,
        timeout=20,
        check=False,
    )
    require(result.returncode == 0, "RESTORE_ENSAYO" if test else "RESTORE_APLICACION")


def install_nat_hook(repair, before):
    # DSM reescribe PREROUTING al aplicar iptables-restore. Usa el salto físico.
    with repair.lock(Path("/run/xtables.lock")):
        require(repair.hashes(repair.snapshot()) == before, "CAMBIO_CONCURRENTE")
        lib = repair.library(4)
        handle = lib.init(b"nat")
        require(handle, "LIB_NAT_INIT")
        try:
            target = b"DEFAULT_PREROUTING"
            require(lib.is_chain(target, handle), "LIB_NAT_CADENA")
            require(not lib.first_rule(b"PREROUTING", handle), "LIB_NAT_SALTO_PREVIO")
            ip_size, size = repair.LAYOUTS[4]
            raw = bytearray(size + 40)
            struct.pack_into("=HH", raw, ip_size + 4, size, len(raw))
            struct.pack_into("=H", raw, size, 40)
            raw[size + 2 : size + 2 + len(target)] = target
            entry = ctypes.create_string_buffer(bytes(raw), len(raw))
            require(lib.insert_entry(b"PREROUTING", entry, 0, handle), "LIB_NAT_INSERCION")
            inserted = lib.first_rule(b"PREROUTING", handle)
            require(inserted and lib.get_target(inserted, handle) == target, "LIB_NAT_NO_COINCIDE")
            require(lib.commit(handle), "LIB_NAT_COMMIT")
        finally:
            lib.free(handle)


def apply(repair, steps, attest):
    for step in steps:
        require(repair.hashes(repair.snapshot()) == step["before"], "CAMBIO_CONCURRENTE")
        attest()
        if step["action"] == "hooks":
            family = step["family"]
            repair.hooks(family, True, step["before"], repair.library(family))
        elif step["action"] == "nat_hook":
            install_nat_hook(repair, step["before"])
        else:
            restore(repair, step, test=True)
            require(repair.hashes(repair.snapshot()) == step["before"], "CAMBIO_CONCURRENTE")
            restore(repair, step, test=False)
        require(repair.hashes(repair.snapshot()) == step["after"], "DERIVA_APLICACION")


def load_runtime(profile_sha):
    source = GRAFANA.read_bytes()
    require(hashlib.sha256(source).hexdigest() == GRAFANA_SHA, "FUENTE_SHA")
    grafana = types.ModuleType("nas_grafana_filter")
    grafana.__file__ = str(GRAFANA)
    exec(compile(source, str(GRAFANA), "exec"), grafana.__dict__)
    checker, repair = grafana.modules()
    raw = checker.read_file(PROFILE, (0o400,))
    require(hashlib.sha256(raw).hexdigest() == profile_sha, "PERFIL_SHA")
    cfg = json.loads(raw)
    baseline, am_id, am_pin, gf_id, gf_pin = (
        cfg[key] for key in ("baseline", "am_journal", "am_sha", "grafana_journal", "grafana_sha")
    )
    require(grafana.JOURNAL_ID.fullmatch(gf_id) and grafana.PIN.fullmatch(gf_pin), "GRAFANA_PIN")
    am, am_plan = grafana.alertmanager_context(checker, repair, baseline, am_id, am_pin)
    root = repair.JOURNAL.parent
    journal = root / ("firewall-grafana-" + gf_id)
    grafana.private_directory(journal)
    approved = grafana.read_proposal(
        checker, repair, journal, baseline, gf_id, gf_pin, (am, am_plan)
    )
    result = json.loads(checker.read_file(journal / "result.json", (0o600,)))
    require(
        result == {"tables": approved["after"]} and not os.path.lexists(journal / "revert.json"),
        "GRAFANA_DIARIO",
    )
    am_journal = root / ("firewall-alertmanager-" + am_id)
    result = json.loads(checker.read_file(am_journal / "result.json", (0o600,)))
    require(
        result == {"tables": am_plan["after"]} and not os.path.lexists(am_journal / "revert.json"),
        "AM_DIARIO",
    )
    target = {key: tuple(map(tuple, rows)) for key, rows in cfg["tables"].items()}
    require(repair.hashes(target) == approved["after"], "PERFIL_TABLAS")
    startup_ipv4 = cfg.get("startup_grafana_ipv4")
    if startup_ipv4 is not None:
        address = ipaddress.IPv4Address(startup_ipv4)
        network = ipaddress.IPv4Network(f"{grafana.GRAFANA}/20", strict=False)
        require(
            address in network
            and network.network_address + 1 < address < network.broadcast_address,
            "GRAFANA_IP_ARRANQUE",
        )
        old = [("-A", grafana.CHAIN, *rule) for rule in grafana.rules(repair)]
        grafana.GRAFANA = str(address)
        new = [("-A", grafana.CHAIN, *rule) for rule in grafana.rules(repair)]
        require(len(old) == len(new) == 2, "GRAFANA_REGLAS_DE_ARRANQUE")
        replacements = {old[0]: new[0], old[1]: new[1]}
        rows = target[grafana.FILTER]
        require(all(rows.count(rule) == 1 for rule in old), "GRAFANA_REGLAS_DE_ARRANQUE")
        target[grafana.FILTER] = tuple(replacements.get(row, row) for row in rows)
    smtp = am_plan["smtp_ipv4"]
    live_am = am_plan["alertmanager"]

    def inspect():
        return (
            repair.host(),
            am.alertmanager_host(repair, live_am["cid"], live_am["network"], smtp, stopped=False),
            grafana.grafana_host(repair),
        )

    return repair, am, smtp, target, inspect, startup_ipv4 is not None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "recover"))
    parser.add_argument("--profile-sha256", required=True)
    args = parser.parse_args()
    try:
        repair, am, smtp, target, inspect, startup = load_runtime(args.profile_sha256)
        with repair.lock(repair.JOURNAL.parent / ".firewall-filter-reconcile.lock", os.O_CREAT):
            identity = inspect()
            observed = repair.snapshot()
            if startup:
                target = startup_target(repair, am, smtp, observed, target)
            steps = recovery_plan(repair, am, smtp, observed, target, startup=startup)

            def attest():
                require(inspect() == identity, "CONTENEDOR_CAMBIO")

            if args.action == "recover":
                apply(repair, steps, attest)
                require(
                    repair.hashes(repair.snapshot()) == repair.hashes(target), "DERIVA_APLICACION"
                )
            attest()
        print(json.dumps({"success": args.action == "recover" or not steps, "steps": len(steps)}))
        return int(args.action == "check" and bool(steps))
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        code = str(error)
        print(
            json.dumps(
                {
                    "success": False,
                    "error": code if re.fullmatch("[A-Z_]+", code) else type(error).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
