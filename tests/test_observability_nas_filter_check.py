import importlib.util
from pathlib import Path

import pytest


def load(name):
    source = Path(__file__).parents[1] / "docker" / "observability" / f"nas_filter_{name}.py"
    spec = importlib.util.spec_from_file_location(f"nas_filter_{name}", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load("check")
repair = load("reconcile")


def baseline():
    builtin = tuple(("CHAIN", chain, "ACCEPT") for chain in repair.BUILTINS)
    v4 = (
        *builtin,
        ("CHAIN", "PPPOE_FORWARD", "-"),
        ("CHAIN", "UNRELATED", "-"),
        ("-A", "FORWARD", "-j", "PPPOE_FORWARD"),
        ("-A", "PPPOE_FORWARD", "-d", "192.168.0.0/24", "-i", "tun0", "-j", "DROP"),
    )
    v6 = builtin
    nat = (
        ("CHAIN", "PREROUTING", "ACCEPT"),
        ("CHAIN", repair.NAT_CHAIN, "-"),
        ("-A", "PREROUTING", "-j", repair.NAT_CHAIN),
        ("-A", repair.NAT_CHAIN, *repair.DNAT),
    )
    result = {"4:filter": v4, "6:filter": v6, "4:nat": nat}
    for family in (4, 6):
        rows = result[f"{family}:filter"]
        for hook in repair.BUILTINS:
            owner = repair.owned_chain(hook)
            rows = (
                *rows,
                ("CHAIN", owner, "-"),
                ("-A", hook, "-j", owner),
                *(("-A", owner, *rule) for rule in repair.own_rules(family, hook)),
            )
        result[f"{family}:filter"] = rows
    return result


def test_unrelated_drift_is_indicated_without_failing_owned_check():
    tables = baseline()
    reference = repair.hashes(tables)
    initial = check.evaluate(repair, tables, reference)
    tables["4:filter"] += (("-A", "UNRELATED", "-j", "RETURN"),)
    changed = check.evaluate(repair, tables, reference)
    assert initial["rules_verified"] == 10 and initial["global_drift"] == []
    assert changed["owned_sha256"] == initial["owned_sha256"]
    assert changed["global_drift"] == ["4:filter"]


@pytest.mark.parametrize("kind", ["prior", "duplicate", "content", "dnat"])
def test_owned_or_reachability_drift_fails(kind):
    tables = baseline()
    if kind == "prior":
        rows = tables["4:filter"]
        index = rows.index(("-A", "FORWARD", "-j", "PPPOE_FORWARD"))
        tables["4:filter"] = rows[:index] + (("-A", "FORWARD", "-j", "ACCEPT"),) + rows[index:]
    elif kind == "duplicate":
        tables["6:filter"] += (("-A", "FORWARD", "-j", repair.owned_chain("FORWARD")),)
    elif kind == "content":
        tables["6:filter"] += (("-A", repair.owned_chain("FORWARD"), "-j", "ACCEPT"),)
    else:
        tables["4:nat"] = tables["4:nat"][:-1]
    with pytest.raises(ValueError, match="(?:HOOK|CADENA|DNAT)_"):
        check.evaluate(repair, tables, repair.hashes(baseline()))
