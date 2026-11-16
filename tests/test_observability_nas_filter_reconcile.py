import importlib.util
from contextlib import nullcontext
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "docker" / "observability" / "nas_filter_reconcile.py"
SPEC = importlib.util.spec_from_file_location("nas_filter_reconcile", SOURCE)
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


def baseline():
    header = tuple(("CHAIN", chain, "ACCEPT") for chain in repair.BUILTINS)
    return {
        "4:filter": header
        + (
            ("CHAIN", "PPPOE_FORWARD", "-"),
            ("-A", "FORWARD", "-j", "PPPOE_FORWARD"),
            ("-A", "PPPOE_FORWARD", "-d", "10.0.0.0/8", "-i", "tun0", "-j", "DROP"),
        ),
        "6:filter": header,
        "4:nat": (
            ("CHAIN", "PREROUTING", "ACCEPT"),
            ("CHAIN", "DEFAULT_PREROUTING", "-"),
            ("-A", "PREROUTING", "-j", "DEFAULT_PREROUTING"),
            ("-A", "DEFAULT_PREROUTING", *repair.DNAT),
        ),
    }


def test_plan_is_scoped_and_exact():
    tables = baseline()
    plan = repair.plan(tables)
    assert plan["expected_filter_rows"] == {"4": 6, "6": 4}
    assert plan["hook_offsets"] == {
        "4": {"INPUT": 0, "FORWARD": 1, "OUTPUT": 0},
        "6": {"INPUT": 0, "FORWARD": 0, "OUTPUT": 0},
    }
    assert plan["tables_before"] == repair.hashes(tables)
    assert repair.plan(tables) == plan


def test_drift_and_duplicate_nat_are_rejected():
    tables = baseline()
    tables["4:nat"] += (("-A", "DEFAULT_PREROUTING", *repair.DNAT),)
    with pytest.raises(ValueError, match="DNAT"):
        repair.plan(tables)
    tables = baseline()
    tables["4:filter"] += (("-A", "FORWARD", "-i", "br-apromv1", "-j", "DROP"),)
    with pytest.raises(ValueError, match="FORWARD"):
        repair.plan(tables)


@pytest.mark.parametrize("family", [4, 6])
def test_jump_entry_matches_historical_x86_layout(family):
    data = repair.jump_entry(family, b"APROM_V1_FORWARD")
    ip_size, entry_size = repair.LAYOUTS[family]
    assert data.raw[:ip_size] == bytes(ip_size)
    assert data.raw[entry_size + 2 : entry_size + 18] == b"APROM_V1_FORWARD"
    assert int.from_bytes(data.raw[entry_size : entry_size + 2], "little") == 40


def test_rollback_detaches_both_families_before_removing_own_rows(monkeypatch):
    states = repair.predicted_states(baseline())
    steps = tuple(repair.operations())
    seen = []
    current = len(steps)

    def mutate(step, enabled, before, libs):
        nonlocal current
        assert not enabled and before == states[current]
        assert before["4:nat"] == states[0]["4:nat"]
        seen.append(step)
        current -= 1

    monkeypatch.setattr(repair, "snapshot", lambda: None)
    monkeypatch.setattr(repair, "hashes", lambda _: states[current])
    monkeypatch.setattr(repair, "mutate", mutate)
    repair.rollback(states, {})
    assert seen == list(reversed(steps)) and current == 0


def test_rollback_refuses_unknown_drift(monkeypatch):
    states = repair.predicted_states(baseline())
    monkeypatch.setattr(repair, "snapshot", lambda: None)
    monkeypatch.setattr(repair, "hashes", lambda _: {"4:filter": "unknown"})
    monkeypatch.setattr(repair, "mutate", lambda *args: pytest.fail("mutation despite drift"))
    with pytest.raises(ValueError, match="REVERSION_ESTADO_DESCONOCIDO"):
        repair.rollback(states, {})


def test_hook_trial_never_commits(monkeypatch):
    class FakeLib:
        def init(self, table):
            assert table == b"filter"
            return 123

        def create_chain(self, chain, handle):
            assert chain.startswith(b"APROM_V1_") and handle == 123
            return True

        def insert_entry(self, chain, entry, offset, handle):
            calls.append((chain, offset))
            assert handle == 123 and entry
            return True

        def commit(self, handle):
            pytest.fail("trial committed")

        def free(self, handle):
            assert handle == 123

    calls = []
    inspect = []
    before = repair.hashes(baseline())
    monkeypatch.setattr(repair, "lock", lambda _: nullcontext())
    monkeypatch.setattr(repair, "snapshot", baseline)
    monkeypatch.setattr(
        repair, "inspect_hooks", lambda lib, family, handle, hooked: inspect.append(hooked)
    )
    repair.hooks(4, True, before, FakeLib(), trial=True)
    assert calls == [(b"INPUT", 0), (b"FORWARD", 1), (b"OUTPUT", 0)]
    assert inspect == [False, True]
