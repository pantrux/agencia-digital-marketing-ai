"""Contrato portable y primitivas Linux reales, sin R2/NFS ni credenciales reales."""

import base64
import hashlib
import hmac
import importlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker/r2-sync-consumer"))
R = importlib.import_module("sync_runtime")

SECRET = b"k" * 32
NOW = 1_799_000_000


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


def envelope(snapshot="a" * 64, now_seconds=NOW):
    body = json.dumps(job(), separators=(",", ":")).encode()
    timestamp = str(now_seconds)
    message = f"v1\n{timestamp}\nPOST\n/v1/sync\n{hashlib.sha256(body).hexdigest()}".encode()
    return R.encode_envelope(
        body, timestamp, "v1=" + hmac.digest(SECRET, message, "sha256").hex(), snapshot
    )


class FakeRuntime:
    snapshot = "a" * 64
    hmac_secret = SECRET
    max_object_bytes = 1024
    credentials = None


class FakeOperation:
    def __init__(self, events, *, state="pending", is_new=True):
        self.events, self.state, self.is_new = events, state, is_new
        self.receipt = R.Publication(
            job()["job_id"],
            job()["asset_id"],
            "proyectos/proyecto_01/01_raw/11111111-1111-4111-8111-111111111111.mp4",
            3,
            job()["sha256"],
            1,
            2,
            True,
            False,
        )

    def reserve(self):
        self.events.append("reserve")
        return R.Reservation(
            self.state, self.is_new, self.receipt if self.state == "completed" else None
        )

    def reconcile(self, receipt):
        self.events.append("reconcile")

    def complete(self, receipt):
        self.events.append("complete")
        return receipt


class EngineTests(unittest.TestCase):
    def test_comando_worker_fijo_y_entorno_sin_secretos(self):
        with patch.dict(os.environ, {"SECRETO_AJENO": "no-heredar", "PYTHONPATH": "/ajeno"}):
            command = R.worker_command("/private/operator.json")
            environment = R.worker_environment()
        self.assertEqual(command[1:4], ["-I", "-B", "-S"])
        self.assertEqual(command[4], str(R.CODE_DIR / "sync_runtime.py"))
        self.assertEqual(command[5:], ["worker", "--config", "/private/operator.json"])
        self.assertNotIn("SECRETO_AJENO", environment)
        self.assertNotIn("PYTHONPATH", environment)

    def drive(self, *, state="pending", is_new=True, fail=False):
        events = []
        operation = FakeOperation(events, state=state, is_new=is_new)

        class Ledger:
            @contextmanager
            def operation(self, *args, **kwargs):
                events.append("lock")
                self_outer.assertEqual(kwargs["wait_seconds"], 0)
                try:
                    yield operation
                finally:
                    events.append("unlock")

        class Storage:
            def publish(self, value, chunks):
                events.append("publish")
                self_outer.assertEqual(b"".join(chunks), b"abc")
                if fail:
                    raise R.StorageError("ESCRITURA_INCOMPLETA")
                return operation.receipt

        class Chunks:
            def __iter__(self):
                return iter([b"abc"])

            def close(self):
                events.append("download-close")

        def download(*args, **kwargs):
            events.append("download")
            return Chunks()

        self_outer = self
        self.events = events
        out = R.process_envelope(
            FakeRuntime(),
            envelope(),
            ledger=Ledger(),
            storage_factory=lambda: Storage(),
            downloader=download,
            now_seconds=lambda: NOW,
        )
        return out, events

    def test_reserva_antes_de_descargar_y_cierra_fuente(self):
        result, events = self.drive()
        self.assertEqual(
            events,
            ["lock", "reserve", "download", "publish", "download-close", "complete", "unlock"],
        )
        self.assertEqual(
            set(result), {"status", "job_id", "asset_id", "relative_path", "bytes", "sha256"}
        )
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("device", result)

    def test_completed_devuelve_recibo_sin_descargar(self):
        result, events = self.drive(state="completed", is_new=False)
        self.assertEqual(events, ["lock", "reserve", "unlock"])
        self.assertEqual(result["sha256"], job()["sha256"])

    def test_pending_previo_reconcilia_antes_de_complete(self):
        _, events = self.drive(is_new=False)
        self.assertLess(events.index("reconcile"), events.index("complete"))

    def test_fallo_publicacion_cierra_iterador_y_no_completa(self):
        with self.assertRaises(R.RuntimeErrorCode):
            self.drive(fail=True)
        self.assertEqual(
            self.events, ["lock", "reserve", "download", "publish", "download-close", "unlock"]
        )

    def test_worker_reverifica_hmac_y_snapshot_sin_datos_privados(self):
        raw = json.loads(envelope())
        for key, value in (
            ("signature", "v1=" + "0" * 64),
            ("snapshot", "b" * 64),
            ("body", base64.b64encode(b"{}").decode()),
        ):
            changed = dict(raw)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(R.RuntimeErrorCode) as caught:
                R.decode_envelope(json.dumps(changed).encode(), FakeRuntime(), lambda: NOW)
            self.assertNotIn(value, str(caught.exception))

    def test_envelope_cerrado_y_acotado_sin_rutas_comandos(self):
        raw = json.loads(envelope())
        raw["config_path"] = "/otro"
        for body in (json.dumps(raw).encode(), b"x" * 8193, b'{"body":"a","body":"b"}'):
            with self.subTest(length=len(body)), self.assertRaises(R.RuntimeErrorCode):
                R.decode_envelope(body, FakeRuntime(), lambda: NOW)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.H = importlib.import_module("sync_http")

    def service(self, *, busy=False, ledger_error=None, delay=0, before_finish=None):
        calls = []

        class Supervisor:
            def __init__(self):
                self.busy = busy
                self.cancel = threading.Event()

            def run(self, raw, verified, *, client=None):
                calls.append(raw)
                time.sleep(delay)
                if before_finish:
                    before_finish()
                return {
                    "status": "completed",
                    "job_id": verified.job.job_id,
                    "asset_id": verified.job.asset_id,
                    "relative_path": verified.job.relative_path,
                    "bytes": verified.job.bytes,
                    "sha256": verified.job.sha256,
                }

        class Ledger:
            def check(self):
                if ledger_error:
                    raise R.LedgerError(ledger_error)

        return self.H.SyncService(
            FakeRuntime(), supervisor=Supervisor(), ledger=Ledger(), now_seconds=lambda: NOW
        ), calls

    def request(self, body=None, *, headers=None, path="/v1/sync", method="POST"):
        e = json.loads(envelope())
        raw = base64.b64decode(e["body"]) if body is None else body
        h = (
            [
                ("Host", "localhost"),
                ("Content-Length", str(len(raw))),
                ("timestamp", e["timestamp"]),
                ("signature", e["signature"]),
            ]
            if headers is None
            else headers
        )
        return (
            f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h) + "\r\n"
        ).encode() + raw

    def exchange(self, request, service, *, budget=1):
        client, server = socket.socketpair()
        client.settimeout(3)
        thread = threading.Thread(
            target=self.H.handle_connection, args=(server, service), kwargs={"read_seconds": budget}
        )
        thread.start()
        try:
            client.sendall(request)
            out = bytearray()
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                out.extend(chunk)
        finally:
            client.close()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        return bytes(out)

    def test_post_autentica_y_responde_sin_metadata_privada(self):
        service, calls = self.service()
        accepted = self.exchange(self.request(), service)
        self.assertTrue(accepted.startswith(b"HTTP/1.1 202 "))
        self.assertEqual(json.loads(accepted.split(b"\r\n\r\n", 1)[1]), {"status": "processing"})
        response = self.exchange_receipt(service)
        self.assertIn(b"Connection: close\r\n", response)
        body = json.loads(response.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(
            set(body), {"status", "job_id", "asset_id", "relative_path", "bytes", "sha256"}
        )
        self.assertNotIn(FakeRuntime.snapshot.encode(), response)
        self.assertEqual(len(calls), 1)

    def exchange_receipt(self, service, *, status=200):
        until = time.monotonic() + 2
        response = self.exchange(self.request(), service)
        while response.startswith(b"HTTP/1.1 202 ") and time.monotonic() < until:
            time.sleep(0.01)
            response = self.exchange(self.request(), service)
        self.assertTrue(response.startswith(f"HTTP/1.1 {status} ".encode()))
        return response

    def test_duplicados_te_y_oversize_nunca_inician_worker(self):
        e = json.loads(envelope())
        n = len(base64.b64decode(e["body"]))
        base = [
            ("Host", "localhost"),
            ("Content-Length", str(n)),
            ("timestamp", e["timestamp"]),
            ("signature", e["signature"]),
        ]
        for extra, code in (
            ([("Content-Length", str(n))], 400),
            ([("Timestamp", e["timestamp"])], 400),
            ([("Transfer-Encoding", "chunked")], 400),
        ):
            service, calls = self.service()
            response = self.exchange(self.request(headers=base + extra), service)
            self.assertTrue(response.startswith(f"HTTP/1.1 {code} ".encode()))
            self.assertFalse(calls)
        service, calls = self.service()
        response = self.exchange(
            self.request(body=b"", headers=[("Host", "localhost"), ("Content-Length", "4097")]),
            service,
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 413 "))
        self.assertFalse(calls)

    def test_firma_incorrecta_ruta_y_metodo_son_errores_fijos(self):
        e = json.loads(envelope())
        raw = base64.b64decode(e["body"])
        bad = self.request(
            headers=[
                ("Host", "localhost"),
                ("Content-Length", str(len(raw))),
                ("timestamp", e["timestamp"]),
                ("signature", "v1=" + "0" * 64),
            ]
        )
        for request, code in (
            (bad, 401),
            (self.request(path="/v1/sync?otro"), 404),
            (self.request(method="PUT"), 405),
        ):
            service, calls = self.service()
            response = self.exchange(request, service)
            self.assertTrue(response.startswith(f"HTTP/1.1 {code} ".encode()))
            self.assertFalse(calls)
            self.assertNotIn(b"<html", response)

    def test_health_ready_busy_sin_alarma_ni_diagnosticos(self):
        for busy, error, expected in (
            (False, None, {"ready": True, "busy": False}),
            (True, None, {"ready": False, "busy": True}),
            (False, "LOCK_TIMEOUT", {"ready": False, "busy": True}),
        ):
            service, _ = self.service(busy=busy, ledger_error=error)
            response = self.exchange(
                self.request(
                    body=b"", method="GET", path="/healthz", headers=[("Host", "localhost")]
                ),
                service,
            )
            self.assertTrue(response.startswith(b"HTTP/1.1 200 "))
            self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1]), expected)

    def test_cabeceras_lentas_cierra_socket_y_no_worker(self):
        service, calls = self.service()
        response = self.exchange(b"POST /v1/sync HTTP/1.1\r\nHost: local\r\n", service, budget=0.05)
        self.assertFalse(calls)
        self.assertEqual(response, b"")

    def test_timer_de_entrada_no_se_mantiene_durante_trabajo(self):
        service, calls = self.service(delay=0.15)
        response = self.exchange(self.request(), service, budget=0.05)
        self.assertTrue(response.startswith(b"HTTP/1.1 202 "))
        self.exchange_receipt(service)
        self.assertEqual(len(calls), 1)

    def test_worker_ocupado_503_retry_after(self):
        service, calls = self.service(busy=True)
        response = self.exchange(self.request(), service)
        self.assertTrue(response.startswith(b"HTTP/1.1 503 "))
        self.assertIn(b"Retry-After: 1\r\n", response)
        self.assertFalse(calls)

    def test_copia_lenta_sobrevive_al_cierre_y_el_reintento_devuelve_recibo(self):
        entered, release = threading.Event(), threading.Event()

        def wait_for_release():
            entered.set()
            release.wait(1)

        service, calls = self.service(before_finish=wait_for_release)
        try:
            self.assertTrue(self.exchange(self.request(), service).startswith(b"HTTP/1.1 202 "))
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.exchange(self.request(), service).startswith(b"HTTP/1.1 202 "))
            self.assertEqual(len(calls), 1)
        finally:
            release.set()

        response = self.exchange_receipt(service)
        self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1])["sha256"], job()["sha256"])
        self.assertEqual(len(calls), 1)

    def test_ocho_conexiones_y_novena_503_sin_listener_real(self):
        stop, release = threading.Event(), threading.Event()
        clients, accepted = [], []
        entered = []
        errors = []
        for _ in range(9):
            client, server = socket.socketpair()
            client.settimeout(3)
            self.addCleanup(client.close)
            self.addCleanup(server.close)
            clients.append(client)
            accepted.append(server)

        class Listener:
            def setsockopt(self, *_args):
                pass

            def bind(self, _address):
                pass

            def listen(self, count):
                self_outer.assertEqual(count, 8)

            def settimeout(self, _seconds):
                pass

            def accept(self):
                if accepted:
                    return accepted.pop(0), ("controlado", 0)
                stop.wait(0.01)
                raise TimeoutError

            def close(self):
                pass

        def hold(connection, _service):
            entered.append(connection)
            release.wait(3)

        runtime = SimpleNamespace(
            config=SimpleNamespace(listen_host="127.0.0.1", listen_port=8091),
            ledger=lambda: SimpleNamespace(check=lambda: None),
        )
        service = SimpleNamespace(
            supervisor=SimpleNamespace(cancel=threading.Event(), busy=False),
            wait_for_worker=lambda _deadline: False,
        )
        self_outer = self

        def run():
            try:
                self.H.serve(runtime, stop=stop)
            except BaseException as error:
                errors.append(error)

        with (
            patch.object(R, "guard_code"),
            patch.object(self.H, "SyncService", return_value=service),
            patch.object(self.H.socket, "socket", return_value=Listener()),
            patch.object(self.H, "handle_connection", side_effect=hold),
        ):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                response = clients[-1].recv(8192)
                self.assertTrue(response.startswith(b"HTTP/1.1 503 "))
                self.assertIn(b"Retry-After: 1\r\n", response)
                self.assertLessEqual(len(entered), 8)
            finally:
                stop.set()
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)

    def test_conflicto_persistente_devuelve_409_sin_datos_externos(self):
        service, _ = self.service()
        service.supervisor.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            R.RuntimeErrorCode("CONFLICT")
        )
        self.assertTrue(self.exchange(self.request(), service).startswith(b"HTTP/1.1 202 "))
        response = self.exchange_receipt(service, status=409)
        self.assertEqual(json.loads(response.split(b"\r\n\r\n", 1)[1]), {"error": "CONFLICT"})

    def test_cuerpo_incompleto_tambien_tiene_plazo_absoluto(self):
        service, calls = self.service()
        response = self.exchange(self.request()[:-1], service, budget=0.05)
        self.assertEqual(response, b"")
        self.assertFalse(calls)

    def test_malformed_framing_no_arranca_worker(self):
        e = json.loads(envelope())
        n = len(base64.b64decode(e["body"]))
        base = [
            ("Host", "localhost"),
            ("Content-Length", str(n)),
            ("timestamp", e["timestamp"]),
            ("signature", e["signature"]),
        ]
        cases = [
            self.request(headers=base + [("signature", e["signature"])]),
            self.request(headers=base + [("Expect", "100-continue")]),
            self.request().replace(
                f"Content-Length: {n}".encode(), f"Content-Length: 0{n}".encode()
            ),
            self.request() + b"GET /healthz HTTP/1.1\r\n\r\n",
        ]
        for request in cases:
            service, calls = self.service()
            response = self.exchange(request, service)
            self.assertTrue(response.startswith(b"HTTP/1.1 400 "))
            self.assertFalse(calls)


def configuration():
    proof = {"device": 1, "inode": 2, "marker_name": ".owner", "marker_sha256": "a" * 64}
    return {
        "schema_version": 1,
        "media_root": "/private/media",
        "media_proof": dict(proof),
        "state_root": "/private/state",
        "state_proof": dict(proof),
        "account_id": "a" * 32,
        "max_object_bytes": 1024,
        "hmac_secret_file": "/private/hmac",
        "r2_access_key_file": "/private/access",
        "r2_secret_key_file": "/private/secret",
        "listen_host": "127.0.0.1",
        "listen_port": 8091,
    }


class ConfigurationTests(unittest.TestCase):
    def test_aprovisionamiento_nas_es_explicito_y_compatible_con_esquema_anterior(self):
        old = configuration()
        self.assertFalse(R.parse_config(json.dumps(old).encode()).provision_on_nas)
        new = {**old, "schema_version": 2, "provision_on_nas": True}
        self.assertTrue(R.parse_config(json.dumps(new).encode()).provision_on_nas)
        for rejected in (
            {**new, "provision_on_nas": False},
            {**old, "schema_version": 2},
            {**old, "provision_on_nas": True},
        ):
            with self.assertRaises(R.RuntimeErrorCode):
                R.parse_config(json.dumps(rejected).encode())

    def test_config_cerrada_y_errores_sin_valores_privados(self):
        parsed = R.parse_config(json.dumps(configuration()).encode())
        self.assertEqual(parsed.max_object_bytes, 1024)
        self.assertEqual(repr(parsed), "RuntimeConfig(<redactado>)")
        changes = (
            {"command": "dato-privado"},
            {"media_root": "/private/../ajeno"},
            {"state_root": "//otro"},
            {"max_object_bytes": True},
            {"max_object_bytes": 0},
            {"listen_host": "127.000.0.1"},
            {"listen_port": 80},
            {"account_id": "A" * 32},
            {"media_proof": {"device": 1}},
        )
        for change in changes:
            value = configuration()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(R.RuntimeErrorCode) as caught:
                R.parse_config(json.dumps(value).encode())
            self.assertEqual(str(caught.exception), "CONFIG_INVALID")
        for raw in (b'{"schema_version":1,"schema_version":1}', b"\xff", b'{"x":NaN}'):
            with self.assertRaises(R.RuntimeErrorCode):
                R.parse_config(raw)


LINUX_PIDFD = (
    sys.platform.startswith("linux")
    and hasattr(os, "pidfd_open")
    and hasattr(signal, "pidfd_send_signal")
)


@unittest.skipUnless(
    LINUX_PIDFD, "Requiere Linux con PIDFD real; Windows no acredita señales ni permisos POSIX."
)
class LinuxRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agency-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.base.chmod(0o700)
        self.media = self.base / "media"
        self.state = self.base / "state"
        self.media.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.raw = self.media / "proyectos/proyecto_01/01_raw"
        self.raw.mkdir(parents=True)
        config = configuration()
        for label, root in (("media", self.media), ("state", self.state)):
            marker = root / ".owner"
            marker.write_bytes(label.encode())
            marker.chmod(0o600)
            info = root.stat()
            config[label + "_root"] = str(root)
            config[label + "_proof"] = {
                "device": info.st_dev,
                "inode": info.st_ino,
                "marker_name": marker.name,
                "marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
            }
        for field, data in (
            ("hmac_secret_file", SECRET),
            ("r2_access_key_file", b"accessFixture"),
            ("r2_secret_key_file", b"secretFixture"),
        ):
            path = self.base / field
            path.write_bytes(data)
            path.chmod(0o600)
            config[field] = str(path)
        self.config_path = self.base / "operator.json"
        self.config_path.write_text(json.dumps(config))
        self.config_path.chmod(0o600)
        self.runtime = R.load_runtime(str(self.config_path))

    def test_secretos_snapshot_rotacion_no_silenciosa_y_repr(self):
        raw = envelope(self.runtime.snapshot)
        self.assertEqual(
            R.decode_envelope(raw, self.runtime, lambda: NOW).job.job_id, job()["job_id"]
        )
        Path(self.runtime.config.hmac_secret_file).write_bytes(b"z" * 32)
        rotated = R.load_runtime(str(self.config_path))
        with self.assertRaises(R.RuntimeErrorCode):
            R.decode_envelope(raw, rotated, lambda: NOW)
        for secret in ("accessFixture", "secretFixture", self.runtime.snapshot, "k" * 32):
            self.assertNotIn(secret, repr(self.runtime))

    def test_nofollow_hardlink_permisos_y_archivo_excesivo(self):
        path = Path(self.runtime.config.r2_secret_key_file)
        path.chmod(0o644)
        with self.assertRaises(R.RuntimeErrorCode):
            R.load_runtime(str(self.config_path))
        path.chmod(0o600)
        linked = self.base / "hardlink"
        os.link(path, linked)
        with self.assertRaises(R.RuntimeErrorCode):
            R.load_runtime(str(self.config_path))
        linked.unlink()
        data = path.read_bytes()
        path.unlink()
        path.symlink_to(self.runtime.config.r2_access_key_file)
        with self.assertRaises(R.RuntimeErrorCode):
            R.load_runtime(str(self.config_path))
        path.unlink()
        path.write_bytes(data * 300)
        path.chmod(0o600)
        with self.assertRaises(R.RuntimeErrorCode):
            R.load_runtime(str(self.config_path))

    def test_serve_no_inicializa_estado_ausente(self):
        H = importlib.import_module("sync_http")
        with (
            patch.object(R, "guard_code"),
            patch.object(H.socket, "socket") as listener,
            self.assertRaises(R.LedgerError),
        ):
            H.serve(self.runtime)
        listener.assert_not_called()
        self.assertFalse((self.state / "sync-ledger.sqlite3").exists())

    def drive(self, downloader):
        return R.process_envelope(
            self.runtime,
            envelope(self.runtime.snapshot),
            downloader=downloader,
            now_seconds=lambda: NOW,
        )

    def test_estado_real_publicacion_y_replay_sin_red(self):
        self.runtime.ledger().initialize()
        result = self.drive(lambda *_args, **_kwargs: (chunk for chunk in (b"a", b"bc")))
        destination = self.raw / (job()["asset_id"] + ".mp4")
        self.assertEqual(destination.read_bytes(), b"abc")
        receipt = self.drive(lambda *_args, **_kwargs: self.fail("No debe descargar un completed"))
        self.assertEqual(receipt, result)

    def test_cancelacion_limpia_parcial_deja_pending_y_recupera(self):
        self.runtime.ledger().initialize()

        def cancelled(*_args, **_kwargs):
            yield b"a"
            raise R.WorkerCancelled()

        with self.assertRaises(R.WorkerCancelled):
            self.drive(cancelled)
        self.assertEqual(sorted(p.name for p in self.raw.iterdir()), [])
        # El pending durable permanece y el mismo trabajo se reconcilia al reintentar.
        receipt = self.drive(lambda *_args, **_kwargs: (chunk for chunk in (b"abc",)))
        self.assertEqual(receipt["status"], "completed")

    def test_busy_flock_sin_reservar_otro_trabajo(self):
        self.runtime.ledger().initialize()
        verified = R.decode_envelope(envelope(self.runtime.snapshot), self.runtime, lambda: NOW)
        with self.runtime.ledger().operation(
            job(), verified.fingerprint, max_object_bytes=1024, wait_seconds=0
        ):
            with self.assertRaises(R.RuntimeErrorCode) as caught:
                self.drive(lambda *_a, **_k: self.fail("No debe descargar"))
            self.assertEqual(caught.exception.code, "BUSY")

    def readonly_code(self):
        staging = tempfile.TemporaryDirectory(prefix="agency-code-test-", dir=Path.home())
        self.addCleanup(staging.cleanup)
        code = Path(staging.name) / "code"
        code.mkdir(mode=0o700)
        for name in R.CODE_FILES:
            path = code / name
            path.write_bytes((R.CODE_DIR / name).read_bytes())
            path.chmod(0o444)
        code.chmod(0o555)
        self.addCleanup(code.chmod, 0o700)
        return code

    def test_worker_cli_fijo_real_replay_y_slot_unico(self):
        self.runtime.ledger().initialize()
        expected = self.drive(lambda *_args, **_kwargs: (chunk for chunk in (b"abc",)))
        code = self.readonly_code()
        now = int(time.time())
        raw = envelope(self.runtime.snapshot, now)
        verified = R.decode_envelope(raw, self.runtime, lambda: now)
        entered, release = threading.Event(), threading.Event()
        native_watch = R._watch_owned
        result = []

        def delayed_watch(*args, **kwargs):
            entered.set()
            release.wait(3)
            return native_watch(*args, **kwargs)

        # El Python del sistema da permisos aptos, si también cumple la versión mínima.
        system_python = shutil.which("python3", path=os.defpath)
        worker_python = R.PYTHON
        if (
            system_python
            and subprocess.run(
                [
                    system_python,
                    "-I",
                    "-B",
                    "-S",
                    "-c",
                    "import sys; sys.exit(sys.version_info < (3, 12))",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            ).returncode
            == 0
        ):
            worker_python = Path(system_python).resolve()
        with patch.object(R, "CODE_DIR", code), patch.object(R, "PYTHON", worker_python):
            supervisor = R.WorkerSupervisor(self.runtime)

            def run():
                try:
                    result.append(supervisor.run(raw, verified))
                except BaseException as error:
                    result.append(error)

            with patch.object(R, "_watch_owned", delayed_watch):
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertTrue(supervisor.busy)
                    with self.assertRaises(R.RuntimeErrorCode) as caught:
                        supervisor.run(raw, verified)
                    self.assertEqual(caught.exception.code, "BUSY")
                finally:
                    release.set()
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [expected])
            self.assertFalse(supervisor.busy)

    def test_paquete_readonly_rechaza_archivo_ajeno(self):
        code = self.readonly_code()
        R.guard_code(code)
        code.chmod(0o700)
        extra = code / "ajeno.py"
        extra.write_bytes(b"# ajeno\n")
        extra.chmod(0o444)
        code.chmod(0o555)
        with self.assertRaises(R.RuntimeErrorCode):
            R.guard_code(code)

    def test_paquete_readonly_rechaza_ancestro_escribible_por_otros(self):
        code = self.readonly_code()
        code.parent.chmod(0o777)
        self.addCleanup(code.parent.chmod, 0o700)
        with self.assertRaises(R.RuntimeErrorCode):
            R.guard_code(code)


@unittest.skipUnless(
    LINUX_PIDFD, "Requiere procesos Linux y PIDFD reales; no se simula esta garantía en Windows."
)
class LinuxPidfdTests(unittest.TestCase):
    def child(self, source):
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c", source],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=R.worker_environment(),
            close_fds=True,
        )
        fd = os.pidfd_open(process.pid, 0)

        def cleanup():
            R._stop_owned(fd)
            process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            os.close(fd)

        self.addCleanup(cleanup)
        return process, fd

    def test_captura_salida_y_no_hereda_entorno_del_padre(self):
        with patch.dict(os.environ, {"PRIVATE_PARENT": "no-heredar"}):
            process, fd = self.child(
                "import os,sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data+b'|'+str(os.environ.get('PRIVATE_PARENT')).encode())"
            )
        out, err, reason, dead = R._watch_owned(process, fd, b"abc", threading.Event(), timeout=3)
        self.assertEqual((out, err, reason, dead), (b"abc|None", b"", None, True))

    def test_timeout_escalada_pidfd_y_proceso_ajeno_intacto(self):
        foreign, foreign_fd = self.child("import time; time.sleep(30)")
        process, fd = self.child(
            "import signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); sys.stdin.buffer.read(); time.sleep(30)"
        )
        start = time.monotonic()
        _, _, reason, dead = R._watch_owned(process, fd, b"abc", threading.Event(), timeout=0.2)
        self.assertEqual(reason, "TIMEOUT")
        self.assertTrue(dead)
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(R._pidfd_ready(foreign_fd))
        self.assertIsNone(foreign.poll())

    def test_salida_excesiva_combinada_detiene_worker(self):
        process, fd = self.child(
            "import sys,time; sys.stdin.buffer.read(); sys.stdout.write('a'*5000); sys.stdout.flush(); sys.stderr.write('b'*5000); sys.stderr.flush(); time.sleep(30)"
        )
        out, err, reason, dead = R._watch_owned(process, fd, b"abc", threading.Event(), timeout=3)
        self.assertEqual(reason, "WORKER_FAILED")
        self.assertTrue(dead)
        self.assertLessEqual(len(out) + len(err), 8192)

    def test_desconexion_cliente_cancela_y_cierra_worker_propio(self):
        process, fd = self.child("import sys,time; sys.stdin.buffer.read(); time.sleep(30)")
        client, server = socket.socketpair()
        self.addCleanup(server.close)
        client.close()
        _, _, reason, dead = R._watch_owned(
            process, fd, b"abc", threading.Event(), client=server, timeout=3
        )
        self.assertEqual(reason, "CANCELLED")
        self.assertTrue(dead)


if __name__ == "__main__":
    unittest.main()
