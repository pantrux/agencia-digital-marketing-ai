"""Pruebas del alta y de la reversión de Grafana con la API simulada."""

import importlib.util
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "grafana_datasource",
    Path(__file__).resolve().parents[1] / "docker/observability/grafana_datasource.py",
)
grafana = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grafana)


class GrafanaDatasourceTests(unittest.TestCase):
    def setUp(self):
        self.org_id = 7
        self.out = {"org_id": self.org_id, "conflicts": {"uid": False, "name": False}}
        self.client = MagicMock()
        self.marker = "test-owned"
        self.tls = {field: "pem-de-prueba" for field in grafana.TLS_FIELDS}
        self.row = {
            "id": 51,
            "orgId": self.org_id,
            "uid": grafana.UID,
            "name": grafana.UID,
            "url": grafana.PROMETHEUS_URL,
            "type": "prometheus",
            "access": "proxy",
            "isDefault": False,
            "jsonData": {
                "tlsAuth": True,
                "tlsAuthWithCACert": True,
                "tlsSkipVerify": False,
                grafana.MARKER: self.marker,
            },
            "secureJsonFields": dict.fromkeys(grafana.TLS_FIELDS, True),
        }
        self.posted = {"datasource": self.row}
        self.query = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"job": job}, "value": [1.0, "1"]} for job in sorted(grafana.JOBS)
                ],
            },
        }

    def apply(self):
        with patch.object(grafana.secrets, "token_hex", return_value=self.marker):
            grafana.apply(self.client, self.out, self.org_id, lambda: self.tls)

    def test_plan_solo_get_y_conflictos_independientes(self):
        self.client.request.side_effect = [
            {"version": grafana.GRAFANA_VERSION, "database": "ok"},
            {"id": self.org_id},
            self.row,
            None,
        ]
        grafana.plan(self.client, self.out)
        self.assertEqual(self.out["conflicts"], {"uid": True, "name": False})
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET"] * 4)

    def test_apply_configura_tls_y_verifica_los_dos_jobs(self):
        self.client.request.side_effect = [self.posted, self.row, {"status": "OK"}, self.query]
        self.apply()
        call = self.client.request.call_args_list[0]
        self.assertEqual(call.args, ("POST", "/api/datasources"))
        self.assertEqual(call.kwargs["payload"]["secureJsonData"], self.tls)
        self.assertIs(call.kwargs["payload"]["jsonData"]["tlsSkipVerify"], False)
        self.assertEqual(self.out["jobs_up"], sorted(grafana.JOBS))
        self.assertEqual(self.out["datasource_id"], 51)

    def test_conflicto_o_organizacion_distinta_rechaza_antes_de_tls_o_post(self):
        material = MagicMock()
        with self.assertRaisesRegex(grafana.Rejected, "ORGANIZACION_DISTINTA"):
            grafana.apply(self.client, self.out, 99, material)
        self.out["conflicts"]["name"] = True
        with self.assertRaisesRegex(grafana.Rejected, "OCUPADO"):
            grafana.apply(self.client, self.out, self.org_id, material)
        self.client.request.assert_not_called()
        material.assert_not_called()

    def test_error_promql_borra_solo_id_confirmado(self):
        self.client.request.side_effect = [
            self.posted,
            self.row,
            {"status": "OK"},
            {},
            self.row,
            {},
            None,
        ]
        with self.assertRaisesRegex(grafana.Rejected, "PROMQL"):
            self.apply()
        self.assertEqual(self.out["cleanup"], "removed")
        self.client.request.assert_any_call("DELETE", "/api/datasources/51", allowed=(200, 404))

    def test_post_ambiguo_reconcilia_marcador_propio(self):
        self.client.request.side_effect = [
            grafana.Rejected("GRAFANA_TRANSPORTE"),
            self.row,
            {},
            None,
        ]
        with self.assertRaisesRegex(grafana.Rejected, "TRANSPORTE"):
            self.apply()
        self.assertEqual(self.out["cleanup"], "removed")

    def test_post_en_carrera_preserva_fuente_ajena(self):
        foreign = dict(self.row, jsonData={grafana.MARKER: "otra-operacion"})
        self.client.request.side_effect = [grafana.Rejected("GRAFANA_HTTP_409"), foreign]
        with self.assertRaisesRegex(grafana.Rejected, "409"):
            self.apply()
        self.assertEqual(self.out["cleanup"], "preserved")
        self.assertEqual(
            [call.args[0] for call in self.client.request.call_args_list], ["POST", "GET"]
        )

    def test_http_rechaza_redireccion_sin_leer_cuerpo(self):
        with patch.object(grafana.http.client, "HTTPConnection") as constructor:
            connection = constructor.return_value
            reply = connection.getresponse.return_value.__enter__.return_value
            reply.status = 302
            client = grafana.Grafana("test-token-123456789")
            with self.assertRaisesRegex(grafana.Rejected, "GRAFANA_HTTP_302"):
                client.request("GET", "/api/org")
            constructor.assert_called_once_with("192.168.48.3", 3000, timeout=15)
            reply.read.assert_not_called()
            connection.close.assert_called_once()

    def test_token_acepta_un_final_de_linea_y_rechaza_inyeccion(self):
        token = b"test-token-123456789"
        self.assertEqual(grafana.read_token(BytesIO(token + b"\r\n")), token.decode())
        with self.assertRaisesRegex(grafana.Rejected, "GRAFANA_BEARER"):
            grafana.read_token(BytesIO(token + b"\n\n"))


if __name__ == "__main__":
    unittest.main()
