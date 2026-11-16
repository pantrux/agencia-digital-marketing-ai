"""Exportación D1 con transporte controlado y SQLite efímero, sin Cloudflare real."""

import asyncio
import importlib.util
import io
import json
import logging
import sqlite3
import ssl
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("d1_export", ROOT / "sre/d1_export.py")
EXPORT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EXPORT
SPEC.loader.exec_module(EXPORT)
MIGRATION = (ROOT / "cloudflare-saas/migrations/0000_resource_owner.sql").read_text(
    encoding="utf-8"
)


def dump_sql():
    with sqlite3.connect(":memory:") as database:
        database.executescript(MIGRATION)
        database.execute("CREATE TABLE control (id INTEGER PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO control VALUES (1,'control público')")
        return ("\n".join(database.iterdump()) + "\n").encode()


class ContractTests(unittest.TestCase):
    def test_sql_valido_se_restaurara_solo_en_memoria_y_acredita_owner(self):
        result = EXPORT.validate_dump(dump_sql())
        self.assertTrue(result["sqlite_integrity_verified"])
        self.assertTrue(result["owner_verified"])
        self.assertFalse(result["cloud_restore_verified"])

    def test_rechaza_dump_truncado_y_owner_ajeno(self):
        for sql in (
            dump_sql()[:-5],
            b"CREATE TABLE ajena (id INT);",
            dump_sql().replace(b"agencia-digital-marketing-ai", b"otro-proyecto"),
        ):
            with self.subTest(size=len(sql)), self.assertRaises(EXPORT.ExportError):
                EXPORT.validate_dump(sql)

    def test_sql_no_puede_adjuntar_archivos_ni_cargar_extensiones(self):
        for statement in (
            b"ATTACH DATABASE 'ajena.db' AS ajena;",
            b"SELECT load_extension('ajena');",
            b"PRAGMA writable_schema=ON;",
        ):
            with self.subTest(statement=statement), self.assertRaises(EXPORT.ExportError):
                EXPORT.validate_dump(dump_sql() + statement)

    def test_sql_virtual_o_integridad_referencial_ajena_falla(self):
        additions = (
            b"CREATE VIRTUAL TABLE f USING fts5(value);",
            b"PRAGMA foreign_keys=OFF;CREATE TABLE parent(id INTEGER PRIMARY KEY);"
            b"CREATE TABLE child(parent_id INTEGER REFERENCES parent(id));INSERT INTO child VALUES(99);",
        )
        for statement in additions:
            with self.subTest(size=len(statement)), self.assertRaises(EXPORT.ExportError):
                EXPORT.validate_dump(dump_sql() + statement)

    def test_url_solo_https_r2_sin_usuario_puerto_ajeno_o_host_confuso(self):
        valid = "https://" + "a" * 32 + ".r2.cloudflarestorage.com/export.sql?firma=sensible"
        self.assertEqual(EXPORT.validate_signed_url(valid), valid)
        for url in (
            valid.replace("https:", "http:"),
            valid.replace(".com/", ".com.evil/"),
            "https://usuario@" + valid[8:],
            "https://127.0.0.1/export.sql",
            "https://evilr2.cloudflarestorage.com/export.sql",
            valid.replace(".com/", ".com:8080/"),
        ):
            with self.subTest(url=url), self.assertRaises(EXPORT.ExportError):
                EXPORT.validate_signed_url(url)

    def test_estado_ausente_solo_es_progreso_con_bookmark(self):
        self.assertEqual(
            EXPORT.parse_poll({"success": True, "result": {"at_bookmark": "opaque-01"}}),
            ("pending", "opaque-01"),
        )
        for payload in (
            {"success": True, "result": {}},
            {"success": True, "result": {"status": "desconocido", "at_bookmark": "opaque-01"}},
            {"success": True, "result": {"status": "error", "error": "no publicar"}},
            {"success": False, "result": {"status": "complete"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(EXPORT.ExportError):
                EXPORT.parse_poll(payload)


class ExportHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.stage = Path(self.directory.name)
        self.values = {
            "CF_ACCOUNT_ID": "a" * 32,
            "CF_D1_DATABASE_ID": "00000000-0000-4000-8000-000000000001",
            "CF_PLATFORM_API_TOKEN": "test-no-es-token-real-" + "x" * 48,
            "CF_D1_EXPORT_STAGING_PATH": str(self.stage),
        }
        self.settings = EXPORT.Settings.from_values(self.values)
        self.access = patch.object(EXPORT, "private_access")
        self.access.start()
        self.addCleanup(self.access.stop)
        marker = self.stage / EXPORT.STAGE_MARKER
        marker.write_text(json.dumps(EXPORT.stage_marker(self.settings)), encoding="utf-8")
        marker.chmod(0o600)
        self.requests = []
        self.clients = []
        self.polls = 0
        self.status = "complete"
        self.sql = dump_sql()
        self.download_status = 200
        self.content_length = None
        self.owner_rows = [EXPORT.EXPECTED_OWNER]
        self.table_rows = [{"name": "_agency_resource_owner", "type": "table", "schema": "main"}]
        self.url = (
            "https://" + "b" * 32 + ".r2.cloudflarestorage.com/export.sql?sig=NO_PUBLICAR_URL"
        )
        self.clock_value = time.monotonic()

    def clock(self):
        return self.clock_value

    async def sleep(self, seconds):
        self.clock_value += seconds

    def response(self, request, data, status=200, headers=None):
        return httpx.Response(
            status, headers=headers, stream=httpx.ByteStream(data), request=request
        )

    async def handler(self, request):
        self.requests.append(request)
        if request.url.host == "api.cloudflare.com":
            body = json.loads(request.content)
            if request.url.path.endswith("/query"):
                if body["sql"].startswith("SELECT"):
                    rows = self.owner_rows
                else:
                    rows = self.table_rows
                result = {"success": True, "result": [{"success": True, "results": rows}]}
            else:
                self.polls += 1
                if self.status == "pending" or self.polls == 1:
                    result = {
                        "success": True,
                        "result": {"at_bookmark": "NO_PUBLICAR_BOOKMARK", "success": True},
                    }
                elif self.status == "complete":
                    result = {
                        "success": True,
                        "result": {
                            "status": "complete",
                            "success": True,
                            "result": {"signed_url": self.url, "filename": "../../ignorado.sql"},
                        },
                    }
                else:
                    result = {"success": True, "result": {"status": self.status}}
            return self.response(
                request,
                json.dumps(result).encode(),
                headers={"Set-Cookie": "api-cookie=no-transferir"},
            )
        headers = {
            "Content-Length": str(
                self.content_length if self.content_length is not None else len(self.sql)
            )
        }
        return self.response(request, self.sql, self.download_status, headers)

    def factory(self, **kwargs):
        self.clients.append(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), **kwargs)

    async def run_export(self):
        return await EXPORT.export_database(
            self.settings, client_factory=self.factory, clock=self.clock, sleeper=self.sleep
        )

    async def test_exporta_con_bookmark_y_cliente_descarga_nuevo_sin_bearer_cookie(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(
            EXPORT.ssl, "create_default_context", return_value=context
        ) as create_context:
            result = await self.run_export()
        create_context.assert_called_once_with()
        self.assertTrue(result["success"])
        self.assertFalse(result["cloud_restore_verified"])
        self.assertEqual((self.stage / EXPORT.OUTPUT_NAME).read_bytes(), self.sql)
        posts = [
            json.loads(request.content)
            for request in self.requests
            if request.url.path.endswith("/export")
        ]
        self.assertEqual(
            posts[0], {"output_format": "polling", "dump_options": {"compatibility": "d1"}}
        )
        self.assertEqual(posts[1]["current_bookmark"], "NO_PUBLICAR_BOOKMARK")
        download = self.requests[-1]
        for header in ("authorization", "cookie", "proxy-authorization", "referer"):
            self.assertNotIn(header, download.headers)
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(all(client["verify"] is context for client in self.clients))
        self.assertTrue(
            all(
                client["trust_env"] is False and client["follow_redirects"] is False
                for client in self.clients
            )
        )
        output = json.dumps(result)
        for private in (
            self.values["CF_PLATFORM_API_TOKEN"],
            self.url,
            "NO_PUBLICAR_BOOKMARK",
            "control público",
        ):
            self.assertNotIn(private, output)

    async def test_pendiente_agota_plazo_sin_exito_presunto(self):
        self.status = "pending"

        async def expire(_seconds):
            self.clock_value += 181

        with patch.object(self, "sleep", side_effect=expire), self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.assertFalse((self.stage / EXPORT.OUTPUT_NAME).exists())

    async def test_reintenta_consulta_y_poll_sin_crear_otra_exportacion(self):
        handler = self.handler
        failures = {"query": False, "poll": False}
        export_bodies = []

        async def fail_once(request):
            operation = request.url.path.rsplit("/", 1)[-1]
            if operation == "export":
                body = json.loads(request.content)
                export_bodies.append(body)
                operation = "poll" if "current_bookmark" in body else "start"
            if operation in failures and not failures[operation]:
                failures[operation] = True
                return self.response(request, b"NO_PUBLICAR_RESPUESTA", 503)
            return await handler(request)

        with patch.object(self, "handler", fail_once):
            result = await self.run_export()

        self.assertTrue(result["success"])
        self.assertTrue(all(failures.values()))
        self.assertEqual(len(export_bodies), 3)
        self.assertNotIn("current_bookmark", export_bodies[0])
        self.assertEqual(export_bodies[1], export_bodies[2])

    async def test_inicio_ambiguo_no_envia_otro_post_ni_publica_datos_remotos(self):
        handler = self.handler
        starts = []

        async def lose_response(request):
            if not request.url.path.endswith("/export"):
                return await handler(request)
            starts.append(json.loads(request.content))
            raise httpx.ReadTimeout("NO_PUBLICAR_RESPUESTA", request=request)

        with (
            patch.object(self, "handler", lose_response),
            self.assertRaisesRegex(EXPORT.ExportError, "TRANSPORTE_API_D1"),
        ):
            await self.run_export()
        self.assertEqual(len(starts), 1)
        self.assertEqual({path.name for path in self.stage.iterdir()}, {EXPORT.STAGE_MARKER})

    async def test_reintenta_descarga_interrumpida_con_un_parcial_nuevo(self):
        class Interrupted(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * 65536
                raise httpx.ReadError("NO_PUBLICAR_RESPUESTA")

        handler = self.handler
        downloads = []

        async def lose_first_download(request):
            if request.method == "GET":
                downloads.append(request)
                if len(downloads) == 1:
                    return httpx.Response(200, stream=Interrupted(), request=request)
            return await handler(request)

        with patch.object(self, "handler", lose_first_download):
            result = await self.run_export()

        self.assertTrue(result["success"])
        self.assertEqual(len(downloads), 2)
        self.assertEqual(downloads[0].url, downloads[1].url)
        self.assertEqual((self.stage / EXPORT.OUTPUT_NAME).read_bytes(), self.sql)
        self.assertFalse((self.stage / EXPORT.PARTIAL_NAME).exists())

    async def test_reintentos_acotados_ante_fallo_persistente(self):
        requests = []

        async def unavailable(request):
            requests.append(request)
            return self.response(request, b"NO_PUBLICAR_RESPUESTA", 503)

        with (
            patch.object(self, "handler", unavailable),
            self.assertRaisesRegex(EXPORT.ExportError, "HTTP_API_503"),
        ):
            await self.run_export()
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request.url.path.endswith("/query") for request in requests))

    async def test_descarga_rechaza_redireccion_y_truncado(self):
        self.download_status = 302
        with self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.download_status = 200
        self.content_length = len(self.sql) + 1
        with self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.assertEqual(set(path.name for path in self.stage.iterdir()), {EXPORT.STAGE_MARKER})

    async def test_sql_oversize_no_se_publica_ni_conserva_parcial(self):
        with patch.object(EXPORT, "MAX_SQL", 64), self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.assertEqual(set(path.name for path in self.stage.iterdir()), {EXPORT.STAGE_MARKER})

    async def test_destino_no_vacio_no_inicia_consultas(self):
        (self.stage / "archivo-ajeno").write_text("conservar", encoding="utf-8")
        with self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.assertEqual(self.requests, [])
        self.assertEqual((self.stage / "archivo-ajeno").read_text(), "conservar")

    async def test_host_firmado_peligroso_no_se_consulta(self):
        self.url = "https://127.0.0.1/NO_PUBLICAR_URL"
        with self.assertRaises(EXPORT.ExportError) as context:
            await self.run_export()
        self.assertNotIn(self.url, str(context.exception))
        self.assertTrue(all(request.url.host == "api.cloudflare.com" for request in self.requests))

    async def test_logging_http_activado_no_publica_url_firmada(self):
        logger = logging.getLogger("httpx")
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        previous_level, previous_disabled = logger.level, logger.disabled
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
        logger.addHandler(handler)
        try:
            await self.run_export()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            logger.disabled = previous_disabled
        self.assertNotIn("NO_PUBLICAR_URL", output.getvalue())
        self.assertNotIn(self.values["CF_PLATFORM_API_TOKEN"], output.getvalue())

    async def test_owner_ajeno_y_tablas_virtuales_bloquean_antes_de_export(self):
        self.owner_rows = [dict(EXPORT.EXPECTED_OWNER, project="ajeno")]
        with self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.assertEqual(len(self.requests), 1)
        self.owner_rows = [EXPORT.EXPECTED_OWNER]
        self.table_rows.append({"name": "fts", "type": "virtual"})
        with self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.assertFalse(any(request.url.path.endswith("/export") for request in self.requests))

    async def test_marcador_staging_no_adopta_db_ajena(self):
        marker = EXPORT.stage_marker(self.settings)
        marker["database_id"] = "00000000-0000-4000-8000-000000000099"
        (self.stage / EXPORT.STAGE_MARKER).write_text(json.dumps(marker), encoding="utf-8")
        with self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.assertEqual(self.clients, [])

    async def test_fallo_transporte_no_filtra_token_url_ni_cuerpo(self):
        async def failed(_request):
            raise httpx.ConnectError(self.values["CF_PLATFORM_API_TOKEN"] + self.url)

        with (
            patch.object(self, "handler", side_effect=failed),
            self.assertRaises(EXPORT.ExportError) as context,
        ):
            await self.run_export()
        self.assertNotIn(self.url, str(context.exception))
        self.assertNotIn(self.values["CF_PLATFORM_API_TOKEN"], str(context.exception))

    async def test_cancelacion_descarga_retira_solo_parcial_propio(self):
        original = self.handler

        async def cancelled(request):
            if request.url.host != "api.cloudflare.com":
                raise asyncio.CancelledError
            return await original(request)

        with (
            patch.object(self, "handler", side_effect=cancelled),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.run_export()
        self.assertEqual(set(path.name for path in self.stage.iterdir()), {EXPORT.STAGE_MARKER})

    async def test_archivo_ajeno_creado_durante_descarga_no_se_sobrescribe(self):
        original = self.handler

        async def collision(request):
            if request.url.host != "api.cloudflare.com":
                (self.stage / EXPORT.OUTPUT_NAME).write_bytes(b"preservar-ajeno")
            return await original(request)

        with (
            patch.object(self, "handler", side_effect=collision),
            self.assertRaises(EXPORT.ExportError),
        ):
            await self.run_export()
        self.assertEqual((self.stage / EXPORT.OUTPUT_NAME).read_bytes(), b"preservar-ajeno")
        self.assertFalse((self.stage / EXPORT.PARTIAL_NAME).exists())

    def test_plan_y_repr_no_publican_token_ni_abren_clientes(self):
        self.assertEqual(EXPORT.plan_export(self.settings)["network_used"], False)
        self.assertNotIn(self.values["CF_PLATFORM_API_TOKEN"], repr(self.settings))
        self.assertEqual(self.clients, [])

    async def test_descarga_lenta_agota_presupuesto_total(self):
        original = self.handler

        async def slow(request):
            if request.url.host != "api.cloudflare.com":
                self.clock_value += 181
            return await original(request)

        with patch.object(self, "handler", side_effect=slow), self.assertRaises(EXPORT.ExportError):
            await self.run_export()
        self.assertFalse((self.stage / EXPORT.OUTPUT_NAME).exists())
        self.assertEqual(EXPORT._LOG_USERS, 0)

    def test_cli_predeterminado_no_invoca_export_ni_publica_token(self):
        vault = self.stage / "vault-temporal"
        vault.write_text(
            "\n".join(f"{key}={value}" for key, value in self.values.items()), encoding="utf-8"
        )
        outside = self.stage / "vault-temporal"
        raw = outside.read_text(encoding="utf-8")
        outside.unlink()
        environment = SimpleNamespace(parse_env=lambda _content: (self.values, []))
        with (
            patch.object(EXPORT, "load_module", return_value=environment),
            patch.object(Path, "read_text", return_value=raw),
            patch.object(
                EXPORT, "export_database", side_effect=AssertionError("No debe exportar")
            ) as export,
            patch("sys.argv", ["d1_export.py"]),
            patch("builtins.print") as output,
        ):
            self.assertEqual(EXPORT.main(), 0)
        export.assert_not_called()
        self.assertNotIn(self.values["CF_PLATFORM_API_TOKEN"], output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
