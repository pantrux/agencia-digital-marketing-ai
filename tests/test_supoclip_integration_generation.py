"""Pruebas de aceptación local con respuestas HTTP y de transcripción de prueba."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docker/supoclip"
FIXTURE = ROOT / ".tools/backup-source-build/supoclip-integration-v1/synthetic-clips-speech-v2.mp4"
TASK_ID = "47726c9c-c627-433c-a50b-f131682216ff"
CLIP_ID = "e407f41f-0916-4fd3-9451-076ed119c420"
UPLOAD = "upload://ae36d10c-2e4c-40d3-a8f6-ae9c5600bc11.mp4"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HTTP = load("probe_integration_http")
MEDIA = load("probe_integration_media")


def mock_task_routes(monkeypatch, statuses, hook_type):
    paths = []

    def get_json(opener, path, expected, **kwargs):
        paths.append((path, expected, kwargs))
        if path == "/api/tasks/create":
            return {"task_id": TASK_ID, "job_id": f"{TASK_ID}:fixture"}
        if path.endswith("/clips"):
            return {
                "task_id": TASK_ID,
                "total_clips": 1,
                "clips": [
                    {
                        "id": CLIP_ID,
                        "hook_type": hook_type,
                        "start_time": "00:04",
                        "end_time": "00:24",
                        "text": "Grabación de prueba",
                    }
                ],
            }
        return {"id": TASK_ID, "status": next(statuses)}

    monkeypatch.setattr(HTTP, "get_json", get_json)
    monkeypatch.setattr(HTTP.time, "sleep", lambda _: None)
    return paths


def test_crea_tarea_autenticada_y_descarga_clip_elegido(monkeypatch, tmp_path):
    paths = mock_task_routes(monkeypatch, iter(["queued", "processing", "completed"]), "statement")
    anonymous, authenticated = object(), object()
    downloaded = b"0000ftypisom"

    def request(opener, route, expected, **kwargs):
        if opener is anonymous:
            assert expected == {401}
            return 401, {}, b""
        assert opener is authenticated
        assert route == f"/api/tasks/{TASK_ID}/clips/{CLIP_ID}/file"
        return 200, {"Content-Type": "video/mp4"}, downloaded

    monkeypatch.setattr(HTTP, "request", request)
    monkeypatch.setattr(HTTP, "inspect_clip", lambda _: {"duration_seconds": 20})
    output = tmp_path / "clip.mp4"
    result = HTTP.generate_clip(anonymous, authenticated, UPLOAD, output)
    assert output.read_bytes() == downloaded
    assert result["clip_id"] == CLIP_ID
    assert result["status"] == "completed"
    assert result["hook_type"] == "statement"
    path, _, args = paths[0]
    assert path == "/api/tasks/create"
    assert args["payload"]["source"]["url"] == UPLOAD
    assert args["payload"]["clip_length"] == "short"
    assert args["payload"]["add_subtitles"] is True
    assert args["payload"]["include_broll"] is False
    assert "Idempotency-Key" in args["headers"]


@pytest.mark.parametrize(
    ("status", "hook_type", "message"),
    [("error", "statement", "error"), ("completed", "fallback", "respaldo")],
)
def test_rechaza_tarea_fallida_y_clip_de_respaldo(
    monkeypatch, tmp_path, status, hook_type, message
):
    mock_task_routes(monkeypatch, iter([status]), hook_type)
    monkeypatch.setattr(HTTP, "request", lambda *a, **k: None)
    output = tmp_path / "clip.mp4"
    with pytest.raises(ValueError, match=message):
        HTTP.generate_clip(object(), object(), UPLOAD, output)
    assert not output.exists()


@pytest.mark.skipif(
    not (FIXTURE.is_file() and shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="Requiere fixture de generación local y FFmpeg",
)
def test_fixture_admite_extraccion_speaches_y_audio_mono(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SOURCE))
    assert MEDIA.CLIP_UPLOAD_BYTES == FIXTURE.stat().st_size
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == MEDIA.CLIP_UPLOAD_SHA
    assert (HTTP.CLIP_FIXTURE_BYTES, HTTP.CLIP_FIXTURE_SHA) == (
        MEDIA.CLIP_UPLOAD_BYTES,
        MEDIA.CLIP_UPLOAD_SHA,
    )
    audio = tmp_path / "audio.mp3"
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi,mpegts",
            "-i",
            str(FIXTURE),
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index:stream_tags=language:stream_disposition",
            "-of",
            "json",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    provider = load("speaches_provider")
    index = provider._select_stream(probe.stdout, "es")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi,mpegts",
            "-i",
            str(FIXTURE),
            "-map",
            f"0:a:{index}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(audio),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    assert 0 < audio.stat().st_size <= provider.MAX_AUDIO_BYTES
    encoded = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(audio),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    metadata = json.loads(encoded.stdout)
    assert metadata["streams"] == [{"codec_name": "mp3", "sample_rate": "16000", "channels": 1}]
    assert 31 <= float(metadata["format"]["duration"]) <= 31.15


def test_sonda_media_exige_espanol_y_tramo_hablado():
    words = [{"start": i * 500, "end": i * 500 + 250, "text": "prueba"} for i in range(58)]
    cache = {"version": 2, "language_code": "es", "words": words, "utterances": [words]}
    assert MEDIA.transcript_summary(json.dumps(cache).encode(), clip_fixture=True)["words"] == 58
    cache["words"] = [
        {**word, "start": i * 100, "end": i * 100 + 50} for i, word in enumerate(words)
    ]
    with pytest.raises(ValueError, match="TRANSCRIPT_SPEECH_SPAN"):
        MEDIA.transcript_summary(json.dumps(cache).encode(), clip_fixture=True)


@pytest.mark.skipif(
    not (FIXTURE.is_file() and shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="Requiere fixture de generación local y FFmpeg",
)
def test_decodifica_clip_vertical_real_y_rechaza_mp4_truncado(tmp_path):
    clip = tmp_path / "vertical.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-i",
            str(FIXTURE),
            "-t",
            "16",
            "-vf",
            "scale=180:320,setsar=1",
            "-c:v",
            "libx264",
            "-threads:v",
            "1",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(clip),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    info = HTTP.inspect_clip(clip)
    assert (info["width"], info["height"], info["duration_seconds"]) == (180, 320, 16)
    data = clip.read_bytes()
    clip.write_bytes(data[: len(data) // 2])
    with pytest.raises(ValueError, match="decodifica|ffprobe"):
        HTTP.inspect_clip(clip)
