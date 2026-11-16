import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "openclaw-agency-setup/mcp_servers/immich_broll.py"
CLIP = "12345678-abcd-4321-8901-abcdef012345"
LIBRARY = "9abcdef0-abcd-4321-8901-abcdef012345"


class FixtureError(Exception):
    def __init__(self, status):
        self.status = status


@pytest.fixture
def adapter(monkeypatch):
    def fake_http(*args, **kwargs):
        pytest.fail("La prueba no configuró una respuesta HTTP")

    common = ModuleType("common")
    common.UpstreamError = FixtureError
    common.request_json = fake_http
    common.read_secret = lambda name: f"mock-{name}"
    monkeypatch.setitem(sys.modules, "common", common)
    spec = importlib.util.spec_from_file_location("phase5_immich_broll", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("IMMICH_INTERNAL_URL", "http://192.168.0.85:2283/")
    monkeypatch.setenv("IMMICH_BROLL_LIBRARY_ID", LIBRARY)
    return module


def test_search_is_scoped_and_projects_only_opaque_references(adapter, monkeypatch):
    def respond(host, port, path, **request):
        assert (host, port, path) == ("192.168.0.85", 2283, "/api/search/smart")
        assert request["body"] == {"query": "bicicleta al sol", "size": 2, "libraryId": LIBRARY}
        assert request["header"] == ("x-api-key", "mock-IMMICH_API_KEY_MCP_FILE")
        assert request["timeout"] > 0 and request["limit"] <= 1024**2
        return {
            "assets": {
                "count": 1,
                "items": [
                    {
                        "id": CLIP,
                        "libraryId": LIBRARY,
                        "ownerId": "privado",
                        "people": ["dato"],
                        "originalPath": "/cliente/original",
                        "description": "cliente",
                    }
                ],
            },
            "albums": {"items": [{"id": "privado"}]},
        }

    monkeypatch.setattr(adapter, "request_json", respond)
    assert adapter.search({"query": " bicicleta al sol ", "size": 2}) == {
        "matches": [{"asset_id": CLIP, "rank": 1, "reference": f"immich://asset/{CLIP}"}]
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "IMMICH_UNAUTHORIZED"), (403, "IMMICH_FORBIDDEN"), (307, "IMMICH_UPSTREAM")],
)
def test_http_errors_never_relay_remote_text(adapter, monkeypatch, status, code):
    def respond(*args, **kwargs):
        raise FixtureError(status)

    monkeypatch.setattr(adapter, "request_json", respond)
    with pytest.raises(ValueError, match=f"^{code}$"):
        adapter.search({"query": "escena de prueba"})


@pytest.mark.parametrize(
    "bad",
    [
        {"query": "  "},
        {"query": "x", "size": True},
        {"query": "x", "size": 21},
        {"query": "x", "libraryId": LIBRARY},
        {"query": "x" * 513},
        {"query": "a\nb"},
    ],
)
def test_rejects_out_of_contract_requests(adapter, bad):
    with pytest.raises(ValueError, match="^IMMICH_ARGUMENTS$"):
        adapter.search(bad)


def test_rejects_an_origin_or_library_outside_scope(adapter, monkeypatch):
    monkeypatch.setenv("IMMICH_INTERNAL_URL", "http://192.168.0.85:2283@192.168.0.6:8090")
    with pytest.raises(ValueError, match="^IMMICH_ORIGIN$"):
        adapter.search({"query": "atardecer"})
    monkeypatch.setenv("IMMICH_INTERNAL_URL", "http://192.168.0.85:2283")
    monkeypatch.setenv("IMMICH_BROLL_LIBRARY_ID", "todas")
    with pytest.raises(ValueError, match="^IMMICH_LIBRARY$"):
        adapter.search({"query": "atardecer"})


def test_rejects_upstream_asset_outside_library(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter,
        "request_json",
        lambda *a, **k: {
            "assets": {"count": 1, "items": [{"id": CLIP, "libraryId": None}]},
        },
    )
    with pytest.raises(ValueError, match="^IMMICH_RESPONSE$"):
        adapter.search({"query": "atardecer"})
