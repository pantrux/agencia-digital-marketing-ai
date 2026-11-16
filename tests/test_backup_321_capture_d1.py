import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from sre import backup_321_capture_d1 as capture

SQL = (
    b"CREATE TABLE _agency_resource_owner(id, project, resource, schema_version);\n"
    b"INSERT INTO _agency_resource_owner VALUES(1,'agencia-digital-marketing-ai','studio-saas-db',1);\n"
)
EXPORT_DATABASE = capture.d1.export_database


class CapturaD1Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        token = self.root / "token"
        token.write_bytes(b"token-sintetico-" + b"x" * 32 + b"\n")
        self.request = {
            "root": str(self.root / "capturas"),
            "run_id": "corrida-01",
            "account_id": "a" * 32,
            "database_id": capture.DATABASE_ID,
            "token_file": str(token),
        }
        self.run_dir = Path(self.request["root"]) / self.request["run_id"]
        self.run_dir.parent.mkdir()
        self.calls = []
        self.frame = bytes.fromhex("28b52ffd") + b"frame-sintetico"
        self.enterContext(patch.object(capture.core, "ROOT", self.root))
        self.enterContext(patch.object(capture.core, "private_directory"))
        self.enterContext(
            patch.object(capture.core, "private", side_effect=lambda p, *a: p.lstat())
        )
        self.enterContext(patch.object(capture.core, "sync_directory"))
        self.enterContext(patch.object(capture.core, "exclusive", side_effect=nullcontext))
        self.enterContext(
            patch.object(capture.core, "read_verified", side_effect=self.leer_fixture)
        )
        self.enterContext(
            patch.object(
                capture.os,
                "statvfs",
                create=True,
                return_value=SimpleNamespace(f_bavail=100000, f_frsize=4096),
            )
        )
        self.enterContext(patch.object(capture.d1, "private_access"))
        self.enterContext(patch.object(capture, "check_tools"))
        self.compress = self.enterContext(
            patch.object(capture, "compress_sql", return_value=self.frame)
        )
        self.export = self.enterContext(
            patch.object(capture.d1, "export_database", side_effect=self.exportar_fixture)
        )

    @staticmethod
    def leer_fixture(path, maximum=None, modes=None):
        raw = path.read_bytes()
        value = raw if maximum is not None else hashlib.sha256(raw).hexdigest()
        return value, capture.core.identity(path.lstat())

    def transportar(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.host.endswith("r2.cloudflarestorage.com"):
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, stream=httpx.ByteStream(SQL))
        self.assertTrue(request.headers["Authorization"].startswith("Bearer token-sintetico-"))
        if request.url.path.endswith("/export"):
            body = {
                "success": True,
                "result": {
                    "status": "complete",
                    "result": {"signed_url": "https://test.r2.cloudflarestorage.com/d.sql"},
                },
            }
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))
        self.assertTrue(request.url.path.endswith("/query"))
        query = json.loads(request.content)["sql"]
        rows = (
            [capture.d1.EXPECTED_OWNER]
            if "_agency_resource_owner" in query
            else [{"name": "_agency_resource_owner", "type": "table"}]
        )
        body = {"success": True, "result": [{"success": True, "results": rows}]}
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))

    async def exportar_fixture(self, settings):
        return await EXPORT_DATABASE(
            settings,
            client_factory=lambda **options: httpx.AsyncClient(
                transport=httpx.MockTransport(self.transportar), **options
            ),
        )

    def capturar(self):
        capture.validate_request(self.request)
        result = {"success": False, "backup_set_complete": False}
        capture.run_capture(self.request, result)
        return result

    def test_captura_nueva_reutiliza_exportador_y_publica_recibo_de_componente(self):
        result = self.capturar()
        self.assertTrue(result["success"], result)
        self.assertFalse(result["backup_set_complete"])
        self.assertFalse(result["cloud_restore_verified"])
        self.assertEqual(result["sql_sha256"], hashlib.sha256(SQL).hexdigest())
        self.assertEqual(result["sha256"], hashlib.sha256(self.frame).hexdigest())
        self.assertEqual(result["staging_name"], "d1.sql.zst")
        self.assertEqual((self.run_dir / "d1.sql.zst").read_bytes(), self.frame)
        self.assertEqual([method for method, _ in self.calls], ["POST", "POST", "POST", "GET"])
        self.assertNotIn("token-sintetico", (self.run_dir / "result.json").read_text())
        with self.assertRaises(FileExistsError):
            self.capturar()
        self.assertEqual(self.export.await_count, 1)

    def test_rechaza_otro_id_antes_de_exportar(self):
        self.request["database_id"] = "00000000-0000-4000-8000-000000000001"
        with self.assertRaisesRegex(ValueError, "D1_BASE_NO_ACREDITADA"):
            self.capturar()
        self.export.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_rechaza_recibo_fuera_de_la_ventana_de_captura(self):
        async def exportar_antiguo(settings):
            result = await self.exportar_fixture(settings)
            result["operation_started_at_utc"] = (
                datetime.now(UTC) - timedelta(hours=1)
            ).isoformat()
            (settings.staging / capture.d1.RECEIPT_NAME).write_text(json.dumps(result))
            return result

        self.export.side_effect = exportar_antiguo
        self.assertEqual(self.capturar()["error"], "D1_VENTANA_CAPTURA")
        self.compress.assert_not_called()
        self.assertFalse((self.run_dir / "d1.sql.zst").exists())

    def test_conserva_el_codigo_del_exportador_en_el_recibo_de_fallo(self):
        self.export.side_effect = capture.d1.TransientExportError("HTTP_API_503")
        result = self.capturar()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "HTTP_API_503")
        self.assertEqual(
            json.loads((self.run_dir / "result.json").read_text())["error"], "HTTP_API_503"
        )
        self.compress.assert_not_called()

    def test_rechaza_sql_alterado_antes_de_comprimir(self):
        async def exportar_alterado(settings):
            result = await self.exportar_fixture(settings)
            (settings.staging / capture.d1.OUTPUT_NAME).write_bytes(b"x" * len(SQL))
            return result

        self.export.side_effect = exportar_alterado
        self.assertEqual(self.capturar()["error"], "D1_SQL_SHA")
        self.compress.assert_not_called()

    def test_rechaza_recibo_en_disco_distinto_del_devuelto(self):
        async def exportar_recibo_alterado(settings):
            result = await self.exportar_fixture(settings)
            receipt = settings.staging / capture.d1.RECEIPT_NAME
            receipt.write_text(json.dumps({**result, "bytes": len(SQL) + 1}))
            return result

        self.export.side_effect = exportar_recibo_alterado
        self.assertEqual(self.capturar()["error"], "D1_RECIBO_EXPORTACION")
        self.compress.assert_not_called()


class CompresionD1Tests(unittest.TestCase):
    def test_verifica_que_el_frame_restaure_exactamente_el_sql(self):
        frame = capture.ZSTD_MAGIC + b"frame-de-prueba"
        with patch.object(capture, "zstd", side_effect=(frame, SQL)) as zstd:
            self.assertEqual(capture.compress_sql(SQL), frame)
        self.assertEqual(zstd.call_args_list[0].args[2], capture.MAX_FRAME)
        self.assertEqual(zstd.call_args_list[1].args[2], len(SQL))

        with (
            patch.object(capture, "zstd", side_effect=(frame, b"contenido diferente")),
            self.assertRaisesRegex(ValueError, "D1_ZSTD_INTEGRIDAD"),
        ):
            capture.compress_sql(SQL)

    @unittest.skipUnless(
        sys.platform == "linux" and capture.ZSTD.is_file() and capture.PRLIMIT.is_file(),
        "Requiere Zstandard y prlimit en Linux",
    )
    def test_comprime_y_descomprime_sql_con_las_herramientas_linux(self):
        frame = capture.compress_sql(SQL)
        self.assertTrue(frame.startswith(capture.ZSTD_MAGIC))
