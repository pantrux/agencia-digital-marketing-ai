"""Compara PNG y preview exacto del split de calibración, sin servicios ni embeddings persistidos."""

import argparse
import io
import json
import math
import re
import sys
from dataclasses import replace
from itertools import combinations, product
from pathlib import Path
from statistics import median

import numpy as np
import onnxruntime as ort
from face_models import MODEL_ID, FaceModels, verify_artifacts
from immich_face_controlface_probe import load_corpus, response_faces
from immich_face_mpfb_probe import pair_metrics, read_regular
from immich_face_qub_probe import cosine_distance, require, sha
from PIL import Image

PIN = re.compile(r"[a-f0-9]{64}")
MAX_RECEIPT = 1024**2
MAX_PREVIEW = 8 * 1024**2
PREVIEW_FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}


def load_pinned_json(path, pin):
    raw = read_regular(path, MAX_RECEIPT)
    require(PIN.fullmatch(pin) and sha(raw) == pin, "JSON_SHA")
    return json.loads(raw)


def load_previews(path, pin, source_pin, fixtures, receipt):
    require(
        receipt["probe"]["split"] == "calibration"
        and receipt["probe"]["manifestSha256"] == source_pin,
        "E2E_SCOPE",
    )
    assets = {row["id"]: row for row in receipt["probe"]["assets"] if row["expectedFaces"] == 1}
    manifest = load_pinned_json(path, pin)
    rows = {row["id"]: row for row in manifest["fixtures"]}
    names = {fixture.name for fixture in fixtures}
    require(
        manifest["schema"] == 1
        and len(manifest["fixtures"]) == len(rows) == 36
        and names == assets.keys() == rows.keys(),
        "PREVIEW_INVENTORY",
    )
    parent = path.parent.resolve(strict=True)
    output = []
    for fixture in fixtures:
        row, asset = rows[fixture.name], assets[fixture.name]
        relative = row["path"]
        extension = relative.rsplit(".", 1)[-1]
        require(
            extension in PREVIEW_FORMATS
            and relative == f"previews/{fixture.name}.{extension}"
            and row["asset_id"] == asset["assetId"]
            and asset["pngSha256"] == fixture.digest
            and asset["identity"] == fixture.identity
            and asset["condition"] == fixture.condition
            and type(row["bytes"]) is int
            and 0 < row["bytes"] <= MAX_PREVIEW
            and PIN.fullmatch(row["sha256"]),
            "PREVIEW_SUBJECT",
        )
        file = parent / relative
        require(file.resolve(strict=True) == file, "PREVIEW_PATH")
        data = read_regular(file, row["bytes"])
        require(len(data) == row["bytes"] and sha(data) == row["sha256"], "PREVIEW_SHA")
        with Image.open(io.BytesIO(data), formats=(PREVIEW_FORMATS[extension],)) as image:
            width, height = image.size
            require(
                width >= 32
                and height >= 32
                and width * height <= 64_000_000
                and getattr(image, "n_frames", 1) == 1,
                "PREVIEW_DIMENSIONS",
            )
            image.verify()
        output.append(
            replace(fixture, digest=row["sha256"], width=width, height=height, image=data)
        )
    return output


def load_cpu(root):
    face = FaceModels(root)
    # Acredita todos los archivos con el manifiesto ADR-003 antes de cargar pesos.
    verify_artifacts(face.root, face.manifest["artifacts"])
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    face.detector = ort.InferenceSession(
        str(face.root / "detection.onnx"), options, providers=["CPUExecutionProvider"]
    )
    face.recognizer = ort.InferenceSession(
        str(face.root / "recognition.onnx"), options, providers=["CPUExecutionProvider"]
    )
    return face


def infer(face, fixtures, score):
    results = {}
    for row in fixtures:
        with Image.open(io.BytesIO(row.image), formats=("PNG", "JPEG", "WEBP")) as image:
            pixels = np.asarray(image.convert("RGB"), dtype=np.uint8)
        detected = face.detect(pixels, min_score=score)
        reply = {
            "imageWidth": row.width,
            "imageHeight": row.height,
            "facial-recognition": face.recognize(pixels, detected),
        }
        faces = response_faces(reply, row, score)
        results[row.name] = (len(faces), faces[0][1] if len(faces) == 1 else None)
    return results


def distance(left, right):
    return cosine_distance(left, right) if left is not None and right is not None else None


def compare(originals, previews, source_results, preview_results, merges, threshold):
    identities = {row.name: row.identity for row in originals}
    by_name = {row.name: row for row in previews}
    source = {name: values[1] for name, values in source_results.items()}
    derived = {name: values[1] for name, values in preview_results.items()}
    image_pairs = [
        {
            "id": row.name,
            "source_faces": source_results[row.name][0],
            "preview_faces": preview_results[row.name][0],
            "source_dimensions": [row.width, row.height],
            "preview_dimensions": [by_name[row.name].width, by_name[row.name].height],
            "distance": distance(source[row.name], derived[row.name]),
        }
        for row in originals
    ]
    shifts = [row["distance"] for row in image_pairs if row["distance"] is not None]
    crossed, deltas = [], []
    for left, right in combinations(identities, 2):
        a, b = distance(source[left], source[right]), distance(derived[left], derived[right])
        if a is None or b is None:
            continue
        deltas.append(abs(b - a))
        if (a <= threshold) != (b <= threshold):
            crossed.append(
                {
                    "left": left,
                    "right": right,
                    "genuine": identities[left] == identities[right],
                    "source": a,
                    "preview": b,
                }
            )
    disputed = []
    for merge in merges:
        groups = merge["identities"]
        require(len(groups) > 1 and set(groups) <= set(identities.values()), "E2E_MERGE_IDENTITY")
        for a, b in combinations(groups, 2):
            pairs = [
                {
                    "left": left,
                    "right": right,
                    "source": distance(source[left], source[right]),
                    "preview": distance(derived[left], derived[right]),
                }
                for left, right in product(
                    (name for name, identity in identities.items() if identity == a),
                    (name for name, identity in identities.items() if identity == b),
                )
            ]
            disputed.append({"identities": [a, b], "pairs": pairs})
    return {
        "same_image": {
            "pairs": image_pairs,
            "evaluable": len(shifts),
            "median": median(shifts) if shifts else None,
            "maximum": max(shifts, default=None),
        },
        "same_pair_absolute_delta_median": median(deltas) if deltas else None,
        "same_pair_absolute_delta_maximum": max(deltas, default=None),
        "threshold_crossings": crossed,
        "source": pair_metrics(
            [(identity, source[name]) for name, identity in identities.items()], (threshold,)
        ),
        "preview": pair_metrics(
            [(identity, derived[name]) for name, identity in identities.items()], (threshold,)
        ),
        "merged_identity_pairs": disputed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--preview-manifest", type=Path, required=True)
    parser.add_argument("--preview-manifest-sha256", required=True)
    parser.add_argument("--e2e-receipt", type=Path, required=True)
    parser.add_argument("--e2e-receipt-sha256", required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = load_pinned_json(args.e2e_receipt, args.e2e_receipt_sha256)
        originals = load_corpus(args.manifest, args.manifest_sha256, "calibration")
        previews = load_previews(
            args.preview_manifest,
            args.preview_manifest_sha256,
            args.manifest_sha256,
            originals,
            receipt,
        )
        score, threshold = (receipt["thresholds"][key] for key in ("minScore", "maxDistance"))
        require(type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1, "SCORE")
        require(
            type(threshold) in (int, float) and math.isfinite(threshold) and 0 <= threshold <= 2,
            "DISTANCE",
        )
        model = load_cpu(args.model_root)
        try:
            result = compare(
                originals,
                previews,
                infer(model, originals, score),
                infer(model, previews, score),
                receipt["probe"]["cluster"]["merges"],
                threshold,
            )
        finally:
            model.close()
        result.update(
            success=True,
            split="calibration",
            model=MODEL_ID,
            provider="CPUExecutionProvider",
            ort_version=ort.__version__,
            fixtures=len(originals),
            minScore=score,
            maxDistance=threshold,
            manifest_sha256=args.manifest_sha256,
            preview_manifest_sha256=args.preview_manifest_sha256,
            e2e_receipt_sha256=args.e2e_receipt_sha256,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as error:
        code = (
            str(error)
            if type(error) is ValueError and re.fullmatch(r"[A-Z0-9_]+", str(error))
            else "PREVIEW_DIAGNOSTIC_FAILED"
        )
        print(json.dumps({"success": False, "error": code}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
