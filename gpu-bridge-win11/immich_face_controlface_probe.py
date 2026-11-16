"""Mide detección y vecindad facial en una partición sintética ControlFace10K."""

import argparse
import io
import json
import math
import re
import stat
import sys
import time
import urllib.error
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from immich_face_controlface_corpus import (
    AGES,
    CENTRAL_SHA,
    COHORT_PLAN_SHAS,
    COHORTS,
    DATASET,
    GENDERS,
    HOLDOUT_PLAN_SHA,
    MAX_MEMBER,
    RACES,
    REVISION,
    THIRD_COHORT,
)
from immich_face_controlface_corpus import (
    HOLDOUT_COHORT as HOLDOUT_COHORT,
)
from immich_face_mpfb_probe import faces_at, float32, pair_metrics, read_regular
from immich_face_qub_probe import (
    BASE_URLS,
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
from PIL import Image

CONDITIONS = ("o3", "o4", "o5")
THRESHOLDS = (0.30, 0.305, 0.31, *(step / 1000 for step in range(325, 576, 25)))
SHA256 = re.compile(r"[a-f0-9]{64}")
MAX_MANIFEST = 256 * 1024
HISTORICAL_MANIFEST_SHA = "eb5209345dcdad607b5beb58d0883ef999ab5b7c75430c173910af334fcda07c"
SECOND_MANIFEST_SHA = "8d5a44eb281c3afec93ce9beee28b3573eeb4415f5a4dee3fedc84d0a990c8c2"


@dataclass(frozen=True)
class Fixture:
    name: str
    identity: str
    condition: str
    digest: str
    width: int
    height: int
    image: bytes


def expected_rows(cohort=None):
    identity = 0
    _, prefix = COHORTS.get(cohort, (0, "cf"))
    for r, race in enumerate(RACES):
        for g, gender in enumerate(GENDERS):
            for a, age in enumerate(AGES):
                identity += 1
                split = "calibration" if (r + g + a) % 2 == 0 else "validation"
                label = f"{prefix}-{identity:02d}"
                if cohort in COHORTS:
                    split = "holdout"
                for condition in CONDITIONS:
                    yield f"{label}-{condition}", label, condition, split, (race, gender, age)


def load_corpus(
    path,
    expected_sha,
    split,
    *,
    cohort=None,
    plan_sha256=None,
    historical_manifest=None,
    second_manifest=None,
):
    holdout = cohort in COHORTS
    third = cohort == THIRD_COHORT
    require(
        (
            holdout
            and split == "holdout"
            and plan_sha256 == (COHORT_PLAN_SHAS[cohort] if third else HOLDOUT_PLAN_SHA)
            and historical_manifest is not None
            and (second_manifest is not None) == third
        )
        or (
            cohort is None
            and split in {"calibration", "validation"}
            and plan_sha256 is None
            and historical_manifest is None
            and second_manifest is None
        ),
        "CORPUS_DIVISION",
    )
    raw = read_regular(path, MAX_MANIFEST)
    require(sha(raw) == expected_sha, "MANIFIESTO_SHA")
    doc = json.loads(raw)
    require(
        doc["schema"] == 1
        and doc["synthetic"] is True
        and doc["dataset"] == DATASET
        and doc["revision"] == REVISION
        and doc["central_sha256"] == CENTRAL_SHA
        and doc["license"] == "CC BY 4.0"
        and doc.get("cohort") == cohort
        and doc.get("plan_sha256") == plan_sha256,
        "MANIFIESTO_PROCEDENCIA",
    )
    rows = doc["fixtures"]
    require(isinstance(rows, list) and len(rows) == 72, "CORPUS_TAMANO")
    by_name = {row["id"]: row for row in rows}
    require(len(by_name) == 72, "CORPUS_DUPLICADO")
    parent = path.parent.resolve(strict=True)
    images = parent / "images"
    require(stat.S_ISDIR(images.lstat().st_mode) and images.resolve() == images, "CORPUS_RAIZ")
    selected = []
    hashes = set()
    sources = defaultdict(set)
    for name, identity, condition, expected_split, stratum in expected_rows(cohort):
        row = by_name[name]
        digest = row["prepared_sha256"]
        width, height = row["width"], row["height"]
        origin = row["source_identity"]
        require(
            row["identity"] == identity
            and row["split"] == expected_split
            and row.get("condition", name.rsplit("-", 1)[1]) == condition
            and row["path"] == f"images/{name}.png"
            and row["expected_faces"] == 1
            and type(width) is int
            and type(height) is int
            and 32 <= width <= 4096
            and 32 <= height <= 4096
            and SHA256.fullmatch(digest)
            and digest not in hashes
            and re.fullmatch(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", origin)
            and row["source_member"].startswith(
                "ControlFace10k/" + "/".join(stratum) + "/identity-" + origin + "/"
            )
            and f"_{condition}_" in row["source_member"].rsplit("/", 1)[1],
            "CORPUS_FICHA",
        )
        hashes.add(digest)
        sources[identity].add(origin)
        if expected_split == split:
            selected.append((name, identity, condition, digest, width, height))
    source_ids = set().union(*sources.values())
    require(
        len(selected) == (72 if holdout else 36)
        and len(source_ids) == 24
        and all(len(ids) == 1 for ids in sources.values()),
        "CORPUS_IDENTIDADES",
    )
    if holdout:
        history = [(historical_manifest, HISTORICAL_MANIFEST_SHA)]
        if third:
            history.append((second_manifest, SECOND_MANIFEST_SHA))
        for previous_manifest, previous_pin in history:
            historical = read_regular(previous_manifest, MAX_MANIFEST)
            require(sha(historical) == previous_pin, "CORPUS_HISTORICO_PIN")
            old_rows = json.loads(historical)["fixtures"]
            old_ids = {row["source_identity"] for row in old_rows}
            old_hashes = {row["prepared_sha256"] for row in old_rows}
            require(not (source_ids & old_ids or hashes & old_hashes), "CORPUS_HOLDOUT_SOLAPADO")
    fixtures = []
    for name, identity, condition, digest, width, height in selected:
        data = read_regular(images / f"{name}.png", MAX_MEMBER)
        require(sha(data) == digest, "CORPUS_PNG_SHA")
        with Image.open(io.BytesIO(data), formats=("PNG",)) as image:
            require(image.size == (width, height) and image.n_frames == 1, "CORPUS_PNG_FORMATO")
            image.verify()
        fixtures.append(Fixture(name, identity, condition, digest, width, height, data))
    return fixtures


def response_faces(response, fixture, min_score):
    require(
        (response.get("imageWidth"), response.get("imageHeight"))
        == (fixture.width, fixture.height),
        "RESPUESTA_DIMENSIONES",
    )
    faces = response.get("facial-recognition")
    require(isinstance(faces, list), "RESPUESTA_ROSTROS")
    results = []
    for face in faces:
        box = face["boundingBox"]
        require(isinstance(box, dict) and set(box) == {"x1", "y1", "x2", "y2"}, "RESPUESTA_CAJA")
        x1, y1, x2, y2 = (box[key] for key in ("x1", "y1", "x2", "y2"))
        require(
            all(type(value) is int for value in box.values())
            and 0 <= x1 < x2 <= fixture.width
            and 0 <= y1 < y2 <= fixture.height,
            "RESPUESTA_CAJA",
        )
        score = face["score"]
        require(type(score) in (int, float) and float32(min_score) <= score <= 1, "RESPUESTA_SCORE")
        embedding = face["embedding"]
        validate_embedding(embedding)
        vector = tuple(json.loads(embedding))
        require(abs(math.sqrt(math.fsum(x * x for x in vector)) - 1) <= 1e-5, "RESPUESTA_NORMA")
        results.append((score, vector))
    return results


def infer(fixtures, request, min_score):
    health_proof(request("/health"))
    entries = entries_for(min_score)
    outputs, retries, proof = {}, [], None
    for fixture in fixtures:
        try:
            result = request("/predict", entries=entries, image=fixture.image)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code != 503:
                raise
            retries.append({"id": fixture.name, "http_status": code})
            time.sleep(1)
            try:
                result = request("/predict", entries=entries, image=fixture.image)
            except urllib.error.HTTPError as retry_error:
                retry_error.close()
                raise ValueError(
                    f"PREDICT_RETRY_EXHAUSTED_{fixture.name.upper().replace('-', '_')}"
                ) from None
        faces = response_faces(result, fixture, min_score)
        if faces:
            proof = health_proof(request("/health"), facial=True)
        outputs[fixture.name] = faces
    health_proof(request("/health"))
    return outputs, retries, proof


def neighbor_graph(records, thresholds):
    identities = {name: identity for name, identity, _vector in records}
    evaluable = [(name, vector) for name, _identity, vector in records if vector is not None]
    distances = [
        (a, b, cosine_distance(left, right)) for (a, left), (b, right) in combinations(evaluable, 2)
    ]
    summary = []
    for threshold in thresholds:
        neighbors = {name: set() for name, _vector in evaluable}
        false_edges = 0
        for left, right, distance in distances:
            if distance <= threshold:
                neighbors[left].add(right)
                neighbors[right].add(left)
                false_edges += identities[left] != identities[right]
        degrees = {}
        for name, links in neighbors.items():
            genuine = sum(identities[name] == identities[other] for other in links)
            degrees[name] = {"genuine": genuine, "impostor": len(links) - genuine}
        components, remaining = [], set(neighbors)
        while remaining:
            component = {min(remaining)}
            frontier = list(component)
            while frontier:
                additional = neighbors[frontier.pop()] - component
                component.update(additional)
                frontier.extend(additional)
            remaining -= component
            components.append(
                {
                    "fixtures": sorted(component),
                    "identities": sorted({identities[name] for name in component}),
                }
            )
        complete = [
            row["identities"][0]
            for row in components
            if len(row["identities"]) == 1 and len(row["fixtures"]) == 3
        ]
        summary.append(
            {
                "maxDistance": threshold,
                "impostor_edges": false_edges,
                "genuine_edges": sum(row["genuine"] for row in degrees.values()) // 2,
                "components": components,
                "mixed_components": sum(len(row["identities"]) > 1 for row in components),
                "pure_three_view_identities": sorted(complete),
                "neighbors_per_fixture": degrees,
            }
        )
    return summary


def summarize(fixtures, outputs, min_score, thresholds):
    by_condition = {condition: Counter(zero=0, one=0, multiple=0) for condition in CONDITIONS}
    singles = dict.fromkeys((fixture.identity for fixture in fixtures), 0)
    records, images = [], []
    for fixture in fixtures:
        faces = faces_at(outputs[fixture.name], min_score)
        count = len(faces)
        by_condition[fixture.condition][("zero", "one", "multiple")[min(count, 2)]] += 1
        singles[fixture.identity] += count == 1
        records.append((fixture.name, fixture.identity, faces[0][1] if count == 1 else None))
        images.append(
            {
                "id": fixture.name,
                "identity": fixture.identity,
                "condition": fixture.condition,
                "faces": count,
                "scores": [face[0] for face in faces],
                "prepared_sha256": fixture.digest,
            }
        )
    return {
        "minScore": min_score,
        "by_condition": by_condition,
        "singles_per_identity": singles,
        "counts": {
            name: sum(row[name] for row in by_condition.values())
            for name in ("zero", "one", "multiple")
        },
        "images": images,
        "pairs": pair_metrics([(identity, vector) for _, identity, vector in records], thresholds),
        "neighbor_graph": neighbor_graph(records, thresholds),
    }


def measure(fixtures, request, mode, min_score=None, max_distance=None):
    scores, thresholds = (
        (SCORES, THRESHOLDS) if mode == "calibration" else ((min_score,), (max_distance,))
    )
    outputs, retries, proof = infer(fixtures, request, scores[0])
    runs = [summarize(fixtures, outputs, score, thresholds) for score in scores]
    return {
        "mode": mode,
        "model": MODEL_ID,
        "bridge_version": VERSION,
        "synthetic": True,
        "identities": 12,
        "fixtures": len(fixtures),
        "direct_minScore": scores[0],
        "score_grid_uses_float32_filter": mode == "calibration",
        "retries": retries,
        "cuda_proof": proof,
        "verified": proof is not None,
        "runs": runs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("calibration", "validation"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--bearer-file", type=Path, required=True)
    parser.add_argument("--base-url", choices=BASE_URLS, default=BASE_URLS[0])
    parser.add_argument("--min-score", type=float, choices=SCORES)
    parser.add_argument("--max-distance", type=float, choices=THRESHOLDS)
    args = parser.parse_args()
    try:
        require(SHA256.fullmatch(args.manifest_sha256), "MANIFIESTO_PIN")
        require(args.bearer_file.name.casefold() != ".env", "BEARER_ARCHIVO")
        require(
            (args.min_score is not None and args.max_distance is not None)
            if args.mode == "validation"
            else (args.min_score is None and args.max_distance is None),
            "UMBRALES_REQUERIDOS",
        )
        fixtures = load_corpus(args.manifest, args.manifest_sha256, args.mode)
        if args.bearer_file == Path("-"):
            token = sys.stdin.buffer.read(4098)
        else:
            token = read_regular(args.bearer_file, 4098)
        started = time.monotonic()
        report = measure(
            fixtures,
            bridge(args.base_url, token.rstrip(b"\r\n")),
            args.mode,
            args.min_score,
            args.max_distance,
        )
        report.update(
            manifest_sha256=args.manifest_sha256,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        code = (
            str(error)
            if type(error) is ValueError and re.fullmatch(r"[A-Z0-9_]+", str(error))
            else "SONDA_FALLIDA"
        )
        print(json.dumps({"verified": False, "error": code}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
