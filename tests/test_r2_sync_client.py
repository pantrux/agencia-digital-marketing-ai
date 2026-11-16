"""SigV4 y descarga acotada con respuestas controladas, sin R2 ni credenciales reales."""

import hashlib
import hmac
import importlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker/r2-sync-consumer"))
CLIENT = importlib.import_module("r2_client")


def job():
    return {
        "job_id": "22222222-2222-4222-8222-222222222222",
        "asset_id": "11111111-1111-4111-8111-111111111111",
        "project_slug": "proyecto_01",
        "extension": "mp4",
        "bytes": 3,
        "sha256": hashlib.sha256(b"abc").hexdigest(),
        "object_key": "ingest/22222222-2222-4222-8222-222222222222/11111111-1111-4111-8111-111111111111.mp4",
    }


CREDS = CLIENT.R2Credentials("a" * 32, "PUBLICTESTACCESS", "public-test-secret")


class Socket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class Response(io.BytesIO):
    def __init__(self, body=b"abc", *, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers if headers is not None else [("Content-Length", "3")]

    def getheaders(self):
        return self.headers


class Connection:
    def __init__(self, response):
        self.response = response
        self.sock = Socket()
        self.closed = False
        self.request_value = None

    def request(self, method, path, *, headers):
        self.request_value = (method, path, headers)

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ClientTests(unittest.TestCase):
    def test_vector_sigv4_con_referencia_independiente(self):
        request = CLIENT.sign_get(job(), 1024, CREDS, "20261106T010203Z")
        host = "a" * 32 + ".r2.cloudflarestorage.com"
        path = "/studio-media-bucket/" + job()["object_key"]
        empty = hashlib.sha256(b"").hexdigest()
        signed = "host;x-amz-content-sha256;x-amz-date"
        canonical = "\n".join(
            [
                "GET",
                path,
                "",
                f"host:{host}\nx-amz-content-sha256:{empty}\nx-amz-date:20261106T010203Z\n",
                signed,
                empty,
            ]
        )
        scope = "20261106/auto/s3/aws4_request"
        message = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                "20261106T010203Z",
                scope,
                hashlib.sha256(canonical.encode()).hexdigest(),
            ]
        )
        key = b"AWS4public-test-secret"
        for part in ("20261106", "auto", "s3", "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request.host, host)
        self.assertEqual(request.path, path)
        self.assertTrue(request.headers["Authorization"].endswith("Signature=" + expected))
        self.assertNotIn(expected, repr(request))
        with self.assertRaises(TypeError):
            request.headers["Authorization"] = "cambio"

    def test_credenciales_y_fecha_reales_no_normalizadas(self):
        for account, access, secret in (
            ("A" * 32, "a", "b"),
            ("a" * 32, "a\n", "b"),
            ("a" * 32, "a", " b"),
        ):
            with self.subTest(account=account), self.assertRaises(CLIENT.R2Error):
                CLIENT.R2Credentials(account, access, secret)
        for date in (
            "20260230T010203Z",
            "20261106T250203Z",
            "00000101T000000Z",
            "20261106T010203Z\n",
        ):
            with self.assertRaises(CLIENT.R2Error):
                CLIENT.sign_get(job(), 1024, CREDS, date)

    def test_fallo_criptografico_no_expone_mensaje_del_proveedor(self):
        with (
            patch.object(CLIENT.hmac, "digest", side_effect=RuntimeError("clave-falsa")),
            self.assertRaises(CLIENT.R2Error) as failure,
        ):
            CLIENT.sign_get(job(), 1024, CREDS, "20261106T010203Z")
        self.assertNotIn("clave-falsa", str(failure.exception))

    def test_descarga_chunked_por_lector_y_cierre(self):
        response = Response()
        connection = Connection(response)
        with patch.object(CLIENT, "_connect", return_value=connection):
            chunks = list(CLIENT.download(job(), 1024, CREDS, total_seconds=10, idle_seconds=2))
        self.assertEqual(b"".join(chunks), b"abc")
        self.assertTrue(connection.closed)
        self.assertTrue(response.closed)
        self.assertEqual(connection.request_value[0], "GET")
        self.assertTrue(all(0 < value <= 2 for value in connection.sock.timeouts))

    def test_http_y_cabeceras_ambiguas_se_rechazan_sin_seguir_redirect(self):
        cases = [
            Response(status=302, headers=[("Location", "https://otro/")]),
            Response(status=403),
            Response(headers=[]),
            Response(headers=[("Content-Length", "3"), ("content-length", "3")]),
            Response(headers=[("Content-Length", "3"), ("Transfer-Encoding", "chunked")]),
            Response(headers=[("Content-Length", "03")]),
            Response(headers=[("Content-Length", "4")]),
            Response(headers=[("Content-Length", "3"), ("Content-Encoding", "gzip")]),
        ]
        for response in cases:
            connection = Connection(response)
            with (
                self.subTest(status=response.status, headers=response.headers),
                patch.object(CLIENT, "_connect", return_value=connection),
                self.assertRaises(CLIENT.R2Error),
            ):
                list(CLIENT.download(job(), 1024, CREDS, total_seconds=10, idle_seconds=2))
            self.assertTrue(connection.closed)
            self.assertTrue(response.closed)

    def test_truncado_excesivo_y_excepcion_sanean_y_cierran(self):
        for response in (Response(b"ab"), Response(b"abcd")):
            connection = Connection(response)
            with (
                patch.object(CLIENT, "_connect", return_value=connection),
                self.assertRaises(CLIENT.R2Error),
            ):
                list(CLIENT.download(job(), 1024, CREDS, total_seconds=10, idle_seconds=2))
            self.assertTrue(connection.closed)
        with (
            patch.object(CLIENT, "_connect", side_effect=OSError("secreto-del-transporte")),
            self.assertRaises(CLIENT.R2Error) as failure,
        ):
            list(CLIENT.download(job(), 1024, CREDS, total_seconds=10, idle_seconds=2))
        self.assertNotIn("secreto", str(failure.exception))

    def test_cancelacion_del_iterador_cierra_la_conexion(self):
        response = Response()
        connection = Connection(response)
        with patch.object(CLIENT, "_connect", return_value=connection):
            iterator = CLIENT.download(job(), 1024, CREDS, total_seconds=10, idle_seconds=2)
            next(iterator)
            iterator.close()
        self.assertTrue(connection.closed)
        self.assertTrue(response.closed)

    def test_trabajo_capturado_no_cambia_la_clave_firmada(self):
        source = job()
        native = CLIENT.validate_job
        calls = 0

        def mutate_after_snapshot(value, limit):
            nonlocal calls
            result = native(value, limit)
            calls += 1
            if calls == 1:
                value["asset_id"] = "33333333-3333-4333-8333-333333333333"
                value["object_key"] = f"ingest/{value['job_id']}/{value['asset_id']}.mp4"
            return result

        connection = Connection(Response())
        with (
            patch.object(CLIENT, "validate_job", side_effect=mutate_after_snapshot),
            patch.object(CLIENT, "_connect", return_value=connection),
        ):
            self.assertEqual(
                b"".join(CLIENT.download(source, 1024, CREDS, total_seconds=10, idle_seconds=2)),
                b"abc",
            )
        self.assertEqual(connection.request_value[1], "/studio-media-bucket/" + job()["object_key"])

    def test_presupuesto_y_clock_invalido_rechazan(self):
        for total, idle in ((0, 1), (True, 1), (301, 1), (2, 3), (10, float("nan"))):
            with self.subTest(total=total), self.assertRaises(CLIENT.R2Error):
                list(CLIENT.download(job(), 1024, CREDS, total_seconds=total, idle_seconds=idle))
        connection = Connection(Response())
        with (
            patch.object(CLIENT, "_connect", return_value=connection),
            patch.object(CLIENT.time, "monotonic", side_effect=[0, 0, 11]),
            self.assertRaises(CLIENT.R2Error),
        ):
            list(CLIENT.download(job(), 1024, CREDS, total_seconds=10, idle_seconds=2))
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
