"""Pruebas del delta Grafana sobre el filtro NAS reparado."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_observability_nas_alertmanager_filter import SMTP, am, baseline_with_nat
from test_observability_nas_filter_check import baseline, check, repair

SOURCE = Path(__file__).parents[1] / "docker" / "observability" / "nas_grafana_filter.py"
SPEC = importlib.util.spec_from_file_location("nas_grafana_filter", SOURCE)
grafana = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grafana)


def test_extension_inserta_solo_en_cadena_propia_antes_de_drop():
    tables = baseline()
    reference = repair.hashes(tables)
    extended = grafana.expected_extension(check, repair, tables, reference)
    v4 = repair.chains(extended["4:filter"])
    original = repair.chains(tables["4:filter"])
    assert v4[grafana.CHAIN]["rules"] == [
        *repair.TRANSIT,
        *grafana.rules(repair),
        *repair.own_rules(4, "FORWARD")[2:],
    ]
    assert v4["FORWARD"] == original["FORWARD"]
    assert extended["6:filter"] == tables["6:filter"]
    assert extended["4:nat"] == tables["4:nat"]


def test_rechaza_deriva_y_aceptacion_antes_del_hook():
    tables = baseline()
    reference = repair.hashes(tables)
    changed = dict(tables)
    rows = changed["4:filter"]
    index = rows.index(("-A", "FORWARD", "-j", grafana.CHAIN))
    changed["4:filter"] = rows[:index] + (("-A", "FORWARD", "-j", "ACCEPT"),) + rows[index:]
    with pytest.raises(ValueError, match="HOOK_FILTRO"):
        grafana.expected_extension(check, repair, changed, reference)
    changed["4:filter"] = rows + (("CHAIN", "AJENA", "-"),)
    with pytest.raises(ValueError, match="GRAFANA_BASE_DERIVA"):
        grafana.expected_extension(check, repair, changed, reference)


def test_payload_solo_ipv4_con_estados_y_direcciones_exactas():
    request, reply = grafana.rules(repair)
    assert ("-i", grafana.GRAFANA_BRIDGE, "-o", grafana.PROMETHEUS_BRIDGE) == request[4:8]
    assert ("-i", grafana.PROMETHEUS_BRIDGE, "-o", grafana.GRAFANA_BRIDGE) == reply[4:8]
    assert "NEW,ESTABLISHED" in request and "ORIGINAL" in request
    assert "ESTABLISHED" in reply and "NEW,ESTABLISHED" not in reply and "REPLY" in reply
    assert grafana.payload(repair, True).decode().splitlines() == [
        "*filter",
        "-I APROM_V1_FORWARD 3 " + " ".join(request),
        "-I APROM_V1_FORWARD 4 " + " ".join(reply),
        "COMMIT",
    ]
    assert grafana.payload(repair, False).decode().splitlines() == [
        "*filter",
        "-D APROM_V1_FORWARD " + " ".join(reply),
        "-D APROM_V1_FORWARD " + " ".join(request),
        "COMMIT",
    ]


def test_reversion_atomica_no_toca_estado_desconocido(monkeypatch):
    tables = baseline()
    reference = repair.hashes(tables)
    extended = grafana.expected_extension(check, repair, tables, reference)
    proposal = {"before": reference, "after": repair.hashes(extended)}
    current = extended
    calls = []

    def restore(_repair, enable, *, test):
        nonlocal current
        calls.append((enable, test))
        if not test:
            current = tables

    monkeypatch.setattr(repair, "snapshot", lambda: current)
    monkeypatch.setattr(grafana, "restore", restore)
    assert grafana.reverse(repair, proposal) == reference
    assert calls == [(False, True), (False, False)]
    assert grafana.reverse(repair, proposal) == reference
    assert len(calls) == 2
    current = dict(tables, **{"4:filter": tables["4:filter"] + (("CHAIN", "AJENA", "-"),)})
    with pytest.raises(ValueError, match="GRAFANA_REVERSION_ESTADO"):
        grafana.reverse(repair, proposal)
    assert len(calls) == 2


def test_test_nunca_confirma_tabla(monkeypatch):
    tables = baseline()
    plan = {"before": repair.hashes(tables)}
    monkeypatch.setattr(grafana, "propose", lambda *_: plan)
    monkeypatch.setattr(repair, "snapshot", lambda: tables)
    calls = []
    monkeypatch.setattr(grafana, "restore", lambda _, enable, *, test: calls.append((enable, test)))
    out = {}
    grafana.operation(check, repair, "test", "base", "id", repair.sha(repair.encode(plan)), out)
    assert out["test_verified"] is True
    assert calls == [(True, True)]


def test_restore_usa_bloqueo_normal_de_xtables(monkeypatch):
    calls = []

    def run(argv, **options):
        assert "XTABLES_LOCKFILE" not in options["env"]
        assert options["input"] == grafana.payload(repair, True)
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(grafana.subprocess, "run", run)
    grafana.restore(repair, True, test=True)
    grafana.restore(repair, True, test=False)
    command = [str(repair.MULTI), "iptables-restore", "--noflush", "--wait", "5"]
    assert calls == [command + ["--test"], command]


def test_deriva_durante_ensayo_impide_commit(monkeypatch):
    tables = baseline()
    after = grafana.expected_extension(check, repair, tables, repair.hashes(tables))
    changed = dict(tables, **{"4:filter": tables["4:filter"] + (("CHAIN", "AJENA", "-"),)})
    snapshots = iter([tables, changed])
    calls = []
    monkeypatch.setattr(repair, "snapshot", lambda: next(snapshots))
    monkeypatch.setattr(grafana, "restore", lambda _, enable, *, test: calls.append((enable, test)))
    with pytest.raises(ValueError, match="GRAFANA_CAMBIO_CONCURRENTE"):
        grafana.change(
            repair, {"before": repair.hashes(tables), "after": repair.hashes(after)}, True
        )
    assert calls == [(True, True)]


def test_plan_fija_identidad_y_dos_huellas(monkeypatch):
    tables = baseline()
    reference = repair.hashes(tables)
    host = {"cid": repair.CID, "pid": 123, "started_at": "utc"}
    grafana_identity = {"cid": grafana.CID, "pid": 456, "network": grafana.NETWORK}
    monkeypatch.setattr(check, "journal", lambda *_: (host, reference))
    monkeypatch.setattr(repair, "host", lambda: host)
    monkeypatch.setattr(repair, "snapshot", lambda: tables)
    monkeypatch.setattr(grafana, "grafana_host", lambda _: grafana_identity)
    plan = grafana.propose(check, repair, "base", "id")
    assert plan["grafana"] == grafana_identity and plan["prometheus"] == host
    assert plan["before"]["4:filter"] != plan["after"]["4:filter"]
    assert plan["before"]["4:nat"] == plan["after"]["4:nat"]
    assert json.loads(repair.encode(plan))["insert_at"] == 3


@pytest.fixture
def alertmanager():
    base = baseline_with_nat()
    reference = repair.hashes(base)
    tables = am.expected_extension(check, repair, base, reference, SMTP)
    plan = {
        "journal_id": "am-test",
        "smtp_ipv4": SMTP,
        "reference": reference,
        "after": repair.hashes(tables),
    }
    return (am, plan), tables


def test_delta_combinado_preserva_alertmanager_y_nat(alertmanager):
    context, tables = alertmanager
    _, plan = context
    after = grafana.expected_extension(check, repair, tables, plan["reference"], context)
    offset = grafana.insert_offset(repair, context)
    assert offset == 8
    original = repair.chains(tables[grafana.FILTER])[grafana.CHAIN]["rules"]
    assert repair.chains(after[grafana.FILTER])[grafana.CHAIN]["rules"] == [
        *original[:offset],
        *grafana.rules(repair),
        *original[offset:],
    ]
    assert grafana.stripped_extension(repair, after, offset) == tables
    assert grafana.payload(repair, True, offset=offset).decode().splitlines()[1:3] == [
        f"-I APROM_V1_FORWARD {i} " + " ".join(row)
        for i, row in enumerate(grafana.rules(repair), start=9)
    ]


def test_check_combinado_admite_deriva_ajena_y_rechaza_reglas_faltantes(monkeypatch, alertmanager):
    context, tables = alertmanager
    _, plan = context
    after = grafana.expected_extension(check, repair, tables, plan["reference"], context)
    identity = {"cid": grafana.CID, "pid": 123}
    proposal = {"after": repair.hashes(after), "insert_at": 9, "grafana": identity}
    current = {**after, "4:nat": (*after["4:nat"], ("CHAIN", "AJENA_NAT", "-"))}
    monkeypatch.setattr(grafana, "alertmanager_attest", lambda *_: {"running": True})
    monkeypatch.setattr(grafana, "grafana_host", lambda _: identity)
    monkeypatch.setattr(repair, "snapshot", lambda: current)
    proof = grafana.check_combined(check, repair, proposal, context)
    assert proof["alertmanager_rules_verified"] == 13
    assert proof["prometheus_rules_verified"] == 10 and proof["dnat_verified"]
    assert proof["global_drift"] == ["4:nat"]

    grafana_row = ("-A", grafana.CHAIN, *grafana.rules(repair)[0])
    current = {
        **after,
        grafana.FILTER: tuple(row for row in after[grafana.FILTER] if row != grafana_row),
    }
    with pytest.raises(ValueError, match="GRAFANA_REGLAS_PROPIAS"):
        grafana.check_combined(check, repair, proposal, context)

    for key in ("6:filter", "4:nat"):
        chain, (_, rules) = next(iter(am.changes(repair, SMTP)[key].items()))
        missing = ("-A", chain, *rules[0])
        current = {**after, key: tuple(row for row in after[key] if row != missing)}
        with pytest.raises(ValueError, match="AM_REGLA_PROPIA"):
            grafana.check_combined(check, repair, proposal, context)


def test_reversion_combinada_preserva_deriva_ajena(monkeypatch, alertmanager):
    context, tables = alertmanager
    _, plan = context
    after = grafana.expected_extension(check, repair, tables, plan["reference"], context)
    drift = ("CHAIN", "AJENA_V6", "-")
    current = {**after, "6:filter": (*after["6:filter"], drift)}
    restored = {**tables, "6:filter": (*tables["6:filter"], drift)}
    calls = []

    def restore(_repair, enable, *, test, offset):
        nonlocal current
        calls.append((enable, test, offset))
        if not test:
            current = restored

    monkeypatch.setattr(grafana, "alertmanager_attest", lambda *_: {"running": False})
    monkeypatch.setattr(repair, "snapshot", lambda: current)
    monkeypatch.setattr(grafana, "restore", restore)
    proposal = {"before": repair.hashes(tables), "after": repair.hashes(after), "insert_at": 9}
    assert grafana.reverse_combined(check, repair, proposal, context) == repair.hashes(restored)
    assert current == restored and calls == [(False, True, 8), (False, False, 8)]
    assert grafana.reverse_combined(check, repair, proposal, context) == repair.hashes(restored)
    assert len(calls) == 2


def test_journal_combinado_exige_plan_alertmanager_exacto(monkeypatch, alertmanager, tmp_path):
    context, tables = alertmanager
    _, plan = context
    host = {"cid": repair.CID}
    proposal = {
        "baseline_plan_sha256": "base",
        "journal_id": "grafana-id",
        "prometheus": host,
        "before": repair.hashes(tables),
        "insert_at": 9,
        "alertmanager": grafana.alertmanager_binding(repair, context),
    }
    raw = repair.encode(proposal)
    monkeypatch.setattr(check, "journal", lambda *_: (host, plan["reference"]))
    monkeypatch.setattr(check, "read_file", lambda *_: raw)
    args = (check, repair, tmp_path, "base", "grafana-id", repair.sha(raw))
    assert grafana.read_proposal(*args, alertmanager=context) == proposal
    with pytest.raises(ValueError, match="GRAFANA_JOURNAL_BASE"):
        grafana.read_proposal(*args)
    with pytest.raises(ValueError, match="GRAFANA_AM_BASE"):
        grafana.read_proposal(*args, alertmanager=(am, {**plan, "journal_id": "ajeno"}))
