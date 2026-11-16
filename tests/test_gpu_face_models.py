"""Geometría y protocolo de YuNet/AuraFace; los dobles no acreditan inferencia CUDA."""

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gpu-bridge-win11"))
SPEC = importlib.util.spec_from_file_location(
    "face_models", ROOT / "gpu-bridge-win11/face_models.py"
)
face = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = face
SPEC.loader.exec_module(face)


def heads():
    return {
        f"{name}_{stride}": np.zeros((1, (640 // stride) ** 2, columns), np.float32)
        for stride in (8, 16, 32)
        for name, columns in (("cls", 1), ("obj", 1), ("bbox", 4), ("kps", 10))
    }


class DecoderTests(unittest.TestCase):
    def test_decodifica_12_heads_caja_y_cinco_landmarks_por_stride(self):
        data = heads()
        index = 2 * 80 + 3
        data["cls_8"][0, index] = 0.81
        data["obj_8"][0, index] = 1.0
        data["bbox_8"][0, index] = [0.5, 0.5, np.log(2), np.log(4)]
        data["kps_8"][0, index] = np.arange(10) / 10
        result = face.decode_yunet(data, min_score=0.7)
        np.testing.assert_allclose(result["boxes"], [[20, 4, 36, 36]], atol=1e-5)
        np.testing.assert_allclose(result["scores"], [0.9], atol=1e-6)
        expected = np.arange(10).reshape(5, 2) / 10 * 8 + [24, 16]
        np.testing.assert_allclose(result["landmarks"][0], expected, atol=1e-5)

    def test_sin_detecciones_conserva_formas_del_protocolo(self):
        result = face.decode_yunet(heads())
        self.assertEqual(result["boxes"].shape, (0, 4))
        self.assertEqual(result["scores"].shape, (0,))
        self.assertEqual(result["landmarks"].shape, (0, 5, 2))

    def test_rechaza_cantidad_formas_y_valores_no_finitos(self):
        for mutation in ("falta", "forma", "nan", "desborde"):
            data = heads()
            if mutation == "falta":
                data.pop("obj_32")
            elif mutation == "forma":
                data["bbox_8"] = np.zeros((1, 6400, 3), np.float32)
            elif mutation == "nan":
                data["cls_8"][0, 0] = np.nan
            else:
                data["cls_8"][0, 0] = data["obj_8"][0, 0] = 1.0
                data["kps_8"][0, 0, 0] = np.finfo(np.float32).max
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                face.decode_yunet(data)

    def test_nms_descarta_solapamiento_y_conserva_orden_de_confianza(self):
        boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [20, 20, 30, 30]], np.float32)
        keep = face.non_maximum_suppression(boxes, np.array([0.8, 0.9, 0.7]), 0.3)
        self.assertEqual(keep.tolist(), [1, 2])


class GeometryTests(unittest.TestCase):
    def test_detector_convierte_rgb_a_bgr_sin_normalizar_y_rellena_abajo(self):
        image = np.zeros((320, 640, 3), np.uint8)
        image[:] = [10, 20, 30]
        blob, scale = face.prepare_detection(image)
        self.assertEqual(blob.shape, (1, 3, 640, 640))
        self.assertEqual(blob.dtype, np.float32)
        np.testing.assert_array_equal(blob[0, :, 0, 0], [30, 20, 10])
        np.testing.assert_array_equal(blob[0, :, 639, 0], [0, 0, 0])
        self.assertEqual(scale, (1.0, 1.0))

    def test_alineacion_recupera_plantilla_bajo_rotacion_escala_y_traslacion(self):
        target = np.asarray(face.ARCFACE_TEMPLATE, np.float64)
        angle = 0.25
        rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        source = target @ rotation.T * 1.7 + [12, 24]
        matrix = face.similarity_transform(source)
        np.testing.assert_allclose(source @ matrix[:, :2].T + matrix[:, 2], target, atol=2e-5)
        with self.assertRaises(ValueError):
            face.similarity_transform(np.ones((5, 2)))

    def test_recognizer_normaliza_rgb_112_a_menos_uno_y_uno(self):
        image = np.zeros((112, 112, 3), np.uint8)
        image[:] = [0, 127, 255]
        blob = face.prepare_recognition(image)
        np.testing.assert_allclose(blob[0, :, 0, 0], [-1, -0.5 / 127.5, 1], atol=1e-6)
        self.assertTrue(blob.flags.c_contiguous)
        with self.assertRaises(ValueError):
            face.prepare_recognition(image[:100])


class ArtifactTests(unittest.TestCase):
    def test_manifiesto_fija_solo_dos_pesos_y_sus_licencias(self):
        manifest = face.load_manifest()
        self.assertEqual(manifest["model_id"], face.MODEL_ID)
        self.assertEqual(
            {model["license"] for model in manifest["models"].values()}, {"MIT", "Apache-2.0"}
        )
        self.assertEqual(len([f for f in manifest["artifacts"] if f["role"] == "weights"]), 2)
        for key, value in [("model_id", "buffalo_l"), ("embedding_dimensions", 128)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                face.validate_manifest(dict(manifest, **{key: value}))
        for key, value in [
            ("license", "CC-BY-NC-4.0"),
            ("sha256", "0" * 64),
            ("path", "../modelo.onnx"),
        ]:
            changed = copy.deepcopy(manifest)
            if key == "license":
                changed["models"]["recognition"][key] = value
            else:
                changed["artifacts"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                face.validate_manifest(changed)

    def test_verificacion_local_rechaza_bytes_extra_y_escape_sin_escribir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "face" / face.MODEL_ID
            root.mkdir(parents=True)
            file = root / "detection.onnx"
            file.write_bytes(b"fixture")
            entries = [
                {
                    "path": "detection.onnx",
                    "size": 7,
                    "sha256": hashlib.sha256(b"fixture").hexdigest(),
                }
            ]
            face.verify_artifacts(root, entries)
            extra = root / "ajeno.txt"
            extra.write_text("conservar")
            with self.assertRaises(ValueError):
                face.verify_artifacts(root, entries)
            self.assertEqual(extra.read_text(), "conservar")
            with self.assertRaises(ValueError):
                face.verify_artifacts(root, [dict(entries[0], path="../otro")])
            extra.unlink()
            file.write_bytes(b"alterar")
            with self.assertRaises(ValueError):
                face.verify_artifacts(root, entries)

    def test_importacion_no_carga_numpy_cv2_ort_ni_immich(self):
        code = "import sys; sys.path.insert(0,sys.argv[1]); import face_models; assert not any(x in sys.modules for x in ('numpy','cv2','onnxruntime','immich_portability'))"
        result = subprocess.run(
            [sys.executable, "-c", code, str(ROOT / "gpu-bridge-win11")],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


class ProtocolTests(unittest.TestCase):
    def test_detector_restituye_coordenadas_de_imagen_original_y_no_padding(self):
        data = heads()
        index = 2 * 80 + 3
        data["cls_8"][0, index] = data["obj_8"][0, index] = 1
        data["bbox_8"][0, index] = [0.5, 0.5, np.log(2), np.log(4)]
        backend = face.FaceModels(Path("face") / face.MODEL_ID)
        backend.detector = SimpleNamespace(run=lambda names, feed: [data[name] for name in names])
        image = np.zeros((640, 1280, 3), np.uint8)
        with patch.object(
            face,
            "prepare_detection",
            return_value=(np.zeros((1, 3, 640, 640), np.float32), (0.5, 0.5)),
        ):
            result = backend.detect(image)
        np.testing.assert_allclose(result["boxes"], [[40, 8, 72, 72]], atol=1e-4)
        np.testing.assert_allclose(result["landmarks"][0], np.tile([48, 32], (5, 1)))

    def test_recognizer_devuelve_json_512_normalizado_y_coordenadas_originales(self):
        class Recognition:
            def run(self, names, feed):
                self.feed = feed
                return [np.arange(1, 513, dtype=np.float32)[None]]

        backend = face.FaceModels(Path("face") / face.MODEL_ID)
        backend.recognizer = Recognition()
        image = np.zeros((112, 112, 3), np.uint8)
        faces = {
            "boxes": np.array([[2, 3, 80, 90]], np.float32),
            "scores": np.array([0.95], np.float32),
            "landmarks": np.asarray([face.ARCFACE_TEMPLATE], np.float32),
        }
        with patch.object(face, "align_crop", return_value=image):
            result = backend.recognize(image, faces)
        self.assertEqual(result[0]["boundingBox"], {"x1": 2.0, "y1": 3.0, "x2": 80.0, "y2": 90.0})
        embedding = json.loads(result[0]["embedding"])
        self.assertEqual(len(embedding), 512)
        self.assertAlmostEqual(np.linalg.norm(embedding), 1.0, places=6)
        self.assertEqual(backend.recognizer.feed["data"].shape, (1, 3, 112, 112))

    def test_serializacion_redondea_caja_a_enteros_como_el_protocolo_immich(self):
        backend = face.FaceModels(Path("face") / face.MODEL_ID)
        backend.recognizer = SimpleNamespace(run=lambda *args: [np.ones((1, 512), np.float32)])
        image = np.zeros((112, 112, 3), np.uint8)
        faces = {
            "boxes": np.array([[2.25, 3.5, 80.75, 90.5]], np.float32),
            "scores": np.array([0.95], np.float32),
            "landmarks": np.asarray([face.ARCFACE_TEMPLATE], np.float32),
        }
        with patch.object(face, "align_crop", return_value=image):
            box = backend.recognize(image, faces)[0]["boundingBox"]
        self.assertEqual(box, {"x1": 2, "y1": 4, "x2": 81, "y2": 90})
        self.assertTrue(all(isinstance(value, int) for value in box.values()))

    def test_serializacion_preserva_caja_positiva_que_rint_colapsaria(self):
        backend = face.FaceModels(Path("face") / face.MODEL_ID)
        backend.recognizer = SimpleNamespace(run=lambda *args: [np.ones((1, 512), np.float32)])
        image = np.zeros((112, 112, 3), np.uint8)
        faces = {
            "boxes": np.array([[2.1, 3.1, 2.2, 3.2]], np.float32),
            "scores": np.array([0.95], np.float32),
            "landmarks": np.asarray([face.ARCFACE_TEMPLATE], np.float32),
        }
        with patch.object(face, "align_crop", return_value=image):
            result = backend.recognize(image, faces)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["boundingBox"], {"x1": 2, "y1": 3, "x2": 3, "y2": 4})

    def test_recognizer_rechaza_128_nan_y_cero(self):
        image = np.zeros((112, 112, 3), np.uint8)
        faces = {
            "boxes": np.array([[2, 3, 80, 90]], np.float32),
            "scores": np.array([0.95], np.float32),
            "landmarks": np.asarray([face.ARCFACE_TEMPLATE], np.float32),
        }
        for values in (np.ones((1, 128)), np.full((1, 512), np.nan), np.zeros((1, 512))):
            backend = face.FaceModels(Path("face") / face.MODEL_ID)
            backend.recognizer = SimpleNamespace(run=lambda *args, _values=values: [_values])
            with (
                self.subTest(shape=values.shape),
                patch.object(face, "align_crop", return_value=image),
                self.assertRaises(ValueError),
            ):
                backend.recognize(image, faces)


class ProfilingTests(unittest.TestCase):
    def test_carga_exige_cuda_primario_y_acredita_ambos_perfiles_solo_al_ejecutar(self):
        import immich_portability as portable

        class Session:
            def __init__(self, model, providers, sess_options, **kwargs):
                self.model, self.providers, self.options = model, providers, sess_options

            def get_providers(self):
                return self.providers

            def disable_fallback(self):
                self.fallback_disabled = True

            def run(self, names, feeds):
                return (
                    [heads()[name] for name in names]
                    if "input" in feeds
                    else [np.ones((1, 512), np.float32)]
                )

            def end_profiling(self):
                path = Path(self.options.profile_file_prefix + ".json")
                path.write_text(
                    json.dumps(
                        [
                            {
                                "cat": "Node",
                                "name": "Conv_kernel_time",
                                "args": {"op_name": "Conv", "provider": "CUDAExecutionProvider"},
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                return str(path)

        fake_ort = SimpleNamespace(SessionOptions=SimpleNamespace, InferenceSession=Session)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profiles"
            profile.mkdir()
            model = root / "face" / face.MODEL_ID
            backend = face.FaceModels(model)
            evidence = portable.CudaEvidence()
            with patch.object(face, "verify_artifacts"):
                backend.load(fake_ort, profile_directory=profile, evidence=evidence)
            self.assertFalse(evidence.snapshot()["ready"])
            image = np.zeros((640, 640, 3), np.uint8)
            backend.detect(image)
            faces = {
                "boxes": np.array([[2, 3, 80, 90]], np.float32),
                "scores": np.array([0.95], np.float32),
                "landmarks": np.asarray([face.ARCFACE_TEMPLATE], np.float32),
            }
            with patch.object(face, "align_crop", return_value=image[:112, :112]):
                backend.recognize(image, faces)
            self.assertEqual(
                set(evidence.snapshot()["inference_profiles"]),
                {"facial-recognition/detection", "facial-recognition/recognition"},
            )
            self.assertTrue(evidence.snapshot()["ready"])
            self.assertTrue(backend.detector.fallback_disabled)
            backend.close()
            self.assertIsNone(backend.detector)
            self.assertIsNone(backend.recognizer)

    def test_carga_rechaza_cpu_como_proveedor_primario(self):
        import immich_portability as portable

        fake_ort = SimpleNamespace(
            SessionOptions=SimpleNamespace,
            InferenceSession=lambda *args, **kwargs: SimpleNamespace(
                disable_fallback=lambda: None, get_providers=lambda: ["CPUExecutionProvider"]
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profiles"
            profile.mkdir()
            backend = face.FaceModels(root / "face" / face.MODEL_ID)
            evidence = portable.CudaEvidence()
            with patch.object(face, "verify_artifacts"), self.assertRaises(RuntimeError):
                backend.load(fake_ort, profile_directory=profile, evidence=evidence)
            self.assertTrue(evidence.snapshot()["backend_failed"])
            self.assertIsNone(backend.detector)


if __name__ == "__main__":
    unittest.main()
