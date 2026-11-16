import io
import json
from copy import deepcopy

import pytest

from docker import verify_phase4_runtime as verifier


def observations():
    rows = []
    for index, (key, contract) in enumerate(verifier.SERVICES.items(), 1):
        if contract.optional:
            continue
        rows.append(
            {
                "key": key,
                "id": f"{index:064x}",
                "image": f"sha256:{index:064x}",
                "status": contract.status,
                "health": "healthy" if contract.health else None,
                "exit_code": 0,
            }
        )
    units = {
        unit: {
            "ActiveState": "active",
            "SubState": substate,
            "UnitFileState": "enabled",
            "Result": "success",
        }
        for unit, substate in verifier.UNITS.items()
    }
    return rows, units


def test_verifies_live_inventory_without_claiming_unpinned_images():
    rows, units = observations()
    result = verifier.verify(rows, units, {})
    assert result["success"]
    assert result["images_pinned"] == 0
    assert len(result["containers"]) == len(rows)
    assert len(result["units"]) == 6
    assert result["units"]["agency-kitsu-zou.service"]["SubState"] == "running"


def test_fails_if_health_migrator_r2_or_unit_do_not_match():
    rows, units = observations()
    by_key = {row["key"]: row for row in rows}
    by_key["agency-n8n-private/n8n"]["health"] = None
    by_key["agency-supoclip-trial-nov13/migrate"]["exit_code"] = 1
    by_key["r2/serve"]["status"] = "exited"
    units["agency-moneyprinter-lan.service"]["SubState"] = "exited"
    result = verifier.verify(rows, units, {})
    assert not result["success"]
    assert set(result["errors"]) == {
        "agency-n8n-private/n8n:health",
        "agency-supoclip-trial-nov13/migrate:exit_code",
        "r2/serve:status",
        "agency-moneyprinter-lan.service:SubState",
    }


def test_rejects_ambiguous_docker_inventory_and_drifted_pin():
    rows, units = observations()
    wrong = deepcopy(rows[0])
    wrong["id"] = "e" * 64
    last = rows[-1]
    result = verifier.verify([*rows, wrong], units, {last["key"]: "sha256:" + "a" * 64})
    assert not result["success"]
    assert result["images_pinned"] == 1
    assert f"{wrong['key']}:duplicate" in result["errors"]
    assert f"{last['key']}:image" in result["errors"]


def test_pins_file_is_closed_and_can_be_partial():
    valid = {"schema": 1, "images": {"r2/serve": "sha256:" + "d" * 64}}
    assert verifier.read_pins(io.BytesIO(json.dumps(valid).encode())) == valid["images"]
    for bad in (
        {"schema": True, "images": {}},
        {"schema": 1, "images": {"unknown": "a"}},
        {"schema": 1, "images": {"r2/serve": "latest"}},
    ):
        with pytest.raises(ValueError):
            verifier.read_pins(io.BytesIO(json.dumps(bad).encode()))
    with pytest.raises(ValueError):
        verifier.read_pins(io.BytesIO(b'{"schema":1,"schema":1,"images":{}}'))


def test_docker_inspection_only_requests_named_projection(monkeypatch):
    fake_id = "0" * 64
    row = {
        "id": fake_id,
        "image": "sha256:" + fake_id,
        "project": "agency-media",
        "service": "postgres-apps",
        "owner": "",
        "r2_role": "",
        "status": "running",
        "health": "healthy",
        "exit_code": 0,
    }
    commands = []

    def docker(*args):
        commands.append(args)
        if args[0] == "ps":
            return fake_id + "\n" if args[-1].endswith("=agency-media") else ""
        return json.dumps(row)

    monkeypatch.setattr(verifier, "docker", docker)
    assert verifier.docker_inventory()[0]["key"] == "agency-media/postgres-apps"
    assert len(commands) == 9
    assert commands[-1][:5] == ("inspect", "--type", "container", "--format", verifier.INSPECT)
    assert ".Config.Env" not in verifier.INSPECT
    assert "{{json .Config}}" not in verifier.INSPECT
