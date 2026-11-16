"""Contratos del proxy ML privado con HTTP local y sin acceso al puente GPU."""

import http.client
import importlib.util
import json
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "immich_ml_auth_proxy", ROOT / "docker/immich-ml-auth/proxy.py"
)
PROXY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROXY
SPEC.loader.exec_module(PROXY)
HTTP_CONNECTION = http.client.HTTPConnection
TOKEN = "a" * 48
BODY = b'--test\r\nContent-Disposition: form-data; name="text"\r\n\r\nhola\r\n--test--\r\n'


class ControlledBackend(BaseHTTPRequestHandler):
    """Backend local que registra bytes y permite interrumpir una respuesta pendiente."""

    def do_GET(self):
        self.exchange()

    def do_POST(self):
        self.exchange()

    def log_message(self, *_arguments):
        pass

    def exchange(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.server.calls_changed:
            self.server.records.append((self.path, dict(self.headers), body))
            self.server.calls_changed.notify_all()
        self.server.entered.set()
        if self.server.mode == "wait":
            if select.select([self.connection], [], [], 3)[0]:
                if self.connection.recv(1) == b"":
                    self.server.disconnected.set()
            return
        payload = self.server.body
        if self.path == "/health":
            payload = json.dumps(self.server.health).encode()
        try:
            self.send_response(self.server.status)
            self.send_header("Content-Type", "application/json")
            if self.server.mode == "chunked":
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(payload)))
            self.send_header("Location", "http://127.0.0.1:1/never")
            self.send_header("Set-Cookie", "secreto-no-reenviable")
            self.end_headers()
            if self.server.mode == "partial":
                self.wfile.write(payload[:1])
                self.wfile.flush()
                self.server.body_started.set()
                if select.select([self.connection], [], [], 3)[0]:
                    if self.connection.recv(1) == b"":
                        self.server.disconnected.set()
                return
            if self.server.mode == "trickle":
                for byte in payload:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if select.select([self.connection], [], [], 0.04)[0]:
                        if self.connection.recv(1) == b"":
                            self.server.disconnected.set()
                            return
                return
            if self.server.mode == "chunked":
                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
            else:
                self.wfile.write(payload)
        except ConnectionError:
            # En Windows el cierre puede llegar como reset o como fin de flujo.
            self.server.disconnected.set()
        except OSError:
            pass


class HttpTests(unittest.TestCase):
    """Pruebas de extremo a extremo del adaptador, con dos sockets loopback propios."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.secret = Path(self.directory.name) / "secret"
        self.secret.write_text(TOKEN, encoding="ascii")
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), ControlledBackend)
        self.backend.daemon_threads = True
        self.backend.records = []
        self.backend.status = 200
        self.backend.body = json.dumps({"clip": json.dumps([0.25] * 512)}).encode()
        self.backend.mode = "normal"
        self.backend.health = {
            "ready": True,
            "cuda_available": True,
            "service": "immich-ml",
            "status": "ok",
        }
        self.backend.entered = threading.Event()
        self.backend.calls_changed = threading.Condition()
        self.backend.disconnected = threading.Event()
        self.backend.body_started = threading.Event()
        self._start(self.backend)
        self.connection_patch = patch.object(
            PROXY,
            "open_upstream",
            side_effect=lambda timeout: HTTP_CONNECTION(
                *self.backend.server_address, timeout=timeout
            ),
            create=True,
        )
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        limits = PROXY.Limits(
            request_bytes=2048,
            response_bytes=4096,
            connections=4,
            predictions=1,
            header_seconds=0.6,
            transfer_seconds=0.8,
            health_seconds=0.6,
            idle_seconds=0.5,
        )
        self.proxy = PROXY.create_server(self.secret, ("127.0.0.1", 0), limits)
        self._start(self.proxy)

    def _start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(3)

        self.addCleanup(stop)

    def request(self, method="POST", path="/predict", body=BODY, headers=None):
        connection = HTTP_CONNECTION(*self.proxy.server_address, timeout=3)
        try:
            connection.request(
                method,
                path,
                body,
                headers or {"Content-Type": "multipart/form-data; boundary=test"},
            )
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def wait_prediction_release(self):
        # El cliente puede leer todo el cuerpo antes de que el proxy cierre el upstream.
        self.assertTrue(self.proxy.predictions.acquire(timeout=1))
        self.proxy.predictions.release()

    def test_preserva_multipart_y_json512_sustituyendo_autorizacion(self):
        status, headers, body = self.request(
            headers={
                "Content-Type": "multipart/form-data; boundary=test",
                "Authorization": "Bearer ajeno",
                "Proxy-Authorization": "Bearer ignorado",
                "X-Forwarded-For": "otro",
                "X-Upstream": "http://host-ajeno",
                "Host": "host-ajeno",
            }
        )
        self.assertEqual((status, body), (200, self.backend.body))
        path, forwarded, original = self.backend.records[0]
        self.assertEqual((path, original), ("/predict", BODY))
        self.assertEqual(forwarded["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn("Proxy-Authorization", forwarded)
        self.assertNotIn("X-Forwarded-For", forwarded)
        self.assertNotIn("Location", headers)
        self.assertNotIn("Set-Cookie", headers)

    def test_ping_y_healthz_consultan_salud_real(self):
        for path in ("/ping", "/healthz"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path, None)
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["ready"])
        self.assertEqual([row[0] for row in self.backend.records], ["/health", "/health"])

    def test_health_no_inventa_disponibilidad_ni_cachea(self):
        self.assertEqual(self.request("GET", "/ping", None)[0], 200)
        self.backend.health["cuda_available"] = False
        self.assertEqual(self.request("GET", "/healthz", None)[0], 503)
        self.assertEqual(len(self.backend.records), 2)

    def test_preserva_error_upstream_sin_seguir_redireccion(self):
        for status in (307, 422, 503):
            self.backend.status = status
            result, _, body = self.request()
            self.assertEqual((result, body), (status, self.backend.body))
            self.wait_prediction_release()
        self.assertEqual(len(self.backend.records), 3)

    def test_rechaza_rutas_metodos_query_y_destinos_absolutos(self):
        for method, path, expected in (
            ("GET", "/predict", 405),
            ("POST", "/ping", 405),
            ("PUT", "/predict", 405),
            ("CONNECT", "host:443", 404),
            ("GET", "/", 404),
            ("GET", "/ping?url=otro", 404),
            ("POST", "http://host-ajeno/predict", 404),
            ("POST", "//predict", 404),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path)[0], expected)
        self.assertEqual(self.backend.records, [])

    def test_rechaza_entrada_demasiado_grande_antes_del_backend(self):
        self.assertEqual(self.request(body=b"a" * 2049)[0], 413)
        self.assertEqual(self.backend.records, [])

    def test_rechaza_tipo_y_framing_ambiguos(self):
        for headers, expected in (
            ({"Content-Type": "application/json"}, 415),
            ({"Content-Type": "multipart/form-data"}, 415),
            ({"Transfer-Encoding": "chunked"}, 400),
            ({"Expect": "100-continue"}, 417),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], expected)
        self.assertEqual(self.backend.records, [])

    def test_respuesta_chunked_se_preserva_y_se_acota(self):
        self.backend.mode = "chunked"
        self.assertEqual(self.request()[2], self.backend.body)
        self.wait_prediction_release()
        self.backend.body = b"x" * 4097
        self.assertEqual(self.request()[0], 502)

    def test_acota_concurrencia_y_libera_slot_tras_desconexion(self):
        self.backend.mode = "wait"
        client = self.start_pending_request()
        self.assertEqual(self.request()[0], 429)
        client.close()
        self.assertTrue(self.backend.disconnected.wait(1.5))
        self.wait_prediction_release()
        self.backend.mode = "normal"
        self.assertEqual(self.request()[0], 200)

    def start_pending_request(self):
        client = socket.create_connection(self.proxy.server_address, timeout=3)
        client.sendall(
            b"POST /predict HTTP/1.1\r\nHost: local\r\nContent-Type: multipart/form-data; boundary=test\r\n"
            + f"Content-Length: {len(BODY)}\r\n\r\n".encode()
            + BODY
        )
        self.addCleanup(client.close)
        self.assertTrue(self.backend.entered.wait(1))
        return client

    def test_desconexion_cierra_upstream_sin_esperar_plazo_completo(self):
        self.backend.mode = "wait"
        client = self.start_pending_request()
        started = time.monotonic()
        client.close()
        self.assertTrue(self.backend.disconnected.wait(0.6))
        self.assertLess(time.monotonic() - started, 0.7)

    def test_desconexion_cierra_upstream_durante_cuerpo_de_respuesta(self):
        self.backend.mode = "partial"
        client = self.start_pending_request()
        self.assertTrue(self.backend.body_started.wait(1))
        client.close()
        self.assertTrue(self.backend.disconnected.wait(0.4))

    def test_rechaza_content_length_duplicado_y_ausente(self):
        for length_headers, expected in (
            (b"Content-Length: 1\r\nContent-Length: 1\r\n", 400),
            (b"", 411),
        ):
            with socket.create_connection(self.proxy.server_address, timeout=2) as client:
                client.sendall(
                    b"POST /predict HTTP/1.1\r\nHost: local\r\n" + length_headers + b"\r\n"
                )
                self.assertIn(f" {expected} ".encode(), client.recv(4096).split(b"\r\n")[0])
        self.assertEqual(self.backend.records, [])

    def test_rechaza_cabeceras_excesivas(self):
        with socket.create_connection(self.proxy.server_address, timeout=2) as client:
            client.sendall(b"GET /ping HTTP/1.1\r\nX-Long: " + b"a" * 4100 + b"\r\n\r\n")
            self.assertIn(b" 431 ", client.recv(4096).split(b"\r\n")[0])
        self.assertEqual(self.backend.records, [])

    def test_limita_conexiones_sin_crear_mas_trabajadores(self):
        self.backend.mode = "wait"
        self.proxy.limits = replace(self.proxy.limits, health_seconds=2)
        clients = []
        try:
            for _ in range(4):
                client = socket.create_connection(self.proxy.server_address, timeout=2)
                clients.append(client)
                client.sendall(b"GET /ping HTTP/1.1\r\nHost: local\r\n\r\n")
            with self.backend.calls_changed:
                self.assertTrue(
                    self.backend.calls_changed.wait_for(
                        lambda: len(self.backend.records) == 4, timeout=1
                    )
                )
            self.assertEqual(self.request("GET", "/ping", None)[0], 503)
        finally:
            for client in clients:
                client.close()

    def test_no_envia_salud_si_cliente_cierra_cabeceras_incompletas(self):
        with socket.create_connection(self.proxy.server_address, timeout=2) as client:
            client.sendall(b"GET /ping HTTP/1.1\r\nHost: local\r\n")
            client.shutdown(socket.SHUT_WR)
            client.recv(4096)
        self.assertEqual(self.backend.records, [])

    def test_plazo_total_interrumpe_upstream(self):
        self.backend.mode = "wait"
        started = time.monotonic()
        self.assertEqual(self.request()[0], 504)
        self.assertTrue(self.backend.disconnected.wait(1))
        self.assertLess(time.monotonic() - started, 1.6)

    def test_plazo_total_no_se_renueva_por_respuesta_gota_a_gota(self):
        self.backend.mode = "trickle"
        started = time.monotonic()
        self.assertEqual(self.request()[0], 504)
        self.assertTrue(self.backend.disconnected.wait(1))
        self.assertLess(time.monotonic() - started, 1.6)

    def test_fallo_conexion_upstream_es_error_saneado(self):
        with patch.object(PROXY, "open_upstream", side_effect=OSError(TOKEN)):
            status, _, body = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn(TOKEN.encode(), body)

    def test_formdata_del_node_real_admite_el_transporte_del_cliente(self):
        script = """
        const form = new FormData();
        form.append('entries', JSON.stringify({clip:{textual:{modelName:'ViT-B-32__openai'}}}));
        form.append('text', 'control público');
        const response = await fetch(process.argv[1] + '/predict', {method:'POST',body:form});
        console.log(response.status);
        """
        url = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, url],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "200")
        self.assertIn(b'name="entries"', self.backend.records[0][2])


class SecretTests(unittest.TestCase):
    """El secreto debe existir en un archivo regular y nunca proceder del entorno."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bridge-secret"

    def test_lee_unico_token_desde_archivo(self):
        self.path.write_bytes(b"a" * 48 + b"\n")
        self.assertEqual(PROXY.load_secret(self.path), "a" * 48)

    def test_falla_si_secreto_ausente(self):
        with self.assertRaises(ValueError):
            PROXY.load_secret(self.path)

    def test_rechaza_secreto_que_no_es_archivo(self):
        with self.assertRaises(ValueError):
            PROXY.load_secret(Path(self.directory.name))

    def test_rechaza_token_vacio_corto_o_con_inyeccion(self):
        for value in (b"", b"corto", b"a" * 48 + b"\r\nX-Injected: si", b"CHANGE_ME" * 8):
            with self.subTest(length=len(value)):
                self.path.write_bytes(value)
                with self.assertRaises(ValueError):
                    PROXY.load_secret(self.path)

    def test_rechaza_ruta_relativa(self):
        with self.assertRaises(ValueError):
            PROXY.load_secret(Path("secreto-relativo"))

    def test_rechaza_secreto_mayor_que_limite(self):
        self.path.write_bytes(b"a" * 4097)
        with self.assertRaises(ValueError):
            PROXY.load_secret(self.path)

    def test_destino_literal_no_procede_del_cliente(self):
        with patch.object(PROXY.http.client, "HTTPConnection") as connection:
            PROXY.open_upstream(2.5)
        connection.assert_called_once_with("192.168.0.6", 3003, timeout=2.5)


if __name__ == "__main__":
    unittest.main()
