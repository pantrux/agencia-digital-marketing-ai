"""Adaptador STT de Speaches: modelos inmutables, CUDA explícita y admisión acotada."""

import asyncio
import ctypes
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
from bisect import bisect_left
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from bridge_common import BridgeConfig, GuardedAsgi, json_response

MODEL_ID = "Systran/faster-whisper-small"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
VERSION = "speaches-0.8.3+c78b77d5.ct2-4.8.5.agency.3"
MIN_FREE_MIB = 2048
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
DLL_HANDLES = []


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_files(model_path, files):
    """Acepta sólo una revisión detached, archivos regulares y huellas fijadas."""
    model_path = Path(model_path)
    if (
        model_path.name != MODEL_REVISION
        or model_path.parent.name != "snapshots"
        or {path.name for path in model_path.parent.iterdir()} != {MODEL_REVISION}
    ):
        raise ValueError("La caché debe contener únicamente la revisión fijada.")
    for parent in (model_path, *model_path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("La caché no admite enlaces ni puntos de reanálisis.")
    for artifact in files:
        name = artifact["filename"]
        if Path(name).name != name or ":" in name or "\\" in name:
            raise ValueError("El manifiesto contiene un nombre de archivo inválido.")
        path = model_path / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != artifact["sha256"]:
            raise ValueError("La huella de un archivo del modelo no coincide.")


def require_memory(memory_reader):
    memory = memory_reader()
    if memory["free_mib"] < MIN_FREE_MIB:
        raise ValueError("La memoria GPU libre es insuficiente para la admisión.")
    return memory


def memory_evidence(before_free, samples):
    minimum = min(samples)
    delta = max(0, before_free - minimum)
    return {
        "minimum_observed_free_mib": minimum,
        "observed_global_peak_delta_mib": delta,
        "memory_samples": len(samples),
        "calibration_within_policy": minimum >= 1024 and delta <= 1536,
    }


def check_words(segments, duration):
    """Consume el generador: crear una transcripción diferida no demuestra inferencia."""
    previous, words = 0.0, []
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("La duración del audio de prueba no es válida.")
    for part in segments:
        for word in part.words or []:
            if not all(math.isfinite(value) for value in (word.start, word.end)) or not (
                previous <= word.start <= word.end <= duration + 0.1
            ):
                raise ValueError("La inferencia devolvió tiempos de palabra inválidos.")
            words.append(word.word.strip())
            previous = word.end
    text = unicodedata.normalize("NFKD", " ".join(words).lower())
    if not words or not all(value in text for value in ("ensayo", "agencia", "digital")):
        raise ValueError("La inferencia no reconoció el contenido sintético esperado.")
    return len(words)


class TranscriptionMetrics:
    """Conserva tiempos agregados por proceso, sin guardar datos del audio."""

    name = "agency_whisper_inference_seconds_per_minute"
    duration_name = f"{name}_by_audio_duration"
    bounds = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 16, 24, 40, 80)
    duration_bounds = (15, 45, 75)
    duration_ranges = ("hasta_15s", "de_15_a_45s", "de_45_a_75s", "mas_de_75s")

    def __init__(self):
        self.lock = threading.Lock()
        self.histograms = {
            group: (0.0, [0] * (len(self.bounds) + 1)) for group in (None, *self.duration_ranges)
        }

    def observe(self, duration, elapsed):
        if not math.isfinite(duration) or duration <= 0:
            return
        seconds_per_minute = elapsed * (60 / duration)
        if not math.isfinite(seconds_per_minute) or seconds_per_minute < 0:
            return
        bucket = bisect_left(self.bounds, seconds_per_minute)
        audio_range = self.duration_ranges[bisect_left(self.duration_bounds, duration)]
        with self.lock:
            for group in (None, audio_range):
                total, buckets = self.histograms[group]
                buckets[bucket] += 1
                self.histograms[group] = (total + seconds_per_minute, buckets)

    def histogram_lines(self, name, total, buckets, audio_range=None):
        label = f'audio_range="{audio_range}"' if audio_range else ""
        label_suffix = f",{label}" if label else ""
        labels = f"{{{label}}}" if label else ""
        cumulative = 0
        for limit, count in zip((*self.bounds, "+Inf"), buckets, strict=True):
            cumulative += count
            yield f'{name}_bucket{{le="{limit}"{label_suffix}}} {cumulative}'
        yield f"{name}_sum{labels} {total}"
        yield f"{name}_count{labels} {cumulative}"

    def render(self):
        with self.lock:
            snapshot = {
                group: (total, tuple(buckets))
                for group, (total, buckets) in self.histograms.items()
            }
        lines = [
            f"# HELP {self.name} Segundos de inferencia por minuto de audio.",
            f"# TYPE {self.name} histogram",
            *self.histogram_lines(self.name, *snapshot[None]),
            f"# HELP {self.duration_name} Segundos de inferencia por minuto según duración del audio.",
            f"# TYPE {self.duration_name} histogram",
        ]
        for group in self.duration_ranges:
            lines.extend(
                self.histogram_lines(self.duration_name, *snapshot[group], audio_range=group)
            )
        return ("\n".join(lines) + "\n").encode("utf-8")


class WhisperRuntime:
    """La salud pasa a disponible únicamente después de transcribir el probe respaldado."""

    def __init__(self, probe, probe_sha256, loader, memory_reader):
        self.probe, self.probe_sha256 = Path(probe), probe_sha256
        self.loader, self.memory_reader = loader, memory_reader
        self.gate = threading.Lock()
        self.compute_gate = threading.Lock()
        self.metrics = TranscriptionMetrics()
        self.verified = False
        self.evidence = {}
        self.stage = "pendiente"

    def verify_inference(self):
        self.verified = False
        if not self.gate.acquire(blocking=False):
            raise ValueError("Ya existe una inferencia propia en curso.")
        try:
            self.stage = "huella_probe"
            if sha256_file(self.probe) != self.probe_sha256:
                raise ValueError("La huella del audio de prueba no coincide.")
            self.stage = "memoria_inicial"
            before = require_memory(self.memory_reader)
            started = time.monotonic()
            samples, sampling_errors, stop = [before["free_mib"]], [], threading.Event()

            def observe():
                while not stop.wait(0.25):
                    try:
                        samples.append(self.memory_reader()["free_mib"])
                    except Exception:
                        sampling_errors.append(True)
                        return

            observer = threading.Thread(target=observe, daemon=True)
            observer.start()
            try:
                self.stage = "carga_modelo"
                loaded = self.loader()
                with loaded if hasattr(loaded, "__enter__") else nullcontext(loaded) as model:
                    if (
                        getattr(model.model, "device", None) != "cuda"
                        or getattr(model.model, "compute_type", None) != "int8_float16"
                    ):
                        raise ValueError("La inferencia exige CUDA con cálculo int8_float16.")
                    self.stage = "inferencia_y_palabras"
                    segments, info = model.transcribe(
                        str(self.probe),
                        language="es",
                        word_timestamps=True,
                        beam_size=1,
                        vad_filter=False,
                        condition_on_previous_text=False,
                    )
                    word_count = check_words(segments, info.duration)
            finally:
                stop.set()
                observer.join(timeout=3.5)
            if observer.is_alive() or sampling_errors:
                raise ValueError("No se completó la medición de memoria durante inferencia.")
            after = self.memory_reader()
            samples.append(after["free_mib"])
            self.evidence = {
                "word_count": word_count,
                "inference_seconds": round(time.monotonic() - started, 3),
                "free_mib_before": before["free_mib"],
                "free_mib_after": after["free_mib"],
                **memory_evidence(before["free_mib"], samples),
            }
            self.stage = "politica_memoria"
            if not self.evidence["calibration_within_policy"]:
                raise ValueError("La prueba excedió la política de memoria observada.")
            self.verified = True
            self.stage = "verificado"
        finally:
            self.gate.release()

    async def readiness(self):
        memory = await asyncio.to_thread(self.memory_reader) if self.verified else None
        return {
            "ready": self.verified and memory["free_mib"] >= MIN_FREE_MIB,
            "cuda_available": self.verified,
            "provider": "CTranslate2 CUDA" if self.verified else "no verificado",
            "model": MODEL_ID,
            "revision": MODEL_REVISION,
            "compute_type": "int8_float16",
            "max_concurrent_inferences": 1,
            **self.evidence,
            **({"current_free_mib": memory["free_mib"]} if memory else {}),
        }


class ExclusiveInference:
    """El bloqueo sigue la inferencia diferida incluso si se cancela el consumidor ASGI."""

    def __init__(self, backend, gate, batched=None, observe=None):
        self.backend, self.gate = backend, gate
        self.model = backend.model
        self.batched = batched
        self.observe = observe

    def transcribe(self, *args, **kwargs):
        if not self.gate.acquire(blocking=False):
            raise RuntimeError("La inferencia CUDA propia sigue ocupada.")
        try:
            started = time.perf_counter() if self.observe else None
            # El lote requiere VAD y sólo usa la primera temperatura de decodificación.
            if (
                self.batched is not None
                and kwargs.get("vad_filter") is True
                and kwargs.get("temperature", 0.0) == 0.0
                and not kwargs.get("initial_prompt")
                and not kwargs.get("hotwords")
            ):
                segments, info = self.batched(*args, **kwargs, batch_size=2)
            else:
                segments, info = self.backend.transcribe(*args, **kwargs)
        except BaseException:
            self.gate.release()
            raise

        def consume():
            try:
                # Entrar ahora en finally permite cerrar incluso un consumidor todavía no iterado.
                yield None
                yield from segments
                if started is not None:
                    self.observe(info.duration, time.perf_counter() - started)
            finally:
                self.gate.release()

        output = consume()
        next(output)
        return output, info


class TranscriptionAdmission:
    """Rechaza exceso de trabajo sin cola y conserva el cuerpo y respuestas de upstream."""

    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if (scope.get("method"), scope.get("path")) == ("GET", "/metrics"):
            body = self.runtime.metrics.render()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/plain; version=0.0.4; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        if (scope.get("method"), scope.get("path")) != ("POST", "/v1/audio/transcriptions"):
            await json_response(send, 404, {"error": "Ruta no habilitada para transcripción."})
            return
        if not self.runtime.verified:
            await json_response(send, 503, {"error": "Inferencia CUDA todavía no acreditada."})
            return
        lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                await json_response(send, 400, {"error": "Tamaño de solicitud inválido."})
                return
            if int(lengths[0]) > MAX_UPLOAD_BYTES:
                await json_response(send, 413, {"error": "El audio excede el tamaño admitido."})
                return
        if not self.runtime.gate.acquire(blocking=False):
            await json_response(send, 429, {"error": "La inferencia propia ya está ocupada."})
            return
        try:
            try:
                await asyncio.to_thread(require_memory, self.runtime.memory_reader)
            except (ValueError, OSError, RuntimeError):
                await json_response(send, 503, {"error": "GPU sin memoria libre suficiente."})
                return
            messages, size, deadline = [], 0, time.monotonic() + 30
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await json_response(
                        send, 408, {"error": "Se agotó el plazo de carga del audio."}
                    )
                    return
                try:
                    event = await asyncio.wait_for(receive(), timeout=remaining)
                except TimeoutError:
                    await json_response(
                        send, 408, {"error": "Se agotó el plazo de carga del audio."}
                    )
                    return
                if event["type"] == "http.disconnect":
                    return
                size += len(event.get("body", b""))
                if size > MAX_UPLOAD_BYTES:
                    await json_response(send, 413, {"error": "El audio excede el tamaño admitido."})
                    return
                messages.append(event)
                if not event.get("more_body", False):
                    break
            iterator = iter(messages)

            async def replay():
                return next(iterator, {"type": "http.request", "body": b"", "more_body": False})

            await self.app(scope, replay, send)
        finally:
            self.runtime.gate.release()


def service_paths(mapping, config, manifest):
    """Las identidades operativas ya deben estar persistidas antes del lanzamiento."""
    expected = {
        "GPU_WHISPER_MODEL": MODEL_ID,
        "GPU_WHISPER_MODEL_REVISION": MODEL_REVISION,
        "GPU_WHISPER_COMPUTE_TYPE": "int8_float16",
    }
    if any(mapping.get(key) != value for key, value in expected.items()):
        raise ValueError("El modelo y el cálculo no coinciden con el contrato fijado.")
    model_path = config.model_cache_root / manifest["model"]["cache_relative"]
    probe = config.install_root / "fixtures" / "whisper-probe.wav"
    if (
        Path(mapping.get("GPU_WHISPER_MODEL_PATH", "")) != model_path
        or Path(mapping.get("GPU_WHISPER_PROBE_PATH", "")) != probe
    ):
        raise ValueError("Las rutas respaldadas no coinciden con el ámbito del servicio.")
    if not re.fullmatch(r"[0-9a-f]{64}", mapping.get("GPU_WHISPER_PROBE_SHA256", "")):
        raise ValueError("La huella del probe sigue pendiente.")
    return model_path, probe


def prepare_dll_search():
    """Añade sólo directorios de este venv al cargador del proceso; conserva sus handles."""
    if sys.platform != "win32":
        raise ValueError("Este adaptador requiere Windows nativo.")
    site = Path(sys.prefix) / "Lib" / "site-packages"
    for folder, names in (
        (site / "ctranslate2", ("ctranslate2.dll", "libiomp5md.dll")),
        (site / "nvidia" / "cublas" / "bin", ("cublas64_12.dll", "cublasLt64_12.dll")),
    ):
        if not all((folder / name).is_file() for name in names):
            raise ValueError("Faltan DLL fijadas en el entorno aislado de transcripción.")
        DLL_HANDLES.append(os.add_dll_directory(str(folder)))
    # Precarga por ruta absoluta: LoadLibrary interno de CT2 también encuentra estos módulos.
    for name in ("cublasLt64_12.dll", "cublas64_12.dll"):
        DLL_HANDLES.append(ctypes.WinDLL(str(site / "nvidia" / "cublas" / "bin" / name)))


def create_app(mapping=None):
    """Factoría explícita; importar este módulo no carga modelos, DLL ni inicia servicios."""
    mapping = os.environ if mapping is None else mapping
    config = BridgeConfig.from_env(mapping)
    manifest = json.loads(
        Path(__file__).with_name("whisper-artifacts.json").read_text(encoding="utf-8")
    )
    model_path, probe = service_paths(mapping, config, manifest)
    verify_model_files(model_path, manifest["model"]["files"])
    binary = Path(mapping["GPU_SMI_PATH"])
    if not binary.is_absolute() or sha256_file(binary) != mapping["GPU_SMI_SHA256"]:
        raise ValueError("El ejecutable NVIDIA no coincide con el respaldo autorizado.")
    prepare_dll_search()
    # Offline se fija antes de importar las bibliotecas del backend. No modifica el sistema.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["CUDA_VISIBLE_DEVICES"] = mapping["GPU_UUID"]

    package_spec = importlib.util.find_spec("speaches")
    if package_spec is None or not package_spec.submodule_search_locations:
        raise ValueError("Falta la fuente Speaches oficial verificada.")
    source_package = Path(next(iter(package_spec.submodule_search_locations)))
    for artifact in manifest["upstream"]["source_files"]:
        if sha256_file(source_package / artifact["path"]) != artifact["sha256"]:
            raise ValueError("La fuente Speaches no coincide con el commit verificado.")

    import ctranslate2
    from fastapi import FastAPI, HTTPException
    from faster_whisper import BatchedInferencePipeline, WhisperModel
    from metrics_service import read_gpu
    from speaches import dependencies, model_aliases
    from speaches.config import Config, WhisperConfig
    from speaches.executors.whisper.model_manager import WhisperModelManager
    from speaches.hf_utils import get_model_repo_path
    from speaches.routers import stt

    model_aliases.MODEL_ID_ALIASES_PATH = source_package.parent.parent / "model_aliases.json"
    model_aliases.load_model_id_aliases.cache_clear()

    def memory_reader():
        measures = read_gpu(binary, mapping["GPU_UUID"])
        return {
            "free_mib": measures["memory_total_mib"] - measures["memory_used_mib"],
            "total_mib": measures["memory_total_mib"],
        }

    runtime = WhisperRuntime(probe, mapping["GPU_WHISPER_PROBE_SHA256"], None, memory_reader)
    whisper_config = WhisperConfig(
        inference_device="cuda",
        device_index=0,
        compute_type="int8_float16",
        num_workers=1,
        ttl=300,
        use_batched_mode=False,
    )

    class PinnedManager(WhisperModelManager):
        def __init__(self, settings):
            super().__init__(settings)
            # El unload público de upstream llama al callback bajo este mismo lock.
            self._lock = threading.RLock()

        def load_model(self, model_id):
            if model_id != MODEL_ID:
                raise HTTPException(404, "El modelo solicitado no está habilitado.")
            return super().load_model(model_id)

        def _load_fn(self, model_id):
            require_memory(memory_reader)
            verify_model_files(model_path, manifest["model"]["files"])
            if ctranslate2.get_cuda_device_count() != 1 or "int8_float16" not in (
                ctranslate2.get_supported_compute_types("cuda", 0)
            ):
                raise ValueError("CUDA int8_float16 no está disponible en la GPU asignada.")
            model = WhisperModel(
                str(model_path),
                device="cuda",
                device_index=0,
                compute_type="int8_float16",
                num_workers=1,
                local_files_only=True,
            )
            if model.model.device != "cuda" or model.model.compute_type != "int8_float16":
                raise ValueError("El backend no cumple el proveedor CUDA fijado.")

            def transcribe_batch(*args, **kwargs):
                # Cada solicitud comienza con su propio estado de marcas temporales.
                return BatchedInferencePipeline(model).transcribe(*args, **kwargs)

            def observe(duration, elapsed):
                if runtime.verified:
                    runtime.metrics.observe(duration, elapsed)

            return ExclusiveInference(model, runtime.compute_gate, transcribe_batch, observe)

    manager = PinnedManager(whisper_config)
    runtime.loader = lambda: manager.load_model(MODEL_ID)

    @asynccontextmanager
    async def lifespan(app):
        try:
            try:
                await asyncio.to_thread(runtime.verify_inference)
            except Exception as error:
                runtime.verified = False
                runtime.evidence = {
                    "error": "No se acreditó la inferencia CUDA de arranque.",
                    "failed_stage": runtime.stage,
                    "error_type": type(error).__name__,
                }
            yield
        finally:
            try:
                manager.unload_model(MODEL_ID)
            except (KeyError, ValueError):
                pass

    upstream = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    upstream.state.whisper_runtime = runtime
    upstream.include_router(stt.router)
    upstream.dependency_overrides[dependencies.get_config] = lambda: Config(
        whisper=whisper_config,
        enable_ui=False,
        log_level="info",
        api_key=None,
    )
    upstream.dependency_overrides[dependencies.get_model_manager] = lambda: manager
    # El router conserva el escaneo y el filtro de la tarjeta original. Sólo encuentra esta repo.
    stt.get_model_repo_path = lambda model: (
        get_model_repo_path(model, cache_dir=model_path.parents[2]) if model == MODEL_ID else None
    )

    def bounded_audio(file):
        audio = dependencies.audio_file_dependency(file)
        if len(audio) > 120 * 16000:
            raise HTTPException(413, "El audio supera 120 segundos.")
        return audio

    # Conserva los metadatos Form de la dependencia original al reemplazar sólo su resultado.
    bounded_audio.__signature__ = inspect.signature(dependencies.audio_file_dependency)
    bounded_audio.__annotations__ = dependencies.audio_file_dependency.__annotations__
    upstream.dependency_overrides[dependencies.audio_file_dependency] = bounded_audio
    return GuardedAsgi(
        TranscriptionAdmission(upstream, runtime), config, "whisper", VERSION, runtime.readiness
    )


def main(argv=None, mapping=None):
    """La prueba explícita consume audio real; no abre un puerto ni imprime secretos."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Transcripción CUDA nativa con artefactos fijados."
    )
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument(
        "--verify-inference", action="store_true", help="Ejecuta el probe respaldado y termina."
    )
    actions.add_argument(
        "--serve", action="store_true", help="Inicia únicamente el puerto STT 8000."
    )
    args = parser.parse_args(argv)
    mapping = os.environ if mapping is None else mapping
    try:
        app = create_app(mapping)
        if args.verify_inference:
            upstream = app.app.app

            async def verify():
                async with upstream.router.lifespan_context(upstream):
                    return await upstream.state.whisper_runtime.readiness()

            result = asyncio.run(verify())
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return 0 if result["ready"] else 2

        import uvicorn

        uvicorn.run(
            app,
            host=mapping["GPU_BRIDGE_HOST"],
            port=8000,
            workers=1,
            log_level="info",
            access_log=False,
            proxy_headers=False,
            server_header=False,
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "ready": False,
                    "error": "No se pudo preparar la transcripción CUDA.",
                    "error_type": type(error).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
