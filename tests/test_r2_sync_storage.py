"""Publicación local: validadores portables y pruebas Linux reales sin red/NAS."""

import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "r2_sync_storage", ROOT / "docker/r2-sync-consumer/sync_storage.py"
)
STORAGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = STORAGE
SPEC.loader.exec_module(STORAGE)
DATA = b"asset-publicado-localmente\n"


def job(data=DATA):
    job_id = "22222222-2222-4222-8222-222222222222"
    asset = "11111111-1111-4111-8111-111111111111"
    return {
        "job_id": job_id,
        "asset_id": asset,
        "project_slug": "proyecto_01",
        "extension": "mp4",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "object_key": f"ingest/{job_id}/{asset}.mp4",
    }


class ValidationTests(unittest.TestCase):
    def test_contrato_snapshot_no_normaliza_y_exige_politica(self):
        source = job()
        snapshot = STORAGE.validate_job(source, 1024)
        source["project_slug"] = "otro"
        self.assertEqual(snapshot.project_slug, "proyecto_01")
        for policy in (None, True, 0, -1, 1.0, 2**53):
            with self.assertRaises(STORAGE.StorageError):
                STORAGE.validate_job(job(), policy)

    def test_ids_slug_extension_tamano_hash_y_clave_rechazados(self):
        changes = [
            {"job_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
            {"asset_id": "11111111-1111-1111-8111-111111111111"},
            {"project_slug": "../otro"},
            {"project_slug": "con"},
            {"project_slug": "_inicio"},
            {"project_slug": "a" * 64},
            {"extension": ".mp4"},
            {"extension": "x/../../y"},
            {"extension": "A"},
            {"bytes": True},
            {"bytes": 0},
            {"bytes": 2**53},
            {"sha256": "A" * 64},
            {"object_key": "https://otro/asset"},
            {"extra": 1},
        ]
        for change in changes:
            value = job()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(STORAGE.StorageError):
                STORAGE.validate_job(value, 1024)

    def test_root_proof_no_acepta_marcador_arbitrario_o_integers_falsos(self):
        for values in (
            (True, 2, ".owner", "a" * 64),
            (1, 0, ".owner", "a" * 64),
            (1, 2, "../owner", "a" * 64),
            (1, 2, "NUL.txt", "a" * 64),
            (1, 2, ".owner", "A" * 64),
        ):
            with self.assertRaises(STORAGE.StorageError):
                STORAGE.RootProof(*values)


@unittest.skipUnless(
    sys.platform.startswith("linux"),
    "Requiere Linux real, dir_fd y /proc/self/fd; Windows no acredita POSIX.",
)
class LinuxStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agency-storage-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.media = self.base / "media"
        self.raw = self.media / "proyectos/proyecto_01/01_raw"
        self.raw.mkdir(parents=True)
        self.marker = self.media / ".owner"
        self.marker.write_bytes(b"raiz-propia-fixture")
        info = self.media.stat()
        self.proof = STORAGE.RootProof(
            info.st_dev,
            info.st_ino,
            self.marker.name,
            hashlib.sha256(self.marker.read_bytes()).hexdigest(),
        )
        self.storage = STORAGE.SyncStorage(self.media, self.proof, max_object_bytes=1024)
        self.destination = self.raw / (job()["asset_id"] + ".mp4")

    def assert_no_partials(self, raw=None):
        self.assertEqual(list((raw or self.raw).glob(".sync-*.partial")), [])

    def test_linkat_proc_fd_real_mismo_inodo_y_exito_idempotente(self):
        seen = []
        native = os.link

        def observe(source, destination, **kwargs):
            self.assertTrue(source.startswith("/proc/self/fd/"))
            self.assertTrue(kwargs["follow_symlinks"])
            self.assertIsInstance(kwargs["dst_dir_fd"], int)
            info = os.fstat(int(source.rsplit("/", 1)[1]))
            seen.append((info.st_dev, info.st_ino))
            return native(source, destination, **kwargs)

        with patch.object(STORAGE.os, "link", side_effect=observe):
            result = self.storage.publish(job(), iter((DATA[:4], DATA[4:])))
        self.assertTrue(result.created)
        self.assertFalse(result.idempotent)
        info = self.destination.stat()
        self.assertEqual((info.st_dev, info.st_ino), seen[0])
        self.assertEqual(info.st_nlink, 1)
        self.assertEqual(self.destination.read_bytes(), DATA)
        self.assert_no_partials()
        result = self.storage.publish(job(), iter((DATA,)))
        self.assertFalse(result.created)
        self.assertTrue(result.idempotent)
        self.assertEqual(self.destination.stat().st_ino, info.st_ino)
        self.assert_no_partials()

    def test_escrituras_cortas_son_completadas(self):
        native = os.write
        with patch.object(STORAGE.os, "write", side_effect=lambda fd, data: native(fd, data[:3])):
            self.storage.publish(job(), iter((DATA,)))
        self.assertEqual(self.destination.read_bytes(), DATA)

    def test_cache_nfs_de_nlink_se_revalida_abriendo_el_destino(self):
        native = os.stat
        cached = 0

        def stale_name(path, **kwargs):
            nonlocal cached
            info = native(path, **kwargs)
            if path == self.destination.name and kwargs.get("dir_fd") is not None and not cached:
                cached += 1
                values = list(info)
                values[3] = 2
                return os.stat_result(values)
            return info

        with patch.object(STORAGE.os, "stat", side_effect=stale_name):
            result = self.storage.publish(job(), iter((DATA,)))
        self.assertEqual(cached, 1)
        self.assertTrue(result.created)
        self.assertEqual(self.destination.stat().st_nlink, 1)
        self.assertEqual(self.destination.read_bytes(), DATA)

    def test_hardlink_real_con_bytes_identicos_sigue_siendo_conflicto(self):
        other = self.base / "mismos-bytes"
        other.write_bytes(DATA)
        os.link(other, self.destination)
        with self.assertRaises(STORAGE.StorageError) as failure:
            self.storage.publish(job(), iter((DATA,)))
        self.assertEqual(failure.exception.code, "CONFLICTO_DESTINO")
        self.assertFalse(failure.exception.published)
        self.assertEqual(self.destination.stat().st_nlink, 2)
        self.assertEqual(other.read_bytes(), DATA)
        self.assert_no_partials()

    def test_nfs_revalida_despues_de_cerrar_el_descriptor_de_escritura(self):
        import fcntl

        native_open, native_stat = os.open, os.fstat
        descriptors = {}

        def observe_open(path, flags, *args, **kwargs):
            fd = native_open(path, flags, *args, **kwargs)
            if isinstance(path, str) and path.startswith(".sync-"):
                descriptors["writer"] = fd
            if path == self.destination.name:
                descriptors["reader"] = fd
            return fd

        def cached_attributes(fd):
            info = native_stat(fd)
            if fd == descriptors.get("reader"):
                try:
                    writing = fcntl.fcntl(descriptors["writer"], fcntl.F_GETFL) & os.O_ACCMODE
                except OSError:
                    writing = os.O_RDONLY
                if writing == os.O_RDWR:
                    values = list(info)
                    values[3] = 2
                    return os.stat_result(values)
            return info

        with (
            patch.object(STORAGE.os, "open", side_effect=observe_open),
            patch.object(STORAGE.os, "fstat", side_effect=cached_attributes),
        ):
            result = self.storage.publish(job(), iter((DATA,)))
        self.assertTrue(result.created)
        self.assertEqual(self.destination.stat().st_nlink, 1)

    def test_truncado_oversize_hash_y_chunk_mutable_no_publican(self):
        for chunks, metadata in (
            ([DATA[:-1]], job()),
            ([DATA + b"x"], job()),
            ([DATA], dict(job(), sha256="0" * 64)),
            ([bytearray(DATA)], job()),
        ):
            with self.subTest(metadata=metadata), self.assertRaises(STORAGE.StorageError):
                self.storage.publish(metadata, iter(chunks))
            self.assertFalse(self.destination.exists())
            self.assert_no_partials()

    def test_escritura_cero_y_excepcion_del_iterador_limpian_sin_destino(self):
        with (
            patch.object(STORAGE.os, "write", return_value=0),
            self.assertRaises(STORAGE.StorageError),
        ):
            self.storage.publish(job(), iter((DATA,)))
        self.assert_no_partials()

        def broken():
            yield DATA[:2]
            raise RuntimeError("error de fuente controlado")

        with self.assertRaises(STORAGE.StorageError):
            self.storage.publish(job(), broken())
        self.assertFalse(self.destination.exists())
        self.assert_no_partials()

    def test_destino_distinto_symlink_hardlink_y_fifo_son_conflictos(self):
        outside = self.base / "fuera"
        outside.write_bytes(b"conservar")
        setups = (
            lambda: self.destination.write_bytes(b"conflicto"),
            lambda: self.destination.symlink_to(outside),
            lambda: os.link(outside, self.destination),
            lambda: os.mkfifo(self.destination),
        )
        for create in setups:
            create()
            with self.assertRaises(STORAGE.StorageError):
                self.storage.publish(job(), iter((DATA,)))
            self.assertEqual(outside.read_bytes(), b"conservar")
            self.assert_no_partials()
            self.destination.unlink()

    def test_colision_de_nombre_parcial_no_adopta_archivo_ajeno(self):
        partial = self.raw / (".sync-" + "0" * 32 + ".partial")
        partial.write_bytes(b"parcial ajeno")
        before = partial.stat()
        with (
            patch.object(STORAGE.secrets, "token_hex", return_value="0" * 32),
            self.assertRaises(STORAGE.StorageError),
        ):
            self.storage.publish(job(), iter((DATA,)))
        self.assertEqual(partial.read_bytes(), b"parcial ajeno")
        self.assertEqual(partial.stat().st_ino, before.st_ino)
        self.assertFalse(self.destination.exists())

    def test_marcador_symlink_y_hardlink_no_acreditan_raiz(self):
        outside = self.base / "marcador-ajeno"
        outside.write_bytes(self.marker.read_bytes())
        self.marker.unlink()
        self.marker.symlink_to(outside)
        with self.assertRaises(STORAGE.StorageError):
            self.storage.publish(job(), iter((DATA,)))
        self.marker.unlink()
        os.link(outside, self.marker)
        with self.assertRaises(STORAGE.StorageError):
            self.storage.publish(job(), iter((DATA,)))
        self.assertFalse(self.destination.exists())
        self.assert_no_partials()

    def test_no_crea_proyectos_ni_admite_padre_symlink(self):
        value = job()
        value["project_slug"] = "ausente"
        with self.assertRaises(STORAGE.StorageError):
            self.storage.publish(value, iter((DATA,)))
        self.assertFalse((self.media / "proyectos/ausente").exists())
        outside = self.base / "otro"
        outside.mkdir()
        self.raw.rename(self.raw.with_name("raw-anterior"))
        self.raw.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(STORAGE.StorageError):
            self.storage.publish(job(), iter((DATA,)))
        self.assertEqual(list(outside.iterdir()), [])

    def test_reemplazo_de_padre_durante_stream_no_desvia_publicacion(self):
        previous = self.media / "proyectos-anterior"

        def replace_parent():
            (self.media / "proyectos").rename(previous)
            self.raw.mkdir(parents=True)
            yield DATA

        with self.assertRaises(STORAGE.StorageError):
            self.storage.publish(job(), replace_parent())
        self.assertEqual(list(self.raw.iterdir()), [])
        old_raw = previous / "proyecto_01/01_raw"
        self.assertFalse((old_raw / self.destination.name).exists())
        self.assert_no_partials(old_raw)

    def test_parcial_cambiado_antes_de_link_no_se_elimina_como_propio(self):
        outside = self.base / "ajeno"
        outside.write_bytes(b"no tocar")
        partials = []

        def replace_partial():
            partial = next(self.raw.glob(".sync-*.partial"))
            partials.append(partial)
            partial.rename(self.raw / "movido")
            partial.symlink_to(outside)
            yield DATA

        with self.assertRaises(STORAGE.StorageError):
            self.storage.publish(job(), replace_partial())
        self.assertEqual(outside.read_bytes(), b"no tocar")
        self.assertTrue(partials[0].is_symlink())
        self.assertFalse(self.destination.exists())

    def test_link_desde_fd_no_sustituye_bytes_si_cambia_nombre_del_parcial(self):
        native = os.link

        def replace_after_check(source, destination, **kwargs):
            partial = next(self.raw.glob(".sync-*.partial"))
            partial.rename(self.raw / "parcial-movido")
            partial.write_bytes(b"bytes ajenos")
            return native(source, destination, **kwargs)

        with (
            patch.object(STORAGE.os, "link", side_effect=replace_after_check),
            self.assertRaises(STORAGE.StorageError) as failure,
        ):
            self.storage.publish(job(), iter((DATA,)))
        self.assertTrue(failure.exception.published)
        self.assertEqual(self.destination.read_bytes(), DATA)

    def test_error_despues_de_link_conserva_destino_publicado(self):
        native = os.fsync
        calls = 0

        def fail_directory(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fsync simulado posterior")
            return native(fd)

        with (
            patch.object(STORAGE.os, "fsync", side_effect=fail_directory),
            self.assertRaises(STORAGE.StorageError) as failure,
        ):
            self.storage.publish(job(), iter((DATA,)))
        self.assertTrue(failure.exception.published)
        self.assertEqual(self.destination.read_bytes(), DATA)
        self.assert_no_partials()

    def test_prueba_externa_y_marcador_se_revalidan(self):
        bad = STORAGE.RootProof(
            self.proof.device, self.proof.inode + 1, self.marker.name, self.proof.marker_sha256
        )
        with self.assertRaises(STORAGE.StorageError):
            STORAGE.SyncStorage(self.media, bad, max_object_bytes=1024)
        self.marker.write_bytes(b"cambio posterior")
        with self.assertRaises(STORAGE.StorageError):
            self.storage.publish(job(), iter((DATA,)))
        self.assert_no_partials()

    def test_snapshot_del_job_no_cambia_destino_durante_iteracion(self):
        value = job()

        def mutate():
            value["project_slug"] = "otro"
            value["asset_id"] = "33333333-3333-4333-8333-333333333333"
            yield DATA

        self.storage.publish(value, mutate())
        self.assertEqual(self.destination.read_bytes(), DATA)

    def test_fds_cerrados_en_exitos_y_fallos(self):
        before = len(list(Path("/proc/self/fd").iterdir()))
        for _ in range(4):
            with self.assertRaises(STORAGE.StorageError):
                self.storage.publish(job(), iter((b"x",)))
            self.storage.publish(job(), iter((DATA,)))
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)


if __name__ == "__main__":
    unittest.main()
