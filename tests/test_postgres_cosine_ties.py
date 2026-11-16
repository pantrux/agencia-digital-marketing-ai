"""Derivación pura y regresión del empate coseno real; no ejecuta SQL."""

import hashlib
import importlib.util
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "docker/postgres-immich/acceptance.sql"


def helper():
    path = ROOT / "docker/postgres-immich/functional_cosine_ties.py"
    spec = importlib.util.spec_from_file_location("functional_cosine_ties", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CosineTies(unittest.TestCase):
    def test_sufijo_solo_lectura_es_identico_salvo_una_sustitucion(self):
        raw = ORIGINAL.read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "3615b4132cdbc7157fc766ad88c326b1832d132ea7a05e1de0c8bce59899885b",
        )
        suffix = raw.partition(b"\\endif")[2]
        self.assertEqual(
            hashlib.sha256(suffix).hexdigest(),
            "51610082ab0efbb1e58b1e2915ced0d8532b24201ff1624bd5446185b095138e",
        )
        derived = helper().derive_readonly_sql(raw)
        self.assertIs(type(derived), bytes)
        self.assertEqual(derived.count(b"OR NOT (q.id = ANY(ids))"), 1)
        self.assertEqual(
            hashlib.sha256(derived).hexdigest(),
            "2a4fe66f86681240dfcfec79fed79b9a4384accc9f99ebbf502521b8fa300889",
        )
        self.assertNotIn(b"OR ids[1] <> q.id", derived)
        self.assertEqual(
            derived.replace(b"OR NOT (q.id = ANY(ids))", b"OR ids[1] <> q.id", 1), suffix
        )
        self.assertEqual(ORIGINAL.read_bytes(), raw)
        self.assertNotIn(b"CREATE INDEX", derived)
        self.assertFalse(any(line.lstrip().startswith(b"\\") for line in derived.splitlines()))

    def test_entrada_alterada_y_tipo_mutable_rechazados(self):
        raw = ORIGINAL.read_bytes()
        for bad in (raw + b"\n", raw.replace(b"1e-6", b"1e-5", 1), bytearray(raw), None):
            with self.subTest(type=type(bad).__name__), self.assertRaises(ValueError):
                helper().derive_readonly_sql(bad)

    def test_q1_real_tiene_diez_ids_oraculo_pero_self_segundo(self):
        # Captura real b7a9d35c…; se conserva sólo la fixture pública, sin depender de .tools.
        ids = [3152, 1, 3448, 297, 2856, 3744, 593, 2560, 4040, 889]
        oracle_ids = [1, 3152, 3448, 297, 2856, 3744, 593, 2560, 4040, 889]
        distances = [
            1.8858187345038857e-7,
            0,
            4.7134675640325696e-7,
            5.654883419303758e-7,
            1.178351265784805e-6,
            1.7907195632993478e-6,
            2.639017506189667e-6,
            3.2050628571145268e-6,
            4.429393979155094e-6,
            5.654602003857256e-6,
        ]
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10)
        self.assertEqual(set(ids), set(oracle_ids))
        self.assertNotEqual(ids[0], 1)  # La regla anterior rechaza este resultado válido.
        self.assertIn(1, ids)  # La regla derivada acepta self sin exigir su posición.
        first_distance = 1.8858187345038857e-7
        self.assertTrue(math.isfinite(first_distance))
        self.assertLessEqual(first_distance, 1e-6)
        self.assertTrue(all(math.isfinite(value) for value in distances))
        self.assertTrue(
            all(b >= a - 1e-6 for a, b in zip(distances[:-1], distances[1:], strict=True))
        )
        oracle_threshold = 5.654602003857256e-6
        self.assertTrue(all(value <= oracle_threshold + 1e-6 for value in distances))
        derived = helper().derive_readonly_sql(ORIGINAL.read_bytes())
        self.assertIn(b"OR NOT (q.id = ANY(ids))", derived)
        self.assertIn(b"cardinality(ids) <> 10", derived)
        self.assertIn(b"count(DISTINCT x)", derived)
        self.assertIn(b"hit.distance < previous_distance - 1e-6", derived)
        self.assertIn(b"hit.distance > threshold + 1e-6", derived)
        self.assertIn(b"WHERE id = ids[1]) > 1e-6", derived)

    def test_ausencia_de_self_sigue_rechazada_con_diez_ids_unicos(self):
        ids = [3152, 99999, 3448, 297, 2856, 3744, 593, 2560, 4040, 889]
        self.assertEqual(len(set(ids)), 10)
        self.assertFalse(1 in ids)
        derived = helper().derive_readonly_sql(ORIGINAL.read_bytes())
        self.assertIn(b"OR NOT (q.id = ANY(ids))", derived)


if __name__ == "__main__":
    unittest.main()
