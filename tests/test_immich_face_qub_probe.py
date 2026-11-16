import io
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

with patch.object(
    sys, "path", [str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"), *sys.path]
):
    import immich_face_qub_probe as probe


def fake_archive():
    raw = io.BytesIO()
    selected = {
        probe.archive_name(identity, condition)
        for identity in probe.IDENTITIES
        for condition in probe.CONDITIONS
    }
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, name in enumerate(sorted(probe.expected_names())):
            tiff = io.BytesIO()
            if name in selected:
                color = (index % 256, index // 256, 0)
                Image.new("RGB", (128, 128), color).save(tiff, "TIFF")
            archive.writestr(name, tiff.getvalue())
    return raw.getvalue()


def test_archive_pin_and_exact_rgb_conversion(monkeypatch):
    raw = fake_archive()
    with pytest.raises(ValueError, match="ZIP_SHA256"):
        probe.load_fixtures(raw)
    monkeypatch.setattr(probe, "ARCHIVE_BYTES", len(raw))
    monkeypatch.setattr(probe, "ARCHIVE_SHA256", probe.sha(raw))
    fixtures = probe.load_fixtures(raw)
    assert len(fixtures) == 18
    assert {identity for identity, *_ in fixtures} == set(probe.IDENTITIES)
    for _, _, _, payload in fixtures:
        with Image.open(io.BytesIO(payload), formats=("PNG",)) as image:
            assert image.size == (128, 128) and image.mode == "RGB"


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
        "proof_age_seconds": 0.25,
        "inference_profiles": {task: {"cuda_nodes": 1} for task in probe.TASKS},
    }


def fake_face(identity):
    coordinates = [0] * 512
    coordinates[probe.IDENTITIES.index(identity)] = 1
    return {
        "score": 0.85,
        "embedding": json.dumps(coordinates),
        "boundingBox": {"x1": 5, "y1": 6, "x2": 85, "y2": 95},
    }


def test_pair_totals_and_cuda_with_one_missed_detection():
    fixtures = [
        (identity, condition, "fake", f"{identity}/{condition}".encode())
        for identity in probe.IDENTITIES
        for condition in probe.CONDITIONS
    ]

    def request(path, *, entries=None, image=None):
        if path == "/health":
            return health()
        assert path == "/predict" and entries == probe.entries_for(0.70)
        identity, condition = image.decode().split("/")
        faces = [] if image == fixtures[0][3] else [fake_face(identity)]
        assert condition in probe.CONDITIONS
        return {"imageWidth": 128, "imageHeight": 128, "facial-recognition": faces}

    result = probe.measure(fixtures, request, (0.70,))
    assert result["verified"] and not result["calibrated"]
    pairs = result["runs"][0]["pairs"]
    assert pairs["genuine"] == {
        "total": 45,
        "evaluated": 40,
        "unavailable": 5,
        "minimum": 0.0,
        "maximum": 0.0,
    }
    assert pairs["impostor"] == {
        "total": 108,
        "evaluated": 96,
        "unavailable": 12,
        "minimum": 1.0,
        "maximum": 1.0,
    }
    assert not any(row["false_matches"] or row["false_rejects"] for row in pairs["thresholds"])
    assert '"embedding"' not in json.dumps(result)


def test_cuda_profiles_and_url_are_fail_closed():
    report = health()
    report["inference_profiles"][probe.TASKS[1]]["cuda_nodes"] = 0
    with pytest.raises(ValueError, match="CUDA_PERFILES"):
        probe.health_proof(report, facial=True)
    with pytest.raises(ValueError, match="PUENTE_URL"):
        probe.bridge("http://example.invalid:3003", b"test-bearer-not-real")


def test_faces_reject_invalid_norm_and_dimensions():
    face = fake_face(probe.IDENTITIES[0])
    response = {"imageWidth": 127, "imageHeight": 128, "facial-recognition": [face]}
    with pytest.raises(ValueError, match="RESPUESTA_DIMENSIONES"):
        probe.face_result(response, 0.70)
    response["imageWidth"] = 128
    face["embedding"] = json.dumps([1] * 512)
    with pytest.raises(ValueError, match="RESPUESTA_NORMA"):
        probe.face_result(response, 0.70)
