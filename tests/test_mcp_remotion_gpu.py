import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "openclaw-agency-setup/mcp_servers/remotion_gpu.py"
SHA256 = "0123456789abcdef" * 4


class FixtureError(Exception):
    def __init__(self, status):
        self.status = status


@pytest.fixture
def adapter(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Una solicitud no válida llegó a la red o a la credencial")

    def authorize_project(project):
        if project != "fixture":
            raise ValueError("PROYECTO_NO_AUTORIZADO")

    common = ModuleType("common")
    common.PROJECT = re.compile(r"[a-z][a-z0-9_-]{0,47}")
    common.JOB = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
    common.UpstreamError = FixtureError
    common.request_json = unexpected
    common.read_secret = unexpected
    common.authorize_project = authorize_project
    monkeypatch.setitem(sys.modules, "common", common)
    spec = importlib.util.spec_from_file_location("phase5_remotion_gpu", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def arguments():
    return {
        "project_id": "fixture",
        "job_id": "test-1",
        "composition": "AgencyVertical",
        "plan": "Free",
        "props": {"title": "Prueba", "duration_seconds": 0.1},
    }


def artifact(path):
    return {"path": path, "bytes": 123, "sha256": SHA256}


def fake_bridge(adapter, monkeypatch, route, response, inspect_body=lambda _: None):
    def read_secret(name):
        assert name == "GPU_BRIDGE_TOKEN_FILE"
        return "opaque-fixture"

    def respond(host, port, path, **request):
        assert (host, port, path) == ("192.168.0.6", 8090, route)
        assert request["header"] == ("Authorization", "Bearer opaque-fixture")
        assert 0 < request["timeout"] <= 1205 and 0 < request["limit"] <= 64 * 1024
        inspect_body(request["body"])
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(adapter, "read_secret", read_secret)
    monkeypatch.setattr(adapter, "request_json", respond)


def stills_response():
    return {
        "files": [artifact("proyectos/fixture/05_revision/test-1/stills/still-2.png")],
        "frames": [2],
        "width": 1080,
        "height": 1920,
        "resolution": "1080p",
        "transparent": False,
        "alpha_verified": False,
        "duration_seconds": 0.1,
        "watermark": True,
    }


def test_stills_uses_nas_scope_and_projects_only_known_fields(adapter, arguments, monkeypatch):
    expected = stills_response()
    response = {**expected, "runtime_path": "P:/private/example"}
    response["files"] = [{**expected["files"][0], "private_path": "P:/hidden"}]

    def inspect(body):
        assert body == {
            **arguments,
            "frames": [2],
            "format": "mp4",
            "resolution": "1080p",
            "props": {
                **arguments["props"],
                "subtitle": "",
                "background": "#102032",
                "accent": "#8fe3c5",
            },
        }

    fake_bridge(adapter, monkeypatch, "/stills", response, inspect)
    assert adapter.stills(arguments) == expected


@pytest.mark.parametrize("alpha", [False, True])
def test_render_validates_codec_audio_and_projects_safe_fields(
    adapter, arguments, monkeypatch, alpha
):
    arguments["plan"] = "Ilimitado" if alpha else "Pro"
    arguments["format"] = "prores4444" if alpha else "mp4"
    arguments["props"]["audio_path"] = "proyectos/fixture/03_assets/prueba.wav"
    arguments["props"]["video_path"] = "_biblioteca_global/compartido.mp4"
    extension = "mov" if alpha else "mp4"
    response = {
        "files": [
            artifact(f"proyectos/fixture/06_renders/test-1/render/master-corrected.{extension}")
        ],
        "codec": "prores_ks" if alpha else "h264_nvenc",
        "hardware_acceleration": not alpha,
        "resolution": "1080p",
        "watermark": False,
        "audio_verified": True,
        "alpha_verified": alpha,
        "audio": {"integrated_lufs": -14.1, "true_peak_dbtp": -1.1, "not_exported": "/private"},
        "media": {
            "duration_seconds": 0.1,
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "video_codec": "prores" if alpha else "h264",
            "video_profile": "4444" if alpha else "High",
            "codec_tag": "ap4h" if alpha else "avc1",
            "audio_codec": "pcm_s24le" if alpha else "aac",
            "has_audio": True,
            "sample_rate": 48000,
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
            "pixel_format": "yuva444p10le" if alpha else "yuv420p",
            "not_exported": "/private",
        },
    }

    def inspect(body):
        assert "frames" not in body
        assert body["props"]["audio_path"] == arguments["props"]["audio_path"]
        assert body["props"]["video_path"] == arguments["props"]["video_path"]

    fake_bridge(adapter, monkeypatch, "/render", response, inspect)
    expected = {**response, "audio": dict(response["audio"]), "media": dict(response["media"])}
    del expected["audio"]["not_exported"]
    del expected["media"]["not_exported"]
    del expected["media"]["video_profile"]
    del expected["media"]["codec_tag"]
    assert adapter.render(arguments) == expected


@pytest.mark.parametrize(
    "change",
    [
        {"resolution": "4k"},
        {"format": "prores4444"},
        {"frames": [3]},
        {"frames": [0, 0]},
        {"frames": [True]},
        {"frames": None},
        {"job_id": "../../test"},
        {"composition": "Different"},
        {"hardware_acceleration": True},
        {"plan": ["Free"]},
    ],
)
def test_rejects_bad_plan_dimensions_frames_or_keys_before_io(adapter, arguments, change):
    with pytest.raises(ValueError, match="^REMOTION_ARGUMENTS$"):
        adapter.stills({**arguments, **change})


@pytest.mark.parametrize(
    "path",
    [
        "proyectos/otro/03_assets/f.wav",
        "proyectos/fixture/03_assets/../f.wav",
        "proyectos/fixture/05_revision/f.wav",
        "P:/fixture/f.wav",
        "_biblioteca_global/aux.wav",
        "_biblioteca_global/dir./fixture.wav",
        "_biblioteca_global/fixture\x00.wav",
        "_biblioteca_global/fixture.exe",
    ],
)
def test_rejects_nas_paths_outside_approved_areas(adapter, arguments, path):
    arguments["props"]["audio_path"] = path
    with pytest.raises(ValueError, match="^REMOTION_ARGUMENTS$"):
        adapter.render(arguments)


def test_rejects_unapproved_project_and_missing_render_audio(adapter, arguments):
    with pytest.raises(ValueError, match="^PROYECTO_NO_AUTORIZADO$"):
        adapter.stills({**arguments, "project_id": "otro"})
    with pytest.raises(ValueError, match="^REMOTION_ARGUMENTS$"):
        adapter.render(arguments)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "REMOTION_UNAUTHORIZED"),
        (403, "REMOTION_FORBIDDEN"),
        (307, "REMOTION_UPSTREAM"),
        (429, "REMOTION_BUSY"),
    ],
)
def test_maps_http_failures_to_safe_codes(adapter, arguments, monkeypatch, status, code):
    fake_bridge(adapter, monkeypatch, "/stills", FixtureError(status))
    with pytest.raises(ValueError, match=f"^{code}$"):
        adapter.stills(arguments)


@pytest.mark.parametrize(
    "bad",
    [
        {"path": "proyectos/otro/05_revision/test-1/stills/still-2.png"},
        {"path": "proyectos/fixture/05_revision/test-1/stills/still-1.png"},
        {"sha256": "invalid"},
        {"bytes": True},
    ],
)
def test_refuses_wrong_artifact_scope_or_metadata(adapter, arguments, monkeypatch, bad):
    response = stills_response()
    response["files"][0].update(bad)
    fake_bridge(adapter, monkeypatch, "/stills", response)
    with pytest.raises(ValueError, match="^REMOTION_RESPONSE$"):
        adapter.stills(arguments)
