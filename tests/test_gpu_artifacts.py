"""Las fuentes del puente no pueden sustituir archivos ni escapar de su caché."""

import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"))
from gpu_artifacts import extract_source, verify_artifact  # noqa: E402


class ArtifactTests(unittest.TestCase):
    def test_hash_and_size_are_both_required(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "fixture"
            file.write_bytes(b"contenido")
            metadata = {"sha256": hashlib.sha256(b"contenido").hexdigest(), "size": 9}
            verify_artifact(file, metadata)
            with self.assertRaises(ValueError):
                verify_artifact(file, metadata | {"size": 8})
            file.write_bytes(b"diferente")
            with self.assertRaises(ValueError):
                verify_artifact(file, metadata)

    def test_selected_source_is_idempotent_and_preserves_other_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for name in ("repo/machine-learning/app.py", "repo/other/ignore.py"):
                    entry = tarfile.TarInfo(name)
                    entry.size = 1
                    bundle.addfile(entry, io.BytesIO(b"x"))
            for _ in range(2):
                extract_source(archive, root / "vendor", "machine-learning")
            self.assertEqual((root / "vendor/app.py").read_bytes(), b"x")
            self.assertFalse((root / "vendor/other").exists())
            (root / "vendor/app.py").write_bytes(b"ajeno")
            with self.assertRaises(ValueError):
                extract_source(archive, root / "vendor", "machine-learning")

    def test_traversal_and_symlinks_rejected(self):
        for name, link in (
            ("repo/machine-learning/../../fuera", False),
            ("repo/machine-learning/link", True),
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "source.tar.gz"
                with tarfile.open(archive, "w:gz") as bundle:
                    entry = tarfile.TarInfo(name)
                    if link:
                        entry.type, entry.linkname = tarfile.SYMTYPE, "../../fuera"
                    bundle.addfile(entry)
                with self.assertRaises(ValueError):
                    extract_source(archive, root / "vendor", "machine-learning")

    def test_explicit_extraction_budget_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                entry = tarfile.TarInfo("repo/app.py")
                entry.size = 2
                bundle.addfile(entry, io.BytesIO(b"xx"))
            with self.assertRaises(ValueError):
                extract_source(archive, root / "vendor", "", max_member_bytes=1)


if __name__ == "__main__":
    unittest.main()
