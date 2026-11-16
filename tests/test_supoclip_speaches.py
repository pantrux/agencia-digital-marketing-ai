"""Cliente stdlib controlado y compatibilidad con conversor AGPL fijado, sin GPU."""

import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/supoclip"))
import speaches_client as CLIENT  # noqa: E402 - carpeta local fija de la unidad.
import speaches_contract as C  # noqa: E402


def response():
    words = [
        {"start": 2, "end": 2.5, "word": " Vídeo", "probability": 0.75},
        {"start": 2.5, "end": 3, "word": " útil.", "probability": 0.9},
    ]
    return {
        "task": "transcribe",
        "language": "es",
        "duration": 4,
        "text": "Vídeo útil.",
        "words": copy.deepcopy(words),
        "segments": [{"start": 2, "end": 3, "text": " Vídeo útil.", "words": words}],
    }


def upstream_converter():
    path = ROOT / "tests/fixtures/supoclip-transcription-pure-f6.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(x for x in tree.body if isinstance(x, ast.FunctionDef))
    assert (
        hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
        == "e89eacdfb96606e9be7d07ada32e51586dbb60e4813bee0872908dca52dd8a53"
    )
    spec = importlib.util.spec_from_file_location("supoclip_pure_agpl_fixture", path)
    module = importlib.util.module_from_spec(spec)
    module.TRANSCRIPT_CACHE_SCHEMA_VERSION = 37
    spec.loader.exec_module(module)
    assert hashlib.sha256(module.AGPL_LICENSE_TEXT.encode()).hexdigest() == (
        "2d57b88304ae76aa40722515c6f740ee8f6473e9d057c3e29b8ba08eeebd4e38"
    )
    return module._whisper_result_to_transcript_data


class ContractTests(unittest.TestCase):
    def test_enteros_y_float_segundos_llegan_a_cache_ms_original_sin_modelos(self):
        convert = upstream_converter()
        raw = response()
        self.assertEqual(convert(raw)["words"][0]["start"], 2)  # Rojo histórico upstream.
        normalized = C.normalize_verbose(raw)
        self.assertIs(type(normalized["words"][0]["start"]), float)
        cache = convert(normalized)
        self.assertEqual([w["start"] for w in cache["words"]], [2000, 2500])
        self.assertEqual([w["end"] for w in cache["words"]], [2500, 3000])
        self.assertEqual([w["confidence"] for w in cache["words"]], [0.75, 0.9])
        self.assertEqual(cache["version"], 37)
        self.assertEqual(cache["text"], "Vídeo útil.")
        self.assertEqual(len(cache["utterances"]), 1)
        self.assertEqual(raw, response())

    def test_no_inventa_probabilidad_segmentos_ni_palabras(self):
        for changed in ("probability", "segments", "words-mismatch"):
            raw = response()
            if changed == "probability":
                raw["segments"][0]["words"][0].pop("probability")
                raw["words"][0].pop("probability")
            elif changed == "segments":
                raw.pop("segments")
            else:
                raw["words"][0]["word"] = " Otro"
            with self.subTest(changed=changed), self.assertRaises(C.SpeachesError):
                C.normalize_verbose(raw)

    def test_tiempos_invalidos_orden_y_probabilidad(self):
        for key, value in (
            ("start", True),
            ("start", -1),
            ("end", float("inf")),
            ("end", 5),
            ("probability", None),
            ("probability", 1.1),
        ):
            raw = response()
            raw["words"][0][key] = value
            raw["segments"][0]["words"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(C.SpeachesError):
                C.normalize_verbose(raw)

    def test_silencio_real_y_unicode_se_conservan(self):
        raw = {
            "task": "transcribe",
            "language": "es",
            "duration": 2,
            "text": "",
            "words": [],
            "segments": [],
        }
        result = C.normalize_verbose(raw)
        self.assertEqual(result["words"], [])
        self.assertEqual(result["segments"], [])
        raw["text"] = "habla sin palabras"
        with self.assertRaises(C.SpeachesError):
            C.normalize_verbose(raw)

    def test_json_acotado_duplicados_nan_utf8_y_control(self):
        bad = [
            b'{"text":"a","text":"b"}',
            b'{"n":NaN}',
            b'{"n":1e999}',
            b"\xff",
            b'{"a":"\\u0000"}',
            b"x" * (C.MAX_RESPONSE_BYTES + 1),
        ]
        for raw in bad:
            with self.subTest(length=len(raw)), self.assertRaises(C.SpeachesError):
                C.parse_verbose(raw)
        self.assertEqual(
            C.parse_verbose(json.dumps(response(), ensure_ascii=False).encode())["text"],
            "Vídeo útil.",
        )


class Wire:
    """HTTP real sobre socketpair propio; ningún listener ni dirección del lab."""

    def __init__(
        self,
        *,
        status=200,
        headers=None,
        body=None,
        hold=False,
        truncated=False,
        pace=0,
        after_request=None,
        header_delay=0,
        status_tail_delay=0,
    ):
        self.body = json.dumps(response(), ensure_ascii=False).encode() if body is None else body
        self.headers = (
            headers
            if headers is not None
            else [("Content-Type", "application/json"), ("Content-Length", str(len(self.body)))]
        )
        self.status, self.hold, self.truncated = status, hold, truncated
        self.pace, self.after_request = pace, after_request
        self.header_delay = header_delay
        self.status_tail_delay = status_tail_delay
        self.client, self.server = socket.socketpair()
        self.server.settimeout(2)
        self.started = threading.Event()
        self.sent_headers = threading.Event()
        self.release = threading.Event()
        self.request = b""
        self.error = None
        self.connection = None
        self.thread = threading.Thread(target=self._serve, name="speaches-fixture")

    def _serve(self):
        try:
            while b"\r\n\r\n" not in self.request:
                block = self.server.recv(65536)
                if not block:
                    return
                self.request += block
            head, body = self.request.split(b"\r\n\r\n", 1)
            length = int(
                next(
                    x.split(b":", 1)[1]
                    for x in head.split(b"\r\n")[1:]
                    if x.lower().startswith(b"content-length:")
                )
            )
            while len(body) < length:
                block = self.server.recv(65536)
                if not block:
                    return
                body += block
            self.request = head + b"\r\n\r\n" + body
            if self.after_request:
                self.after_request()
            self.started.set()
            header = f"HTTP/1.1 {self.status} Fixture\r\n".encode()
            header += b"".join(f"{k}: {v}\r\n".encode() for k, v in self.headers)
            if self.header_delay and self.release.wait(self.header_delay):
                return
            if self.status_tail_delay:
                self.server.sendall(header[:1])
                if self.release.wait(self.status_tail_delay):
                    return
                header = header[1:]
            self.server.sendall(header + b"Connection: close\r\n\r\n")
            self.sent_headers.set()
            if self.hold:
                self.release.wait(2)
            elif self.pace:
                for octet in self.body:
                    if self.release.wait(self.pace):
                        return
                    self.server.sendall(bytes([octet]))
            else:
                self.server.sendall(self.body[:-1] if self.truncated else self.body)
        except (OSError, StopIteration, ValueError) as error:
            self.error = type(error).__name__
        finally:
            self.server.close()

    def connect(self, deadline):
        wire = self

        class Connection(CLIENT._FixedConnection):
            def connect(self):
                self.sock = wire.client
                deadline.attach(wire.client)
                wire.client.settimeout(deadline.check())

        self.connection = Connection(deadline)
        return self.connection

    def __enter__(self):
        self.thread.start()
        self.patcher = patch.object(CLIENT, "_connect", self.connect)
        self.patcher.start()
        return self

    def __exit__(self, *_args):
        self.patcher.stop()
        self.release.set()
        self.client.close()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("El fixture dejó un hilo propio vivo")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audio = Path(self.directory.name) / "fixture.wav"
        self.audio.write_bytes(b"audio-sintetico" * 10000)
        self.token = "TEST_ONLY_NOT_A_CREDENTIAL_" * 2

    @contextmanager
    def token_mock(self):
        with patch.object(CLIENT, "_read_token", return_value=self.token):
            yield

    def call(self, **kwargs):
        return CLIENT.transcribe_audio(self.audio, "token-fixture", **kwargs)

    def test_multipart_http_real_fijo_sin_proxy_cookie_y_audio_por_chunks(self):
        with (
            self.token_mock(),
            Wire() as wire,
            patch.dict(
                os.environ,
                {"HTTP_PROXY": "http://invalid.example:1", "ALL_PROXY": "http://invalid.example:2"},
            ),
        ):
            result = self.call(language="es")
        header, body = wire.request.split(b"\r\n\r\n", 1)
        self.assertTrue(header.startswith(b"POST /v1/audio/transcriptions HTTP/1.1\r\n"))
        self.assertIn(b"Host: 192.168.0.6:8000\r\n", header)
        self.assertIn(b"Authorization: Bearer " + self.token.encode(), header)
        self.assertNotIn(b"Cookie:", header)
        self.assertNotIn(b"Proxy-", header)
        self.assertIn(self.audio.read_bytes(), body)
        for name, value in [
            ("model", CLIENT.MODEL),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
            ("stream", "false"),
            ("language", "es"),
        ]:
            self.assertIn(f'name="{name}"\r\n\r\n{value}\r\n'.encode(), body)
        self.assertNotIn(str(self.audio).encode(), body)
        self.assertEqual(result["words"][0]["start"], 2.0)
        self.assertEqual(wire.client.fileno(), -1)
        self.assertIsNone(wire.error)

    def test_idle_timeout_cierra_socket_y_no_se_clasifica_como_archivo_invalido(self):
        with self.token_mock(), Wire(hold=True) as wire:
            with self.assertRaises(C.SpeachesError) as caught:
                self.call(total_seconds=0.3, idle_seconds=0.05)
            self.assertEqual(caught.exception.code, "SPEACHES_TIMEOUT")
            self.assertEqual(wire.client.fileno(), -1)

    def test_espera_primer_byte_ampliada_es_opt_in(self):
        with self.token_mock(), Wire(header_delay=0.35):
            with self.assertRaises(C.SpeachesError) as caught:
                self.call(total_seconds=3, idle_seconds=0.15)
            self.assertEqual(caught.exception.code, "SPEACHES_TIMEOUT")
        with self.token_mock(), Wire(header_delay=0.35) as wire:
            result = self.call(total_seconds=3, idle_seconds=0.15, first_byte_seconds=1.5)
            self.assertEqual(result["text"], "Vídeo útil.")
            self.assertEqual(wire.client.fileno(), -1)

    def test_idle_aplica_desde_primer_byte_aun_con_status_incompleto(self):
        with self.token_mock(), Wire(status_tail_delay=0.35) as wire:
            with self.assertRaises(C.SpeachesError) as caught:
                self.call(total_seconds=3, idle_seconds=0.15, first_byte_seconds=1.5)
            self.assertEqual(caught.exception.code, "SPEACHES_TIMEOUT")
            self.assertFalse(wire.sent_headers.is_set())
            self.assertEqual(wire.client.fileno(), -1)

    def test_total_acota_espera_inicial_mas_larga(self):
        with self.token_mock(), Wire(header_delay=0.75) as wire:
            with self.assertRaises(C.SpeachesError) as caught:
                self.call(total_seconds=0.35, idle_seconds=0.15, first_byte_seconds=1.5)
            self.assertEqual(caught.exception.code, "SPEACHES_TIMEOUT")
            self.assertFalse(wire.sent_headers.is_set())
            self.assertEqual(wire.client.fileno(), -1)

    def test_cancelacion_en_espera_inicial_cierra_socket(self):
        cancel = threading.Event()
        waiting = threading.Event()
        select_socket = CLIENT.select.select

        def observe_wait(*args):
            waiting.set()
            return select_socket(*args)

        with (
            self.token_mock(),
            Wire(header_delay=1) as wire,
            patch.object(CLIENT.select, "select", side_effect=observe_wait),
        ):
            thread = threading.Thread(target=lambda: (waiting.wait(1), cancel.set()))
            thread.start()
            try:
                with self.assertRaises(C.SpeachesError) as caught:
                    self.call(
                        total_seconds=3,
                        idle_seconds=0.15,
                        first_byte_seconds=1.5,
                        cancel_event=cancel,
                    )
            finally:
                thread.join(2)
            self.assertTrue(waiting.is_set())
            self.assertFalse(thread.is_alive())
            self.assertEqual(caught.exception.code, "SPEACHES_CANCELLED")
            self.assertEqual(wire.client.fileno(), -1)

    def test_cancelacion_tras_headers_cierra_socket_retenido_y_archivo(self):
        cancel = threading.Event()
        captured = []
        original = CLIENT._file
        foreign_a, foreign_b = socket.socketpair()
        self.addCleanup(foreign_a.close)
        self.addCleanup(foreign_b.close)

        @contextmanager
        def capture(*args, **kwargs):
            with original(*args, **kwargs) as opened:
                captured.append(opened[0])
                yield opened

        with self.token_mock(), Wire(hold=True) as wire, patch.object(CLIENT, "_file", capture):
            timer = threading.Thread(target=lambda: (wire.sent_headers.wait(1), cancel.set()))
            timer.start()
            with self.assertRaises(C.SpeachesError) as caught:
                self.call(cancel_event=cancel, total_seconds=1, idle_seconds=0.5)
            timer.join(2)
            self.assertEqual(caught.exception.code, "SPEACHES_CANCELLED")
            self.assertEqual(wire.client.fileno(), -1)
            self.assertTrue(all(x.closed for x in captured))
            foreign_a.sendall(b"centinela-ajeno")
            self.assertEqual(foreign_b.recv(64), b"centinela-ajeno")
        self.assertFalse(any(t.name == "speaches-deadline" for t in threading.enumerate()))

    def test_plazo_absoluto_y_cancelacion_previa_no_inician_otro_transporte(self):
        cancel = threading.Event()
        cancel.set()
        with self.token_mock(), patch.object(CLIENT, "_connect") as connect:
            with self.assertRaises(C.SpeachesError) as caught:
                self.call(cancel_event=cancel)
            self.assertEqual(caught.exception.code, "SPEACHES_CANCELLED")
            connect.assert_not_called()
        before = time.monotonic()
        with self.token_mock(), Wire(hold=True) as wire:
            with self.assertRaises(C.SpeachesError) as caught:
                self.call(total_seconds=0.08, idle_seconds=0.08)
            self.assertEqual(caught.exception.code, "SPEACHES_TIMEOUT")
            self.assertEqual(wire.client.fileno(), -1)
        self.assertLess(time.monotonic() - before, 1)

    def test_respuesta_lenta_con_progreso_no_extiende_plazo_absoluto(self):
        before = time.monotonic()
        with self.token_mock(), Wire(pace=0.01):
            with self.assertRaises(C.SpeachesError) as caught:
                self.call(total_seconds=0.15, idle_seconds=0.1)
            self.assertEqual(caught.exception.code, "SPEACHES_TIMEOUT")
        self.assertLess(time.monotonic() - before, 1)

    def test_estados_error_sin_seguir_redirect_ni_filtrar_cuerpo(self):
        codes = {
            301: "REDIRECT_REJECTED",
            401: "AUTH_REJECTED",
            413: "AUDIO_TOO_LARGE",
            429: "BUSY",
            500: "UPSTREAM_REJECTED",
        }
        for status, code in codes.items():
            with (
                self.subTest(status=status),
                self.token_mock(),
                Wire(status=status, body=self.token.encode()),
            ):
                with self.assertRaises(C.SpeachesError) as caught:
                    self.call()
                self.assertEqual(str(caught.exception), "SPEACHES_" + code)
                self.assertNotIn(self.token, repr(caught.exception))

    def test_framing_respuesta_duplicado_compresion_te_oversize_y_truncado(self):
        raw = json.dumps(response()).encode()
        invalid = [
            [
                ("Content-Length", "1"),
                ("Content-Length", "1"),
                ("Content-Type", "application/json"),
            ],
            [
                ("Content-Length", str(C.MAX_RESPONSE_BYTES + 1)),
                ("Content-Type", "application/json"),
            ],
            [("Content-Length", "01"), ("Content-Type", "application/json")],
            [("Content-Length", str(len(raw))), ("Content-Type", "text/html")],
            [
                ("Content-Length", str(len(raw))),
                ("Content-Type", "application/json"),
                ("Transfer-Encoding", "chunked"),
            ],
            [
                ("Content-Length", str(len(raw))),
                ("Content-Type", "application/json"),
                ("Content-Encoding", "gzip"),
            ],
        ]
        for headers in invalid:
            with self.subTest(headers=headers), self.token_mock(), Wire(headers=headers, body=raw):
                with self.assertRaises(C.SpeachesError) as caught:
                    self.call()
                self.assertEqual(caught.exception.code, "SPEACHES_INVALID_RESPONSE")
        with self.token_mock(), Wire(truncated=True):
            with self.assertRaises(C.SpeachesError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "SPEACHES_RESPONSE_TRUNCATED")

    def test_audio_grande_ruta_formato_y_lenguaje_rechazados_antes_red(self):
        with self.audio.open("wb") as stream:
            stream.truncate(C.MAX_AUDIO_BYTES + 1)
        with self.token_mock(), patch.object(CLIENT, "_connect") as connect:
            with self.assertRaises(C.SpeachesError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "SPEACHES_AUDIO_TOO_LARGE")
            self.audio.write_bytes(b"audio")
            for language in ("es\r\nInjected: yes", "ES", 1):
                with self.subTest(language=language), self.assertRaises(C.SpeachesError):
                    self.call(language=language)
            self.audio = self.audio.with_suffix(".mp4")
            self.audio.write_bytes(b"video-original")
            with self.assertRaises(C.SpeachesError):
                self.call()
            connect.assert_not_called()

    def test_excepcion_transporte_no_se_confunde_con_fichero_ni_filtra_error(self):
        with self.token_mock(), patch.object(CLIENT, "_connect", side_effect=OSError(self.token)):
            with self.assertRaises(C.SpeachesError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "SPEACHES_TRANSPORT_FAILED")
            self.assertNotIn(self.token, str(caught.exception))

    def test_audio_modificado_durante_peticion_no_devuelve_exito_ni_reintenta(self):
        def mutate():
            with self.audio.open("ab") as stream:
                stream.write(b"cambio")

        with self.token_mock(), Wire(after_request=mutate):
            with self.assertRaises(C.SpeachesError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "SPEACHES_INPUT_CHANGED")

    def test_presupuestos_invalidos_no_abren_archivo_ni_transporte(self):
        with patch.object(CLIENT, "_file") as opened, patch.object(CLIENT, "_connect") as connect:
            for total, idle in (
                (301, 15),
                (300, 16),
                (True, 1),
                (1, 2),
                (1, 0),
                (float("inf"), 1),
                (10**1000, 1),
                (1, float("nan")),
            ):
                with (
                    self.subTest(total=total, idle=idle),
                    self.assertRaises(C.SpeachesError) as caught,
                ):
                    self.call(total_seconds=total, idle_seconds=idle)
                self.assertEqual(caught.exception.code, "SPEACHES_BUDGET_REJECTED")
            opened.assert_not_called()
            connect.assert_not_called()


class FileTests(unittest.TestCase):
    def test_token_estricto_no_recorta_nueva_linea_y_errores_no_filtran_valor(self):
        for raw in (b"short", b"A" * 257, b"A" * 32 + b"\n", b"A" * 32 + b"\x00", b"\xff" * 32):

            @contextmanager
            def opened(*_args, _raw=raw, **_kwargs):
                yield io.BytesIO(_raw), None

            with self.subTest(length=len(raw)), patch.object(CLIENT, "_file", opened):
                with self.assertRaises(C.SpeachesError) as caught:
                    CLIENT._read_token("token-fixture")
                self.assertEqual(caught.exception.code, "SPEACHES_SECRET_FILE_REJECTED")

        @contextmanager
        def valid(*_args, **_kwargs):
            yield io.BytesIO(b"TEST_ONLY_NOT_A_CREDENTIAL_1234567890"), None

        with patch.object(CLIENT, "_file", valid):
            self.assertEqual(
                CLIENT._read_token("token-fixture"), "TEST_ONLY_NOT_A_CREDENTIAL_1234567890"
            )

    def test_hardlink_no_se_adopta_como_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            audio.write_bytes(b"audio")
            alias = Path(directory) / "alias.wav"
            os.link(audio, alias)
            with self.assertRaises(C.SpeachesError):
                with CLIENT._file(audio, C.MAX_AUDIO_BYTES):
                    self.fail("No debe abrir archivo con alias")

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "NoFollow/modos/UID requieren Linux real"
    )
    def test_bearer_linux_uid_modos_symlink_ancestro_y_archivo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "token"
            secret.write_bytes(b"TEST_ONLY_NOT_A_CREDENTIAL_1234567890")
            secret.chmod(0o600)
            self.assertTrue(CLIENT._read_token(secret).startswith("TEST_ONLY_"))
            secret.chmod(0o400)
            self.assertTrue(CLIENT._read_token(secret).startswith("TEST_ONLY_"))
            for mode in (0o644, 0o640, 0o660, 0o700):
                secret.chmod(mode)
                with self.subTest(mode=mode), self.assertRaises(C.SpeachesError):
                    CLIENT._read_token(secret)
            secret.chmod(0o600)
            link = root / "link"
            link.symlink_to(secret)
            with self.assertRaises(C.SpeachesError):
                CLIENT._read_token(link)
            ancestor = root / "ancestor"
            ancestor.symlink_to(root, target_is_directory=True)
            with self.assertRaises(C.SpeachesError):
                CLIENT._read_token(ancestor / "token")

    @unittest.skipUnless(
        sys.platform.startswith("linux") and os.geteuid() == 0,
        "Propietario ajeno se comprueba como root Linux en fixture",
    )
    def test_bearer_linux_propietario_ajeno_rechazado(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "token"
            secret.write_bytes(b"TEST_ONLY_NOT_A_CREDENTIAL_1234567890")
            secret.chmod(0o600)
            os.chown(secret, 65534, 65534)
            with self.assertRaises(C.SpeachesError):
                CLIENT._read_token(secret)


if __name__ == "__main__":
    unittest.main()
