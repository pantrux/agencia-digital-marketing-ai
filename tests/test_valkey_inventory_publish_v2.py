"""Publicación cerrada; --real añade la regresión con las entradas locales fijadas."""

import copy
import hashlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "docker/valkey/inventory_image_v2.py"
L = types.ModuleType("valkey_inventory_publication_v2_test")
L.__file__ = str(PATH)
exec(compile(PATH.read_bytes(), str(PATH), "exec"), L.__dict__)
NAMES = {
    "inventory.json",
    "file-catalog.json",
    "os-packages.cdx.json",
    "binary-materials.json",
    "provenance.json",
}
CORE_SHA = "2d1f335fbb458d34c80b3a2c0fa993d75bc5cf0858377ac9bb1a25966976ee6d"
REAL_REQUESTED = False


def data():
    outputs = {name: L.canonical({"fixture": name}) for name in NAMES}
    return plan_for(outputs), outputs


def plan_for(outputs):
    return {
        "outputs": {
            name: {"sha256": L.digest(raw), "bytes": len(raw)} for name, raw in outputs.items()
        }
    }


def assert_publication(case, target, plan, outputs):
    receipt = L.publish(target, plan, outputs)
    case.assertEqual(
        {item.name for item in target.iterdir()},
        NAMES | {".owner.json", "plan.json", "receipt.json"},
    )
    case.assertEqual(receipt["outputs"], plan["outputs"])
    case.assertTrue(receipt["artifacts_published"])
    for flag in ("binary_accepted", "sca_executed", "runtime_executed"):
        case.assertIs(receipt[flag], False)
    for name, raw in outputs.items():
        written = (target / name).read_bytes()
        case.assertEqual(written, raw)
        case.assertEqual(hashlib.sha256(written).hexdigest(), plan["outputs"][name]["sha256"])
        case.assertEqual(len(written), plan["outputs"][name]["bytes"])
    marker = json.loads((target / ".owner.json").read_bytes())
    case.assertEqual(marker["owner"], "agencia-digital-marketing-ai:valkey-inventory:v2")
    case.assertEqual(marker["path"], str(target))
    case.assertEqual(marker["plan_sha256"], L.digest(L.canonical(plan)))
    case.assertEqual(json.loads((target / "receipt.json").read_bytes()), receipt)
    case.assertEqual((target / "plan.json").read_bytes(), L.canonical(plan))
    with case.assertRaises(FileExistsError):
        L.publish(target, plan, outputs)
    return receipt


class PublicationTests(unittest.TestCase):
    def test_all_five_closed_names_including_cdx_are_published(self):
        plan, outputs = data()
        with tempfile.TemporaryDirectory() as temporary:
            assert_publication(self, Path(temporary) / "own", plan, outputs)

    def test_extra_missing_and_dangerous_names_fail_before_marker_or_payload(self):
        _, original = data()
        bad = [
            {**original, "extra.json": b"{}"},
            {name: raw for name, raw in original.items() if name != "inventory.json"},
            {},
        ]
        for name in (
            "../escape.json",
            "folder/child.json",
            "folder\\child.json",
            "CON.json",
            "nul.json",
            "inventory.json:stream",
            "Inventory.json",
            "os-packages.json",
            "os-packages.cdx.json.",
        ):
            wrong = dict(original)
            wrong[name] = wrong.pop("os-packages.cdx.json")
            bad.append(wrong)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            sentinel = parent / "foreign"
            sentinel.write_bytes(b"keep")
            for number, outputs in enumerate(bad):
                with self.subTest(number=number):
                    target = parent / ("rejected-" + str(number))
                    with self.assertRaisesRegex(ValueError, "^OUTPUT_NAMES$"):
                        L.publish(target, plan_for(outputs), outputs)
                    self.assertEqual({item.name for item in target.iterdir()}, {"failure.json"})
                    self.assertFalse(
                        json.loads((target / "failure.json").read_bytes())["artifacts_published"]
                    )
            self.assertEqual(sentinel.read_bytes(), b"keep")
            self.assertFalse((parent.parent / "escape.json").exists())

    def test_list_of_names_is_not_an_output_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "^OUTPUT_NAMES$"):
                L.publish(Path(temporary) / "own", {}, list(NAMES))

    def test_tampered_expected_hash_leaves_only_failure(self):
        plan, outputs = data()
        plan = copy.deepcopy(plan)
        plan["outputs"]["os-packages.cdx.json"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "own"
            with self.assertRaisesRegex(ValueError, "^OUTPUT_PLAN$"):
                L.publish(target, plan, outputs)
            self.assertEqual({item.name for item in target.iterdir()}, {"failure.json"})

    def test_foreign_directory_is_preserved_and_not_adopted(self):
        plan, outputs = data()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "foreign"
            target.mkdir()
            (target / "sentinel").write_bytes(b"unrelated")
            with self.assertRaises(FileExistsError):
                L.publish(target, plan, outputs)
            self.assertEqual({item.name for item in target.iterdir()}, {"sentinel"})
            self.assertEqual((target / "sentinel").read_bytes(), b"unrelated")

    def test_default_cli_returns_plan_without_publication(self):
        plan, outputs = data()
        with (
            patch.object(L, "prepare", return_value=(plan, outputs)),
            patch.object(L, "publish", side_effect=AssertionError("NO_PUBLICATION")),
            patch.object(sys, "stdout", new_callable=io.StringIO) as stream,
        ):
            self.assertEqual(L.main([]), 0)
            self.assertIs(json.loads(stream.getvalue())["artifacts_published"], False)
        self.assertEqual(L.RUN_ROOT, ROOT / ".tools/evidence/phase4-valkey-inventory-v2")
        self.assertEqual(L.CORE_SHA, CORE_SHA)


class RealPreparedOutputTests(unittest.TestCase):
    def test_real_readonly_prepare_and_all_five_outputs_publish_in_private_fixture(self):
        if not REAL_REQUESTED:
            self.skipTest("La regresión con los artefactos privados se habilita con --real.")
        before_root = L.RUN_ROOT.exists()
        old_cli = (ROOT / "docker/valkey/inventory_image.py").read_bytes()
        core = (ROOT / "docker/valkey/inventory_core.py").read_bytes()
        plan, outputs = L.prepare()
        self.assertEqual(set(outputs), NAMES)
        self.assertEqual(plan["sources"]["core"], CORE_SHA)
        self.assertEqual(plan["counts"]["apk"], 23)
        self.assertEqual(plan["counts"]["native_binaries"], 3)
        with tempfile.TemporaryDirectory(prefix="valkey-publication-v2-") as temporary:
            assert_publication(self, Path(temporary) / "outputs", plan, outputs)
        self.assertEqual(L.RUN_ROOT.exists(), before_root)
        self.assertEqual((ROOT / "docker/valkey/inventory_image.py").read_bytes(), old_cli)
        self.assertEqual((ROOT / "docker/valkey/inventory_core.py").read_bytes(), core)
        self.assertEqual(hashlib.sha256(core).hexdigest(), CORE_SHA)


def load_tests(loader, _standard, _pattern):
    suite = loader.loadTestsFromTestCase(PublicationTests)
    if REAL_REQUESTED:
        suite.addTests(loader.loadTestsFromTestCase(RealPreparedOutputTests))
    return suite


if __name__ == "__main__":
    if "--real" in sys.argv:
        REAL_REQUESTED = True
        sys.argv.remove("--real")
    unittest.main()
