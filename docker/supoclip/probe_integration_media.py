"""Comprueba la carga física y Speaches desde el worker del ensayo SupoClip."""

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

TEMP = Path("/state/temp")
UPLOAD_BYTES = 89894
UPLOAD_SHA = "499d9bde244c4f294e103634da2cb42e5c60090257eab755d68af916483e5f00"
CLIP_UPLOAD_BYTES = 694534
CLIP_UPLOAD_SHA = "b0ec158e3d73c41d9ba4c2cf070a8400382b5f7b50ab7fc0f6722c18eed40606"
TOKEN_FILE = "/run/secrets/supoclip-speaches"


def require(condition, code):
    if not condition:
        raise ValueError(code)


def read_owned(path, limit):
    require(path.resolve(strict=True) == path, "FILE_PATH")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid == info.st_gid == 1000
            and 0 < info.st_size <= limit,
            "FILE_IDENTITY",
        )
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        require(
            len(data) == info.st_size
            and (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
            "FILE_CHANGED",
        )
        return data


def transcript_summary(raw, *, clip_fixture=False):
    cache = json.loads(raw)
    require(cache["version"] == 2 and cache["language_code"] == "es", "TRANSCRIPT_SCHEMA")
    words = cache["words"]
    expected_words, max_segments, max_ms = (45, 16, 32000) if clip_fixture else (28, 8, 11250)
    require(
        expected_words <= len(words) <= (120 if clip_fixture else expected_words)
        and 1 <= len(cache["utterances"]) <= max_segments,
        "TRANSCRIPT_COUNTS",
    )
    last = 0
    for word in words:
        start, end = word["start"], word["end"]
        require(
            type(start) is int
            and type(end) is int
            and last <= start <= end <= max_ms
            and isinstance(word["text"], str)
            and bool(word["text"].strip()),
            "TRANSCRIPT_WORD_TIME",
        )
        last = start
    if clip_fixture:
        require(words[-1]["end"] - words[0]["start"] >= 20000, "TRANSCRIPT_SPEECH_SPAN")
    return {
        "words": len(words),
        "segments": len(cache["utterances"]),
        "language": "es",
        "first_word_ms": words[0]["start"],
        "last_word_ms": words[-1]["end"],
        "cache_sha256": hashlib.sha256(raw).hexdigest(),
    }


def main():
    require(
        (len(sys.argv) == 2 or (len(sys.argv) == 3 and sys.argv[2] == "--clip-fixture"))
        and os.getuid() == os.getgid() == 1000,
        "WORKER_IDENTITY",
    )
    clip_fixture = len(sys.argv) == 3
    expected_bytes = CLIP_UPLOAD_BYTES if clip_fixture else UPLOAD_BYTES
    expected_sha = CLIP_UPLOAD_SHA if clip_fixture else UPLOAD_SHA
    leaf = sys.argv[1]
    require(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.mp4", leaf
        ),
        "UPLOAD_NAME",
    )
    source = TEMP / "uploads" / leaf
    data = read_owned(source, expected_bytes)
    require(
        len(data) == expected_bytes and hashlib.sha256(data).hexdigest() == expected_sha,
        "UPLOAD_BYTES",
    )
    require(
        TEMP.resolve(strict=True) == TEMP
        and stat.S_IMODE(TEMP.lstat().st_mode) == 0o700
        and TEMP.lstat().st_uid == TEMP.lstat().st_gid == 1000,
        "TEMP_IDENTITY",
    )
    os.environ.clear()
    os.environ.update(
        PATH="/usr/bin:/bin",
        LC_ALL="C.UTF-8",
        PYTHON_DOTENV_DISABLED="1",
        TRANSCRIPTION_PROVIDER="speaches",
        SPEACHES_BEARER_TOKEN_FILE=TOKEN_FILE,
        SELF_HOST="true",
        MEDIA_STORAGE="local",
        OUTPUT_DIR="/state/outputs",
        TEMP_DIR=str(TEMP),
    )
    sys.path[:0] = ["/opt/supoclip/backend", "/opt/supoclip/site"]
    from src.media.transcription import get_video_transcript

    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="supoclip-integration-", dir=TEMP) as work:
        video = Path(work) / "speech.mp4"
        with video.open("xb") as stream:
            stream.write(data)
        result = get_video_transcript(video)
        require(isinstance(result, str) and bool(result.strip()), "TRANSCRIPT_EMPTY")
        proof = transcript_summary(
            read_owned(video.with_suffix(".transcript_cache.json"), 1024 * 1024),
            clip_fixture=clip_fixture,
        )
    require(
        not Path(work).exists() and read_owned(source, expected_bytes) == data, "FIXTURE_CLEANUP"
    )
    print(
        json.dumps(
            {
                "success": True,
                "provider": "speaches",
                "upload_sha256": expected_sha,
                "upload_bytes": expected_bytes,
                "seconds": round(time.monotonic() - began, 3),
                **proof,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        detail = str(error)
        code = detail if re.fullmatch(r"[A-Z_]{1,65}", detail) else type(error).__name__
        print(json.dumps({"success": False, "error": code}, sort_keys=True))
        raise SystemExit(1) from None
