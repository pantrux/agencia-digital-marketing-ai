"""Inspecciona medios NAS y transcribe audio preparado en el puente GPU."""

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import time
import unicodedata
import wave
from pathlib import Path, PurePosixPath

from common import JOB, UpstreamError, authorize_project, read_secret, request_json

HOST = "192.168.0.6"
NAS = Path("/mnt/producciones")
SOURCE_FOLDERS = {"01_raw", "03_assets", "04_edicion", "06_renders"}
VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
SHA = re.compile(r"[a-f0-9]{64}")
MEDIA_FIELDS = (
    "duration_seconds",
    "width",
    "height",
    "fps",
    "video_codec",
    "video_profile",
    "codec_tag",
    "audio_codec",
    "has_audio",
    "sample_rate",
    "color_space",
    "color_transfer",
    "color_primaries",
    "pixel_format",
)
RULER_FIELDS = (
    "y_min",
    "y_max",
    "canvas_width",
    "canvas_height",
    "original_width",
    "original_height",
    "source_scale",
    "source_x",
    "source_y",
)
MEDIA_NAMES = set(MEDIA_FIELDS) - {
    "duration_seconds",
    "width",
    "height",
    "fps",
    "has_audio",
    "sample_rate",
}
MAX_SVG = 70 * 1024**2
MAX_PREVIEW = 2 * 1024**2
MAX_AUDIO_SECONDS = 600
CHUNK_SECONDS = 120
AUDIO_RATE = 16000


def source_path(value: str, project: str, suffixes: set[str]) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 500
        or any(
            character in '\\<>:"|?*' or unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError("RUTA_MEDIO_INVALIDA")
    parts = value.split("/")
    if (
        len(parts) < 4
        or parts[:2] != ["proyectos", project]
        or parts[2] not in SOURCE_FOLDERS
        or any(
            not part
            or part in {".", ".."}
            or part.endswith((".", " "))
            or re.match(r"(?i)^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", part)
            for part in parts
        )
    ):
        raise ValueError("RUTA_MEDIO_INVALIDA")
    source = PurePosixPath(value)
    if source.suffix.lower() not in suffixes:
        raise ValueError("FORMATO_MEDIO_INVALIDO")
    return source


def _numbers(values: list, maximum: float) -> bool:
    return all(type(v) in (int, float) and 0 <= v <= maximum for v in values)


def _artifacts(value: object, project: str, job: str, samples: int) -> list[dict]:
    if not isinstance(value, list) or len(value) != samples + 1:
        raise ValueError("INSPECCION_RESPUESTA_INVALIDA")
    prefix = f"proyectos/{project}/05_revision/{job}/inspect/"
    expected = {prefix + f"inspect-{i}.png" for i in range(samples)} | {
        prefix + "contact-sheet.svg"
    }
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("INSPECCION_RESPUESTA_INVALIDA")
        path, size, digest = (item.get(name) for name in ("path", "bytes", "sha256"))
        if (
            not isinstance(path, str)
            or path not in expected
            or type(size) is not int
            or not 0 < size <= 100 * 1024**2
            or not isinstance(digest, str)
            or not SHA.fullmatch(digest)
        ):
            raise ValueError("INSPECCION_RESPUESTA_INVALIDA")
        expected.remove(path)
        result.append({"path": path, "bytes": size, "sha256": digest})
    return result


def inspect_media(project: str, job: str, path: str, samples: int = 6) -> dict:
    authorize_project(project)
    if (
        not isinstance(job, str)
        or not JOB.fullmatch(job)
        or type(samples) is not int
        or not 1 <= samples <= 12
    ):
        raise ValueError("INSPECCION_ARGUMENTOS")
    source = source_path(path, project, VIDEO)
    token = read_secret("GPU_BRIDGE_TOKEN_FILE")
    try:
        result = request_json(
            HOST,
            8090,
            "/inspect",
            body={
                "project_id": project,
                "job_id": job,
                "input_path": str(source),
                "samples": samples,
            },
            header=("Authorization", "Bearer " + token),
            timeout=1205,
            limit=1024**2,
        )
    except UpstreamError as error:
        code = {
            401: "PUENTE_NO_AUTENTICADO",
            403: "PUENTE_NO_AUTORIZADO",
            409: "TRABAJO_EXISTENTE",
            429: "PUENTE_OCUPADO",
        }
        raise ValueError(code.get(error.status, "INSPECCION_NO_DISPONIBLE")) from None
    objects = result.get("media"), result.get("ruler"), result.get("audio")
    times = result.get("samples_seconds")
    media, ruler, audio = objects
    if (
        not isinstance(media, dict)
        or not set(MEDIA_FIELDS) <= media.keys()
        or not _numbers([media["duration_seconds"], media["fps"]], 600)
        or media["duration_seconds"] <= 0
        or any(type(media[k]) is not int or not 0 < media[k] <= 16384 for k in ("width", "height"))
        or any(
            media[k] is not None
            and (
                not isinstance(media[k], str)
                or re.fullmatch(r"[A-Za-z0-9 ._()+-]{1,80}", media[k]) is None
            )
            for k in MEDIA_NAMES
        )
        or type(media["has_audio"]) is not bool
        or (
            media["sample_rate"] is not None
            and (type(media["sample_rate"]) is not int or not 0 < media["sample_rate"] <= 768000)
        )
        or not isinstance(ruler, dict)
        or not set(RULER_FIELDS) <= ruler.keys()
        or (ruler["y_min"], ruler["y_max"], ruler["canvas_width"], ruler["canvas_height"])
        != (0, 1920, 1080, 1920)
        or (ruler["original_width"], ruler["original_height"]) != (media["width"], media["height"])
        or not _numbers([ruler[k] for k in ("source_scale", "source_x", "source_y")], 1920)
        or not isinstance(times, list)
        or len(times) != samples
        or not _numbers(times, media["duration_seconds"])
        or sorted(set(times)) != times
    ):
        raise ValueError("INSPECCION_RESPUESTA_INVALIDA")
    if audio is not None:
        if not isinstance(audio, dict):
            raise ValueError("INSPECCION_RESPUESTA_INVALIDA")
        audio = audio_projection(audio)
    return {
        "media": {k: media[k] for k in MEDIA_FIELDS},
        "ruler": {k: ruler[k] for k in RULER_FIELDS},
        "audio": audio,
        "samples_seconds": times,
        "files": _artifacts(result.get("files"), project, job, samples),
    }


def audio_projection(audio: dict) -> dict:
    validated = {}
    for key in ("integrated_lufs", "true_peak_dbtp"):
        value = audio.get(key)
        if type(value) not in (int, float) or not -250 <= value <= 30 or not math.isfinite(value):
            raise ValueError("INSPECCION_RESPUESTA_INVALIDA")
        validated[key] = value
    return validated


def ruler_image(inspection: dict) -> bytes:
    """Rasteriza la tira verificada para que el agente pueda medir el encuadre."""
    artifact = next(
        row for row in inspection["files"] if row["path"].endswith("/contact-sheet.svg")
    )
    source = NAS / artifact["path"]
    if source.resolve(strict=True) != source or not 0 < artifact["bytes"] <= MAX_SVG:
        raise ValueError("TIRA_NO_DISPONIBLE")
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        svg = stream.read(artifact["bytes"] + 1)
    if len(svg) != artifact["bytes"] or hashlib.sha256(svg).hexdigest() != artifact["sha256"]:
        raise ValueError("TIRA_HASH_INVALIDO")
    # El worker emite XML estático con sólo PNG incrustados. Se excluyen
    # las construcciones que podrían referir a archivos o URLs externas.
    if (
        not svg.startswith(b'<svg xmlns="http://www.w3.org/2000/svg"')
        or not svg.endswith(b"</svg>")
        or re.search(rb"<!|<\?|\burl\s*\(|\b(?:src|style|base)\s*=", svg, re.I)
        or any(
            not value.startswith(b'"data:image/png;base64,')
            for value in re.findall(rb'\b(?:xlink:)?href\s*=\s*(["\'][^"\']*["\'])', svg, re.I)
        )
    ):
        raise ValueError("TIRA_FORMATO_INVALIDO")
    try:
        # El demuxer de esta versión necesita un descriptor con seek.
        with tempfile.NamedTemporaryFile(suffix=".svg") as copy:
            copy.write(svg)
            copy.flush()
            process = subprocess.run(
                [
                    "/usr/bin/ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-f",
                    "svg_pipe",
                    "-c:v",
                    "librsvg",
                    "-i",
                    copy.name,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(1000,iw)':-2",
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    "3",
                    "-f",
                    "image2pipe",
                    "pipe:1",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=True,
            )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("TIRA_CONVERSION_NO_DISPONIBLE") from None
    image = process.stdout
    if (
        not 0 < len(image) <= MAX_PREVIEW
        or not image.startswith(b"\xff\xd8")
        or not image.endswith(b"\xff\xd9")
    ):
        raise ValueError("TIRA_CONVERSION_NO_DISPONIBLE")
    return image


def read_audio(command, source, timeout):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("RUTA_MEDIO_INVALIDA")
        return subprocess.run(
            command[:1]
            + ["-protocol_whitelist", "file,pipe", "-f", source.suffix.lower()[1:]]
            + command[1:]
            + [f"/proc/self/fd/{stream.fileno()}"],
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            timeout=timeout,
            check=True,
        ).stdout


def audio_duration(source):
    try:
        result = read_audio(
            ["/usr/bin/ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json"],
            source,
            12,
        )
        duration = float(json.loads(result)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        raise ValueError("AUDIO_DURACION_NO_DISPONIBLE") from None
    if not 0 < duration <= MAX_AUDIO_SECONDS + 0.05:
        raise ValueError("AUDIO_DURACION_INVALIDA")
    return duration


def prepare_audio(source, target):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb"):
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("RUTA_MEDIO_INVALIDA")
        subprocess.run(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                source.suffix.lower()[1:],
                "-i",
                f"/proc/self/fd/{fd}",
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                str(AUDIO_RATE),
                "-c:a",
                "pcm_s16le",
                "-t",
                str(MAX_AUDIO_SECONDS + 1),
                str(target),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(fd,),
            timeout=25,
            check=True,
        )


def transcribe_chunks(media, token_file, language, send, deadline):
    with tempfile.TemporaryDirectory(prefix="agencia-transcripcion-") as directory:
        prepared = Path(directory) / "audio.wav"
        chunk = prepared.with_name("fragmento.wav")
        prepare_audio(media, prepared)
        with wave.open(str(prepared), "rb") as track:
            frames = track.getnframes()
            if (track.getnchannels(), track.getsampwidth(), track.getframerate()) != (
                1,
                2,
                AUDIO_RATE,
            ) or not 0 < frames <= MAX_AUDIO_SECONDS * AUDIO_RATE:
                raise ValueError("AUDIO_DURACION_INVALIDA")
            text, words, detected = [], [], language
            for offset in range(0, frames, CHUNK_SECONDS * AUDIO_RATE):
                count = min(frames - offset, CHUNK_SECONDS * AUDIO_RATE)
                samples = track.readframes(count)
                if len(samples) != count * 2:
                    raise ValueError("AUDIO_FRAGMENTO_INVALIDO")
                with wave.open(str(chunk), "wb") as part:
                    part.setparams((1, 2, AUDIO_RATE, 0, "NONE", "not compressed"))
                    part.writeframes(samples)
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    raise ValueError("SPEACHES_TIMEOUT")
                result = send(
                    chunk,
                    token_file,
                    language=language,
                    total_seconds=remaining,
                    idle_seconds=min(15, remaining),
                    first_byte_seconds=min(45, remaining),
                )
                duration = count / AUDIO_RATE
                if abs(duration - result["duration"]) > 0.1:
                    raise ValueError("AUDIO_FRAGMENTO_INVALIDO")
                if result["text"].strip():
                    text.append(result["text"].strip())
                    if detected is None:
                        detected = result["language"]
                seconds = offset / AUDIO_RATE
                for word in result["words"]:
                    start = seconds + min(word["start"], duration)
                    end = seconds + min(word["end"], duration)
                    words.append({"word": word["word"], "start": start, "end": end})
            return {
                "language": detected,
                "duration": frames / AUDIO_RATE,
                "text": " ".join(text),
                "words": words,
            }


def transcribe(project: str, path: str, language: str | None = None) -> dict:
    authorize_project(project)
    source = source_path(path, project, {".mp3", ".wav"})
    # El instalador incluye las fuentes compartidas de Speaches junto a este adaptador.
    try:
        from speaches_client import transcribe_audio
        from speaches_contract import SpeachesError
    except ImportError:
        raise ValueError("SPEACHES_NO_INSTALADO") from None
    media = NAS / Path(source)
    try:
        if media.resolve(strict=True) != media:
            raise ValueError("RUTA_MEDIO_INVALIDA")
        token_file = Path(os.environ.get("GPU_BRIDGE_TOKEN_FILE", ""))
        deadline = time.monotonic() + 290
        duration = audio_duration(media)
        if duration > CHUNK_SECONDS:
            return transcribe_chunks(media, token_file, language, transcribe_audio, deadline)
        result = transcribe_audio(media, token_file, language=language, first_byte_seconds=45)
    except SpeachesError as error:
        raise ValueError(error.code) from None
    except (OSError, subprocess.SubprocessError, wave.Error):
        raise ValueError("MEDIO_NO_DISPONIBLE") from None
    return {
        "language": result["language"],
        "duration": result["duration"],
        "text": result["text"],
        "words": [{k: item[k] for k in ("word", "start", "end")} for item in result["words"]],
    }
