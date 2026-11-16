#!/usr/bin/env python3
"""Comprueba las operaciones multimedia de Zou con archivos sintéticos pequeños."""

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

VERSIONS = ("9.0.2-zou.1", "9.0.2-zou.2")
SITE = Path("/opt/zou/site")
MOVIE_SHA = "8f5b31a1b6364e42306defcd564918ccf278dd6199b8f7c1d9b5e9f02716b7df"
EXTERNAL_FIXTURES = {
    "mkv": ("af9d1699253dec4223bd371cfcfadd9fc30b5c7b4232adc82e344117b6543475", ("hevc", "aac")),
    "webm": ("44beb01be7ecbc94d230971e93f4c7c2208ea2070d03d22f87b02ee36c0e9f3e", ("vp9", "opus")),
}
GENERATED_FIXTURES = {
    "mp4": ("mp4", "libx264", "aac", ("h264", "aac")),
    "mov": ("mov", "prores_ks", "pcm_s16le", ("prores", "pcm_s16le")),
    "avi": ("avi", "mpeg4", "libmp3lame", ("mpeg4", "mp3")),
    "wmv": ("asf", "wmv2", "wmav2", ("wmv2", "wmav2")),
    "m4v": ("ipod", "libx264", "aac", ("h264", "aac")),
}
MEDIA_LIMIT = 8 * 1024 * 1024
FIXTURE_LIMIT = 64 * 1024
CLIP_DURATION = 0.75


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(tool, *args):
    result = subprocess.run(
        [tool, *map(str, args)],
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        timeout=20,
    )
    if result.returncode:
        details = result.stderr.decode("utf-8", "replace")[-900:].strip()
        raise RuntimeError(f"{tool} terminó con código {result.returncode}. {details}")
    return result.stdout


def inspect_media(path):
    length = path.stat().st_size
    require(0 < length < MEDIA_LIMIT, f"Tamaño de salida inválido para {path.name}")
    return json.loads(
        command("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path)
    )


def verify_video(path, codecs, size, minimum_duration):
    info = inspect_media(path)
    videos = [stream for stream in info["streams"] if stream["codec_type"] == "video"]
    audio = [stream for stream in info["streams"] if stream["codec_type"] == "audio"]
    require(
        len(videos) == 1 and videos[0]["codec_name"] == codecs[0],
        f"Vídeo inesperado en {path.name}",
    )
    expected_audio = [] if codecs[1] is None else [codecs[1]]
    require(
        [stream["codec_name"] for stream in audio] == expected_audio,
        f"Audio inesperado en {path.name}",
    )
    require(
        (videos[0]["width"], videos[0]["height"]) == size, f"Resolución inesperada en {path.name}"
    )
    duration = float(info["format"].get("duration", 0))
    require(minimum_duration <= duration <= 3, f"Duración inesperada en {path.name}")
    return {
        "video": codecs[0],
        "audio": codecs[1],
        "resolucion": list(size),
        "segundos": round(duration, 3),
    }


def verify_png(path, size):
    info = inspect_media(path)
    with path.open("rb") as stream:
        require(stream.read(8) == b"\x89PNG\r\n\x1a\n", "Firma PNG incorrecta")
    (stream,) = info["streams"]
    require(
        stream["codec_name"] == "png" and (stream["width"], stream["height"]) == size,
        "PNG inesperado",
    )
    return list(size)


def generate(path, container, video_codec, audio_codec, size=(128, 72), frequency=660):
    width, height = size
    video = f"testsrc2=size={width}x{height}:rate=12:duration={CLIP_DURATION}"
    args = ["-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", video]
    if audio_codec is not None:
        audio = f"sine=frequency={frequency}:sample_rate=48000:duration={CLIP_DURATION}"
        args.extend(["-f", "lavfi", "-i", audio, "-map", "0:v", "-map", "1:a", "-c:a", audio_codec])
    pixel_format = "yuv422p10le" if video_codec == "prores_ks" else "yuv420p"
    args.extend(
        ["-threads", "1", "-c:v", video_codec, "-pix_fmt", pixel_format, "-f", container, path]
    )
    command("ffmpeg", *args)


def copy_fixture(source, destination, sha):
    require(stat.S_ISREG(source.lstat().st_mode), "El archivo sintético debe ser regular")
    with source.open("rb") as stream:
        data = stream.read(FIXTURE_LIMIT + 1)
    require(
        0 < len(data) <= FIXTURE_LIMIT and hashlib.sha256(data).hexdigest() == sha,
        f"Huella inesperada para el archivo sintético {destination.name}",
    )
    destination.write_bytes(data)


def verify_tools(expected_version):
    for tool in ("ffmpeg", "ffprobe"):
        version = command(tool, "-version").splitlines()[0].decode().split()
        require(version[:3] == [tool, "version", expected_version], f"Versión inesperada de {tool}")
        protocols = command(tool, "-hide_banner", "-protocols").decode().splitlines()
        observed = {}
        section = None
        for line in protocols:
            if line in ("Input:", "Output:"):
                section = line
                observed[section] = set()
            elif section is not None and line.startswith(" "):
                observed[section].add(line.strip())
        require(
            observed == {"Input:": {"file", "pipe"}, "Output:": {"file", "pipe"}},
            f"Protocolos inesperados en {tool}",
        )


def load_movie():
    path = SITE / "zou/utils/movie.py"
    require(
        hashlib.sha256(path.read_bytes()).hexdigest() == MOVIE_SHA,
        "Huella inesperada del módulo movie.py",
    )
    sys.path.insert(0, str(SITE))
    spec = importlib.util.spec_from_file_location("zou_media_movie", path)
    movie = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(movie)
    return movie


def exercise(movie, root, fixture_paths):
    size = (128, 72)
    output_codecs = ("h264", "aac")
    sources = {}
    result = {"formatos": {}, "sin_audio": {}, "png": {}, "concatenacion": {}}
    for extension, (container, video, audio, codecs) in GENERATED_FIXTURES.items():
        path = root / f"entrada.{extension}"
        generate(path, container, video, audio)
        sources[extension] = (path, codecs)
    for extension, (sha, codecs) in EXTERNAL_FIXTURES.items():
        path = root / f"entrada.{extension}"
        copy_fixture(fixture_paths[extension], path, sha)
        sources[extension] = (path, codecs)
    for extension, (path, codecs) in sources.items():
        original = verify_video(path, codecs, size, 0.6)
        require(
            movie.get_movie_size(str(path)) == size,
            f"Zou leyó dimensiones incorrectas en {extension}",
        )
        normalized = root / f"normalizado-{extension}.mp4"
        movie.normalize_encoding(
            str(path), extension, str(normalized), 12, 1, *size, preset="ultrafast"
        )
        result["formatos"][extension] = {
            "entrada": original,
            "normalizado": verify_video(normalized, output_codecs, size, 0.6),
        }

    first = sources["mp4"][0]
    result["png"]["miniatura"] = verify_png(Path(movie.generate_thumbnail(str(first))), size)
    result["png"]["mosaico"] = verify_png(Path(movie.generate_tile(str(first))), (1424, 200))
    silent_mp4 = root / "silencioso.mp4"
    silent_webm = root / "silencioso.webm"
    generate(silent_mp4, "mp4", "libx264", None)
    command(
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        sources["webm"][0],
        "-map",
        "0:v",
        "-c:v",
        "copy",
        "-f",
        "webm",
        silent_webm,
    )
    for path, codec in ((silent_mp4, "h264"), (silent_webm, "vp9")):
        verify_video(path, (codec, None), size, 0.6)
        require(not movie.has_soundtrack(str(path)), "Zou detectó audio en un archivo sin pista")
        code, _, _ = movie.add_empty_soundtrack(str(path))
        require(code == 0 and movie.has_soundtrack(str(path)), "Zou no añadió la pista de silencio")
        output_codec = next(
            stream["codec_name"]
            for stream in inspect_media(path)["streams"]
            if stream["codec_type"] == "video"
        )
        require(output_codec in (codec, "h264"), "El vídeo silencioso cambió a un códec inesperado")
        result["sin_audio"][path.suffix[1:]] = verify_video(path, (output_codec, "aac"), size, 0.6)

    second = root / "igual.mp4"
    generate(second, "mp4", "libx264", "aac", frequency=440)
    same = root / "concatenado-igual.mp4"
    combined = movie.build_playlist_movie(
        movie.concat_demuxer,
        [(str(first), first.name), (str(second), second.name)],
        str(same),
        *size,
        12,
    )
    require(combined["success"], "Zou no concatenó los archivos de formato idéntico")
    result["concatenacion"]["demultiplexor"] = verify_video(same, output_codecs, size, 1.35)

    varied = root / "distinto.mov"
    # Zou escala después de fijar SAR. Conservamos la proporción de pantalla
    # para verificar resoluciones y códecs distintos con el filtro existente.
    generate(varied, "mov", "prores_ks", "pcm_s16le", (96, 54), frequency=330)
    different = root / "concatenado-distinto.mp4"
    combined = movie.build_playlist_movie(
        movie.concat_filter,
        [(str(first), first.name), (str(varied), varied.name)],
        str(different),
        *size,
        12,
    )
    require(combined["success"], "Zou no concatenó los archivos de resolución y códecs distintos")
    result["concatenacion"]["filtro"] = verify_video(different, output_codecs, size, 1.35)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mkv", required=True, type=Path, help="Archivo sintético HEVC/AAC fijado")
    parser.add_argument("--version-esperada", choices=VERSIONS, default=VERSIONS[-1])
    parser.add_argument(
        "--webm", required=True, type=Path, help="Archivo sintético VP9/Opus fijado"
    )
    parser.add_argument("--ejecutar", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    require(
        sys.platform == "linux" and (os.getuid(), os.getgid()) == (1000, 1000),
        "Se requiere Linux con UID:GID 1000:1000",
    )
    require(
        {item.name for item in Path("/sys/class/net").iterdir()} == {"lo"},
        "La sonda requiere aislamiento de red",
    )
    os.environ["PATH"] = "/usr/bin:/bin"
    if not args.ejecutar:
        with subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", __file__, *sys.argv[1:], "--ejecutar"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise RuntimeError("La sonda superó el límite de 120 segundos") from None
        if process.returncode:
            sys.stderr.buffer.write(stderr[-3000:])
            return process.returncode
        sys.stdout.buffer.write(stdout)
        return 0

    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (MEDIA_LIMIT, MEDIA_LIMIT))
    cpu = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, cpu[:2])
    verify_tools(args.version_esperada)
    movie = load_movie()
    with tempfile.TemporaryDirectory(prefix="zou-media-", dir="/tmp") as directory:
        tempfile.tempdir = directory
        try:
            result = exercise(movie, Path(directory), {"mkv": args.mkv, "webm": args.webm})
        finally:
            tempfile.tempdir = None
    print(
        json.dumps(
            {
                "ok": True,
                "version": args.version_esperada,
                "protocolos": ["file", "pipe"],
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"Sonda de multimedia falló. {exc}", file=sys.stderr)
        raise SystemExit(1) from None
