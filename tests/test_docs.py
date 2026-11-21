"""Pruebas del control documental sin Git ni llamadas reales a GitHub."""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ESPECIFICACION = importlib.util.spec_from_file_location(
    "check_docs", RAIZ / "scripts/ci/check_docs.py"
)
MODULO = importlib.util.module_from_spec(ESPECIFICACION)
ESPECIFICACION.loader.exec_module(MODULO)


class PruebasDocumentacion(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.raiz = Path(self.temporal.name)
        shutil.copytree(RAIZ / "docs/ci", self.raiz / "docs/ci")
        shutil.copytree(RAIZ / "docs/decisions", self.raiz / "docs/decisions")
        # La evidencia enlaza contratos externos a docs/ci, sin copiar runtimes ni secretos.
        for relative in (
            "ESTADO.md",
            "docs/specs/FASE-3-PUENTE-GPU.md",
            "docs/specs/FASE-4-APLICACIONES-Y-BACKUPS.md",
            "docs/specs/FASE-5-OPENCLAW-Y-MCP.md",
            "docs/specs/FASE-6-CLOUDFLARE-Y-PORTAL.md",
            "docs/operations/STUDIO-EXPERIENCIA-CONVERSACIONAL.md",
            "docker/README-phase4-runtime.md",
            "docker/README-r2-service.md",
            "docker/immich-ml-auth/README.md",
            "docker/immich-server/README-ACEPTACION-FACIAL.md",
            "docker/immich-server/README-LAN.md",
            "docker/n8n/README-private-deploy.md",
            "docker/observability/README-nas.md",
            "docker/r2-sync-consumer/README-multiple-sources.md",
            "gpu-bridge-win11/README.md",
            "gpu-bridge-win11/Stop-GpuBridge.ps1",
            "ingress/README.md",
            "sre/README-backup-321-orchestrator.md",
            "sre/README-d1-export.md",
            "tests/test_gpu_stop.py",
        ):
            destination = self.raiz / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(RAIZ / relative, destination)
        (self.raiz / ".github/workflows").mkdir(parents=True)
        shutil.copyfile(
            RAIZ / ".github/workflows/ci-cd-media-platform.yml",
            self.raiz / ".github/workflows/ci-cd-media-platform.yml",
        )

    def test_conjunto_documental_completo_es_valido(self):
        self.assertEqual(MODULO.validate_structure(self.raiz), [])

    def test_omitir_runbook_bloquea_control(self):
        (self.raiz / "docs/ci/runbooks/rb-01-gpu-bridge-unreachable.md").unlink()
        self.assertTrue(MODULO.validate_structure(self.raiz))

    def test_enlace_local_roto_bloquea_control(self):
        indice = self.raiz / "docs/ci/README.md"
        indice.write_text(
            indice.read_text(encoding="utf-8") + "\n[Error](ausente.md)\n", encoding="utf-8"
        )
        self.assertTrue(MODULO.validate_structure(self.raiz))

    def test_runbook_sin_mitigacion_bloquea_control(self):
        ruta = self.raiz / "docs/ci/runbooks/rb-01-gpu-bridge-unreachable.md"
        ruta.write_text(
            ruta.read_text(encoding="utf-8").replace("## Mitigación", "## Otro título"),
            encoding="utf-8",
        )
        self.assertTrue(MODULO.validate_structure(self.raiz))

    def test_workflow_sin_septimo_control_bloquea(self):
        ruta = self.raiz / ".github/workflows/ci-cd-media-platform.yml"
        ruta.write_text(
            ruta.read_text(encoding="utf-8").replace("  gate-7-docs:", "  otro-control:"),
            encoding="utf-8",
        )
        self.assertTrue(MODULO.validate_structure(self.raiz))

    def test_cambio_workflow_sin_documentacion_bloquea(self):
        self.assertTrue(MODULO.validate_changes([".github/workflows/ci-cd-media-platform.yml"]))

    def test_cambio_workflow_con_receta_y_diagrama_aprueba(self):
        self.assertEqual(
            MODULO.validate_changes(
                [
                    ".github/workflows/ci-cd-media-platform.yml",
                    "docs/ci/recipes/developer-push-flow.md",
                    "docs/ci/diagrams/pipeline-overview.mmd",
                ]
            ),
            [],
        )

    def test_cambio_ajeno_a_despliegue_no_exige_receta(self):
        self.assertEqual(MODULO.validate_changes(["tests/test_una_funcion.py"]), [])


class PruebasCambiosGitHub(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.evento = Path(self.temporal.name) / "evento.json"
        self.entorno = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "equipo/plataforma",
            "GITHUB_TOKEN": "valor-de-prueba-no-es-secreto",
            "GITHUB_EVENT_PATH": str(self.evento),
            "GITHUB_EVENT_NAME": "pull_request",
        }

    def guardar_evento(self, valor):
        import json

        self.evento.write_text(json.dumps(valor), encoding="utf-8")

    def test_paginacion_pull_request_completa(self):
        self.guardar_evento({"number": 17})
        paginas = {
            "/repos/equipo/plataforma/pulls/17": {
                "changed_files": 101,
                "head": {"sha": "a" * 40},
                "base": {"sha": "b" * 40},
            },
            "/repos/equipo/plataforma/pulls/17/files?per_page=100&page=1": [
                {"filename": f"src/archivo-{numero}.ts"} for numero in range(100)
            ],
            "/repos/equipo/plataforma/pulls/17/files?per_page=100&page=2": [
                {"filename": "src/ultimo.ts", "previous_filename": "src/anterior.ts"}
            ],
        }
        archivos = MODULO.changes_from_github(self.entorno, paginas.__getitem__)
        self.assertIn("src/ultimo.ts", archivos)
        self.assertIn("src/anterior.ts", archivos)
        self.assertEqual(len(archivos), 102)

    def test_respuesta_pull_request_incompleta_es_error(self):
        self.guardar_evento({"number": 17})

        def consultar(ruta):
            if ruta.endswith("/17"):
                return {
                    "changed_files": 2,
                    "head": {"sha": "a" * 40},
                    "base": {"sha": "b" * 40},
                }
            return [{"filename": "src/solo-uno.ts"}]

        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, consultar)

    def test_solicitud_que_cambia_durante_paginacion_se_rechaza(self):
        self.guardar_evento({"number": 17})
        lecturas_metadatos = 0

        def consultar(ruta):
            nonlocal lecturas_metadatos
            if ruta.endswith("/17"):
                lecturas_metadatos += 1
                return {
                    "changed_files": 1,
                    "head": {"sha": ("a" if lecturas_metadatos == 1 else "c") * 40},
                    "base": {"sha": "b" * 40},
                }
            return [{"filename": "src/uno.ts"}]

        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, consultar)

    def test_compare_con_limite_de_archivos_no_puede_acreditar_integridad(self):
        self.entorno["GITHUB_EVENT_NAME"] = "push"
        self.guardar_evento({"before": "a" * 40, "after": "b" * 40})
        respuesta = {"files": [{"filename": f"src/{n}.ts"} for n in range(300)]}
        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, lambda ruta: respuesta)

    def test_push_desde_licencia_usa_arboles_completos_al_llegar_al_limite(self):
        self.entorno["GITHUB_EVENT_NAME"] = "push"
        anterior, actual = "a" * 40, "b" * 40
        arbol_anterior, arbol_actual = "c" * 40, "d" * 40
        self.guardar_evento({"before": anterior, "after": actual})
        prefijo = "/repos/equipo/plataforma"
        fuentes = [f"src/archivo-{i}.ts" for i in range(300)]
        rutas_requeridas = [MODULO.WORKFLOW, MODULO.RECIPE, MODULO.DIAGRAM]
        licencia = {"type": "blob", "path": "LICENSE", "sha": "e" * 40, "mode": "100644"}
        respuestas = {
            f"{prefijo}/compare/{anterior}...{actual}?per_page=100&page=1": {
                "files": [{"filename": path} for path in fuentes]
            },
            f"{prefijo}/commits/{anterior}": {
                "sha": anterior,
                "commit": {"tree": {"sha": arbol_anterior}},
            },
            f"{prefijo}/commits/{actual}": {
                "sha": actual,
                "commit": {"tree": {"sha": arbol_actual}},
            },
            f"{prefijo}/git/trees/{arbol_anterior}?recursive=1": {
                "sha": arbol_anterior,
                "truncated": False,
                "tree": [licencia],
            },
            f"{prefijo}/git/trees/{arbol_actual}?recursive=1": {
                "sha": arbol_actual,
                "truncated": False,
                "tree": [
                    licencia,
                    *[
                        {"type": "blob", "path": path, "sha": "f" * 40, "mode": "100644"}
                        for path in fuentes + rutas_requeridas
                    ],
                ],
            },
        }
        cambios = MODULO.changes_from_github(self.entorno, respuestas.__getitem__)
        self.assertCountEqual(cambios, fuentes + rutas_requeridas)
        self.assertEqual(MODULO.validate_changes(cambios), [])

    def test_push_inicial_sin_arbol_verificable_falla_cerrado(self):
        self.entorno["GITHUB_EVENT_NAME"] = "push"
        self.guardar_evento({"before": "0" * 40, "after": "b" * 40})
        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, lambda ruta: {})

    def respuestas_push_inicial(self):
        self.entorno["GITHUB_EVENT_NAME"] = "push"
        self.guardar_evento({"before": "0" * 40, "after": "b" * 40})
        return {
            "/repos/equipo/plataforma/commits/" + "b" * 40: {
                "sha": "b" * 40,
                "commit": {"tree": {"sha": "c" * 40}},
            },
            "/repos/equipo/plataforma/git/trees/" + "c" * 40 + "?recursive=1": {
                "sha": "c" * 40,
                "truncated": False,
                "tree": [
                    {"path": ruta, "type": "blob", "sha": "d" * 40}
                    for ruta in (
                        ".github/workflows/ci-cd-media-platform.yml",
                        "docs/ci/recipes/developer-push-flow.md",
                        "docs/ci/diagrams/pipeline-overview.mmd",
                    )
                ],
            },
        }

    def test_primer_push_completo_aprueba_sin_consultar_historial(self):
        respuestas = self.respuestas_push_inicial()
        archivos = MODULO.changes_from_github(self.entorno, respuestas.__getitem__)
        self.assertEqual(len(archivos), 3)
        self.assertEqual(MODULO.validate_changes(archivos), [])

    def test_primer_push_con_arbol_truncado_se_rechaza(self):
        respuestas = self.respuestas_push_inicial()
        arbol = respuestas["/repos/equipo/plataforma/git/trees/" + "c" * 40 + "?recursive=1"]
        arbol["truncated"] = True
        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, respuestas.__getitem__)

    def test_primer_push_sin_indicador_integridad_se_rechaza(self):
        respuestas = self.respuestas_push_inicial()
        arbol = respuestas["/repos/equipo/plataforma/git/trees/" + "c" * 40 + "?recursive=1"]
        arbol.pop("truncated")
        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, respuestas.__getitem__)

    def test_primer_push_sin_receta_requerida_se_rechaza(self):
        respuestas = self.respuestas_push_inicial()
        arbol = respuestas["/repos/equipo/plataforma/git/trees/" + "c" * 40 + "?recursive=1"]
        arbol["tree"] = [entrada for entrada in arbol["tree"] if "recipes/" not in entrada["path"]]
        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, respuestas.__getitem__)

    def test_primer_push_sha_distinto_del_evento_se_rechaza(self):
        respuestas = self.respuestas_push_inicial()
        respuestas["/repos/equipo/plataforma/commits/" + "b" * 40]["sha"] = "e" * 40
        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, respuestas.__getitem__)

    def test_push_valido_incluye_ruta_anterior_de_renombrado(self):
        self.entorno["GITHUB_EVENT_NAME"] = "push"
        self.guardar_evento({"before": "a" * 40, "after": "b" * 40})
        respuesta = {
            "files": [{"filename": "nuevo.yml", "previous_filename": "docker/anterior.yml"}]
        }
        self.assertEqual(
            MODULO.changes_from_github(self.entorno, lambda ruta: respuesta),
            ["nuevo.yml", "docker/anterior.yml"],
        )

    def test_sin_token_no_omite_verificacion(self):
        self.guardar_evento({"number": 17})
        self.entorno.pop("GITHUB_TOKEN")
        with self.assertRaises(ValueError):
            MODULO.changes_from_github(self.entorno, lambda ruta: {})

    def test_ejecucion_manual_solo_valida_estructura(self):
        self.entorno["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.assertEqual(MODULO.changes_from_github(self.entorno), [])


if __name__ == "__main__":
    unittest.main()
