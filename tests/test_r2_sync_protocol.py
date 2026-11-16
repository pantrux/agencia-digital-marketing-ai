"""Vectores públicos WebCrypto/TS y rechazos de protocolo puro, sin red o archivos."""

import hashlib
import hmac
import importlib
import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/r2-sync-consumer"))
PROTOCOL = importlib.import_module("sync_protocol")
KEY = bytes(range(32))  # Vector público; no es una credencial.
TIME = "1700000000"
HASH = "d46ba757c04bdbc4e20dd939a555409f753b98258554fb6ba22521eedd94b7fd"
SIGNATURE = "v1=75b234bd0eec94e62ce7759905041298cdc510d584bddc27bcf2686dce70e4b3"


def fixture():
    return {
        "job_id": "1e0b3af5-2f67-4bf0-8a20-4b7d0b1c2d3e",
        "asset_id": "2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f",
        "project_slug": "campana-2026",
        "extension": "mp4",
        "bytes": 12345,
        "sha256": "a" * 64,
        "object_key": "ingest/1e0b3af5-2f67-4bf0-8a20-4b7d0b1c2d3e/2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f.mp4",
    }


def body(value=None):
    return json.dumps(value or fixture(), ensure_ascii=False, separators=(",", ":")).encode()


def signed(data, timestamp=TIME, secret=KEY):
    # Firma independiente del SUT: permite probar cuerpos inválidos con MAC válido.
    digest = hashlib.sha256(data).hexdigest()
    message = f"v1\n{timestamp}\nPOST\n/v1/sync\n{digest}".encode("ascii")
    return "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest()


class ProtocolTests(unittest.TestCase):
    def verify(self, data=None, timestamp=TIME, signature=None, secret=KEY, policy=12345, now=None):
        data = body() if data is None else data
        signature = signed(data, timestamp) if signature is None else signature
        return PROTOCOL.verify_request(
            data, timestamp, signature, secret, policy, now or (lambda: 1700000000)
        )

    def test_vector_webcrypto_existente_ts_y_resultado_inmutable(self):
        result = self.verify(signature=SIGNATURE)
        self.assertEqual(result.fingerprint, HASH)
        self.assertEqual(result.body_sha256, HASH)
        self.assertEqual(result.job.job_id, fixture()["job_id"])
        self.assertEqual(
            result.destination_relative_path,
            "proyectos/campana-2026/01_raw/2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f.mp4",
        )
        with self.assertRaises(FrozenInstanceError):
            result.fingerprint = "0" * 64
        with self.assertRaises(FrozenInstanceError):
            result.job.bytes = 1
        self.assertNotIn("secret", repr(result))

    def test_vectores_webcrypto_independientes_extremos_de_clave_numero_timestamp(self):
        result = self.verify(
            timestamp="9007199254740991",
            signature="v1=9201d829b7b3e43ff8253b7fd9b2826a863bcdf9080ba0482796e4fa3e30f10a",
            now=lambda: 2**53 - 1,
        )
        self.assertEqual(result.fingerprint, HASH)
        value = fixture()
        value.update(project_slug="a" * 63, extension="a1b2c3d4e5", bytes=2**53 - 1)
        value["object_key"] = value["object_key"].replace(".mp4", ".a1b2c3d4e5")
        result = self.verify(
            body(value),
            timestamp="0",
            signature="v1=74827ebe008379045ee22ac884b6b7fec8e4632187c93d3366d2c14023d55de5",
            secret=bytes(range(128)),
            policy=2**53 - 1,
            now=lambda: 0,
        )
        self.assertEqual(
            result.fingerprint, "f10b24be953eaa88e9f32e4003ce8baf194f756c3a1f0da376b3fd5524759b32"
        )

    def test_ventana_inclusiva_y_rechazo_fuera_de_300_segundos(self):
        for offset in (-300, 300):
            self.assertEqual(
                self.verify(now=lambda offset=offset: 1700000000 + offset).fingerprint, HASH
            )
        for offset in (-301, 301):
            with self.subTest(offset=offset), self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(now=lambda offset=offset: 1700000000 + offset)

    def test_no_hay_cache_replay_oculto_y_renovar_tiempo_conserva_fingerprint(self):
        self.assertEqual(self.verify(), self.verify())
        self.assertEqual(
            self.verify(timestamp="1700000001", now=lambda: 1700000001).fingerprint, HASH
        )

    def test_timestamp_decimal_canonico_y_entero_seguro(self):
        for timestamp in (
            "",
            "01700000000",
            "+1700000000",
            "-1",
            "1.7e9",
            "1700000000.0",
            "1700000000\n",
            "9007199254740992",
            1700000000,
            None,
        ):
            with self.subTest(timestamp=timestamp), self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(timestamp=timestamp, signature=SIGNATURE)

    def test_reloj_obligatorio_entero_seguro_y_error_saneado(self):
        for value in (
            True,
            1700000000.0,
            -1,
            2**53,
            float("inf"),
            float("nan"),
            "1700000000",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(now=lambda value=value: value)

        def broken():
            raise RuntimeError("diagnostico privado del reloj")

        with self.assertRaises(PROTOCOL.ProtocolError) as error:
            self.verify(now=broken)
        self.assertNotIn("privado", str(error.exception))
        with self.assertRaises(PROTOCOL.ProtocolError):
            PROTOCOL.verify_request(body(), TIME, SIGNATURE, KEY, 12345, None)

    def test_mac_alterado_clave_distinta_y_tamper_con_cuerpo_canonico(self):
        value = fixture()
        value["bytes"] = 12344
        for data, signature, key in (
            (body(), SIGNATURE[:-1] + "0", KEY),
            (body(value), SIGNATURE, KEY),
            (body(), SIGNATURE, bytes(range(1, 33))),
        ):
            with self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(data, signature=signature, secret=key)

    def test_firma_cerrada_y_compare_digest_real(self):
        for signature in (
            "",
            SIGNATURE.upper(),
            SIGNATURE + "\n",
            "v2=" + "a" * 64,
            "v1=" + "a" * 63,
            "v1=" + "g" * 64,
            None,
            b"v1=" + b"a" * 64,
        ):
            with self.subTest(signature=signature), self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(signature=signature if signature is not None else "no-firma")
        with self.assertRaises(PROTOCOL.ProtocolError):
            PROTOCOL.verify_request(body(), TIME, None, KEY, 12345, lambda: 1700000000)
        native = hmac.compare_digest
        with patch.object(PROTOCOL.hmac, "compare_digest", wraps=native) as compare:
            self.verify(signature=SIGNATURE)
        compare.assert_called_once()

    def test_fallo_del_proveedor_hmac_no_expone_diagnostico(self):
        with patch.object(PROTOCOL.hmac, "digest", side_effect=RuntimeError("diagnostico privado")):
            with self.assertRaises(PROTOCOL.ProtocolError) as error:
                self.verify(signature=SIGNATURE)
        self.assertEqual(error.exception.code, "FIRMA")
        self.assertNotIn("privado", str(error.exception))

    def test_metodo_ruta_orden_y_saltos_fijos_en_mac(self):
        for message in (
            f"v1\n{TIME}\nGET\n/v1/sync\n{HASH}",
            f"v1\n{TIME}\nPOST\n/v1/sync/\n{HASH}",
            f"v1\r\n{TIME}\nPOST\n/v1/sync\n{HASH}",
            f"v1\n{TIME}\nPOST\n/v1/sync\n{HASH}\n",
        ):
            signature = "v1=" + hmac.new(KEY, message.encode(), hashlib.sha256).hexdigest()
            with self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(signature=signature)

    def test_clave_inmutable_32_a_128_bytes_sin_fugas(self):
        for secret in (
            b"",
            b"x" * 31,
            b"x" * 129,
            bytearray(KEY),
            memoryview(KEY),
            "privado-no-registrar" * 3,
            None,
        ):
            with (
                self.subTest(tipo=type(secret).__name__),
                self.assertRaises(PROTOCOL.ProtocolError) as error,
            ):
                self.verify(secret=secret, signature=SIGNATURE)
            self.assertNotIn("privado", str(error.exception))

    def test_cuerpos_no_bytes_inmutables_vacio_y_maximo(self):
        for data in (
            b"",
            b"x" * 4097,
            body().decode(),
            bytearray(body()),
            memoryview(body()),
            None,
        ):
            with self.subTest(tipo=type(data).__name__), self.assertRaises(PROTOCOL.ProtocolError):
                PROTOCOL.verify_request(data, TIME, SIGNATURE, KEY, 12345, lambda: 1700000000)
        at_limit = body() + b" " * (4096 - len(body()))
        with self.assertRaises(PROTOCOL.ProtocolError) as error:
            self.verify(at_limit)
        self.assertEqual(error.exception.code, "CANONICO")

    def test_representaciones_json_no_canonicas_con_mac_valido(self):
        original = body()
        alternatives = (
            b" " + original,
            original + b"\n",
            original.replace(b"12345", b"1.2345e4"),
            original.replace(b"12345", b"12345.0"),
            original.replace(b"campana", b"\\u0063ampana"),
            original.replace(b"ingest/", b"ingest\\/"),
            original.replace(b"{", b'{"job_id":"ignorado",', 1),
            json.dumps(dict(reversed(list(fixture().items()))), separators=(",", ":")).encode(),
        )
        for data in alternatives:
            with self.subTest(data=data[:40]), self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(data)

    def test_utf8_json_bom_constantes_duplicados_y_anidacion_rechazados(self):
        for data in (
            b"\xc3(",
            b"\xef\xbb\xbf" + body(),
            body()[:-1],
            b"{" + b'"a":1,"a":1}',
            b"[" * 1500 + b"0" + b"]" * 1500,
            body().replace(b"12345", b"NaN"),
            body().replace(b"12345", b"Infinity"),
            body().replace(b"campana", b"\\ud800"),
        ):
            with self.subTest(data=data[:30]), self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(data)

    def test_esquema_tipos_ids_y_politica_se_revalidan(self):
        for change in (
            {"bytes": True},
            {"bytes": 0},
            {"bytes": 2**53},
            {"project_slug": "con"},
            {"project_slug": "A"},
            {"job_id": fixture()["job_id"].upper()},
            {"sha256": "A" * 64},
            {"extension": ".mp4"},
            {"object_key": "https://ajeno.invalid/"},
            {"token": "no-persistir"},
        ):
            value = fixture()
            value.update(change)
            with self.subTest(change=change), self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(body(value))
        for policy in (None, True, 0, 12344, 1.0, 2**53):
            with self.subTest(policy=policy), self.assertRaises(PROTOCOL.ProtocolError):
                self.verify(policy=policy)


if __name__ == "__main__":
    unittest.main()
