import importlib.util
import shlex
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, ROOT / path)
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    return source


@pytest.fixture
def guards(monkeypatch):
    if importlib.util.find_spec("fcntl") is None:
        monkeypatch.setitem(sys.modules, "fcntl", ModuleType("fcntl"))
    return (
        module(Path("docker/moneyprinter/lan_guard.py")),
        module(Path("docker/observability/nas_operator.py")),
        module(Path("docker/supoclip/supoclip_boot.py")),
    )


def local_tables(guards, *, installed, obs=True):
    money, observer, supoclip = guards
    tables = {}
    for family, table in ((4, "iptables"), (6, "ip6tables")):
        _, _, hooks = observer.delta("media0", "172.26.1.2")[family]
        forward = [list(row) for row in hooks[:2]] if obs else []
        rows = {"FORWARD": [*forward, ["-j", "DOCKER-USER"]], "INPUT": [], "DOCKER-USER": []}
        if obs:
            rows["INPUT"].append(list(hooks[2]))
        for chain in money.CHAINS:
            if installed:
                rows[chain].extend(money.old_rules(table, chain))
                rows[chain].extend(money.new_rules(table, chain))
        if family == 4:
            rows["INPUT"].extend(map(list, supoclip.INPUT_RULES))
            rows["DOCKER-USER"].extend(map(list, supoclip.USER_RULES))
        rows["DOCKER-USER"].append(["-j", "RETURN"])
        tables[table] = rows
    return tables


def mock_tables(monkeypatch, money, tables):
    monkeypatch.setattr(money, "chain_rows", lambda table, chain: tables[table][chain])
    monkeypatch.setattr(money, "firewall_command", lambda *args: None)


@pytest.mark.parametrize("table", ["iptables", "ip6tables"])
def test_cold_start_keeps_exact_collector_guards_and_inserts_moneyprinter(
    monkeypatch, guards, table
):
    money, _observer, _supoclip = guards
    tables = local_tables(guards, installed=False)
    mock_tables(monkeypatch, money, tables)
    statements, other_rules, old = money.table_state(table)
    assert statements[0].startswith("-I DOCKER-USER 1 ")
    first_input = next(line for line in statements if line.startswith("-I INPUT "))
    assert first_input.startswith("-I INPUT 2 ")
    assert other_rules["INPUT"] == tables[table]["INPUT"]
    assert not old


def test_verify_preserves_installed_observability_moneyprinter_and_supoclip(monkeypatch, guards):
    money, _observer, _supoclip = guards
    tables = local_tables(guards, installed=True)
    mock_tables(monkeypatch, money, tables)
    assert money.firewall(verify_only=True) is False


def test_missing_observability_is_supported(monkeypatch, guards):
    money, _observer, _supoclip = guards
    tables = local_tables(guards, installed=True, obs=False)
    mock_tables(monkeypatch, money, tables)
    assert money.firewall(verify_only=True) is False


@pytest.mark.parametrize("table", ["iptables", "ip6tables"])
def test_generic_accept_or_partial_observability_blocks_boot(monkeypatch, guards, table):
    money, _observer, _supoclip = guards
    tables = local_tables(guards, installed=False)
    rows = tables[table]
    mock_tables(monkeypatch, money, tables)
    rows["FORWARD"].insert(0, ["-j", "ACCEPT"])
    with pytest.raises(ValueError, match="FIREWALL_FORWARD"):
        money.table_state(table)
    del rows["FORWARD"][0]
    del rows["INPUT"][0]
    with pytest.raises(ValueError, match="FIREWALL_OBSERVABILIDAD"):
        money.table_state(table)


def test_cold_boot_three_guards_preserve_each_others_rules(monkeypatch, guards):
    money, observer, supoclip = guards
    address = "172.26.1.2"
    families = {"iptables": 4, "ip6tables": 6}
    docker_rules = {
        "INPUT": ("ACCEPT", []),
        "FORWARD": ("DROP", [("-j", "DOCKER-USER"), ("-j", "DOCKER-FORWARD")]),
        "DOCKER-USER": ("-", [("-j", "RETURN")]),
    }
    initial = {4: docker_rules, 6: docker_rules}
    filtered = observer.expected_firewall(initial, observer.delta("media0", address), remove=False)
    tables = {
        table: {
            chain: [list(rule) for rule in rows] for chain, (_, rows) in filtered[family].items()
        }
        for table, family in families.items()
    }
    mock_tables(monkeypatch, money, tables)

    def apply(table, statements, test):
        if not test:
            for statement in statements:
                command, chain, index, *rule = shlex.split(statement)
                assert command == "-I"
                tables[table][chain].insert(int(index) - 1, rule)

    monkeypatch.setattr(money, "restore", apply)
    assert money.firewall(verify_only=False)
    installed, _ = supoclip.firewall_change(
        {chain: list(map(tuple, rules)) for chain, rules in tables["iptables"].items()}
    )
    tables["iptables"] = {chain: list(map(list, rules)) for chain, rules in installed.items()}
    assert money.firewall(verify_only=True) is False
    combined = {
        family: {
            chain: (filtered[family][chain][0], list(map(tuple, rules)))
            for chain, rules in tables[table].items()
        }
        for table, family in families.items()
    }
    monkeypatch.setattr(observer, "iptables_save", lambda family: combined[family])
    assert observer.verify_firewall("media0", address, present=True) == combined
    assert supoclip.same_rules(installed, supoclip.firewall_change(installed)[0])
