"""Selecciona y verifica un umbral facial sintético MPFB con el puente Immich."""

import argparse
import io
import json
import math
import re
import stat
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from immich_face_mpfb_corpus import MPFB_REVISION, MPFB_VERSION, PACK, SEEDS, VIEWS
from immich_face_qub_probe import (
    BASE_URLS,
    DISTANCES,
    MODEL_ID,
    SCORES,
    VERSION,
    bridge,
    cosine_distance,
    entries_for,
    health_proof,
    require,
    sha,
)
from immich_service import validate_embedding
from PIL import Image, ImageDraw
from PIL import __version__ as PILLOW_VERSION

SIZE = 512
CONDITIONS = tuple(view[0] for view in VIEWS)
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_PNG = 4 * 1024**2
MAX_MANIFEST = 1024**2


@dataclass(frozen=True)
class Fixture:
    name: str
    identity: str | None
    split: str
    condition: str
    digest: str
    image: bytes


def read_regular(path, limit):
    entry = path.lstat()
    require(stat.S_ISREG(entry.st_mode) and 0 < entry.st_size <= limit, "ARCHIVO_TIPO_TAMANO")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) == entry.st_size, "ARCHIVO_CAMBIO")
    return data


def load_corpus(path, expected_sha):
    raw = read_regular(path, MAX_MANIFEST)
    require(sha(raw) == expected_sha, "MANIFIESTO_SHA")
    doc = json.loads(raw)
    require(
        doc["schema"] == 1
        and doc["synthetic"] is True
        and doc["asset_license"] == "CC0-1.0"
        and doc["asset_pack"] == PACK
        and SHA256.fullmatch(doc["asset_pack_sha256"])
        and doc["mpfb_version"] == MPFB_VERSION
        and doc["mpfb_reference_commit"] == MPFB_REVISION,
        "MANIFIESTO_PROCEDENCIA",
    )
    ids = tuple(f"mh-{index:03d}" for index in range(1, 17))
    declared = doc["identities"]
    require(
        [(row["identity"], row["seed"]) for row in declared] == list(zip(ids, SEEDS, strict=True)),
        "CORPUS_IDENTIDADES",
    )
    meshes = {row["mesh_sha256"] for row in declared}
    require(len(meshes) == 16 and all(SHA256.fullmatch(value) for value in meshes), "CORPUS_MALLAS")
    root = path.parent.resolve(strict=True)
    images = root / "images"
    require(stat.S_ISDIR(images.lstat().st_mode) and images.resolve() == images, "CORPUS_RAIZ")
    rows = doc["fixtures"]
    require(len(rows) == 96, "CORPUS_TAMANO")
    by_name = {row["id"]: row for row in rows}
    expected = {f"{identity}_{condition}" for identity in ids for condition in CONDITIONS}
    require(by_name.keys() == expected, "CORPUS_LISTA")
    fixtures, digests = [], set()
    for index, identity in enumerate(ids):
        split = "calibration" if index < 8 else "validation"
        for condition in CONDITIONS:
            name = f"{identity}_{condition}"
            row = by_name[name]
            filename = f"{name}.png"
            require(row["split"] == split and row["identity"] == identity, "CORPUS_DIVISION")
            require(
                row["condition"] == condition
                and row["path"] == f"images/{filename}"
                and (row["width"], row["height"], row["expected_faces"]) == (SIZE, SIZE, 1),
                "CORPUS_FICHA",
            )
            digest = row["prepared_sha256"]
            require(digest == row["original_sha256"] and digest not in digests, "CORPUS_HASHES")
            image = read_regular(images / filename, MAX_PNG)
            require(sha(image) == digest, "CORPUS_PNG_SHA")
            with Image.open(io.BytesIO(image), formats=("PNG",)) as png:
                require(
                    png.size == (SIZE, SIZE) and png.mode == "RGB" and png.n_frames == 1,
                    "CORPUS_PNG_FORMATO",
                )
                png.verify()
            fixtures.append(Fixture(name, identity, split, condition, digest, image))
            digests.add(digest)
    return fixtures


def negative_controls():
    solid = Image.new("RGB", (SIZE, SIZE), (118, 118, 118))
    blocks = Image.new("RGB", (SIZE, SIZE), (203, 109, 56))
    ImageDraw.Draw(blocks).rectangle((148, 57, 437, 384), fill=(32, 87, 123))
    gradient = Image.linear_gradient("L").resize((SIZE, SIZE)).convert("RGB")
    for name, image in (("fondo", solid), ("bloques", blocks), ("gradiente", gradient)):
        out = io.BytesIO()
        image.save(out, format="PNG")
        raw = out.getvalue()
        yield Fixture(f"neg-{name}", None, "control", name, sha(raw), raw)


def float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def faces_at(faces, min_score):
    # NumPy compares the detector's float32 confidences against a float32 scalar.
    floor = float32(min_score)
    return [face for face in faces if face[0] >= floor]


def face_result(response, min_score):
    require(
        response.get("imageHeight") == SIZE and response.get("imageWidth") == SIZE,
        "RESPUESTA_DIMENSIONES",
    )
    faces = response.get("facial-recognition")
    require(isinstance(faces, list), "RESPUESTA_ROSTROS")
    results = []
    for face in faces:
        box = face["boundingBox"]
        require(isinstance(box, dict) and set(box) == {"x1", "y1", "x2", "y2"}, "RESPUESTA_CAJA")
        coordinates = (box["x1"], box["y1"], box["x2"], box["y2"])
        x1, y1, x2, y2 = coordinates
        require(
            all(type(value) is int for value in coordinates)
            and 0 <= x1 < x2 <= SIZE
            and 0 <= y1 < y2 <= SIZE,
            "RESPUESTA_CAJA",
        )
        score = face["score"]
        require(type(score) in (float, int) and float32(min_score) <= score <= 1, "RESPUESTA_SCORE")
        embedding = face["embedding"]
        try:
            validate_embedding(embedding)
        except ValueError:
            raise ValueError("RESPUESTA_EMBEDDING") from None
        vector = tuple(json.loads(embedding))
        require(abs(math.sqrt(math.fsum(x * x for x in vector)) - 1) <= 1e-5, "RESPUESTA_NORMA")
        results.append((score, vector, coordinates))
    return results


def infer(fixtures, request, min_score):
    entries = entries_for(min_score)
    outputs, proof = {}, None
    for fixture in fixtures:
        faces = face_result(request("/predict", entries=entries, image=fixture.image), min_score)
        if faces and fixture.identity is not None:
            proof = health_proof(request("/health"), facial=True)
        outputs[fixture.name] = faces
    health_proof(request("/health"))
    return outputs, proof


def percentiles(sorted_distances):
    if not sorted_distances:
        return None
    return {
        str(q): sorted_distances[round((len(sorted_distances) - 1) * q / 100)]
        for q in (5, 25, 50, 75, 95)
    }


def pair_metrics(records, thresholds):
    counts = Counter()
    distances = {"genuine": [], "impostor": []}
    for (left_id, left), (right_id, right) in combinations(records, 2):
        label = "genuine" if left_id == right_id else "impostor"
        counts[label] += 1
        if left is not None and right is not None:
            distances[label].append(cosine_distance(left, right))
    stats = {}
    for label, values in distances.items():
        values.sort()
        stats[label] = {
            "total": counts[label],
            "evaluated": len(values),
            "unavailable": counts[label] - len(values),
            "minimum": values[0] if values else None,
            "maximum": values[-1] if values else None,
            "percentiles_nearest": percentiles(values),
        }
    genuine, impostor = distances["genuine"], distances["impostor"]
    stats["thresholds"] = []
    for threshold in thresholds:
        false_matches = sum(distance <= threshold for distance in impostor)
        false_rejects = sum(distance > threshold for distance in genuine)
        stats["thresholds"].append(
            {
                "maxDistance": threshold,
                "false_matches": false_matches,
                "false_rejects": false_rejects,
                "true_matches": len(genuine) - false_rejects,
                "false_match_rate": false_matches / len(impostor) if impostor else None,
                "false_reject_rate": false_rejects / len(genuine) if genuine else None,
            }
        )
    return stats


def summarize(fixtures, outputs, controls, control_outputs, min_score, thresholds):
    singles, by_condition, records, rows = {}, {}, [], []
    for fixture in fixtures:
        faces = faces_at(outputs[fixture.name], min_score)
        counts = by_condition.setdefault(fixture.condition, Counter(zero=0, one=0, multiple=0))
        counts[("zero", "one", "multiple")[min(len(faces), 2)]] += 1
        singles[fixture.identity] = singles.get(fixture.identity, 0) + (len(faces) == 1)
        records.append((fixture.identity, faces[0][1] if len(faces) == 1 else None))
        rows.append({"id": fixture.name, "png_sha256": fixture.digest, "faces": len(faces)})
    negatives = [
        {
            "id": fixture.name,
            "png_sha256": fixture.digest,
            "faces": len(faces_at(control_outputs[fixture.name], min_score)),
        }
        for fixture in controls
    ]
    return {
        "minScore": min_score,
        "identities": len(singles),
        "images": rows,
        "by_condition": {
            name: {**counts, "single_rate": counts["one"] / sum(counts.values())}
            for name, counts in by_condition.items()
        },
        "singles_per_identity": singles,
        "negatives": negatives,
        "negative_images_with_faces": sum(row["faces"] > 0 for row in negatives),
        "pairs": pair_metrics(records, thresholds),
    }


def candidate_for(calibration):
    candidates = []
    for run in calibration:
        if min(run["singles_per_identity"].values()) < 3 or run["negative_images_with_faces"]:
            continue
        for row in run["pairs"]["thresholds"]:
            order = (
                row["false_matches"],
                -row["true_matches"],
                row["maxDistance"],
                run["minScore"],
            )
            candidates.append(order)
    if not candidates:
        return None
    _, _, distance, score = min(candidates)
    return {"minScore": score, "maxDistance": distance, "minFaces": 3}


def check_equivalence(fixtures, low_outputs, request):
    high = SCORES[-1]
    # Usa el positivo de mayor confianza y un caso donde el filtro retira caras, si existe.
    strong = max(
        fixtures,
        key=lambda fixture: max((face[0] for face in low_outputs[fixture.name]), default=0),
    )
    others = [fixture for fixture in fixtures if fixture.name != strong.name]
    weaker = next(
        (
            fixture
            for fixture in others
            if len(faces_at(low_outputs[fixture.name], high)) < len(low_outputs[fixture.name])
        ),
        others[0],
    )
    subset = (strong, weaker)
    direct, proof = infer(subset, request, high)
    for fixture in subset:
        filtered = faces_at(low_outputs[fixture.name], high)
        measured = direct[fixture.name]
        require(len(filtered) == len(measured), "UMBRAL_EQUIVALENCIA")
        for (low_score, low_vector, low_box), (score, vector, box) in zip(
            filtered, measured, strict=True
        ):
            require(
                low_box == box
                and abs(low_score - score) <= 1e-6
                and cosine_distance(low_vector, vector) <= 1e-6,
                "UMBRAL_EQUIVALENCIA",
            )
    return {
        "direct_minScore": high,
        "images": [fixture.name for fixture in subset],
        "direct_faces": sum(len(faces) for faces in direct.values()),
        "cuda_proof": proof,
    }


def evaluate(fixtures, request):
    health_proof(request("/health"))
    calibration = [fixture for fixture in fixtures if fixture.split == "calibration"]
    validation = [fixture for fixture in fixtures if fixture.split == "validation"]
    controls = list(negative_controls())
    low, proof = infer([*calibration, *controls], request, SCORES[0])
    require(proof is not None, "CUDA_POSITIVOS_CALIBRACION")
    equivalence = check_equivalence(calibration, low, request)
    results = [summarize(calibration, low, controls, low, score, DISTANCES) for score in SCORES]
    del low
    candidate = candidate_for(results)
    report = {
        "model": MODEL_ID,
        "bridge_version": VERSION,
        "scope": "synthetic_mpfb",
        "pillow": PILLOW_VERSION,
        "calibration_cuda_proof": proof,
        "filter_equivalence": equivalence,
        "calibration": results,
        "candidate": candidate,
        "validation": None,
        "synthetic_candidate_passed": False,
        "calibrated": False,
    }
    if candidate is None:
        return report
    score = candidate["minScore"]
    fixed, proof = infer(validation, request, score)
    require(proof is not None, "CUDA_POSITIVOS_VALIDACION")
    # Los controles compartidos ya se ejecutaron, sus recuentos no son muestras de holdout.
    controls_at_score = next(
        result["negatives"] for result in results if result["minScore"] == score
    )
    outcome = summarize(validation, fixed, (), {}, score, (candidate["maxDistance"],))
    outcome["negatives"] = controls_at_score
    outcome["negative_images_with_faces"] = sum(row["faces"] > 0 for row in controls_at_score)
    outcome["negative_controls_shared"] = True
    outcome["cuda_proof"] = proof
    report["validation"] = outcome
    report["synthetic_candidate_passed"] = (
        min(outcome["singles_per_identity"].values()) >= candidate["minFaces"]
        and outcome["pairs"]["thresholds"][0]["false_matches"] == 0
        and outcome["negative_images_with_faces"] == 0
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", required=True, type=Path, help="fixtures.json del productor MPFB"
    )
    parser.add_argument("--manifest-sha256", required=True, help="SHA-256 fijado antes de inferir")
    parser.add_argument(
        "--bearer-file", type=Path, help="Archivo privado con sólo el Bearer, o - para stdin"
    )
    parser.add_argument("--base-url", choices=BASE_URLS, default=BASE_URLS[0])
    parser.add_argument(
        "--fixtures-only", action="store_true", help="Valida el corpus sin llamar al puente"
    )
    args = parser.parse_args()
    try:
        require(SHA256.fullmatch(args.manifest_sha256), "MANIFIESTO_SHA_FORMATO")
        fixtures = load_corpus(args.manifest, args.manifest_sha256)
        if args.fixtures_only:
            report = {
                "fixtures": len(fixtures),
                "negative_controls": {
                    fixture.name: fixture.digest for fixture in negative_controls()
                },
            }
        else:
            require(
                args.bearer_file is not None and args.bearer_file.name != ".env", "BEARER_ARCHIVO"
            )
            if args.bearer_file == Path("-"):
                token = sys.stdin.buffer.read(4098)
            else:
                token = read_regular(args.bearer_file, 4098)
            started = time.monotonic()
            report = evaluate(fixtures, bridge(args.base_url, token.rstrip(b"\r\n")))
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["manifest_sha256"] = args.manifest_sha256
    except (OSError, ValueError, TypeError, KeyError, AttributeError, SyntaxError) as error:
        code = str(error) if type(error) is ValueError else "SONDA_MPFB_FALLIDA"
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "SONDA_MPFB_FALLIDA"
        print(json.dumps({"synthetic_candidate_passed": False, "error": code}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if args.fixtures_only or report["synthetic_candidate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
