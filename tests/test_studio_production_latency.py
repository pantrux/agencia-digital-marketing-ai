import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from sre import audit_studio_production_latency as audit


def order(*, elapsed=200, duration=60_000, mode="production", test_domain=0, portal=90):
    return {
        "confirmed_at": 100,
        "portal_started_at": portal,
        "delivered_at": 100 + elapsed,
        "duration_ms": duration,
        "provider_mode": mode,
        "reserved_test_domain": test_domain,
        "status": "completed",
        "manual_review": 0,
        "manual_adjustment": 0,
    }


class StudioProductionLatencyTests(unittest.TestCase):
    def test_window_keeps_authenticated_trial_orders_out_of_production(self):
        rows = [
            order(elapsed=240),
            order(elapsed=943, mode="sandbox", test_domain=1),
            order(elapsed=420, mode="production", test_domain=1),
            order(elapsed=150, mode="sandbox"),
            order(elapsed=300, portal=None),
        ]
        report = audit.build_report(rows, 1200, audit.signed_duration)
        self.assertEqual(report["portal_autenticado"]["muestras_de_60_s"], 4)
        trial = report["cohortes"]["portal_ensayo"]
        self.assertEqual((trial["muestras_de_60_s"], trial["p95_observado_s"]), (2, 943))
        production = report["cohortes"]["portal_produccion"]
        self.assertEqual(production["muestras_de_60_s"], 1)
        self.assertEqual(production["p95_observado_s"], 240)
        self.assertEqual(production["p95_observado_s_desde_reserva"], 250)
        self.assertEqual(report["cohortes"]["portal_sandbox"]["p95_observado_s"], 150)
        self.assertEqual(report["cohortes"]["origen_sin_acreditar"]["p95_observado_s"], 300)

    def test_missing_duration_and_no_samples_are_not_zero_latency(self):
        report = audit.build_report([order(duration=None)], 1200, audit.signed_duration)
        production = report["cohortes"]["portal_produccion"]
        self.assertEqual(production["completados"], 1)
        self.assertEqual(production["completados_con_duracion_cotejada"], 0)
        self.assertEqual(production["muestras_de_60_s"], 0)
        self.assertIsNone(production["p95_observado_s"])
        self.assertIsNone(production["p95_observado_s_desde_reserva"])

    def test_includes_reels_one_frame_away_and_treats_360_as_late(self):
        rows = [order(elapsed=359, duration=60_033), order(elapsed=360, duration=59_967)]
        report = audit.build_report(rows, 1200, audit.signed_duration)
        production = report["cohortes"]["portal_produccion"]
        self.assertEqual(production["muestras_de_60_s"], 2)
        self.assertEqual(production["entregados_antes_de_360_s"], 1)
        self.assertEqual(production["p95_observado_s"], 360)

    def test_excluye_una_entrega_reanudada_aunque_la_guarda_manual_ya_no_exista(self):
        migrations = Path(__file__).resolve().parents[1] / "cloudflare-saas/migrations"
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            for migration in sorted(migrations.glob("*.sql")):
                db.executescript(migration.read_text(encoding="utf-8"))
            db.execute(
                "INSERT INTO users(id,google_sub,email) VALUES ('titular','google','a@e-scale.example')"
            )
            for key, duration in (("automatica", 235), ("recuperada", 2492)):
                db.execute(
                    """INSERT INTO orders (id,user_id,plan_id,kind,status,idempotency_key,
                       project_slug,request_sha256,queue_priority,provider_mode,created_at,portal_started_at)
                       VALUES (?,'titular','ilimitado','production','completed',?,?,?,0,'sandbox',900,900)""",
                    (key, key, key, "a" * 64),
                )
                db.execute(
                    """INSERT INTO production_briefs (order_id,user_id,objective,audience,tone,confirmed_at)
                       VALUES (?,'titular','Mostrar la colección','Público del sur','',1000)""",
                    (key,),
                )
                if key == "recuperada":
                    db.execute(
                        "INSERT INTO production_manual_reviews VALUES (?,1,?,1100)",
                        (key, "b" * 64),
                    )
                    db.execute(
                        """INSERT INTO production_manual_adjustments
                           (order_id,number,request_key,revision,review_sha256,instructions,requested_at)
                           VALUES (?,1,'solicitud',1,?,'Corregir apertura',1200)""",
                        (key, "b" * 64),
                    )
                    db.execute(
                        """UPDATE production_manual_adjustments
                           SET status='ready',result_sha256=?,resolved_at=1250 WHERE order_id=?""",
                        ("c" * 64, key),
                    )
                db.execute(
                    """INSERT INTO delivery_kits (id,order_id,user_id,provider_mode,title,bytes,sha256,
                       r2_etag,r2_version,created_at,duration_ms)
                       VALUES (?,?,'titular','sandbox','Piloto',20,?,'etag','v1',?,60000)""",
                    (key.ljust(36, "-"), key, "d" * 64, 1000 + duration),
                )
            rows = audit.read_window(
                lambda sql, params: [dict(row) for row in db.execute(sql, params)], 4000
            )
        self.assertEqual(
            [(row["manual_review"], row["manual_adjustment"]) for row in rows], [(0, 0), (0, 1)]
        )
        report = audit.build_report(rows, 4000, audit.signed_duration)["cohortes"]["portal_sandbox"]
        self.assertEqual(report["completados"], 2)
        self.assertEqual(report["completados_con_duracion_cotejada"], 2)
        self.assertEqual(report["completados_con_intervencion_manual"], 1)
        self.assertEqual(report["muestras_de_60_s"], 1)
        self.assertEqual(report["p95_observado_s"], 235)
        self.assertEqual(report["p95_observado_s_desde_reserva"], 335)
