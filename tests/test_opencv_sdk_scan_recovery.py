"""Recuperación de hasher Windows; fixtures y archivos temporales, sin Trivy/SSH/SDK."""

import contextlib
import copy
import hashlib
import importlib.util
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def driver():
    path = ROOT / "docker/kitsu-zou/recover_opencv_sdk_scan.py"
    spec = importlib.util.spec_from_file_location("opencv_scan_recovery_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def info(*, ctime, **changes):
    return SimpleNamespace(
        **{
            "st_dev": 1,
            "st_ino": 2,
            "st_size": 3,
            "st_mtime_ns": 4,
            "st_nlink": 1,
            "st_mode": stat.S_IFREG | 0o600,
            "st_ctime_ns": ctime,
            "st_file_attributes": 0,
            **changes,
        }
    )


class FileSignatures(unittest.TestCase):
    def test_segunda_lectura_acotada_y_crecimiento_entre_lecturas_rechazado(self):
        module = driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "datos.bin"
            path.write_bytes(b"12345")
            hashed = {"bytes": 3, "sha256": hashlib.sha256(b"123").hexdigest()}
            with (
                patch.object(module, "hash_file", return_value=hashed),
                patch.object(Path, "read_bytes", side_effect=AssertionError("lectura sin límite")),
            ):
                with self.assertRaises(ValueError):
                    module.read_exact(path, 3)

    def test_ctime_distinto_por_api_pero_estable_es_valido(self):
        module = driver()
        module.validate_signatures(info(ctime=10), info(ctime=20), info(ctime=20), info(ctime=10))

    def test_mutacion_dentro_de_cada_api_o_puente_rechaza(self):
        module = driver()
        base = [info(ctime=10), info(ctime=20), info(ctime=20), info(ctime=10)]
        for position, field in (
            (2, "st_ctime_ns"),
            (3, "st_ctime_ns"),
            (2, "st_mtime_ns"),
            (3, "st_ino"),
            (2, "st_size"),
            (3, "st_nlink"),
        ):
            values = [SimpleNamespace(**vars(row)) for row in base]
            setattr(values[position], field, getattr(values[position], field) + 1)
            with self.subTest(position=position, field=field), self.assertRaises(ValueError):
                module.validate_signatures(*values)
        for wrong in (
            info(ctime=20, st_ino=3),
            info(ctime=20, st_mode=stat.S_IFDIR | 0o700),
            info(ctime=20, st_file_attributes=0x400),
        ):
            with self.assertRaises(ValueError):
                module.validate_signatures(base[0], wrong, wrong, base[3])

    def test_hasher_real_y_reproduccion_del_fallo_v2_sin_tocar_sus_fuentes(self):
        module = driver()
        legacy = module.load_exact(
            "audit_recovery_legacy_fixture",
            ROOT / "docker/kitsu-zou/audit_opencv_sdk.py",
            module.OLD_SCANNER,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.bin"
            path.write_bytes(b"datos propios")
            original = path.stat()
            altered = SimpleNamespace(
                **{
                    name: getattr(original, name)
                    for name in (
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_nlink",
                        "st_mode",
                        "st_ctime_ns",
                    )
                }
            )
            altered.st_ctime_ns = original.st_ctime_ns + 1000
            altered.st_file_attributes = 0
            with patch.object(os, "fstat", return_value=altered):
                with self.assertRaises(ValueError):
                    legacy.hash_file(path, 1024)
                result = module.hash_file(path, 1024)
            self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertNotEqual(result["fstat"]["ctime_ns"], result["lstat"]["ctime_ns"])

    def test_limite_y_numero_de_bytes_exactos(self):
        module = driver()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.bin"
            path.write_bytes(b"1234")
            with self.assertRaises(ValueError):
                module.hash_file(path, 3)
            self.assertEqual(module.hash_file(path, 4)["bytes"], 4)


class RecoveryContract(unittest.TestCase):
    def test_plan_puro_no_invoca_ejecucion(self):
        module = driver()
        with (
            patch.object(module, "execute", side_effect=AssertionError("no ejecutar")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(module.main([]), 0)
        self.assertIn('"scan_executed": false', output.getvalue())

    def test_prefijo_anterior_no_adopta_salida_ya_existente(self):
        module = driver()
        with tempfile.TemporaryDirectory() as directory:
            prefix = str(Path(directory) / "own")
            module.require_unused_outputs(prefix)
            Path(prefix + ".trivy.json").write_bytes(b"original")
            with self.assertRaises(ValueError):
                module.require_unused_outputs(prefix)
            self.assertEqual(Path(prefix + ".trivy.json").read_bytes(), b"original")


def scanner_fixture():
    components, packages = [], []
    for i in range(91):
        name, origin, version = f"pkg-{i}", f"origin-{i}", "1.0-r1"
        purl = f"pkg:apk/wolfi/{name}@{version}?arch=x86_64&distro=20230201"
        components.append(
            {
                "name": name,
                "version": version,
                "purl": purl,
                "properties": [
                    {"name": "aquasecurity:trivy:SrcName", "value": origin},
                    {"name": "aquasecurity:trivy:SrcVersion", "value": version},
                ],
            }
        )
        packages.append(
            {
                "Name": name,
                "Version": version,
                "SrcName": origin,
                "SrcVersion": version,
                "Arch": "x86_64",
                "Identifier": {"PURL": purl},
            }
        )
    components.append({"name": "numpy", "version": "2.4.6", "purl": "pkg:pypi/numpy@2.4.6"})
    report = {
        "Results": [
            {"Class": "os-pkgs", "Type": "wolfi", "Packages": packages},
            {
                "Class": "lang-pkgs",
                "Type": "python-pkg",
                "Packages": [
                    {
                        "Name": "numpy",
                        "Version": "2.4.6",
                        "Identifier": {"PURL": "pkg:pypi/numpy@2.4.6"},
                    }
                ],
            },
        ]
    }
    return {"components": components}, report


class WarningClassification(unittest.TestCase):
    def setUp(self):
        self.module = driver()
        self.sbom = self.module.load_exact(
            "recovery_sbom_fixture",
            ROOT / "docker/kitsu-zou/opencv_sdk_sbom.py",
            "183d7db164ed11d14406f36d8c22b3ac70179ed1512f5f29ec34547f8b3fd8d6",
        )
        self.bom, self.report = scanner_fixture()

    def result(self, messages):
        return SimpleNamespace(
            status="completed",
            exit_code=0,
            cleanup_complete=True,
            stdout=b"",
            stderr=b"".join(
                b"2026-11-06T11:50:33-03:00\tWARN\t" + m.encode() + b"\n" for m in messages
            ),
        )

    def test_tres_warnings_exactos_clasificados_con_identidades_completas(self):
        result = self.result(self.module.KNOWN_WARNINGS)
        assessment = self.module.review_report(result, self.report, self.bom, self.sbom)
        self.assertEqual(len(assessment["warning_classifications"]), 3)
        self.assertTrue(assessment["source_identity_verified"])

    def test_warning_nuevo_y_error_no_se_convierten_en_aprobacion(self):
        for text in (
            "Nuevo aviso",
            "Third-party SBOM may lead to inaccurate vulnerability detection adicional",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.module.review_report(self.result([text]), self.report, self.bom, self.sbom)
        result = self.result([])
        result.stderr = b"2026-11-06T11:50:33-03:00\tERROR\tfallo\n"
        with self.assertRaises(ValueError):
            self.module.review_report(result, self.report, self.bom, self.sbom)

    def test_origen_omitido_o_divergente_purl_y_arch_rechazados(self):
        for field in ("SrcName", "SrcVersion", "Arch", "Identifier"):
            value = copy.deepcopy(self.report)
            value["Results"][0]["Packages"][0].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.review_report(
                    self.result(self.module.KNOWN_WARNINGS), value, self.bom, self.sbom
                )
        value = copy.deepcopy(self.report)
        value["Results"][0]["Packages"][0]["SrcName"] = "otro-origen"
        with self.assertRaises(ValueError):
            self.module.review_report(
                self.result(self.module.KNOWN_WARNINGS), value, self.bom, self.sbom
            )

    def test_high_conservado_aunque_los_warnings_sean_conocidos(self):
        self.report["Results"][0]["Vulnerabilities"] = [
            {"Severity": "HIGH", "VulnerabilityID": "CVE-fixture"}
        ]
        assessment = self.module.review_report(
            self.result(self.module.KNOWN_WARNINGS), self.report, self.bom, self.sbom
        )
        self.assertEqual(
            assessment["high_critical"], [{"Severity": "HIGH", "VulnerabilityID": "CVE-fixture"}]
        )


if __name__ == "__main__":
    unittest.main()
