import unittest

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
