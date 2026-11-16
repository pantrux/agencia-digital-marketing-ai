"""Prueba sesión y carga de un vídeo sintético mediante el proxy de SupoClip."""

import argparse
import hashlib
import http.cookiejar
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import suppress
from pathlib import Path

ORIGIN = "http://192.168.0.85:3080"
FIXTURE_BYTES = 89894
FIXTURE_SHA = "499d9bde244c4f294e103634da2cb42e5c60090257eab755d68af916483e5f00"
CLIP_FIXTURE_BYTES = 694534
CLIP_FIXTURE_SHA = "b0ec158e3d73c41d9ba4c2cf070a8400382b5f7b50ab7fc0f6722c18eed40606"
SIGNED = {"x-supoclip-user-id", "x-supoclip-ts", "x-supoclip-signature"}
VIDEO = re.compile(r"upload://([a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}\.mp4)")
UUID = re.compile(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}")
CAP = 2 * 1024**2
CLIP_CAP = 32 * 1024**2
TASK_SECONDS = 600


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, message):
    if not condition:
        raise ValueError(message)


def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar),
        NoRedirect(),
    )


def request(opener, route, expected, *, payload=None, headers=None, limit=CAP, timeout=20):
    sent = {"Origin": ORIGIN, "Accept-Encoding": "identity", **(headers or {})}
    verb = "POST" if payload is not None else "GET"
    if isinstance(payload, dict):
        sent["Content-Type"] = "application/json"
        payload = json.dumps(payload).encode()
    req = urllib.request.Request(ORIGIN + route, data=payload, headers=sent, method=verb)
    try:
        result = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        result = error
    with result:
        require(result.code in expected, f"{verb} {route}: HTTP {result.code}")
        raw = result.read(limit + 1)
        require(len(raw) <= limit, f"Respuesta demasiado grande en {route}")
        return result.code, result.headers, raw


def get_json(opener, route, expected, **kwargs):
    _, headers, raw = request(opener, route, expected, **kwargs)
    require("application/json" in headers.get("Content-Type", ""), f"JSON ausente en {route}")
    return json.loads(raw)


def multipart(video, fixture_sha=FIXTURE_SHA):
    token = "supoclip-prueba-media-" + fixture_sha[:24]
    data = (
        f"--{token}\r\n"
        'Content-Disposition: form-data; name="video"; filename="speech.mp4"\r\n'
        "Content-Type: video/mp4\r\n\r\n"
    ).encode()
    return data + video + f"\r\n--{token}--\r\n".encode(), {
        "Content-Type": f"multipart/form-data; boundary={token}"
    }


def inspect_clip(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            "mov",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=False,
        timeout=20,
    )
    require(result.returncode == 0 and not result.stderr, "ffprobe rechazó el clip")
    media = json.loads(result.stdout)
    streams = {stream["codec_type"]: stream for stream in media["streams"]}
    video, audio = streams.get("video", {}), streams.get("audio", {})
    duration = float(media["format"]["duration"])
    require(
        video.get("codec_name") == "h264"
        and audio.get("codec_name") == "aac"
        and 14 <= duration <= 31
        and video["height"] > video["width"] > 0,
        "Códecs, duración o formato del clip inesperados",
    )
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            "mov",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    require(decoded.returncode == 0 and not decoded.stderr, "El clip no se decodifica completo")
    return {
        "duration_seconds": duration,
        "width": video["width"],
        "height": video["height"],
        "video_codec": "h264",
        "audio_codec": "aac",
    }


def generate_clip(anonymous, authenticated, video_path, output):
    require(not output.exists() and not output.is_symlink(), "El archivo de salida ya existe")
    request(anonymous, "/api/tasks/create", {401}, payload={})
    task = get_json(
        authenticated,
        "/api/tasks/create",
        {200},
        payload={
            "source": {"url": video_path, "title": "Prueba sintética de SupoClip"},
            "processing_mode": "fast",
            "clip_length": "short",
            "output_format": "vertical",
            "add_subtitles": True,
            "include_broll": False,
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    task_id, job_id = task["task_id"], task["job_id"]
    require(UUID.fullmatch(task_id) and bool(job_id), "Tarea o trabajo de cola inválido")
    route = f"/api/tasks/{task_id}"
    deadline = time.monotonic() + TASK_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0, f"Tarea {task_id}: plazo agotado")
        current = get_json(authenticated, route, {200}, timeout=min(20, remaining))
        status = current["status"]
        if status == "completed":
            break
        require(status in {"queued", "processing"}, f"Tarea {task_id}: estado terminal {status}")
        time.sleep(min(2, max(0, deadline - time.monotonic())))

    listing = get_json(authenticated, route + "/clips", {200})
    clips = listing["clips"]
    require(
        listing["task_id"] == task_id and len(clips) == listing["total_clips"] > 0,
        f"Tarea {task_id}: lista de clips vacía o incongruente",
    )
    clip = next(
        (candidate for candidate in clips if candidate.get("hook_type") != "fallback"), None
    )
    require(clip is not None, f"Tarea {task_id}: sólo generó clips de respaldo")
    clip_id = clip["id"]
    require(
        UUID.fullmatch(clip_id) and bool(clip["text"].strip()),
        "Clip o transcripción inválidos",
    )
    start, end = (clip[field] for field in ("start_time", "end_time"))
    times = []
    for stamp in (start, end):
        require(re.fullmatch(r"\d\d:[0-5]\d", stamp), "Marca de tiempo inválida")
        minutes, seconds = map(int, stamp.split(":"))
        times.append(minutes * 60 + seconds)
    require(
        0 <= times[0] < times[1] <= 32 and 15 <= times[1] - times[0] <= 30,
        "Rango de clip fuera del fixture",
    )
    download = f"{route}/clips/{clip_id}/file"
    request(anonymous, download, {401})
    _, headers, data = request(authenticated, download, {200}, limit=CLIP_CAP)
    require(
        headers.get("Content-Type", "").split(";")[0] == "video/mp4"
        and len(data) >= 12
        and data[4:8] == b"ftyp",
        "Descarga MP4 inválida",
    )
    with tempfile.TemporaryDirectory(prefix="supoclip-clip-") as work:
        staged = Path(work) / "clip.mp4"
        staged.write_bytes(data)
        media = inspect_clip(staged)
    with output.open("xb") as stream:
        stream.write(data)
    return {
        "task_id": task_id,
        "job_id": job_id,
        "status": "completed",
        "clip_id": clip_id,
        "clips": len(clips),
        "hook_type": clip.get("hook_type"),
        "start_time": start,
        "end_time": end,
        "subtitles_requested": True,
        "download_bytes": len(data),
        "download_sha256": hashlib.sha256(data).hexdigest(),
        **media,
    }


def run(fixture, account, clip_output=None, *, clip_fixture=False):
    long_fixture = clip_fixture or clip_output is not None
    fixture_bytes = CLIP_FIXTURE_BYTES if long_fixture else FIXTURE_BYTES
    fixture_sha = CLIP_FIXTURE_SHA if long_fixture else FIXTURE_SHA
    if clip_output is not None:
        require(
            clip_output.parent.is_dir()
            and not clip_output.exists()
            and not clip_output.is_symlink(),
            "La salida debe ser un archivo nuevo en un directorio existente",
        )
    with fixture.open("rb") as stream:
        video = stream.read(fixture_bytes + 1)
    require(
        len(video) == fixture_bytes and hashlib.sha256(video).hexdigest() == fixture_sha,
        "Vídeo distinto",
    )
    email, password = account["email"], account["password"]
    require(
        isinstance(email, str)
        and re.fullmatch(r"supoclip-prueba-[a-z0-9-]+@example\.invalid", email)
        and isinstance(password, str)
        and 16 <= len(password) <= 100,
        "Cuenta de prueba inválida",
    )
    anonymous, authenticated = client(), client()
    request(anonymous, "/healthz", {200})
    _, _, signin = request(anonymous, "/sign-in", {200})
    _, _, sitemap = request(anonymous, "/sitemap.xml", {200})
    match = re.search(rb'["\'](/_next/static/[A-Za-z0-9_./-]+\.(?:js|css))["\']', signin)
    require(
        match is not None and ORIGIN.encode("ascii") in sitemap, "Interfaz o sitemap inesperados"
    )
    request(anonymous, match[1].decode("ascii"), {200})
    authorization_route = "/api/upload/authorization"
    session_route = "/api/auth/get-session"
    require(get_json(anonymous, session_route, {200}) is None, "Sesión anónima presente")
    request(anonymous, authorization_route, {401}, payload={})
    request(anonymous, "/upload", {401}, payload=b"")

    credentials = {"email": email, "password": password}
    get_json(
        authenticated,
        "/api/auth/sign-up/email",
        {200},
        payload={"name": "Prueba SupoClip", **credentials},
    )
    get_json(authenticated, "/api/auth/sign-out", {200}, payload={})
    get_json(authenticated, "/api/auth/sign-in/email", {200}, payload=credentials)
    session = get_json(authenticated, session_route, {200})
    require(session["user"]["email"] == email and bool(session["user"]["id"]), "Sesión inesperada")
    auth = get_json(
        authenticated,
        authorization_route,
        {200},
        payload={"filename": "speech.mp4", "size": len(video), "contentType": "video/mp4"},
    )
    require(
        auth["directUpload"] is True and auth["uploadUrl"] == ORIGIN + "/upload",
        "URL de carga inesperada",
    )
    signed = auth["headers"]
    require(
        set(signed) == SIGNED and signed["x-supoclip-user-id"] == session["user"]["id"],
        "Firma no vinculada a la sesión",
    )
    require(
        re.fullmatch(r"[a-f0-9]{64}", signed["x-supoclip-signature"]) is not None,
        "Formato de firma inesperado",
    )
    data, media_headers = multipart(video, fixture_sha)
    result = get_json(
        authenticated, "/upload", {200}, payload=data, headers={**signed, **media_headers}
    )
    uploaded = VIDEO.fullmatch(result["video_path"])
    require(uploaded is not None, "Identificador de carga inesperado")
    generated = None
    try:
        if clip_output is not None:
            generated = generate_clip(anonymous, authenticated, result["video_path"], clip_output)
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired):
        with suppress(KeyError, TypeError, ValueError, OSError):
            get_json(authenticated, "/api/auth/sign-out", {200}, payload={})
        raise
    get_json(authenticated, "/api/auth/sign-out", {200}, payload={})
    require(get_json(authenticated, session_route, {200}) is None, "La sesión sigue activa")
    request(authenticated, authorization_route, {401}, payload={})
    return {
        "fixture_sha256": fixture_sha,
        "fixture_bytes": len(video),
        "origin": ORIGIN,
        "signed_upload_status": 200,
        "unauthenticated_upload_status": 401,
        "upload_leaf": uploaded[1],
        "logout_verified": True,
        "success": True,
        **({"generation": generated} if generated is not None else {}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--clip-output", type=Path, help="Ejecuta la generación y guarda el MP4")
    mode.add_argument(
        "--clip-fixture", action="store_true", help="Sólo carga el fixture de generación"
    )
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(4097)
        require(0 < len(raw) <= 4096, "DTO de cuenta inválido")
        account = json.loads(raw)
        require(set(account) == {"email", "password"}, "DTO de cuenta inválido")
        print(
            json.dumps(
                run(args.fixture, account, args.clip_output, clip_fixture=args.clip_fixture),
                sort_keys=True,
            )
        )
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        detail = str(error) if type(error) is ValueError else type(error).__name__
        print(json.dumps({"success": False, "error": detail[:200]}, sort_keys=True))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
