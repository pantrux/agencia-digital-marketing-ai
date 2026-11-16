"""Ejecuta la ruta real de subtítulos y comprueba la descarga autenticada."""

import hashlib
import json
import os
import re
import signal
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

WAV_SHA = "00bf9721fcfbe2dfd69780d912048c969098d693c8ee25216a8b4f49da94fb99"


def fetch(url, key=None):
    headers = {} if key is None else {"x-api-key": key}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=4) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as error:
        return error.code, b""


def probe(task_id):
    assert os.geteuid() == os.getegid() == 33
    from app.config import config
    from app.services import subtitle, task

    assert config.app["subtitle_provider"] == "speaches"
    wav = Path("/run/mpt/fixtures/whisper-probe.wav")
    with wav.open("rb") as stream:
        raw = stream.read(493625)
    assert len(raw) == 493624 and hashlib.sha256(raw).hexdigest() == WAV_SHA
    task_root = Path("/run/mpt/storage/tasks")
    assert re.fullmatch(r"speaches-probe-v[1-9][0-9]*", task_id)
    folder = task_root / task_id
    folder.mkdir(mode=0o700)
    params = SimpleNamespace(subtitle_enabled=True, subtitle_display_mode="word_by_word")
    started = time.monotonic()
    output = Path(task.generate_subtitle(folder.name, params, "", None, str(wav)))
    elapsed = round(time.monotonic() - started, 3)
    assert output == folder / "subtitle.srt"
    cues = subtitle.file_to_subtitles(str(output))
    assert len(cues) == 28
    srt = output.read_bytes()
    url = f"http://127.0.0.1:8080/tasks/{folder.name}/subtitle.srt"
    anonymous, _ = fetch(url)
    authenticated, response = fetch(url, config.app["api_key"])
    assert anonymous in {401, 403} and authenticated == 200 and response == srt
    return {
        "success": True,
        "word_cues": len(cues),
        "srt_sha256": hashlib.sha256(srt).hexdigest(),
        "srt_bytes": len(srt),
        "path": str(output),
        "anonymous": anonymous,
        "authenticated": authenticated,
        "transcription_seconds": elapsed,
    }


if __name__ == "__main__":
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.alarm(90)
    try:
        result = probe(sys.argv[1] if len(sys.argv) == 2 else "speaches-probe-v1")
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        result = {
            "success": False,
            "error": type(error).__name__,
            "module": Path(frame.filename).name,
            "line": frame.lineno,
        }
        code = getattr(error, "code", None)
        if (
            isinstance(code, str)
            and code.startswith(("MPT_", "SPEACHES_"))
            and code.replace("_", "").isalpha()
        ):
            result["error_code"] = code
    print(json.dumps({"agency_probe": result}, sort_keys=True))
    raise SystemExit(0 if result["success"] else 1)
