"""Inventario y cierre de metadatos, sin SDK, ZIP ni procesos secundarios."""

import asyncio
import base64
import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sre"))
SEMVER = importlib.import_module("go_module_semver")
INVENTORY = importlib.import_module("restic_module_inventory")


class SemverTests(unittest.TestCase):
    def test_precedencia_go_y_pseudoversiones(self):
        ordered = [
            "v1.2.3-0.20260101000000-aaaaaaaaaaaa",
            "v1.2.3-0.20260201000000-bbbbbbbbbbbb",
            "v1.2.3-alpha",
            "v1.2.3-alpha.2",
            "v1.2.3-alpha.10",
            "v1.2.3-rc.1",
            "v1.2.3",
            "v1.2.4",
        ]
        for before, after in zip(ordered, ordered[1:], strict=False):
            self.assertLess(SEMVER.compare(before, after), 0)
        self.assertEqual(SEMVER.compare("v2.0.0", "v2.0.0+incompatible"), 0)

    def test_rechaza_version_no_canonica_o_inyeccion(self):
        for version in (
            "v1",
            "v1.2",
            "1.2.3",
            "v01.2.3",
            "v1.2.3-01",
            "v1.2.3+otro",
            "v1.2.3/../../x",
            "v1.2.3\x00",
        ):
            with self.assertRaises(ValueError):
                SEMVER.version(version)

    def test_major_de_ruta_y_compatibilidad_gopkg(self):
        for path, version in [
            ("example.com/pkg/v2", "v2.3.0"),
            ("example.com/pkg", "v2.0.0+incompatible"),
            ("gopkg.in/check.v1", "v0.0.0-20200101000000-aaaaaaaaaaaa"),
            ("gopkg.in/yaml.v3", "v3.0.1"),
        ]:
            SEMVER.validate_module(path, version)
        for path, version in [
            ("example.com/pkg/v2", "v1.2.3"),
            ("example.com/pkg", "v2.0.0"),
            ("example.com/pkg/v1", "v1.2.3"),
            ("gopkg.in/yaml.v3", "v2.0.0"),
        ]:
            with self.assertRaises(ValueError):
                SEMVER.validate_module(path, version)

    def test_escape_proxy_es_inyectivo_y_no_admite_rutas_windows_peligrosas(self):
        self.assertEqual(SEMVER.escape("github.com/Azure/a"), "github.com/!azure/a")
        SEMVER.validate_module("git.sr.ht/~sbinet/gg", "v0.6.0")
        for path in (
            "example.com/../x",
            "example.com/NUL.txt",
            "example.com/a.",
            "example.com/a:ads",
            "example.com/a\\b",
            "example.com/!azure/a",
            "example.com/.hidden",
            "example.com/name~1",
        ):
            with self.assertRaises(ValueError):
                SEMVER.validate_module(path, "v1.0.0")


class ParserTests(unittest.TestCase):
    def test_go_mod_canonico_y_directivas_ignoradas_de_dependencia(self):
        result = INVENTORY.parse_mod(
            b"module example.com/main\ngo 1.26.0\nrequire (\n example.com/a v1.2.3 // indirect\n)\nreplace example.com/unused => ../local\n",
            dependency=True,
        )
        self.assertEqual(
            result["requires"], [{"module": "example.com/a", "version": "v1.2.3", "indirect": True}]
        )
        self.assertIn("replace", result["ignored_directives"])
        with self.assertRaises(ValueError):
            INVENTORY.parse_mod(b"module example.com/main\nreplace a => b\n")

    def test_no_acepta_utf8_invalido_duplicados_o_sintaxis_no_soportada(self):
        for content in (
            b"module example.com/main\xff",
            b"module example.com/main\nrequire (\nexample.com/a v1.0.0\n",
            b"module example.com/main\nrequire example.com/a v1.0.0\nrequire example.com/a v1.1.0\n",
            b'module "example.com/\\x61"\n',
            b"module example.com/main\nunknown x\n",
        ):
            with self.assertRaises(ValueError):
                INVENTORY.parse_mod(content)

    def test_go_sum_solo_registra_h1_sin_afirmar_sumdb(self):
        digest = "h1:" + base64.b64encode(b"x" * 32).decode()
        data = f"example.com/a v1.0.0 {digest}\nexample.com/a v1.0.0/go.mod {digest}\n".encode()
        parsed = INVENTORY.parse_sum(data)
        self.assertEqual(len(parsed), 2)
        with self.assertRaises(ValueError):
            INVENTORY.parse_sum(data + b"example.com/a v1.0.0 h1:invalid\n")

    def test_info_exige_identidad_exacta_tiempo_utc_y_json_no_ambiguo(self):
        good = json.dumps({"Version": "v1.0.0", "Time": "2026-01-01T00:00:00Z"}).encode()
        INVENTORY.parse_info(good, "v1.0.0")
        for body in (
            good.replace(b"v1.0.0", b"v1.1.0"),
            b'{"Version":"v1.0.0","Version":"v1.0.0","Time":"2026-01-01T00:00:00Z"}',
            b"\xff",
        ):
            with self.assertRaises(ValueError):
                INVENTORY.parse_info(body, "v1.0.0")


class ClosureTests(unittest.TestCase):
    def collect(self, graph, **kwargs):
        calls = []
        stored = {}

        def respond(request):
            calls.append(request)
            self.assertEqual(request.url.host, "proxy.golang.org")
            self.assertNotIn("authorization", request.headers)
            path = request.url.path
            module, tail = path.strip("/").split("/@v/")
            version, kind = tail.rsplit(".", 1)
            if kind == "mod":
                body = graph[(module, version)]
            elif kind == "info":
                body = json.dumps({"Version": version, "Time": "2026-01-01T00:00:00Z"}).encode()
            else:
                raise AssertionError("No se permite ZIP")

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield body

            return httpx.Response(200, stream=Stream(), headers={"Content-Length": str(len(body))})

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond),
                trust_env=False,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                return await INVENTORY.collect_metadata(
                    [{"module": "example.com/a", "version": "v1.0.0"}],
                    client,
                    lambda path, data: stored.setdefault(path, data),
                    **kwargs,
                )

        with (
            patch("subprocess.run", side_effect=AssertionError),
            patch("subprocess.Popen", side_effect=AssertionError),
        ):
            result = asyncio.run(run())
        return result, calls, stored

    def test_ciclo_y_varias_versiones_son_cierre_de_requisitos_no_mvs(self):
        graph = {
            ("example.com/a", "v1.0.0"): b"module example.com/a\nrequire example.com/b v1.0.0\n",
            (
                "example.com/b",
                "v1.0.0",
            ): b"module example.com/b\nrequire (\nexample.com/a v1.0.0\nexample.com/b v1.1.0\n)\n",
            ("example.com/b", "v1.1.0"): b"module example.com/b\n",
        }
        result, calls, stored = self.collect(graph)
        self.assertTrue(result["metadata_constraint_closure_complete"])
        self.assertEqual(len(result["nodes"]), 3)
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(stored), 6)
        self.assertFalse(result["mvs_verified"])
        self.assertFalse(result["sumdb_verified"])
        self.assertFalse(result["sources_utf8_verified"])

    def test_presupuesto_o_identidad_erronea_no_producen_cierre_completo(self):
        graph = {("example.com/a", "v1.0.0"): b"module example.com/other\n"}
        with self.assertRaises(ValueError):
            self.collect(graph)
        graph = {
            ("example.com/a", "v1.0.0"): b"module example.com/a\nrequire example.com/b v1.0.0\n"
        }
        with self.assertRaises(ValueError):
            self.collect(graph, max_nodes=1)

    def test_cache_propietaria_rechaza_bytes_alterados_y_archivos_sin_ledger(self):
        with tempfile.TemporaryDirectory(prefix="owned-module-cache-fixture-") as folder:
            store = INVENTORY.MetadataStore.__new__(INVENTORY.MetadataStore)
            store.proxy = Path(folder) / "proxy"
            store.known = {}
            store.files = []
            relative = "example.com/a/@v/v1.0.0.mod"
            target = store.proxy.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True)
            target.write_bytes(b"good")
            with self.assertRaises(ValueError):
                store.get(relative)
            store.known[relative] = {
                "path": relative,
                "size": 4,
                "sha256": hashlib.sha256(b"good").hexdigest(),
            }
            self.assertEqual(store.get(relative), b"good")
            target.write_bytes(b"evil")
            with self.assertRaises(ValueError):
                store.get(relative)

    def test_metadatos_cacheados_se_revalidan_sin_http(self):
        blobs = {
            "example.com/a/@v/v1.0.0.mod": b"module example.com/a\n",
            "example.com/a/@v/v1.0.0.info": b'{"Version":"v1.0.0","Time":"2026-01-01T00:00:00Z"}',
        }

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _r: (_ for _ in ()).throw(AssertionError("No debe consultar HTTP"))
                )
            ) as client:
                return await INVENTORY.collect_metadata(
                    [{"module": "example.com/a", "version": "v1.0.0"}],
                    client,
                    lambda *_args: None,
                    cache_read=blobs.get,
                )

        result = asyncio.run(run())
        self.assertEqual(result["cache_hits"], 2)
        self.assertEqual(result["http_requests"], 0)
        self.assertTrue(result["metadata_constraint_closure_complete"])


if __name__ == "__main__":
    unittest.main()
