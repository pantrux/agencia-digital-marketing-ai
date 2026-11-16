"""Mide pares sintéticos de la muestra QUB por el puente CUDA de Immich."""

import argparse
import hashlib
import io
import json
import math
import re
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from itertools import combinations
from pathlib import Path

from face_models import MODEL_ID
from immich_portability import VERSION
from immich_service import multipart, validate_embedding
from PIL import Image
from PIL import __version__ as PILLOW_VERSION

ARCHIVE_BYTES = 7_849_501
ARCHIVE_SHA256 = "643d48e39ddc36e4a22097b55f33fc22747594eb4c760ce141ddefb79835dc45"
DOI = "10.17034/c362100e-215f-4099-bd2e-ee2c98cf1b1d"
ROOT = "syn-Face (Representative Sample)"
IDENTITIES = ("00001", "00006", "00007")
CONDITIONS = (
    "A_270_90_128",
    "A_240_90_128",
    "A_300_90_128",
    "B_270_90_128",
    "C_270_75_128",
    "D_270_90_128",
)
AXES = (("A", "B", "C", "D"), (0, 180, 210, 240, 270, 300, 330), (45, 60, 75, 90))
DISTANCES = tuple(i / 1000 for i in range(300, 701, 25))
SCORES = (0.60, 0.65, 0.70, 0.75, 0.80)
BASE_URLS = ("http://127.0.0.1:3003", "http://192.168.0.6:3003")
TASKS = ("facial-recognition/detection", "facial-recognition/recognition")
MAX_RESPONSE = 1024**2


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def archive_name(identity, condition):
    return f"{ROOT}/{identity}/{identity}_{condition}.tiff"


def expected_names():
    lighting, azimuths, elevations = AXES
    return {
        archive_name(identity, f"{light}_{azimuth}_{elevation}_{size}")
        for identity in IDENTITIES
        for light in lighting
        for azimuth in azimuths
        for elevation in elevations
        for size in (16, 32, 64, 128)
    }


def load_fixtures(raw):
    require(len(raw) == ARCHIVE_BYTES and sha(raw) == ARCHIVE_SHA256, "ZIP_SHA256")
    fixtures = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [info.filename for info in archive.infolist() if not info.is_dir()]
        require(len(members) == 1344 and set(members) == expected_names(), "ZIP_INVENTARIO")
        for identity in IDENTITIES:
            for condition in CONDITIONS:
                name = archive_name(identity, condition)
                tiff = archive.read(name)
                with Image.open(io.BytesIO(tiff), formats=("TIFF",)) as source:
                    require(source.size == (128, 128) and source.n_frames == 1, "TIFF_DIMENSIONES")
                    converted = io.BytesIO()
                    rgb = source.convert("RGB")
                    rgb.info.clear()
                    rgb.save(converted, format="PNG")
                fixtures.append((identity, condition, sha(tiff), converted.getvalue()))
    require(len({sha(png) for _, _, _, png in fixtures}) == len(fixtures), "PNG_DUPLICADO")
    return fixtures


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("REDIRECCION_RECHAZADA")


def bridge(base_url, token):
    require(base_url in BASE_URLS, "PUENTE_URL")
    require(re.fullmatch(rb"[A-Za-z0-9._~+/=-]{16,4096}", token), "BEARER_FORMATO")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(path, *, entries=None, image=None):
        headers = {"Authorization": "Bearer " + token.decode("ascii"), "Accept": "application/json"}
        body = None
        if entries is not None:
            body, headers["Content-Type"] = multipart(entries, image=image)
        request = urllib.request.Request(base_url + path, body, headers)
        with opener.open(request, timeout=45) as response:
            require(response.status == 200, "PUENTE_HTTP")
            raw = response.read(MAX_RESPONSE + 1)
        require(len(raw) <= MAX_RESPONSE, "PUENTE_RESPUESTA_TAMANO")
        return json.loads(raw)

    return request


def health_proof(health, *, facial=False):
    require(
        health.get("service") == "immich-ml"
        and health.get("version") == VERSION
        and health.get("ready") is True
        and health.get("cuda_available") is True
        and health.get("cuda_verified") is True
        and health.get("backend_failed") is False
        and health.get("face_model") == MODEL_ID
        and health.get("commercial_face_model") is True
        and health.get("models_resident") == 0,
        "CUDA_SALUD",
    )
    if not facial:
        return {}
    age = health.get("proof_age_seconds")
    require(type(age) in (int, float) and math.isfinite(age) and 0 <= age <= 15, "CUDA_VIGENCIA")
    profiles = health.get("inference_profiles", {})
    nodes = {task: profiles.get(task, {}).get("cuda_nodes") for task in TASKS}
    require(all(type(count) is int and count > 0 for count in nodes.values()), "CUDA_PERFILES")
    return {"cuda_nodes": nodes, "proof_age_seconds": age}


def face_result(response, min_score):
    require(
        response.get("imageHeight") == 128 and response.get("imageWidth") == 128,
        "RESPUESTA_DIMENSIONES",
    )
    faces = response.get("facial-recognition")
    require(isinstance(faces, list), "RESPUESTA_ROSTROS")
    vectors, scores = [], []
    for face in faces:
        box = face.get("boundingBox")
        require(
            isinstance(box, dict)
            and set(box) == {"x1", "y1", "x2", "y2"}
            and all(type(coord) is int for coord in box.values())
            and 0 <= box["x1"] < box["x2"] <= 128
            and 0 <= box["y1"] < box["y2"] <= 128,
            "RESPUESTA_CAJA",
        )
        score = face.get("score")
        require(type(score) in (int, float) and min_score - 1e-6 <= score <= 1, "RESPUESTA_SCORE")
        embedding = face.get("embedding")
        try:
            validate_embedding(embedding)
        except ValueError:
            raise ValueError("RESPUESTA_EMBEDDING") from None
        vector = tuple(json.loads(embedding))
        require(abs(math.sqrt(math.fsum(x * x for x in vector)) - 1) <= 1e-5, "RESPUESTA_NORMA")
        scores.append(score)
        vectors.append(vector)
    return scores, vectors


def entries_for(min_score):
    return {
        "facial-recognition": {
            "detection": {"modelName": MODEL_ID, "options": {"minScore": min_score}},
            "recognition": {"modelName": MODEL_ID},
        }
    }


def cosine_distance(left, right):
    dot = math.fsum(x * y for x, y in zip(left, right, strict=True))
    norm = math.sqrt(math.fsum(x * x for x in left) * math.fsum(y * y for y in right))
    return 1 - max(-1.0, min(1.0, dot / norm))


def pair_summary(records):
    counts = Counter()
    distances = {"genuine": [], "impostor": []}
    for (left_id, left), (right_id, right) in combinations(records, 2):
        label = "genuine" if left_id == right_id else "impostor"
        counts[label] += 1
        if left is not None and right is not None:
            distances[label].append(cosine_distance(left, right))

    def summary(label):
        values = distances[label]
        return {
            "total": counts[label],
            "evaluated": len(values),
            "unavailable": counts[label] - len(values),
            "minimum": min(values, default=None),
            "maximum": max(values, default=None),
        }

    thresholds = []
    genuine, impostor = distances["genuine"], distances["impostor"]
    for threshold in DISTANCES:
        false_rejects = sum(distance > threshold for distance in genuine)
        false_matches = sum(distance <= threshold for distance in impostor)
        thresholds.append(
            {
                "maxDistance": threshold,
                "false_rejects": false_rejects,
                "false_matches": false_matches,
                "false_reject_rate": false_rejects / len(genuine) if genuine else None,
                "false_match_rate": false_matches / len(impostor) if impostor else None,
            }
        )
    return {
        "genuine": summary("genuine"),
        "impostor": summary("impostor"),
        "thresholds": thresholds,
    }


def measure(fixtures, request, min_scores):
    health_proof(request("/health"))
    results = []
    for min_score in min_scores:
        entries = entries_for(min_score)
        rows, records, proof = [], [], None
        for identity, condition, tiff_sha, image in fixtures:
            scores, vectors = face_result(
                request("/predict", entries=entries, image=image), min_score
            )
            if vectors:
                proof = health_proof(request("/health"), facial=True)
            rows.append(
                {
                    "identity": identity,
                    "condition": condition,
                    "faces": len(scores),
                    "scores": scores,
                    "tiff_sha256": tiff_sha,
                    "png_sha256": sha(image),
                }
            )
            records.append((identity, vectors[0] if len(vectors) == 1 else None))
        health_proof(request("/health"))
        pairs = pair_summary(records)
        results.append(
            {
                "minScore": min_score,
                "images": rows,
                "pairs": pairs,
                "singles_per_identity": dict(
                    Counter(identity for identity, vector in records if vector is not None)
                ),
                "cuda_proof": proof,
            }
        )
    verified = all(
        result["cuda_proof"] is not None
        and result["pairs"]["genuine"]["evaluated"] > 0
        and result["pairs"]["impostor"]["evaluated"] > 0
        for result in results
    )
    return {
        "doi": DOI,
        "license": "CC-BY-4.0",
        "archive_sha256": ARCHIVE_SHA256,
        "pillow": PILLOW_VERSION,
        "model": MODEL_ID,
        "bridge_version": VERSION,
        "identities": len(IDENTITIES),
        "verified": verified,
        "calibrated": False,
        "runs": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path, help="ZIP acreditado. Usa el valor - para leerlo por stdin."
    )
    parser.add_argument(
        "--bearer-file", type=Path, help="Archivo privado con sólo el Bearer, o - para stdin."
    )
    parser.add_argument("--base-url", choices=BASE_URLS, default=BASE_URLS[0])
    parser.add_argument("--min-score", action="append", type=float, choices=SCORES)
    parser.add_argument("--fixtures-only", action="store_true")
    args = parser.parse_args()
    try:
        require(args.archive is not None, "ZIP_REQUERIDO")
        if args.archive == Path("-"):
            raw = sys.stdin.buffer.read(ARCHIVE_BYTES + 1)
        else:
            with args.archive.open("rb") as source:
                raw = source.read(ARCHIVE_BYTES + 1)
        fixtures = load_fixtures(raw)
        if args.fixtures_only:
            report = {
                "archive_sha256": ARCHIVE_SHA256,
                "identities": list(IDENTITIES),
                "conditions": list(CONDITIONS),
                "fixtures": len(fixtures),
                "pillow": PILLOW_VERSION,
            }
        else:
            require(
                args.bearer_file is not None and args.bearer_file.name != ".env", "BEARER_ARCHIVO"
            )
            if args.bearer_file == Path("-"):
                require(args.archive != Path("-"), "ENTRADA_DUPLICADA")
                token = sys.stdin.buffer.read(4098).rstrip(b"\r\n")
            else:
                with args.bearer_file.open("rb") as source:
                    token = source.read(4098).rstrip(b"\r\n")
            started = time.monotonic()
            min_scores = tuple(dict.fromkeys(args.min_score or (0.70,)))
            report = measure(fixtures, bridge(args.base_url, token), min_scores)
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    except (OSError, ValueError, TypeError, AttributeError, zipfile.BadZipFile) as error:
        code = str(error) if type(error) is ValueError else "SONDA_FALLIDA"
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "SONDA_FALLIDA"
        print(json.dumps({"verified": False, "error": code}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report.get("verified", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
