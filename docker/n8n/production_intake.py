"""Verifica el traspaso de n8n y prepara la radiografía del crudo en OpenClaw."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import wave
from pathlib import Path

from production_handoff import (
    MAX_ENVELOPE,
    SPOOL,
    canonical,
    eligible,
    load_profile,
    publish,
    read_regular,
    verify,
)
from production_inbox import MEDIA, UUID, matches

VIDEO_FORMAT = {"mp4": "mov", "mov": "mov", "webm": "matroska"}
MAX_SECONDS = 600


def inspect_metadata(probe):
    duration = float(probe["format"]["duration"])
    streams = probe["streams"]
    picture = next((s for s in streams if s.get("codec_type") == "video"), {})
    if not 0 < duration <= MAX_SECONDS or any(
        type(picture.get(k)) is not int or not 0 < picture[k] <= 16384 for k in ("width", "height")
    ):
        raise ValueError("Crudo fuera del contrato del inspector")
    return duration, any(s.get("codec_type") == "audio" for s in streams)


def fingerprint(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode):
            raise ValueError("Derivado inválido")
        return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(), "bytes": meta.st_size}


def identity(meta):
    return (
        meta.st_dev,
        meta.st_ino,
        meta.st_mode,
        meta.st_size,
        meta.st_mtime_ns,
        meta.st_ctime_ns,
    )


def extract_audio(input_args, fd, target):
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".audio-", suffix=".wav") as output:
        subprocess.run(
            [
                "/usr/bin/ffmpeg",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                *input_args,
                "-map",
                "0:a:0",
                "-vn",
                "-t",
                str(MAX_SECONDS),
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "1",
                output.name,
            ],
            check=True,
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        with wave.open(output.name, "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
                1,
                2,
                48000,
            ) or not wav.getnframes():
                raise ValueError("WAV fuera del contrato de transcripción")
        expected = fingerprint(output.name)
        output.flush()
        os.fsync(output.fileno())
        try:
            os.link(output.name, target)
        except FileExistsError:
            if fingerprint(target) != expected:
                raise ValueError("WAV existente con contenido distinto") from None
        return expected


def prepare_source(media, audio_name):
    project = MEDIA / "proyectos" / media["project_slug"]
    raw = project / "01_raw" / (media["asset_id"] + "." + media["extension"])
    if raw.resolve(strict=True) != raw:
        raise ValueError("Ruta de crudo inválida")
    fd = os.open(raw, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size != media["bytes"]:
            raise ValueError("Tamaño o identidad de crudo inválidos")
        if hashlib.file_digest(source, "sha256").hexdigest() != media["sha256"]:
            raise ValueError("Huella de crudo inválida")
        source.seek(0)
        args = [
            "-protocol_whitelist",
            "file",
            "-f",
            VIDEO_FORMAT[media["extension"]],
            "-i",
            f"/proc/self/fd/{fd}",
        ]
        probe = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                *args,
                "-show_entries",
                "stream=codec_type,width,height:format=duration",
                "-of",
                "json",
            ],
            check=True,
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=25,
        )
        duration, has_audio = inspect_metadata(json.loads(probe.stdout))
        audio = None
        if has_audio:
            assets = project / "03_assets"
            assets.mkdir(mode=0o700, exist_ok=True)
            if assets.resolve(strict=True) != assets or not assets.is_dir():
                raise ValueError("Directorio de audio inválido")
            source.seek(0)
            target = assets / audio_name
            audio = {"path": str(target.relative_to(MEDIA)), **extract_audio(args, fd, target)}
        if identity(meta) != identity(os.fstat(fd)) or identity(meta) != identity(
            raw.stat(follow_symlinks=False)
        ):
            raise ValueError("El crudo cambió durante la preparación")
    return {
        "media": media,
        "raw": str(raw.relative_to(MEDIA)),
        "duration_seconds": duration,
        "audio": audio,
    }


def prepare(order, journal):
    first = prepare_source(order["media"], "intake-source.wav")
    receipt = {"order": order, **{key: value for key, value in first.items() if key != "media"}}
    if order.get("additional_media"):
        receipt["additional_sources"] = [
            prepare_source(media, f"intake-source-{media['asset_id']}.wav")
            for media in order["additional_media"]
        ]
    publish(journal, canonical(receipt) + b"\n")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("profile", type=Path)
    cli.add_argument("journal", type=Path)
    opts = cli.parse_args()
    profile = load_profile(opts.profile)
    if SPOOL.resolve(strict=True) != SPOOL or opts.journal.resolve(strict=True) != opts.journal:
        raise ValueError("Directorios de recepción inválidos")
    key = bytes.fromhex(profile["key"])
    failed = False
    for path in sorted(SPOOL.glob("*.json")):
        if not matches(UUID, path.stem):
            continue
        try:
            order = verify(read_regular(path, MAX_ENVELOPE), key, path.stem)
            journal = opts.journal / path.name
            if eligible(order, profile) and not journal.exists():
                prepare(order, journal)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, wave.Error):
            failed = True
    if failed:
        raise SystemExit("Falló la preparación de una orden de producción")


if __name__ == "__main__":
    main()
