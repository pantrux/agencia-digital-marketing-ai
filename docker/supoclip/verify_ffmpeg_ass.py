"""Prueba FFmpeg y FFprobe con vídeo, audio y subtítulos ASS sintéticos."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

WIDTH, HEIGHT = 320, 180
FONT = "LiberationSans-Regular.ttf"
FONT_SHA256 = "559f38bdfcf12beead8a744579715dc7249d6978616e757de7d8cf06403aa531"
ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Liberation Sans,26,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,10,10,12,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,0:00:00.25,0:00:00.90,Default,,0,0,0,,Cámara y acción
"""


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def binary(path, pin):
    require(path.is_absolute() and re.fullmatch(r"[a-f0-9]{64}", pin), "BINARIO_O_PIN")
    path = path.resolve(strict=True)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 256 * 1024**2, "BINARIO_IRREGULAR")
    with path.open("rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == pin, "BINARIO_SHA")
    return path


def run(tool, arguments, cwd):
    env = {"LC_ALL": "C"}
    if os.name == "nt":
        env["SystemRoot"] = os.environ["SystemRoot"]
    result = subprocess.run(
        [tool, *arguments],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    require(len(result.stdout) + len(result.stderr) <= 1024**2, "FFMPEG_SALIDA_EXCESIVA")
    return result


def checked(tool, arguments, cwd, code):
    result = run(tool, arguments, cwd)
    require(result.returncode == 0, code)
    return result


def stream_info(ffprobe, filename, cwd):
    return json.loads(
        checked(
            ffprobe,
            [
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels",
                "-of",
                "json",
                filename,
            ],
            cwd,
            "FFPROBE_FALLO",
        ).stdout
    )["streams"]


def visible_pixels(ffmpeg, instant, cwd):
    frame = checked(
        ffmpeg,
        [
            "-v",
            "error",
            "-ss",
            instant,
            "-i",
            "clip.mp4",
            "-frames:v",
            "1",
            "-an",
            "-threads",
            "1",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        cwd,
        "DECODIFICACION_FALLO",
    ).stdout
    require(len(frame) == WIDTH * HEIGHT, "TAMANO_DE_CUADRO")
    return sum(pixel > 32 for pixel in frame)


def probe(ffmpeg, ffprobe, font):
    require(len(font) <= 4 * 1024**2 and digest(font) == FONT_SHA256, "FUENTE_SHA")
    with tempfile.TemporaryDirectory(prefix="supoclip-ass-") as cwd:
        work = Path(cwd)
        filters = checked(ffmpeg, ["-hide_banner", "-filters"], work, "FILTROS_FALLO").stdout
        require(re.search(rb"(?m)^\s*[.TSC]{2,3}\s+subtitles\s+V->V\b", filters), "SIN_FILTRO_ASS")
        (work / FONT).write_bytes(font)
        (work / "captions.ass").write_text(ASS, encoding="utf-8", newline="\n")
        render = checked(
            ffmpeg,
            [
                "-hide_banner",
                "-loglevel",
                "info",
                "-nostdin",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:r=12:d=1.5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1.5",
                "-filter_threads",
                "1",
                "-vf",
                "subtitles=filename=captions.ass:fontsdir=.",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                "clip.mp4",
            ],
            work,
            "QUEMADO_ASS_FALLO",
        )
        require(
            b"libass API version" in render.stderr and FONT.encode() in render.stderr,
            "LIBASS_FUENTE_NO_ACREDITADA",
        )
        frames = {
            instant: visible_pixels(ffmpeg, instant, work) for instant in ("0.0", "0.5", "1.1")
        }
        require(frames["0.0"] == frames["1.1"] == 0 and frames["0.5"] >= 100, "ASS_NO_VISIBLE")
        video = stream_info(ffprobe, "clip.mp4", work)
        require(
            len(video) == 2
            and video[0]["codec_name"] == "h264"
            and video[0]["width"] == WIDTH
            and video[0]["height"] == HEIGHT
            and video[0]["r_frame_rate"] == "12/1"
            and video[1]["codec_name"] == "aac",
            "CLIP_FORMATO",
        )
        checked(
            ffmpeg,
            [
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-i",
                "clip.mp4",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                "-c:a",
                "libmp3lame",
                "audio.mp3",
            ],
            work,
            "EXTRACCION_MP3_FALLO",
        )
        audio = stream_info(ffprobe, "audio.mp3", work)
        require(
            len(audio) == 1
            and audio[0]["codec_name"] == "mp3"
            and audio[0]["channels"] == 1
            and audio[0]["sample_rate"] == "16000",
            "MP3_FORMATO",
        )
        version = checked(ffmpeg, ["-version"], work, "VERSION_FALLO").stdout.splitlines()[0]
        return {
            "success": True,
            "scope": "synthetic-ass-mp3",
            "platform": sys.platform,
            "ffmpeg_version": version.decode("ascii"),
            "font_sha256": digest(font),
            "visible_pixels": frames,
            "clip": {"sha256": digest((work / "clip.mp4").read_bytes()), "streams": video},
            "mp3": {"sha256": digest((work / "audio.mp3").read_bytes()), "streams": audio},
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffmpeg-sha256", required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--ffprobe-sha256", required=True)
    parser.add_argument("--font", type=Path, required=True)
    args = parser.parse_args()
    try:
        ffmpeg = binary(args.ffmpeg, args.ffmpeg_sha256)
        ffprobe = binary(args.ffprobe, args.ffprobe_sha256)
        with args.font.open("rb") as stream:
            font = stream.read(4 * 1024**2 + 1)
        result = probe(ffmpeg, ffprobe, font)
        result.update(ffmpeg_sha256=args.ffmpeg_sha256, ffprobe_sha256=args.ffprobe_sha256)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(json.dumps({"success": False, "error": code}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
