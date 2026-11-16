"""Prueba que una auditoría vacía, incompleta o vulnerable no acredite Python."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packaging.markers import default_environment

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_python_audit", PROJECT / "scripts/ci/check_python_audit.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PINS = {"paramiko": "5.1.0", "cryptography": "50.0.2", "pyyaml": "6.0.3"}


def report():
    return {
        "dependencies": [
            {"name": name, "version": version, "vulns": []} for name, version in PINS.items()
        ],
        "fixes": [],
    }


class AuditoriaPython(unittest.TestCase):
    def test_acepta_grafo_fijado_completo_y_sin_hallazgos(self):
        self.assertEqual(MODULE.validate_report(report(), PINS), [])

    def test_rechaza_informe_vacio_o_sin_dependencias(self):
        for value in (None, {}, [], {"dependencies": []}):
            with self.subTest(report=value):
                self.assertTrue(MODULE.validate_report(value, PINS))

    def test_omitir_paramiko_o_su_transitiva_impide_aprobar(self):
        for missing in ("paramiko", "cryptography"):
            value = report()
            value["dependencies"] = [
                dependency for dependency in value["dependencies"] if dependency["name"] != missing
            ]
            with self.subTest(missing=missing):
                self.assertTrue(MODULE.validate_report(value, PINS))

    def test_rechaza_version_ajena_paquete_sin_pin_y_duplicados(self):
        for change in ("version", "extra", "duplicate"):
            value = report()
            if change == "version":
                value["dependencies"][0]["version"] = "4.0.0"
            elif change == "extra":
                value["dependencies"].append({"name": "otro", "version": "1.0", "vulns": []})
            else:
                value["dependencies"].append({"name": "PARAMIKO", "version": "5.1.0", "vulns": []})
            with self.subTest(change=change):
                self.assertTrue(MODULE.validate_report(value, PINS))

    def test_vulnerabilidad_omision_o_esquema_incompleto_bloquean(self):
        for field in (
            {"vulns": [{"id": "PYSEC-PRUEBA"}]},
            {"skip_reason": "no resuelto"},
            {"vulns": None},
        ):
            value = report()
            value["dependencies"][0].update(field)
            with self.subTest(field=field):
                self.assertTrue(MODULE.validate_report(value, PINS))

    def test_carga_inclusiones_y_constraints_con_rutas_relativas(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "storage").mkdir()
            (root / "requirements-dev.txt").write_text(
                "PyYAML==6.0.3\n-r storage/requirements-synology.txt\n-c requirements-python-constraints.txt\n"
            )
            (root / "storage/requirements-synology.txt").write_text("paramiko==5.1.0\n")
            (root / "requirements-python-constraints.txt").write_text("cryptography==50.0.2\n")
            self.assertEqual(MODULE.load_expected_pins(root), PINS)

    def test_coteja_el_pin_condicional_solo_en_su_plataforma(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements-dev.txt").write_text(
                'PyYAML==6.0.3\ntzdata==2026.6; sys_platform == "win32"\n', encoding="utf-8"
            )
            for platform, dependencies in (
                ("linux", {"pyyaml": "6.0.3"}),
                ("win32", {"pyyaml": "6.0.3", "tzdata": "2026.6"}),
            ):
                environment = {**default_environment(), "sys_platform": platform}
                with (
                    self.subTest(platform=platform),
                    patch(
                        "packaging.markers.default_environment",
                        return_value=environment,
                    ),
                ):
                    expected = MODULE.load_expected_pins(root)
                    self.assertEqual(expected, dependencies)
                    audit = {
                        "dependencies": [
                            {"name": name, "version": version, "vulns": []}
                            for name, version in dependencies.items()
                        ]
                    }
                    self.assertEqual(MODULE.validate_report(audit, expected), [])

    def test_requisitos_no_fijados_conflictivos_o_fuera_del_proyecto_se_rechazan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for text in (
                "paramiko>=5\n",
                'paramiko==5.*; sys_platform == "win32"\n',
                "paramiko[extra]==5.1.0\n",
                "paramiko @ https://example.invalid/archivo.whl\n",
                'paramiko==5.1.0; plataforma_inexistente == "win32"\n',
                "paramiko==5.1.0\nParamiko==4.0.0\n",
                "-r ../externo.txt\n",
            ):
                (root / "requirements-dev.txt").write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    MODULE.load_expected_pins(root)


if __name__ == "__main__":
    unittest.main()
