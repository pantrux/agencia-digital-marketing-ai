import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "n8n"))

import pack_wa_sqlite_wasm_delta as delta  # noqa: E402


def wasm(version, source_id):
    return b"\0asm\1\0\0\0" + version + b"\0" + source_id + b"\0"


def module(name):
    jspi = "WebAssembly.promising; WebAssembly.Suspending;" if name.endswith("jspi") else ""
    return f'const wasm = "{name}.wasm"; {jspi} export default () => wasm;\n'.encode()


def fixture():
    original = {"package/LICENSE": (b"licencia original\n", 0o644)}
    built = {}
    for name in delta.VARIANTS:
        for ext, old, new in (
            ("mjs", module(name) + b"// original", module(name)),
            ("wasm", wasm(b"3.50.1", delta.SQLITE_OLD_ID), wasm(b"3.54.1", delta.SOURCE_ID)),
        ):
            filename = f"{name}.{ext}"
            original[f"package/dist/{filename}"] = (old, 0o644)
            built[filename] = new
    return original, built


class WasmDeltaTests(unittest.TestCase):
    def test_replaces_only_six_members_and_keeps_original_bytes_and_mode(self):
        original, built = fixture()
        untouched = dict(original)
        raw, receipt = delta.repack(original, built)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            self.assertEqual(set(tar.getnames()), set(original))
            for name, expected in built.items():
                self.assertEqual(tar.extractfile(f"package/dist/{name}").read(), expected)
                self.assertEqual(receipt["replaced"][name]["new_sha256"], delta.sha(expected))
            self.assertEqual(
                tar.extractfile("package/LICENSE").read(), original["package/LICENSE"][0]
            )
            self.assertEqual(tar.getmember("package/LICENSE").mode, 0o644)
        self.assertEqual(original, untouched)
        self.assertEqual(receipt["package_integrity"], delta.sri(raw))
        self.assertFalse(receipt["browser_verified"])
        self.assertEqual(delta.repack(original, built), (raw, receipt))

    def test_rejects_old_wasm_or_lookalike_new_build(self):
        original, built = fixture()
        for invalid in (
            original["package/dist/wa-sqlite.wasm"][0],
            wasm(b"3.54.1", b"otro id"),
            wasm(b"3.54.1", delta.SOURCE_ID) + delta.SQLITE_OLD_ID,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                delta.repack(original, {**built, "wa-sqlite.wasm": invalid})

    def test_rejects_loader_with_wrong_pair_or_no_jspi(self):
        original, built = fixture()
        for invalid in (
            module("wa-sqlite-async"),
            module("wa-sqlite-jspi").replace(b"WebAssembly.Suspending", b"NoJSPI"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                delta.repack(original, {**built, "wa-sqlite-jspi.mjs": invalid})

    def test_rejects_missing_extra_or_directory_output(self):
        _, built = fixture()
        with tempfile.TemporaryDirectory() as root:
            dist = Path(root)
            for name, raw in built.items():
                (dist / name).write_bytes(raw)
            self.assertEqual(delta.read_dist(dist), built)
            extra = dist / "extra.wasm"
            extra.touch()
            with self.assertRaises(ValueError):
                delta.read_dist(dist)
            extra.unlink()
            leaf = dist / "wa-sqlite.wasm"
            leaf.unlink()
            with self.assertRaises(ValueError):
                delta.read_dist(dist)
            leaf.mkdir()
            with self.assertRaises(ValueError):
                delta.read_dist(dist)

    def test_requires_new_exact_package_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / delta.OUTPUT
            with self.assertRaises(ValueError):
                delta.write_new(path.with_name("baseline.tgz"), b"payload")
            delta.write_new(path, b"payload")
            with self.assertRaises(FileExistsError):
                delta.write_new(path, b"other")
            self.assertEqual(path.read_bytes(), b"payload")


if __name__ == "__main__":
    unittest.main()
