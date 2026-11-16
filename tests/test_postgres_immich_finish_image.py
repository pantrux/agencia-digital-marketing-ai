"""Recuperación de imagen existente: fallback real y lectores acotados, sin red."""

import copy
import importlib.util
import io
import json
import stat
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location(
        "finish_image_contract", ROOT / "docker/postgres-immich/finish_image_remote.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExistingImage(unittest.TestCase):
    def setUp(self):
        self.remote = load()
        self.obs = json.loads(
            (ROOT / "docker/postgres-immich/finish-image-observations.json").read_bytes()
        )
        self.inspection = {
            "id": self.obs["image_id"],
            "descriptor": {
                "digest": self.obs["image_id"],
                "annotations": {"config.digest": self.obs["config_digest"]},
            },
            "user": "70:70",
            "entrypoint": ["docker-entrypoint.sh"],
            "cmd": ["postgres"],
            "os": "linux",
            "architecture": "amd64",
            "labels": {"cl.e-scale.agencia.owner": self.obs["owner"]},
        }
        self.log = (
            "#7 exporting manifest "
            + self.obs["image_id"]
            + " done\n#7 exporting config "
            + self.obs["config_digest"]
            + " done\n"
        )

    def test_fallback_real_iid_manifiesto_no_inventa_config_en_metadata(self):
        metadata = {"containerimage.digest": self.obs["image_id"]}
        old = copy.deepcopy(metadata)
        result = self.remote.resolve_identity(
            metadata, self.obs["image_id"], self.inspection, self.log, self.obs
        )
        self.assertEqual(result["iid_interpretation"], "manifest-fallback")
        self.assertEqual(result["config_digest"], self.obs["config_digest"])
        self.assertEqual(metadata, old)
        self.assertNotIn("containerimage.config.digest", metadata)

    def test_config_explicito_exige_iid_config_y_no_mezcla_ids(self):
        metadata = {
            "containerimage.digest": self.obs["image_id"],
            "containerimage.config.digest": self.obs["config_digest"],
        }
        result = self.remote.resolve_identity(
            metadata, self.obs["config_digest"], self.inspection, self.log, self.obs
        )
        self.assertEqual(result["iid_interpretation"], "config")
        with self.assertRaises(ValueError):
            self.remote.resolve_identity(
                metadata, self.obs["image_id"], self.inspection, self.log, self.obs
            )

    def test_inspect_y_log_deben_converger_en_config_real(self):
        metadata = {"containerimage.digest": self.obs["image_id"]}
        altered = copy.deepcopy(self.inspection)
        altered["descriptor"]["annotations"]["config.digest"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            self.remote.resolve_identity(
                metadata, self.obs["image_id"], altered, self.log, self.obs
            )
        with self.assertRaises(ValueError):
            self.remote.resolve_identity(
                metadata,
                self.obs["image_id"],
                self.inspection,
                self.log.replace(self.obs["config_digest"], "sha256:" + "0" * 64),
                self.obs,
            )

    def read(self, name, *, mode=None, inode=None, data=b"contenido publico"):
        observations = copy.deepcopy(self.obs)
        expected = observations["files"][name]
        expected.update(size=len(data), sha256=self.remote.digest(data))
        info = types.SimpleNamespace(
            st_mode=stat.S_IFREG | (expected["mode"] if mode is None else mode),
            st_uid=0,
            st_gid=0,
            st_nlink=1,
            st_size=len(data),
            st_dev=expected["device"],
            st_ino=expected["inode"] if inode is None else inode,
        )

        class Stream(io.BytesIO):
            def fileno(self):
                return 123

        with (
            patch.object(self.remote.os, "O_NOFOLLOW", 131072, create=True),
            patch.object(self.remote.os, "O_NONBLOCK", 2048, create=True),
            patch.object(self.remote.os, "open", return_value=123),
            patch.object(self.remote.os, "fdopen", return_value=Stream(data)),
            patch.object(self.remote.os, "fstat", return_value=info),
            patch.object(self.remote.os, "stat", return_value=info),
        ):
            return self.remote.read_exact_file(99, name, observations)

    def test_metadata_0644_solo_exacta_sin_ampliar_otros_archivos(self):
        self.assertEqual(self.read("build-metadata.json"), b"contenido publico")
        with self.assertRaises(ValueError):
            self.read(
                "build-metadata.json", inode=self.obs["files"]["build-metadata.json"]["inode"] + 1
            )
        with self.assertRaises(ValueError):
            self.read("image-id.txt", mode=0o644)

    def test_perfil_referencia_historia_sin_b64_duplicado(self):
        profile = self.remote.proposal(self.obs)
        profile["sources"] = {
            "local": "a" * 64,
            "remote": "b" * 64,
            "observations": self.remote.OBSERVATIONS_SHA,
        }
        self.remote.validate_profile(profile, self.obs)
        changed = dict(profile, historical_profile_b64="x" * 400000)
        with self.assertRaises(ValueError):
            self.remote.validate_profile(changed, self.obs)


class FinishTransport(unittest.TestCase):
    def test_framing_pasa_por_collector_v4_real(self):
        spec = importlib.util.spec_from_file_location(
            "finish_framing_tests", ROOT / "tests/test_postgres_immich_postverify.py"
        )
        prior = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prior)
        with patch.object(prior, "load", return_value=load()):
            prior.FramingContract().test_main_informa_success_que_acepta_collector_v4_real()

    def test_plan_por_defecto_no_lee_boveda_ni_conecta(self):
        import contextlib

        spec = importlib.util.spec_from_file_location(
            "finish_local_plan", ROOT / "docker/postgres-immich/finish_image.py"
        )
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        runtime, sdk = local.dependencies()
        with (
            patch.object(local, "dependencies", return_value=(runtime, sdk)),
            patch.object(runtime, "read_values", side_effect=AssertionError("No debe leer bóveda")),
            patch.object(sdk, "connect", side_effect=AssertionError("No debe conectar")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(local.main([]), 0)


if __name__ == "__main__":
    unittest.main()
