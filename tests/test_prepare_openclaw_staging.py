import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "sre/prepare_openclaw_staging.py"
SPEC = importlib.util.spec_from_file_location("prepare_openclaw_staging", SCRIPT)
staging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(staging)
HOME = PurePosixPath("/home/test")


def fixture():
    entries = {
        agent: {"model": model, "workspace": f"~/spaces/{agent}", "token": "CANARY_PRIVATE"}
        for agent, model in staging.AGENTS.items()
    }
    entries["other-team"] = {"model": "other/model", "workspace": "~/other-team"}
    return {"agents": {"entries": entries}, "gateway": {"token": "CANARY_GATEWAY"}}


def project(config, inventory=None, memories=None):
    def read(root, path, maximum):
        assert str(root) == "/home/test/spaces/agency-editor"
        assert maximum <= staging.backup.MAX_FILE
        return memories["/".join(path)]

    return staging.project(
        staging.config_entries(staging.backup.canonical(config)),
        inventory or {"schema": 1, "workspaces": {}},
        "agency-state",
        HOME,
        read,
    )


@pytest.mark.parametrize("style", ["list", "entries"])
def test_projects_exactly_five_explicit_models_and_no_secrets(style):
    config = fixture()
    entries = config["agents"]["entries"]
    entries["agency-editor"]["model"] = {"primary": "openai/gpt-6.1-terra", "fallbacks": ["a/b"]}
    if style == "list":
        config["agents"] = {"list": [dict(row, id=agent) for agent, row in entries.items()]}
    manifest, files = project(config)
    assert set(files) == {f"{agent}/config.json" for agent in staging.AGENTS}
    for agent, model in staging.AGENTS.items():
        assert json.loads(files[f"{agent}/config.json"]) == {"id": agent, "model": model}
    assert b"CANARY" not in manifest + b"".join(files.values())
    tar = staging.backup.pack_tar(manifest, files, "agency-state")
    assert staging.backup.unpack_tar(tar, "agency-state") == (manifest, files)


@pytest.mark.parametrize(
    "problem", ["missing", "wrong-model", "duplicate", "ambiguous", "defaults"]
)
def test_refuses_missing_duplicate_or_drifted_agency_config(problem):
    config = fixture()
    entries = config["agents"]["entries"]
    if problem == "missing":
        del entries["agency-editor"]
    if problem == "wrong-model":
        entries["agency-editor"]["model"] = "provider/changed"
    if problem == "duplicate":
        rows = [dict(row, id=agent) for agent, row in entries.items()]
        config["agents"] = {"list": rows + [rows[0]]}
    if problem == "ambiguous":
        config["agents"]["list"] = []
    if problem == "defaults":
        del entries["agency-editor"]["model"]
        config["agents"]["defaults"] = {"model": "openai/gpt-6.1-terra"}
    with pytest.raises(ValueError):
        project(config)


def test_copies_only_reviewed_markdown_from_the_configured_workspace():
    memories = {"MEMORY.md": b"Approved bytes.\n", "memory/day.md": b"Explicit note.\n"}
    root = "/home/test/spaces/agency-editor"
    paths = {name: staging.backup.digest(data) for name, data in memories.items()}
    selection = {"schema": 1, "workspaces": {"agency-editor": {"root": root, "files": paths}}}
    manifest, files = project(fixture(), selection, memories)
    assert files["agency-editor/MEMORY.md"] == memories["MEMORY.md"]
    assert files["agency-editor/memory/day.md"] == memories["memory/day.md"]
    assert len(files) == 7
    assert project(fixture(), selection, memories)[0] == manifest
    with pytest.raises(ValueError, match="bytes revisados"):
        project(fixture(), selection, memories | {"MEMORY.md": b"changed"})
    selection["workspaces"]["agency-editor"]["root"] = "/home/test/other-team"
    with pytest.raises(ValueError, match="raíz autorizada"):
        project(fixture(), selection, memories)


@pytest.mark.parametrize("path", ["config.json", "auth.json", "../MEMORY.md", "memory/token.bin"])
def test_excludes_unapproved_paths(path):
    inventory = {
        "schema": 1,
        "workspaces": {
            "agency-editor": {"root": "/home/test/spaces/agency-editor", "files": {path: "a" * 64}},
        },
    }
    with pytest.raises(ValueError):
        project(fixture(), inventory)


def test_rejects_workspace_shared_with_another_team():
    config = fixture()
    config["agents"]["entries"]["other-team"]["workspace"] = "~/spaces/agency-editor"
    inventory = {
        "schema": 1,
        "workspaces": {
            "agency-editor": {
                "root": "/home/test/spaces/agency-editor",
                "files": {"MEMORY.md": "a" * 64},
            },
        },
    }
    with pytest.raises(ValueError, match="superponen"):
        project(config, inventory)


def test_rejects_workspace_overlapping_an_agent_state_directory():
    config = fixture()
    config["agents"]["entries"]["other-team"]["agentDir"] = "~/spaces/agency-editor/other-state"
    inventory = {
        "schema": 1,
        "workspaces": {
            "agency-editor": {
                "root": "/home/test/spaces/agency-editor",
                "files": {"MEMORY.md": "a" * 64},
            },
        },
    }
    with pytest.raises(ValueError, match="directorio de estado"):
        project(config, inventory)


@pytest.mark.skipif(sys.platform != "linux", reason="O_NOFOLLOW y dir_fd de Linux")
def test_linux_publication_pinned_is_accepted_by_existing_capture_contract(tmp_path):
    config_dir = tmp_path / "source"
    config_dir.mkdir(mode=0o700)
    config = config_dir / "openclaw.json"
    config.write_bytes(staging.backup.canonical(fixture()))
    config.chmod(0o600)
    inventory = config_dir / "inventory.json"
    inventory.write_bytes(b'{"schema":1,"workspaces":{}}')
    inventory.chmod(0o600)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    base = [sys.executable, "-I", "-B", str(SCRIPT), "synthetic", str(config), str(inventory)]
    first = subprocess.run(base, capture_output=True, check=True, timeout=8)
    pin = json.loads(first.stdout)["manifest_sha256"]
    command = [*base, "--publish", str(private), "staging"]
    rejected = subprocess.run([*command, "0" * 64], capture_output=True, check=False, timeout=8)
    assert rejected.returncode == 1 and not (private / "staging").exists()
    subprocess.run([*command, pin], capture_output=True, check=True, timeout=8)
    manifest, files = staging.backup.read_staging(private, "staging", pin, "synthetic")
    assert staging.backup.digest(manifest) == pin and len(files) == 5
    assert b"CANARY" not in b"".join(files.values())
    assert not list(config_dir.glob("staging"))
    symlink = config_dir / "link"
    symlink.symlink_to(inventory)
    os.link(inventory, config_dir / "hardlink")
    with pytest.raises(OSError):
        staging.read_source(symlink, 1024)
    with pytest.raises(ValueError, match="no ordinario"):
        staging.read_source(inventory, 1024)
