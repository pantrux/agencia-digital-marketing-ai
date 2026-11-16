import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

with patch.object(
    sys, "path", [str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"), *sys.path]
):
    import immich_face_mpfb_probe as probe
    from immich_face_qub_probe import TASKS


def corpus(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    fixtures, identities = [], []
    for index, seed in enumerate(probe.SEEDS, 1):
        identity = f"mh-{index:03d}"
        identities.append({"identity": identity, "seed": seed, "mesh_sha256": f"{index:064x}"})
        for view, condition in enumerate(probe.CONDITIONS):
            name = f"{identity}_{condition}"
            image = images / f"{name}.png"
            Image.new("RGB", (512, 512), (index, view, 0)).save(image)
            digest = probe.sha(image.read_bytes())
            fixtures.append(
                {
                    "id": name,
                    "identity": identity,
                    "condition": condition,
                    "split": "calibration" if index <= 8 else "validation",
                    "path": f"images/{name}.png",
                    "original_sha256": digest,
                    "prepared_sha256": digest,
                    "width": 512,
                    "height": 512,
                    "expected_faces": 1,
                }
            )
    path = tmp_path / "fixtures.json"
    manifest = {
        "schema": 1,
        "synthetic": True,
        "asset_license": "CC0-1.0",
        "asset_pack": probe.PACK,
        "asset_pack_sha256": "1" * 64,
        "mpfb_version": probe.MPFB_VERSION,
        "mpfb_reference_commit": probe.MPFB_REVISION,
        "identities": identities,
        "fixtures": fixtures,
    }
    path.write_text(json.dumps(manifest), encoding="utf8")
    return path, manifest


def health():
    return {
        "service": "immich-ml",
        "version": probe.VERSION,
        "ready": True,
        "cuda_available": True,
        "cuda_verified": True,
        "backend_failed": False,
        "face_model": probe.MODEL_ID,
        "commercial_face_model": True,
        "models_resident": 0,
        "proof_age_seconds": 0.1,
        "inference_profiles": {task: {"cuda_nodes": 1} for task in TASKS},
    }


def test_corpus_pins_and_split(tmp_path):
    path, manifest = corpus(tmp_path)
    pins = probe.sha(path.read_bytes())
    assert len(probe.load_corpus(path, pins)) == 96
    with pytest.raises(ValueError, match="MANIFIESTO_SHA"):
        probe.load_corpus(path, "0" * 64)
    image = tmp_path / manifest["fixtures"][0]["path"]
    original = image.read_bytes()
    image.write_bytes(original + b"change")
    with pytest.raises(ValueError, match="CORPUS_PNG_SHA"):
        probe.load_corpus(path, pins)
    image.write_bytes(original)
    manifest["fixtures"][48]["split"] = "calibration"
    path.write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(ValueError, match="CORPUS_DIVISION"):
        probe.load_corpus(path, probe.sha(path.read_bytes()))


def test_frozen_validation_and_no_vectors(tmp_path):
    path, _ = corpus(tmp_path)
    fixtures = probe.load_corpus(path, probe.sha(path.read_bytes()))
    received = []

    def request(endpoint, *, entries=None, image=None):
        if endpoint == "/health":
            return health()
        with Image.open(io.BytesIO(image)) as source:
            index, condition, blue = source.getpixel((0, 0))
        score = entries["facial-recognition"]["detection"]["options"]["minScore"]
        received.append((index, condition, blue, score))
        faces = []
        if 1 <= index <= 16 and blue == 0:
            values = [0] * 512
            values[index] = 1
            faces = [
                {
                    "score": 0.85,
                    "embedding": json.dumps(values),
                    "boundingBox": {"x1": 20, "y1": 25, "x2": 300, "y2": 310},
                }
            ]
        return {"imageHeight": 512, "imageWidth": 512, "facial-recognition": faces}

    result = probe.evaluate(fixtures, request)
    assert result["synthetic_candidate_passed"]
    assert result["candidate"] == {"minScore": 0.60, "maxDistance": 0.30, "minFaces": 3}
    assert len(result["calibration"]) == 5
    assert result["validation"]["pairs"]["genuine"]["evaluated"] == 120
    assert result["validation"]["pairs"]["impostor"]["evaluated"] == 1008
    assert result["validation"]["negative_images_with_faces"] == 0
    assert result["validation"]["by_condition"]["frontal"]["single_rate"] == 1
    assert result["filter_equivalence"]["direct_faces"] == 2
    assert len(received) == 101
    assert all(entry[3] == 0.60 for entry in received[:51])
    assert all(entry[3] == 0.80 for entry in received[51:53])
    assert all(9 <= entry[0] <= 16 and entry[3] == 0.60 for entry in received[53:])
    assert '"embedding"' not in json.dumps(result)


def test_score_boundaries_and_cuda_rejection():
    edge = probe.float32(0.70)
    face = (edge, (1,), (0, 0, 1, 1))
    assert probe.faces_at([face], 0.70) == [face]
    assert probe.faces_at([face], 0.75) == []

    response = {"imageHeight": 512, "imageWidth": 512, "facial-recognition": []}
    response["imageWidth"] = 128
    with pytest.raises(ValueError, match="RESPUESTA_DIMENSIONES"):
        probe.face_result(response, 0.60)
    report = health()
    report["inference_profiles"][TASKS[1]]["cuda_nodes"] = 0
    with pytest.raises(ValueError, match="CUDA_PERFILES"):
        probe.health_proof(report, facial=True)
