"""Pruebas de rutas e inspección con un puente GPU simulado."""

import importlib.util
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER = Path(__file__).resolve().parents[1] / "openclaw-agency-setup" / "mcp_servers"


@pytest.fixture
def inspector(monkeypatch):
    monkeypatch.syspath_prepend(str(SERVER))
    spec = importlib.util.spec_from_file_location(
        "agency_inspector_test", SERVER / "media_inspector.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_result(inspector):
    data = {key: None for key in inspector.MEDIA_FIELDS}
    data.update(
        {"duration_seconds": 2.0, "width": 1080, "height": 1920, "fps": 30, "has_audio": True}
    )
    ruler = {
        "y_min": 0,
        "y_max": 1920,
        "canvas_width": 1080,
        "canvas_height": 1920,
        "original_width": 1080,
        "original_height": 1920,
        "source_scale": 1.0,
        "source_x": 0,
        "source_y": 0,
    }
    prefix = "proyectos/prueba/05_revision/ensayo/inspect/"
    return {
        "media": data,
        "ruler": ruler,
        "audio": {"integrated_lufs": -14.2, "true_peak_dbtp": -1.2},
        "samples_seconds": [1.0],
        "files": [
            {"path": prefix + name, "bytes": 170, "sha256": "a" * 64}
            for name in ("inspect-0.png", "contact-sheet.svg")
        ],
    }


def test_accepts_exact_artifacts_and_ruler(inspector, monkeypatch):
    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "prueba")
    monkeypatch.setattr(inspector, "read_secret", lambda key: "simulado")
    result = fixture_result(inspector)
    seen = []

    def request(host, port, route, **kwargs):
        seen.append((host, port, route, kwargs["body"]))
        return result

    monkeypatch.setattr(inspector, "request_json", request)
    returned = inspector.inspect_media("prueba", "ensayo", "proyectos/prueba/01_raw/test.mp4", 1)
    assert returned == result
    assert seen == [
        (
            "192.168.0.6",
            8090,
            "/inspect",
            {
                "project_id": "prueba",
                "job_id": "ensayo",
                "input_path": "proyectos/prueba/01_raw/test.mp4",
                "samples": 1,
            },
        )
    ]


@pytest.mark.parametrize(
    "path",
    [
        "proyectos/ajeno/01_raw/test.mp4",
        "proyectos/prueba/01_raw/../c.mp4",
        "proyectos/prueba/01_raw/CON.mp4",
        "https://ejemplo.test/test.mp4",
    ],
)
def test_rejects_unowned_or_invalid_path_before_secret(inspector, monkeypatch, path):
    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "prueba")
    monkeypatch.setattr(inspector, "read_secret", lambda key: pytest.fail("Credencial innecesaria"))
    with pytest.raises(ValueError, match="RUTA_MEDIO_INVALIDA"):
        inspector.inspect_media("prueba", "ensayo", path, 1)


def test_rejects_unexpected_file_in_response(inspector, monkeypatch):
    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "prueba")
    monkeypatch.setattr(inspector, "read_secret", lambda key: "simulado")
    result = fixture_result(inspector)
    result["files"][1]["path"] = "proyectos/ajeno/05_revision/ensayo/inspect/contact-sheet.svg"
    monkeypatch.setattr(inspector, "request_json", lambda *args, **kwargs: result)
    with pytest.raises(ValueError, match="INSPECCION_RESPUESTA_INVALIDA"):
        inspector.inspect_media("prueba", "ensayo", "proyectos/prueba/01_raw/test.mp4", 1)


def test_transcribe_audio_largo_con_marcas_absolutas(inspector, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "prueba")
    monkeypatch.setenv("GPU_BRIDGE_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setattr(inspector, "NAS", tmp_path)
    relative = "proyectos/prueba/01_raw/audio.wav"
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.touch()
    seen = []

    def prepare(_source, target):
        with wave.open(str(target), "wb") as track:
            track.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            track.writeframes(b"\x00\x00" * 16000 * 241)

    def send(path, token, *, language, **_limits):
        with wave.open(str(path), "rb") as track:
            duration = track.getnframes() / track.getframerate()
        seen.append((duration, language))
        return {
            "language": "es",
            "duration": duration,
            "text": "Parte.",
            "words": [{"word": "Parte.", "start": 0.1, "end": 0.3}],
        }

    monkeypatch.setattr(inspector, "audio_duration", lambda source: 241)
    monkeypatch.setattr(inspector, "prepare_audio", prepare)
    monkeypatch.setitem(sys.modules, "speaches_client", SimpleNamespace(transcribe_audio=send))
    monkeypatch.setitem(
        sys.modules, "speaches_contract", SimpleNamespace(SpeachesError=RuntimeError)
    )
    result = inspector.transcribe("prueba", relative, "es")
    assert seen == [(120, "es"), (120, "es"), (1, "es")]
    assert result["duration"] == 241
    assert result["text"] == "Parte. Parte. Parte."
    assert [word["start"] for word in result["words"]] == [0.1, 120.1, 240.1]
