import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from sre import audit_studio_production_latency as audit
from sre import studio_production_metrics as metrics


class StudioProductionMetricsTests(unittest.TestCase):
    def test_coteja_resultados_multipart_en_esquema_real_y_publica_solo_agregados(self):
        now = 1_794_787_200
        migrations = Path(__file__).resolve().parents[1] / "cloudflare-saas/migrations"
        cases = (
            ("verified", "production", "real.example", True, "completed", None, 0),
            ("no-outbox", "production", "real.example", True, "completed", None, 0),
            ("wrong-sha", "production", "real.example", True, "aborted", "sha256_mismatch", 0),
            ("wrong-meta", "sandbox", "real.example", True, "aborted", "metadata_mismatch", 0),
            ("expired", "sandbox", "fake.invalid", True, "aborted", "expired", 0),
            ("cancelled", "production", "real.example", True, "aborted", "cancelled", 0),
            ("unknown", "production", "real.example", False, "aborted", None, 0),
            ("pending", "production", "real.example", True, "completing", None, 0),
            ("future", "production", "real.example", True, "completing", None, 1),
            ("old", "production", "real.example", True, "completing", None, -audit.WINDOW - 1),
        )
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            for migration in sorted(migrations.glob("*.sql")):
                db.executescript(migration.read_text(encoding="utf-8"))
            for key, mode, domain, portal, status, reason, offset in cases:
                epoch = now + offset
                db.execute(
                    "INSERT INTO users(id,google_sub,email) VALUES (?,?,?)",
                    (
                        key,
                        key,
                        f"private-canary@{domain}",
                    ),
                )
                db.execute(
                    """INSERT INTO orders (id,user_id,plan_id,kind,status,idempotency_key,
                    project_slug,request_sha256,queue_priority,provider_mode,created_at,portal_started_at)
                    VALUES (?,?,'free','production','queued',?,?,?,0,?,?,?)""",
                    (key, key, key, key, "a" * 64, mode, epoch, epoch if portal else None),
                )
                db.execute(
                    """INSERT INTO upload_sessions (id,user_id,order_id,object_key,sha256,bytes,
                    mime_type,extension,upload_id,status,object_etag,created_at,expires_at,abort_reason)
                    VALUES (?,?,?,?,?,20,'video/mp4','mp4','upload',?,'etag',?,?,?)""",
                    (
                        key,
                        key,
                        key,
                        f"ingest/{key}/asset.mp4",
                        "a" * 64,
                        status,
                        epoch,
                        epoch + 100,
                        reason,
                    ),
                )
            db.execute(
                """INSERT INTO usage_ledger(id,user_id,order_id,period_start,units,status,created_at)
                VALUES ('verified','verified','verified','2026-11-01',1,'reserved',?)""",
                (now,),
            )
            db.execute("INSERT INTO job_outbox(order_id,payload) VALUES ('verified','{}')")
            db.execute(
                "UPDATE upload_sessions SET direct_put_no_response_observed=1 WHERE id IN ('verified','future')"
            )
            db.execute(
                "UPDATE upload_sessions SET direct_put_403_observed=1 WHERE id IN ('verified','expired','old')"
            )
            uploads, signals = metrics.read_upload_window(
                lambda sql, params: [dict(row) for row in db.execute(sql, params)], now
            )

        nonzero = {
            cohort: {result: value for result, value in results.items() if value}
            for cohort, results in uploads.items()
        }
        self.assertEqual(
            nonzero,
            {
                "portal_produccion": {
                    "verificada": 1,
                    "completa_sin_despacho": 1,
                    "sha256_distinto": 1,
                    "cancelada": 1,
                    "pendiente": 1,
                },
                "portal_sandbox": {"metadatos_distintos": 1},
                "portal_ensayo": {"vencida": 1},
                "origen_sin_acreditar": {"motivo_desconocido": 1},
            },
        )
        self.assertEqual(signals["portal_produccion"], {"http_403": 1, "sin_respuesta": 1})
        self.assertEqual(signals["portal_ensayo"], {"http_403": 1, "sin_respuesta": 0})
        report = audit.build_report([], now, audit.signed_duration)
        payload = metrics.metrics_payload(report, 10_000, uploads=uploads, upload_signals=signals)
        raw = json.dumps(payload)
        self.assertNotIn("private-canary", raw)
        series = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        gauge = next(item for item in series if item["name"] == "studio_upload_30d_sessions")
        points = gauge["gauge"]["dataPoints"]
        self.assertEqual(len(points), 32)
        self.assertEqual(sum(int(point["asInt"]) for point in points), 8)
        signals_gauge = next(
            item for item in series if item["name"] == "studio_upload_30d_put_observed_sessions"
        )
        signal_points = signals_gauge["gauge"]["dataPoints"]
        self.assertEqual(len(signal_points), 8)
        self.assertEqual(sum(int(point["asInt"]) for point in signal_points), 3)

    def test_serializes_count_and_missing_percentile_without_customer_identifiers(self):
        sample = {
            "confirmed_at": 100,
            "portal_started_at": 98,
            "delivered_at": 349,
            "duration_ms": 60_000,
            "provider_mode": "sandbox",
            "reserved_test_domain": 1,
            "status": "completed",
            "manual_review": 0,
            "manual_adjustment": 0,
            "order_id": "PRIVATE_ID_CANARY",
        }
        assisted = {**sample, "manual_adjustment": 1, "delivered_at": 1815}
        report = audit.build_report([sample, assisted], 2000, audit.signed_duration)
        uploads, signals = metrics.read_upload_window(lambda *_: [], 2000)
        payload = metrics.metrics_payload(
            report, 2_001_000_000_000, uploads=uploads, upload_signals=signals
        )
        self.assertNotIn("PRIVATE_ID_CANARY", json.dumps(payload))
        scope = payload["resourceMetrics"][0]["scopeMetrics"][0]
        by_name = {entry["name"]: entry["gauge"]["dataPoints"] for entry in scope["metrics"]}
        p95_points = by_name["studio_delivery_30d_brief_p95_seconds"]
        self.assertEqual(len(p95_points), 1)
        self.assertEqual(p95_points[0]["asInt"], "249")
        self.assertEqual(p95_points[0]["timeUnixNano"], "2001000000000")
        counts = by_name["studio_delivery_30d_reel_samples"]
        values = {
            point["attributes"][0]["value"]["stringValue"]: point["asInt"] for point in counts
        }
        self.assertEqual(values["portal_ensayo"], "1")
        self.assertEqual(values["portal_produccion"], "0")
        assisted_points = by_name["studio_delivery_30d_completed_with_manual_review"]
        self.assertEqual(assisted_points[1]["asInt"], "1")

    def test_rejects_partial_prometheus_delivery(self):
        with self.assertRaisesRegex(ValueError, "OTLP_PARCIAL"):
            metrics.check_ack(200, b'{"partialSuccess":{"rejectedDataPoints":"1"}}')
        metrics.check_ack(200, b"{}")
        metrics.check_ack(200, b"")
