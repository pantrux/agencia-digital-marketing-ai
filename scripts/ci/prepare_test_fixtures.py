"""Prepara entradas públicas fijadas para las pruebas de contratos."""

import hashlib
import json
import re
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "docker/postgres-immich/musl-build-inputs.json"
MANIFEST_SHA256 = "8d5a521ef8b2827f5198b9dcb5148c05f0aad743c7ce39912c75491139f5f745"
VECTORCHORD_URL = "https://codeload.github.com/supervc-stack/VectorChord/tar.gz/"


def prepare_vectorchord():
    manifest = (ROOT / MANIFEST).read_bytes()
    if hashlib.sha256(manifest).hexdigest() != MANIFEST_SHA256:
        raise ValueError("El manifiesto de VectorChord no coincide con la huella fijada.")
    source = json.loads(manifest)["vectorchord_source"]
    if (
        not re.fullmatch(r"[a-f0-9]{40}", source["revision"])
        or source["url"] != VECTORCHORD_URL + source["revision"]
    ):
        raise ValueError("El origen de VectorChord no corresponde a la revisión fijada.")
    destination = (
        ROOT
        / ".tools/postgres-immich-build"
        / f"vendor-{MANIFEST_SHA256[:16]}"
        / "downloads/vectorchord-source.tar.gz"
    )
    if not destination.resolve().is_relative_to(ROOT):
        raise ValueError("La ruta de los insumos sale del proyecto.")

    size = source["size"]
    existing = destination.exists()
    if existing:
        with destination.open("rb") as stream:
            archive = stream.read(size + 1)
    else:
        with urlopen(source["url"], timeout=20) as response:
            archive = response.read(size + 1)
    if len(archive) != size or hashlib.sha256(archive).hexdigest() != source["sha256"]:
        raise ValueError("El archivo de VectorChord no coincide con el tamaño y SHA-256 fijados.")
    if not existing:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(archive)
    print(f"Archivo público de VectorChord verificado ({size} bytes).")


if __name__ == "__main__":
    prepare_vectorchord()
