import hashlib
import importlib.util
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "openclaw-agency-setup/upstream-skills"
SPEC = importlib.util.spec_from_file_location("agency_upstream_skills", BASE / "review_skills.py")
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)

COMMIT = "a" * 40
FILE = "skills/demo/SKILL.md"
CONTENT = b"---\nname: demo\n---\nEjemplo de datos.\n"
REPOSITORY = "Example/skills"
RAW_URL = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{FILE}"
ZIP_URL = f"https://codeload.github.com/{REPOSITORY}/zip/{COMMIT}"


def manifest():
    return {
        "schema": 1,
        "sources": [
            {
                "id": "example",
                "requested": "Example/skills",
                "repository": REPOSITORY,
                "commit": COMMIT,
                "license_evidence": "fixture",
                "skills": [FILE],
                "files": {
                    FILE: {"size": len(CONTENT), "sha256": hashlib.sha256(CONTENT).hexdigest()}
                },
            }
        ],
    }


def archive(name, mode, content):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        entry = zipfile.ZipInfo(f"skills-{COMMIT}/{name}")
        entry.create_system = 3
        entry.external_attr = mode << 16
        bundle.writestr(entry, content)
    return data.getvalue()


def test_raw_materializes_only_the_exact_commit_and_file(tmp_path):
    def fetch(url, maximum):
        assert (url, maximum) == (RAW_URL, len(CONTENT))
        return CONTENT

    output = tmp_path / "review"
    result = review.stage(manifest(), output, transport="raw", fetch=fetch)
    assert (output / "example" / FILE).read_bytes() == CONTENT
    assert json.loads((output / "receipt.json").read_text(encoding="utf-8")) == result
    assert result["files"] == 1 and result["skills"] == 1
    assert result["sources"]["example"] == COMMIT


def test_zip_accepts_a_regular_entry_and_preserves_exact_bytes(tmp_path):
    def fetch(url, maximum):
        assert url == ZIP_URL and maximum <= 16 * 1024**2
        return archive(FILE, stat.S_IFREG | 0o755, CONTENT)

    output = tmp_path / "review"
    review.stage(manifest(), output, transport="zip", fetch=fetch)
    assert (output / "example" / FILE).read_bytes() == CONTENT
    if os.name != "nt":
        assert (output / "example" / FILE).stat().st_mode & 0o111 == 0


@pytest.mark.parametrize(
    ("transport", "payload"),
    [("raw", b"x" * len(CONTENT)), ("zip", archive(FILE, stat.S_IFLNK | 0o777, CONTENT))],
)
def test_there_is_no_output_if_hash_or_archive_type_fails(tmp_path, transport, payload):
    output = tmp_path / "review"
    with pytest.raises(ValueError):
        review.stage(manifest(), output, transport=transport, fetch=lambda *_: payload)
    assert not output.exists()


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "skills\\demo\\note.md",
        "aux.txt",
        "a/%2e%2e/b.md",
        "a/file:stream.md",
        "/absolute.md",
        "a/./reference.md",
        "SKILLS/DEMO/skill.md",
        "SKILLS/DEMO/reference.md",
    ],
)
def test_unsafe_or_colliding_paths_rejected_before_network(tmp_path, path):
    pins = manifest()
    pins["sources"][0]["files"][path] = pins["sources"][0]["files"][FILE]

    def unexpected(*_):
        pytest.fail("Manifiesto inválido hizo una consulta")

    with pytest.raises(ValueError):
        review.stage(pins, tmp_path / "review", transport="raw", fetch=unexpected)


def test_does_not_reuse_a_destination_or_a_linked_parent(tmp_path):
    real = tmp_path / "target"
    real.mkdir()
    with pytest.raises(ValueError):
        review.stage(manifest(), real, transport="raw", fetch=lambda *_: pytest.fail())
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("El entorno de pruebas no permite crear symlinks")
    with pytest.raises(ValueError):
        review.stage(manifest(), linked / "review", transport="raw", fetch=lambda *_: pytest.fail())
    assert list(real.iterdir()) == []


def test_redirect_never_substitutes_source():
    handler = review.RejectRedirect()
    assert handler.redirect_request(None, None, 302, "", None, "https://ejemplo.invalid") is None


def test_frozen_manifest_matches_the_phase_five_skill_catalogue():
    value = json.loads((BASE / "manifest.json").read_text(encoding="utf-8"))
    sources = review.validate(value)
    names = {Path(entry).parent.name for source in sources for entry in source["skills"]}
    assert len(sources) == 6 and names == set(
        "ig-audit ig-caption ig-carousel ig-dm ig-human ig-plan ig-reel ig-story ig-viral "
        "ab-testing ad-creative copywriting cro marketing-psychology onboarding product-marketing "
        "social video meta-ads remotion-best-practices ai-image-generation ai-video-generation".split()
    )
    assert sum("LICENSE" in source["files"] for source in sources) == 4
