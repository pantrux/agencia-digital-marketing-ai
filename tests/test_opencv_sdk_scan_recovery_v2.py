"""Descriptor OS real y vista de paquetes; sin Trivy, SSH o SDK."""

import copy
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def driver():
    path = ROOT / "docker/kitsu-zou/recover_opencv_sdk_scan_v2.py"
    spec = importlib.util.spec_from_file_location("opencv_scan_recovery_v2_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixtures():
    components, packages = [], []
    for i in range(91):
        name, version, source = f"fixture-{i}", "1-r0", f"origin-{i}"
        purl = f"pkg:apk/wolfi/{name}@{version}?arch=x86_64&distro=20230201"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": purl,
                "properties": [
                    {"name": "aquasecurity:trivy:SrcName", "value": source},
                    {"name": "aquasecurity:trivy:SrcVersion", "value": version},
                ],
            }
        )
        packages.append(
            {
                "Name": name,
                "Version": version,
                "SrcName": source,
                "SrcVersion": version,
                "Arch": "x86_64",
                "Identifier": {"PURL": purl},
            }
        )
    components += [
        {"type": "library", "name": "numpy", "version": "2.4.6", "purl": "pkg:pypi/numpy@2.4.6"},
        {"type": "operating-system", "name": "wolfi", "version": "20230201", "bom-ref": "os:wolfi"},
    ]
    report = {
        "Metadata": {"OS": {"Family": "wolfi", "Name": "20230201", "informativo": True}},
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
        ],
    }
    return {"components": components}, report


class OperatingSystem(unittest.TestCase):
    def setUp(self):
        self.module = driver()
        self.identity = {"ID": "wolfi", "VERSION_ID": "20230201"}
        self.bom, self.report = fixtures()

    def test_descriptor_unico_sin_purl_y_vista_library_no_muta_bom_fisico(self):
        self.module.validate_bom_os(self.bom, self.identity)
        before = copy.deepcopy(self.bom)
        view = self.module.libraries_only(self.bom)
        self.assertEqual(len(view["components"]), 92)
        self.assertTrue(all(c["type"] == "library" for c in view["components"]))
        self.assertEqual(before, self.bom)
        self.assertNotIn("purl", self.bom["components"][-1])

    def test_sin_os_duplicado_o_divergente_rechaza_preflight(self):
        for edit in ("missing", "duplicate", "wrong", "purl"):
            bom = copy.deepcopy(self.bom)
            if edit == "missing":
                bom["components"].pop()
            elif edit == "duplicate":
                bom["components"].append(copy.deepcopy(bom["components"][-1]))
            elif edit == "wrong":
                bom["components"][-1]["version"] = "otro"
            else:
                bom["components"][-1]["purl"] = "pkg:apk/wolfi/os@1"
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.module.validate_bom_os(bom, self.identity)

    def test_report_os_none_o_version_distinta_rechaza(self):
        self.module.validate_report_os(self.report, self.identity)
        for os_value in (None, {"Family": "none", "Name": ""}, {"Family": "wolfi", "Name": "otro"}):
            report = copy.deepcopy(self.report)
            report["Metadata"]["OS"] = os_value
            with self.assertRaises(ValueError):
                self.module.validate_report_os(report, self.identity)

    def test_review_93_componentes_fisicos_valida_solo_92_libraries_y_conserva_high(self):
        sbom = self.module.load_exact(
            "recovery_v2_sbom_fixture",
            ROOT / "docker/kitsu-zou/opencv_sdk_sbom.py",
            "183d7db164ed11d14406f36d8c22b3ac70179ed1512f5f29ec34547f8b3fd8d6",
        )
        self.report["Results"][0]["Vulnerabilities"] = [
            {"Severity": "HIGH", "VulnerabilityID": "CVE-fixture"}
        ]
        result = SimpleNamespace(
            status="completed", exit_code=0, cleanup_complete=True, stdout=b"", stderr=b""
        )
        review = self.module.review_report(result, self.report, self.bom, sbom, self.identity)
        self.assertEqual(len(review["high_critical"]), 1)
        self.assertTrue(review["operating_system_verified"])
        self.assertTrue(review["source_identity_verified"])
        self.assertEqual(len(self.bom["components"]), 93)


if __name__ == "__main__":
    unittest.main()
