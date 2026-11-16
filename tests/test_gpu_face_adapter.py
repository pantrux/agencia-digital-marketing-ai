"""HTTP multipart original con proveedores controlados, sin descargar pesos ni usar CUDA."""

import asyncio
import io
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gpu-bridge-win11"))
import face_adapter as adapter  # noqa: E402
import immich_service as service  # noqa: E402
from bridge_common import BridgeConfig, GuardedAsgi  # noqa: E402
from face_models import MODEL_ID  # noqa: E402


class DetectionOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")
    min_score: float = Field(default=0.7, alias="minScore")


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    model_name: str = Field(alias="modelName")
    options: dict = Field(default_factory=dict)


class DetectionSlot(Slot):
    options: DetectionOptions = Field(default_factory=DetectionOptions)


class Clip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    textual: Slot | None = None
    visual: Slot | None = None


class Facial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detection: DetectionSlot | None = None
    recognition: Slot | None = None


class Pipeline(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    clip: Clip = Field(default_factory=Clip)
    facial_recognition: Facial = Field(default_factory=Facial, alias="facial-recognition")
    ocr: Facial = Field(default_factory=Facial)

    def entries(self):
        # Simula solamente el límite del registro CLIP; el rostro nuevo nunca debe llegar aquí.
        if self.facial_recognition.detection or self.facial_recognition.recognition:
            raise AssertionError("No debe resolver modelos faciales en el registro legacy")
        return [
            SimpleNamespace(name=slot.model_name)
            for slot in (self.clip.visual, self.clip.textual)
            if slot is not None
        ]


class FaceProvider:
    def __init__(self, events):
        self.events = events
        self.detector = self.recognizer = None
        self.score = None

    def load(self, *args, **kwargs):
        self.events.append("carga facial")
        self.detector = self.recognizer = object()
        return self

    def detect(self, image, *, min_score):
        self.score = min_score
        self.events.append("detección facial")
        return {"boxes": [[10, 12, 30, 40]], "scores": [0.98], "landmarks": []}

    def recognize(self, image, faces):
        self.events.append("reconocimiento facial")
        return [
            {
                "boundingBox": {"x1": 10, "y1": 12, "x2": 30, "y2": 40},
                "score": 0.98,
                "embedding": json.dumps([1 / 512**0.5] * 512),
            }
        ]

    def close(self):
        self.events.append("liberación facial")
        self.detector = self.recognizer = None


def upstream_fixture(events):
    module = SimpleNamespace(app=FastAPI(), PIPELINE_REQUEST=TypeAdapter(Pipeline))
    module.model_cache = SimpleNamespace(clear=lambda: events.append("liberación CLIP"), _models={})

    async def run(function):
        return await asyncio.to_thread(function)

    def get_entries(entries: Annotated[str, Form()]):
        return Pipeline.model_validate_json(entries).entries()

    async def run_inference(payload, entries):
        events.append("CLIP original")
        result = {"clip": json.dumps([0.1] * 512)}
        if not isinstance(payload, str):
            result.update(imageWidth=payload.width, imageHeight=payload.height)
        return result

    @module.app.post("/predict")
    async def predict(
        entries: Annotated[object, Depends(get_entries)],
        image: Annotated[bytes | None, File()] = None,
        text: Annotated[str | None, Form()] = None,
    ):
        if image is None and text is None:
            raise HTTPException(400, "Falta imagen o texto")
        payload = Image.open(io.BytesIO(image)).convert("RGB") if image is not None else text
        return await module.run_inference(payload, entries)

    module.run, module.get_entries, module.run_inference = run, get_entries, run_inference
    return module


class HttpAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.faces = FaceProvider(self.events)
        self.upstream = upstream_fixture(self.events)
        self.backend = service.NativeImmich(None, {}, None, {})
        self.backend.module, self.backend.started = self.upstream, True
        self.backend.memory_free_mib = lambda: 4096
        self.backend.face_models = self.faces
        self.face_adapter = adapter.CommercialFaceAdapter(
            self.upstream, self.faces, "ViT-B-32__openai", lambda: self.faces.load()
        )
        self.face_adapter.install()
        self.backend.app = self.upstream.app
        config = BridgeConfig(
            "192.168.0.6", "t" * 48, Path("C:/GPU-Bridge"), Path("C:/GPU-Bridge/models")
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=GuardedAsgi(self.backend, config, "immich-ml", "3.3.6", None),
                client=("127.0.0.1", 4567),
            ),
            base_url="http://test",
            headers={"Authorization": "Bearer " + "t" * 48},
        )
        image = io.BytesIO()
        Image.new("RGB", (64, 48), (20, 40, 60)).save(image, format="PNG")
        self.image = image.getvalue()

    async def asyncTearDown(self):
        await self.client.aclose()

    def entries(self):
        return {
            "clip": {"visual": {"modelName": "ViT-B-32__openai"}},
            "facial-recognition": {
                "detection": {"modelName": MODEL_ID, "options": {"minScore": 0.83}},
                "recognition": {"modelName": MODEL_ID},
            },
        }

    async def send(self, entries):
        return await self.client.post(
            "/predict",
            data={"entries": json.dumps(entries)},
            files={"image": ("synthetic.png", self.image, "image/png")},
        )

    async def test_peticion_combinada_preserva_clip_rostros_dimensiones_y_liberacion(self):
        response = await self.send(self.entries())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["imageWidth"], result["imageHeight"]), (64, 48))
        self.assertEqual(len(json.loads(result["clip"])), 512)
        self.assertEqual(len(json.loads(result["facial-recognition"][0]["embedding"])), 512)
        self.assertEqual(self.faces.score, 0.83)
        self.assertIn("CLIP original", self.events)
        self.assertIsNone(self.faces.detector)

    async def test_buffalo_se_rechaza_antes_de_cualquier_inferencia_comercial(self):
        entries = self.entries()
        entries["facial-recognition"]["recognition"]["modelName"] = "buffalo_l"
        response = await self.send(entries)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("CLIP original", self.events)
        self.assertNotIn("carga facial", self.events)

    async def test_modelos_ocr_y_entradas_ajenas_se_rechazan_sin_inferencia(self):
        for entries in (
            {"clip": {"visual": {"modelName": "otro/modelo"}}},
            {"ocr": {"detection": {"modelName": "otro/modelo"}}},
            {"tarea-ajena": {}},
            {},
        ):
            with self.subTest(entries=entries):
                self.assertEqual((await self.send(entries)).status_code, 422)
        self.assertNotIn("CLIP original", self.events)
        self.assertNotIn("carga facial", self.events)

    async def test_proteccion_exterior_exige_bearer_antes_del_adaptador(self):
        response = await self.client.post(
            "/predict",
            data={"entries": json.dumps(self.entries())},
            files={"image": ("synthetic.png", self.image, "image/png")},
            headers={"Authorization": "Bearer incorrecto"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.events)

    async def test_reconocimiento_sin_deteccion_y_options_fuera_de_rango_son_explicitos(self):
        entries = self.entries()
        entries["facial-recognition"].pop("detection")
        self.assertEqual((await self.send(entries)).status_code, 400)
        entries = self.entries()
        entries["facial-recognition"]["detection"]["options"]["minScore"] = 1.5
        self.assertEqual((await self.send(entries)).status_code, 422)
        self.assertNotIn("carga facial", self.events)

    async def test_solo_clip_no_carga_rostros_y_solo_deteccion_preserva_salida(self):
        entries = self.entries()
        response = await self.send({"clip": entries["clip"]})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("facial-recognition", response.json())
        self.assertNotIn("carga facial", self.events)
        entries = self.entries()["facial-recognition"]
        entries.pop("recognition")
        response = await self.send({"facial-recognition": entries})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["facial-recognition"]["boxes"], [[10, 12, 30, 40]])
        self.assertNotIn("reconocimiento facial", self.events)

    async def test_texto_clip_original_funciona_y_textual_con_imagen_se_rechaza(self):
        entries = {"clip": {"textual": {"modelName": "ViT-B-32__openai"}}}
        response = await self.client.post(
            "/predict", data={"entries": json.dumps(entries), "text": "a plant"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(json.loads(response.json()["clip"])), 512)
        self.assertNotIn("imageWidth", response.json())
        entries["facial-recognition"] = self.entries()["facial-recognition"]
        response = await self.send(entries)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("carga facial", self.events)

    async def test_error_facial_no_devuelve_clip_parcial_y_libera_sesiones(self):
        def failing(*args):
            raise RuntimeError("detalle interno que no debe aparecer")

        self.faces.recognize = failing
        self.face_adapter.fail_backend = lambda: self.events.append("GPU invalidada")
        response = await self.send(self.entries())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("clip", response.json())
        self.assertNotIn("detalle interno", response.text)
        self.assertIn("GPU invalidada", self.events)
        self.assertIsNone(self.faces.detector)
        self.assertIn("liberación CLIP", self.events)

    async def test_cancelacion_retiene_semaforo_hasta_terminar_trabajo_del_proveedor(self):
        entered, finish = threading.Event(), threading.Event()
        original = self.faces.recognize

        def slow(*args):
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("La prueba no liberó el proveedor controlado")
            return original(*args)

        self.faces.recognize = slow
        pending = asyncio.create_task(self.send(self.entries()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            pending.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.backend.requests_lock.locked())
            response = await self.send(self.entries())
            self.assertEqual(response.status_code, 503)
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertFalse(self.backend.requests_lock.locked())
            self.assertIsNone(self.faces.detector)
        finally:
            finish.set()
            if not pending.done():
                await pending


class FactoryTests(unittest.TestCase):
    def test_factory_comercial_anade_referencia_facial_sin_cargar_cuda(self):
        mapping = {
            "GPU_BRIDGE_HOST": "192.168.0.6",
            "GPU_BRIDGE_BEARER_TOKEN": "t" * 48,
            "GPU_INSTALL_ROOT": "C:/GPU-Bridge",
            "GPU_MODEL_CACHE_ROOT": "C:/GPU-Bridge/models",
            "GPU_IMMICH_CLIP_MODEL": "ViT-B-32__openai",
            "GPU_IMMICH_FACE_MODEL": MODEL_ID,
        }
        app = service.create_app(mapping)
        self.assertEqual(app.app.face_manifest["model_id"], MODEL_ID)
        self.assertFalse(app.app.started)
        self.assertIsNone(app.app.ort)
        self.assertEqual(
            {model["name"] for model in app.app.manifest["models"]},
            {"ViT-B-32__openai", "buffalo_l"},
        )

    def test_release_cierra_rostros_incluso_si_no_hay_modulo_legacy(self):
        backend = service.NativeImmich(None, {}, None, {})
        backend.face_models = FaceProvider([]).load()
        backend.release_models()
        self.assertIsNone(backend.face_models.detector)


if __name__ == "__main__":
    unittest.main()
