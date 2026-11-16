"""Regresiones de la distribución local de Zou, sin instalar código descargado."""

import base64
import csv
import importlib.util
import io
import json
import os
import py_compile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "docker/kitsu-zou/derive_zou_wheel.py"
spec = importlib.util.spec_from_file_location("zou_derivative", MODULE)
derive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(derive)


def fixture(changes=None):
    members = {
        "zou/__init__.py": b'__version__ = "1.1.1"\n',
        "zou-1.1.1.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: zou\nVersion: 1.1.1\n"
            b"Requires-Python: >=3.11, <3.15\n"
            b"Requires-Dist: psycopg[binary]==3.3.4\n"
            b'Requires-Dist: psycopg[c]==3.3.4; extra == "prodpg"\n'
            b"Requires-Dist: sqlalchemy==2.0.49\nProvides-Extra: prodpg\n"
            b"\nDescripcion exacta: \xc3\xb1\nVersion: 1.1.1\n"
        ),
        "zou-1.1.1.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
        ),
        "zou-1.1.1.dist-info/licenses/LICENSE": b"AGPL-3.0-only\n",
        "zou-1.1.1.dist-info/entry_points.txt": b"[console_scripts]\nzou=zou.cli:cli\n",
    }
    members.update(changes or {})
    record = "zou-1.1.1.dist-info/RECORD"
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\n")
    for name, raw in sorted(members.items()):
        digest = base64.urlsafe_b64encode(derive.hashlib.sha256(raw).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + digest.decode(), str(len(raw))))
    writer.writerow((record, "", ""))
    members[record] = text.getvalue().encode()
    return pack(members)


def pack(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    return output.getvalue()


class ZouWheelTests(unittest.TestCase):
    def test_derivative_keeps_code_license_and_entry_points(self):
        original = fixture()
        result, proof = derive.derive_bytes(original)
        with (
            zipfile.ZipFile(io.BytesIO(original)) as before,
            zipfile.ZipFile(io.BytesIO(result)) as after,
        ):
            for name in before.namelist():
                if name.endswith(("/METADATA", "/RECORD")):
                    continue
                mapped = name.replace("zou-1.1.1.dist-info/", "zou-1.1.1+agency1.dist-info/")
                self.assertEqual(before.read(name), after.read(mapped))
            metadata = after.read("zou-1.1.1+agency1.dist-info/METADATA")
            expected = before.read("zou-1.1.1.dist-info/METADATA")
            expected = expected.replace(b"\nVersion: 1.1.1\n", b"\nVersion: 1.1.1+agency1\n", 1)
            expected = expected.replace(b"psycopg[binary]==3.3.4", b"psycopg==3.3.4", 1)
            expected = expected.replace(b"psycopg[c]==3.3.4", b"psycopg==3.3.4", 1)
            self.assertEqual(metadata, expected)
            self.assertIn(b"Version: 1.1.1+agency1\n", metadata)
            self.assertIn(b'Requires-Dist: psycopg==3.3.4; extra == "prodpg"', metadata)
            self.assertIn(b"Descripcion exacta: \xc3\xb1\nVersion: 1.1.1\n", metadata)
            self.assertNotIn(b"psycopg[", metadata)
        self.assertEqual(proof["unchanged_members"], 4)
        self.assertEqual(derive.derive_bytes(original)[0], result)

    def test_record_covers_every_changed_path_and_digest(self):
        result, _ = derive.derive_bytes(fixture())
        members = derive.inspect_wheel(result, derive.NEW_DIST)
        self.assertIn(derive.NEW_DIST + "/RECORD", members)
        self.assertFalse(any("1.1.1.dist-info" in name for name in members))

    def test_rejects_modified_source_record(self):
        raw = fixture()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["zou/__init__.py"] += b"changed"
        with self.assertRaisesRegex(ValueError, "RECORD_HASH"):
            derive.derive_bytes(pack(members))

    def test_rejects_ambiguous_metadata_and_missing_expected_line(self):
        for bad in [b"Version: 1.1.1\nVersion: 1.1.1\n", b"Version: 1.1.2\n"]:
            with self.subTest(bad=bad):
                raw = fixture()
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    meta = archive.read("zou-1.1.1.dist-info/METADATA")
                raw = fixture(
                    {"zou-1.1.1.dist-info/METADATA": meta.replace(b"Version: 1.1.1\n", bad, 1)}
                )
                with self.assertRaises(ValueError):
                    derive.derive_bytes(raw)

    def test_rejects_traversal_case_collision_signatures_and_symlinks(self):
        for name in [
            "../evil",
            "zou/../evil",
            "C:/evil",
            "zou/evil:stream",
            "ZOU/__init__.py",
            "zou-1.1.1.dist-info/RECORD.jws",
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                derive.derive_bytes(fixture({name: b"foreign"}))
        raw = fixture()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = [(i, archive.read(i)) for i in archive.infolist()]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for info, body in entries:
                if info.filename == "zou/__init__.py":
                    info.external_attr = 0o120777 << 16
                archive.writestr(info, body)
        with self.assertRaisesRegex(ValueError, "ZIP_TYPE"):
            derive.derive_bytes(stream.getvalue())

    def test_rejects_duplicate_zip_record_and_local_header_name(self):
        raw = fixture()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        rec = "zou-1.1.1.dist-info/RECORD"
        members[rec] += members[rec].splitlines(keepends=True)[0]
        with self.assertRaisesRegex(ValueError, "RECORD_DUPLICATE"):
            derive.derive_bytes(pack(members))
        raw = bytearray(fixture())
        offset = raw.find(b"zou/__init__.py")
        raw[offset] = ord("Z")
        with self.assertRaises(ValueError):
            derive.derive_bytes(bytes(raw))

    def test_rejects_archive_bounds(self):
        with self.assertRaisesRegex(ValueError, "ZIP_SIZE"):
            derive.inspect_wheel(fixture(), derive.OLD_DIST, max_total=16)

    def test_rejects_duplicate_zip_members_and_corrupt_crc(self):
        stream = io.BytesIO(fixture())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(stream, "a") as archive:
                archive.writestr("zou/__init__.py", b"foreign")
        with self.assertRaisesRegex(ValueError, "ZIP_DUPLICATE"):
            derive.derive_bytes(stream.getvalue())
        raw = fixture().replace(b'__version__ = "1.1.1"', b'__version__ = "1.1.2"', 1)
        with self.assertRaisesRegex(ValueError, "ZIP_INVALID"):
            derive.derive_bytes(raw)


def graph_metadata(name, version="1.0", requirements=(), extras=()):
    return (
        f"Name: {name}\nVersion: {version}\nRequires-Python: >=3.11\n"
        + "".join(f"Requires-Dist: {line}\n" for line in requirements)
        + "".join(f"Provides-Extra: {extra}\n" for extra in extras)
        + "\n"
    ).encode()


class ClosureTests(unittest.TestCase):
    def data(self):
        return {
            "zou": graph_metadata(
                "zou",
                "1.1.1+agency1",
                [
                    "psycopg==3.3.4",
                    'psycopg==3.3.4; extra == "prodpg"',
                    "sqlalchemy==2.0.49",
                    "storage[s3,swift]==1.0",
                ],
                ["prodpg"],
            ),
            "psycopg": graph_metadata(
                "psycopg",
                "3.3.4",
                [
                    'typing-extensions>=4.6; python_version < "3.13"',
                    'psycopg-binary==3.3.4; extra == "binary"',
                    'psycopg-c==3.3.4; extra == "c"',
                    'psycopg-pool; extra == "pool"',
                ],
                ["binary", "c", "pool"],
            ),
            "sqlalchemy": graph_metadata("sqlalchemy", "2.0.49"),
            "storage": graph_metadata(
                "storage",
                requirements=['s3; extra == "s3"', 'swift; extra == "swift"'],
                extras=["s3", "swift"],
            ),
            "s3": graph_metadata("s3"),
            "swift": graph_metadata("swift"),
            "typing-extensions": graph_metadata("typing-extensions", "4.15.0"),
            "psycopg-binary": graph_metadata("psycopg-binary", "3.3.4"),
        }

    def test_preserves_marker_extras_and_typing_without_inventing_pool(self):
        for extras in [(), ("prodpg",)]:
            closure = derive.validate_closure(self.data(), extras=extras)
            self.assertEqual(closure["excluded"], ["psycopg-binary"])
            self.assertEqual(closure["selected_count"], 7)
            self.assertEqual(closure["extras"]["psycopg"], [])
            self.assertEqual(closure["extras"]["storage"], ["s3", "swift"])
            self.assertEqual(closure["active_edges"], 7 if extras else 6)

    def test_rejects_missing_transitive_incompatible_version_and_unknown_extra(self):
        for mode in ["missing", "version", "extra", "url"]:
            data = self.data()
            if mode == "missing":
                del data["swift"]
            elif mode == "version":
                data["sqlalchemy"] = graph_metadata("sqlalchemy", "1.0")
            elif mode == "extra":
                data["storage"] = graph_metadata("storage", extras=["s3"])
            else:
                data["s3"] = graph_metadata("s3", requirements=["evil @ https://example.com/a.whl"])
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                derive.validate_closure(data)

    def test_sca_requires_exact_versions_without_skips_duplicates_or_findings(self):
        inventory = [{"name": "zou", "version": "1.1.1"}, {"name": "psycopg", "version": "3.3.4"}]
        rows = [{**row, "vulns": []} for row in inventory]
        result = derive.validate_baseline_sca({"dependencies": rows}, inventory, ["zou"])
        self.assertEqual(result["selected_upstream_versions"], 1)
        self.assertFalse(result["new_queries"])
        for altered in [
            rows[:-1],
            rows + [rows[0]],
            [rows[0], {**rows[1], "version": "3.3.3"}],
            [rows[0], {**rows[1], "skip_reason": "unsupported"}],
            [{**rows[0], "vulns": [{"id": "CVE-fixture"}]}, rows[1]],
        ]:
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                derive.validate_baseline_sca({"dependencies": altered}, inventory, ["zou"])


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.raw, self.proof = derive.derive_bytes(fixture())
        self.plan = {"schema_version": 1, "manifest_sha256": "a" * 64, "wheel": self.proof}
        self.pin = derive.digest(derive.canonical_json(self.plan))

    def test_requires_reviewed_hash_before_writing(self):
        with self.assertRaisesRegex(ValueError, "PLAN_PIN"):
            derive.publish(self.root, self.raw, self.plan, "b" * 64)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_exclusive_publication_has_receipt_and_is_idempotent(self):
        first = derive.publish(self.root, self.raw, self.plan, self.pin)
        second = derive.publish(self.root, self.raw, self.plan, self.pin)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual((self.root / derive.OUTPUT).read_bytes(), self.raw)
        receipt = json.loads((self.root / "derived-receipt.json").read_bytes())
        self.assertEqual(receipt["wheel_sha256"], derive.digest(self.raw))

    def test_preserves_foreign_output_and_refuses_missing_intent(self):
        target = self.root / derive.OUTPUT
        target.write_bytes(self.raw)
        with self.assertRaisesRegex(ValueError, "OUTPUT_WITHOUT_INTENT"):
            derive.publish(self.root, self.raw, self.plan, self.pin)
        self.assertEqual(target.read_bytes(), self.raw)

    def test_detects_tamper_after_intent_and_never_overwrites(self):
        derive.publish(self.root, self.raw, self.plan, self.pin)
        target = self.root / derive.OUTPUT
        target.write_bytes(b"foreign")
        with self.assertRaisesRegex(ValueError, "OUTPUT_CHANGED"):
            derive.publish(self.root, self.raw, self.plan, self.pin)
        self.assertEqual(target.read_bytes(), b"foreign")

    def test_refuses_hardlink_without_reading_other_file(self):
        target = self.root / "target"
        target.write_bytes(b"foreign")
        other = self.root / "hardlink"
        derive.os.link(target, other)
        with self.assertRaisesRegex(ValueError, "FILE_TYPE"):
            derive.read_file(other, 1024)

    def test_bounds_file_reads_and_rejects_ambiguous_json(self):
        target = self.root / "input.json"
        target.write_bytes(b'{"owner":1,"owner":2}')
        with self.assertRaisesRegex(ValueError, "FILE_SIZE"):
            derive.read_file(target, 4)
        for raw in [target.read_bytes(), b'{"value": NaN}', b'{"value": Infinity}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                derive.strict_json(raw)


class AccessLoaderTests(unittest.TestCase):
    def test_executes_only_verified_bytes_despite_poisoned_pyc_or_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "helper.py"
            source = (
                b"def check_access(path, *, private=False):\n"
                b"    return (__file__, str(path), private)\n"
            )
            helper.write_bytes(source)
            poison = root / "poison.py"
            poison_source = b"raise AssertionError('PYC_OR_LOADER_EXECUTED')\n"
            poison.write_bytes(poison_source + b"#" * (len(source) - len(poison_source)))
            timestamp = int(helper.stat().st_mtime)
            os.utime(helper, (timestamp, timestamp))
            os.utime(poison, (timestamp, timestamp))
            cache = Path(importlib.util.cache_from_source(str(helper)))
            cache.parent.mkdir()
            py_compile.compile(str(poison), cfile=str(cache), doraise=True)
            manifest = {"access_helper": {"path": "helper.py", "sha256": derive.digest(source)}}
            with patch.object(derive, "WORKSPACE", root):
                access = derive.load_access(manifest)
                self.assertEqual(access("fixture", private=True), (str(helper), "fixture", True))
                with patch(
                    "importlib.util.spec_from_file_location",
                    side_effect=AssertionError("LOADER_NOT_ALLOWED"),
                ):
                    access = derive.load_access(manifest)
                    self.assertEqual(access("fixture"), (str(helper), "fixture", False))


if __name__ == "__main__":
    unittest.main()
