# Plan de Implementación v4.1: Agencia Digital de Marketing AI (Cloudflare Edge + Homelab Multi-Agente + GPU LAN + Metodología CI/CD & SRE)

* **Directorio Raíz del Proyecto:** `D:\Proyectos\Agencia Digital de Marketing AI`
* **Bóveda Maestra de Credenciales:** [`D:\Proyectos\Agencia Digital de Marketing AI\.env`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/.env)
* **Documento de Arquitectura:** [`docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md)

---

**Cambio autorizado de almacenamiento (2026-11-05):** [ADR-001](decisions/ADR-001-volumen-producciones.md) establece `/volume2/producciones`; el destino anterior `/volume1/producciones` queda sustituido.

**Cambios vigentes de alcance (2026-11-05):** [ADR-002](decisions/ADR-002-pagos-mercadopago.md) establece MercadoPago como único proveedor de pago inicial para los cuatro planes, con Flow y Webpay posteriores. [ADR-003](decisions/ADR-003-modelos-faciales-comerciales.md) sustituye la selección facial de evaluación por YuNet y solo AuraFace `glintr100.onnx`, conservando el protocolo Immich y los modelos GPT exactos.

## 0. Regla Operativa Obligatoria: Respaldo de Credenciales en `.env`

> [!IMPORTANT]
> **Directriz Inquebrantable para el Agente de Implementación:**
> 1. **Toda credencial generada y/o utilizada** en cualquier etapa del despliegue (contraseñas de bases de datos, API keys de Immich/Kitsu/n8n, IDs y tokens de Cloudflare D1/KV/R2/Queues, tokens de GitHub Actions Runner, secretos de MercadoPago o proveedores futuros cuando se incorporen, llaves SSH o tokens de OpenClaw) **DEBE quedar respaldada de inmediato en el archivo [`D:\Proyectos\Agencia Digital de Marketing AI\.env`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/.env)**.
> 2. **Orden de Ejecución para Secretos:** Primero se escribe o actualiza el valor real en `D:\Proyectos\Agencia Digital de Marketing AI\.env` (con su comentario `# Para qué / # Dónde / # Riesgo al rotar`), segundo se agrega la clave vacía (`CHANGE_ME`) en `.env.example`, y tercero se inyecta en el destino (Cloudflare Secret, GitHub Secret o contenedor remoto).
> 3. **Prohibición de Git sin autorización:** No ejecutar ningún comando `git` salvo instrucción explícita del usuario. Todos los documentos, comentarios y runbooks deben estar en **español**.

---

## 1. Estructura de Directorios del Proyecto (`D:\Proyectos\Agencia Digital de Marketing AI`)

```text
D:\Proyectos\Agencia Digital de Marketing AI\
├── .env                                       # Bóveda maestra de credenciales (NO commitear)
├── .env.example                               # Plantilla limpia versionable
├── .gitignore                                 # Bloqueo estricto de .env y llaves privadas
├── README.md                                  # Índice general del proyecto
├── ESTADO.md                                  # Bitácora viva de avance por fases (Fase 0 a Fase 7)
├── docs/
│   ├── ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md # Arquitectura Maestra v4.1
│   ├── PLAN-DE-IMPLEMENTACION.md              # Este plan de implementación detallado
│   ├── decisions/                             # Decisiones de arquitectura aceptadas
│   │   ├── ADR-001-volumen-producciones.md
│   │   ├── ADR-002-pagos-mercadopago.md
│   │   └── ADR-003-modelos-faciales-comerciales.md
│   ├── PROMPT-MAESTRO-AGENTE-IMPLEMENTACION.md# Prompt maestro de arranque para el agente
│   └── ci/                                    # Estándar documental CI/CD y SRE
│       ├── README.md
│       ├── diagrams/
│       │   ├── pipeline-overview.mmd
│       │   └── observability-sre-topology.mmd
│       ├── recipes/
│       │   ├── developer-push-flow.md
│       │   └── sre-rollback.md
│       └── runbooks/
│           ├── rb-01-gpu-bridge-unreachable.md
│           ├── rb-02-cloudflare-d1-migration-fail.md
│           ├── rb-03-agent-loop-or-openai-degraded.md
│           ├── rb-04-r2-nas-sync-backlog.md
│           └── rb-05-kitsu-immich-db-recovery.md
├── .github/
│   └── workflows/
│       └── ci-cd-media-platform.yml           # Pipeline híbrido Cloud + Self-Hosted Runner (.85)
├── cloudflare-saas/                           # Plano 1: Cloudflare Pages + Worker + D1 + KV + R2 + Queues
│   ├── wrangler.toml
│   ├── migrations/
│   │   └── 0001_schema.sql
│   ├── src/
│   │   └── index.ts
│   └── portal-app/
├── vcenter/                                   # Plano 3 (Infra VM): PowerCLI para vm-media-01 (.85)
│   ├── media-vm-config.ps1
│   └── 01-deploy-media-vm.ps1
├── storage/                                   # Plano 3 (Storage): NFSv4.1 y SMB3 en Synology (.8)
│   └── 02-setup-synology-storage.ps1
├── docker/                                    # Plano 3 (Apps): Kitsu, Immich, n8n, SupoClip, R2 Sync
│   ├── docker-compose.yml
│   └── r2-sync-consumer/
├── gpu-bridge-win11/                          # Plano 4 (GPU): Demonios CUDA/NVENC en Windows 11 (.6)
│   └── start-gpu-bridge.ps1
├── openclaw-agency-setup/                     # Plano 2 (Agentes IA): 5 Agentes + Skills + 4 MCPs en .7
│   ├── setup.sh
│   ├── patch_config.py
│   ├── mcp_servers/
│   └── skills/
├── ingress/                                   # WAF BunkerWeb y Nginx Proxy Manager en Synology (.8)
│   └── configure-npm-hosts.ps1
└── sre/                                       # Plano 5 (SRE): Backups 3-2-1, Smoke Tests y Rollback
    ├── sre-backup-321.sh
    ├── smoke-test-e2e.sh
    └── auto-rollback.sh
```

---

## 2. Fases de Implementación Ordenadas (Fase 0 a Fase 7)

### Fase 0: Cimientos SRE, Estructura `docs/ci/` y Pipeline CI/CD Base
1. Verificar que [`D:\Proyectos\Agencia Digital de Marketing AI\.env`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/.env) contenga todas las credenciales iniciales y generar los 5 Runbooks operativos (`rb-01` a `rb-05`) dentro de `docs/ci/runbooks/`.
2. Construir `.github/workflows/ci-cd-media-platform.yml` con los **7 Quality Gates bloqueantes** (`gitleaks`, `lint`, `tsc`, `vitest`, `docker compose config`, `d1 dry-run`, `docs/ci/ check`) y los scripts `sre/smoke-test-e2e.sh` y `sre/auto-rollback.sh`.
3. **Verificación Fase 0:** Validar sintaxis de workflows y comprobar que `gitleaks` y los validadores locales pasen sin errores. Actualizar `ESTADO.md`.

---

### Fase 1: Aprovisionamiento de `vm-media-01` (`192.168.0.85`) en vCenter y Self-Hosted Runner
1. Crear `vcenter/media-vm-config.ps1` y `vcenter/01-deploy-media-vm.ps1` leyendo las credenciales de vSphere desde `D:\Proyectos\Agencia Digital de Marketing AI\.env`.
2. Clonar `vm-media-01` desde `TPL-ubuntu2404-k8s` en `datastore1` (`6 vCPU`, `16 GB RAM`, `80 GB Thin Disk`, IP `192.168.0.85/24`, PortGroup `LAN`).
3. Instalar en `vm-media-01`: Docker Engine, Docker Compose v2, `nfs-common`, `rclone`, `restic`, `jq` y configurar el servicio `systemd` para el **GitHub Actions Self-Hosted Runner** (usuario sin privilegios root).
4. Respaldar cualquier llave SSH o token del runner en `D:\Proyectos\Agencia Digital de Marketing AI\.env`.
5. Tomar snapshot inicial en vCenter: `01-vm-limpia`.
6. **Verificación Fase 1:** `ping 192.168.0.85`, `ssh jandrade@192.168.0.85 "docker compose version"` y confirmar snapshot en vCenter. Actualizar `ESTADO.md`.

---

### Fase 2: Almacenamiento Compartido Triple en Synology DSM (`192.168.0.8`)
**Destino autorizado:** `/volume2/producciones`, según [ADR-001](decisions/ADR-001-volumen-producciones.md). El recurso, los montajes y la prueba compartida cuentan ya con [evidencia real](ci/evidence/2026-11-05-fase-2.md); el responsable confirmó el cierre el 2026-11-05 tras 220 pruebas Python y controles finales aprobados. La auditoría del 2026-11-05 observó 6094,56 GiB de capacidad y aproximadamente 2753,63 GiB libres (2,69 TiB) en `/volume2`; no acredita los 41 TB previstos anteriormente. Preservar el datastore ESXi existente `/volume2/NFS`, sus exportaciones y los demás consumidores. La revisión de dependencias encontró el datastore montado y sin VMs registradas asociadas; no demuestra que esté vacío.

1. Crear y ejecutar `storage/02-setup-synology-storage.ps1` conectándose a `192.168.0.8:2222`.
2. Crear la jerarquía `/volume2/producciones/{_biblioteca_global,_backups,proyectos/sansevieras/{01_raw..06_renders}}`.
3. Configurar exportación **NFSv4.1** de `/volume2/producciones` hacia `192.168.0.85` (`vm-media-01`) y `192.168.0.7` (`Ubuntu-Openclaw`), montando ambos de forma persistente en `/etc/fstab` sobre `/mnt/producciones`. Conservar las versiones NFS 2/3 y las exportaciones existentes; coordinar el impacto de habilitar 4.1 sobre los clientes activos.
4. Mapear `\\192.168.0.8\producciones` por **SMB3** como unidad `P:` en `Windows 11` (`192.168.0.6`).
5. **Verificación Fase 2:** Escribir un archivo de prueba desde `.85` en `/mnt/producciones/.probe`, leerlo desde `.7` (`/mnt/producciones/.probe`) y desde `.6` (`P:\.probe`). Actualizar `ESTADO.md`.

---

### Fase 3: Puente GPU Stateless y Exportador de Métricas en `Windows 11` (`192.168.0.6`)
1. Crear `gpu-bridge-win11/start-gpu-bridge.ps1` en `D:\Proyectos\Agencia Digital de Marketing AI\gpu-bridge-win11\` e instalarlo en `C:\GPU-Bridge\`.
2. Levantar y verificar los 4 endpoints sobre la **NVIDIA GeForce RTX 3060 Ti (8 GB VRAM)** autenticados con `GPU_BRIDGE_BEARER_TOKEN` (respaldado en `.env`):
   * **`:3003` — `immich-machine-learning` (CUDA):** Embeddings CLIP y procesamiento facial con el protocolo Immich original. Registrar el ID facial propio `agency_yunet_auraface_v1` de [ADR-003](decisions/ADR-003-modelos-faciales-comerciales.md): YuNet 2023mar y únicamente AuraFace-v1 `glintr100.onnx`, con los dos commits y seis artefactos fijados en `gpu-bridge-win11/face-artifacts.json`. Conservar licencias y tarjetas, verificar hashes completos y acreditar detección/reconocimiento CUDA nuevos; no reutilizar embeddings Buffalo_L ni afirmar autorización comercial de esos pesos históricos.
   * **`:8000` — `speaches / faster-whisper-api` (CUDA):** Endpoint `/v1/audio/transcriptions` con marcas de tiempo por palabra.
   * **`:8090` — `remotion-nvenc-worker` (CUDA/NVENC):** Endpoints `/stills` (capturas QA en 2s), `/inspect` (tira de contactos graduada `Y: 0..1920px` + `ebur128`) y `/render` (`h264_nvenc`, `--color-space=bt709`, mezcla `-14 LUFS`, marca de agua condicional para plan `Free` y semáforo `MAX_CONCURRENT_NVENC=2`).
   * **`:9835` — `gpu-metrics-exporter`:** Métricas Prometheus de VRAM, temperatura y sesiones NVENC + watchdog de auto-healing.
3. Configurar reglas de Windows Firewall permitiendo acceso a `:3003`, `:8000`, `:8090` y `:9835` únicamente desde `192.168.0.85`, `192.168.0.7` y `192.168.0.8`.
4. **Verificación Fase 3:** Invocar `/health` en los 4 puertos desde `vm-media-01` (`192.168.0.85`) y desde `Ubuntu-Openclaw` (`192.168.0.7`). Actualizar `ESTADO.md`.

---

### Fase 4: Stack Docker Open-Source en `vm-media-01` (`192.168.0.85`) y Backups 3-2-1
1. Generar `docker/docker-compose.yml` inyectando exclusivamente las contraseñas ya respaldadas en `D:\Proyectos\Agencia Digital de Marketing AI\.env`.
2. Desplegar en `/opt/media-platform/` sobre `192.168.0.85`:
   * **`postgres-immich` (VectorChord) + `postgres-kitsu` + `valkey`** en el disco NVMe local (`/var/lib/media-platform/`).
   * **`kitsu-zou` (`:8081` / `:5000`):** Inicializar base `zoudb`, crear usuario admin y respaldar token en `.env`.
   * **`immich-server` (`:2283`):** Conectar librería externa `/mnt/producciones`, apuntar ML a `http://192.168.0.6:3003`, configurar explícitamente el ID facial de [ADR-003](decisions/ADR-003-modelos-faciales-comerciales.md) y generar nuevos embeddings y umbrales verificados antes de datos de clientes. Generar `IMMICH_API_KEY_MCP` y **respaldarla inmediatamente en `D:\Proyectos\Agencia Digital de Marketing AI\.env`**.
   * **`n8n` (`:5678`), `supoclip` (`:3080`), `moneyprinter` (`:8501`) y `r2-sync-consumer` (`:3010`)**.
   * **Colectores SRE:** `otel-collector`, `node-exporter` y `cadvisor` exportando hacia el stack de monitoreo en `192.168.0.8`.
3. Instalar `sre/sre-backup-321.sh` en cron diario (`03:30 AM`) para respaldar PostgreSQL, D1 y OpenClaw en `/volume2/producciones/_backups/` y en Cloudflare R2 (`studio-backups-bucket`).
4. **Verificación Fase 4:** Todos los contenedores en estado `healthy`, login funcional en Kitsu e Immich, y ejecución exitosa de una corrida real de `sre-backup-321.sh`. Actualizar `ESTADO.md`.

---

### Fase 5: Equipo de 5 Agentes (`agency-*`), Skills con URLs Exactas y 4 MCPs en `192.168.0.7`
1. Desarrollar en `openclaw-agency-setup/mcp_servers/` los 4 servidores MCP:
   * `mcp-media-inspector` (Faster-Whisper `:8000` + `ffprobe/ebur128` + tira de contactos graduada `Y: 0..1920px`).
   * `mcp-immich-broll` (Smart Search CLIP contra `192.168.0.85:2283`).
   * `mcp-remotion-gpu` (Stills QA `/stills` y Render Master `/render` contra `192.168.0.6:8090`).
   * `mcp-kitsu-review` (Creación de entregables, subida de versiones `v1..vN` y lectura de feedback por fotograma en `192.168.0.85:8081`).
2. Desarrollar las 3 skills de contrato interno en `openclaw-agency-setup/skills/`: `protocolo-agencia-audiovisual`, `operacion-worker-remotion-gpu` y `rubrica-qa-audiovisual`.
3. Crear `openclaw-agency-setup/setup.sh` y `patch_config.py` para que en `192.168.0.7`:
   * Instale desde sus **URLs exactas declaradas** las suites de skills:
     * `npx skills add https://github.com/jakeschincariol/instagram-agent-skill -y -g` (`ig-reel`, `ig-viral`, `ig-caption`, `ig-story`, `ig-dm`, `ig-human`, `ig-carousel`, `ig-plan`, `ig-audit`)
     * `npx skills add https://github.com/coreyhaines31/marketingskills -y -g` (`product-marketing`, `onboarding`, `marketing-psychology`, `cro`, `ab-testing`, `ad-creative`, `copywriting`, `social`, `video`)
     * `npx skills add kostja94/marketing-skills@meta-ads -y -g`
     * `npx skills add https://github.com/remotion-dev/skills -y -g`
     * `npx skills add agentspace-so/runcomfy-agent-skills@ai-image-generation -y -g`
     * `npx skills add halt-catch-fire/skills@ai-video-generation -y -g`
   * Sincronice las skills locales `edicion-de-video` y `remotion-dev`.
   * Registre de forma idempotente en `~/.openclaw/openclaw.json` los 5 agentes con sus modelos exactos:
     * `agency-account` (**Sofía 📋** · `openai/gpt-6.1-terra`)
     * `agency-strategist` (**Mateo 🧠** · `openai/gpt-6.1-sol`)
     * `agency-director` (**Valeria 🎬** · `openai/gpt-6.1-sol`)
     * `agency-editor` (**Lucas 🎞️** · `openai/gpt-6.1-terra`)
     * `agency-critic` (**Elena 🔍** · `openai/gpt-6.1-sol`)
4. **Verificación Fase 5:** Probar invocación de cada uno de los 5 agentes y los 4 MCPs en `192.168.0.7:18789`. Actualizar `ESTADO.md`.

---

### Fase 6: Capa SaaS en Cloudflare Edge (`studio.e-scale.cl`), Google OAuth, 4 Planes y WAF
1. Crear en Cloudflare (usando `CF_API_TOKEN` de `.env`):
   * Base de datos D1 `studio-saas-db`, namespace KV `STUDIO_SESSIONS_KV`, buckets R2 `studio-media-bucket` y `studio-backups-bucket`, y cola `studio-jobs-queue` (+ `studio-jobs-dlq`).
   * **Respaldar inmediatamente en `D:\Proyectos\Agencia Digital de Marketing AI\.env`** los IDs reales (`CF_D1_DATABASE_ID`, `CF_KV_NAMESPACE_ID`).
2. Aplicar la migración `cloudflare-saas/migrations/0001_schema.sql` en D1 creando las tablas `users`, `subscriptions`, `plan_quotas`, `usage_ledger` y `orders` con los 4 planes (`free`, `pro`, `ultra`, `ilimitado`).
3. Desplegar `studio-api-worker` y `studio-portal-web` (Cloudflare Pages) con login **Google OAuth 2.0**, **MercadoPago como único proveedor de pago inicial** para el catálogo `Free`, `Pro`, `Ultra`, `Ilimitado`, *Edge Quota Gatekeeper*, subida directa Multipart a R2 (`Uppy`), chat con **Sofía 📋** y Sala de Entrega de Kits. Elegir la modalidad concreta de MercadoPago conforme a la cuenta y producto comprobados; respaldar sus credenciales de prueba antes de usarlas, validar estados y notificaciones autenticadas e idempotentes. Por [ADR-002](decisions/ADR-002-pagos-mercadopago.md), Flow y Webpay son posteriores y Stripe queda fuera de esta entrega; conservar sus reservas de bóveda y plantilla sin exigirlas para aceptar el alcance inicial.
4. Configurar **BunkerWeb WAF (`192.168.0.8:7000`)** y **Nginx Proxy Manager (`192.168.0.8:81`)** para exponer de forma segura `api-studio.e-scale.cl` (`-> .85:3010`), `kitsu.e-scale.cl` (`-> .85:8081`) e `immich.e-scale.cl` (`-> .85:2283`).
5. **Verificación Fase 6:** Login real con Google en `https://studio.e-scale.cl`, verificación de cuota por plan en D1/KV, prueba de subida directa Multipart a R2 y flujo MercadoPago en entorno de prueba con estados y notificaciones verificados. Obtener un token no acredita cuenta habilitada, API ni webhooks; los pagos reales requieren una aceptación posterior con credenciales productivas. Flow, Webpay y Stripe no bloquean la verificación inicial. Actualizar `ESTADO.md` solo con resultados realmente ejecutados.

#### Espacios por marca del plan Ilimitado

- El titular de una cobertura Ilimitado activa puede crear, consultar y editar el nombre, la audiencia y el tono de cada marca. Cada perfil pertenece a una cuenta y un entorno de facturación. Los titulares de otros planes reciben un rechazo al gestionar perfiles o asociarlos a nuevas producciones.
- Al reservar una producción con marca, D1 guarda una copia del perfil en la misma transacción que reserva el cupo. Esa copia alimenta los campos de audiencia y tono del brief que confirma el titular. La edición posterior de la marca se aplica a las próximas producciones.
- La selección de marca es opcional. La idempotencia incluye la marca seleccionada. El historial permite identificar la marca asociada a cada pedido. El código del portal conserva las rutas de Google, pagos y carga de vídeo.
- Se verifica el flujo con dos perfiles de una cuenta, otra cuenta, una cobertura distinta y dos entornos. La verificación incluye reintentos, reserva en D1, copia del perfil tras editarlo y confirmación del brief desde el portal. Se ejecutan `npm exec -- vitest run tests-ts/studio-ingest.test.ts`, `npm run typecheck` y `npm --prefix cloudflare-saas/studio-portal-web run build`.

---

### Fase 7: Prueba E2E de Producción, Simulación de Auto-Rollback SRE y Cierre
1. Ejecutar `sre/smoke-test-e2e.sh` validando todos los planos (`Cloudflare Edge`, `.85`, `.7`, `.6` y `.8`).
2. Realizar una **producción E2E completa**: subir media cruda por `studio.e-scale.cl`, ejecutar el ciclo completo del equipo (`Sofía 📋 → Mateo 🧠 → Valeria 🎬 → Lucas 🎞️ → Elena 🔍`) con render NVENC a `-14 LUFS`, auditoría multimodal (`Score >= 85`) y publicación del **Kit Completo de Campaña** en el portal y en Kitsu.
3. Simular un fallo controlado en sonda post-deploy para comprobar que `sre/auto-rollback.sh` revierte automáticamente en `< 90 segundos`.
4. Verificar que **todas** las credenciales utilizadas en los 7 planos estén documentadas y respaldadas en `D:\Proyectos\Agencia Digital de Marketing AI\.env`.

#### Ajustes humanos tras una revisión manual

- El titular puede enviar instrucciones para una producción detenida en revisión. Se aplica el límite del plan registrado al reservar la producción. Pro incluye un ajuste, Ultra incluye tres e Ilimitado admite ajustes sin tope. El ajuste conserva el cupo ya consumido por la producción.
- Cada ajuste se vincula a la revisión y a la huella de escaleta que causó la pausa. D1 admite un ajuste pendiente por orden y conserva el historial. Repetir la misma solicitud devuelve su estado, y reutilizar su clave con otras instrucciones devuelve un conflicto.
- El proceso de dirección lee el brief confirmado y el ajuste desde el despacho autenticado. Genera una escaleta con el medio acreditado y conserva la anterior. El despacho verifica la orden, el original, el brief, la revisión y las huellas antes de registrar el resultado. Una escaleta apta permite continuar hacia render y QA. Si el ajuste aún requiere intervención, la orden conserva el bloqueo de entrega y refleja el intento consumido.
- La aceptación incluye acceso por titular, límites de los cuatro planes, conflictos y reintentos, conservación del brief, bloqueo de entrega mientras hay revisión, dirección corregida y publicación posterior de un kit aprobado.
