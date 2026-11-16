"""El paquete del renderizador incluye fuente propia y excluye cachés descargables."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"))
from install_render import install_render, source_bundle  # noqa: E402


class RenderInstallTests(unittest.TestCase):
    def test_manifest_excludes_dependencies_artifacts_and_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "src/start.ts",
                "composition/index.tsx",
                "node_modules/library.js",
                "artifacts/browser.zip",
                "tests/local.test.ts",
                "package-lock.json",
            ):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(name, encoding="utf-8")
            files, digest = source_bundle(root)
            self.assertEqual(
                set(files), {"src/start.ts", "composition/index.tsx", "package-lock.json"}
            )
            self.assertEqual(len(digest), 64)
            (root / "src/start.ts").write_text("cambio", encoding="utf-8")
            self.assertNotEqual(source_bundle(root)[1], digest)

    def test_source_update_rejects_changed_dependency_lock_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, installed = root / "source", root / "render-worker"
            source.mkdir()
            installed.mkdir()
            (source / "package-lock.json").write_text('{"packages":{}}', encoding="utf-8")
            (installed / "package-lock.json").write_text("anterior", encoding="utf-8")
            files, digest = source_bundle(source)
            mapping = {
                "GPU_RENDER_WORKER_ROOT": str(installed),
                "GPU_RENDER_SOURCE_BUNDLE_SHA256": digest,
                "GPU_RENDER_PACKAGE_LOCK_SHA256": files["package-lock.json"],
            }
            with (
                patch("install_render.RENDER_SOURCE", source),
                patch(
                    "install_render.require_installation",
                    return_value=SimpleNamespace(install_root=root),
                ),
                self.assertRaises(ValueError),
            ):
                install_render(mapping, source_only=True)
            self.assertEqual((installed / "package-lock.json").read_text(), "anterior")


if __name__ == "__main__":
    unittest.main()
