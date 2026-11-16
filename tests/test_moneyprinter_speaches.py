"""Contrato MIT de transcripción remota; sin importar la aplicación ni modelos."""

import ast
import contextlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/supoclip"))
spec = importlib.util.spec_from_file_location(
    "moneyprinter_speaches", ROOT / "docker/moneyprinter/speaches_provider.py"
)
provider = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = provider
spec.loader.exec_module(provider)
sys.path.pop(0)


def response():
    words = [
        {"start": 0, "end": 0.4, "word": " El", "probability": 0.9},
        {"start": 0.4, "end": 1, "word": " 3.14", "probability": 0.8},
        {"start": 1, "end": 1.5, "word": " llegó.", "probability": 0.7},
        {"start": 1.5, "end": 2, "word": " ¡Sí!", "probability": 0.9},
    ]
    return {
        "language": "es",
        "duration": 2,
        "text": "El 3.14 llegó. ¡Sí!",
        "words": words,
        "segments": [{"start": 0, "end": 2, "text": "El 3.14 llegó. ¡Sí!", "words": words}],
    }


class RemoteContract(unittest.TestCase):
    def test_word_and_sentence_modes_preserve_recognition_and_seconds(self):
        data = response()
        words = provider.convert_response(data, word_level=True)
        self.assertEqual([c.text for c in words.cues], ["El", "3.14", "llegó.", "¡Sí!"])
        self.assertEqual(
            [(c.start, c.end) for c in words.cues], [(0.0, 0.4), (0.4, 1.0), (1.0, 1.5), (1.5, 2.0)]
        )
        sentences = provider.convert_response(data, word_level=False)
        self.assertEqual([c.text for c in sentences.cues], ["El 3.14 llegó.", "¡Sí!"])
        self.assertEqual(
            provider.render_srt(sentences),
            "1\n00:00:00,000 --> 00:00:01,500\nEl 3.14 llegó.\n\n2\n00:00:01,500 --> 00:00:02,000\n¡Sí!\n\n",
        )
        self.assertEqual(data, response())

    def test_real_silence_is_distinct_and_never_fabricates_cues(self):
        outcome = provider.convert_response(
            {"language": None, "duration": 1, "text": "", "words": [], "segments": []}
        )
        self.assertEqual(outcome.status, "silence")
        self.assertEqual(outcome.cues, ())
        self.assertEqual(provider.render_srt(outcome), "")

    def test_missing_probability_or_inconsistent_words_fail(self):
        for mutate in [
            lambda x: x["words"][0].pop("probability"),
            lambda x: x["words"][0].update(start=-1),
            lambda x: x.update(text=""),
        ]:
            data = response()
            mutate(data)
            with self.assertRaisesRegex(provider.ProviderError, "MPT_SPEACHES_INVALID_RESPONSE"):
                provider.convert_response(data)

    def test_unknown_provider_and_wrong_types_never_choose_default(self):
        for value in [None, "", "whisper", "Speaches", "http://example", 1, True]:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(provider.ProviderError, "MPT_SUBTITLE_PROVIDER_REJECTED"),
            ):
                provider.select_provider(value)
        self.assertEqual(provider.select_provider("speaches"), "speaches")
        self.assertEqual(provider.select_provider("edge"), "edge")

    def test_fixed_client_arguments_cancel_and_errors_do_not_leak(self):
        cancel = object()
        with patch.object(provider, "transcribe_audio", return_value=response()) as request:
            outcome = provider.transcribe(
                "fixture.mp3", "private-token", word_level=True, cancel_event=cancel
            )
        self.assertEqual(outcome.status, "transcribed")
        self.assertEqual(
            request.call_args.kwargs,
            {
                "cancel_event": cancel,
                "language": None,
                "total_seconds": 300,
                "idle_seconds": 15,
                "first_byte_seconds": 45,
            },
        )
        for error in [
            OSError("secret-token-body"),
            provider.SpeachesError("secret-token-body"),
            provider.SpeachesError("SPEACHES_BUSY"),
        ]:
            with patch.object(provider, "transcribe_audio", side_effect=error):
                with self.assertRaises(provider.ProviderError) as caught:
                    provider.transcribe("fixture.mp3", "private-token")
            self.assertNotIn("secret", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_invalid_mode_is_rejected_before_transport(self):
        with patch.object(
            provider, "transcribe_audio", side_effect=AssertionError("NO_HTTP")
        ) as request:
            with self.assertRaisesRegex(provider.ProviderError, "MPT_SPEACHES_INPUT_REJECTED"):
                provider.transcribe("fixture.mp3", "private", word_level=1)
        request.assert_not_called()

    def test_serializer_rejects_forged_cues_without_inventing_duration(self):
        for cue in [
            provider.Cue("text\nforged", 0.0, 1.0),
            provider.Cue("text", 2.0, 1.0),
            provider.Cue("", 0.0, 1.0),
        ]:
            with self.subTest(cue=cue), self.assertRaises(provider.ProviderError):
                provider.render_srt(provider.Outcome("transcribed", (cue,)))


def patcher_module():
    path = ROOT / "docker/moneyprinter/apply_speaches_provider.py"
    namespace = {"__file__": str(path), "__name__": "mpt_patch_test"}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def extracted(raw, names, scope):
    nodes = [n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef) and n.name in names]
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, "<funciones-MIT-parcheadas>", "exec"), scope)
    return scope


class OverlayContract(unittest.TestCase):
    # Fixtures MIT propios; no importan la aplicación ni código AGPL.
    subtitle_source = b"""import threading
from timeit import default_timer as timer
try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None
from app.config import config
model_size = "large-v3"
device = "cpu"
compute_type = "int8"
initial_prompt = None
model = None
_model_init_lock = threading.Lock()
def _ensure_model_loaded():
    raise AssertionError("MODELO_LOCAL")
def create(audio_file, subtitle_file="", word_level=False, log_details=True):
    _ensure_model_loaded()
def transcribe_audio_bytes(audio_bytes):
    return "fixture"
"""
    task_source = b"""from app.services import subtitle
def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    raise AssertionError("RUTA_SIN_PARCHE")
def start(task_id, params, video_script, sub_maker, audio_file):
    subtitle_path = generate_subtitle(task_id, params, video_script, sub_maker, audio_file)
    return {"subtitle_path": subtitle_path}
"""
    ui_source = b"""from app.services import subtitle
def _render_subtitle_settings(panel, params):
    subtitle_settings_disabled = not params.subtitle_enabled
    return "resto-de-la-interfaz"
"""

    def patched(self):
        patcher = patcher_module()
        return patcher, patcher.patch_application(
            {
                "app/services/subtitle.py": self.subtitle_source,
                "app/services/task.py": self.task_source,
                "webui/Main.py": self.ui_source,
            }
        )

    def test_local_imports_model_loader_and_download_path_are_removed(self):
        _, output = self.patched()
        tree = ast.parse(output["app/services/subtitle.py"])
        self.assertFalse(
            any(
                isinstance(n, ast.Name) and n.id in {"WhisperModel", "_ensure_model_loaded"}
                for n in ast.walk(tree)
            )
        )
        self.assertFalse(
            any(
                isinstance(n, ast.ImportFrom) and n.module == "faster_whisper"
                for n in ast.walk(tree)
            )
        )

    def test_create_silence_error_and_success_never_publish_old_result(self):
        _, output = self.patched()
        calls = []
        cfg = types.SimpleNamespace(
            app={"subtitle_provider": "speaches", "subtitle_speaches_token_file": "secret-file"}
        )
        scope = extracted(
            output["app/services/subtitle.py"],
            {"create"},
            {
                "config": cfg,
                "agency_speaches": provider,
                "write_subtitle_file": lambda target, body: calls.append((target, body)) or True,
            },
        )
        with patch.object(provider, "transcribe", return_value=provider.Outcome("silence", ())):
            self.assertEqual(scope["create"]("audio.mp3", "own-stage.srt"), "silence")
        self.assertEqual(calls, [])
        with patch.object(
            provider, "transcribe", side_effect=provider.ProviderError("MPT_SPEACHES_BUSY")
        ):
            with self.assertRaises(provider.ProviderError):
                scope["create"]("audio.mp3", "foreign.srt")
        self.assertEqual(calls, [])
        with patch.object(
            provider, "transcribe", return_value=provider.convert_response(response())
        ):
            self.assertIsNone(scope["create"]("audio.mp3", "own-stage.srt"))
        self.assertEqual(calls[0][0], "own-stage.srt")

    def test_task_failure_is_marked_and_speaches_works_without_submaker(self):
        _, output = self.patched()
        events = []
        scope = extracted(
            output["app/services/task.py"],
            {"start"},
            {
                "agency_speaches": provider,
                "generate_subtitle": lambda *_args: (_ for _ in ()).throw(
                    provider.ProviderError("MPT_SPEACHES_BUSY")
                ),
                "_mark_task_failed": lambda *args: events.append(args) or {"failed": True},
            },
        )
        self.assertEqual(scope["start"]("own-task", None, "", None, "audio.mp3"), {"failed": True})
        self.assertEqual(events, [("own-task", "subtitle", "MPT_SPEACHES_BUSY")])
        cfg = types.SimpleNamespace(app={"subtitle_provider": "speaches"})
        with tempfile.TemporaryDirectory() as temporary:
            final = Path(temporary) / "subtitle.srt"
            final.write_text("previous", encoding="utf-8")

            @contextlib.contextmanager
            def stage(_path):
                p = Path(temporary) / "stage.srt"
                p.write_text("", encoding="utf-8")
                try:
                    yield str(p)
                finally:
                    if p.exists():
                        p.unlink()

            def create(**kwargs):
                Path(kwargs["subtitle_file"]).write_text("recognized", encoding="utf-8")

            sub = types.SimpleNamespace(
                create=create,
                file_to_subtitles=lambda p: (
                    [(1, "time", Path(p).read_text())] if Path(p).read_text() else []
                ),
                correct=lambda **_kwargs: (_ for _ in ()).throw(
                    AssertionError("NO_CORREGIR_GUION")
                ),
            )
            scope = extracted(
                output["app/services/task.py"],
                {"generate_subtitle"},
                {
                    "config": cfg,
                    "agency_speaches": provider,
                    "logger": types.SimpleNamespace(info=lambda *_: None, warning=lambda *_: None),
                    "path": os.path,
                    "utils": types.SimpleNamespace(task_dir=lambda _: temporary),
                    "staged_subtitle_file": stage,
                    "subtitle": sub,
                    "os": os,
                },
            )
            params = types.SimpleNamespace(subtitle_enabled=True, subtitle_display_mode="sentence")
            self.assertEqual(
                scope["generate_subtitle"](
                    "own-task", params, "different script", None, "audio.mp3"
                ),
                str(final),
            )
            self.assertEqual(final.read_text(), "recognized")
            final.write_text("foreign-sentinel")
            sub.create = lambda **_kwargs: (_ for _ in ()).throw(
                provider.ProviderError("MPT_SPEACHES_BUSY")
            )
            with self.assertRaises(provider.ProviderError):
                scope["generate_subtitle"]("own-task", params, "", None, "audio.mp3")
            self.assertEqual(final.read_text(), "foreign-sentinel")
            sub.create = lambda **_kwargs: "silence"
            self.assertEqual(
                scope["generate_subtitle"]("own-task", params, "", None, "audio.mp3"), ""
            )
            self.assertEqual(final.read_text(), "foreign-sentinel")

    def test_bytes_caller_uses_own_temporary_wav_and_cleans_after_silence(self):
        _, output = self.patched()
        paths = []

        def create(audio, _subtitle, **_kwargs):
            paths.append(Path(audio))
            self.assertEqual(Path(audio).read_bytes(), b"synthetic-wav")
            return "silence"

        scope = extracted(
            output["app/services/subtitle.py"],
            {"transcribe_audio_bytes"},
            {
                "agency_speaches": provider,
                "tempfile": tempfile,
                "os": os,
                "create": create,
                "file_to_subtitles": lambda _path: (_ for _ in ()).throw(
                    AssertionError("NO_LEER_SRT_ANTERIOR")
                ),
            },
        )
        self.assertEqual(scope["transcribe_audio_bytes"](b"synthetic-wav"), "")
        self.assertFalse(paths[0].parent.exists())
        for data in [b"", None, b"x" * (24 * 1024**2 + 1)]:
            with (
                self.subTest(size=None if data is None else len(data)),
                self.assertRaises(provider.ProviderError),
            ):
                scope["transcribe_audio_bytes"](data)

    def test_ui_explains_effect_has_no_secret_input_and_rejects_unknown(self):
        _, output = self.patched()
        values, errors = [], []
        cfg = types.SimpleNamespace(app={"subtitle_provider": "speaches"})

        def select(label, **kwargs):
            self.assertEqual(kwargs["options"], ["speaches", "edge"])
            self.assertEqual(kwargs["format_func"]("speaches"), "Transcribir el audio")
            self.assertEqual(kwargs["format_func"]("edge"), "Usar tiempos de la voz generada")
            return "edge"

        scope = extracted(
            output["webui/Main.py"],
            {"_render_subtitle_provider"},
            {
                "config": cfg,
                "agency_speaches": provider,
                "stable_selectbox": select,
                "_set_runtime_config": lambda *v: values.append(v),
                "st": types.SimpleNamespace(error=lambda x: errors.append(x)),
            },
        )
        self.assertEqual(scope["_render_subtitle_provider"](False), "edge")
        self.assertEqual(values, [("app", "subtitle_provider", "edge")])
        cfg.app["subtitle_provider"] = "whisper"
        with self.assertRaises(provider.ProviderError):
            scope["_render_subtitle_provider"](False)
        self.assertEqual(len(values), 1)
        self.assertEqual(len(errors), 1)

    def test_unexpected_upstream_shape_rejects_without_rewriting(self):
        patcher = patcher_module()
        with self.assertRaises(ValueError):
            patcher.patch_application(
                {
                    "app/services/subtitle.py": self.subtitle_source.replace(
                        b"def create(", b"def renamed("
                    ),
                    "app/services/task.py": self.task_source,
                    "webui/Main.py": self.ui_source,
                }
            )


class PublicationGuards(unittest.TestCase):
    def test_no_publication_without_reviewed_hash_or_with_foreign_entry(self):
        patcher = patcher_module()
        outputs = {"app/services/example.py": b"# fixture propia\n"}
        plan = {"outputs": patcher.output_inventory(outputs)}
        pin = patcher.digest(patcher.canonical(plan))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                patcher.publish(root, outputs, plan, "0" * 64)
            self.assertEqual(list(root.iterdir()), [])
            (root / "foreign.txt").write_bytes(b"sentinel")
            with self.assertRaises(ValueError):
                patcher.publish(root, outputs, plan, pin)
            self.assertEqual((root / "foreign.txt").read_bytes(), b"sentinel")

    def test_exclusive_overlay_is_idempotent_and_detects_tamper(self):
        patcher = patcher_module()
        outputs = {"app/services/example.py": b"# fixture propia\n"}
        plan = {"outputs": patcher.output_inventory(outputs)}
        pin = patcher.digest(patcher.canonical(plan))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(patcher.publish(root, outputs, plan, pin)["created"])
            self.assertFalse(patcher.publish(root, outputs, plan, pin)["created"])
            target = root / "overlay/app/services/example.py"
            target.write_bytes(b"foreign")
            with self.assertRaises(ValueError):
                patcher.publish(root, outputs, plan, pin)
            self.assertEqual(target.read_bytes(), b"foreign")

    def test_paths_and_input_hashes_cannot_escape_or_execute(self):
        patcher = patcher_module()
        for path in [
            "../outside",
            "/absolute",
            "a/../outside",
            "a\\outside",
            "a:stream",
            "NUL/device",
            "trailing./file",
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                patcher.output_inventory({path: b"fixture"})
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary) / "data.py"
            p.write_bytes(b"raise AssertionError('NO_EJECUTAR')\n")
            with self.assertRaises(ValueError):
                patcher.pinned_read(p, "0" * 64)


if __name__ == "__main__":
    unittest.main()
