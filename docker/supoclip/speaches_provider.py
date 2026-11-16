"""Proveedor Speaches exclusivo; extracción CPU acotada, sin fallback/modelos.

La selección de pista conserva la precedencia y alias de SupoClip f6cb7e2,
backend/src/media/ffmpeg.py (AGPL-3.0, copyright 2025 Sami Hindi).
La licencia y pins se conservan en source-patches.json y en el overlay generado.
"""

from __future__ import annotations

import json
import os
import select
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from speaches_client import _file, transcribe_audio
from speaches_contract import MAX_AUDIO_BYTES, SpeachesError

TOTAL_SECONDS = 300
OUTPUT_LIMIT = 64 * 1024
_BLOCKED = []
_BLOCKED_LOCK = threading.Lock()
_ALIASES = {
    "ar": {"ara"},
    "bg": {"bul"},
    "ca": {"cat"},
    "cs": {"ces", "cze"},
    "da": {"dan"},
    "de": {"deu", "ger"},
    "el": {"ell", "gre"},
    "en": {"eng"},
    "es": {"spa"},
    "fa": {"fas", "per"},
    "fi": {"fin"},
    "fr": {"fra", "fre"},
    "he": {"heb"},
    "hi": {"hin"},
    "hr": {"hrv"},
    "hu": {"hun"},
    "id": {"ind"},
    "it": {"ita"},
    "ja": {"jpn"},
    "ko": {"kor"},
    "ms": {"msa", "may"},
    "nl": {"nld", "dut"},
    "no": {"nor", "nob", "nno"},
    "pl": {"pol"},
    "pt": {"por"},
    "ro": {"ron", "rum"},
    "ru": {"rus"},
    "sk": {"slk", "slo"},
    "sl": {"slv"},
    "sv": {"swe"},
    "th": {"tha"},
    "tr": {"tur"},
    "uk": {"ukr"},
    "ur": {"urd"},
    "vi": {"vie"},
    "zh": {"zho", "chi"},
}

# El hijo espera autorización por stdin antes de exec. Si pidfd_open falla,
# cerrar stdin basta para terminar este hijo fijo sin enviar señales por PID.
_GATE = """import json,os,resource,sys
raw=sys.stdin.buffer.readline(8193)
if not raw or len(raw)>8192 or not raw.endswith(b'\\n'):sys.exit(2)
argv=json.loads(raw)
if type(argv)is not list or not argv or argv[0]not in('/usr/bin/ffmpeg','/usr/bin/ffprobe'):sys.exit(2)
if any(type(v)is not str or '\\x00'in v for v in argv):sys.exit(2)
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
soft,hard=resource.getrlimit(resource.RLIMIT_FSIZE)
maximum=25165825
maximum=maximum if hard==resource.RLIM_INFINITY else min(maximum,hard)
resource.setrlimit(resource.RLIMIT_FSIZE,(maximum,maximum))
os.execve(argv[0],argv,{'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'})
"""


def _check(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise SpeachesError("SPEACHES_CANCELLED")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SpeachesError("SPEACHES_TIMEOUT")
    return remaining


def _ready(pidfd, seconds=0):
    poll = select.poll()
    poll.register(pidfd, select.POLLIN)
    return bool(poll.poll(int(seconds * 1000)))


def _stop(pidfd):
    for action in (signal.SIGTERM, signal.SIGKILL):
        if _ready(pidfd):
            return True
        try:
            signal.pidfd_send_signal(pidfd, action, None, 0)
        except ProcessLookupError:
            return True
        if _ready(pidfd, 2):
            return True
    return False


def _reap_blocked():
    with _BLOCKED_LOCK:
        remaining = []
        for process, pidfd in _BLOCKED:
            dead = _ready(pidfd) if pidfd is not None else process.poll() is not None
            if dead:
                process.wait(timeout=0.1)
                if pidfd is not None:
                    os.close(pidfd)
            else:
                remaining.append((process, pidfd))
        _BLOCKED[:] = remaining
        if remaining:
            raise SpeachesError("SPEACHES_EXTRACTION_BUSY")


def _run_tool(argv, *, deadline, cancel_event, input_fd, cwd):
    """Sólo FFprobe/FFmpeg propios, sin shell ni entorno heredado; PIDFD retenido."""
    _check(deadline, cancel_event)
    if (
        not sys.platform.startswith("linux")
        or not hasattr(os, "pidfd_open")
        or not hasattr(signal, "pidfd_send_signal")
    ):
        raise SpeachesError("SPEACHES_EXTRACTION_UNSUPPORTED")
    _reap_blocked()
    if argv[0] not in ("/usr/bin/ffmpeg", "/usr/bin/ffprobe"):
        raise SpeachesError("SPEACHES_EXTRACTION_FAILED")
    payload = json.dumps(argv, ensure_ascii=True).encode("ascii") + b"\n"
    if len(payload) > 8192:
        raise SpeachesError("SPEACHES_EXTRACTION_FAILED")
    process = None
    pidfd = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c", _GATE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(input_fd,),
            close_fds=True,
            shell=False,
            bufsize=0,
            cwd=cwd,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        try:
            pidfd = os.pidfd_open(process.pid, 0)
        except OSError:
            process.stdin.close()
            process.wait(timeout=2)
            raise SpeachesError("SPEACHES_EXTRACTION_FAILED") from None
        output = {"stdout": bytearray(), "stderr": bytearray()}
        sent = 0
        eof = set()
        with selectors.DefaultSelector() as selector:
            for name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(
                    stream, selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ, name
                )
            while not (_ready(pidfd) and len(eof) == 2):
                _check(deadline, cancel_event)
                for key, _mask in selector.select(min(0.05, _check(deadline, cancel_event))):
                    if key.data == "stdin":
                        sent += os.write(key.fileobj.fileno(), payload[sent:])
                        if sent == len(payload):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        block = os.read(key.fileobj.fileno(), 8192)
                        if not block:
                            selector.unregister(key.fileobj)
                            eof.add(key.data)
                        else:
                            output[key.data].extend(block)
                            if len(output[key.data]) > OUTPUT_LIMIT:
                                raise SpeachesError("SPEACHES_EXTRACTION_OUTPUT_LIMIT")
        process.wait(timeout=0.1)
        if process.returncode:
            raise SpeachesError("SPEACHES_EXTRACTION_FAILED")
        return bytes(output["stdout"])
    except SpeachesError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SpeachesError("SPEACHES_EXTRACTION_FAILED") from None
    finally:
        stopped = True
        if pidfd is not None:
            stopped = _stop(pidfd)
            if stopped:
                os.close(pidfd)
                process.wait(timeout=0.1)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            if pidfd is None:
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    stopped = False
            if not stopped:
                # No se pierde la identidad ni se ejecuta otra herramienta mientras
                # el hijo propio sigue vivo. El supervisor externo conserva su rol.
                with _BLOCKED_LOCK:
                    _BLOCKED.append((process, pidfd))
                raise SpeachesError("SPEACHES_EXTRACTION_CLEANUP_FAILED")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _select_stream(raw, language):
    def invalid(_value):
        raise ValueError("non-finite")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=invalid)
        streams = value["streams"]
        if type(streams) is not list or not 0 < len(streams) <= 128:
            raise ValueError("streams")
        ranked = []
        for index, stream in enumerate(streams):
            tags = stream.get("tags") or {}
            disposition = stream.get("disposition") or {}
            tag = (tags.get("language") or "").strip().lower()
            mismatch = int(
                bool(language) and tag != language and tag not in _ALIASES.get(language, set())
            )
            secondary = int(
                any(
                    disposition.get(key)
                    for key in ("comment", "visual_impaired", "hearing_impaired", "dub")
                )
            )
            not_primary = int(not (disposition.get("default") or disposition.get("original")))
            ranked.append((mismatch, secondary, not_primary, index))
        return min(ranked)[-1]
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        raise SpeachesError("SPEACHES_AUDIO_PROBE_FAILED") from None


def _transcript(
    video_path, runtime_config, *, format_transcript, cache_transcript, cancel_event=None
):
    """Único proveedor; caché y formato originales reciben segundos float."""
    deadline = time.monotonic() + TOTAL_SECONDS
    _check(deadline, cancel_event)
    if getattr(runtime_config, "transcription_provider", None) != "speaches":
        raise SpeachesError("SPEACHES_PROVIDER_REQUIRED")
    token_file = getattr(runtime_config, "speaches_bearer_token_file", None)
    if (
        type(token_file) is not str
        or not token_file.startswith("/")
        or ".." in token_file.split("/")
    ):
        raise SpeachesError("SPEACHES_TOKEN_FILE_REQUIRED")
    maximum = getattr(runtime_config, "max_video_upload_bytes", None)
    if type(maximum) is not int or maximum <= 0:
        raise SpeachesError("SPEACHES_INPUT_REJECTED")
    language = getattr(runtime_config, "transcription_language", "")
    if type(language) is not str:
        raise SpeachesError("SPEACHES_INPUT_REJECTED")
    language = language.replace("-", "_").split("_", 1)[0] or None
    with (
        _file(video_path, maximum) as (video, _info),
        tempfile.TemporaryDirectory(prefix="agency-supoclip-speaches-") as directory,
    ):
        source = f"/proc/self/fd/{video.fileno()}"
        # Sólo demuxers de archivo: un playlist suministrado no abre URLs/otros archivos.
        input_args = [
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi,mpegts",
            "-i",
            source,
        ]
        probe = [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            *input_args,
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index:stream_tags=language:stream_disposition",
            "-of",
            "json",
        ]
        raw = _run_tool(
            probe,
            deadline=min(deadline, time.monotonic() + 15),
            cancel_event=cancel_event,
            input_fd=video.fileno(),
            cwd=directory,
        )
        index = _select_stream(raw, language)
        audio = Path(directory) / "audio.mp3"
        argv = [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            *input_args,
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
        ]
        try:
            _run_tool(
                argv,
                deadline=deadline,
                cancel_event=cancel_event,
                input_fd=video.fileno(),
                cwd=directory,
            )
        except SpeachesError as error:
            # El límite kernel es 24MiB+1, nunca un -fs que pare silenciosamente
            # en un audio parcial que todavía aparenta un tamaño aceptable.
            if (
                error.code == "SPEACHES_EXTRACTION_FAILED"
                and audio.exists()
                and audio.lstat().st_size > MAX_AUDIO_BYTES
            ):
                raise SpeachesError("SPEACHES_AUDIO_TOO_LARGE") from None
            raise
        with _file(audio, MAX_AUDIO_BYTES):
            remaining = _check(deadline, cancel_event)
            result = transcribe_audio(
                audio,
                token_file,
                language=language,
                cancel_event=cancel_event,
                total_seconds=remaining,
                idle_seconds=min(15, remaining),
            )
    _check(deadline, cancel_event)
    formatted = format_transcript(result)
    cache_transcript(video_path, result)
    return "\n".join(formatted)


def get_transcript_with_speaches(
    video_path, runtime_config, *, format_transcript, cache_transcript, cancel_event=None
):
    """No expone excepciones del filesystem, formateador o caché del llamador."""
    try:
        return _transcript(
            video_path,
            runtime_config,
            format_transcript=format_transcript,
            cache_transcript=cache_transcript,
            cancel_event=cancel_event,
        )
    except SpeachesError:
        raise
    except Exception:
        raise SpeachesError("SPEACHES_PROVIDER_FAILED") from None
