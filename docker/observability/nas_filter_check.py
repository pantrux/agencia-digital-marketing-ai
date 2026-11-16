"""Verifica en lectura el aislamiento de Prometheus registrado en DSM."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import types
from pathlib import Path

SOURCE = Path(__file__).resolve().with_name("nas_filter_reconcile.py")
SOURCE_SHA = "691bf425f629318fe2aef960b86efdc6d37ef93273ab70638ec554fe411d30db"
PIN = re.compile(r"[a-f0-9]{64}")


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_file(path, modes):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and (info.st_uid, info.st_gid) == (0, 0)
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) in modes,
            "ARCHIVO_IDENTIDAD",
        )
        raw = stream.read(65537)
        after = os.fstat(stream.fileno())
    require(
        0 < len(raw) <= 65536
        and len(raw) == info.st_size
        and (info.st_mtime_ns, info.st_ctime_ns) == (after.st_mtime_ns, after.st_ctime_ns),
        "ARCHIVO_CAMBIO",
    )
    return raw


def source():
    raw = read_file(SOURCE, (0o400, 0o444, 0o600, 0o644))
    require(digest(raw) == SOURCE_SHA, "FUENTE_SHA")
    module = types.ModuleType("nas_filter_reconcile")
    exec(compile(raw, str(SOURCE), "exec"), module.__dict__)
    return module


def journal(repair, plan_pin):
    root = repair.JOURNAL
    for path in (root.parent, root):
        info = path.lstat()
        require(
            path.resolve(strict=True) == path
            and stat.S_ISDIR(info.st_mode)
            and (info.st_uid, info.st_gid) == (0, 0)
            and stat.S_IMODE(info.st_mode) == 0o700,
            "JOURNAL_IDENTIDAD",
        )
    require(not os.path.lexists(root / "rollback.json"), "JOURNAL_REVERTIDO")
    intent = json.loads(read_file(root / "intent.json", (0o600,)))
    result = json.loads(read_file(root / "result.json", (0o600,)))
    require(
        intent["plan_sha256"] == plan_pin and result["tables"] == intent["states"][-1],
        "JOURNAL_PLAN",
    )
    reference = result["tables"]
    require(
        set(reference) == {"4:filter", "6:filter", "4:nat"}
        and all(PIN.fullmatch(value) for value in reference.values()),
        "JOURNAL_TABLAS",
    )
    return intent["container"], reference


def references(table, target):
    return [
        chain
        for chain, row in table.items()
        for rule in row["rules"]
        if any(
            token in ("-j", "-g", "--jump", "--goto") and rule[index + 1] == target
            for index, token in enumerate(rule[:-1])
        )
    ]


def evaluate(repair, tables, reference):
    fingerprints = {}
    total = 0
    for family in (4, 6):
        key = f"{family}:filter"
        table = repair.chains(tables[key])
        proof = {}
        for hook in repair.BUILTINS:
            target = repair.owned_chain(hook)
            prior = [("-j", "PPPOE_FORWARD")] if family == 4 and hook == "FORWARD" else []
            prefix = [*prior, ("-j", target)]
            require(
                table[hook]["rules"][: len(prefix)] == prefix
                and references(table, target) == [hook],
                "HOOK_FILTRO",
            )
            expected = list(repair.own_rules(family, hook))
            require(table.get(target) == {"policy": "-", "rules": expected}, "CADENA_PROPIA")
            proof[hook] = {"chain": target, "prefix": prefix, "rules": expected}
            total += len(expected)
        previous = table.get("PPPOE_FORWARD", {}).get("rules", [])
        if family == 4:
            require(
                len(previous) == 1
                and len(previous[0]) == 6
                and previous[0][0] == "-d"
                and previous[0][2:] == ("-i", "tun0", "-j", "DROP")
                and ipaddress.ip_network(previous[0][1], strict=False).version == 4,
                "HOOK_PPPOE",
            )
            proof["pppoe_scope"] = previous[0]
        fingerprints[key] = digest(repair.encode(proof))
    nat = repair.chains(tables["4:nat"])
    nat_prefix = ("-j", repair.NAT_CHAIN)
    require(
        nat["PREROUTING"]["rules"][:1] == [nat_prefix]
        and nat.get(repair.NAT_CHAIN) == {"policy": "-", "rules": [repair.DNAT]}
        and references(nat, repair.NAT_CHAIN) == ["PREROUTING"]
        and sum(repair.DNAT == rule for chain in nat.values() for rule in chain["rules"]) == 1,
        "DNAT_PROPIO",
    )
    fingerprints["4:nat"] = digest(repair.encode([nat_prefix, repair.DNAT]))
    live = repair.hashes(tables)
    return {
        "rules_verified": total,
        "dnat_verified": True,
        "owned_sha256": fingerprints,
        "tables_sha256": live,
        "global_drift": sorted(key for key in reference if live[key] != reference[key]),
    }


def check(repair, plan_pin):
    expected, reference = journal(repair, plan_pin)
    require(repair.host() == expected, "CONTENEDOR_EPOCA")
    first = repair.snapshot()
    result = evaluate(repair, first, reference)
    second = evaluate(repair, repair.snapshot(), reference)
    require(result["owned_sha256"] == second["owned_sha256"], "HUELLA_CAMBIO")
    require(
        repair.host() == expected and journal(repair, plan_pin) == (expected, reference),
        "ESTADO_CAMBIO",
    )
    return {
        **second,
        "plan_sha256": plan_pin,
        "table_change_during_check": result["tables_sha256"] != second["tables_sha256"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-sha256", required=True)
    args = parser.parse_args()
    try:
        require(PIN.fullmatch(args.plan_sha256), "PLAN_PIN")
        result = {"success": True, **check(source(), args.plan_sha256)}
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        subprocess.SubprocessError,
    ) as error:
        code = str(error) if type(error) is ValueError else "COMPROBACION_FALLIDA"
        result = {
            "success": False,
            "error": code if re.fullmatch(r"[A-Z0-9_]+", code) else "COMPROBACION_FALLIDA",
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
