"""Compara reglas de agrupación en la calibración sintética de ControlFace10K."""

import argparse
import json
import random
import re
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

from immich_face_controlface_corpus import HOLDOUT_COHORT
from immich_face_controlface_probe import SHA256, infer, load_corpus
from immich_face_mpfb_probe import read_regular
from immich_face_qub_probe import BASE_URLS, MODEL_ID, bridge, cosine_distance, require

MIN_SCORE = 0.60
BASELINE_RADIUS = 0.475
CONSENSUS_RADIUS = 0.55
MIN_GROUP = 3
RANDOM_PERMUTATIONS = 16
PERMUTATION_SEED = 20261113


def partition(order, distances, radius, *, reciprocal=False):
    adjacency = {name: set() for name in order}
    for left, right in combinations(order, 2):
        pair = (min(left, right), max(left, right))
        if distances[pair] <= radius:
            adjacency[left].add(right)
            adjacency[right].add(left)
    if reciprocal:
        nearest = {}
        for name, candidates in adjacency.items():
            ranked = sorted(
                (distances[min(name, other), max(name, other)], other) for other in candidates
            )
            cutoff = ranked[1][0] if len(ranked) > 2 and ranked[1][0] == ranked[2][0] else None
            # Un empate en el segundo puesto no se resuelve por el nombre ni por el orden.
            nearest[name] = {other for distance, other in ranked[:2] if distance != cutoff}
        adjacency = {
            name: {other for other in candidates if name in nearest[other]}
            for name, candidates in nearest.items()
        }

    remaining, groups = set(order), []
    while remaining:
        start = min(remaining)
        component, frontier = {start}, [start]
        while frontier:
            additional = adjacency[frontier.pop()] - component
            component.update(additional)
            frontier.extend(additional)
        remaining.difference_update(component)
        if len(component) >= MIN_GROUP:
            groups.append(tuple(sorted(component)))
    edges = tuple(
        sorted(
            (left, right) for left, links in adjacency.items() for right in links if left < right
        )
    )
    return tuple(groups), edges


def score(groups, edges, identities):
    sizes = Counter(identities.values())
    coverage = Counter(identities[name] for group in groups for name in group)
    memberships = [Counter(identities[name] for name in group) for group in groups]
    splits = Counter(identity for group in memberships for identity in group)
    false_edges = sum(identities[left] != identities[right] for left, right in edges)
    return {
        "groups": len(groups),
        "assigned": sum(coverage.values()),
        "unassigned": len(identities) - sum(coverage.values()),
        "mixed_groups": sum(len(group) > 1 for group in memberships),
        "split_identities": sum(count > 1 for count in splits.values()),
        "pure_complete_identities": sum(
            len(group) == 1 and all(count == sizes[identity] for identity, count in group.items())
            for group in memberships
        ),
        "genuine_edges": len(edges) - false_edges,
        "impostor_edges": false_edges,
        "unassigned_by_identity": {
            identity: count - coverage[identity]
            for identity, count in sorted(sizes.items())
            if coverage[identity] < count
        },
    }


def analyze(fixtures, outputs):
    # Los identificadores opacos no contienen etiquetas. Cada imagen puede aportar un solo vector.
    require(len({row.digest for row in fixtures}) == len(fixtures), "CONSENSO_IMAGEN_DUPLICADA")
    counts = Counter(zero=0, one=0, multiple=0)
    vectors = {}
    for fixture in fixtures:
        faces = outputs[fixture.name]
        counts[("zero", "one", "multiple")[min(2, len(faces))]] += 1
        if len(faces) == 1:
            vectors[fixture.digest] = faces[0][1]
    distances = {
        (left, right): cosine_distance(vectors[left], vectors[right])
        for left, right in combinations(sorted(vectors), 2)
    }
    order = tuple(vectors)
    rng = random.Random(PERMUTATION_SEED)
    orders = [
        order,
        order[::-1],
        *(tuple(rng.sample(order, len(order))) for _ in range(RANDOM_PERMUTATIONS)),
    ]
    outcomes = {}
    for name, radius, reciprocal in (
        ("radius_0475", BASELINE_RADIUS, False),
        ("radius_055", CONSENSUS_RADIUS, False),
        ("reciprocal_top2_055", CONSENSUS_RADIUS, True),
    ):
        groups, edges = partition(order, distances, radius, reciprocal=reciprocal)
        require(
            all(
                partition(trial, distances, radius, reciprocal=reciprocal) == (groups, edges)
                for trial in orders[1:]
            ),
            "CONSENSO_DEPENDE_DEL_ORDEN",
        )
        outcomes[name] = (groups, edges)

    identities = {row.digest: row.identity for row in fixtures}
    return {
        "detection_counts": counts,
        "distance_pairs": len(distances),
        "order_trials": len(orders),
        "order_invariant": True,
        "arms": {
            name: score(groups, edges, identities) for name, (groups, edges) in outcomes.items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--bearer-file", type=Path, required=True)
    parser.add_argument("--base-url", choices=BASE_URLS, default=BASE_URLS[0])
    parser.add_argument("--cohort", choices=(HOLDOUT_COHORT,))
    parser.add_argument("--plan-sha256")
    parser.add_argument("--historical-manifest", type=Path)
    args = parser.parse_args()
    try:
        require(SHA256.fullmatch(args.manifest_sha256), "MANIFIESTO_PIN")
        require(args.bearer_file.name.casefold() != ".env", "BEARER_ARCHIVO")
        split = "holdout" if args.cohort else "calibration"
        fixtures = load_corpus(
            args.manifest,
            args.manifest_sha256,
            split,
            cohort=args.cohort,
            plan_sha256=args.plan_sha256,
            historical_manifest=args.historical_manifest,
        )
        token = (
            sys.stdin.buffer.read(4098)
            if args.bearer_file == Path("-")
            else read_regular(args.bearer_file, 4098)
        )
        outputs, retries, proof = infer(
            fixtures, bridge(args.base_url, token.rstrip(b"\r\n")), MIN_SCORE
        )
        require(proof is not None, "CONSENSO_CUDA")
        report = analyze(fixtures, outputs)
        report.update(
            verified=True,
            split=split,
            synthetic=True,
            model=MODEL_ID,
            manifest_sha256=args.manifest_sha256,
            minScore=MIN_SCORE,
            baseline_radius=BASELINE_RADIUS,
            consensus_radius=CONSENSUS_RADIUS,
            min_group=MIN_GROUP,
            cutoff_ties="abstain",
            permutation_seed=PERMUTATION_SEED,
            cuda_proof=proof,
            retries=retries,
        )
        if args.cohort:
            report.update(
                cohort=args.cohort,
                plan_sha256=args.plan_sha256,
                primary_arm="reciprocal_top2_055",
                identities=24,
                fixtures=len(fixtures),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
