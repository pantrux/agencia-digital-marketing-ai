import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "docker/supoclip/backend_runtime.py"
SPEC = importlib.util.spec_from_file_location("supoclip_backend_runtime", SOURCE)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def make_archive(rows):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, raw in rows:
            node = tarfile.TarInfo(name)
            node.size, node.mode = len(raw), 0o444
            archive.addfile(node, io.BytesIO(raw))
    return output.getvalue()


class BackendRuntimeTests(unittest.TestCase):
    def test_unpack_preserves_exact_member_and_empty_file(self):
        rows = [("backend/src/a.py", b""), ("LICENSE", b"licence\n")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "files.tar"
            bundle.write_bytes(make_archive(rows))
            runtime.unpack(bundle, root / "target", dict(map(self.pin, rows)))
            for name, raw in rows:
                self.assertEqual((root / "target" / name).read_bytes(), raw)

    def test_archive_member_scope_and_tampering_fail(self):
        rows = [("backend/src/a.py", b"pass\n")]
        for members in (
            [("backend/src/a.py", b"evil\n")],
            [("../escape", b"escape")],
            rows + rows,
        ):
            with self.subTest(members=members), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bundle = root / "files.tar"
                bundle.write_bytes(make_archive(members))
                with self.assertRaises(ValueError):
                    runtime.unpack(bundle, root / "target", dict(map(self.pin, rows)))
                self.assertFalse((root / "escape").exists())

    def test_locked_requirements_are_unique(self):
        pin = b"0" * 64
        row = b"package_name==1.2.3 --hash=sha256:" + pin + b"\n"
        self.assertEqual(runtime.locked_distributions(row), {"package-name": "1.2.3"})
        with self.assertRaises(ValueError):
            runtime.locked_distributions(row + row.replace(b"_", b"-"))

    @staticmethod
    def pin(row):
        name, raw = row
        return name, {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
