import hashlib
import hmac
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sre import backup_321_d1_clock as clock


class D1ClockTests(unittest.TestCase):
    key = bytes.fromhex("7b" * 32)

    def request(self, day, stamp):
        millis = int(stamp.timestamp()) * 1000
        body = f"studio-d1-clock-v1\n{day}\n{millis}\n".encode()
        signature = hmac.new(self.key, body, hashlib.sha256).hexdigest()
        return json.dumps({"day": day, "scheduled_ms": millis, "signature": signature}).encode()

    def test_accepts_the_same_chilean_time_in_winter_and_summer(self):
        for stamp in (datetime(2026, 7, 15, 7, tzinfo=UTC), datetime(2026, 11, 15, 6, tzinfo=UTC)):
            with self.subTest(stamp=stamp):
                day = stamp.date().isoformat()
                self.assertEqual(
                    clock.confirm_order(self.request(day, stamp), self.key, stamp), day
                )

    def test_rejects_wrong_signature_even_inside_the_window(self):
        stamp = datetime(2026, 11, 15, 6, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, "ORDEN_FIRMA"):
            clock.confirm_order(self.request("2026-11-15", stamp), b"bad" * 10, stamp)

    def test_rejects_a_signed_request_outside_its_date_and_time_window(self):
        stamp = datetime(2026, 11, 15, 6, tzinfo=UTC)
        for now in (
            stamp - timedelta(seconds=1),
            stamp + timedelta(minutes=13),
            stamp + timedelta(days=1),
        ):
            with self.subTest(now=now), self.assertRaisesRegex(ValueError, "ORDEN_VENTANA"):
                clock.confirm_order(self.request("2026-11-15", stamp), self.key, now)
        with self.assertRaisesRegex(ValueError, "ORDEN_VENTANA"):
            clock.confirm_order(
                self.request("2026-11-15", stamp + timedelta(hours=1)), self.key, stamp
            )

    def test_rejects_a_duplicate_json_field(self):
        stamp = datetime(2026, 11, 15, 6, tzinfo=UTC)
        raw = self.request("2026-11-15", stamp).replace(b"{", b'{"day":"2026-11-15",', 1)
        with self.assertRaises(ValueError):
            clock.confirm_order(raw, self.key, stamp)

    def test_espera_la_orden_hasta_el_minuto_08_en_ambos_horarios(self):
        for base in (datetime(2026, 7, 15, 7, tzinfo=UTC), datetime(2026, 11, 15, 6, tzinfo=UTC)):
            with self.subTest(base=base):
                self.assertIsNone(
                    clock.order_origin(None, self.key, base + timedelta(minutes=7, seconds=59))
                )
                self.assertEqual(
                    clock.order_origin(None, self.key, base + timedelta(minutes=8)),
                    "temporizador-local",
                )
                self.assertEqual(
                    clock.order_origin(None, self.key, base + timedelta(minutes=12)),
                    "temporizador-local",
                )
                with self.assertRaisesRegex(ValueError, "ORDEN_VENTANA"):
                    clock.order_origin(None, self.key, base + timedelta(minutes=13))

    def test_prioriza_la_orden_firmada_y_rechaza_una_firma_invalida(self):
        base = datetime(2026, 11, 15, 6, tzinfo=UTC)
        now = base + timedelta(minutes=9)
        self.assertEqual(
            clock.order_origin(self.request("2026-11-15", base), self.key, now), "r2-firmada"
        )
        with self.assertRaisesRegex(ValueError, "ORDEN_FIRMA"):
            clock.order_origin(self.request("2026-11-15", base), b"x" * 32, now)

    def test_arranque_tardio_respalda_el_dia_actual_sin_consultar_la_orden(self):
        profile = {"d1": {}}
        capture = SimpleNamespace(validate_request=lambda _: None)
        late = datetime(2026, 11, 21, 13, 37, tzinfo=UTC)
        with patch.object(clock, "produce_backup", return_value={"success": True}) as backup:
            self.assertEqual(
                clock.run_scheduled(late, lambda: profile, capture, None), {"success": True}
            )
        backup.assert_called_once_with(
            "2026-11-21", profile, capture, origin="temporizador-recuperacion"
        )

    def test_arranque_antes_de_las_tres_respeta_la_ventana(self):
        early = datetime(2026, 11, 21, 5, 59, tzinfo=UTC)
        with patch.object(clock, "produce_backup") as backup:
            self.assertEqual(
                clock.run_scheduled(early, None, None, None),
                {"success": True, "status": "antes-de-la-ventana"},
            )
        backup.assert_not_called()

    def test_muestra_el_codigo_seguro_de_captura_en_el_fallo_del_servicio(self):
        def fail_capture(_request, result):
            result.update(success=False, error="HTTP_API_503")

        with (
            patch.object(clock, "ROOT", Path("/captura-d1-sintetica")),
            patch.object(clock, "backup_request"),
            patch.object(clock.os.path, "lexists", return_value=False),
            self.assertRaisesRegex(ValueError, "CAPTURA_D1_FALLIDA_HTTP_API_503"),
        ):
            clock.produce_backup(
                "2026-11-15",
                {"credentials": {}, "d1": {}},
                SimpleNamespace(run_capture=fail_capture),
            )
