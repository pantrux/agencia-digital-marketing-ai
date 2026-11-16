"""Contrato local de transcripción; estos dobles no acreditan CUDA real."""

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gpu-bridge-win11"))
SPEC = importlib.util.spec_from_file_location(
    "whisper_service", ROOT / "gpu-bridge-win11/whisper_service.py"
)
whisper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = whisper
SPEC.loader.exec_module(whisper)


def segment(word="ensayo", start=0.0, end=0.4):
    return SimpleNamespace(words=[SimpleNamespace(word=word, start=start, end=end)])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / "probe.wav"
        self.audio.write_bytes(b"audio sintetico de prueba")
        self.model = SimpleNamespace(
            model=SimpleNamespace(device="cuda", compute_type="int8_float16"),
            transcribe=lambda *args, **kwargs: (
                iter([segment("ensayo"), segment("agencia digital", 0.5, 0.9)]),
                SimpleNamespace(duration=1.0),
            ),
        )
        self.runtime = whisper.WhisperRuntime(
            self.audio,
            hashlib.sha256(self.audio.read_bytes()).hexdigest(),
            lambda: self.model,
            lambda: {"free_mib": 4096, "total_mib": 8192},
        )

    def test_salud_no_acredita_cuda_antes_de_inferencia(self):
        self.assertEqual(asyncio.run(self.runtime.readiness())["cuda_available"], False)
        self.runtime.verify_inference()
        result = asyncio.run(self.runtime.readiness())
        self.assertTrue(result["ready"])
        self.assertTrue(result["cuda_available"])
        self.assertEqual(result["word_count"], 2)

    def test_rechaza_cpu_aunque_haya_palabras(self):
        self.model.model.device = "cpu"
        with self.assertRaisesRegex(ValueError, "CUDA"):
            self.runtime.verify_inference()
        self.assertFalse(asyncio.run(self.runtime.readiness())["ready"])

    def test_sin_vram_no_carga_modelo(self):
        self.runtime.memory_reader = lambda: {"free_mib": 1024, "total_mib": 8192}
        self.runtime.loader = lambda: self.fail("No debe cargar con VRAM insuficiente")
        with self.assertRaisesRegex(ValueError, "memoria"):
            self.runtime.verify_inference()

    def test_audio_modificado_no_carga_modelo(self):
        self.audio.write_bytes(b"otro audio")
        self.runtime.loader = lambda: self.fail("No debe cargar un probe alterado")
        with self.assertRaisesRegex(ValueError, "huella"):
            self.runtime.verify_inference()

    def test_generador_se_consume_y_rechaza_tiempos_invalidos(self):
        self.model.transcribe = lambda *args, **kwargs: (
            iter([segment("prueba", 0.8, 0.2)]),
            SimpleNamespace(duration=1.0),
        )
        with self.assertRaisesRegex(ValueError, "tiempos"):
            self.runtime.verify_inference()

    def test_palabras_sin_contenido_esperado_no_acreditan_inferencia(self):
        self.model.transcribe = lambda *args, **kwargs: (
            iter([segment("silencio")]),
            SimpleNamespace(duration=1.0),
        )
        with self.assertRaisesRegex(ValueError, "contenido"):
            self.runtime.verify_inference()

    def test_probe_libera_contexto_de_modelo_incluso_ante_error(self):
        closed = []

        @contextmanager
        def loader():
            try:
                yield self.model
            finally:
                closed.append(True)

        self.runtime.loader = loader
        self.runtime.verify_inference()
        self.assertEqual(closed, [True])

    def test_cache_solo_acepta_la_revision_y_huellas_fijadas(self):
        revision = (
            self.root
            / "models--Systran--faster-whisper-small"
            / "snapshots"
            / whisper.MODEL_REVISION
        )
        revision.mkdir(parents=True)
        (revision / "model.bin").write_bytes(b"modelo controlado")
        files = [
            {"filename": "model.bin", "sha256": hashlib.sha256(b"modelo controlado").hexdigest()}
        ]
        whisper.verify_model_files(revision, files)
        (revision / "model.bin").write_bytes(b"modelo alterado")
        with self.assertRaisesRegex(ValueError, "huella"):
            whisper.verify_model_files(revision, files)

    def test_cache_rechaza_otra_revision_y_enlace(self):
        snapshots = self.root / "models--Systran--faster-whisper-small" / "snapshots"
        revision = snapshots / whisper.MODEL_REVISION
        revision.mkdir(parents=True)
        (snapshots / "otra-revision").mkdir()
        with self.assertRaisesRegex(ValueError, "revisión"):
            whisper.verify_model_files(revision, [])

    def test_bloqueo_real_se_retiene_hasta_consumir_el_generador(self):
        gate = threading.Lock()
        model = whisper.ExclusiveInference(self.model, gate)
        first, _ = model.transcribe("fixture")
        self.assertTrue(gate.locked())
        with self.assertRaisesRegex(RuntimeError, "ocupada"):
            model.transcribe("fixture")
        list(first)
        self.assertFalse(gate.locked())

    def test_generador_fallido_libera_bloqueo_y_no_simula_palabras(self):
        def failing():
            raise ValueError("fallo controlado")
            yield  # pragma: no cover

        self.model.transcribe = lambda *args, **kwargs: (failing(), SimpleNamespace(duration=1))
        gate = threading.Lock()
        output, _ = whisper.ExclusiveInference(self.model, gate).transcribe("fixture")
        with self.assertRaisesRegex(ValueError, "controlado"):
            list(output)
        self.assertFalse(gate.locked())

    def test_generador_cerrado_antes_de_consumir_libera_el_bloqueo(self):
        gate = threading.Lock()
        output, _ = whisper.ExclusiveInference(self.model, gate).transcribe("fixture")
        output.close()
        self.assertFalse(gate.locked())

    def test_solicitud_con_vad_usa_lotes_acotados_y_conserva_marcas(self):
        gate = threading.Lock()
        marks = [segment("ensayo")]

        def batched(audio, *, vad_filter, word_timestamps, batch_size):
            self.assertEqual(audio, "fixture")
            self.assertTrue(vad_filter and word_timestamps)
            self.assertEqual(batch_size, 2)
            return iter(marks), SimpleNamespace(duration=60)

        model = whisper.ExclusiveInference(self.model, gate, batched)
        result, info = model.transcribe("fixture", vad_filter=True, word_timestamps=True)
        self.assertTrue(gate.locked())
        self.assertEqual(list(result), marks)
        self.assertEqual(info.duration, 60)
        self.assertFalse(gate.locked())

    def test_sin_vad_o_con_modo_especial_conserva_decodificacion_secuencial(self):
        def must_not_batch(*args, **kwargs):
            self.fail("El lote no admite este modo con la semántica existente")

        model = whisper.ExclusiveInference(self.model, threading.Lock(), must_not_batch)
        for options in (
            {"vad_filter": False},
            {"vad_filter": True, "temperature": 0.5},
            {"vad_filter": True, "initial_prompt": "Una referencia contextual"},
            {"vad_filter": True, "hotwords": "Valparaíso"},
        ):
            with self.subTest(options=options):
                result, info = model.transcribe("fixture", **options)
                self.assertEqual(len(list(result)), 2)
                self.assertEqual(info.duration, 1.0)

    def test_memoria_informa_pico_global_observado_y_no_limite_duro(self):
        result = whisper.memory_evidence(4096, [4096, 3000, 3200])
        self.assertEqual(result["observed_global_peak_delta_mib"], 1096)
        self.assertTrue(result["calibration_within_policy"])
        self.assertFalse(whisper.memory_evidence(4096, [4096, 900])["calibration_within_policy"])

    def test_salud_degrada_si_memoria_actual_es_insuficiente(self):
        self.runtime.verify_inference()
        self.runtime.memory_reader = lambda: {"free_mib": 1000, "total_mib": 8192}
        result = asyncio.run(self.runtime.readiness())
        self.assertFalse(result["ready"])
        self.assertTrue(result["cuda_available"])


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_metricas_agregan_solo_inferencias_completas_y_normalizan_a_un_minuto(self):
        metrics = whisper.TranscriptionMetrics()
        ticks = iter((10.0, 14.0, 20.0, 24.0, 30.0, 39.0))

        def failing():
            yield segment("ensayo")
            raise ValueError("fallo de prueba")

        backend = SimpleNamespace(
            model=object(),
            transcribe=lambda outcome: (
                failing() if outcome == "fallo" else iter([segment("ensayo")]),
                SimpleNamespace(duration=30 if outcome == "corta" else 60),
            ),
        )
        model = whisper.ExclusiveInference(backend, threading.Lock(), observe=metrics.observe)
        with patch.object(whisper.time, "perf_counter", side_effect=lambda: next(ticks)):
            with self.assertRaisesRegex(ValueError, "fallo de prueba"):
                list(model.transcribe("fallo")[0])
            model.transcribe("abortada")[0].close()
            list(model.transcribe("corta")[0])
            list(model.transcribe("completa")[0])

        async def app(*args):
            self.fail("La lectura de métricas no debe alcanzar el motor")

        runtime = SimpleNamespace(metrics=metrics)
        responses = []
        await whisper.TranscriptionAdmission(app, runtime)(
            {"type": "http", "method": "GET", "path": "/metrics"}, None, _sender(responses)
        )
        self.assertEqual(responses[0]["status"], 200)
        body = responses[1]["body"].decode()
        self.assertIn("agency_whisper_inference_seconds_per_minute_count 2\n", body)
        self.assertIn("agency_whisper_inference_seconds_per_minute_sum 17.0\n", body)
        self.assertIn('agency_whisper_inference_seconds_per_minute_bucket{le="8"} 1\n', body)
        self.assertNotIn("ensayo", body)

    async def test_solo_una_peticion_y_cuerpo_original_hasta_final(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        bodies = []

        async def app(scope, receive, send):
            entered.set()
            bodies.append((await receive())["body"])
            await finish.wait()
            await whisper.json_response(send, 200, {"words": []})

        runtime = SimpleNamespace(
            verified=True,
            gate=threading.Lock(),
            memory_reader=lambda: {"free_mib": 4096, "total_mib": 8192},
        )
        guarded = whisper.TranscriptionAdmission(app, runtime)
        scope = {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions"}

        async def receive():
            return {"type": "http.request", "body": b"multipart sin alterar"}

        first, second = [], []
        task = asyncio.create_task(guarded(scope, receive, _sender(first)))
        await entered.wait()
        await guarded(scope, receive, _sender(second))
        self.assertEqual(second[0]["status"], 429)
        finish.set()
        await task
        self.assertEqual(first[0]["status"], 200)
        self.assertEqual(bodies, [b"multipart sin alterar"])
        self.assertFalse(runtime.gate.locked())

    async def test_no_expone_descargas_ni_rutas_tts(self):
        async def app(*args):
            self.fail("La ruta no permitida no debe alcanzar upstream")

        guarded = whisper.TranscriptionAdmission(app, SimpleNamespace())
        messages = []
        await guarded(
            {"type": "http", "method": "POST", "path": "/v1/models"}, None, _sender(messages)
        )
        self.assertEqual(messages[0]["status"], 404)

    async def test_cuerpo_excesivo_se_rechaza_antes_de_upstream(self):
        async def app(*args):
            self.fail("El cuerpo excesivo no debe alcanzar upstream")

        runtime = SimpleNamespace(
            verified=True, gate=threading.Lock(), memory_reader=lambda: {"free_mib": 4096}
        )
        messages = []
        await whisper.TranscriptionAdmission(app, runtime)(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/audio/transcriptions",
                "headers": [(b"content-length", str(whisper.MAX_UPLOAD_BYTES + 1).encode())],
            },
            None,
            _sender(messages),
        )
        self.assertEqual(messages[0]["status"], 413)
        self.assertFalse(runtime.gate.locked())

    async def test_chunked_excesivo_no_alcanza_parser_upstream(self):
        async def app(*args):
            self.fail("El cuerpo chunked excesivo no debe llegar a upstream")

        runtime = SimpleNamespace(
            verified=True, gate=threading.Lock(), memory_reader=lambda: {"free_mib": 4096}
        )
        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"x" * (whisper.MAX_UPLOAD_BYTES + 1),
                "more_body": False,
            }

        await whisper.TranscriptionAdmission(app, runtime)(
            {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions"},
            receive,
            _sender(messages),
        )
        self.assertEqual(messages[0]["status"], 413)
        self.assertFalse(runtime.gate.locked())


def _sender(messages):
    async def send(message):
        messages.append(message)

    return send


class ArtifactTests(unittest.TestCase):
    def test_manifiesto_fija_commit_y_cada_archivo_modelo(self):
        manifest = json.loads((ROOT / "gpu-bridge-win11/whisper-artifacts.json").read_text())
        self.assertEqual(manifest["model"]["revision"], "536b0662742c02347bc0e980a01041f333bce120")
        for artifact in [manifest["upstream"], *manifest["wheels"], *manifest["model"]["files"]]:
            self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(artifact["url"].startswith("https://"))
        self.assertEqual(len(manifest["model"]["files"]), 5)

    def test_rutas_declaradas_no_permiten_otra_cache_ni_modelo(self):
        config = SimpleNamespace(
            install_root=Path("C:/GPU-Bridge"), model_cache_root=Path("C:/GPU-Bridge/models")
        )
        manifest = {
            "model": {
                "cache_relative": f"whisper/hub/models--Systran--faster-whisper-small/snapshots/{whisper.MODEL_REVISION}"
            }
        }
        mapping = {
            "GPU_WHISPER_MODEL": whisper.MODEL_ID,
            "GPU_WHISPER_MODEL_REVISION": whisper.MODEL_REVISION,
            "GPU_WHISPER_COMPUTE_TYPE": "int8_float16",
            "GPU_WHISPER_MODEL_PATH": str(
                config.model_cache_root / manifest["model"]["cache_relative"]
            ),
            "GPU_WHISPER_PROBE_PATH": str(config.install_root / "fixtures/whisper-probe.wav"),
            "GPU_WHISPER_PROBE_SHA256": "0" * 64,
        }
        whisper.service_paths(mapping, config, manifest)
        for key, value in [
            ("GPU_WHISPER_MODEL", "otro/modelo"),
            ("GPU_WHISPER_COMPUTE_TYPE", "auto"),
            ("GPU_WHISPER_MODEL_PATH", "C:/otra-cache"),
            ("GPU_WHISPER_PROBE_SHA256", "CHANGE_ME"),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                whisper.service_paths(dict(mapping, **{key: value}), config, manifest)

    def test_importar_no_carga_dependencias_cuda_ni_consulta_hardware(self):
        code = (
            "import sys; sys.path.insert(0,sys.argv[1]); import whisper_service; "
            "assert not any(n in sys.modules for n in ('ctranslate2','speaches','faster_whisper')); "
            "assert not whisper_service.DLL_HANDLES"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(ROOT / "gpu-bridge-win11")],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
