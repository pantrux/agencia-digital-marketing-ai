"""Prefijo alternativo y guardas OpenEXR; fixtures sin SDK, CMake o SSH."""

import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def driver():
    path = ROOT / "docker/kitsu-zou/prepare_openexr3_inputs.py"
    spec = importlib.util.spec_from_file_location("openexr3_inputs_tests", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class InputsTests(unittest.TestCase):
    def setUp(self):
        self.m = driver()

    def fixtures(self):
        bodies = {
            "usr/lib/libdeflate.a": b"!<arch>\nfixture",
            "usr/lib/libOpenEXR-3_5.so.34.3.5.3": b"shared",
        }
        entries = {
            name: {"kind": "file", "size": len(raw), "sha256": self.m.digest(raw)}
            for name, raw in bodies.items()
        }
        entries["usr/lib/libOpenEXR-3_5.so.34"] = {
            "kind": "symlink",
            "target": "libOpenEXR-3_5.so.34.3.5.3",
        }
        return bodies, {"usr/lib/libOpenEXR-3_5.so.34": "libOpenEXR-3_5.so.34.3.5.3"}, entries

    def test_proyeccion_exacta_con_static_solo_como_dato_y_enlaces_locales(self):
        files, links, expected = self.fixtures()
        self.m.validate_projection(files, links, expected)
        for bad in [
            ({**files, "extra": b"x"}, links),
            ({k: v for k, v in files.items() if not k.endswith(".a")}, links),
            (files, {**links, "usr/lib/libOpenEXR-3_5.so.34": "../../etc/passwd"}),
        ]:
            with self.assertRaises(ValueError):
                self.m.validate_projection(*bad, expected)

    def test_guard_rechaza_fallback_y_entrada_a_static_en_link_real(self):
        self.m.check_build_graph(
            [{"file": "/source/modules/core/src/test.cpp"}],
            "  LINK_LIBRARIES = /prefix/usr/lib/libOpenEXR-3_5.so.34\n",
        )
        for commands, ninja in [
            ([{"file": "/source/3rdparty/openexr/IlmImf/foo.cpp"}], ""),
            (
                [{"file": "/source/modules/core/a.cpp"}],
                "  LINK_LIBRARIES = /prefix/usr/lib/libdeflate.a\n",
            ),
            (
                [{"file": "/source/modules/core/a.cpp"}],
                "  LINK_LIBRARIES = /prefix/usr/lib/$\nlibdeflate.a\n",
            ),
        ]:
            with self.subTest(commands=commands, ninja=ninja), self.assertRaises(ValueError):
                self.m.check_build_graph(commands, ninja)

    def test_target_report_exige_version_ubicacion_y_target_completo(self):
        expected = {
            "OpenEXR::OpenEXR": "usr/lib/libOpenEXR-3_5.so.34.3.5.3",
            "libdeflate::libdeflate_static": "usr/lib/libdeflate.a",
        }
        report = {
            "OpenEXR_VERSION": "3.5.3",
            "targets": {name: "/prefix/" + path for name, path in expected.items()},
            "Threads_FOUND": True,
        }
        self.m.validate_targets(report, expected, "/prefix")
        for changed in [
            {**report, "OpenEXR_VERSION": "2.3.0"},
            {**report, "targets": {}},
            {**report, "targets": {**report["targets"], "OpenEXR::OpenEXR": "/usr/lib/unknown.so"}},
        ]:
            with self.assertRaises(ValueError):
                self.m.validate_targets(changed, expected, "/prefix")

    def test_prueba_enlace_exige_needed_de_openexr3_y_no_ilmimf2(self):
        self.m.validate_needed(["libOpenEXR-3_5.so.34", "libc.so.6"])
        for names in [[], ["libIlmImf-2_3.so.24"], ["libOpenEXR-3_5.so.34", "libIlmImf.so"]]:
            with self.assertRaises(ValueError):
                self.m.validate_needed(names)

    def test_requerimiento_cmake_y_guard_se_generan_sin_compilar(self):
        cmake, source = self.m.probe_sources(
            {"OpenEXR::OpenEXR": "usr/lib/libOpenEXR-3_5.so.34.3.5.3"}
        )
        self.assertIn("find_package(OpenEXR 3.5.3 EXACT CONFIG REQUIRED)", cmake)
        self.assertIn("OpenEXR::OpenEXR", cmake)
        self.assertIn("Imf::RgbaInputFile", source)
        recipe = self.m.derive_recipe(b"a" * 64, "b" * 64)
        self.assertIn(b"-DCMAKE_REQUIRE_FIND_PACKAGE_OpenEXR=ON", recipe)
        self.assertIn(b"--compile-commands", recipe)
        self.assertIn(b"--parallel 2", recipe)

    def test_plan_es_puro_y_prepare_requiere_pin_y_root_flag(self):
        with (
            patch.object(self.m, "plan", return_value={"fixture": True}),
            patch.object(self.m, "prepare", side_effect=AssertionError("escritura")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(self.m.main(["plan"]), 0)
            self.assertEqual(self.m.main(["prepare"]), 1)

    def test_writer_excl_no_adopta_destino_ni_extractall(self):
        files, links, expected = self.fixtures()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "prefix"
            self.m.write_projection(target, files, links, expected)
            with tarfile.open(target / "overlay.tar", "r:") as archive:
                self.assertEqual(
                    archive.extractfile("usr/lib/libdeflate.a").read(),
                    files["usr/lib/libdeflate.a"],
                )
                self.assertTrue(archive.getmember("usr/lib/libOpenEXR-3_5.so.34").issym())
            with self.assertRaises(FileExistsError):
                self.m.write_projection(target, files, links, expected)

    def test_cobertura_nueva_no_reutiliza_sca_historica_ni_declara_partials_instalados(self):
        spec = json.loads((ROOT / "docker/kitsu-zou/openexr3-inputs.json").read_bytes())
        self.m.validate_sca_scope(spec["required_sca_scope"], spec["sources"])
        for field, bad in [
            ("expected_apk_identities", 91),
            ("historical_2ce_receipt_covers_new_inputs", True),
            ("new_inputs_sca_verified", True),
        ]:
            changed = {**spec["required_sca_scope"], field: bad}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.m.validate_sca_scope(changed, spec["sources"])

    def test_guard_generado_y_lector_son_solo_funciones_sin_sdk(self):
        scope = {"__name__": "fixture"}
        exec(compile(self.m.runtime_guard_source(), "<fixture-guard>", "exec"), scope)
        scope["validate_needed"](["libOpenEXR-3_5.so.34", "libc.so.6"])
        with self.assertRaises(ValueError):
            scope["validate_needed"](["libOpenEXR-3_5.so.34", "foreign.so"])
        elf_scope = {}
        exec(compile(self.m.elf_reader_source(), "<fixture-elf>", "exec"), elf_scope)
        self.assertIn("elf_info", elf_scope)

    def test_snapshot_sdk_mayor256k_se_lee_con_limite_acotado_por_rol(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            raw = b"x" * (1024 * 1024 + 1)
            path.write_bytes(raw)
            self.assertEqual(self.m.read_regular(path, self.m.reference_limit("sdk_capture")), raw)
            with self.assertRaises(ValueError):
                self.m.read_regular(path, self.m.reference_limit("old_recipe"))


if __name__ == "__main__":
    unittest.main()
