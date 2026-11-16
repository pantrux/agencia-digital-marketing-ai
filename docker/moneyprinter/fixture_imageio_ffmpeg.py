"""Comprueba una rueda pura instalada y la selección del FFmpeg autorizado."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from unittest.mock import patch


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffmpeg-sha256", required=True)
    args = parser.parse_args()
    require(
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "PYTHON_ISOLATION"
    )
    require(args.site.is_absolute() and args.site.is_dir(), "SITE_PATH")
    require(
        args.ffmpeg.is_absolute()
        and args.ffmpeg.is_file()
        and digest(args.ffmpeg) == args.ffmpeg_sha256,
        "FFMPEG_PIN",
    )
    sys.path.insert(0, str(args.site))
    dist = distribution("imageio-ffmpeg")
    require(
        dist.version == "0.6.0" and dist.metadata["Requires-Python"] == ">=3.9", "DIST_METADATA"
    )
    require("Tag: py3-none-any\n" in dist.read_text("WHEEL"), "WHEEL_TAG")

    import imageio_ffmpeg

    package = args.site / "imageio_ffmpeg"
    require(Path(imageio_ffmpeg.__file__).resolve().parent == package.resolve(), "IMPORT_ORIGIN")
    require(
        {path.name for path in (package / "binaries").iterdir()} == {"README.md", "__init__.py"},
        "BUNDLED_BINARY",
    )
    executable = str(args.ffmpeg)
    os.environ["IMAGEIO_FFMPEG_EXE"] = executable
    require(imageio_ffmpeg.get_ffmpeg_exe() == executable, "EXPLICIT_RESOLUTION")

    check_output = subprocess.check_output
    calls = []

    def bounded_probe(command, **kwargs):
        require(command == [executable, "-version"] and not calls, "FFMPEG_COMMAND")
        calls.append(True)
        return check_output(command, timeout=8, **kwargs)

    with patch.object(subprocess, "check_output", bounded_probe):
        ffmpeg_version = imageio_ffmpeg.get_ffmpeg_version()
    require(
        len(calls) == 1 and re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,79}", ffmpeg_version),
        "FFMPEG_VERSION",
    )
    require(digest(args.ffmpeg) == args.ffmpeg_sha256, "FFMPEG_CHANGED")
    print(
        json.dumps(
            {
                "imageio_ffmpeg": dist.version,
                "ffmpeg_version": ffmpeg_version,
                "ffmpeg_sha256": args.ffmpeg_sha256,
                "explicit_path_verified": True,
                "version_calls": len(calls),
                "bundled_executables": 0,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
