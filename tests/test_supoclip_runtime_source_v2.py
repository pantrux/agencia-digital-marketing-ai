import ast
import importlib.util
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "docker/supoclip/prepare_runtime_source_v2.py"
SPEC = importlib.util.spec_from_file_location("supoclip_runtime_source_v2", SOURCE)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeSourceV2Tests(unittest.TestCase):
    def test_editor_uses_private_ass_and_keeps_stock_probe(self):
        source = (
            b"def render_document(path):\n"
            b'    probe = ["ffprobe", path]\n'
            b'    filters = f"ass={path}"\n'
            b'    command = ["ffmpeg", "-filter_complex", filters]\n'
            b"    return probe, command\n"
            b'\ndef render_other(path):\n    return ["ffmpeg", path]\n'
        )
        previous = runtime.load_previous()
        actual = runtime.patch_editor(previous, source)
        self.assertEqual(actual.count(previous.ASS.encode()), 1)
        tree = ast.parse(actual)
        namespace = {}
        exec(compile(tree, "<fixture>", "exec"), namespace)
        self.assertEqual(
            namespace["render_document"]("captions.ass"),
            (["ffprobe", "captions.ass"], [previous.ASS, "-filter_complex", "ass=captions.ass"]),
        )
        self.assertEqual(namespace["render_other"]("clip.mp4"), ["ffmpeg", "clip.mp4"])

    def test_editor_patch_rejects_multiple_av_candidates(self):
        source = (
            b"def render_document(path):\n"
            b'    filters = f"ass={path}"\n'
            b'    return ["ffmpeg", filters], ["ffmpeg", path]\n'
        )
        with self.assertRaises(ValueError):
            runtime.patch_editor(runtime.load_previous(), source)
