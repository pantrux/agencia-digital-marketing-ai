"""Deriva la codificación de vistas previas de fotos para un ensayo aislado de Immich."""

import argparse
import hashlib
import json
from pathlib import Path

PATCHES = {
    "media.repository.js": (
        "ffe55645bc6387e23720e43582ed6724d0edf6b178c143d4615ca2ac7ed220a4",
        b"            progressive: options.progressive,\n        })\n            .toFile(output);",
        b"            progressive: options.progressive,\n"
        b"            ...(options.agencyLosslessPreview === true && options.format === 'webp'"
        b" ? { lossless: true } : {}),\n        })\n            .toFile(output);",
    ),
    "media.service.js": (
        "81937bc0dc487e28d339e0bfa4676876a9f7f2c27faf848105e3db28ca6608d8",
        b"const previewOptions = { ...image.preview, ...baseOptions, format: previewFormat };",
        b"const previewOptions = { ...image.preview, ...baseOptions, format: previewFormat,"
        b" agencyLosslessPreview: previewFormat === 'webp' };",
    ),
}


def derive(name, raw):
    pin, before, after = PATCHES[name]
    if hashlib.sha256(raw).hexdigest() != pin or raw.count(before) != 1:
        raise ValueError(f"La fuente de Immich no coincide con la revisión acreditada: {name}")
    return raw.replace(before, after, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    outputs = {name: derive(name, (args.source / name).read_bytes()) for name in PATCHES}
    args.output.mkdir(mode=0o700)
    for name, raw in outputs.items():
        with (args.output / name).open("xb") as handle:
            handle.write(raw)
    print(json.dumps({name: hashlib.sha256(raw).hexdigest() for name, raw in outputs.items()}))


if __name__ == "__main__":
    main()
