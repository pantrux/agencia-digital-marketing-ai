"""Extiende el pipeline original de Immich sin sustituir multipart ni omitir CLIP."""

import asyncio
import inspect
import math
from dataclasses import dataclass

from face_models import MODEL_ID


@dataclass(frozen=True)
class FacePlan:
    clip_entries: list
    clip_visual: bool
    clip_textual: bool
    detection: bool
    recognition: bool
    min_score: float


class CommercialFaceAdapter:
    def __init__(self, upstream, faces, clip_model, load_faces, fail_backend=None):
        self.upstream, self.faces, self.clip_model = upstream, faces, clip_model
        self.load_faces, self.fail_backend = load_faces, fail_backend
        self.original_run = upstream.run_inference

    def parse_entries(self, entries):
        from fastapi import HTTPException

        try:
            parsed = self.upstream.PIPELINE_REQUEST.validate_json(entries)
        except ValueError:
            raise HTTPException(
                422, "La solicitud no cumple el esquema original de Immich."
            ) from None
        if parsed.ocr.detection is not None or parsed.ocr.recognition is not None:
            raise HTTPException(422, "Esta instalación no admite tareas OCR.")
        clip_slots = (parsed.clip.visual, parsed.clip.textual)
        if any(slot is not None and slot.model_name != self.clip_model for slot in clip_slots):
            raise HTTPException(422, "El modelo CLIP solicitado no está habilitado.")
        facial = parsed.facial_recognition
        if any(
            slot is not None and slot.model_name != MODEL_ID
            for slot in (facial.detection, facial.recognition)
        ):
            raise HTTPException(
                422, "El modelo facial solicitado no está permitido en modo comercial."
            )
        if facial.recognition is not None and facial.detection is None:
            raise HTTPException(
                400, "El reconocimiento facial requiere detección en la misma petición."
            )
        score = float(facial.detection.options.min_score) if facial.detection is not None else 0.7
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise HTTPException(422, "options.minScore debe ser finito y estar entre cero y uno.")
        # entries() sólo consulta el registro original de CLIP; nunca resuelve el nombre facial.
        clip_only = type(parsed)(clip=parsed.clip)
        clips = list(clip_only.entries())
        if not clips and facial.detection is None:
            raise HTTPException(422, "No se solicitó ninguna tarea admitida.")
        return FacePlan(
            clips,
            parsed.clip.visual is not None,
            parsed.clip.textual is not None,
            facial.detection is not None,
            facial.recognition is not None,
            score,
        )

    def install(self):
        previous = getattr(self.upstream, "_agency_face_adapter", None)
        if previous is not None:
            if previous is self:
                return self
            raise RuntimeError("La aplicación Immich ya tiene otro adaptador facial instalado.")
        parser = self.parse_entries

        def dependency(entries):
            return parser(entries)

        dependency.__signature__ = inspect.signature(self.upstream.get_entries)
        dependency.__annotations__ = self.upstream.get_entries.__annotations__
        self.upstream.app.dependency_overrides[self.upstream.get_entries] = dependency
        self.upstream.run_inference = self.dispatch
        self.upstream._agency_face_adapter = self
        return self

    async def dispatch(self, payload, plan):
        # Una desconexión no libera el semáforo exterior mientras CUDA siga trabajando en un hilo.
        pending = asyncio.create_task(self._dispatch(payload, plan))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                await pending
            finally:
                raise

    async def _dispatch(self, payload, plan):
        from fastapi import HTTPException

        if not isinstance(plan, FacePlan):
            raise HTTPException(422, "Falta el plan validado de la petición Immich.")
        if plan.clip_textual and not isinstance(payload, str):
            raise HTTPException(
                400,
                "CLIP textual requiere texto; la entrada original elige imagen si ambas existen.",
            )
        if plan.clip_visual and isinstance(payload, str):
            raise HTTPException(400, "CLIP visual requiere una imagen.")
        if plan.detection and (isinstance(payload, str) or not hasattr(payload, "convert")):
            raise HTTPException(400, "Las tareas faciales requieren una imagen.")
        if plan.detection and payload.width * payload.height > 64_000_000:
            raise HTTPException(413, "La imagen supera el tamaño facial permitido.")
        response = await self.original_run(payload, plan.clip_entries) if plan.clip_entries else {}
        if plan.detection:

            def infer():
                import numpy as np

                try:
                    self.load_faces()
                    image = np.asarray(payload.convert("RGB"), dtype=np.uint8)
                    detected = self.faces.detect(image, min_score=plan.min_score)
                    return self.faces.recognize(image, detected) if plan.recognition else detected
                except Exception:
                    if self.fail_backend:
                        self.fail_backend()
                    raise HTTPException(
                        503, "La inferencia facial CUDA no está disponible."
                    ) from None
                finally:
                    self.faces.close()

            response["facial-recognition"] = await self.upstream.run(infer)
        if hasattr(payload, "height") and hasattr(payload, "width"):
            response["imageHeight"], response["imageWidth"] = payload.height, payload.width
        return response
