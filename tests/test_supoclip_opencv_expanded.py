import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docker/supoclip/opencv_expanded.py"


class ExpandedOpenCvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("opencv_expanded_test", SOURCE)
        cls.build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.build)

    def config(self):
        b = self.build
        cache = "\n".join(f"{name}:STRING={value}" for name, value in b.REQUIRED.items())
        macros = "\n".join(f"#define HAVE_{name} 1" for name in b.CODECS)
        modules = "\n".join(
            f"#define HAVE_OPENCV_{name.upper()}" for name in b.MODULES if name != "python3"
        )
        ninja = (
            "COMMAND = /usr/bin/protoc-29.5.0 --cpp_out=. opencv-onnx.proto\n"
            "DEFINES = -DHAVE_FFMPEG -DHAVE_WEBP\n"
            "build cv2.cpython-312-x86_64-linux-gnu.so: fixture\n"
        )
        log = "--   Protobuf: /usr/lib/libprotobuf.so.29.5.0 (29.5.0)\n"
        return [cache, macros, modules, ninja, log]

    def test_external_providers_and_generated_onnx_gate(self):
        b = self.build
        b.check_config(*self.config())
        for index, old, new in (
            (0, "BUILD_PROTOBUF:STRING=OFF", "BUILD_PROTOBUF:STRING=ON"),
            (0, "__VALID_FFMPEG:STRING=TRUE", "__VALID_FFMPEG:STRING=FALSE"),
            (1, "HAVE_JPEG", "OTHER_JPEG"),
            (2, "HAVE_OPENCV_DNN", "OTHER_DNN"),
            (3, "opencv-onnx.proto", "unrelated.proto"),
            (4, "/usr/lib/libprotobuf.so.29.5.0", "build"),
        ):
            with self.subTest(field=old):
                values = self.config()
                values[index] = values[index].replace(old, new)
                with self.assertRaises(ValueError):
                    b.check_config(*values)

    def test_loader_relocation_preserves_relative_python_extensions(self):
        b = self.build
        loader = b"BINARIES_PATHS = [os.path.join(LOADER_DIR, 'lib')] + BINARIES_PATHS\n"
        py = b"PYTHON_EXTENSIONS_PATHS = [os.path.join(LOADER_DIR, 'python-3.12')]\n"
        self.assertEqual(b.relocate_loader(loader, py), loader.replace(b"'lib'", b"'.libs'"))
        for bad in (loader + loader, loader.replace(b"LOADER_DIR", b"'/work/stage'")):
            with self.assertRaises(ValueError):
                b.relocate_loader(bad, py)

    def test_wheel_record_reuses_zou_validator(self):
        b = self.build
        validate = b.helper(
            ROOT / "docker/kitsu-zou/inventory_opencv_wheel.py",
            b.RECORD_HELPER_SHA,
            ("validate_record",),
        )["validate_record"]
        files = {"cv2/__init__.py": b"# loader\n", "cv2/data/fixture.xml": b"fixture"}
        files[b.DIST + "/RECORD"] = b.make_record(files)
        validate(files, b.DIST)
        files["cv2/data/fixture.xml"] = b"altered"
        with self.assertRaises(ValueError):
            validate(files, b.DIST)
