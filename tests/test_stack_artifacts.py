"""Inventario candidato de Fase 4: identidad no equivale a auditoría o despliegue."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "docker/validate_stack_artifacts.py"
MANIFEST = ROOT / "docker/stack-artifacts.json"
SPEC = importlib.util.spec_from_file_location("stack_artifacts", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
REQUIRED = {
    "postgres-apps",
    "postgres-immich-base",
    "immich-server",
    "valkey",
    "n8n",
    "n8n-runners",
    "moneyprinter",
    "otel-collector",
    "node-exporter",
    "cadvisor",
    "kitsu-ui",
    "zou-api",
    "supoclip",
    "vectorchord-extension",
    "pgvector-extension",
}


class InventarioStack(unittest.TestCase):
    def manifest(self):
        return MODULE.load_json(MANIFEST)

    def artifact(self, data, role):
        return next(item for item in data["artifacts"] if item["role"] == role)

    def test_inventario_real_con_roles_fijos_solo_acredita_estructura(self):
        data = self.manifest()
        self.assertEqual({item["role"] for item in data["artifacts"]}, REQUIRED)
        self.assertEqual(MODULE.validate_inventory(data), [])
        result = MODULE.assess(data)
        self.assertTrue(result["structure_valid"])
        self.assertFalse(result["security_clean"])
        self.assertFalse(result["runtime_verified"])
        self.assertFalse(result["production_ready"])
        self.assertFalse(result["deployment_enabled"])
        self.assertEqual(result["audit_status"], "not_verified")

    def test_no_puede_omitir_duplicar_o_inventar_roles(self):
        original = self.manifest()
        for role in REQUIRED:
            data = copy.deepcopy(original)
            data["artifacts"] = [item for item in data["artifacts"] if item["role"] != role]
            with self.subTest(missing=role):
                self.assertTrue(MODULE.validate_inventory(data))
        for role in ("postgres-apps", "arbitrario"):
            data = copy.deepcopy(original)
            item = copy.deepcopy(data["artifacts"][0])
            item["role"] = role
            data["artifacts"].append(item)
            with self.subTest(added=role):
                self.assertTrue(MODULE.validate_inventory(data))

    def test_pins_no_admiten_latest_rangos_o_digests_incompletos(self):
        for field, value in (
            ("version", "latest"),
            ("version", ">=16"),
            ("index_digest", "sha256:123"),
            ("platform_digest", "a" * 64),
            ("tag", "latest"),
        ):
            data = self.manifest()
            entry = self.artifact(data, "postgres-apps")
            target = entry if field == "version" else entry["image"]
            target[field] = value
            with self.subTest(field=field, value=value):
                self.assertTrue(MODULE.validate_inventory(data))

    def test_plataforma_linux_amd64_no_puede_cambiarse_por_declaracion(self):
        for change in ("target", "artifact"):
            data = self.manifest()
            if change == "target":
                data["platform"] = "linux/arm64"
            else:
                self.artifact(data, "postgres-apps")["platform"] = "windows/amd64"
            self.assertTrue(MODULE.validate_inventory(data))

    def test_origenes_no_aceptan_credenciales_redirectores_o_repositorios_ajenos(self):
        for field, value in (
            ("repository", "https://github.com/otro/postgres"),
            ("revision", "main"),
            ("license_url", "https://usuario:secreto@github.com/postgres/postgres/LICENSE"),
        ):
            data = self.manifest()
            self.artifact(data, "postgres-apps")["source"][field] = value
            with self.subTest(field=field):
                self.assertTrue(MODULE.validate_inventory(data))
        data = self.manifest()
        self.artifact(data, "immich-server")["image"]["repository"] = "ghcr.io/ajeno/immich"
        self.assertTrue(MODULE.validate_inventory(data))

    def test_artefactos_https_requieren_sha_tamano_y_revision_fijos(self):
        for field, value in (
            ("url", "https://example.invalid/archive.tar.gz"),
            ("sha256", ""),
            ("size", True),
            ("size", -1),
        ):
            data = self.manifest()
            self.artifact(data, "zou-api")["download"][field] = value
            with self.subTest(field=field):
                self.assertTrue(MODULE.validate_inventory(data))

    def test_no_reetiqueta_n8n_como_osi_ni_extiende_su_uso_a_clientes(self):
        for field, value in (
            ("spdx", "MIT"),
            ("classification", "osi"),
            ("use", "customer-facing"),
        ):
            data = self.manifest()
            self.artifact(data, "n8n")["license"][field] = value
            self.assertTrue(MODULE.validate_inventory(data))

    def test_vectorchord_fija_eleccion_agpl_y_no_afirma_apache(self):
        data = self.manifest()
        license_info = self.artifact(data, "vectorchord-extension")["license"]
        self.assertEqual(license_info["spdx"], "AGPL-3.0-only")
        self.assertEqual(license_info["declared_expression"], "AGPL-3.0-only OR Elastic-2.0")
        for change in ("Apache-2.0", "Elastic-2.0"):
            altered = copy.deepcopy(data)
            self.artifact(altered, "vectorchord-extension")["license"]["spdx"] = change
            self.assertTrue(MODULE.validate_inventory(altered))

    def test_base_pg_y_versiones_extensiones_no_pueden_degradarse(self):
        for role, version in (
            ("postgres-apps", "16.10"),
            ("immich-server", "3.3.7"),
            ("vectorchord-extension", "0.4.3"),
            ("pgvector-extension", "0.8.1"),
        ):
            data = self.manifest()
            self.artifact(data, role)["version"] = version
            with self.subTest(role=role):
                self.assertTrue(MODULE.validate_inventory(data))

    def test_plan_derivado_no_inventa_imagen_construida_o_compatibilidad_probada(self):
        for field, value in (
            ("base_role", "valkey"),
            ("postgresql", "16.10"),
            ("extension_roles", ["vectorchord-extension"]),
            ("built_image_digest", "sha256:" + "a" * 64),
            ("runtime_verified", True),
        ):
            data = self.manifest()
            data["postgres_immich_build"][field] = value
            self.assertTrue(MODULE.validate_inventory(data))

    def test_autodeclarar_auditoria_o_produccion_aprobada_se_rechaza(self):
        for key in ("security_clean", "runtime_verified", "production_ready", "deployment_enabled"):
            data = self.manifest()
            data[key] = True
            with self.subTest(claim=key):
                self.assertTrue(MODULE.validate_inventory(data))
        data = self.manifest()
        data["audit_status"] = "clean"
        self.assertTrue(MODULE.validate_inventory(data))

    def test_importar_y_validar_no_ejecuta_red_procesos_ni_consulta_boveda(self):
        with (
            patch("socket.create_connection", side_effect=AssertionError("red")),
            patch("subprocess.run", side_effect=AssertionError("proceso")),
        ):
            spec = importlib.util.spec_from_file_location("stack_import_no_effects", MODULE_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.validate_inventory(self.manifest()), [])

    def test_json_rechaza_duplicados_y_valores_no_finitos(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.json"
            for text in ('{"schema_version":1,"schema_version":1}', '{"value":NaN}'):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    MODULE.load_json(path)

    def test_tipos_json_invalidos_se_rechazan_sin_excepcion_del_validador(self):
        for value in ([], {}, None, True):
            data = self.manifest()
            self.artifact(data, "postgres-apps")["image"]["tag"] = value
            with self.subTest(value=value):
                self.assertTrue(MODULE.validate_inventory(data))

    def test_runtime_del_plan_requiere_false_booleano_y_no_cero_numerico(self):
        data = self.manifest()
        data["postgres_immich_build"]["runtime_verified"] = 0
        self.assertTrue(MODULE.validate_inventory(data))

    def test_plan_vectorchord_glibc_no_acepta_base_musl_ni_reetiquetarla(self):
        for retag in (False, True):
            data = self.manifest()
            image = copy.deepcopy(self.artifact(data, "postgres-apps")["image"])
            if retag:
                image["tag"] = "16.15-trixie"
            self.artifact(data, "postgres-immich-base")["image"] = image
            with self.subTest(retag=retag):
                self.assertTrue(MODULE.validate_inventory(data))

    def test_cli_estructura_valida_no_se_presenta_como_seguridad_o_produccion(self):
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--manifest", str(MANIFEST)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["structure_valid"])
        self.assertFalse(report["security_clean"])
        self.assertFalse(report["production_ready"])


if __name__ == "__main__":
    unittest.main()
