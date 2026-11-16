import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "openclaw-agency-setup"
SPEC = importlib.util.spec_from_file_location("agency_stage_skills", APP / "stage_skills.py")
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)


def pin(raw):
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def test_projecting_skills_preserves_shared_ad_creative_reference(monkeypatch, tmp_path):
    pins = {
        "schema": 1,
        "sources": [{"id": "marketing", "skills": ["skills/ad-creative/SKILL.md"], "files": {}}],
    }
    marketing = pins["sources"][0]["files"]
    files = {
        "skills/ad-creative/SKILL.md": b"skill",
        "skills/ad-creative/references/guide.md": b"guide",
        "skills/ads/references/meta-decision-system.md": b"meta",
    }
    for path, raw in files.items():
        target = tmp_path / "marketing" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        marketing[path] = pin(raw)
    monkeypatch.setattr(stage, "ROSTER", {"agency-strategist": (None, None, ("ad-creative",))})
    result = stage.project_public(tmp_path, pins)
    assert result["ad-creative"] == {
        "ad-creative/" + path: raw
        for path, raw in {"SKILL.md": b"skill", "references/guide.md": b"guide"}.items()
    } | {"ads/references/meta-decision-system.md": b"meta"}
    (tmp_path / "marketing" / "skills" / "ad-creative" / "SKILL.md").write_bytes(b"new")
    with pytest.raises(ValueError, match="HUELLA"):
        stage.project_public(tmp_path, pins)


def test_selecting_workspaces_rejects_any_aliased_foreign_agent(tmp_path):
    paths = {agent: tmp_path / f"workspace-{agent}" for agent in stage.ROSTER}
    entries = {agent: {"workspace": str(path)} for agent, path in paths.items()}
    cfg = {"agents": {"entries": entries}}
    assert stage.workspaces(cfg, tmp_path) == paths
    entries["someone-else"] = {"workspace": str(paths["agency-account"] / "shared")}
    with pytest.raises(ValueError, match="SOLAPADO"):
        stage.workspaces(cfg, tmp_path)


def test_exact_files_reused_and_other_files_preserved(tmp_path):
    owned = tmp_path / "workspace-agency-account"
    own_path = owned / "skills/demo/SKILL.md"
    existing = owned / "skills/another/SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"personal")
    files = {own_path: b"official"}
    assert stage.publish(files, apply=False) == {"new": 1, "identical": 0}
    assert not own_path.exists()
    assert stage.publish(files, apply=True) == {"new": 1, "identical": 0}
    assert stage.publish(files, apply=True) == {"new": 0, "identical": 1}
    assert existing.read_bytes() == b"personal"
    other_owned = owned / "skills/next/SKILL.md"
    with pytest.raises(ValueError, match="CONFLICTO"):
        stage.publish({other_owned: b"new", own_path: b"different"}, apply=True)
    assert not other_owned.exists() and own_path.read_bytes() == b"official"


def test_refuses_a_symlink_at_skill_destination(tmp_path):
    linked = tmp_path / "skills"
    original = tmp_path / "another"
    original.mkdir()
    try:
        linked.symlink_to(original, target_is_directory=True)
    except OSError:
        pytest.skip("El entorno de pruebas no permite crear symlinks")
    with pytest.raises(ValueError):
        stage.publish({linked / "demo/SKILL.md": b"own"}, apply=True)
    assert list(original.iterdir()) == []


def test_rejects_existing_file_with_an_external_hardlink(tmp_path):
    import os

    outside = tmp_path / "personal.md"
    outside.write_bytes(b"same")
    own = tmp_path / "SKILL.md"
    try:
        os.link(outside, own)
    except OSError:
        pytest.skip("El sistema de archivos no permite enlaces duros")
    with pytest.raises(ValueError, match="CONFLICTO"):
        stage.publish({own: b"same"}, apply=True)
    assert outside.read_bytes() == b"same"


def test_patch_compare_rejects_unrelated_changes_while_accepting_cli_metadata(tmp_path):
    before = {
        "agents": {"entries": {"foreign": {"name": "unchanged"}}},
        "mcp": {"servers": {"existing": {"command": "x"}}},
    }
    patch = {"agents": {"entries": {"agency-account": {"skills": ["fixture"]}}}}
    after = {
        "agents": {"entries": before["agents"]["entries"] | patch["agents"]["entries"]},
        "mcp": before["mcp"],
        "meta": {"lastTouchedAt": "now", "lastTouchedVersion": "version"},
    }
    paths = [tmp_path / f"{kind}.json" for kind in ("before", "after", "patch")]
    for target, data in zip(paths, (before, after, patch), strict=True):
        target.write_text(json.dumps(data), encoding="utf-8")
    stage.check_config(*paths)
    after["mcp"] = {}
    paths[1].write_text(json.dumps(after), encoding="utf-8")
    with pytest.raises(ValueError, match="DIFERENCIA"):
        stage.check_config(*paths)


def test_rejects_a_patch_that_reorders_foreign_plugins():
    original = {"plugins": {"load": {"paths": ["/plugin/one", "/plugin/two"]}}}
    changed = {"plugins": {"load": {"paths": ["/plugin/two", "/plugin/one", "/plugin/agency"]}}}
    with pytest.raises(ValueError, match="FUERA_DE_ALCANCE"):
        stage._checked_patch(original, changed)
