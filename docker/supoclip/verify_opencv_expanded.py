"""Comprueba la rueda ampliada en su runtime Linux con entradas fijadas."""

import argparse
import hashlib
import json
import platform
import re
import sys
from pathlib import Path

YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
HAAR = {
    "haarcascade_frontalface_default.xml": "0f7d4527844eb514d4a4948e822da90fbb16a34a0bbbbc6adc6498747a5aafb0",
    "haarcascade_profileface.xml": "b39a4a3be45539db146a7fc1d3e761a292c196eb88421185e6a615b3055e612d",
}
HEADS = tuple(
    f"{name}_{stride}" for name in ("cls", "obj", "bbox", "kps") for stride in (8, 16, 32)
)


def require(value, message):
    if not value:
        raise ValueError(message)


def pinned(path, sha256, maximum):
    require(path.is_absolute() and path.is_file() and not path.is_symlink(), "ENTRADA_IRREGULAR")
    require(0 < path.stat().st_size <= maximum, "ENTRADA_TAMANO")
    with path.open("rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == sha256, "ENTRADA_SHA256")


def video_probe(cv2, np, path):
    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    try:
        require(capture.isOpened() and capture.getBackendName() == "FFMPEG", "VIDEOIO_FFMPEG")
        require(abs(capture.get(cv2.CAP_PROP_FPS) - 12) < 0.01, "VIDEO_FPS")
        frames = []
        for _ in range(9):
            ok, frame = capture.read()
            require(ok and frame.shape == (180, 320, 3), "VIDEO_FRAME")
            frames.append(frame)
        require(not np.array_equal(frames[0], frames[6]), "VIDEO_FIXTURE_SIN_CAMBIO")
        require(capture.set(cv2.CAP_PROP_POS_FRAMES, 6), "VIDEO_SEEK_FRAME")
        ok, seeked = capture.read()
        require(ok and np.array_equal(seeked, frames[6]), "VIDEO_SEEK_CONTENIDO")
        return {"backend": capture.getBackendName(), "frames_read": len(frames), "seek_frame": 6}
    finally:
        capture.release()


def facial_probe(cv2, np, yunet, site):
    haar = Path(cv2.data.haarcascades).resolve(strict=True)
    require(haar.is_relative_to(site), "HAAR_FUERA_DEL_WHEEL")
    for name, sha256 in HAAR.items():
        path = haar / name
        pinned(path, sha256, 1024**2)
        require(not cv2.CascadeClassifier(str(path)).empty(), "HAAR_NO_CARGA")

    blank = np.zeros((320, 320, 3), dtype=np.uint8)
    net = cv2.dnn.readNetFromONNX(str(yunet))
    require(set(net.getUnconnectedOutLayersNames()) == set(HEADS), "YUNET_SALIDAS")
    net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
    net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
    net.setInput(cv2.dnn.blobFromImage(blank))
    heads = net.forward(HEADS)
    require(len(heads) == len(HEADS), "YUNET_SIN_INFERENCIA")
    for name, value in zip(HEADS, heads, strict=True):
        field, stride = name.rsplit("_", 1)
        cells = (320 // int(stride)) ** 2
        require(value.shape == (1, cells, {"bbox": 4, "kps": 10}.get(field, 1)), "YUNET_FORMA")
        require(np.isfinite(value).all(), "YUNET_SALIDA_NO_FINITA")
    detector = cv2.FaceDetectorYN.create(str(yunet), "", (320, 320))
    status, detections = detector.detect(blank)
    require(status == 1 and (detections is None or not detections.size), "YUNET_IMAGEN_VACIA")
    return {"yunet_heads": len(heads), "haar_cascades": len(HAAR), "synthetic_blank_faces": 0}


def motion_probe(cv2, np):
    original = np.zeros((128, 128), dtype=np.uint8)
    for x, y in ((15, 12), (48, 29), (69, 57), (22, 91), (88, 94)):
        original[y : y + 11, x : x + 11] = 255
    shifted = np.roll(original, (2, 3), axis=(0, 1))
    points = cv2.goodFeaturesToTrack(original, 24, 0.01, 5)
    require(points is not None and len(points) >= 8, "FLUJO_PUNTOS")
    moved, status, _ = cv2.calcOpticalFlowPyrLK(original, shifted, points, None)
    valid = status.reshape(-1) == 1
    require(valid.sum() >= 8, "FLUJO_SEGUIMIENTO")
    matrix, inliers = cv2.estimateAffinePartial2D(points[valid], moved[valid])
    require(matrix is not None and inliers.sum() >= 8, "CALIB3D_AFIN")
    require(np.allclose(matrix[:, 2], (3, 2), atol=0.35), "CALIB3D_TRASLACION")
    return {"tracked_points": int(valid.sum()), "translation": matrix[:, 2].tolist()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--video-sha256", required=True)
    parser.add_argument("--yunet", type=Path, required=True)
    args = parser.parse_args()
    try:
        require(sys.platform == "linux" and platform.machine() == "x86_64", "RUNTIME_LINUX_AMD64")
        require(sys.version_info[:2] == (3, 12), "RUNTIME_CPYTHON312")
        require(args.site.is_absolute() and not args.site.is_symlink(), "SITE_PATH")
        site = args.site.resolve(strict=True)
        require(site.is_dir(), "SITE_PATH")
        pinned(args.video, args.video_sha256, 16 * 1024**2)
        pinned(args.yunet, YUNET_SHA256, 512 * 1024)
        sys.path.insert(0, str(site))
        import cv2
        import numpy as np

        for module in (cv2, np):
            require(Path(module.__file__).resolve().is_relative_to(site), "IMPORT_FUERA_DEL_SITE")
        require(cv2.__version__ == "4.13.0" and np.__version__ == "2.4.4", "VERSION_RUNTIME")
        build = cv2.getBuildInformation()
        require(re.search(r"(?m)^\s*FFMPEG:\s+YES\b", build), "BUILD_FFMPEG")
        require(re.search(r"(?m)^\s*Protobuf:.*\b29\.5\.0\b", build), "BUILD_PROTOBUF_EXTERNO")
        result = {
            "success": True,
            "opencv": cv2.__version__,
            "numpy": np.__version__,
            "video": video_probe(cv2, np, args.video),
            "facial": facial_probe(cv2, np, args.yunet, site),
            "motion": motion_probe(cv2, np),
        }
    except (OSError, ImportError, ValueError) as error:
        print(json.dumps({"success": False, "error": str(error)[:240]}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
