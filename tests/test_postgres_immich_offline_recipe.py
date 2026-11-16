"""Contrato offline y árboles sintéticos; sin Docker, SDK ni compiladores."""

import hashlib
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docker/postgres-immich/offline_recipe.py"


def load():
    spec = importlib.util.spec_from_file_location("offline_recipe", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OfflineRecipe(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.allowed = {
            ("vchord", "0.0.0"),
            ("xtask", "0.0.0"),
            ("pgrx", "0.17.0"),
            ("rusqlite", "0.38.0"),
            ("libsqlite3-sys", "0.36.0"),
            ("object", "0.38.1"),
            ("wasm-bindgen-shared", "0.2.108"),
        }
        self.vchord = (
            "vchord v0.0.0 (/work/source)\npgrx v0.17.0\nrusqlite v0.38.0\nlibsqlite3-sys v0.36.0\n"
        )
        self.xtask = "xtask v0.0.0 (/work/source/crates/xtask)\nobject v0.38.1\n"

    def test_importacion_no_ejecuta_procesos(self):
        with mock.patch("subprocess.run") as run:
            load()
        run.assert_not_called()

    def test_arboles_exigen_ambos_roots_y_sqlite_normal(self):
        result = self.module.validate_graph(self.vchord, self.xtask, self.allowed)
        self.assertFalse(result["wasm_bindgen_shared_selected"])
        self.assertTrue(result["target_graph_observed"])
        self.assertFalse(result["compile_enabled"])
        for vchord, xtask in (
            (self.vchord.replace("libsqlite3-sys v0.36.0\n", ""), self.xtask),
            (self.vchord, "object v0.38.1\n"),
        ):
            with self.assertRaises(ValueError):
                self.module.validate_graph(vchord, xtask, self.allowed)

    def test_arbol_rechaza_git_seleccionado_en_vchord_o_xtask(self):
        selected = "wasm-bindgen-shared v0.2.108\n"
        for vchord, xtask in (
            (self.vchord + selected, self.xtask),
            (self.vchord, self.xtask + selected),
        ):
            with self.assertRaises(ValueError):
                self.module.validate_graph(vchord, xtask, self.allowed)

    def test_arbol_rechaza_paquetes_ajenos_controles_y_salida_excesiva(self):
        for suffix in (
            "otro v9.0.0\n",
            "\x1b[32mverde\n",
            "x" * (256 * 1024 + 1),
            "rusqlite v0.38.0 (https://example.org/source)\n",
        ):
            with self.assertRaises(ValueError):
                self.module.validate_graph(self.vchord + suffix, self.xtask, self.allowed)

    def test_plan_acota_un_contenedor_sin_secretos_ni_nas(self):
        build_id = "a" * 32
        inputs = f"/opt/media-platform/build/contexts/postgres-immich/{build_id}/inputs"
        outputs = f"/var/lib/media-platform/build/postgres-immich/{build_id}/outputs"
        with mock.patch("subprocess.run") as run:
            result = self.module.container_plan(inputs, outputs, "b" * 64)
        run.assert_not_called()
        args = result["create_arguments"]
        for flag, value in (
            ("--network", "none"),
            ("--cpus", "4"),
            ("--memory", "12g"),
            ("--memory-swap", "12g"),
            ("--pids-limit", "256"),
        ):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(args.count("--mount"), 2)
        self.assertNotIn("--privileged", args)
        self.assertFalse(result["sdk_executed"])
        self.assertFalse(result["compile_enabled"])
        self.assertTrue(result["persist_cid_before_start"])

    def test_plan_rechaza_paths_ajenos_escape_y_ids_distintos(self):
        build_id = "a" * 32
        inputs = f"/opt/media-platform/build/contexts/postgres-immich/{build_id}/inputs"
        outputs = f"/var/lib/media-platform/build/postgres-immich/{build_id}/outputs"
        for source, target in (
            ("/mnt/producciones", outputs),
            (inputs + "/../inputs", outputs),
            (inputs, outputs.replace(build_id, "c" * 32)),
        ):
            with self.assertRaises(ValueError):
                self.module.container_plan(source, target, "b" * 64)

    def test_parche_exige_sha_y_cuatro_invocaciones_exactas(self):
        original = b'.args(["metadata", "--format-version", "1"])\ncommand.args(["rustc"]);\n.args(["rustc"])\n.args(["clippy"])\n'
        expected = original.replace(
            b'["metadata", "--format-version", "1"]',
            b'["metadata", "--format-version", "1", "--frozen"]',
        )
        expected = expected.replace(b'["rustc"]', b'["rustc", "--frozen"]').replace(
            b'["clippy"]', b'["clippy", "--frozen"]'
        )
        self.assertEqual(
            self.module.frozen_xtask(
                original, hashlib.sha256(original).hexdigest(), hashlib.sha256(expected).hexdigest()
            ),
            expected,
        )
        with self.assertRaises(ValueError):
            self.module.frozen_xtask(original, "0" * 64, hashlib.sha256(expected).hexdigest())
        with self.assertRaises(ValueError):
            self.module.frozen_xtask(
                original + b'.args(["rustc"])\n',
                hashlib.sha256(original + b'.args(["rustc"])\n').hexdigest(),
                "0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
