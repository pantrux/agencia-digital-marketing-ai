"""Prueba mínimos declarativos de fases sin consultar ni modificar máquinas reales."""

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_phase", PROJECT / "scripts/ci/validate_phase.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

FOUNDATION_FILES = (
    ".github/workflows/ci-cd-media-platform.yml",
    "sre/smoke-test-e2e.sh",
    "sre/auto-rollback.sh",
    "docs/ci/README.md",
    "docs/ci/diagrams/pipeline-overview.mmd",
    "docs/ci/runbooks/rb-01-gpu-bridge-unreachable.md",
    "docs/ci/runbooks/rb-02-cloudflare-d1-migration-fail.md",
    "docs/ci/runbooks/rb-03-agent-loop-or-openai-degraded.md",
    "docs/ci/runbooks/rb-04-r2-nas-sync-backlog.md",
    "docs/ci/runbooks/rb-05-kitsu-immich-db-recovery.md",
)
MEDIA_FILES = (
    "vcenter/01-deploy-media-vm.ps1",
    "vcenter/media-vm-config.ps1",
    "vcenter/media-vcenter-trust-format.ps1",
    "vcenter/Get-VCenterCertificate.ps1",
    "vcenter/Get-VCenterCertificateBundle.ps1",
    "vcenter/Register-VCenterTrust.ps1",
    "vcenter/Register-VCenterCA.ps1",
    "vcenter/Test-MediaCloudInit.ps1",
    "vcenter/Test-PowerCLIContracts.ps1",
    "vcenter/New-MediaBaselineSnapshot.ps1",
    "vcenter/finalize-media-network.py",
    "vcenter/bootstrap-media-vm.sh",
    "vcenter/README.md",
    "requirements-dev.txt",
    "tests/test_vcenter_config.py",
    "tests/test_vcenter_trust_format.py",
    "tests/test_cloud_init_probe.py",
    "tests/test_vcenter_snapshots.py",
    "tests/test_media_network.py",
    "tests/test_media_bootstrap.py",
)
STORAGE_FILES = (
    "storage/02-setup-synology-storage.ps1",
    "storage/synology_storage_audit.py",
    "storage/synology_storage_apply.py",
    "storage/configure-nfs-client.sh",
    "storage/nfs_client_config.py",
    "storage/Map-AgencySmb.ps1",
    "storage/verify_smb_session.py",
    "storage/requirements-synology.txt",
    "storage/README-Synology.md",
    "storage/README-NFS-Linux.md",
    "storage/README-SMB-Windows.md",
    "docs/decisions/ADR-001-volumen-producciones.md",
    "tests/test_synology_audit.py",
    "tests/test_synology_storage_plan.py",
    "tests/test_nfs_client.py",
    "tests/test_smb_mapping.py",
    "tests/test_smb_session.py",
)

GPU_CRITICAL_FILES = (
    "gpu-bridge-win11/bridge_common.py",
    "gpu-bridge-win11/bridge_supervisor.py",
    "gpu-bridge-win11/metrics_service.py",
    "gpu-bridge-win11/immich_service.py",
    "gpu-bridge-win11/whisper_service.py",
    "gpu-bridge-win11/face_models.py",
    "gpu-bridge-win11/face_adapter.py",
    "gpu-bridge-win11/face-artifacts.json",
    "gpu-bridge-win11/immich-artifacts.json",
    "gpu-bridge-win11/whisper-artifacts.json",
    "gpu-bridge-win11/start-gpu-bridge.ps1",
    "gpu-bridge-win11/Stop-GpuBridge.ps1",
    "gpu-bridge-win11/Configure-GpuFirewall.ps1",
    "gpu-bridge-win11/Apply-GpuFirewallElevated.ps1",
    "gpu-bridge-win11/render-worker/src/server.ts",
    "gpu-bridge-win11/render-worker/package-lock.json",
    "scripts/ci/check_gpu_dependencies.py",
)

APPLICATION_CRITICAL_FILES = (
    "docker/docker-compose.yml",
    "docker/compose.synthetic.env",
    "scripts/ci/check_compose_base.py",
    "docker/phase4-runtime.json",
    "docker/verify_phase4_runtime.py",
    "docker/kitsu-ui/compose.yml",
    "docker/immich-server/compose.lan.yml",
    "docker/n8n/compose.private.yml",
    "docker/supoclip/compose.functional.yml",
    "docker/moneyprinter/compose.lan.yml",
    "docker/immich-ml-auth/proxy.py",
    "docker/supoclip/speaches_provider.py",
    "docker/moneyprinter/speaches_provider.py",
    "docker/r2_service.py",
    "docker/r2_service_boot.py",
    "docker/r2-sync-consumer/agency-r2-sync.service",
    "docker/r2-sync-consumer/agency-r2-sync.timer",
    "docker/r2-sync-consumer/agency-nas-r2-project.service",
    "docker/r2-sync-consumer/sync_nas_entry.py",
    "docker/r2-sync-consumer/sync_provision.py",
    "docker/r2-sync-consumer/sync_ledger.py",
    "docker/observability/compose.nas.yml",
    "docker/observability/alertmanager.nas.Dockerfile",
    "docker/observability/alertmanager_smtp_profile.py",
    "docker/observability/nas_alertmanager_filter.py",
    "docker/observability/prometheus.alertmanager.nas.yml",
    "sre/sre-backup-321.sh",
    "sre/backup_321_verify_r2_restore.py",
    "sre/cron.d/agency-backup-321-daily",
    "sre/backup_321_orchestrator.py",
    "sre/backup_321_verify_daily_restore.py",
    "sre/backup_321_d1_clock.py",
    "sre/d1_backup_metrics.py",
    "sre/systemd/agency-d1-backup-metrics.service",
    "sre/systemd/agency-d1-backup-metrics.timer",
    "tests/test_d1_backup_metrics.py",
    "sre/backup_321_openclaw_clock.py",
    "sre/backup_321_openclaw_transport.py",
    "sre/backup_321_verify_openclaw_restore.py",
    "sre/systemd/agency-backup-321-daily.service",
    "sre/systemd/agency-backup-d1-clock.service",
    "sre/systemd/agency-backup-d1-clock.timer",
    "sre/systemd/agency-backup-openclaw-clock.service",
    "sre/systemd/agency-backup-openclaw-clock.timer",
    "cloudflare-saas/studio-d1-backup/src/index.ts",
    "cloudflare-saas/studio-d1-backup/wrangler.example.jsonc",
    "sre/vault_change_queue.py",
    "sre/pause_vault_quota_archive_v2.py",
    "tests/test_vault_changes.py",
    "tests/test_r2_sync_provision.py",
    "docs/specs/FASE-4-APLICACIONES-Y-BACKUPS.md",
)
SAAS_CRITICAL_FILES = (
    "docs/specs/FASE-6-CLOUDFLARE-Y-PORTAL.md",
    "ingress/phase6_ingress.py",
    "ingress/npm_phase6_operator.py",
    "ingress/agency-studio-tunnel.service",
    "ingress/origin_relay.py",
    "openclaw-agency-setup/sofia-broker/sofia_broker.py",
    "openclaw-agency-setup/sofia-broker/sofia_mtls_proxy.py",
    "openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/index.mjs",
    "openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/openclaw.plugin.json",
    "openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/package.json",
    "cloudflare-saas/studio-api-worker/src/index.ts",
    "cloudflare-saas/studio-api-worker/src/billing.ts",
    "cloudflare-saas/studio-api-worker/src/brands.ts",
    "cloudflare-saas/studio-api-worker/src/ingest.ts",
    "cloudflare-saas/studio-api-worker/src/kits.ts",
    "cloudflare-saas/studio-api-worker/wrangler.example.jsonc",
    "cloudflare-saas/studio-portal-web/app/page.tsx",
    "cloudflare-saas/studio-portal-web/next.config.mjs",
    "cloudflare-saas/studio-portal-web/package-lock.json",
    "cloudflare-saas/studio-portal-web/pages-worker.js",
    "cloudflare-saas/migrations/0000_resource_owner.sql",
    "cloudflare-saas/migrations/0001_schema.sql",
    "cloudflare-saas/migrations/0002_billing.sql",
    "cloudflare-saas/migrations/0003_delivery_kits.sql",
    "cloudflare-saas/migrations/0014_upload_abort_reason.sql",
    "cloudflare-saas/migrations/0015_brand_profiles.sql",
    "tests/test_phase6_ingress.py",
    "tests/test_npm_phase6_operator.py",
    "tests/test_sofia_broker.py",
    "tests/test_sofia_mtls_proxy.py",
    "tests/test_d1_saas_schema.py",
    "tests-ts/studio-ingest.test.ts",
    "tests-ts/studio-identity.test.ts",
    "tests-ts/studio-billing.test.ts",
    "tests-ts/studio-delivery-kits.test.ts",
    "tests-ts/studio-chat.test.ts",
)
PRODUCTION_CRITICAL_FILES = (
    "cloudflare-saas/studio-api-worker/src/manual-adjustments.ts",
    "cloudflare-saas/migrations/0016_production_manual_adjustments.sql",
    "cloudflare-saas/migrations/0017_upload_put_observations.sql",
    "sre/sre_control.py",
    "sre/rollback_openclaw_roster.py",
    "tests/fixtures/sre/simulation-ok.json",
    "docker/n8n/production_sofia.py",
    "docker/n8n/production_mateo.py",
    "docker/n8n/production_valeria.py",
    "docker/n8n/production_lucas.py",
    "docker/n8n/production_elena.py",
    "docker/n8n/production_delivery.py",
    "docker/n8n/production_publish.py",
    "docker/n8n/agency-production-publish.timer",
    "docker/kitsu-zou/provision_order.py",
    "scripts/ci/check_env.py",
)


class ValidacionFases(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in (
            *FOUNDATION_FILES,
            *MEDIA_FILES,
            *STORAGE_FILES,
            *getattr(MODULE, "GPU_BRIDGE_FILES", ()),
            *getattr(MODULE, "APPLICATIONS_BACKUP_FILES", ()),
            *getattr(MODULE, "OPENCLAW_MCP_FILES", ()),
            *getattr(MODULE, "PORTAL_SAAS_FILES", ()),
            *getattr(MODULE, "PRODUCTION_SRE_FILES", ()),
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Prueba local sin infraestructura.\n", encoding="utf-8")

    def manifest(self, phase):
        return {
            "schema_version": 1,
            "phase": phase,
            "deployment_enabled": False,
            "scope": "Comprobación declarativa sin acreditar producción.",
            "required_files": list(FOUNDATION_FILES)
            + (list(MEDIA_FILES) if phase >= 1 else [])
            + (list(STORAGE_FILES) if phase >= 2 else [])
            + (list(getattr(MODULE, "GPU_BRIDGE_FILES", ())) if phase >= 3 else [])
            + (list(getattr(MODULE, "APPLICATIONS_BACKUP_FILES", ())) if phase >= 4 else [])
            + (list(getattr(MODULE, "OPENCLAW_MCP_FILES", ())) if phase >= 5 else [])
            + (list(getattr(MODULE, "PORTAL_SAAS_FILES", ())) if phase >= 6 else [])
            + (list(getattr(MODULE, "PRODUCTION_SRE_FILES", ())) if phase >= 7 else []),
            "pending_infrastructure": ["Verificaciones reales separadas del control CI."],
        }

    def test_fase_cero_valida_con_cd_deshabilitado(self):
        self.assertEqual(MODULE.validate_manifest(self.root, self.manifest(0), 0), [])

    def test_fase_uno_exige_tambien_cimientos_y_acepta_estructura_completa(self):
        self.assertEqual(MODULE.validate_manifest(self.root, self.manifest(1), 1), [])

    def test_fase_declarada_puede_usarse_sin_forzar_un_numero_en_ci(self):
        self.assertEqual(MODULE.validate_manifest(self.root, self.manifest(1)), [])

    def test_fase_dos_valida_sin_evidencia_local_ni_contactar_infraestructura(self):
        self.assertFalse((self.root / ".tools").exists())
        self.assertEqual(MODULE.validate_manifest(self.root, self.manifest(2), 2), [])

    def test_fases_futuras_se_rechazan_aunque_estan_todos_los_minimos_actuales(self):
        for phase in (8, 9):
            with self.subTest(phase=phase):
                self.assertTrue(MODULE.validate_manifest(self.root, self.manifest(phase), phase))

    def test_fase_dos_no_permite_omitir_minimos_de_almacenamiento(self):
        for name in STORAGE_FILES:
            with self.subTest(file=name):
                manifest = self.manifest(2)
                manifest["required_files"].remove(name)
                errors = MODULE.validate_manifest(self.root, manifest, 2)
                self.assertTrue(any("mínimo" in error and name in error for error in errors))

    def test_fase_dos_exige_archivos_fisicos_y_conserva_los_minimos_de_vm(self):
        for name in (*STORAGE_FILES, "vcenter/Test-PowerCLIContracts.ps1"):
            with self.subTest(file=name):
                path = self.root / name
                path.unlink()
                errors = MODULE.validate_manifest(self.root, self.manifest(2), 2)
                self.assertIn(f"Falta archivo obligatorio: {name}", errors)
                path.write_text("Prueba local.\n", encoding="utf-8")

    def test_contrato_sdk_de_fase_uno_sigue_activo_hasta_fase_siete(self):
        workflow = yaml.safe_load(
            (PROJECT / ".github/workflows/ci-cd-media-platform.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["gate-5-infra"]["steps"]
        contracts = [
            step for step in steps if "./vcenter/Test-PowerCLIContracts.ps1" in step.get("run", "")
        ]
        self.assertEqual(len(contracts), 1)
        clauses = contracts[0]["if"].split(" || ")
        allowed = set()
        for clause in clauses:
            match = re.fullmatch(r"steps\.phase\.outputs\.current == '([0-9]+)'", clause)
            self.assertIsNotNone(match, "La condición SDK debe declarar las fases admitidas.")
            allowed.add(int(match.group(1)))
        self.assertEqual(allowed, {1, 2, 3, 4, 5, 6, 7})

    def test_fase_tres_valida_sin_infraestructura_y_conserva_los_minimos_anteriores(self):
        manifest = self.manifest(3)
        self.assertEqual(MODULE.validate_manifest(self.root, manifest, 3), [])
        self.assertTrue(set(GPU_CRITICAL_FILES) <= set(manifest["required_files"]))
        self.assertFalse((self.root / ".tools").exists())
        for name in ("storage/Map-AgencySmb.ps1", "vcenter/Test-PowerCLIContracts.ps1"):
            with self.subTest(file=name):
                candidate = self.manifest(3)
                candidate["required_files"].remove(name)
                self.assertTrue(MODULE.validate_manifest(self.root, candidate, 3))

    def test_minimos_de_fase_tres_existen_en_las_fuentes_reales(self):
        self.assertEqual(MODULE.validate_manifest(PROJECT, self.manifest(3), 3), [])

    def test_fase_tres_no_puede_ocultar_servicios_autenticacion_o_locks(self):
        for name in GPU_CRITICAL_FILES:
            with self.subTest(file=name):
                manifest = self.manifest(3)
                self.assertIn(name, manifest["required_files"])
                manifest["required_files"].remove(name)
                errors = MODULE.validate_manifest(self.root, manifest, 3)
                self.assertTrue(any("mínimo" in error and name in error for error in errors))

    def test_fase_tres_requiere_archivos_fisicos_y_rechaza_publicacion_automatica(self):
        manifest = self.manifest(3)
        manifest["deployment_enabled"] = True
        self.assertTrue(MODULE.validate_manifest(self.root, manifest, 3))
        path = self.root / "gpu-bridge-win11/bridge_common.py"
        self.assertTrue(path.is_file())
        path.unlink()
        self.assertIn(
            "Falta archivo obligatorio: gpu-bridge-win11/bridge_common.py",
            MODULE.validate_manifest(self.root, self.manifest(3), 3),
        )

    def test_fase_solicitada_distinta_del_manifiesto_se_rechaza(self):
        self.assertTrue(MODULE.validate_manifest(self.root, self.manifest(0), 1))
        self.assertIn(
            "La fase solicitada no coincide con las capacidades declaradas.",
            MODULE.validate_manifest(self.root, self.manifest(3), 4),
        )

    def test_fase_cuatro_valida_con_fuentes_reales_y_sin_evidencia_privada(self):
        manifest = self.manifest(4)
        self.assertFalse((self.root / ".tools").exists())
        self.assertTrue(set(APPLICATION_CRITICAL_FILES) <= set(manifest["required_files"]))
        self.assertEqual(MODULE.validate_manifest(self.root, manifest, 4), [])
        self.assertEqual(MODULE.validate_manifest(PROJECT, manifest, 4), [])

    def test_fase_cuatro_exige_minimos_nuevos_y_heredados(self):
        for name in (*APPLICATION_CRITICAL_FILES, "gpu-bridge-win11/bridge_common.py"):
            with self.subTest(file=name):
                manifest = self.manifest(4)
                self.assertIn(name, manifest["required_files"])
                manifest["required_files"].remove(name)
                self.assertIn(
                    f"La fase 4 debe declarar el archivo mínimo: {name}",
                    MODULE.validate_manifest(self.root, manifest, 4),
                )

    def test_fase_cuatro_exige_archivo_fisico_y_fase_tres_no_lo_necesita(self):
        name = "docker/docker-compose.yml"
        (self.root / name).unlink()
        self.assertIn(
            f"Falta archivo obligatorio: {name}",
            MODULE.validate_manifest(self.root, self.manifest(4), 4),
        )
        self.assertEqual(MODULE.validate_manifest(self.root, self.manifest(3), 3), [])

    def test_fase_cinco_valida_y_exige_catalogo_y_adaptadores(self):
        manifest = self.manifest(5)
        self.assertEqual(MODULE.validate_manifest(PROJECT, manifest, 5), [])
        name = "openclaw-agency-setup/openclaw-plugin/catalog.json"
        manifest["required_files"].remove(name)
        self.assertIn(
            f"La fase 5 debe declarar el archivo mínimo: {name}",
            MODULE.validate_manifest(self.root, manifest, 5),
        )

    def test_fase_seis_valida_sin_infraestructura_y_hereda_fase_cinco(self):
        self.assertFalse((self.root / ".tools").exists())
        manifest = self.manifest(6)
        self.assertEqual(MODULE.validate_manifest(self.root, manifest, 6), [])
        self.assertEqual(
            set(manifest["required_files"]) - set(self.manifest(5)["required_files"]),
            set(MODULE.PORTAL_SAAS_FILES),
        )
        self.assertTrue(set(SAAS_CRITICAL_FILES) <= set(manifest["required_files"]))

    def test_minimos_de_fase_seis_existen_en_las_fuentes_reales(self):
        self.assertEqual(MODULE.validate_manifest(PROJECT, self.manifest(6), 6), [])

    def test_fase_seis_requiere_declarar_los_minimos_nuevos_y_heredados(self):
        for name in (*SAAS_CRITICAL_FILES, "openclaw-agency-setup/openclaw-plugin/catalog.json"):
            with self.subTest(file=name):
                manifest = self.manifest(6)
                manifest["required_files"] = [
                    file for file in manifest["required_files"] if file != name
                ]
                self.assertIn(
                    f"La fase 6 debe declarar el archivo mínimo: {name}",
                    MODULE.validate_manifest(self.root, manifest, 6),
                )

    def test_minimos_saas_fisicos_solo_se_exigen_al_declarar_fase_seis(self):
        for name in SAAS_CRITICAL_FILES:
            path = self.root / name
            self.assertTrue(path.is_file())
            path.unlink()
        errors = MODULE.validate_manifest(self.root, self.manifest(6), 6)
        self.assertTrue(
            all(f"Falta archivo obligatorio: {name}" in errors for name in SAAS_CRITICAL_FILES)
        )
        self.assertEqual(MODULE.validate_manifest(self.root, self.manifest(5), 5), [])

    def test_fase_siete_valida_fuentes_y_hereda_los_minimos_saas(self):
        self.assertFalse((self.root / ".tools").exists())
        manifest = self.manifest(7)
        self.assertEqual(MODULE.validate_manifest(self.root, manifest, 7), [])
        self.assertEqual(MODULE.validate_manifest(PROJECT, manifest, 7), [])
        self.assertTrue(set(self.manifest(6)["required_files"]) < set(manifest["required_files"]))
        self.assertTrue(set(PRODUCTION_CRITICAL_FILES) <= set(manifest["required_files"]))

    def test_fase_siete_rechaza_omisiones_del_manifiesto_y_del_disco(self):
        for name in (*PRODUCTION_CRITICAL_FILES, "cloudflare-saas/studio-api-worker/src/kits.ts"):
            with self.subTest(file=name):
                manifest = self.manifest(7)
                manifest["required_files"].remove(name)
                self.assertIn(
                    f"La fase 7 debe declarar el archivo mínimo: {name}",
                    MODULE.validate_manifest(self.root, manifest, 7),
                )
                path = self.root / name
                path.unlink()
                self.assertIn(
                    f"Falta archivo obligatorio: {name}",
                    MODULE.validate_manifest(self.root, self.manifest(7), 7),
                )
                path.write_text("Prueba local.\n", encoding="utf-8")

    def test_habilitar_cd_se_rechaza_en_todas_las_fases_admitidas(self):
        for phase in range(8):
            with self.subTest(phase=phase):
                manifest = self.manifest(phase)
                manifest["deployment_enabled"] = True
                self.assertTrue(
                    any(
                        "deployment_enabled=false" in error
                        for error in MODULE.validate_manifest(self.root, manifest, phase)
                    )
                )

    def test_lista_vacia_no_puede_omitir_los_minimos(self):
        manifest = self.manifest(1)
        manifest["required_files"] = []
        self.assertTrue(MODULE.validate_manifest(self.root, manifest, 1))

    def test_fase_uno_rechaza_faltar_snapshot_probe_finalizador_o_bootstrap(self):
        for name in (
            "vcenter/New-MediaBaselineSnapshot.ps1",
            "vcenter/Test-MediaCloudInit.ps1",
            "vcenter/finalize-media-network.py",
            "vcenter/bootstrap-media-vm.sh",
            "docs/ci/README.md",
        ):
            with self.subTest(file=name):
                path = self.root / name
                path.unlink()
                self.assertTrue(MODULE.validate_manifest(self.root, self.manifest(1), 1))
                path.write_text("Prueba local.\n", encoding="utf-8")

    def test_no_admite_rutas_fuera_del_directorio_del_proyecto(self):
        manifest = self.manifest(0)
        manifest["required_files"].append("../boveda.env")
        self.assertTrue(MODULE.validate_manifest(self.root, manifest, 0))

    def test_campos_ausentes_y_tipo_de_fase_incorrecto_no_se_aceptan(self):
        for manifest in ({}, {"schema_version": 1, "phase": True}):
            with self.subTest(manifest=manifest):
                self.assertTrue(MODULE.validate_manifest(self.root, manifest, 0))


if __name__ == "__main__":
    unittest.main()
