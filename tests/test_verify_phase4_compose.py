import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from docker import verify_phase4_compose as verify


def manifest():
    return verify.load_manifest(Path("docker/phase4-runtime.json"))


def test_manifest_keeps_projects_and_supoclip_file_histories():
    data = manifest()
    verify.validate(data, sources=False)
    spec = data["projects"]["agency-supoclip-trial-nov13"]
    assert [group["files"] for group in spec["groups"]] == [
        ["compose.functional.yml", "compose.integration.override.yml"],
        [
            "compose.functional.yml",
            "compose.integration.override.yml",
            "compose.local-model.yml",
            "compose.transcript-timestamps.yml",
        ],
    ]
    assert spec["replacement_overlay"]["source"].endswith("compose.boot.yml")
    assert data["engine"]["r2"]["roles"] == {"serve": "running", "init": "optional_exited"}


@pytest.mark.evidencia_operativa
def test_manifest_matches_operational_evidence():
    verify.validate(manifest())


def test_manifest_rejects_changed_scope():
    data = manifest()
    data["projects"]["agency-media"]["groups"][0]["roles"].append("foreign")
    with pytest.raises(ValueError, match="ROLES"):
        verify.validate(data)


def test_commands_keep_project_and_role_scope():
    data = manifest()
    project = "agency-supoclip-trial-nov13"
    spec = data["projects"][project]
    command = verify.compose_args(project, spec, spec["groups"][1])
    assert command[-4:] == ["config", "--quiet", "model", "worker"]
    assert command[command.index("--project-name") + 1] == project
    assert command[command.index("--env-file") + 1] == "/dev/null"
    assert "compose.boot.yml" not in " ".join(command)


def test_scoped_variables_reject_unrelated_and_malformed_values():
    data = manifest()
    profiles = {
        project: {key: "value" for key in spec["variables"]}
        for project, spec in data["projects"].items()
        if spec["variables"]
    }
    assert verify.scoped_env(data, json.dumps(profiles).encode()) == profiles
    profiles["agency-observability-nas-v1"]["DOCKER_HOST"] = "tcp://example.invalid"
    with pytest.raises(ValueError, match="VARIABLES"):
        verify.scoped_env(data, json.dumps(profiles).encode())
    del profiles["agency-observability-nas-v1"]["DOCKER_HOST"]
    profiles["agency-observability-nas-v1"]["AGENCY_OBS_NODE_IMAGE"] = "one\ntwo"
    with pytest.raises(ValueError, match="VARIABLES"):
        verify.scoped_env(data, json.dumps(profiles).encode())


def test_check_expects_read_only_moneyprinter_env_only(monkeypatch):
    data = manifest()
    variables = Path("/tmp/phase4-test-variables.json")
    profiles = {
        project: {key: "value" for key in spec["variables"]}
        for project, spec in data["projects"].items()
        if spec["variables"]
    }
    compose_files = {
        str(Path(spec["workdir"]) / name): record["installed_sha256"].encode()
        for spec in data["projects"].values()
        for name, record in spec["files"].items()
    }
    expected = {
        str(Path(spec["env_file"])): 0o400 if project == "agency-moneyprinter-lan" else 0o600
        for project, spec in data["projects"].items()
        if spec["env_file"] != "/dev/null"
    }
    seen = {}

    def checked(path, mode, *, content=False):
        if path == variables:
            assert (mode, content) == (0o600, True)
            return json.dumps(profiles).encode()
        if path.name == "compose.env":
            assert content is False
            seen[str(path)] = mode
            verify.require(expected[str(path)] == mode, "ARCHIVO_IDENTIDAD")
            return None
        return compose_files[str(path)]

    monkeypatch.setattr(verify.sys, "platform", "linux")
    monkeypatch.setattr(
        verify.os, "uname", lambda: SimpleNamespace(nodename=verify.HOST), raising=False
    )
    monkeypatch.setattr(verify.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(verify.os, "getegid", lambda: 0, raising=False)
    monkeypatch.setattr(verify, "checked_file", checked)
    monkeypatch.setattr(verify, "sha", lambda raw: raw.decode())
    monkeypatch.setattr(
        verify.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    result = verify.check(data, variables)
    assert seen == expected
    assert all(group["config_verified"] for rows in result.values() for group in rows)
