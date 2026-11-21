"""Pruebas de recuperación de las reglas propias al recargar DSM."""

from pathlib import Path
from runpy import run_path
from types import SimpleNamespace

import pytest
from test_observability_nas_alertmanager_filter import SMTP, am, baseline_with_nat
from test_observability_nas_filter_check import check, repair
from test_observability_nas_grafana_filter import grafana

SOURCE = Path(__file__).parents[1] / "docker" / "observability" / "nas_filter_recover.py"


def states():
    base = baseline_with_nat()
    reference = repair.hashes(base)
    layer = am.expected_extension(check, repair, base, reference, SMTP)
    target = grafana.expected_extension(
        check,
        repair,
        layer,
        reference,
        (
            am,
            {
                "reference": reference,
                "smtp_ipv4": SMTP,
            },
        ),
    )
    lost = {}
    for key, rows in target.items():
        if key.endswith(":filter"):
            lost[key] = tuple(
                row for row in rows if not any(field.startswith("APROM_V1_") for field in row)
            )
        else:
            lost[key] = tuple(
                row
                for row in rows
                if row
                not in {
                    ("-A", repair.NAT_CHAIN, *repair.DNAT),
                    ("-A", am.POSTROUTING, *am.changes(repair, SMTP)[key][am.POSTROUTING][1][0]),
                }
            )
    return lost, target


def plan(live, target):
    return run_path(str(SOURCE))["recovery_plan"](repair, am, SMTP, live, target)


def test_reponer_cadenas_saltos_y_nat_sin_tocar_reglas_de_dsm():
    live, target = states()
    steps = plan(live, target)
    assert [(step["table"], step["action"]) for step in steps] == [
        ("4:filter", "restore"),
        ("4:filter", "hooks"),
        ("6:filter", "restore"),
        ("6:filter", "hooks"),
        ("4:nat", "restore"),
    ]
    assert steps[-1]["after"] == repair.hashes(target)
    assert all("PPPOE_FORWARD" not in step.get("payload", "") for step in steps)
    assert plan(target, target) == []


def test_reponer_solo_la_regla_ausente_en_cadena_propia():
    _, target = states()
    missing = ("-A", grafana.CHAIN, *grafana.rules(repair)[0])
    live = {**target, "4:filter": tuple(row for row in target["4:filter"] if row != missing)}
    [step] = plan(live, target)
    assert step["action"] == "restore" and step["table"] == "4:filter"
    assert step["payload"].count("-I ") == 1
    assert step["after"] == repair.hashes(target)


def test_rechaza_reglas_ajenas_distintas_y_reglas_propias_conflictivas():
    live, target = states()
    changed = {**live, "4:filter": (*live["4:filter"], ("-A", "INPUT", "-j", "DROP"))}
    with pytest.raises(ValueError, match="TOPOLOGIA_DSM"):
        plan(changed, target)
    changed = {**target, "4:filter": (*target["4:filter"], ("-A", grafana.CHAIN, "-j", "ACCEPT"))}
    with pytest.raises(ValueError, match="REGLA_PROPIA_INESPERADA"):
        plan(changed, target)


def test_arranque_reconstruye_nat_y_conserva_las_cadenas_recreadas_por_docker():
    live, approved = states()
    docker = ("CHAIN", "DOCKER_NUEVA", "-")
    foreign = ("-A", "DOCKER_NUEVA", "-j", "RETURN")
    live["4:filter"] += (docker, foreign)
    live["4:nat"] = tuple(row for row in live["4:nat"] if repair.NAT_CHAIN not in row)
    recover = run_path(str(SOURCE))
    target = recover["startup_target"](repair, am, SMTP, live, approved)
    chain = repair.chains(target["4:filter"])["DOCKER_NUEVA"]
    assert chain == {"policy": "-", "rules": [("-j", "RETURN")]}
    steps = recover["recovery_plan"](repair, am, SMTP, live, target, startup=True)
    assert [(step["table"], step["action"]) for step in steps][-2:] == [
        ("4:nat", "restore"),
        ("4:nat", "nat_hook"),
    ]
    assert "-N DEFAULT_PREROUTING" in steps[-2]["payload"]
    assert "-A PREROUTING" not in steps[-2]["payload"]
    assert steps[-1]["after"] == repair.hashes(target)
    assert recover["recovery_plan"](repair, am, SMTP, target, target, startup=True) == []


def test_arranque_rechaza_un_salto_nat_ajeno_en_prerouting():
    live, approved = states()
    live["4:nat"] += (("-A", "PREROUTING", "-j", "DROP"),)
    recover = run_path(str(SOURCE))
    with pytest.raises(ValueError, match="PREROUTING_INESPERADO"):
        recover["startup_target"](repair, am, SMTP, live, approved)


def test_arranque_conserva_default_forward_detras_del_filtro_propio():
    live, approved = states()
    live["4:filter"] += (
        ("CHAIN", "DEFAULT_FORWARD", "-"),
        ("-A", "FORWARD", "-j", "DEFAULT_FORWARD"),
        ("-A", "DEFAULT_FORWARD", "-j", "DOCKER-USER"),
    )
    recover = run_path(str(SOURCE))
    target = recover["startup_target"](repair, am, SMTP, live, approved)
    table = repair.chains(target["4:filter"])
    assert table["FORWARD"]["rules"] == [
        ("-j", "PPPOE_FORWARD"),
        ("-j", repair.owned_chain("FORWARD")),
        ("-j", "DEFAULT_FORWARD"),
    ]
    assert table["DEFAULT_FORWARD"] == repair.chains(live["4:filter"])["DEFAULT_FORWARD"]
    steps = recover["recovery_plan"](repair, am, SMTP, live, target, startup=True)
    [ipv4] = [step for step in steps if step.get("preserve_dsm_forward")]
    assert (ipv4["action"], ipv4["family"]) == ("hooks", 4)
    assert recover["recovery_plan"](repair, am, SMTP, target, target, startup=True) == []


def test_arranque_de_contenedores_exige_filtros_y_revierte_si_falla(monkeypatch):
    recover = run_path(str(SOURCE))
    launch = recover["start_containers"]
    identities = ({"cid": "prometheus"}, {"cid": "alertmanager"}, {})
    commands = []

    def command(argv):
        commands.append(argv[-2:])
        if argv[-1] == "prometheus":
            raise ValueError("COMANDO_RECHAZADO")
        return argv[-1].encode()

    host = SimpleNamespace(
        DOCKER="docker",
        snapshot=lambda: {"filter": "seguro"},
        hashes=lambda state: state,
        command=command,
    )
    monkeypatch.setattr(
        launch.__globals__["subprocess"], "run", lambda argv, **_options: commands.append(argv[-2:])
    )
    with pytest.raises(ValueError, match="CAMBIO_CONCURRENTE"):
        launch(host, identities, {"filter": "incompleto"}, lambda: identities)
    assert commands == []
    with pytest.raises(ValueError, match="COMANDO_RECHAZADO"):
        launch(host, identities, {"filter": "seguro"}, lambda: identities)
    assert [command[-1] for command in commands] == [
        "alertmanager",
        "prometheus",
        "prometheus",
        "alertmanager",
    ]
