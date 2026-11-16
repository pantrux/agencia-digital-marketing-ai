"""Verifica metadatos de propiedad D1 con SQLite efímero, sin Cloudflare ni datos reales."""

import sqlite3
import unittest
from pathlib import Path

SQL = Path(__file__).resolve().parents[1] / "cloudflare-saas/migrations/0000_resource_owner.sql"


class D1ResourceOwner(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)

    def test_crea_solo_propiedad_y_repetir_conserva_el_registro_original(self):
        sql = SQL.read_text(encoding="utf-8")
        self.db.executescript(sql)
        first = self.db.execute("SELECT * FROM _agency_resource_owner").fetchall()
        self.db.executescript(sql)
        self.assertEqual(self.db.execute("SELECT * FROM _agency_resource_owner").fetchall(), first)
        self.assertEqual(first[0][0:4], (1, "agencia-digital-marketing-ai", "studio-saas-db", 1))
        self.assertEqual(
            self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
            [("_agency_resource_owner",)],
        )

    def test_una_segunda_identidad_no_puede_cumplir_el_contrato(self):
        self.db.executescript(SQL.read_text(encoding="utf-8"))
        for identifier, project in ((2, "agencia-digital-marketing-ai"), (1, "otro-proyecto")):
            with self.subTest(identifier=identifier, project=project):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(
                        "INSERT OR REPLACE INTO _agency_resource_owner (id, project, resource, schema_version) VALUES (?,?,?,?)",
                        (identifier, project, "studio-saas-db", 1),
                    )

    def test_tabla_ajena_no_se_borra_ni_se_adopta_por_el_nombre(self):
        self.db.execute("CREATE TABLE _agency_resource_owner (foreign_value TEXT)")
        self.db.execute("INSERT INTO _agency_resource_owner VALUES ('preservar')")
        self.db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            self.db.executescript(SQL.read_text(encoding="utf-8"))
        self.assertEqual(
            self.db.execute("SELECT * FROM _agency_resource_owner").fetchall(), [("preservar",)]
        )


if __name__ == "__main__":
    unittest.main()
