"""Proveedor remoto y overlay reproducible; nunca importa la aplicación ni modelos."""

import ast
import copy
import importlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/supoclip"))
P = importlib.import_module("speaches_provider")
A = importlib.import_module("apply_remote_transcription")


def sample():
    word = {"start": 0.0, "end": 1.0, "word": " Prueba.", "probability": 0.8}
    return {
        "task": "transcribe",
        "language": "es",
        "duration": 1.0,
        "text": "Prueba.",
        "words": [copy.deepcopy(word)],
        "segments": [{"start": 0.0, "end": 1.0, "text": " Prueba.", "words": [word]}],
    }


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"fixture-publica-no-video-real")
        self.sentinel = self.root / "video.transcription.mp3"
        self.sentinel.write_bytes(b"centinela-ajeno-no-adoptar")
        self.config = types.SimpleNamespace(
            transcription_provider="speaches",
            speaches_bearer_token_file="/run/secrets/speaches",
            transcription_language="es",
            max_video_upload_bytes=4_000_000_000,
        )
        self.commands = []
        self.audio_paths = []

    def fake_tool(self, argv, *, deadline, cancel_event, input_fd, cwd):
        self.commands.append(argv)
        self.assertGreater(deadline, P.time.monotonic())
        self.assertEqual(os.read(input_fd, 7), b"fixture")
        os.lseek(input_fd, 0, os.SEEK_SET)
        if sys.platform.startswith("linux"):
            self.assertEqual(Path(cwd).stat().st_mode & 0o777, 0o700)
        if argv[0] == "/usr/bin/ffprobe":
            return json.dumps(
                {
                    "streams": [
                        {"tags": {"language": "eng"}, "disposition": {"default": 1}},
                        {"tags": {"language": "spa"}, "disposition": {"default": 1}},
                        {"tags": {"language": "spa"}, "disposition": {"comment": 1}},
                    ]
                }
            ).encode()
        self.audio_paths.append(Path(argv[-1]))
        Path(argv[-1]).write_bytes(b"audio-mp3-fixture")
        return b""

    def invoke(self, **kwargs):
        return P.get_transcript_with_speaches(
            self.video,
            self.config,
            format_transcript=lambda result: ["texto-formateado"],
            cache_transcript=lambda path, result: self.cache.append((path, result)),
            **kwargs,
        )

    def test_extraccion_privada_stream_correcto_cache_y_cleanup_sin_fallback(self):
        self.cache = []
        with (
            patch.object(P, "_run_tool", self.fake_tool),
            patch.object(P, "transcribe_audio", return_value=sample()) as remote,
        ):
            self.assertEqual(self.invoke(), "texto-formateado")
        self.assertEqual(len(self.commands), 2)
        argv = self.commands[1]
        self.assertIn("-n", argv)
        self.assertNotIn("-y", argv)
        self.assertEqual(argv[argv.index("-map") + 1], "0:a:1")
        self.assertNotIn(str(self.video), argv)
        self.assertTrue(any("/proc/self/fd/" in value for value in argv))
        self.assertEqual(remote.call_args.args[1], "/run/secrets/speaches")
        self.assertEqual(remote.call_args.kwargs["language"], "es")
        self.assertLessEqual(remote.call_args.kwargs["total_seconds"], 300)
        self.assertEqual(len(self.cache), 1)
        self.assertEqual(self.cache[0], (self.video, sample()))
        self.assertTrue(all(not path.parent.exists() for path in self.audio_paths))

    def test_fallo_http_no_cambia_proveedor_ni_escribe_cache(self):
        self.cache = []
        with (
            patch.object(P, "_run_tool", self.fake_tool),
            patch.object(P, "transcribe_audio", side_effect=P.SpeachesError("SPEACHES_BUSY")),
        ):
            with self.assertRaises(P.SpeachesError) as caught:
                self.invoke()
            self.assertEqual(str(caught.exception), "SPEACHES_BUSY")
        self.assertEqual(self.cache, [])
        self.assertTrue(all(not path.parent.exists() for path in self.audio_paths))

    def test_error_cache_o_formato_no_expone_detalles_del_llamador(self):
        for which in ("format", "cache"):
            with (
                self.subTest(which=which),
                patch.object(P, "_run_tool", self.fake_tool),
                patch.object(P, "transcribe_audio", return_value=sample()),
            ):

                def failed(*args):
                    raise OSError("TEST_ONLY_PRIVATE_FIXTURE_DETAIL")

                with self.assertRaisesRegex(P.SpeachesError, "^SPEACHES_PROVIDER_FAILED$"):
                    P.get_transcript_with_speaches(
                        self.video,
                        self.config,
                        format_transcript=failed if which == "format" else lambda result: ["ok"],
                        cache_transcript=failed if which == "cache" else lambda path, result: None,
                    )
        self.assertEqual(self.sentinel.read_bytes(), b"centinela-ajeno-no-adoptar")

    def test_ffmpeg_falla_sin_enviar_video_original_ni_escribir_cache(self):
        self.cache = []

        def failed(argv, **kwargs):
            self.fake_tool(argv, **kwargs)
            if argv[0] == "/usr/bin/ffmpeg":
                raise P.SpeachesError("SPEACHES_EXTRACTION_FAILED")
            return json.dumps({"streams": [{"tags": {}, "disposition": {}}]}).encode()

        with patch.object(P, "_run_tool", failed), patch.object(P, "transcribe_audio") as remote:
            with self.assertRaises(P.SpeachesError) as caught:
                self.invoke()
            self.assertEqual(str(caught.exception), "SPEACHES_EXTRACTION_FAILED")
            remote.assert_not_called()
        self.assertEqual(self.cache, [])
        self.assertTrue(all(not path.parent.exists() for path in self.audio_paths))

    def test_cancelacion_previa_no_lanza_herramienta_ni_red(self):
        self.cache = []
        event = threading.Event()
        event.set()
        with patch.object(P, "_run_tool") as tool, patch.object(P, "transcribe_audio") as remote:
            with self.assertRaises(P.SpeachesError) as caught:
                self.invoke(cancel_event=event)
            self.assertEqual(str(caught.exception), "SPEACHES_CANCELLED")
            tool.assert_not_called()
            remote.assert_not_called()

    def test_probe_invalido_o_sin_audio_falla_no_elige_implicitamente(self):
        self.cache = []
        for raw in (b"{}", b'{"streams":[]}', b'{"streams":[],"streams":[]}', b"not-json"):
            with (
                self.subTest(raw=raw),
                patch.object(P, "_run_tool", return_value=raw),
                patch.object(P, "transcribe_audio") as remote,
            ):
                with self.assertRaises(P.SpeachesError):
                    self.invoke()
                remote.assert_not_called()

    def test_selector_ajeno_y_token_path_ausente_rechazados(self):
        self.cache = []
        for provider in ("whisper", "assemblyai", "youtube_captions", "", "unknown"):
            self.config.transcription_provider = provider
            with self.subTest(provider=provider), patch.object(P, "_run_tool") as tool:
                with self.assertRaises(P.SpeachesError):
                    self.invoke()
                tool.assert_not_called()
        self.config.transcription_provider = "speaches"
        self.config.speaches_bearer_token_file = None
        with self.assertRaises(P.SpeachesError):
            self.invoke()

    def test_audio_resultante_grande_falla_antes_de_red_y_se_limpia(self):
        self.cache = []

        def huge(argv, **kwargs):
            result = self.fake_tool(argv, **kwargs)
            if argv[0] == "/usr/bin/ffmpeg":
                with Path(argv[-1]).open("wb") as stream:
                    stream.truncate(P.MAX_AUDIO_BYTES + 1)
            return result

        with patch.object(P, "_run_tool", huge), patch.object(P, "transcribe_audio") as remote:
            with self.assertRaises(P.SpeachesError) as caught:
                self.invoke()
            self.assertEqual(str(caught.exception), "SPEACHES_AUDIO_TOO_LARGE")
            remote.assert_not_called()
        self.assertTrue(all(not path.parent.exists() for path in self.audio_paths))

    def test_limite_archivo_de_extractor_no_se_confunde_con_transcripcion_parcial(self):
        self.cache = []

        def limited(argv, **kwargs):
            result = self.fake_tool(argv, **kwargs)
            if argv[0] == "/usr/bin/ffmpeg":
                with Path(argv[-1]).open("wb") as stream:
                    stream.truncate(P.MAX_AUDIO_BYTES + 1)
                raise P.SpeachesError("SPEACHES_EXTRACTION_FAILED")
            return result

        with patch.object(P, "_run_tool", limited), patch.object(P, "transcribe_audio") as remote:
            with self.assertRaisesRegex(P.SpeachesError, "^SPEACHES_AUDIO_TOO_LARGE$"):
                self.invoke()
            remote.assert_not_called()
        self.assertEqual(self.cache, [])


def definitions(text, names, context=None):
    """Compila sólo definiciones seleccionadas del texto SHA comprobado, sin imports app."""
    nodes = [
        node
        for node in ast.parse(text).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names
    ]
    assert {node.name for node in nodes} == set(names)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    scope = {} if context is None else context
    exec(compile(module, "<funciones-upstream-fijadas>", "exec"), scope)
    return scope


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.source.mkdir(mode=0o700)
        self.value, self.manifest_sha = A.manifest()
        self.items = {item["path"]: item for item in self.value["files"]}
        for item in self.value["files"]:
            path = self.source / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item["source_text"], encoding="utf-8", newline="\n")
        self.destination = self.root / "new-overlay"

    def changed(self, path):
        item = self.items[path]
        return A.patch_source(item["source_text"].encode(), item).decode()

    def test_config_exige_selector_y_ruta_sin_fallback_AssemblyAI(self):
        context = {
            "os": os,
            "math": math,
            "get_cached_setting": lambda name: None,
            "setting_prefers_admin": lambda name: False,
        }
        before = definitions(
            self.items["backend/src/config.py"]["source_text"], ["Config"], context.copy()
        )["Config"]
        self.assertEqual(before._normalize_transcription_provider("typo"), "assemblyai")
        after = definitions(self.changed("backend/src/config.py"), ["Config"], context.copy())[
            "Config"
        ]
        for value in (None, "", "typo", "assemblyai", "whisper", "youtube_captions"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "^SPEACHES_PROVIDER_REQUIRED$"),
            ):
                after._normalize_transcription_provider(value)
        self.assertEqual(after._normalize_transcription_provider("speaches"), "speaches")
        for path in (
            None,
            "",
            "TOKEN_LITERAL",
            "https://invalid.example/token",
            "/run/../token",
            "/run//token",
            "/run/token\n",
        ):
            env = {"TRANSCRIPTION_PROVIDER": "speaches"}
            if path is not None:
                env["SPEACHES_BEARER_TOKEN_FILE"] = path
            with self.subTest(path=path), patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "^SPEACHES_TOKEN_FILE_REQUIRED$"):
                    after()
        with patch.dict(
            os.environ,
            {
                "TRANSCRIPTION_PROVIDER": "speaches",
                "SPEACHES_BEARER_TOKEN_FILE": "/run/secrets/speaches",
            },
            clear=True,
        ):
            config = after()
            self.assertEqual(config.speaches_bearer_token_file, "/run/secrets/speaches")

    def test_common_elimina_import_modelo_y_conserva_esquema_cache_real(self):
        original = ast.parse(self.items["backend/src/media/common.py"]["source_text"])
        changed = ast.parse(self.changed("backend/src/media/common.py"))
        imported_before = {
            alias.name
            for node in ast.walk(original)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_after = {
            alias.name
            for node in ast.walk(changed)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertIn("whisper", imported_before)
        self.assertFalse({"whisper", "torch"} & imported_after)
        assignments = {
            node.targets[0].id: node.value
            for node in changed.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        }
        self.assertIsNone(ast.literal_eval(assignments["_whisper"]))
        self.assertIs(ast.literal_eval(assignments["_WHISPER_AVAILABLE"]), False)
        self.assertEqual(ast.literal_eval(assignments["TRANSCRIPT_CACHE_SCHEMA_VERSION"]), 2)

    def test_dispatch_solo_remoto_formato_y_cache_originales_sin_importar_app(self):
        text = self.changed("backend/src/media/transcription.py")
        config = types.SimpleNamespace(transcription_provider="speaches")
        formatter, cacher = Mock(), Mock()
        scope = definitions(
            text,
            ["get_video_transcript"],
            {
                "get_config": lambda: config,
                "format_transcript_for_analysis": formatter,
                "cache_transcript_data": cacher,
                "_get_transcript_with_whisper": Mock(side_effect=AssertionError("LOCAL_MODEL")),
                "_get_transcript_with_assemblyai": Mock(side_effect=AssertionError("FALLBACK")),
            },
        )
        event = threading.Event()
        with patch.object(P, "get_transcript_with_speaches", return_value="remoto") as remote:
            self.assertEqual(
                scope["get_video_transcript"](Path("fixture.mp4"), cancel_event=event), "remoto"
            )
            self.assertIs(remote.call_args.kwargs["format_transcript"], formatter)
            self.assertIs(remote.call_args.kwargs["cache_transcript"], cacher)
            self.assertIs(remote.call_args.kwargs["cancel_event"], event)
            config.transcription_provider = "whisper"
            with self.assertRaisesRegex(ValueError, "SPEACHES_PROVIDER_REQUIRED"):
                scope["get_video_transcript"](Path("fixture.mp4"))
            self.assertEqual(remote.call_count, 1)

    def test_cache_y_formato_reales_conservan_version_probabilidad_y_ms(self):
        text = self.changed("backend/src/media/transcription.py")
        scope = definitions(
            text,
            [
                "_whisper_result_to_transcript_data",
                "cache_transcript_data",
                "format_transcript_for_analysis",
                "format_ms_to_timestamp",
            ],
            {"json": json, "logger": Mock(), "TRANSCRIPT_CACHE_SCHEMA_VERSION": 2},
        )
        result = sample()
        self.assertEqual(
            scope["format_transcript_for_analysis"](result), ["[00:00 - 00:01] Prueba."]
        )
        video = self.root / "own-video.mp4"
        scope["cache_transcript_data"](video, result)
        cached = json.loads(video.with_suffix(".transcript_cache.json").read_text())
        self.assertEqual(cached["version"], 2)
        self.assertEqual(cached["words"][0]["end"], 1000)
        self.assertEqual(cached["words"][0]["confidence"], 0.8)

    def test_selector_pista_coincide_con_funcion_AGPL_fijada(self):
        reference = self.value["ffmpeg_reference"]
        scope = definitions(
            reference["language_function"] + "\n\n" + reference["selector_function"],
            ["_audio_language_matches", "select_audio_stream_index"],
            {
                "json": json,
                "_ISO_639_2_CODES": {k: set(v) for k, v in reference["aliases"].items()},
                "_SECONDARY_AUDIO_DISPOSITIONS": (
                    "comment",
                    "visual_impaired",
                    "hearing_impaired",
                    "dub",
                ),
            },
        )
        streams = [
            {"tags": {"language": "spa"}, "disposition": {"comment": 1}},
            {"tags": {"language": "eng"}, "disposition": {"default": 1}},
            {"tags": {"language": "spa"}, "disposition": {"original": 1}},
            {"tags": {"language": "ger"}, "disposition": {}},
        ]
        raw = json.dumps({"streams": streams}).encode()
        scope["run_ffmpeg_command"] = lambda *args, **kwargs: types.SimpleNamespace(
            returncode=0, stdout=raw.decode()
        )
        for language in ("es", "en", "de", "ja", "pt", ""):
            with self.subTest(language=language):
                self.assertEqual(
                    P._select_stream(raw, language),
                    scope["select_audio_stream_index"](Path("fixture.mp4"), language),
                )

    def test_plan_sin_efectos_hash_obsoleto_y_publicacion_nueva_sin_adopcion(self):
        plan, outputs = A.make_plan(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(len(outputs), 7)
        with self.assertRaisesRegex(A.PatchError, "PATCH_PLAN_STALE"):
            A.apply_overlay(self.source, self.destination, "0" * 64)
        self.assertFalse(self.destination.exists())
        receipt = A.apply_overlay(self.source, self.destination, A.digest(A.canonical(plan)))
        self.assertEqual(receipt["files"], 7)
        for name, raw in outputs.items():
            self.assertEqual((self.destination / name).read_bytes(), raw)
        with self.assertRaisesRegex(A.PatchError, "PATCH_DESTINATION_REJECTED"):
            A.apply_overlay(self.source, self.destination, A.digest(A.canonical(plan)))

    def test_cambio_fuente_fuera_del_fragmento_o_helper_rechazado(self):
        path = self.source / "backend/src/config.py"
        path.write_bytes(path.read_bytes() + b"\n# diferencia\n")
        with self.assertRaisesRegex(A.PatchError, "PATCH_SOURCE_MISMATCH"):
            A.make_plan(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_destino_ajeno_ancestro_symlink_y_fuente_hardlink_no_adoptados(self):
        self.destination.mkdir()
        sentinel = self.destination / "foreign.txt"
        sentinel.write_bytes(b"no-tocar")
        with self.assertRaises(A.PatchError):
            A.make_plan(self.source, self.destination)
        self.assertEqual(sentinel.read_bytes(), b"no-tocar")
        original = self.source / "backend/src/config.py"
        os.link(original, self.root / "config-alias")
        with self.assertRaises(A.PatchError):
            A.make_plan(self.source, self.root / "other-overlay")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Symlink/modes requieren Linux real")
    def test_symlink_de_ancestro_destino_rechazado_sin_crear_archivo(self):
        link = self.root / "symlink"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(A.PatchError):
            A.make_plan(self.source, link / "forbidden")
        self.assertFalse((self.root / "forbidden").exists())


@unittest.skipUnless(
    sys.platform.startswith("linux") and hasattr(os, "pidfd_open"),
    "Supervisor PIDFD se acredita con Python fixture en Linux; nunca FFmpeg real",
)
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.input = (self.root / "input").open("w+b")
        self.input.write(b"fixture")
        self.input.flush()
        self.addCleanup(self.input.close)
        self.created = []
        original = subprocess.Popen

        def capture(*args, **kwargs):
            process = original(*args, **kwargs)
            self.created.append(process)
            return process

        self.capture = capture

    def invoke(self, gate, *, seconds=2, cancel=None):
        with patch.object(P, "_GATE", gate), patch.object(P.subprocess, "Popen", self.capture):
            return P._run_tool(
                ["/usr/bin/ffprobe", "fixed-fixture"],
                deadline=P.time.monotonic() + seconds,
                cancel_event=cancel,
                input_fd=self.input.fileno(),
                cwd=self.root,
            )

    def assert_reaped(self):
        self.assertEqual(len(self.created), 1)
        process = self.created[0]
        self.assertIsNotNone(process.poll())
        self.assertTrue(
            all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
        )

    def test_supervisor_salida_y_entorno_desde_cero_sin_ejecutar_ffmpeg(self):
        gate = "import os,sys;sys.stdin.buffer.readline();sys.stdout.write('sin-secreto' if 'SECRET_FIXTURE' not in os.environ else 'ERROR')"
        with patch.dict(os.environ, {"SECRET_FIXTURE": "TEST_ONLY_NOT_A_CREDENTIAL"}):
            self.assertEqual(self.invoke(gate), b"sin-secreto")
        self.assert_reaped()

    def test_salida_excesiva_se_cierra_sin_registrar_cuerpo(self):
        gate = "import sys;sys.stdin.buffer.readline();sys.stdout.buffer.write(b'x'*100000);sys.stdout.flush()"
        with self.assertRaisesRegex(P.SpeachesError, "^SPEACHES_EXTRACTION_OUTPUT_LIMIT$"):
            self.invoke(gate)
        self.assert_reaped()

    def test_cancelacion_y_plazo_solo_hijo_propio_centinela_preservado(self):
        sentinel = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c", "import time;time.sleep(20)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        pidfd = os.pidfd_open(sentinel.pid, 0)
        try:
            for cancelled in (False, True):
                self.created = []
                event = threading.Event()
                timer = threading.Timer(0.1, event.set)
                if cancelled:
                    timer.start()
                gate = "import sys,time;sys.stdin.buffer.readline();time.sleep(20)"
                try:
                    with self.assertRaises(P.SpeachesError) as caught:
                        self.invoke(gate, seconds=1 if cancelled else 0.15, cancel=event)
                    self.assertEqual(
                        str(caught.exception),
                        "SPEACHES_CANCELLED" if cancelled else "SPEACHES_TIMEOUT",
                    )
                    self.assert_reaped()
                    self.assertIsNone(sentinel.poll())
                finally:
                    timer.cancel()
                    if cancelled:
                        timer.join()
        finally:
            self.assertTrue(P._stop(pidfd))
            sentinel.wait(timeout=1)
            os.close(pidfd)

    def test_fallo_pidfd_no_autoriza_exec_y_hijo_sale_por_EOF(self):
        # Con el gate original, sólo EOF puede completarse sin la autorización JSON.
        with patch.object(P.os, "pidfd_open", side_effect=OSError("fixture-private-diagnostic")):
            with self.assertRaisesRegex(P.SpeachesError, "^SPEACHES_EXTRACTION_FAILED$"):
                self.invoke(P._GATE)
        self.assert_reaped()


if __name__ == "__main__":
    unittest.main()
