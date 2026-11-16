"""Deriva el archivo TypeScript corregido a partir de la fuente n8n fijada."""

import argparse
import hashlib
import json
import stat
from pathlib import Path

RELATIVE = Path("packages/@n8n/backend-common/src/utils/flatted-async.ts")
SOURCE_SHA = "86ef3a641fde6231eaf4d8b70eb8298aed0a71f7e1bbf5f094353b002ccfc20e"
PATCH_SHA = "c6105456d189f94252eda827ea68a6ed47f277040f6615723f88a3b85eeb55fd"
OUTPUT_SHA = "09ef205769d914b509ad625daf532cfb02900ada03ce930feed4047d01cd3b6f"
PATCH = Path(__file__).with_name("flatted-async-resolver-v1.ts")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_pinned(path, expected):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not 0 < info.st_size <= 8192
        or getattr(info, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("Entrada inesperada")
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) != info.st_size or sha(raw) != expected:
        raise ValueError("SHA-256 de entrada inesperado")
    return raw


def derive(source, fragment):
    if (sha(source), sha(fragment)) != (SOURCE_SHA, PATCH_SHA):
        raise ValueError("SHA-256 de entrada inesperado")
    begin = source.index(b"//#region Flatted reference resolution")
    end = source.index(b"/**\n * Parse a flatted JSON string asynchronously", begin)
    result = source[:begin] + fragment + source[end:]
    if sha(result) != OUTPUT_SHA:
        raise ValueError("SHA-256 de salida inesperado")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Archivo nuevo para el overlay")
    args = parser.parse_args()
    result = derive(
        read_pinned(args.source_root / RELATIVE, SOURCE_SHA), read_pinned(PATCH, PATCH_SHA)
    )
    if args.output is not None:
        with args.output.open("xb") as stream:
            stream.write(result)
    print(
        json.dumps(
            {
                "relative": RELATIVE.as_posix(),
                "source_sha256": SOURCE_SHA,
                "fragment_sha256": PATCH_SHA,
                "output_sha256": OUTPUT_SHA,
                "output_bytes": len(result),
                "written": args.output is not None,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
