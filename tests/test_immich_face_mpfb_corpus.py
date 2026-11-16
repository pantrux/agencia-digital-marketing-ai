import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

with patch.object(
    sys, "path", [str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"), *sys.path]
):
    import immich_face_mpfb_corpus as corpus


def arguments(tmp_path):
    return [
        "--output",
        str(tmp_path / "new"),
        "--assets-zip",
        "cc0.zip",
        "--assets-sha256",
        "0" * 64,
    ]


def seed_manifest(tmp_path, calibration, validation, cohort="fresh-v1"):
    path = tmp_path / "seeds.json"
    content = json.dumps(
        {"schema": 1, "cohort": cohort, "calibration": calibration, "validation": validation}
    )
    path.write_bytes(content.encode("utf-8"))
    return [
        "--seed-manifest",
        str(path),
        "--seed-manifest-sha256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    ]


@pytest.mark.parametrize("count", [None, 2, 16])
def test_historical_selection(tmp_path, count):
    explicit_count = ["--count", str(count)] if count is not None else []
    args = corpus.read_arguments(arguments(tmp_path) + explicit_count)
    assert args.identities == tuple(
        (f"mh-{index:03d}", seed, "calibration" if index <= 8 else "validation")
        for index, seed in enumerate(corpus.SEEDS[: count or 2], start=1)
    )
    assert args.seed_manifest_sha256 is None


@pytest.mark.parametrize("size", [4, 8])
def test_new_cohort_has_six_views_and_separate_partitions(tmp_path, size):
    extra = seed_manifest(tmp_path, list(range(size)), list(range(10, 10 + size)))
    args = corpus.read_arguments(arguments(tmp_path) + extra)
    assert args.cohort == "fresh-v1"
    assert args.seed_manifest_sha256 == extra[-1]
    assert args.identities[:size] == tuple(
        (f"mh-fresh-v1-{index + 1:03d}", index, "calibration") for index in range(size)
    )
    assert args.identities[size:] == tuple(
        (f"mh-fresh-v1-{size + index + 1:03d}", 10 + index, "validation") for index in range(size)
    )
    assert len(corpus.VIEWS) == 6
    assert not args.output.exists()


@pytest.mark.parametrize(
    ("calibration", "validation", "cohort", "reason"),
    [
        ([0, 1], [1, 2], "fresh-v1", "repetidas"),
        ([0, 0], [1, 2], "fresh-v1", "repetidas"),
        ([0, corpus.SEEDS[0]], [1, 2], "fresh-v1", "históricas"),
        ([0, True], [1, 2], "fresh-v1", "enteras"),
        ([0, -1], [1, 2], "fresh-v1", "enteras"),
        ([0, 2**32], [1, 2], "fresh-v1", "enteras"),
        ([0, 1], [2], "fresh-v1", "partición"),
        ([0, 1], [2, 3], "../historical", "cohorte"),
    ],
)
def test_rejects_overlapping_or_invalid_selection(
    tmp_path, calibration, validation, cohort, reason
):
    extra = seed_manifest(tmp_path, calibration, validation, cohort)
    with pytest.raises(ValueError, match=reason):
        corpus.read_arguments(arguments(tmp_path) + extra)


def test_manifest_pin_required_and_exclusive_with_count(tmp_path):
    extra = seed_manifest(tmp_path, [0, 1], [2, 3])
    with pytest.raises(SystemExit):
        corpus.read_arguments(arguments(tmp_path) + extra + ["--count", "2"])
    with pytest.raises(ValueError, match="juntos"):
        corpus.read_arguments(arguments(tmp_path) + extra[:2])
    with pytest.raises(ValueError, match="juntos"):
        corpus.read_arguments(arguments(tmp_path) + extra[2:])
    with pytest.raises(ValueError, match="incorrecto"):
        corpus.read_arguments(arguments(tmp_path) + extra[:-1] + ["1" * 64])
