"""Impide confundir la validación de cimientos con un despliegue de producción."""

import argparse
import json
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
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
MEDIA_VM_FILES = (
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
GPU_BRIDGE_FILES = (
    "gpu-bridge-win11/bridge_common.py",
    "gpu-bridge-win11/bridge_supervisor.py",
    "gpu-bridge-win11/launch_service.py",
    "gpu-bridge-win11/metrics_service.py",
    "gpu-bridge-win11/immich_service.py",
    "gpu-bridge-win11/immich_portability.py",
    "gpu-bridge-win11/whisper_service.py",
    "gpu-bridge-win11/face_models.py",
    "gpu-bridge-win11/face_adapter.py",
    "gpu-bridge-win11/gpu_artifacts.py",
    "gpu-bridge-win11/install_gpu_bridge.py",
    "gpu-bridge-win11/install_inference.py",
    "gpu-bridge-win11/install_render.py",
    "gpu-bridge-win11/face-artifacts.json",
    "gpu-bridge-win11/immich-artifacts.json",
    "gpu-bridge-win11/whisper-artifacts.json",
    "gpu-bridge-win11/requirements-tests-cpu.txt",
    "gpu-bridge-win11/requirements-metrics.lock",
    "gpu-bridge-win11/requirements-immich.in",
    "gpu-bridge-win11/requirements-whisper.in",
    "gpu-bridge-win11/requirements-whisper.lock",
    "gpu-bridge-win11/start-gpu-bridge.ps1",
    "gpu-bridge-win11/Stop-GpuBridge.ps1",
    "gpu-bridge-win11/Configure-GpuFirewall.ps1",
    "gpu-bridge-win11/Apply-GpuFirewallElevated.ps1",
    "gpu-bridge-win11/README.md",
    "gpu-bridge-win11/render-worker/package.json",
    "gpu-bridge-win11/render-worker/package-lock.json",
    "gpu-bridge-win11/render-worker/versions.json",
    "gpu-bridge-win11/render-worker/tsconfig.json",
    "eslint.config.js",
    "gpu-bridge-win11/render-worker/src/server.ts",
    "gpu-bridge-win11/render-worker/src/start.ts",
    "gpu-bridge-win11/render-worker/src/backend.ts",
    "gpu-bridge-win11/render-worker/src/security.ts",
    "gpu-bridge-win11/render-worker/src/contracts.ts",
    "gpu-bridge-win11/render-worker/src/config.ts",
    "gpu-bridge-win11/render-worker/src/process.ts",
    "gpu-bridge-win11/render-worker/src/nas.ts",
    "gpu-bridge-win11/render-worker/src/scratch.ts",
    "gpu-bridge-win11/render-worker/src/media.ts",
    "gpu-bridge-win11/render-worker/src/qa.ts",
    "gpu-bridge-win11/render-worker/src/remotion.ts",
    "gpu-bridge-win11/render-worker/src/composition-cache.ts",
    "gpu-bridge-win11/render-worker/composition/index.tsx",
    "gpu-bridge-win11/render-worker/composition/Reel.tsx",
    "scripts/ci/check_gpu_dependencies.py",
    "tests/test_ci_gpu_dependencies.py",
    "tests/test_gpu_common.py",
    "tests/test_gpu_supervisor.py",
    "tests/test_gpu_stop.py",
    "tests/test_gpu_firewall.py",
    "tests/test_gpu_firewall_wrapper.py",
    "tests/test_gpu_immich.py",
    "tests/test_gpu_whisper.py",
    "tests/test_gpu_face_models.py",
    "tests/test_gpu_face_adapter.py",
    "tests/test_gpu_metrics.py",
    "tests/test_gpu_artifacts.py",
    "tests/test_gpu_install.py",
    "tests/test_gpu_inference_install.py",
    "tests/test_gpu_render_install.py",
    "docs/specs/FASE-3-PUENTE-GPU.md",
    "docs/decisions/ADR-003-modelos-faciales-comerciales.md",
)
APPLICATIONS_BACKUP_FILES = (
    "docs/specs/FASE-4-APLICACIONES-Y-BACKUPS.md",
    "docker/stack-artifacts.json",
    "docker/validate_stack_artifacts.py",
    "docker/phase4-runtime.json",
    "docker/verify_phase4_compose.py",
    "docker/verify_phase4_runtime.py",
    "docker/docker-compose.yml",
    "docker/compose.synthetic.env",
    "scripts/ci/check_compose_base.py",
    "docker/kitsu-ui/compose.yml",
    "docker/kitsu-zou/agency-kitsu-zou.service",
    "docker/kitsu-zou/preview_mount_guard.py",
    "docker/immich-server/compose.lan.yml",
    "docker/immich-server/agency-immich-lan.service",
    "docker/immich-ml-auth/proxy.py",
    "docker/n8n/compose.private.yml",
    "docker/n8n/agency-n8n-private.service",
    "docker/supoclip/compose.functional.yml",
    "docker/supoclip/compose.integration.override.yml",
    "docker/supoclip/compose.local-model.yml",
    "docker/supoclip/compose.transcript-timestamps.yml",
    "docker/supoclip/agency-supoclip-private.service",
    "docker/supoclip/speaches_provider.py",
    "docker/moneyprinter/compose.lan.yml",
    "docker/moneyprinter/agency-moneyprinter-lan.service",
    "docker/moneyprinter/speaches_provider.py",
    "docker/r2_service.py",
    "docker/r2_service_boot.py",
    "docker/r2-sync-consumer/agency-r2-sync.service",
    "docker/r2-sync-consumer/agency-r2-sync.timer",
    "docker/r2-sync-consumer/agency-nas-r2-project.service",
    "docker/r2-sync-consumer/sync_nas_entry.py",
    "docker/r2-sync-consumer/sync_provision.py",
    "tests/test_r2_sync_provision.py",
    "docker/r2-sync-consumer/sync_http.py",
    "docker/r2-sync-consumer/sync_protocol.py",
    "docker/r2-sync-consumer/sync_storage.py",
    "docker/r2-sync-consumer/sync_ledger.py",
    "docker/observability/compose.nas.yml",
    "docker/observability/otel-collector.nas-v2.yml",
    "docker/observability/agency-observability-nas.service",
    "docker/observability/nas_operator.py",
    "docker/observability/alertmanager.nas.Dockerfile",
    "docker/observability/alertmanager_smtp_profile.py",
    "docker/observability/nas_alertmanager_filter.py",
    "docker/observability/prometheus.alertmanager.nas.yml",
    "sre/sre-backup-321.sh",
    "sre/backup_321_daily.py",
    "sre/backup_321_verify_r2_restore.py",
    "sre/backup_321_daily_retention.py",
    "sre/cron.d/agency-backup-321-daily",
    "sre/d1_export.py",
    "sre/openclaw_backup.py",
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
    "sre/systemd/agency-backup-321-daily.timer",
    "sre/systemd/agency-backup-d1-clock.service",
    "sre/systemd/agency-backup-d1-clock.timer",
    "sre/systemd/agency-backup-openclaw-clock.service",
    "sre/systemd/agency-backup-openclaw-clock.timer",
    "cloudflare-saas/studio-d1-backup/src/index.ts",
    "cloudflare-saas/studio-d1-backup/wrangler.example.jsonc",
    "sre/vault_change_worker.py",
    "sre/vault_change_queue.py",
    "sre/pause_vault_quota_archive_v2.py",
    "tests/test_vault_changes.py",
    "sre/README-backup-321-orchestrator.md",
)
OPENCLAW_MCP_FILES = (
    "docs/specs/FASE-5-OPENCLAW-Y-MCP.md",
    "openclaw-agency-setup/setup.sh",
    "openclaw-agency-setup/patch_config.py",
    "openclaw-agency-setup/stage_skills.py",
    "openclaw-agency-setup/openclaw-plugin/catalog.json",
    "openclaw-agency-setup/openclaw-plugin/index.mjs",
    "openclaw-agency-setup/openclaw-plugin/openclaw.plugin.json",
    "openclaw-agency-setup/openclaw-plugin/package.json",
    "openclaw-agency-setup/mcp_servers/common.py",
    "openclaw-agency-setup/mcp_servers/serve.py",
    "openclaw-agency-setup/mcp_servers/call_stdio.py",
    "openclaw-agency-setup/mcp_servers/export_catalog.py",
    "openclaw-agency-setup/mcp_servers/media_inspector.py",
    "openclaw-agency-setup/mcp_servers/immich_broll.py",
    "openclaw-agency-setup/mcp_servers/remotion_gpu.py",
    "openclaw-agency-setup/mcp_servers/kitsu_review.py",
    "openclaw-agency-setup/mcp_servers/requirements-linux-x86_64.txt",
    "openclaw-agency-setup/upstream-skills/manifest.json",
    "openclaw-agency-setup/upstream-skills/local-manifest.json",
    "openclaw-agency-setup/upstream-skills/review_skills.py",
    "openclaw-agency-setup/skills/protocolo-agencia-audiovisual/SKILL.md",
    "openclaw-agency-setup/skills/operacion-worker-remotion-gpu/SKILL.md",
    "openclaw-agency-setup/skills/rubrica-qa-audiovisual/SKILL.md",
    "tests/test_mcp_media_inspector.py",
    "tests/test_mcp_immich_broll.py",
    "tests/test_mcp_remotion_gpu.py",
    "tests/test_mcp_kitsu_review.py",
    "tests/test_agency_upstream_skills.py",
)
PORTAL_SAAS_FILES = (
    "docs/specs/FASE-6-CLOUDFLARE-Y-PORTAL.md",
    "docs/specs/MERCADOPAGO-FASE-6.md",
    "docs/specs/ENTREGAS-FASE-6.md",
    "ingress/phase6_ingress.py",
    "ingress/npm_phase6_operator.py",
    "ingress/agency-studio-tunnel.service",
    "ingress/origin_relay.py",
    "ingress/README-origin-relay.md",
    "ingress/README.md",
    "openclaw-agency-setup/sofia-broker/sofia_broker.py",
    "openclaw-agency-setup/sofia-broker/sofia_mtls_proxy.py",
    "openclaw-agency-setup/sofia-broker/agency-sofia-mtls.service.review",
    "openclaw-agency-setup/sofia-broker/openclaw.review.json",
    "openclaw-agency-setup/sofia-broker/agency-sofia-broker.service.review",
    "openclaw-agency-setup/sofia-broker/agency-sofia-gateway.service.review",
    "openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/index.mjs",
    "openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/openclaw.plugin.json",
    "openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/package.json",
    "openclaw-agency-setup/sofia-broker/README.md",
    "cloudflare-saas/studio-api-worker/src/index.ts",
    "cloudflare-saas/studio-api-worker/src/platform.ts",
    "cloudflare-saas/studio-api-worker/src/identity.ts",
    "cloudflare-saas/studio-api-worker/src/orders.ts",
    "cloudflare-saas/studio-api-worker/src/ingest.ts",
    "cloudflare-saas/studio-api-worker/src/brands.ts",
    "cloudflare-saas/studio-api-worker/src/production-dispatch.ts",
    "cloudflare-saas/studio-api-worker/src/production-brief.ts",
    "cloudflare-saas/studio-api-worker/src/jobs.ts",
    "cloudflare-saas/studio-api-worker/src/mercadopago.ts",
    "cloudflare-saas/studio-api-worker/src/billing.ts",
    "cloudflare-saas/studio-api-worker/src/kits.ts",
    "cloudflare-saas/studio-api-worker/src/chat.ts",
    "cloudflare-saas/studio-api-worker/wrangler.example.jsonc",
    "scripts/ci/check_d1_migrations.py",
    "cloudflare-saas/studio-api-worker/INGESTA.md",
    "cloudflare-saas/studio-portal-web/package.json",
    "cloudflare-saas/studio-portal-web/package-lock.json",
    "cloudflare-saas/studio-portal-web/tsconfig.json",
    "cloudflare-saas/studio-portal-web/next.config.mjs",
    "cloudflare-saas/studio-portal-web/app/layout.tsx",
    "cloudflare-saas/studio-portal-web/app/page.tsx",
    "cloudflare-saas/studio-portal-web/app/studio.css",
    "cloudflare-saas/studio-portal-web/app/upload.ts",
    "cloudflare-saas/studio-portal-web/app/upload-duration.ts",
    "cloudflare-saas/studio-portal-web/pages-worker.js",
    "cloudflare-saas/studio-portal-web/pages-routes.json",
    "cloudflare-saas/studio-portal-web/public/_headers",
    "cloudflare-saas/migrations/0000_resource_owner.sql",
    "cloudflare-saas/migrations/0001_schema.sql",
    "cloudflare-saas/migrations/0002_billing.sql",
    "cloudflare-saas/migrations/0003_delivery_kits.sql",
    "cloudflare-saas/migrations/0004_production_dispatch.sql",
    "cloudflare-saas/migrations/0005_production_briefs.sql",
    "cloudflare-saas/migrations/0006_production_portal_origin.sql",
    "cloudflare-saas/migrations/0007_delivery_duration.sql",
    "cloudflare-saas/migrations/0008_production_qa_rejection.sql",
    "cloudflare-saas/migrations/0009_production_resolution.sql",
    "cloudflare-saas/migrations/0010_production_manual_review.sql",
    "cloudflare-saas/migrations/0011_production_output_format.sql",
    "cloudflare-saas/migrations/0012_delivery_prores.sql",
    "cloudflare-saas/migrations/0013_billing_webhook_retry.sql",
    "cloudflare-saas/migrations/0014_upload_abort_reason.sql",
    "cloudflare-saas/migrations/0015_brand_profiles.sql",
    "src/media/sync-contract.ts",
    "src/media/r2-signing.ts",
    "tests/test_phase6_ingress.py",
    "tests/test_npm_phase6_operator.py",
    "tests/test_sofia_broker.py",
    "tests/test_sofia_mtls_proxy.py",
    "tests/test_d1_resource_owner.py",
    "tests/test_d1_saas_schema.py",
    "tests-ts/r2-signing.test.ts",
    "tests-ts/sync-contract.test.ts",
    "tests-ts/studio-ingest.test.ts",
    "tests-ts/studio-identity.test.ts",
    "tests-ts/studio-mercadopago.test.ts",
    "tests-ts/studio-billing.test.ts",
    "tests-ts/studio-delivery-kits.test.ts",
    "tests-ts/studio-chat.test.ts",
)
PRODUCTION_SRE_FILES = (
    "cloudflare-saas/studio-api-worker/src/manual-adjustments.ts",
    "cloudflare-saas/migrations/0016_production_manual_adjustments.sql",
    "cloudflare-saas/migrations/0017_upload_put_observations.sql",
    "sre/sre_control.py",
    "sre/rollback_openclaw_roster.py",
    "docs/ci/recipes/sre-rollback.md",
    "scripts/ci/check_env.py",
    "docker/n8n/production_inbox.py",
    "docker/n8n/production_inbox_firewall.py",
    "docker/n8n/production_dispatch_workflow.py",
    "docker/n8n/production_handoff.py",
    "docker/n8n/production_intake.py",
    "docker/n8n/production_sofia.py",
    "docker/n8n/production_mateo.py",
    "docker/n8n/production_valeria.py",
    "docker/n8n/production_visual_rpc.mjs",
    "docker/n8n/production_lucas.py",
    "docker/n8n/production_elena.py",
    "docker/n8n/production_delivery.py",
    "docker/n8n/production_publish.py",
    "docker/n8n/agency-production-inbox.service",
    "docker/n8n/agency-production-handoff.service",
    "docker/n8n/agency-production-handoff.timer",
    "docker/n8n/agency-production-intake.service",
    "docker/n8n/agency-production-intake.timer",
    "docker/n8n/agency-production-sofia.service",
    "docker/n8n/agency-production-sofia.timer",
    "docker/n8n/agency-production-mateo.service",
    "docker/n8n/agency-production-mateo.timer",
    "docker/n8n/agency-production-valeria.service",
    "docker/n8n/agency-production-valeria.timer",
    "docker/n8n/agency-production-lucas.service",
    "docker/n8n/agency-production-lucas.timer",
    "docker/n8n/agency-production-elena.service",
    "docker/n8n/agency-production-elena.timer",
    "docker/n8n/agency-production-delivery.service",
    "docker/n8n/agency-production-publish.service",
    "docker/n8n/agency-production-publish.timer",
    "docker/kitsu-zou/provision_order.py",
    "tests/test_production_handoff.py",
    "tests/test_kitsu_project_provision.py",
    "tests/test_sre.py",
    "tests/fixtures/sre/simulation-ok.json",
)


def required_files_for_phase(phase: int) -> tuple[str, ...]:
    """Devuelve mínimos fijos; el manifiesto no puede reducir las obligaciones."""
    if phase == 0:
        return FOUNDATION_FILES
    if phase == 1:
        return FOUNDATION_FILES + MEDIA_VM_FILES
    if phase == 2:
        return FOUNDATION_FILES + MEDIA_VM_FILES + STORAGE_FILES
    if phase == 3:
        return FOUNDATION_FILES + MEDIA_VM_FILES + STORAGE_FILES + GPU_BRIDGE_FILES
    if phase == 4:
        return required_files_for_phase(3) + APPLICATIONS_BACKUP_FILES
    if phase == 5:
        return required_files_for_phase(4) + OPENCLAW_MCP_FILES
    if phase == 6:
        return required_files_for_phase(5) + PORTAL_SAAS_FILES
    if phase == 7:
        return required_files_for_phase(6) + PRODUCTION_SRE_FILES
    raise ValueError("La fase solicitada todavía no tiene un validador implementado.")


def validate_manifest(
    root: Path, manifest: object, requested_phase: int | None = None
) -> list[str]:
    """Valida capacidades de código, sin acreditar pruebas de máquinas reales."""
    if not isinstance(manifest, dict):
        return ["El manifiesto debe ser un objeto JSON."]
    errors = []
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        errors.append("La versión del esquema del manifiesto no es válida.")
    phase = manifest.get("phase")
    if type(phase) is not int or phase not in range(8):
        return errors + ["La fase declarada todavía no tiene un validador implementado."]
    if requested_phase is not None and (
        type(requested_phase) is not int or requested_phase != phase
    ):
        errors.append("La fase solicitada no coincide con las capacidades declaradas.")
    if manifest.get("deployment_enabled") is not False:
        errors.append("Las fases 0 a 7 exigen deployment_enabled=false.")
    if not isinstance(manifest.get("scope"), str) or not manifest["scope"].strip():
        errors.append("El manifiesto debe describir el alcance de su comprobación.")
    pending = manifest.get("pending_infrastructure")
    if not isinstance(pending, list) or any(not isinstance(item, str) for item in pending):
        errors.append("El manifiesto debe declarar una lista de verificaciones pendientes.")
    declared = manifest.get("required_files")
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        return errors + ["required_files debe ser una lista de rutas relativas."]
    safe_paths = []
    for name in declared:
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != name
            or not (root / name).resolve().is_relative_to(root.resolve())
        ):
            errors.append("El manifiesto contiene una ruta no portable o fuera del proyecto.")
        else:
            safe_paths.append(name)
    if len(safe_paths) != len(set(safe_paths)):
        errors.append("El manifiesto declara rutas obligatorias duplicadas.")
    for name in required_files_for_phase(phase):
        if name not in safe_paths:
            errors.append(f"La fase {phase} debe declarar el archivo mínimo: {name}")
    for name in sorted(set(safe_paths)):
        if not (root / name).is_file():
            errors.append(f"Falta archivo obligatorio: {name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", type=int, help="Exige coincidencia con la fase declarada")
    args = parser.parse_args()
    try:
        manifest = json.loads((ROOT / "config/phase-capabilities.json").read_text(encoding="utf-8"))
        errors = validate_manifest(ROOT, manifest, args.phase)
    except (OSError, ValueError):
        print("No se pudo leer un manifiesto de capacidades válido.")
        return 1
    for error in errors:
        print(error)
    if errors:
        return 1
    for item in manifest["pending_infrastructure"]:
        print(f"Sin verificar infraestructura: {item}")
    print(
        f"Fase {manifest['phase']}: mínimos declarativos válidos; CD deshabilitado. "
        "Esta comprobación no ejecuta ni acredita infraestructura real."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
