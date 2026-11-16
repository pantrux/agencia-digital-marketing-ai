import itertools
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"))

from immich_face_controlface_consensus import analyze, partition  # noqa: E402


def distances(edges, default=0.9):
    names = sorted({name for edge in edges for name in edge})
    return {pair: edges.get(pair, default) for pair in itertools.combinations(names, 2)}


def test_reciprocity_removes_the_only_cross_group_edge():
    names = "abcdef"
    matrix = distances(
        {
            ("a", "b"): 0.40,
            ("b", "c"): 0.48,
            ("a", "d"): 0.49,
            ("d", "e"): 0.40,
            ("d", "f"): 0.41,
            ("e", "f"): 0.42,
        }
    )
    for order in itertools.permutations(names):
        groups, edges = partition(order, matrix, 0.55, reciprocal=True)
        assert groups == (("a", "b", "c"), ("d", "e", "f"))
        assert ("a", "d") not in edges
    assert partition(names, matrix, 0.475)[0] == (("d", "e", "f"),)
    assert partition(names, matrix, 0.55)[0] == (tuple(names),)


def test_exact_tie_for_second_neighbor_abstains_instead_of_using_ids():
    matrix = distances({("a", "b"): 0.40, ("a", "c"): 0.5, ("a", "d"): 0.5})
    for order in itertools.permutations("abcd"):
        assert partition(order, matrix, 0.55, reciprocal=True) == ((), (("a", "b"),))


def test_analysis_scores_labels_after_grouping_and_counts_unusable_assets():
    fixtures = [
        SimpleNamespace(name=name, digest=digest, identity=identity)
        for name, digest, identity in (
            ("a", "5", "one"),
            ("b", "4", "one"),
            ("c", "1", "one"),
            ("d", "2", "two"),
        )
    ]
    unit = (1.0, 0.0)
    outputs = {name: [(0.9, unit)] for name in "abc"} | {"d": []}
    result = analyze(fixtures, outputs)
    consensus = result["arms"]["reciprocal_top2_055"]
    assert result["distance_pairs"] == 3
    assert result["detection_counts"] == {"zero": 1, "one": 3, "multiple": 0}
    assert result["order_trials"] == 18
    assert consensus["assigned"] == 3
    assert consensus["pure_complete_identities"] == 1
    assert consensus["unassigned"] == 1
    assert consensus["mixed_groups"] == 0
