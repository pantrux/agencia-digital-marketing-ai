"""Solicita imágenes de revisión y maestros al puente Remotion de la agencia."""

import math
import re
from sys import float_info

from common import JOB, PROJECT, UpstreamError, authorize_project, read_secret, request_json

HOST = "192.168.0.6"
PORT = 8090
FPS = 30
MAX_SECONDS = 600
TIMEOUT_SECONDS = 1205
MAX_JSON = 64 * 1024
DIMENSIONS = {"1080p": (1080, 1920), "4k": (2160, 3840)}
PLANS = ("Free", "Pro", "Ultra", "Ilimitado")
FORMATS = ("mp4", "prores4444")
REQUEST_FIELDS = {"project_id", "job_id", "composition", "plan", "format", "resolution", "props"}
PROP_FIELDS = {
    "title",
    "subtitle",
    "duration_seconds",
    "background",
    "accent",
    "video_path",
    "audio_path",
}
MEDIA_DIRS = {"01_raw", "03_assets", "04_edicion", "06_renders"}
MEDIA_EXTENSIONS = {
    "mp4",
    "mov",
    "mkv",
    "webm",
    "m4v",
    "mp3",
    "wav",
    "flac",
    "m4a",
    "aac",
    "ogg",
    "png",
    "jpg",
    "jpeg",
}
WINDOWS_DEVICE = re.compile(r"(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.IGNORECASE)
CONTROL_OR_SURROGATE = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
HEX_COLOR = re.compile(r"#[a-fA-F0-9]{6}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _text(value: object, maximum: int) -> str:
    # Las longitudes del contrato del worker se cuentan en unidades UTF-16.
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or CONTROL_OR_SURROGATE.search(value)
        or len(value.encode("utf-16-le")) > maximum * 2
    ):
        raise ValueError("REMOTION_ARGUMENTS")
    return value


def _media_path(value: object, project: str) -> str:
    path = _text(value, 500)
    if any(character in path for character in '\\<>:"|?*'):
        raise ValueError("REMOTION_ARGUMENTS")
    parts = path.split("/")
    if any(not part or part.endswith((".", " ")) or WINDOWS_DEVICE.match(part) for part in parts):
        raise ValueError("REMOTION_ARGUMENTS")
    own_project = len(parts) >= 4 and parts[:2] == ["proyectos", project] and parts[2] in MEDIA_DIRS
    global_library = len(parts) >= 2 and parts[0] == "_biblioteca_global"
    extension = parts[-1].rsplit(".", 1)[-1].lower()
    if not (own_project or global_library) or extension not in MEDIA_EXTENSIONS:
        raise ValueError("REMOTION_ARGUMENTS")
    return path


def _number(value: object, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and minimum <= value <= maximum


def _frame_count(seconds: float) -> int:
    return math.floor(seconds * FPS + 0.5)


def _request(arguments: dict, *, wants_stills: bool) -> dict:
    fields = REQUEST_FIELDS | {"frames"} if wants_stills else REQUEST_FIELDS
    if not isinstance(arguments, dict) or set(arguments) - fields:
        raise ValueError("REMOTION_ARGUMENTS")
    project = arguments.get("project_id")
    job = arguments.get("job_id")
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise ValueError("REMOTION_ARGUMENTS")
    if not isinstance(job, str) or not JOB.fullmatch(job):
        raise ValueError("REMOTION_ARGUMENTS")
    authorize_project(project)

    plan = arguments.get("plan")
    output_format = arguments.get("format", "mp4")
    resolution = arguments.get("resolution", "1080p")
    if (
        arguments.get("composition") != "AgencyVertical"
        or plan not in PLANS
        or output_format not in FORMATS
        or resolution not in ("1080p", "4k")
        or (resolution == "4k" and plan not in ("Ultra", "Ilimitado"))
        or (output_format == "prores4444" and plan != "Ilimitado")
    ):
        raise ValueError("REMOTION_ARGUMENTS")

    raw_props = arguments.get("props")
    if not isinstance(raw_props, dict) or set(raw_props) - PROP_FIELDS:
        raise ValueError("REMOTION_ARGUMENTS")
    title = _text(raw_props.get("title"), 120)
    subtitle = _text(raw_props.get("subtitle", ""), 240)
    background = _text(raw_props.get("background", "#102032"), 7)
    accent = _text(raw_props.get("accent", "#8fe3c5"), 7)
    duration = raw_props.get("duration_seconds")
    if (
        not title.strip()
        or not HEX_COLOR.fullmatch(background)
        or not HEX_COLOR.fullmatch(accent)
        or not _number(duration, 0.1, MAX_SECONDS)
    ):
        raise ValueError("REMOTION_ARGUMENTS")
    props = {
        "title": title,
        "subtitle": subtitle,
        "duration_seconds": duration,
        "background": background,
        "accent": accent,
    }
    for key in ("video_path", "audio_path"):
        if key in raw_props:
            props[key] = _media_path(raw_props[key], project)
    if not wants_stills and "audio_path" not in props:
        raise ValueError("REMOTION_ARGUMENTS")

    body = {
        "project_id": project,
        "job_id": job,
        "composition": "AgencyVertical",
        "plan": plan,
        "format": output_format,
        "resolution": resolution,
        "props": props,
    }
    if wants_stills:
        count = _frame_count(duration)
        frames = arguments.get("frames", [min(60, count - 1)])
        if (
            not isinstance(frames, list)
            or not 1 <= len(frames) <= 8
            or any(type(frame) is not int or not 0 <= frame < count for frame in frames)
            or len(set(frames)) != len(frames)
        ):
            raise ValueError("REMOTION_ARGUMENTS")
        body["frames"] = frames
    return body


def _post(path: str, body: dict) -> dict:
    token = read_secret("GPU_BRIDGE_TOKEN_FILE")
    try:
        return request_json(
            HOST,
            PORT,
            path,
            body=body,
            header=("Authorization", f"Bearer {token}"),
            timeout=TIMEOUT_SECONDS,
            limit=MAX_JSON,
        )
    except UpstreamError as error:
        code = {
            401: "REMOTION_UNAUTHORIZED",
            403: "REMOTION_FORBIDDEN",
            409: "REMOTION_JOB_EXISTS",
            422: "REMOTION_REJECTED",
            429: "REMOTION_BUSY",
        }.get(error.status, "REMOTION_UPSTREAM")
        raise ValueError(code) from None


def _fields(response: dict, expected: dict) -> dict:
    if not isinstance(response, dict) or any(
        type(response.get(key)) is not type(value) or response[key] != value
        for key, value in expected.items()
    ):
        raise ValueError("REMOTION_RESPONSE")
    return expected


def _files(response: dict, allowed_paths: list[set[str]]) -> list[dict]:
    raw = response.get("files")
    if not isinstance(raw, list) or len(raw) != len(allowed_paths):
        raise ValueError("REMOTION_RESPONSE")
    files = []
    for item, allowed in zip(raw, allowed_paths, strict=True):
        if not isinstance(item, dict):
            raise ValueError("REMOTION_RESPONSE")
        path, size, digest = item.get("path"), item.get("bytes"), item.get("sha256")
        if (
            not isinstance(path, str)
            or path not in allowed
            or type(size) is not int
            or not 0 < size <= 2**53 - 1
            or not isinstance(digest, str)
            or not SHA256.fullmatch(digest)
        ):
            raise ValueError("REMOTION_RESPONSE")
        files.append({"path": path, "bytes": size, "sha256": digest})
    return files


def stills(arguments: dict) -> dict:
    """Devuelve los PNG de revisión del trabajo y sus hashes."""
    body = _request(arguments, wants_stills=True)
    response = _post("/stills", body)
    width, height = DIMENSIONS[body["resolution"]]
    transparent = body["format"] == "prores4444"
    summary = _fields(
        response,
        {
            "width": width,
            "height": height,
            "resolution": body["resolution"],
            "transparent": transparent,
            "alpha_verified": transparent,
            "watermark": body["plan"] == "Free",
        },
    )
    duration = _frame_count(body["props"]["duration_seconds"]) / FPS
    frames = response.get("frames")
    if (
        frames != body["frames"]
        or any(type(frame) is not int for frame in frames)
        or not _number(response.get("duration_seconds"), duration, duration)
    ):
        raise ValueError("REMOTION_RESPONSE")
    prefix = f"proyectos/{body['project_id']}/05_revision/{body['job_id']}/stills/"
    files = _files(response, [{f"{prefix}still-{frame}.png"} for frame in frames])
    return {**summary, "files": files, "frames": frames, "duration_seconds": duration}


def _render_media(response: dict, body: dict, transparent: bool) -> dict:
    media = response.get("media")
    width, height = DIMENSIONS[body["resolution"]]
    projected = _fields(
        media,
        {
            "width": width,
            "height": height,
            "has_audio": True,
            "sample_rate": 48000,
            "video_codec": "prores" if transparent else "h264",
            "audio_codec": "pcm_s24le" if transparent else "aac",
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
        },
    )
    fps, duration = media.get("fps"), media.get("duration_seconds")
    expected_duration = _frame_count(body["props"]["duration_seconds"]) / FPS
    pixel_format = media.get("pixel_format")
    pixel_formats = ("yuva444p10le", "yuva444p12le") if transparent else ("yuv420p",)
    if (
        not _number(fps, FPS - 0.01, FPS + 0.01)
        or not _number(duration, expected_duration - 0.1, expected_duration + 0.1)
        or duration <= 0
        or pixel_format not in pixel_formats
        or (
            transparent
            and (media.get("video_profile") != "4444" or media.get("codec_tag") != "ap4h")
        )
    ):
        raise ValueError("REMOTION_RESPONSE")
    return {**projected, "fps": fps, "duration_seconds": duration, "pixel_format": pixel_format}


def render(arguments: dict) -> dict:
    """Devuelve el maestro del trabajo una vez validados vídeo y audio."""
    body = _request(arguments, wants_stills=False)
    response = _post("/render", body)
    transparent = body["format"] == "prores4444"
    summary = _fields(
        response,
        {
            "codec": "prores_ks" if transparent else "h264_nvenc",
            "hardware_acceleration": not transparent,
            "resolution": body["resolution"],
            "watermark": body["plan"] == "Free",
            "audio_verified": True,
            "alpha_verified": transparent,
        },
    )
    audio = response.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("REMOTION_RESPONSE")
    loudness, peak = audio.get("integrated_lufs"), audio.get("true_peak_dbtp")
    if not _number(loudness, -15, -13) or not _number(peak, -float_info.max, -1):
        raise ValueError("REMOTION_RESPONSE")
    media = _render_media(response, body, transparent)
    prefix = f"proyectos/{body['project_id']}/06_renders/{body['job_id']}/render/"
    extension = "mov" if transparent else "mp4"
    files = _files(
        response, [{f"{prefix}master.{extension}", f"{prefix}master-corrected.{extension}"}]
    )
    return {
        **summary,
        "files": files,
        "audio": {"integrated_lufs": loudness, "true_peak_dbtp": peak},
        "media": media,
    }
