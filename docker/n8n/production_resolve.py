"""Traspasa un anuncio acreditado de Studio a una línea de tiempo editable."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from production_handoff import canonical, publish, read_regular, render_plan
from production_inbox import UUID, matches
from production_valeria import AD_MUSIC_ID, source_paths

FPS = 30
FRAMES = 900
MUSIC_PATH = f"_biblioteca_global/musica-stock/{AD_MUSIC_ID}.mp3"
MUSIC_SHA256 = "8433b770a630d9b1594fd484442c677907ece899a4d149954cd2e74fd733e311"
LIMIT = 131072
IMAGE_LIMIT = 2 * 1024**3
PACKAGE_FILES = {
    "imagen-edicion.mov",
    "voz-original.wav",
    "musica-catalogo.wav",
    "montaje.otio",
    "subtitulos.srt",
}


def inspect_file(path, max_bytes=1024**3):
    if path.resolve(strict=True) != path:
        raise ValueError("Se requiere un archivo regular en su ruta original")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= max_bytes:
            raise ValueError("Tamaño de medio fuera del límite")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stamp = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if any(
            stamp != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            for info in (os.fstat(fd), path.stat(follow_symlinks=False))
        ):
            raise ValueError("El archivo cambió durante la lectura")
    return {"sha256": digest, "bytes": before.st_size}


def validated_cut(intake, direction):
    order = intake["order"]
    order_id, media = order["order_id"], order["media"]
    if (
        not matches(UUID, order_id)
        or media["project_slug"] != "job-" + order_id.replace("-", "")
        or not matches(UUID, media["asset_id"])
        or media["extension"] not in {"mp4", "mov", "webm"}
    ):
        raise ValueError("Identidad de producción inválida")
    decision = direction["escaleta"]
    render = decision["render"]
    props = render["props"]
    if (
        direction["order_id"] != order_id
        or direction["source_sha256"] != media["sha256"]
        or decision["estado"] != "lista_para_stills"
        or render["project_id"] != media["project_slug"]
        or render["plan"] != render_plan(order)
        or render["plan"] == "Free"
        or render["composition"] != "AgencyVertical"
        or render["format"] != "mp4"
        or (props["video_path"], props["audio_path"]) != source_paths(intake)
        or props["duration_seconds"] != FRAMES / FPS
        or props["ad"]["soundtrack"] != AD_MUSIC_ID
    ):
        raise ValueError("El montaje de 30 s no corresponde a una escaleta acreditada")
    source_frames = int(intake["duration_seconds"] * FPS)
    if not FRAMES <= source_frames <= 600 * FPS:
        raise ValueError("Duración del crudo fuera del límite")
    scenes = props["scenes"]
    if not 1 <= len(scenes) <= 12:
        raise ValueError("Cantidad de escenas fuera del contrato")
    total = 0
    for scene in scenes:
        start, end = scene["source_start_frame"], scene["source_end_frame"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= source_frames:
            raise ValueError("Corte fuera del crudo")
        total += end - start
    if total != FRAMES:
        raise ValueError("El montaje no suma 900 fotogramas")
    previous = 0
    for cue in props["ad"]["captions"]:
        start, end, caption = cue["start_frame"], cue["end_frame"], cue["text"]
        if (
            type(start) is not int
            or type(end) is not int
            or not previous <= start < end <= FRAMES
            or not isinstance(caption, str)
            or not 0 < len(caption) <= 240
            or len(caption.splitlines()) != 1
        ):
            raise ValueError("Subtítulo inválido o fuera de secuencia")
        previous = end
    return order, props, source_frames


def srt_time(frame):
    milliseconds = (frame * 1000 + FPS // 2) // FPS
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"00:{minutes:02}:{seconds:02},{milliseconds:03}"


def timeline(props, package, source_frames):
    import opentimelineio as otio

    def span(start, length):
        return otio.opentime.TimeRange(
            otio.opentime.RationalTime(start, FPS), otio.opentime.RationalTime(length, FPS)
        )

    def clip(path, start, length, available):
        return otio.schema.Clip(
            name=path.stem,
            source_range=span(start, length),
            media_reference=otio.schema.ExternalReference(
                target_url=path.as_uri(), available_range=span(0, available)
            ),
        )

    cut = otio.schema.Timeline(name="Anuncio de Studio")
    picture = otio.schema.Track(name="Imagen", kind="Video")
    voice = otio.schema.Track(name="Voz original", kind="Audio")
    bed = otio.schema.Track(name="Música de catálogo", kind="Audio")
    cut.tracks.extend((picture, voice, bed))
    offset = 0
    for scene in props["scenes"]:
        start = scene["source_start_frame"]
        length = scene["source_end_frame"] - start
        picture.append(clip(package / "imagen-edicion.mov", start, length, source_frames))
        voice_length = min(length, max(0, 26 * FPS - offset))
        if voice_length:
            voice.append(clip(package / "voz-original.wav", start, voice_length, source_frames))
        if voice_length < length:
            voice.append(otio.schema.Gap(source_range=span(0, length - voice_length)))
        if label := scene.get("label"):
            cut.tracks.markers.append(
                otio.schema.Marker(
                    name=label["text"],
                    marked_range=span(
                        offset + label["start_frame"], label["end_frame"] - label["start_frame"]
                    ),
                )
            )
        offset += length
    bed.append(clip(package / "musica-catalogo.wav", 0, FRAMES, FRAMES))
    cut.tracks.markers.append(
        otio.schema.Marker(
            name=f"{props['ad']['brand']} · {props['ad']['cta']}",
            marked_range=span(26 * FPS, 4 * FPS),
        )
    )
    return otio.adapters.write_to_string(cut, "otio_json")


def transcode(ffmpeg, source, destination, options):
    subprocess.run(
        [ffmpeg, "-v", "error", "-nostdin", "-n", "-i", str(source), *options, str(destination)],
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=900,
    )


def prepare(intake, direction, media_root, package, *, ffmpeg="ffmpeg"):
    order, props, source_frames = validated_cut(intake, direction)
    if package.exists():
        verify_package(order, direction, package)
        return json.loads(read_regular(package / "traspaso.json", LIMIT))
    video, audio = (media_root / path for path in source_paths(intake))
    music = media_root / MUSIC_PATH
    expected = {
        video: {key: order["media"][key] for key in ("sha256", "bytes")},
        audio: {key: intake["audio"][key] for key in ("sha256", "bytes")},
        music: inspect_file(music, 10 * 1024**2),
    }
    if expected[music]["sha256"] != MUSIC_SHA256 or any(
        inspect_file(path) != metadata for path, metadata in expected.items() if path != music
    ):
        raise ValueError("Los medios no coinciden con la ingesta o el catálogo")
    otio = timeline(props, package, source_frames)
    with tempfile.TemporaryDirectory(prefix=".resolve-", dir=package.parent) as staging:
        work = Path(staging) / "paquete"
        work.mkdir()
        receipt = write_package(
            ffmpeg,
            video,
            audio,
            music,
            work,
            otio,
            source_frames,
            props,
            expected,
            order["order_id"],
            direction,
        )
        work.rename(package)
    return receipt


def write_package(
    ffmpeg, video, audio, music, work, otio, source_frames, props, expected, order_id, direction
):
    transcode(
        ffmpeg,
        video,
        work / "imagen-edicion.mov",
        [
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-vf",
            "fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
            "-frames:v",
            str(source_frames),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "4",
            "-crf",
            "19",
            "-maxrate",
            "16M",
            "-bufsize",
            "32M",
            "-g",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            "-movflags",
            "+faststart",
        ],
    )
    shutil.copyfile(audio, work / "voz-original.wav")
    transcode(
        ffmpeg,
        music,
        work / "musica-catalogo.wav",
        ["-map", "0:a:0", "-t", "30", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s24le"],
    )
    if any(inspect_file(path) != metadata for path, metadata in expected.items()):
        raise ValueError("Un medio cambió mientras se preparaba el montaje")
    captions = [
        f"{i}\n{srt_time(cue['start_frame'])} --> {srt_time(cue['end_frame'])}\n{cue['text']}\n"
        for i, cue in enumerate(props["ad"]["captions"], 1)
    ]
    (work / "montaje.otio").write_text(otio, encoding="utf-8")
    (work / "subtitulos.srt").write_text("\n".join(captions), encoding="utf-8")
    receipt = {
        "order_id": order_id,
        "direction_sha256": hashlib.sha256(canonical(direction)).hexdigest(),
        "soundtrack_sha256": MUSIC_SHA256,
        "files": package_fingerprints(work),
    }
    (work / "traspaso.json").write_bytes(canonical(receipt) + b"\n")
    return receipt


def package_fingerprints(package):
    return {
        name: inspect_file(package / name, IMAGE_LIMIT if name == "imagen-edicion.mov" else 1024**3)
        for name in PACKAGE_FILES
    }


def verify_package(order, direction, package):
    receipt = json.loads(read_regular(package / "traspaso.json", LIMIT))
    cut_sha = hashlib.sha256(canonical(direction)).hexdigest()
    if (
        receipt["order_id"] != order["order_id"]
        or receipt["direction_sha256"] != cut_sha
        or receipt["soundtrack_sha256"] != MUSIC_SHA256
        or receipt["files"].keys() != PACKAGE_FILES
    ):
        raise ValueError("El traspaso pertenece a otro montaje")
    if package_fingerprints(package) != receipt["files"]:
        raise ValueError("Los insumos de edición cambiaron")
    return cut_sha


def open_resolve(intake, direction, media_root, package):
    import DaVinciResolveScript as script

    order, _, _ = validated_cut(intake, direction)
    revision = verify_package(order, direction, package)
    resolve = script.scriptapp("Resolve")
    if not resolve:
        raise RuntimeError("Resolve Studio no está abierto o el scripting local está desactivado")
    name = f"Studio {order['order_id']} {revision[:8]}"
    manager = resolve.GetProjectManager()
    project = manager.CreateProject(name)
    if not project:
        raise RuntimeError(
            "Resolve no creó el proyecto; abre el proyecto existente si ya se importó"
        )
    for key, value in (
        ("timelineResolutionWidth", "1080"),
        ("timelineResolutionHeight", "1920"),
        ("timelineFrameRate", "30"),
    ):
        if not project.SetSetting(key, value):
            raise RuntimeError(f"Resolve rechazó el ajuste {key}")
    cut = project.GetMediaPool().ImportTimelineFromFile(str(package / "montaje.otio"))
    if not cut or cut.GetTrackCount("video") != 1 or cut.GetTrackCount("audio") != 2:
        raise RuntimeError("Resolve no importó la pista de imagen y las dos pistas de audio")
    if cut.GetEndFrame() - cut.GetStartFrame() != FRAMES:
        raise RuntimeError("Resolve alteró la duración del montaje")
    if (
        not project.SetCurrentTimeline(cut)
        or not manager.SaveProject()
        or not resolve.OpenPage("fairlight")
    ):
        raise RuntimeError("Resolve no pudo guardar y abrir la mezcla en Fairlight")
    return {"proyecto": name, "fotogramas": FRAMES, "pistas_audio": 2}


def audit_export(intake, direction, media_root, package):
    from production_elena import review_video

    order, props, _ = validated_cut(intake, direction)
    cut_sha = verify_package(order, direction, package)
    export = package / "exportacion.mp4"
    digest = inspect_file(export)["sha256"]
    music = media_root / MUSIC_PATH
    if inspect_file(music, 10 * 1024**2)["sha256"] != MUSIC_SHA256:
        raise ValueError("Pista de referencia fuera del catálogo")
    evidence = {
        "order_id": order["order_id"],
        "direction_sha256": cut_sha,
        "master_sha256": digest,
        "fase": "edicion_externa",
    }
    target = package / f"dictamen-{digest}.json"
    if target.exists():
        saved = json.loads(read_regular(target, LIMIT))
        if any(saved[key] != value for key, value in evidence.items()):
            raise ValueError("Dictamen ajeno a esta exportación")
        return saved
    request_id = str(
        uuid.uuid5(uuid.UUID(order["order_id"]), f"edicion-externa:{cut_sha}:{digest}")
    )
    result = {
        **evidence,
        **review_video(
            export,
            digest,
            props,
            direction["escaleta"]["angulo"],
            render_plan(order),
            "1080p",
            request_id,
            music_reference=music,
        ),
        "modelo": "openai/gpt-6.1-sol",
        "runtime": "openclaw",
    }
    if inspect_file(music, 10 * 1024**2)["sha256"] != MUSIC_SHA256:
        raise ValueError("Cambió la pista de catálogo durante la revisión")
    publish(target, canonical(result) + b"\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = {"preparar": prepare, "abrir": open_resolve, "revisar": audit_export}
    parser.add_argument("accion", choices=actions)
    for name in ("ingesta", "escaleta", "medios", "destino"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    intake, direction = (
        json.loads(read_regular(path, LIMIT)) for path in (args.ingesta, args.escaleta)
    )
    result = actions[args.accion](
        intake, direction, args.medios.resolve(strict=True), args.destino.absolute()
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
