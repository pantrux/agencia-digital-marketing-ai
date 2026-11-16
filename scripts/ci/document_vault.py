"""Completa comentarios de la bóveda y regenera una plantilla sin valores reales."""

import os

from check_env import KEY, ROOT, parse_env

GROUPS = [
    (
        ("VSPHERE_", "ESXI_"),
        "Aprovisionamiento y snapshots de vSphere",
        "vcenter/ y vCenter/ESXi (.2/.3)",
    ),
    (("ROUTER_",), "Acceso y verificación del router perimetral", "router (.1)"),
    (
        ("GREPTILE_API_KEY",),
        "Autenticación de la revisión de cambios de Studio",
        "Cuenta Greptile del repositorio de Studio",
    ),
    (
        ("SYNOLOGY_",),
        "Acceso al almacenamiento compartido",
        "Synology DSM (.8), clientes NFS/SMB",
    ),
    (
        ("BUNKERWEB_",),
        "Configuración y autenticación del cortafuegos web",
        "BunkerWeb en Synology (.8)",
    ),
    (
        ("NPM_",),
        "Administración del proxy de entrada",
        "Nginx Proxy Manager en Synology (.8)",
    ),
    (
        ("CF_", "STUDIO_PUBLIC_", "STUDIO_API_"),
        "Recursos y acceso a Cloudflare Edge",
        "Cloudflare y ejecutor de despliegues (.85)",
    ),
    (
        ("STUDIO_",),
        "Autenticación y firma de tráfico de la plataforma",
        "Worker de Cloudflare y consumidor (.85)",
    ),
    (
        ("R2_",),
        "Acceso y ubicación de objetos y respaldos",
        "Cloudflare R2 y sincronizador (.85)",
    ),
    (
        ("RESTIC_",),
        "Cifrado de respaldos 3-2-1",
        "restic en vm-media-01 (.85) y custodia local",
    ),
    (
        ("GOOGLE_CLIENT_", "GOOGLE_REDIRECT_", "GOOGLE_TEST_ACCOUNT_"),
        "Autenticación Google OAuth del portal",
        "Worker studio-api-worker y consola de Google",
    ),
    (
        ("GOOGLE_KEYCLOAK_",),
        "Respaldo del cliente de OAuth existente de Keycloak",
        "Consola de Google y proveedor de identidad de Keycloak",
    ),
    (
        ("STRIPE_",),
        "Suscripciones y verificación de pagos Stripe",
        "Worker y Stripe Billing",
    ),
    (("FLOW_",), "Cobro y validación de notificaciones Flow.cl", "Worker y Flow.cl"),
    (
        ("MP_",),
        "Cobro y validación de notificaciones MercadoPago",
        "Worker y MercadoPago",
    ),
    (
        ("OPENCLAW_", "OPENWEBUI_", "AGENT_"),
        "Equipo de cinco agentes y sus conexiones",
        "Ubuntu-Openclaw (.7), n8n y consumidor (.85)",
    ),
    (
        ("AGENCY_MEDIA_",),
        "Permisos de proyecto para los adaptadores audiovisuales",
        "perfil privado de MCP en Ubuntu-OpenClaw (.7)",
    ),
    (
        ("GOOGLE_AI_", "ELEVENLABS_"),
        "Proveedor auxiliar de generación audiovisual",
        "agentes (.7) y servicios de medios (.85)",
    ),
    (
        ("MEDIA_VM_", "MEDIA_TEMPLATE_"),
        "Identidad y red de la VM de medios",
        "vCenter (.2) y vm-media-01 (.85)",
    ),
    (
        ("MEDIA_",),
        "Identidad, imágenes y rutas propias de la plataforma de medios",
        "controlador Windows (.6), construcción y contenedores de vm-media-01 (.85)",
    ),
    (
        ("BACKUP_", "RCLONE_"),
        "Herramientas verificadas de copia y transferencia",
        "CLI privados en Windows (.6) y vm-media-01 (.85)",
    ),
    (
        ("KITSU_", "ZOU_"),
        "Gestión audiovisual Kitsu/Zou",
        "contenedores Kitsu/Zou y PostgreSQL (.85)",
    ),
    (
        ("IMMICH_",),
        "Catálogo de medios e integración MCP Immich",
        "Immich y PostgreSQL (.85), MCP (.7)",
    ),
    (("N8N_", "AGENCY_N8N_"), "Orquestación y cifrado de flujos n8n", "n8n (.85)"),
    (
        ("REDIS_",),
        "Autenticación de Valkey/Redis",
        "Valkey y clientes de plataforma (.85)",
    ),
    (
        ("GPU_",),
        "Acceso al puente GPU y almacenamiento SMB",
        "Windows GPU (.6), consumidores (.85/.7/.8)",
    ),
    (
        ("GITHUB_", "GITEA_"),
        "Integración del ejecutor CI/CD",
        "servicio de ejecución sin root en vm-media-01 (.85)",
    ),
    (
        ("MON_", "SIGNOZ_", "SAL_"),
        "Observabilidad y correlación de trazas",
        "colectores (.85/.7/.6) y monitoreo (.8)",
    ),
    (
        ("GRAFANA_",),
        "Configuración y verificación de fuentes de datos",
        "API de la organización de Grafana en Synology DSM (.8)",
    ),
    (
        ("PROMETHEUS_NAS_POSTBOOT_",),
        "Huellas de la recuperación de las reglas de Prometheus",
        "filtro de observabilidad de Synology DSM (.8)",
    ),
    (
        ("SOFIA_",),
        "Identidades y autenticación del chat de Sofía",
        "perfil dedicado de OpenClaw y su intermediario de red (.7)",
    ),
    (
        ("SRE_STUDIO_DRILL_",),
        "Identidad y huellas del ensayo de reversión de Studio",
        "Cloudflare, vm-media-01 y Ubuntu-OpenClaw",
    ),
    (
        ("D1_BACKUP_MONITOR_",),
        "Huellas del monitor de la copia diaria de D1 y de su reversión",
        "Recolector en vm-media-01 y configuración privada de observabilidad en Synology (.8)",
    ),
    (
        ("SRE_ROLLBACK_ALERT_",),
        "Identidad, huellas y preimágenes de la alerta de reversión de Studio",
        "Configuración privada de Prometheus y Alertmanager en Synology (.8) y ejecutor SRE (.85)",
    ),
    (
        ("META_", "WHATSAPP_"),
        "Configuración de notificaciones operativas",
        "servicio de alertas y Meta Cloud API",
    ),
    (
        ("SMTP_",),
        "Entrega de correo de operaciones",
        "servicio de alertas y proveedor SMTP",
    ),
]


def documentation(key: str) -> list[str]:
    group = next((item for item in GROUPS if key.startswith(item[0])), None)
    if group is None:
        raise ValueError(f"Documentar ubicación de la variable {key} antes de usarla.")
    _, purpose, location = group
    if key == "MEDIA_TEMPLATE_CLOUDINIT_VMWARE_VERIFIED":
        purpose = "Habilitar LAN únicamente después de verificar cloud-init en el clon aislado"
        location = "aprovisionador vCenter y prueba aislada de vm-media-01"
        risk = "Solo establecer true con evidencia real; false bloquea la personalización y el arranque LAN."
    elif key.startswith("GITHUB_RUNNER_") and key.endswith("_B64"):
        purpose = "Respaldo recuperable de la identidad del ejecutor GitHub Actions"
        risk = "Requiere registro coordinado del ejecutor; respaldar la identidad nueva antes de iniciar su servicio."
    elif key == "AGENCY_N8N_TLS_CA_KEY_B64":
        purpose = "Firmar certificados del acceso HTTPS interno de n8n"
        location = "bóveda local, fuera de la VM y los clientes"
        risk = "Distribuir la CA nueva y reemitir el certificado del servidor."
    elif "PRIVATE_KEY" in key:
        risk = "Actualizar los accesos SSH autorizados y todos los consumidores antes de retirar la clave anterior."
    elif key == "RESTIC_PASSWORD":
        risk = "Conservar clave anterior: cambiarla sin migración impide descifrar respaldos históricos."
    elif key == "N8N_ENCRYPTION_KEY":
        risk = "Conservar clave anterior y migrar credenciales cifradas antes de reiniciar n8n."
    elif key.endswith(("PASSWORD", "SECRET", "SECRET_KEY", "ENCRYPTION_KEYS")):
        risk = "Coordinar cambio en origen y todos los consumidores; reiniciar sesiones o conexiones afectadas."
    elif "TOKEN" in key or "API_KEY" in key or key.endswith("KEY_ID"):
        risk = "Actualizar consumidores antes de revocar la credencial anterior; comprobar permisos y vigencia."
    else:
        risk = "Actualizar referencias y configuración declarativa; verificar rutas, red y servicios consumidores."
    return [
        f"# Para qué: {purpose}; variable {key}.",
        f"# Dónde: {location}.",
        f"# Riesgo al rotar: {risk}",
    ]


def main() -> int:
    path = ROOT / ".env"
    original = path.read_text(encoding="utf-8-sig")
    before, errors = parse_env(original)
    if errors:
        raise ValueError("Corregir sintaxis de la bóveda antes de documentarla.")
    result = []
    for line in original.splitlines():
        match = KEY.fullmatch(line.strip())
        if match:
            while result and result[-1].startswith(
                ("# Para qué:", "# Dónde:", "# Riesgo al rotar:")
            ):
                result.pop()
            result.extend(documentation(match[1]))
        result.append(line)
    updated = "\n".join(result) + "\n"
    after, errors = parse_env(updated)
    if errors or before != after:
        raise ValueError("La documentación no puede alterar valores.")
    temporary = path.with_name(".env.documentacion.tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    template = [
        "# Plantilla canónica. Obtener valores únicamente desde la bóveda .env.",
        "",
    ]
    for key in before:
        template.extend(documentation(key))
        template.extend([f"{key}=CHANGE_ME", ""])
    (ROOT / ".env.example").write_text("\n".join(template), encoding="utf-8", newline="\n")
    print(f"Documentadas {len(before)} variables; valores conservados; plantilla sincronizada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
