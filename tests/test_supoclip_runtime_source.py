import ast
import importlib.util
import io
import tarfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "docker/supoclip/prepare_runtime_source.py"
SPEC = importlib.util.spec_from_file_location("supoclip_runtime_source", SOURCE)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeSourceTests(unittest.TestCase):
    def test_only_subtitle_function_uses_private_ffmpeg(self):
        before = b'def render():\n    return ["ffmpeg"]\n\ndef extract():\n    return ["ffmpeg"]\n'
        after = runtime.patch_ass(before, "render")
        calls = {
            function.name: function.body[0].value.elts[0].value
            for function in ast.parse(after).body
        }
        self.assertEqual(
            calls, {"render": "/opt/supoclip-ffmpeg-ass/bin/ffmpeg", "extract": "ffmpeg"}
        )

    def test_ambiguous_subtitle_argv_rejected(self):
        before = b'def render():\n    return ["ffmpeg"], ["ffmpeg"]\n'
        with self.assertRaises(ValueError):
            runtime.patch_ass(before, "render")

    def test_reframe_keeps_stock_binary_without_subtitles(self):
        before = (
            b"def render_reframed_clip_ffmpeg(subs):\n"
            b'    if not exists("ffmpeg"):\n        raise RuntimeError\n'
            b'    return ["ffmpeg"]\n'
        )
        after = runtime.patch_ass(before, "render_reframed_clip_ffmpeg")
        namespace = {"exists": lambda name: name == "ffmpeg"}
        exec(after, namespace)
        render = namespace["render_reframed_clip_ffmpeg"]
        self.assertEqual(render(None), ["ffmpeg"])
        self.assertEqual(render("subtitles=fixture.ass"), [runtime.ASS])

    def test_context_tar_reproducible_and_modes_safe(self):
        members = {"backend/src/main.py": b"pass\n", "LICENSE": b"license"}
        archive = runtime.make_tar(members)
        self.assertEqual(archive, runtime.make_tar(dict(reversed(tuple(members.items())))))
        with tarfile.open(fileobj=io.BytesIO(archive)) as reader:
            self.assertEqual([member.name for member in reader], sorted(members))
            for member in reader.getmembers():
                self.assertEqual(
                    (member.uid, member.gid, member.mode, member.mtime), (0, 0, 0o444, 0)
                )
                self.assertEqual(reader.extractfile(member).read(), members[member.name])

    def test_replace_requires_exactly_one_preimage(self):
        with self.assertRaises(ValueError):
            runtime.replace_once(b"upstream upstream", b"upstream", b"local")
