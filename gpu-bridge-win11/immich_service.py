"""Sirve la aplicación oficial de Immich ML con CUDA comprobado y protección común."""

import asyncio
import ctypes
import gc
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import struct
import sys
import sysconfig
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import immich_portability as portable


def preload_cuda(manifest):
    """Carga DLL verificadas del entorno propio; la sonda posterior acredita su uso."""
    if os.name != "nt":
        raise RuntimeError("Este adaptador requiere Windows nativo.")
    for artifact in manifest["artifacts"]:
        if (
            artifact["role"] == "runtime"
            and importlib.metadata.version(artifact["name"]) != artifact["version"]
        ):
            raise RuntimeError("Una dependencia instalada no coincide con el manifiesto.")
    site = Path(sysconfig.get_paths()["purelib"])
    directories = [
        site / "nvidia" / name / "bin"
        for name in (
            "cuda_runtime",
            "cublas",
            "cudnn",
            "cufft",
            "curand",
            "cuda_nvrtc",
            "nvjitlink",
        )
    ]
    handles = []
    for directory in directories:
        portable._plain_path(directory)
        if not directory.is_dir():
            raise RuntimeError("Falta un directorio de DLL CUDA del entorno propio.")
        handles.append(os.add_dll_directory(str(directory)))
    mandatory = (
        "cudart64_12.dll",
        "cublas64_12.dll",
        "cublasLt64_12.dll",
        "cudnn64_9.dll",
        "cufft64_11.dll",
        "curand64_10.dll",
    )
    if any(
        not any((directory / name).is_file() for directory in directories) for name in mandatory
    ):
        raise RuntimeError("Faltan DLL CUDA/cuDNN requeridas; no se admite degradación a CPU.")
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"
    ort = importlib.import_module("onnxruntime")
    ort.preload_dlls(cuda=True, cudnn=True, msvc=True, directory="")
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("ONNX Runtime no ofrece CUDA en este entorno.")
    runtime = ctypes.CDLL(str(directories[0] / "cudart64_12.dll"))
    runtime.cudaSetDevice.argtypes, runtime.cudaSetDevice.restype = [ctypes.c_int], ctypes.c_int
    if runtime.cudaSetDevice(0) != 0:
        raise RuntimeError("No se pudo seleccionar el dispositivo CUDA propio.")
    runtime.cudaMemGetInfo.argtypes = [
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    runtime.cudaMemGetInfo.restype = ctypes.c_int
    return ort, runtime, handles


class NativeImmich:
    def __init__(self, config, mapping, upstream_root, manifest):
        self.config, self.mapping, self.source, self.manifest = (
            config,
            mapping,
            upstream_root,
            manifest,
        )
        self.app = self.module = self.ort = self.runtime = None
        self.face_manifest = self.face_models = self.face_adapter = None
        self.dll_handles, self.started, self.failed = [], False, False
        self.error_type = None
        self.requests_lock = asyncio.Lock()
        self.probe_lock = asyncio.Lock()

    def memory_free_mib(self):
        free, total = ctypes.c_size_t(), ctypes.c_size_t()
        if (
            self.runtime.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total)) != 0
            or total.value == 0
            or free.value > total.value
        ):
            raise RuntimeError("No se pudo consultar la memoria CUDA compartida.")
        return free.value // (1024 * 1024)

    def probe(self):
        """MatMul real pequeño; no conserva sesiones ni pesos tras medir CUDA."""
        numpy, onnx = importlib.import_module("numpy"), importlib.import_module("onnx")
        helper = onnx.helper
        tensor = onnx.TensorProto.FLOAT
        graph = helper.make_graph(
            [helper.make_node("MatMul", ["a", "b"], ["y"])],
            "bridge_cuda_probe",
            [helper.make_tensor_value_info(name, tensor, [2, 2]) for name in ("a", "b")],
            [helper.make_tensor_value_info("y", tensor, [2, 2])],
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=10)
        session = portable.strict_cuda_session(
            self.ort.InferenceSession,
            model.SerializeToString(),
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            provider_options=[{"device_id": "0", "arena_extend_strategy": "kSameAsRequested"}, {}],
            sess_options=self.ort.SessionOptions(),
            model_kind="probe",
        )
        values = numpy.ones((2, 2), dtype=numpy.float32)
        try:
            result = session.run(None, {"a": values, "b": values})[0]
            if not numpy.array_equal(result, numpy.full((2, 2), 2, dtype=numpy.float32)):
                portable.CUDA_EVIDENCE.fail()
                raise RuntimeError("La sonda CUDA no produjo el cálculo esperado.")
        finally:
            del session
            gc.collect()

    def initialize(self):
        source = portable.verify_source(self.source, self.manifest)
        cache = self.config.model_cache_root / "immich"
        portable.configure_runtime(cache, self.manifest)
        for model in self.manifest["models"]:
            if self.face_manifest is not None and model["task"] == "facial-recognition":
                continue
            portable.verify_model_cache(
                cache / model["task"] / model["name"], model["name"], model["task"]
            )
        if self.face_manifest is not None:
            from face_models import FaceModels, verify_artifacts

            self.face_models = FaceModels(
                self.config.model_cache_root / self.face_manifest["cache_relative"]
            )
            verify_artifacts(self.face_models.root, self.face_manifest["artifacts"])
        os.environ.update(
            {
                "GPU_MODEL_CACHE_ROOT": str(self.config.model_cache_root),
                "MACHINE_LEARNING_CACHE_FOLDER": str(cache),
                "MACHINE_LEARNING_MODEL_REVISION": "main",
                "MACHINE_LEARNING_DEVICE_ID": "0",
                "MACHINE_LEARNING_DEVICE_IDS": "0",
                "MACHINE_LEARNING_WORKERS": "1",
                "MACHINE_LEARNING_REQUEST_THREADS": "1",
                "MACHINE_LEARNING_MAX_BATCH_SIZE__FACIAL_RECOGNITION": "1",
                "MACHINE_LEARNING_MODEL_TTL": "30",
                "MACHINE_LEARNING_MODEL_TTL_POLL_S": "5",
                "MACHINE_LEARNING_MODEL_ARENA": "false",
                "IMMICH_LOG_LEVEL": "info",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "ORT_DISABLE_TELEMETRY": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        services = Path(__file__).resolve().parent
        os.environ["PYTHONPATH"] = os.pathsep.join((str(source), str(services)))
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        sys.dont_write_bytecode = True
        for name, module in tuple(sys.modules.items()):
            if name == "immich_ml" or name.startswith("immich_ml."):
                file = getattr(module, "__file__", None)
                if file and not Path(file).resolve().is_relative_to(source):
                    raise RuntimeError("Immich ya fue importado desde una fuente distinta.")
        portable.CUDA_EVIDENCE = portable.CudaEvidence()
        self.ort, self.runtime, self.dll_handles = preload_cuda(self.manifest)
        self.module = importlib.import_module("immich_ml.main")
        if Path(self.module.__file__).resolve() != source / "immich_ml/main.py":
            raise RuntimeError("La aplicación no procede de la fuente verificada.")
        if self.memory_free_mib() < 512:
            raise RuntimeError(
                "La GPU no tiene margen para la sonda inicial; no se alteran procesos ajenos."
            )
        self.probe()
        if self.face_models is not None:
            from face_adapter import CommercialFaceAdapter

            def load_faces():
                return self.face_models.load(
                    self.ort,
                    profile_directory=portable.runtime_profile_directory(),
                    evidence=portable.CUDA_EVIDENCE,
                )

            self.face_adapter = CommercialFaceAdapter(
                self.module,
                self.face_models,
                self.mapping["GPU_IMMICH_CLIP_MODEL"],
                load_faces,
                fail_backend=portable.CUDA_EVIDENCE.fail,
            ).install()
        self.app = self.module.app

    def release_models(self):
        if self.face_models is not None:
            self.face_models.close()
        if self.module is not None:
            self.module.model_cache.clear()
            gc.collect()

    async def readiness(self):
        if not self.started or self.failed:
            return {"ready": False, "cuda_available": False, "backend_error_type": self.error_type}
        async with self.probe_lock:
            proof = portable.CUDA_EVIDENCE.snapshot()
            if (
                not proof["backend_failed"]
                and (proof["proof_age_seconds"] is None or proof["proof_age_seconds"] > 60)
                and not self.requests_lock.locked()
            ):
                await asyncio.to_thread(self.probe)
            available = "CUDAExecutionProvider" in self.ort.get_available_providers()
            result = portable.CUDA_EVIDENCE.snapshot()
            free = await asyncio.to_thread(self.memory_free_mib)
            return dict(
                result,
                ready=result["ready"] and available,
                cuda_available=available,
                gpu_memory_free_mib=free,
                models_resident=len(self.module.model_cache._models)
                + (
                    sum(
                        session is not None
                        for session in (self.face_models.detector, self.face_models.recognizer)
                    )
                    if self.face_models is not None
                    else 0
                ),
                face_model=self.mapping.get("GPU_IMMICH_FACE_MODEL"),
                commercial_face_model=self.face_manifest is not None,
            )

    async def lifespan(self, scope, receive, send):
        first = await receive()
        if first["type"] != "lifespan.startup":
            raise RuntimeError("Secuencia de ciclo de vida inválida.")
        try:
            await asyncio.to_thread(self.initialize)
        except Exception as error:
            self.failed, self.error_type = True, type(error).__name__
            portable.CUDA_EVIDENCE.fail()
            await send({"type": "lifespan.startup.complete"})
            while (await receive())["type"] != "lifespan.shutdown":
                pass
            await send({"type": "lifespan.shutdown.complete"})
            return
        pending = True

        async def original_receive():
            nonlocal pending
            if pending:
                pending = False
                return first
            return await receive()

        async def original_send(message):
            if message["type"] == "lifespan.startup.complete":
                self.started = True
            await send(message)

        try:
            await self.app(scope, original_receive, original_send)
        finally:
            self.started = False
            self.release_models()
            for handle in self.dll_handles:
                handle.close()

    async def __call__(self, scope, receive, send):
        from bridge_common import json_response

        if scope["type"] == "lifespan":
            await self.lifespan(scope, receive, send)
            return
        if not self.started or self.failed:
            await json_response(send, 503, {"error": "Backend Immich CUDA no disponible."})
            return
        if scope.get("path") != "/predict":
            await self.app(scope, receive, send)
            return
        if self.requests_lock.locked() or self.probe_lock.locked():
            await json_response(send, 503, {"error": "El backend GPU está ocupado."})
            return
        async with self.requests_lock:
            if await asyncio.to_thread(self.memory_free_mib) < 512:
                await json_response(send, 503, {"error": "Memoria GPU temporalmente insuficiente."})
                return
            messages, response_bytes = [], 0

            async def collect(message):
                nonlocal response_bytes
                response_bytes += len(message.get("body", b""))
                if response_bytes > 4 * 1024 * 1024:
                    raise RuntimeError("La respuesta del modelo excede el límite previsto.")
                messages.append(message)

            try:
                await self.app(scope, receive, collect)
            finally:
                await asyncio.to_thread(self.release_models)
            for message in messages:
                await send(message)


def create_app(mapping=None, upstream_root=None):
    """Fábrica ASGI sin instalación, descargas, sockets ni importaciones CUDA al crearla."""
    from bridge_common import BridgeConfig, GuardedAsgi
    from face_models import MODEL_ID
    from face_models import load_manifest as load_face_manifest

    mapping = dict(os.environ if mapping is None else mapping)
    config = BridgeConfig.from_env(mapping)
    manifest = portable.load_manifest()
    chosen = {model["task"]: model["name"] for model in manifest["models"]}
    if mapping.get("GPU_IMMICH_CLIP_MODEL") != chosen["clip"] or mapping.get(
        "GPU_IMMICH_FACE_MODEL"
    ) not in {chosen["facial-recognition"], MODEL_ID}:
        raise ValueError(
            "Los modelos del lanzador deben coincidir con los identificadores respaldados."
        )
    source = (
        Path(upstream_root)
        if upstream_root
        else config.install_root / "immich/vendor" / portable.SOURCE_COMMIT / "machine-learning"
    )
    if upstream_root and not source.is_relative_to(config.install_root):
        raise ValueError("La fuente debe estar dentro de la instalación propia.")
    backend = NativeImmich(config, mapping, source, manifest)
    if mapping["GPU_IMMICH_FACE_MODEL"] == MODEL_ID:
        backend.face_manifest = load_face_manifest()
    return GuardedAsgi(
        backend, config, service="immich-ml", version=portable.VERSION, readiness=backend.readiness
    )


def multipart(entries, *, image=None, text=None):
    boundary = (
        "agencia-immich-"
        + hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()[:24]
    )
    chunks = []
    for name, value in (("entries", json.dumps(entries)), ("text", text)):
        if value is not None:
            chunks.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
                ).encode()
            )
    if image is not None:
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="astronaut.png"\r\nContent-Type: image/png\r\n\r\n'
            ).encode()
            + image
            + b"\r\n"
        )
    return b"".join(
        chunks
    ) + f"--{boundary}--\r\n".encode(), "multipart/form-data; boundary=" + boundary


def validate_embedding(value):
    if not isinstance(value, str):
        raise ValueError("No se recibió un embedding del protocolo original.")
    values = json.loads(value)
    if not isinstance(values, list) or len(values) != 512:
        raise ValueError("El embedding no tiene las 512 dimensiones esperadas.")
    if any(type(number) not in (int, float) for number in values):
        raise ValueError("El embedding debe contener únicamente números JSON.")
    try:
        normalized = [float(number) for number in values]
        raw = struct.pack("<512f", *normalized)
    except (OverflowError, struct.error):
        raise ValueError("El embedding excede el rango float32 esperado.") from None
    if not all(math.isfinite(number) for number in normalized) or not any(normalized):
        raise ValueError("El embedding está vacío o contiene valores no finitos.")
    return hashlib.sha256(raw).hexdigest()


def smoke_test(base_url, image_path, mapping):
    manifest = portable.load_manifest()
    url = urlsplit(base_url)
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "192.168.0.6"}
        or url.port != 3003
        or url.username
        or url.password
        or url.path not in {"", "/"}
    ):
        raise ValueError("La prueba solo puede consultar el servicio propio en el puerto3003.")
    image = Path(image_path).read_bytes()
    if hashlib.sha256(image).hexdigest() != manifest["acceptance_image"]["sha256"]:
        raise ValueError("La imagen de aceptación no coincide con el artefacto público fijado.")
    token = mapping["GPU_BRIDGE_BEARER_TOKEN"]

    def call(path, entries=None, *, picture=None, text=None):
        headers = {"Authorization": "Bearer " + token}
        data = None
        if entries is not None:
            data, headers["Content-Type"] = multipart(entries, image=picture, text=text)
        request = urllib.request.Request(base_url.rstrip("/") + path, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())

    clip, face = mapping["GPU_IMMICH_CLIP_MODEL"], mapping["GPU_IMMICH_FACE_MODEL"]
    started = time.monotonic()
    textual = call(
        "/predict", {"clip": {"textual": {"modelName": clip}}}, text="a person in a spacesuit"
    )
    visual = call("/predict", {"clip": {"visual": {"modelName": clip}}}, picture=image)
    faces = call(
        "/predict",
        {
            "facial-recognition": {
                "detection": {"modelName": face},
                "recognition": {"modelName": face},
            }
        },
        picture=image,
    )
    found = faces.get("facial-recognition")
    if not isinstance(found, list) or not found:
        raise ValueError("La prueba positiva no detectó ni procesó rostros.")
    for item in found:
        validate_embedding(item.get("embedding"))
        if not 0 <= item.get("score", -1) <= 1 or not isinstance(item.get("boundingBox"), dict):
            raise ValueError("La respuesta facial no conserva el protocolo esperado.")
    health = call("/health")
    required = {
        "clip/textual",
        "clip/visual",
        "facial-recognition/detection",
        "facial-recognition/recognition",
    }
    if (
        health.get("ready") is not True
        or health.get("cuda_available") is not True
        or not required <= set(health.get("inference_profiles", {}))
        or health.get("models_resident") != 0
    ):
        raise ValueError("No se acreditaron perfiles CUDA, salud y liberación de los modelos.")
    return {
        "verified": True,
        "service": "immich-ml",
        "version": portable.VERSION,
        "image_sha256": manifest["acceptance_image"]["sha256"],
        "faces": len(found),
        "text_embedding_sha256": validate_embedding(textual.get("clip")),
        "image_embedding_sha256": validate_embedding(visual.get("clip")),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "health": health,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-image", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:3003")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = smoke_test(args.base_url, args.smoke_image, os.environ)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: result[key] for key in ("verified", "version", "faces", "elapsed_seconds")}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
