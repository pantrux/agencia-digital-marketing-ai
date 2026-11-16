"""Comprueba inventario de datos ZIP; nunca importa el código del payload."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import unittest
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "docker/python-runtime/inventory_pip_vendor.py"


def fixture(*, vendor="CacheControl==0.14.4\nsetuptools==70.3.0\n", extra=None, corrupt=False):
    files = {
        "pip/__init__.py": b'__version__ = "26.2.2"\n',
        "pip/_vendor/vendor.txt": vendor.encode(),
        "pip/_vendor/cachecontrol/__init__.py": b"# copia vendorizada\n",
        "pip/_vendor/cachecontrol/LICENSE.txt": b"MIT\n",
        "pip/_vendor/pkg_resources/__init__.py": b"# subconjunto\n",
        "pip/_vendor/pkg_resources/LICENSE": b"MIT\n",
        "pip-26.2.2.dist-info/METADATA": b"Metadata-Version: 2.4\nName: pip\nVersion: 26.2.2\n\n",
    }
    files.update(extra or {})
    table = io.StringIO(newline="")
    writer = csv.writer(table, lineterminator="\n")
    for name, raw in sorted(files.items()):
        sha = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        writer.writerow([name, "sha256=" + sha, len(raw)])
    writer.writerow(["pip-26.2.2.dist-info/RECORD", "", ""])
    files["pip-26.2.2.dist-info/RECORD"] = table.getvalue().encode()
    if corrupt:
        files["pip/__init__.py"] += b"# distinto\n"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    return out.getvalue()


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("pip_vendor_inventory_test", SOURCE)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def test_real_zip_record_and_subset(self):
        result = self.m.inventory_wheel(fixture())
        self.assertEqual(result["pip"]["version"], "26.2.2")
        self.assertEqual(result["declared_vendor_distributions"], 2)
        subset = next(x for x in result["vendors"] if x["name"] == "setuptools")
        self.assertEqual(subset["namespace"], "pkg_resources")
        self.assertFalse(subset["whole_distribution_verified"])
        self.assertFalse(result["runtime_approved"])
        self.assertEqual(len(self.m.cyclonedx(result)["components"]), 3)

    def test_record_detects_changed_content(self):
        with self.assertRaisesRegex(ValueError, "RECORD"):
            self.m.inventory_wheel(fixture(corrupt=True))

    def test_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Ruta"):
            self.m.inventory_wheel(fixture(extra={"../fuera.py": b"x"}))

    def test_duplicate_zip_member_is_rejected(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("pip/__init__.py", b"a")
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("pip/__init__.py", b"b")
        with self.assertRaisesRegex(ValueError, "duplicad"):
            self.m.inventory_wheel(out.getvalue())

    def test_duplicate_vendor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicad"):
            self.m.inventory_wheel(fixture(vendor="CacheControl==0.14.4\ncachecontrol==0.14.4\n"))

    def test_missing_namespace_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "namespace"):
            self.m.inventory_wheel(fixture(vendor="desconocido==1.0\n"))

    def test_vendor_expression_is_not_interpreted(self):
        with self.assertRaisesRegex(ValueError, "Declaración"):
            self.m.inventory_wheel(fixture(vendor="CacheControl>=0.14.4\n"))

    def test_unlisted_vendor_code_is_not_silently_covered(self):
        result = self.m.inventory_wheel(
            fixture(extra={"pip/_vendor/no_declarado/__init__.py": b"# otro\n"})
        )
        self.assertEqual(result["unattributed_vendor_namespaces"], ["no_declarado"])
        self.assertFalse(result["identity_complete"])

    def test_stubs_and_supplier_notes_are_explicit_data(self):
        result = self.m.inventory_wheel(
            fixture(
                extra={
                    "pip/_vendor/cachecontrol.pyi": b"# tipos\n",
                    "pip/_vendor/README.rst": b"# notas\n",
                }
            )
        )
        self.assertEqual(result["unattributed_vendor_namespaces"], [])
        self.assertEqual(result["supplier_notes"][0]["path"], "pip/_vendor/README.rst")

    def test_supplier_bom_version_mismatch_is_rejected(self):
        bom = {"bomFormat": "CycloneDX", "components": [{"name": "CacheControl", "version": "9.9"}]}
        with self.assertRaisesRegex(ValueError, "SBOM"):
            self.m.inventory_wheel(
                fixture(extra={"pip/_vendor/bom.cdx.json": json.dumps(bom).encode()})
            )

    def test_directory_suffix_does_not_hide_unattributed_code(self):
        result = self.m.inventory_wheel(
            fixture(extra={"pip/_vendor/cachecontrol.py/hidden.py": b"# otro\n"})
        )
        self.assertEqual(result["unattributed_vendor_namespaces"], ["cachecontrol.py"])
        self.assertFalse(result["namespace_declarations_complete"])


if __name__ == "__main__":
    unittest.main()
