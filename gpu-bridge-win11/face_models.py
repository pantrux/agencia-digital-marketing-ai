"""YuNet y AuraFace propios: geometría CPU y sesiones CUDA acreditadas por Immich."""

import hashlib
import json
import stat
from pathlib import Path

MODEL_ID = "agency_yunet_auraface_v1"
SIZE = 640
STRIDES = (8, 16, 32)
ARCFACE_TEMPLATE = (
    (38.2946, 51.6963),
    (73.5318, 51.5014),
    (56.0252, 71.7366),
    (41.5493, 92.3655),
    (70.7299, 92.2041),
)
YUNET_OUTPUTS = tuple(
    f"{name}_{stride}" for name in ("cls", "obj", "bbox", "kps") for stride in STRIDES
)
ZOO_REVISION = "d4938dfc9d4ec5d098bfa33e98b3f3345a236586"
AURA_REVISION = "af6d057c9b0ec4071d4c49c80e3539258798b609"
PINNED = (
    (
        "detection.onnx",
        "weights",
        "detection",
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        232589,
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    (
        "recognition.onnx",
        "weights",
        "recognition",
        "glintr100.onnx",
        260694151,
        "a7933ea5330113b01c9b60351d8f4c33003f145d8470ac5f0e52ee2effe25c60",
    ),
    (
        "LICENSE.YuNet.txt",
        "license",
        "detection",
        "models/face_detection_yunet/LICENSE",
        1085,
        "c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958",
    ),
    (
        "NOTICE.YuNet.md",
        "model_card",
        "detection",
        "models/face_detection_yunet/README.md",
        2745,
        "a45578c40b04f9aaa1e154bca1d0179ffad6dab840458ac8f7cb0a9362413b72",
    ),
    (
        "LICENSE.AuraFace.md",
        "license",
        "recognition",
        "LICENSE.md",
        12624,
        "609e2cb599f84aaa41d8ef29d8fdb04d164fab22e8d9292ca34a599d0f56a338",
    ),
    (
        "NOTICE.AuraFace.md",
        "model_card",
        "recognition",
        "README.md",
        3959,
        "106272348689716d3c159ff16ec42e5f36aafebca591b3678b375f8aa2d12cde",
    ),
)


def validate_manifest(manifest):
    if (
        manifest.get("schema_version") != 1
        or manifest.get("model_id") != MODEL_ID
        or manifest.get("embedding_dimensions") != 512
        or manifest.get("cache_relative") != f"face/{MODEL_ID}"
    ):
        raise ValueError(
            "La identidad facial no coincide con el contrato propio de 512 dimensiones."
        )
    models = manifest.get("models", {})
    if (
        set(models) != {"detection", "recognition"}
        or models["detection"].get("license") != "MIT"
        or models["recognition"].get("license") != "Apache-2.0"
        or models["detection"].get("revision") != ZOO_REVISION
        or models["recognition"].get("revision") != AURA_REVISION
    ):
        raise ValueError("Los modelos o las licencias no coinciden con los pesos aprobados.")
    expected = []
    for path, role, model, remote, size, sha in PINNED:
        repo, revision = (
            ("opencv/opencv_zoo", ZOO_REVISION)
            if model == "detection"
            else ("fal/AuraFace-v1", AURA_REVISION)
        )
        expected.append(
            {
                "path": path,
                "role": role,
                "model": model,
                "url": f"https://huggingface.co/{repo}/resolve/{revision}/{remote}",
                "size": size,
                "sha256": sha,
            }
        )
    if manifest.get("artifacts") != expected:
        raise ValueError(
            "Los seis artefactos deben conservar exactamente rutas, URLs y huellas fijadas."
        )
    return manifest


def load_manifest(path=None):
    path = Path(path) if path else Path(__file__).with_name("face-artifacts.json")
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def verify_artifacts(root, entries):
    """Sólo lee archivos regulares propios; no descarga, adopta, limpia ni escribe cachés."""
    root = Path(root)
    if (
        not root.is_absolute()
        or root.name != MODEL_ID
        or root.parent.name != "face"
        or not root.is_dir()
    ):
        raise ValueError("La caché facial no corresponde al nombre propio fijado.")
    if any(path.is_symlink() or path.is_junction() for path in (root, *root.parents)):
        raise ValueError("La caché facial no admite enlaces ni reanálisis.")
    expected = set()
    for entry in entries:
        name = entry["path"]
        if Path(name).name != name or name in {".", ".."} or any(char in name for char in "\\:/"):
            raise ValueError("La ruta de un artefacto sale de la caché facial.")
        if name in expected:
            raise ValueError("El manifiesto duplica un artefacto facial.")
        expected.add(name)
        path = root / name
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or path.is_symlink()
            or getattr(metadata, "st_file_attributes", 0) & 0x400
            or metadata.st_size != entry["size"]
        ):
            raise ValueError("Un artefacto facial no es un archivo propio del tamaño fijado.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != entry["sha256"]:
            raise ValueError("La huella de un artefacto facial no coincide.")
    if {path.name for path in root.iterdir()} != expected:
        raise ValueError(
            "La caché facial contiene archivos no declarados; se conservan sin adoptarlos."
        )


def non_maximum_suppression(boxes, scores, threshold=0.3):
    """IoU sin píxel inclusivo, igual al convenio de rectángulos flotantes de OpenCV."""
    import numpy as np

    if not 0 <= threshold <= 1:
        raise ValueError("El umbral NMS debe estar entre cero y uno.")
    pending = np.argsort(-scores, kind="stable")[:5000]
    selected = []
    areas = np.maximum(0, boxes[:, 2:] - boxes[:, :2]).prod(axis=1)
    while pending.size:
        chosen, others = pending[0], pending[1:]
        selected.append(chosen)
        intersection = np.maximum(
            0,
            np.minimum(boxes[chosen, 2:], boxes[others, 2:])
            - np.maximum(boxes[chosen, :2], boxes[others, :2]),
        ).prod(axis=1)
        union = areas[chosen] + areas[others] - intersection
        overlap = np.divide(intersection, union, out=np.zeros_like(intersection), where=union > 0)
        pending = others[overlap <= threshold]
    return np.asarray(selected, dtype=np.intp)


def decode_yunet(heads, *, min_score=0.7, nms_threshold=0.3):
    """Decodifica las doce salidas de YuNet; no interpreta heads SCRFD como YuNet."""
    import numpy as np

    if set(heads) != set(YUNET_OUTPUTS) or not 0 <= min_score <= 1:
        raise ValueError("Las salidas o el umbral no corresponden al detector YuNet fijado.")
    boxes, scores, landmarks = [], [], []
    for stride in STRIDES:
        width = SIZE // stride
        checked = {}
        for name, columns in (("cls", 1), ("obj", 1), ("bbox", 4), ("kps", 10)):
            array = np.asarray(heads[f"{name}_{stride}"], dtype=np.float32)
            if array.shape != (1, width * width, columns) or not np.isfinite(array).all():
                raise ValueError("YuNet devolvió una forma o valores no finitos inválidos.")
            checked[name] = array[0]
        confidence = np.sqrt(
            np.clip(checked["cls"][:, 0], 0, 1) * np.clip(checked["obj"][:, 0], 0, 1)
        )
        selected = np.flatnonzero(confidence >= min_score)
        grid = np.column_stack((selected % width, selected // width))
        prediction = checked["bbox"][selected]
        with np.errstate(over="raise", invalid="raise"):
            try:
                center = (grid + prediction[:, :2]) * stride
                extent = np.exp(prediction[:, 2:]) * stride / 2
                decoded = np.concatenate((center - extent, center + extent), axis=1)
                points = (checked["kps"][selected].reshape(-1, 5, 2) + grid[:, None]) * stride
                limit = np.finfo(np.float32).max
                if any(
                    not np.isfinite(array).all() or (np.abs(array) > limit).any()
                    for array in (decoded, points)
                ):
                    raise ValueError("YuNet devolvió coordenadas fuera del intervalo float32.")
            except FloatingPointError:
                raise ValueError(
                    "YuNet devolvió una geometría fuera del intervalo finito."
                ) from None
        boxes.append(decoded.astype(np.float32))
        scores.append(confidence[selected])
        landmarks.append(points.astype(np.float32))
    boxes, scores, landmarks = (
        np.concatenate(boxes),
        np.concatenate(scores),
        np.concatenate(landmarks),
    )
    keep = non_maximum_suppression(boxes, scores, nms_threshold)
    return {"boxes": boxes[keep], "scores": scores[keep], "landmarks": landmarks[keep]}


def checked_image(image, shape=None):
    import numpy as np

    image = np.asarray(image)
    if (
        image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
        or not image.shape[0]
        or not image.shape[1]
        or image.shape[0] * image.shape[1] > 64_000_000
        or (shape is not None and image.shape[:2] != shape)
    ):
        raise ValueError("Se requiere una imagen RGB uint8 válida dentro del tamaño permitido.")
    return image


def prepare_detection(image):
    """RGB→BGR, letterbox superior izquierdo y NCHW 0..255; YuNet no usa normalización ArcFace."""
    import numpy as np

    image = checked_image(image)
    height, width = image.shape[:2]
    ratio = SIZE / max(height, width)
    new_width, new_height = max(1, int(width * ratio)), max(1, int(height * ratio))
    bgr = image[:, :, ::-1]
    if (new_height, new_width) != (height, width):
        import cv2

        bgr = cv2.resize(bgr, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((SIZE, SIZE, 3), dtype=np.uint8)
    canvas[:new_height, :new_width] = bgr
    blob = np.ascontiguousarray(canvas.transpose(2, 0, 1)[None], dtype=np.float32)
    return blob, (new_width / width, new_height / height)


def similarity_transform(landmarks):
    """Mínimos cuadrados de similitud sin reflexión hacia los cinco puntos ArcFace112."""
    import numpy as np

    source = np.asarray(landmarks, dtype=np.float64)
    if source.shape != (5, 2) or not np.isfinite(source).all():
        raise ValueError("La alineación requiere cinco puntos faciales finitos.")
    target = np.asarray(ARCFACE_TEMPLATE, dtype=np.float64)
    centered, goal = source - source.mean(0), target - target.mean(0)
    variance = np.square(centered).sum()
    if variance < 1e-8 or np.linalg.matrix_rank(centered) < 2:
        raise ValueError("Los puntos faciales no permiten una alineación estable.")
    left, singular, right = np.linalg.svd(centered.T @ goal)
    signs = np.array([1.0, np.sign(np.linalg.det(right.T @ left.T))])
    rotation = right.T @ np.diag(signs) @ left.T
    linear = rotation * np.dot(singular, signs) / variance
    translation = target.mean(0) - linear @ source.mean(0)
    return np.column_stack((linear, translation)).astype(np.float32)


def align_crop(image, landmarks):
    import cv2

    return cv2.warpAffine(
        checked_image(image),
        similarity_transform(landmarks),
        (112, 112),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def prepare_recognition(crop):
    """El ONNX AuraFace comienza en Conv: recibe RGB externo normalizado con media/std127.5."""
    import numpy as np

    image = checked_image(crop, (112, 112))
    return np.ascontiguousarray((image.transpose(2, 0, 1)[None].astype(np.float32) - 127.5) / 127.5)


def checked_faces(faces):
    import numpy as np

    boxes = np.asarray(faces["boxes"], dtype=np.float32)
    scores = np.asarray(faces["scores"], dtype=np.float32)
    landmarks = np.asarray(faces["landmarks"], dtype=np.float32)
    count = len(scores)
    if (
        boxes.shape != (count, 4)
        or scores.shape != (count,)
        or landmarks.shape != (count, 5, 2)
        or not all(np.isfinite(array).all() for array in (boxes, scores, landmarks))
        or not ((scores >= 0) & (scores <= 1)).all()
        or not (boxes[:, 2:] > boxes[:, :2]).all()
    ):
        raise ValueError("Las caras no cumplen el contrato de cajas, confianza y cinco puntos.")
    return boxes, scores, landmarks


class FaceModels:
    """El llamador conserva ciclo de vida/serialización HTTP; aquí sólo viven dos modelos propios."""

    def __init__(self, model_root):
        self.root = Path(model_root)
        self.manifest = load_manifest()
        self.detector = self.recognizer = None

    def load(self, ort, *, profile_directory, evidence=None):
        """Carga explícita con el mismo acreditador CUDA de Immich, sin descargas ni fallback."""
        import immich_portability as portable

        if self.detector is not None or self.recognizer is not None:
            raise RuntimeError("Los modelos faciales propios ya están cargados.")
        verify_artifacts(self.root, self.manifest["artifacts"])
        profile = Path(profile_directory)
        if (
            not profile.is_absolute()
            or not profile.is_dir()
            or not profile.resolve().is_relative_to(self.root.resolve().parents[1])
            or any(path.is_symlink() or path.is_junction() for path in (profile, *profile.parents))
        ):
            raise ValueError("El perfil debe permanecer en la caché propia sin enlaces.")
        try:
            for kind, attribute, file in (
                ("detection", "detector", "detection.onnx"),
                ("recognition", "recognizer", "recognition.onnx"),
            ):
                options = ort.SessionOptions()
                options.intra_op_num_threads = options.inter_op_num_threads = 1
                options.enable_mem_pattern = False
                session = portable.strict_cuda_session(
                    ort.InferenceSession,
                    str(self.root / file),
                    providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                    provider_options=[
                        {
                            "device_id": "0",
                            "arena_extend_strategy": "kSameAsRequested",
                            "gpu_mem_limit": "1073741824",
                            "cudnn_conv_algo_search": "HEURISTIC",
                        },
                        {},
                    ],
                    sess_options=options,
                    profile_directory=profile,
                    evidence=evidence,
                    model_kind=f"facial-recognition/{kind}",
                )
                setattr(self, attribute, session)
        except Exception:
            self.close()
            raise
        return self

    def detect(self, image_rgb, *, min_score=0.7):
        import numpy as np

        if self.detector is None:
            raise RuntimeError("El detector facial propio todavía no está cargado.")
        image = checked_image(image_rgb)
        blob, scale = prepare_detection(image)
        outputs = self.detector.run(list(YUNET_OUTPUTS), {"input": blob})
        if len(outputs) != len(YUNET_OUTPUTS):
            raise ValueError("El detector no devolvió los doce heads fijados.")
        faces = decode_yunet(dict(zip(YUNET_OUTPUTS, outputs, strict=True)), min_score=min_score)
        factors = np.asarray(scale, dtype=np.float32)
        faces["boxes"] /= np.tile(factors, 2)
        faces["landmarks"] /= factors
        height, width = image.shape[:2]
        faces["boxes"] = np.clip(faces["boxes"], 0, [width, height, width, height]).astype(
            np.float32
        )
        valid = (faces["boxes"][:, 2:] > faces["boxes"][:, :2]).all(axis=1)
        return {key: value[valid] for key, value in faces.items()}

    def recognize(self, image_rgb, faces):
        import numpy as np

        image = checked_image(image_rgb)
        boxes, scores, landmarks = checked_faces(faces)
        if not len(scores):
            return []
        if self.recognizer is None:
            raise RuntimeError("El reconocedor facial propio todavía no está cargado.")
        result = []
        for box, score, points in zip(boxes, scores, landmarks, strict=True):
            height, width = image.shape[:2]
            bounded = np.clip(box, 0, [width, height, width, height])
            if not (bounded[2:] > bounded[:2]).all():
                raise ValueError("La caja facial no intersecta la imagen original.")
            integer_box = np.rint(bounded).astype(np.int64)
            for axis in (0, 1):
                if integer_box[axis + 2] <= integer_box[axis]:
                    integer_box[axis] = int(np.floor(bounded[axis]))
                    integer_box[axis + 2] = int(np.ceil(bounded[axis + 2]))
            crop = align_crop(image, points)
            outputs = self.recognizer.run(["1333"], {"data": prepare_recognition(crop)})
            if len(outputs) != 1:
                raise ValueError("AuraFace debe devolver únicamente su embedding.")
            embedding = np.asarray(outputs[0], dtype=np.float32)
            if embedding.shape != (1, 512) or not np.isfinite(embedding).all():
                raise ValueError("AuraFace debe devolver exactamente 512 valores finitos.")
            norm = np.linalg.norm(embedding.astype(np.float64))
            if not np.isfinite(norm) or norm < 1e-12:
                raise ValueError("El embedding facial no puede ser nulo.")
            embedding = (embedding[0] / norm).astype(np.float32)
            result.append(
                {
                    # Se conserva precisión geométrica hasta la frontera de enteros de Immich.
                    "boundingBox": dict(
                        zip(("x1", "y1", "x2", "y2"), map(int, integer_box), strict=True)
                    ),
                    "score": float(score),
                    # Immich 3.3.6 transporta una cadena JSON, no Base64 ni un array HTTP distinto.
                    "embedding": json.dumps(
                        embedding.tolist(), separators=(",", ":"), allow_nan=False
                    ),
                }
            )
        return result

    def close(self):
        self.detector = self.recognizer = None
