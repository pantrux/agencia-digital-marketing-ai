"""Comprueba el día exigible y el cotejo de los dos destinos de D1."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from sre import d1_backup_metrics as metrics


@pytest.mark.parametrize("month,hour", [(7, 7), (11, 6)])
def test_espera_la_ventana_en_ambos_horarios(month, hour):
    assert metrics.expected_run(datetime(2026, month, 16, hour, 26, 59, tzinfo=UTC)) == (
        f"d1-clock-2026-{month:02}-15"
    )
    assert metrics.expected_run(datetime(2026, month, 16, hour, 27, tzinfo=UTC)) == (
        f"d1-clock-2026-{month:02}-16"
    )


def receipt(snapshot):
    return {
        "success": True,
        "exit_code": 0,
        "receipt_sha256": "d" * 64,
        "snapshot_id": snapshot * 64,
        "reconciliation_required": False,
    }


@pytest.mark.parametrize(
    "problem", [None, "otro_dia", "fallo", "solo_nas", "id_repetido", "huella"]
)
def test_coteja_dia_success_y_recibos_de_dos_destinos(problem):
    run_id = "d1-clock-2026-11-16"
    receipts = {"nas": receipt("a"), "r2": receipt("b")}
    result = {
        "run_id": run_id,
        "success": True,
        "d1_backup_complete": True,
        "manifest_sha256": "c" * 64,
        "destinations": deepcopy(receipts),
    }
    if problem == "otro_dia":
        result["run_id"] = "d1-clock-2026-11-15"
    elif problem == "fallo":
        result["d1_backup_complete"] = False
    elif problem == "solo_nas":
        receipts.pop("r2")
        result["destinations"].pop("r2")
    elif problem == "id_repetido":
        receipts["r2"] = result["destinations"]["r2"] = receipt("a")
    elif problem == "huella":
        receipts["r2"]["receipt_sha256"] = "e" * 64
    assert metrics.confirmed(run_id, result, receipts) is (problem is None)


def test_un_recibo_ausente_y_el_monitor_sin_muestras_son_estados_distintos():
    payload = metrics.metrics_payload("ausente", 10_000_000_000)
    reported = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    assert {row["name"]: row["gauge"]["dataPoints"][0]["asInt"] for row in reported} == {
        "studio_d1_backup_expected_day_confirmed": "0",
        "studio_d1_backup_report_unixtime_seconds": "10",
    }
