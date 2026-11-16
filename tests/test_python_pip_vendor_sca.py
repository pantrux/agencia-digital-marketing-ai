"""Cobertura exacta y hallazgos de las hipótesis PyPI, sin ejecutar scanners."""

import copy
import importlib.util
import unittest
from pathlib import Path


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "docker/python-runtime/scan_pip_vendors.py"
        spec = importlib.util.spec_from_file_location("pip_sca_tests", path)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def setUp(self):
        self.components = [{"name": "pip", "version": "26.2.2", "purl": "pkg:pypi/pip@26.2.2"}]
        self.report = {
            "Results": [
                {
                    "Type": "python-pkg",
                    "Packages": [
                        {
                            "Name": "pip",
                            "Version": "26.2.2",
                            "Identifier": {"PURL": "pkg:pypi/pip@26.2.2"},
                        }
                    ],
                }
            ]
        }

    def test_exact_positive_and_high_retained(self):
        self.report["Results"][0]["Vulnerabilities"] = [
            {
                "VulnerabilityID": "TEST-1",
                "PkgName": "pip",
                "InstalledVersion": "26.2.2",
                "Severity": "HIGH",
            }
        ]
        before = copy.deepcopy(self.report)
        result = self.m.validate_coverage(self.report, self.components)
        self.assertEqual(result["high_critical"], 1)
        self.assertEqual(result["findings"], 1)
        self.assertEqual(self.report, before)

    def test_zero_packages_does_not_mean_zero_findings(self):
        self.report["Results"][0]["Packages"] = []
        with self.assertRaisesRegex(ValueError, "Cobertura"):
            self.m.validate_coverage(self.report, self.components)

    def test_duplicate_is_rejected(self):
        self.report["Results"][0]["Packages"] *= 2
        with self.assertRaisesRegex(ValueError, "Cobertura"):
            self.m.validate_coverage(self.report, self.components)

    def test_wrong_purl_is_rejected(self):
        self.report["Results"][0]["Packages"][0]["Identifier"]["PURL"] = "pkg:npm/pip@26.2.2"
        with self.assertRaisesRegex(ValueError, "PURL"):
            self.m.validate_coverage(self.report, self.components)

    def test_unknown_severity_is_retained(self):
        self.report["Results"][0]["Vulnerabilities"] = [
            {
                "VulnerabilityID": "TEST-1",
                "PkgName": "pip",
                "InstalledVersion": "26.2.2",
                "Severity": "UNKNOWN",
            }
        ]
        self.assertEqual(self.m.validate_coverage(self.report, self.components)["unknown"], 1)

    def test_unattributed_finding_is_rejected(self):
        self.report["Results"][0]["Vulnerabilities"] = [
            {
                "VulnerabilityID": "TEST-1",
                "PkgName": "otro",
                "InstalledVersion": "1",
                "Severity": "HIGH",
            }
        ]
        with self.assertRaisesRegex(ValueError, "Hallazgo"):
            self.m.validate_coverage(self.report, self.components)


if __name__ == "__main__":
    unittest.main()
