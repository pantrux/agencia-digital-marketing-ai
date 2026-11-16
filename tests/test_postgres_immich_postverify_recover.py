"""Excepciones de lectura exactas, conservando controles generales y sin red."""

import copy
import hashlib
import importlib.util
import io
import stat
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def remote():
    return load("docker/postgres-immich/postverify_recover_remote.py", "recover_exact_contract")


class ExactReader(unittest.TestCase):
    def setUp(self):
        self.remote = remote()
        self.name = "usr/local/lib/postgresql/vchord.so"
        self.data = b"contenido sintetico"
        self.expected = copy.deepcopy(self.remote.SPECIAL_FILES[self.name])
        self.expected.update(size=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())

    def read(self, *, name=None, mode=0o700, inode=None, nlink=1, data=None):
        name = self.name if name is None else name
        payload = self.data if data is None else data
        info = types.SimpleNamespace(
            st_mode=stat.S_IFREG | mode,
            st_uid=0,
            st_gid=0,
            st_nlink=nlink,
            st_size=len(payload),
            st_dev=self.expected["device"],
            st_ino=self.expected["inode"] if inode is None else inode,
        )

        class Stream(io.BytesIO):
            def fileno(self):
                return 123

        class Normal:
            @staticmethod
            def identity(value):
                return {"device": value.st_dev, "inode": value.st_ino}

        with (
            patch.object(self.remote.os, "O_NOFOLLOW", 131072, create=True),
            patch.object(self.remote.os, "O_NONBLOCK", 2048, create=True),
            patch.object(self.remote, "SPECIAL_FILES", {self.name: self.expected}),
            patch.object(self.remote.os, "open", return_value=123),
            patch.object(self.remote.os, "fdopen", return_value=Stream(payload)),
            patch.object(self.remote.os, "fstat", return_value=info),
            patch.object(self.remote.os, "stat", return_value=info),
        ):
            return self.remote.read_owned(Normal(), 99, name.rsplit("/", 1)[-1], logical_path=name)

    def test_0700_solo_en_path_y_metadata_y_sha_observados(self):
        data, row = self.read()
        self.assertEqual(data, self.data)
        self.assertEqual(row["mode"], 0o700)

    def test_0700_en_otro_path_se_rechaza(self):
        with self.assertRaises(ValueError):
            self.read(name="usr/local/lib/postgresql/otra.so")

    def test_mismo_contenido_no_autoriza_otro_inodo_ni_hardlink(self):
        with self.assertRaises(ValueError):
            self.read(inode=self.expected["inode"] + 1)
        with self.assertRaises(ValueError):
            self.read(nlink=2)

    def test_sha_incorrecto_y_modo_cambiado_rechazan(self):
        with self.assertRaises(ValueError):
            self.read(data=b"x" * len(self.data))
        with self.assertRaises(ValueError):
            self.read(mode=0o755)

    def test_archivo_generico_0600_sigue_aceptado(self):
        data, _row = self.read(name="usr/local/share/licenses/notice", mode=0o600)
        self.assertEqual(data, self.data)


_BASE = load("tests/test_postgres_immich_postverify_accept.py", "accept_regression_tests")


class CargoRoleRegression(_BASE.CargoRoles):
    def setUp(self):
        self.remote = remote()


class FramingAndSourceRegression(_BASE.AcceptanceBoundary):
    def setUp(self):
        self.patcher = patch.object(_BASE, "load", return_value=remote())
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()


class RecoveryProfile(unittest.TestCase):
    def test_wrapper_preserva_fallo_y_rechaza_manifiesto_alterado(self):
        import json

        value = remote()
        base = {"historia": "literal"}
        base_sha = value.digest(value.canonical(base))
        receipt = {
            "stage": value.ACCEPT_STAGE,
            "success": False,
            "cid": value.CID,
            "source_sha256": value.ACCEPT_SOURCE,
            "profile_sha256": base_sha,
        }
        failed = value.envelope(receipt)
        manifest = json.loads(
            (ROOT / "docker/postgres-immich/postverify-recovery-files.json").read_bytes()
        )
        profile = {
            "schema_version": 1,
            "cid": value.CID,
            "budget_seconds": 180,
            "maximum_archive_bytes": value.MAX_ARCHIVE,
            "base_profile": base,
            "failed_acceptance": failed,
            "files_manifest": manifest,
            "sources": {
                "local": "a" * 64,
                "remote": "b" * 64,
                "manifest": value.FILES_MANIFEST_SHA,
            },
        }
        with (
            patch.object(value, "ACCEPT_PROFILE", base_sha),
            patch.object(value, "ACCEPT_FAILED", failed["receipt_sha256"]),
            patch.object(value, "validate_base_profile"),
        ):
            value.validate_profile(profile)
            altered = copy.deepcopy(profile)
            altered["files_manifest"]["files"][self_path := "usr/local/lib/postgresql/vchord.so"][
                "inode"
            ] += 1
            self.assertIn(self_path, value.SPECIAL_FILES)
            with self.assertRaises(ValueError):
                value.validate_profile(altered)
            altered = copy.deepcopy(profile)
            altered["failed_acceptance"] = value.envelope(dict(receipt, success=True))
            with self.assertRaises(ValueError):
                value.validate_profile(altered)
        self.assertFalse(profile["failed_acceptance"]["receipt"]["success"])


if __name__ == "__main__":
    unittest.main()
