"""Diagnóstico sintético de AuraFace con y sin reflejo horizontal."""

import argparse
import io
import json
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np
from face_models import MODEL_ID, FaceModels, align_crop, prepare_recognition
from immich_face_controlface_consensus import BASELINE_RADIUS, MIN_SCORE
from immich_face_controlface_corpus import THIRD_COHORT, THIRD_PLAN_SHA
from immich_face_controlface_probe import (
    HISTORICAL_MANIFEST_SHA,
    SHA256,
    load_corpus,
    neighbor_graph,
)
from immich_face_mpfb_probe import pair_metrics, percentiles
from immich_face_qub_probe import cosine_distance, require
from PIL import Image


def raw_embedding(session, crop):
    outputs = session.run(["1333"], {"data": prepare_recognition(crop)})
    require(len(outputs) == 1, "FLIP_OUTPUT")
    embedding = np.asarray(outputs[0], dtype=np.float32)
    require(embedding.shape == (1, 512) and np.isfinite(embedding).all(), "FLIP_VECTOR")
    return embedding[0]


def unit(embedding):
    norm = np.linalg.norm(embedding.astype(np.float64))
    require(np.isfinite(norm) and norm >= 1e-12, "FLIP_NORM")
    return (embedding / norm).astype(np.float32).tolist()


def infer(model, fixtures):
    baseline, flipped = {}, {}
    counts = Counter(zero=0, one=0, multiple=0)
    baseline_ms, incremental_ms = [], []
    for fixture in fixtures:
        with Image.open(io.BytesIO(fixture.image), formats=("PNG",)) as image:
            pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
        points = model.detect(pixels, min_score=MIN_SCORE)["landmarks"]
        counts[("zero", "one", "multiple")[min(len(points), 2)]] += 1
        if len(points) != 1:
            continue
        crop = align_crop(pixels, points[0])
        if not baseline:
            # Calienta ambos brazos y completa la primera acreditación CUDA antes del cronómetro.
            raw_embedding(model.recognizer, crop)
            raw_embedding(model.recognizer, np.ascontiguousarray(crop[:, ::-1, :]))
        started = perf_counter()
        raw = raw_embedding(model.recognizer, crop)
        baseline[fixture.name] = unit(raw)
        ended = perf_counter()
        baseline_ms.append(1000 * (ended - started))
        mirrored = raw_embedding(model.recognizer, np.ascontiguousarray(crop[:, ::-1, :]))
        flipped[fixture.name] = unit(raw + mirrored)
        incremental_ms.append(1000 * (perf_counter() - ended))
    return baseline, flipped, counts, baseline_ms, incremental_ms


def distribution(values):
    values = sorted(values)
    return {
        "count": len(values),
        "minimum": values[0] if values else None,
        "maximum": values[-1] if values else None,
        "percentiles_nearest": percentiles(values),
    }


def summarize(fixtures, baseline, flipped):
    reports = {}
    for name, vectors in (("baseline", baseline), ("flip_sum", flipped)):
        records = [(row.name, row.identity, vectors.get(row.name)) for row in fixtures]
        graph = neighbor_graph(records, (BASELINE_RADIUS,))[0]
        reports[name] = pair_metrics(
            [(identity, vector) for _, identity, vector in records], (BASELINE_RADIUS,)
        )
        reports[name]["grouping"] = {
            "complete_pure_identities": len(graph["pure_three_view_identities"]),
            "mixed_components": graph["mixed_components"],
        }

    shifts = {"genuine": [], "impostor": []}
    crossings = {label: Counter(accepted_only_baseline=0, accepted_only_flip=0) for label in shifts}
    for left, right in combinations(fixtures, 2):
        a, b = left.name, right.name
        if a not in baseline or b not in baseline:
            continue
        label = "genuine" if left.identity == right.identity else "impostor"
        before = cosine_distance(baseline[a], baseline[b])
        after = cosine_distance(flipped[a], flipped[b])
        shifts[label].append(after - before)
        crossings[label]["accepted_only_baseline"] += before <= BASELINE_RADIUS < after
        crossings[label]["accepted_only_flip"] += after <= BASELINE_RADIUS < before
    reports["paired_distance_delta"] = {
        label: {
            "signed_flip_minus_baseline": distribution(values),
            "absolute": distribution(abs(value) for value in values),
            "threshold_crossings": crossings[label],
        }
        for label, values in shifts.items()
    }
    reports["same_face_drift"] = distribution(
        cosine_distance(vector, flipped[name]) for name, vector in baseline.items()
    )
    return reports


def run_cpu(root, fixtures):
    import onnxruntime as ort
    from immich_face_controlface_preview_diagnostic import load_cpu

    model = load_cpu(root)
    try:
        return infer(model, fixtures), ort.__version__, None
    finally:
        model.close()


def run_cuda(root, profile_parent, fixtures):
    from immich_portability import CudaEvidence, load_manifest
    from immich_service import preload_cuda

    ort, _runtime, handles = preload_cuda(load_manifest())
    evidence = CudaEvidence()
    model = FaceModels(root)
    try:
        with TemporaryDirectory(prefix="aura-flip-", dir=profile_parent) as profile:
            try:
                model.load(ort, profile_directory=Path(profile), evidence=evidence)
                result = infer(model, fixtures)
                proof = evidence.snapshot()
                require(
                    proof["cuda_verified"]
                    and set(proof["inference_profiles"])
                    == {
                        "facial-recognition/detection",
                        "facial-recognition/recognition",
                    },
                    "FLIP_CUDA_PROOF",
                )
                return result, ort.__version__, proof["inference_profiles"]
            finally:
                model.close()
    finally:
        for handle in handles:
            handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--provider", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--cuda-profile-parent", type=Path)
    parser.add_argument("--cohort", choices=(THIRD_COHORT,))
    parser.add_argument("--plan-sha256")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--historical-manifest", type=Path)
    parser.add_argument("--second-manifest", type=Path)
    args = parser.parse_args()
    if (args.provider == "cuda") != (args.cuda_profile_parent is not None):
        parser.error("--cuda-profile-parent se requiere únicamente con --provider cuda")
    third = args.cohort == THIRD_COHORT
    holdout_arguments = (
        args.plan_sha256,
        args.manifest_sha256,
        args.historical_manifest,
        args.second_manifest,
    )
    if third:
        if args.provider != "cuda" or not all(holdout_arguments):
            parser.error(
                "La tercera cohorte requiere CUDA, ambas huellas y ambos manifiestos previos"
            )
        if args.plan_sha256 != THIRD_PLAN_SHA or not SHA256.fullmatch(args.manifest_sha256):
            parser.error("Huellas inválidas para la tercera cohorte")
    elif any(value is not None for value in holdout_arguments):
        parser.error("Los argumentos de validación requieren --cohort")
    split = "holdout" if third else "calibration"
    manifest_pin = args.manifest_sha256 if third else HISTORICAL_MANIFEST_SHA
    try:
        fixtures = load_corpus(
            args.manifest,
            manifest_pin,
            split,
            cohort=args.cohort,
            plan_sha256=args.plan_sha256,
            historical_manifest=args.historical_manifest,
            second_manifest=args.second_manifest,
        )
        if args.provider == "cuda":
            result, version, proof = run_cuda(args.model_root, args.cuda_profile_parent, fixtures)
        else:
            result, version, proof = run_cpu(args.model_root, fixtures)
        baseline, flipped, counts, baseline_ms, incremental_ms = result
        require(baseline, "FLIP_NO_FACES")
        report = summarize(fixtures, baseline, flipped)
        report.update(
            success=True,
            split=split,
            model=MODEL_ID,
            provider="CUDAExecutionProvider" if args.provider == "cuda" else "CPUExecutionProvider",
            ort_version=version,
            cuda_proof=proof,
            manifest_sha256=manifest_pin,
            minScore=MIN_SCORE,
            maxDistance=BASELINE_RADIUS,
            fixtures=len(fixtures),
            detection_counts=counts,
            timing_ms={
                "baseline_recognition": distribution(baseline_ms),
                "incremental_reflection_and_fusion": distribution(incremental_ms),
                "incremental_total": sum(incremental_ms),
                "scope": "aligned_crops_after_warmup",
            },
        )
        if third:
            candidate = report["flip_sum"]
            identities = len({fixture.identity for fixture in fixtures})
            report.update(
                cohort=args.cohort,
                plan_sha256=args.plan_sha256,
                primary_arm="flip_sum",
                primary_gate_passed=(
                    counts["one"] == len(fixtures)
                    and candidate["thresholds"][0]["false_matches"] == 0
                    and candidate["grouping"]["complete_pure_identities"] == identities
                    and candidate["grouping"]["mixed_components"] == 0
                ),
                identities=identities,
            )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RuntimeError,
        ImportError,
    ) as error:
        code = (
            str(error)
            if type(error) is ValueError
            and str(error).isascii()
            and str(error).isupper()
            and str(error).replace("_", "").isalnum()
            else "FLIP_FAILED"
        )
        print(json.dumps({"success": False, "error": code}), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
