"""Prueba real TextClip, composición y decodificación con MoviePy derivado y Pillow 13."""

import argparse
import hashlib
import json
import os
import sys
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--font-sha256", required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffmpeg-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    require(
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "PYTHON_ISOLATION"
    )
    require(Path(__file__).resolve().parent == Path.cwd().resolve(), "FIXTURE_WORKDIR")
    require(args.site.is_absolute() and args.site.is_dir(), "SITE_PATH")
    require(
        args.output_dir.is_absolute()
        and args.output_dir.is_dir()
        and not any(args.output_dir.iterdir()),
        "OUTPUT_DIR",
    )
    for material, expected in ((args.font, args.font_sha256), (args.ffmpeg, args.ffmpeg_sha256)):
        require(
            material.is_absolute() and material.is_file() and digest(material) == expected,
            "MATERIAL_PIN",
        )
    sys.path.insert(0, str(args.site))
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["FFMPEG_BINARY"] = str(args.ffmpeg)
    os.environ["IMAGEIO_FFMPEG_EXE"] = str(args.ffmpeg)

    require(
        version("moviepy") == "2.2.1+agency1" and version("Pillow") == "13.0.0", "FIXTURE_VERSIONS"
    )
    import numpy as np
    from moviepy import ColorClip, CompositeVideoClip, TextClip, VideoFileClip, config
    from PIL import __version__ as pillow_version

    require(
        pillow_version == "13.0.0" and config.FFMPEG_BINARY == str(args.ffmpeg), "FIXTURE_IMPORTS"
    )
    require(not config.DOTENV, "FIXTURE_DOTENV")
    output = args.output_dir / "moviepy-pillow-composite.mp4"
    with ExitStack() as clips:
        background = clips.enter_context(ColorClip((320, 180), color=(17, 31, 47), duration=1))
        subtitle = clips.enter_context(
            TextClip(
                font=str(args.font),
                text="Agencia QA\n3.14",
                font_size=32,
                color="white",
                stroke_color="black",
                stroke_width=1,
                method="caption",
                size=(280, None),
                text_align="center",
                duration=0.5,
            )
            .with_start(0.25)
            .with_position(("center", "center"))
        )
        composite = clips.enter_context(CompositeVideoClip([background, subtitle]))
        base = background.get_frame(0)
        require(np.array_equal(composite.get_frame(0.125), base), "TEXT_BEFORE_START")
        changed = np.count_nonzero(np.any(composite.get_frame(0.5) != base, axis=2))
        require(changed > 500 and np.array_equal(composite.get_frame(0.875), base), "TEXT_FRAME")
        composite.write_videofile(
            str(output), fps=12, codec="libx264", audio=False, threads=1, logger=None
        )
        video = clips.enter_context(VideoFileClip(str(output), audio=False))
        require(
            video.size == [320, 180] and video.fps == 12 and abs(video.duration - 1) <= 0.09,
            "MP4_PROBE",
        )
        require(
            np.count_nonzero(np.all(video.get_frame(0.5) > 180, axis=2)) > 250
            and not np.any(video.get_frame(0.125) > 180),
            "MP4_TEXT",
        )
    require(
        digest(args.font) == args.font_sha256 and digest(args.ffmpeg) == args.ffmpeg_sha256,
        "MATERIAL_CHANGED",
    )
    print(
        json.dumps(
            {
                "moviepy": version("moviepy"),
                "pillow": pillow_version,
                "changed_pixels": int(changed),
                "resolution": [320, 180],
                "fps": 12,
                "duration_seconds": video.duration,
                "output_bytes": output.stat().st_size,
                "output_sha256": digest(output),
                "ffmpeg_sha256": args.ffmpeg_sha256,
                "font_sha256": args.font_sha256,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
