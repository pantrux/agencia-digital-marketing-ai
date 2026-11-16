"""Sonda facial sintética con transporte simulado y particiones verificadas."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from PIL import Image

with patch.object(
    sys, "path", [str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"), *sys.path]
):
    import immich_face_controlface_probe as probe
    from immich_face_controlface_corpus import THIRD_COHORT, THIRD_PLAN_SHA
    from immich_face_qub_probe import TASKS


class ProbeTests(unittest.TestCase):
    def test_fresh_holdout_requires_pins_and_rejects_old_images_or_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "images").mkdir()
            fixtures = []
            for i, (name, identity, condition, split, stratum) in enumerate(
                probe.expected_rows(probe.HOLDOUT_COHORT)
            ):
                origin = f"00000000-0000-0000-0000-{int(identity[4:]):012x}"
                file = root / "images" / f"{name}.png"
                Image.new("RGB", (48, 36), (i, 0, 0)).save(file)
                fixtures.append(
                    {
                        "id": name,
                        "identity": identity,
                        "condition": condition,
                        "split": split,
                        "expected_faces": 1,
                        "width": 48,
                        "height": 36,
                        "path": f"images/{file.name}",
                        "source_identity": origin,
                        "source_member": "ControlFace10k/"
                        + "/".join(stratum)
                        + f"/identity-{origin}/r0_g0_a25_{condition}_fixture.png",
                        "prepared_sha256": probe.sha(file.read_bytes()),
                    }
                )
            manifest = root / "fixtures.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "synthetic": True,
                        "dataset": probe.DATASET,
                        "revision": probe.REVISION,
                        "central_sha256": probe.CENTRAL_SHA,
                        "license": "CC BY 4.0",
                        "cohort": probe.HOLDOUT_COHORT,
                        "plan_sha256": probe.HOLDOUT_PLAN_SHA,
                        "fixtures": fixtures,
                    }
                ),
                encoding="utf-8",
            )
            pin = probe.sha(manifest.read_bytes())
            historical = root / "old.json"
            arguments = {
                "cohort": probe.HOLDOUT_COHORT,
                "plan_sha256": probe.HOLDOUT_PLAN_SHA,
                "historical_manifest": historical,
            }
            with self.assertRaisesRegex(ValueError, "CORPUS_DIVISION"):
                probe.load_corpus(manifest, pin, "holdout")
            with self.assertRaisesRegex(ValueError, "MANIFIESTO_PROCEDENCIA"):
                probe.load_corpus(manifest, pin, "calibration")
            with self.assertRaisesRegex(ValueError, "CORPUS_DIVISION"):
                probe.load_corpus(
                    manifest, pin, "holdout", **(arguments | {"plan_sha256": "a" * 64})
                )

            fresh_id = fixtures[0]["source_identity"]
            fresh_hash = fixtures[0]["prepared_sha256"]
            for old_id, old_hash, overlap in (
                ("00000000-0000-0000-0000-00000000ffff", "f" * 64, False),
                (fresh_id, "f" * 64, True),
                ("00000000-0000-0000-0000-00000000ffff", fresh_hash, True),
            ):
                historical.write_text(
                    json.dumps(
                        {"fixtures": [{"source_identity": old_id, "prepared_sha256": old_hash}]}
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "CORPUS_HISTORICO_PIN"):
                    probe.load_corpus(manifest, pin, "holdout", **arguments)
                with patch.object(
                    probe, "HISTORICAL_MANIFEST_SHA", probe.sha(historical.read_bytes())
                ):
                    if overlap:
                        with self.assertRaisesRegex(ValueError, "CORPUS_HOLDOUT_SOLAPADO"):
                            probe.load_corpus(manifest, pin, "holdout", **arguments)
                    else:
                        selected = probe.load_corpus(manifest, pin, "holdout", **arguments)
                        self.assertEqual(len(selected), 72)
                        self.assertEqual(len({fixture.identity for fixture in selected}), 24)

            second = root / "second.json"
            second.write_text(historical.read_text(encoding="utf-8"), encoding="utf-8")
            historical.write_text(json.dumps({"fixtures": []}), encoding="utf-8")
            doc = json.loads(manifest.read_bytes())
            doc.update(cohort=THIRD_COHORT, plan_sha256=THIRD_PLAN_SHA)
            for row in doc["fixtures"]:
                original = root / row["path"]
                row["id"] = row["id"].replace("cfh-", "cfh3-")
                row["identity"] = row["identity"].replace("cfh-", "cfh3-")
                row["path"] = f"images/{row['id']}.png"
                original.rename(root / row["path"])
            manifest.write_text(json.dumps(doc), encoding="utf-8")
            pin = probe.sha(manifest.read_bytes())
            arguments.update(cohort=THIRD_COHORT, plan_sha256=THIRD_PLAN_SHA)
            with self.assertRaisesRegex(ValueError, "CORPUS_DIVISION"):
                probe.load_corpus(manifest, pin, "holdout", **arguments)
            with (
                patch.object(probe, "HISTORICAL_MANIFEST_SHA", probe.sha(historical.read_bytes())),
                patch.object(probe, "SECOND_MANIFEST_SHA", probe.sha(second.read_bytes())),
                self.assertRaisesRegex(ValueError, "CORPUS_HOLDOUT_SOLAPADO"),
            ):
                probe.load_corpus(manifest, pin, "holdout", **arguments, second_manifest=second)

    def test_old_manifest_and_split_do_not_open_other_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "images").mkdir()
            rows = []
            for i, (name, identity, condition, split, stratum) in enumerate(probe.expected_rows()):
                source = f"00000000-0000-0000-0000-{int(identity[3:]):012x}"
                file = root / "images" / (name + ".png")
                if split == "calibration":
                    Image.new("RGB", (48, 36), (i, 0, 0)).save(file)
                rows.append(
                    {
                        "id": name,
                        "identity": identity,
                        "split": split,
                        "expected_faces": 1,
                        "width": 48,
                        "height": 36,
                        "path": "images/" + file.name,
                        "source_identity": source,
                        "source_member": "ControlFace10k/"
                        + "/".join(stratum)
                        + f"/identity-{source}/r0_g0_a25_{condition}_fixture.png",
                        "prepared_sha256": probe.sha(file.read_bytes())
                        if file.exists()
                        else f"{i:064x}",
                    }
                )
            manifest = root / "fixtures.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "synthetic": True,
                        "dataset": probe.DATASET,
                        "revision": probe.REVISION,
                        "central_sha256": probe.CENTRAL_SHA,
                        "license": "CC BY 4.0",
                        "fixtures": rows,
                    }
                ),
                encoding="utf-8",
            )
            fixtures = probe.load_corpus(manifest, probe.sha(manifest.read_bytes()), "calibration")
            self.assertEqual(len(fixtures), 36)
            self.assertEqual(len({fixture.identity for fixture in fixtures}), 12)
            self.assertEqual((fixtures[0].width, fixtures[0].height), (48, 36))
            with self.assertRaises(FileNotFoundError):
                probe.load_corpus(manifest, probe.sha(manifest.read_bytes()), "validation")

    def test_retry_only_after_503_and_emit_no_vectors(self):
        fixture = probe.Fixture("cf-01-o3", "cf-01", "o3", "0" * 64, 48, 36, b"image")
        calls = []

        def request(path, *, entries=None, image=None):
            if path == "/health":
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
                    "inference_profiles": {task: {"cuda_nodes": 2} for task in TASKS},
                }
            calls.append(entries["facial-recognition"]["detection"]["options"]["minScore"])
            if len(calls) == 1:
                raise HTTPError(path, 503, "fixture", None, None)
            return {
                "imageWidth": 48,
                "imageHeight": 36,
                "facial-recognition": [
                    {
                        "score": 0.7,
                        "boundingBox": {"x1": 1, "y1": 2, "x2": 48, "y2": 36},
                        "embedding": json.dumps([1] + [0] * 511),
                    }
                ],
            }

        with patch.object(probe.time, "sleep"):
            report = probe.measure([fixture], request, "calibration")
        self.assertEqual(calls, [0.6, 0.6])
        self.assertEqual(report["retries"], [{"id": fixture.name, "http_status": 503}])
        self.assertNotIn("embedding", json.dumps(report))
        self.assertEqual(report["runs"][-1]["counts"], {"one": 0, "zero": 1, "multiple": 0})
        self.assertEqual(len(report["runs"][0]["neighbor_graph"]), len(probe.THRESHOLDS))

    def test_graph_includes_impostor_edges_and_preserves_missed_pairs(self):
        graph = probe.neighbor_graph(
            [
                ("a1", "a", (1, 0)),
                ("a2", "a", (1, 0)),
                ("a3", "a", (1, 0)),
                ("b1", "b", (1, 0)),
                ("b2", "b", None),
            ],
            (0.305,),
        )[0]
        self.assertEqual(graph["genuine_edges"], 3)
        self.assertEqual(graph["impostor_edges"], 3)
        self.assertEqual(graph["mixed_components"], 1)
        self.assertEqual(graph["pure_three_view_identities"], [])
        self.assertEqual(graph["neighbors_per_fixture"]["a1"], {"genuine": 2, "impostor": 1})
        self.assertNotIn("b2", graph["neighbors_per_fixture"])
