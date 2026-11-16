"""Fixtures puras: archivos y mapas son datos, nunca JavaScript ejecutable."""

import base64
import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
import unittest
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "docker/kitsu-zou/inventory_embedded_js.py"
SPEC = importlib.util.spec_from_file_location("embedded_js_inventory", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def source_map(sources, mappings="AAAA", contents=None):
    return {
        "version": 3,
        "sources": sources,
        "sourcesContent": contents or ["export const x=1;" for _ in sources],
        "names": [],
        "mappings": mappings,
    }


class MapTests(unittest.TestCase):
    def test_solo_fuente_referida_por_mappings(self):
        result = MODULE.map_references(
            source_map(["../node_modules/one/index.js", "../node_modules/unused/index.js"])
        )
        self.assertEqual(result, {0})

    def test_delta_negativo_valido_y_estado_entre_lineas(self):
        self.assertEqual(MODULE.map_references(source_map(["a", "b"], "ACAA;ADAA")), {0, 1})

    def test_rechaza_vlq_incompleto_campos_indices_y_json_invalido(self):
        for mapping in ("g", "A!", "AA", "ADAA", "ACAA", "AAAAA", "ABAA"):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                MODULE.map_references(source_map(["a"], mapping))

    def test_mapas_indexados_o_sin_fuentes_no_se_ignoran(self):
        for value in (
            {"version": 3, "sections": []},
            source_map([], "AAAA"),
            {**source_map(["a"]), "version": 2},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.map_references(value)

    def test_nombres_scoped_anidados_y_locales(self):
        self.assertEqual(
            MODULE.package_name("../../node_modules/@scope/pkg/lib/index.js"), "@scope/pkg"
        )
        self.assertEqual(
            MODULE.package_name("webpack://X/./node_modules/a/node_modules/b/x.js"), "b"
        )
        self.assertIsNone(MODULE.package_name("../../src/components/foo.vue"))
        self.assertIsNone(MODULE.package_name('webpack://X/external "react"'))


class InventoryTests(unittest.TestCase):
    def files(self):
        return {
            "assets/app.js": b"const x=1;\n//# sourceMappingURL=app.js.map\n",
            "assets/app.js.map": json.dumps(
                source_map(["../node_modules/one/index.js", "../node_modules/two/index.js"])
            ).encode(),
            "assets/unmapped.js": b'const version="999.0.0";',
            "assets/native.wasm": b"\x00asm\x01\x00\x00\x00",
        }

    def test_catalogo_y_componentes_sin_inventar_version(self):
        result = MODULE.inventory_artifact("fixture", self.files(), [])
        self.assertEqual([p["name"] for p in result["packages"]], ["one"])
        self.assertIsNone(result["packages"][0]["version"])
        self.assertEqual(
            result["coverage"]["javascript_without_linked_map"], ["assets/unmapped.js"]
        )
        self.assertEqual(result["coverage"]["wasm_files"], ["assets/native.wasm"])
        self.assertFalse(result["sca_approved"])

    def test_mapa_sin_generado_o_con_enlace_remoto_se_rechaza(self):
        for mutation in (
            lambda f: f.pop("assets/app.js"),
            lambda f: f.update(
                {"assets/app.js": b"//# sourceMappingURL=https://example.org/app.js.map\n"}
            ),
        ):
            files = self.files()
            mutation(files)
            with self.assertRaises(ValueError):
                MODULE.inventory_artifact("fixture", files, [])

    def test_fuente_original_cambiada_cambia_fingerprint(self):
        before = MODULE.inventory_artifact("fixture", self.files(), [])
        files = self.files()
        files["assets/app.js.map"] = json.dumps(
            source_map(["../node_modules/one/index.js"], contents=["modified"])
        ).encode()
        after = MODULE.inventory_artifact("fixture", files, [])
        self.assertNotEqual(
            before["packages"][0]["sources"][0]["sha256"],
            after["packages"][0]["sources"][0]["sha256"],
        )

    def test_mapa_inline_se_decodifica_como_datos_sin_ejecutar_js(self):
        raw = json.dumps(source_map(["node_modules/inline-only/index.js"])).encode()
        files = {
            "inline.js": b"throw new Error('NO EJECUTAR');\n//# sourceMappingURL=data:application/json;charset=utf8;base64,"
            + base64.b64encode(raw)
            + b"\n"
        }
        result = MODULE.inventory_artifact("fixture", files, [])
        self.assertEqual([row["name"] for row in result["packages"]], ["inline-only"])
        self.assertTrue(result["maps"][0]["inline"])
        self.assertEqual(result["maps"][0]["sha256"], hashlib.sha256(raw).hexdigest())

    def test_version_solo_por_selector_revisado_y_hash_del_miembro(self):
        files = {"jquery.js": b"/*! jQuery v2.2.4 | licencia */\n"}
        evidence = [
            {
                "member": "jquery.js",
                "sha256": hashlib.sha256(files["jquery.js"]).hexdigest(),
                "name": "jquery",
                "version": "2.2.4",
                "needle": "/*! jQuery v2.2.4 |",
                "kind": "self-declared-banner",
            }
        ]
        result = MODULE.inventory_artifact("fixture", files, evidence)
        self.assertEqual(result["version_evidence"][0]["version"], "2.2.4")
        self.assertEqual(result["version_evidence"][0]["offset"], 0)
        with self.assertRaises(ValueError):
            MODULE.inventory_artifact(
                "fixture", {"jquery.js": files["jquery.js"] + b"modified"}, evidence
            )

    def test_nombre_de_archivo_no_confirma_version(self):
        result = MODULE.inventory_artifact(
            "fixture", {"handlebars-4.0.5.js": b"/* sin identidad */"}, []
        )
        self.assertEqual(result["version_evidence"], [])
        self.assertEqual(result["packages"], [])

    def test_lock_es_candidato_y_no_demuestra_bytes_del_bundle(self):
        lock = {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "kitsu", "version": "1.0.77"},
                "node_modules/one": {
                    "version": "2.3.4",
                    "resolved": "https://registry.npmjs.org/one/-/one-2.3.4.tgz",
                    "integrity": "sha512-AAAA",
                },
            },
        }
        result = MODULE.inventory_artifact("fixture", self.files(), [], lock)
        package = result["packages"][0]
        self.assertIsNone(package["version"])
        self.assertEqual(package["lock_candidates"][0]["version"], "2.3.4")
        self.assertFalse(package["lock_candidates"][0]["bundle_bytes_verified"])


class ArchiveTests(unittest.TestCase):
    def tar(self, entries):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:") as archive:
            for name, value, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(value) if kind == tarfile.REGTYPE else 0
                archive.addfile(member, io.BytesIO(value) if member.isfile() else None)
        return gzip.compress(data.getvalue(), mtime=0)

    def test_lee_archivos_sin_extraer(self):
        raw = self.tar([("./assets/app.js", b"one", tarfile.REGTYPE)])
        self.assertEqual(MODULE.read_archive(raw, "tar.gz"), {"assets/app.js": b"one"})

    def test_rechaza_escape_enlace_duplicado_y_colision_casefold(self):
        for entries in (
            [("../escape.js", b"x", tarfile.REGTYPE)],
            [("a.js", b"", tarfile.SYMTYPE)],
            [("a.js", b"x", tarfile.REGTYPE), ("./a.js", b"y", tarfile.REGTYPE)],
            [("A.js", b"x", tarfile.REGTYPE), ("a.js", b"y", tarfile.REGTYPE)],
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                MODULE.read_archive(self.tar(entries), "tar.gz")

    def test_zip_rechaza_enlace_y_tamano_excesivo(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            info = zipfile.ZipInfo("symlink.js")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "target")
        with self.assertRaises(ValueError):
            MODULE.read_archive(stream.getvalue(), "zip")
        with self.assertRaises(ValueError):
            MODULE.read_archive(
                self.tar([("a.js", b"abc", tarfile.REGTYPE)]), "tar.gz", maximum_total=2
            )

    def test_fuentes_flasgger_deben_coincidir_con_sdist(self):
        MODULE.compare_embedded(
            {"flasgger/ui3/x.js": b"one"}, {"flasgger-0.9.7.1/flasgger/ui3/x.js": b"one"}
        )
        with self.assertRaises(ValueError):
            MODULE.compare_embedded(
                {"flasgger/ui3/x.js": b"two"}, {"flasgger-0.9.7.1/flasgger/ui3/x.js": b"one"}
            )


if __name__ == "__main__":
    unittest.main()
