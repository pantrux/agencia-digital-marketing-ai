import hashlib
from pathlib import PurePosixPath

import pytest

from sre import backup_321_verify_openclaw_restore as verifier
from sre import openclaw_backup as archive
from sre import prepare_openclaw_staging as prepare


def archive_fixture(model=None):
    files = {
        f"{agent}/config.json": archive.canonical(
            {"id": agent, "model": model if agent == "agency-account" and model else expected}
        )
        for agent, expected in prepare.AGENTS.items()
    }
    files["agency-account/MEMORY.md"] = b"Nota de ensayo\n"
    manifest = archive.canonical(
        {
            "schema": 1,
            "owner": archive.OWNER,
            "scope": "agency-state",
            "files": [
                {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for path, data in sorted(files.items())
            ],
        }
    )
    frame = archive.pack_tar(manifest, files, "agency-state")
    outer_manifest = f"{archive.digest(frame)}  {archive.ARCHIVE}\n".encode()
    capture = {
        "archive_size": len(frame),
        "archive_sha256": archive.digest(frame),
        "manifest_sha256": archive.digest(manifest),
        "files": len(files),
    }
    return frame, outer_manifest, capture


def test_recupera_cinco_configuraciones_y_una_memoria_desde_el_snapshot(monkeypatch):
    frame, manifest, capture = archive_fixture()
    monkeypatch.setattr(archive, "zstd", lambda data, *, decompress: data)
    stored = {"SHA256SUMS": manifest, archive.ARCHIVE: frame}

    def stream(path):
        yield stored[path.name]

    result = verifier.verify_archive(stream, archive.digest(manifest), capture)
    assert result == {"agentes": 5, "memorias": 1, "archivo_sha256": archive.digest(frame)}


def test_rechaza_manifiesto_del_snapshot_ajeno_antes_de_leer_el_tar():
    _, manifest, capture = archive_fixture()

    def stream(path):
        assert path == PurePosixPath("/SHA256SUMS")
        yield manifest

    with pytest.raises(ValueError, match="MANIFIESTO_OPENCLAW"):
        verifier.verify_archive(stream, "0" * 64, capture)


def test_rechaza_un_modelo_ajeno_aunque_coincidan_las_huellas(monkeypatch):
    frame, manifest, capture = archive_fixture(model="openai/otro")
    monkeypatch.setattr(archive, "zstd", lambda data, *, decompress: data)
    stored = {"SHA256SUMS": manifest, archive.ARCHIVE: frame}

    def stream(path):
        yield stored[path.name]

    with pytest.raises(ValueError, match="OPENCLAW_PROYECCION"):
        verifier.verify_archive(stream, archive.digest(manifest), capture)


def test_acota_el_tar_antes_de_leer_otro_bloque():
    _, manifest, capture = archive_fixture()

    def stream(path):
        if path.name == "SHA256SUMS":
            yield manifest
            return
        yield b"a" * (capture["archive_size"] + 1)
        pytest.fail("La lectura continuó después de superar la cota acreditada")

    with pytest.raises(ValueError, match="ARCHIVO_OPENCLAW_COTA"):
        verifier.verify_archive(stream, archive.digest(manifest), capture)
