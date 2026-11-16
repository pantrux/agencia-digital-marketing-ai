# Cómo verificar salud y revertir una entrega

Usar cuando una entrega coordinada falla salud. El manifiesto v2 habilita la reversión acotada a la versión comprobada del Worker, los servicios Compose indicados y las etiquetas declaradas de agentes de Studio. La versión 1 conserva el bloqueo histórico de la sustitución de toda la configuración OpenClaw. El presupuesto de 85 s incluye sondas, restauración y verificación posterior.

## Ensayo local de Fase 0

Desde la raíz del proyecto, sin tocar infraestructura:

```bash
python -m unittest discover -s tests -p 'test_sre.py' -v
python sre/sre_control.py smoke --simulate tests/fixtures/sre/simulation-ok.json
```

**Resultado esperado:** pruebas aprobadas y salida cero en simulación sana. Las pruebas incluyen fallos y rechazos de manifiestos; no acreditan disponibilidad LAN ni de Cloudflare.

## Prerrequisitos para una reversión real

- Ejecutar desde `.85`, con Python 3, Bash, Docker Compose, SSH LAN y acceso autenticado a la API de Cloudflare. El entorno protegido contiene `CF_ACCOUNT_ID`, `CF_PLATFORM_API_TOKEN`, `SRE_OPENCLAW_SSH_IDENTITY` y `SRE_OPENCLAW_KNOWN_HOSTS`. Configurar `SRE_PROMETHEUS_MTLS_DIR` si el usuario del proceso difiere del titular de los certificados.
- Como operador en `vm-media-01`, usar `cd "$HOME/.local/share/agency-sre-v1"` antes de ejecutar los comandos reales de esta receta. Los cuatro archivos de SRE se instalaron con huellas cotejadas y permisos privados.
- Sondas protegidas desde `vm-media-01` con `GPU_BRIDGE_BEARER_TOKEN` en el entorno y el certificado cliente de Prometheus instalado en `~/.agency-prometheus-bridge-client-v1/valid`. Este directorio debe pertenecer al usuario que ejecuta la sonda. Los accesos Cloudflare y SSH se preparan antes del incidente.
- Manifiesto v2 con `release_sha`, Worker y versiones actual y estable, archivo Compose bajo `/opt/media-platform` con hash, proyecto y servicios exactos, y artefacto OpenClaw con hash y usuario SSH. El artefacto contiene `expected` y `previous`, dos mapas de identificador de agente a etiqueta. El hash del manifiesto llega por un canal de confianza distinto del propio archivo.
- Compatibilidad de código y esquema comprobada; restaurar datos requiere [rb-02](../runbooks/rb-02-cloudflare-d1-migration-fail.md) o [rb-05](../runbooks/rb-05-kitsu-immich-db-recovery.md).

## Procedimiento

1. Conservar ID de entrega y resultado de salud; impedir entregas concurrentes. Seleccionar el manifiesto estable y su hash de confianza:

   ```bash
   : "${MANIFIESTO_PREVIO:?Falta ruta del manifiesto estable}"
   : "${HASH_MANIFIESTO_PREVIO:?Falta SHA256 de confianza del manifiesto}"
   bash sre/smoke-test-e2e.sh --rollback-manifest "$MANIFIESTO_PREVIO" --rollback-manifest-sha256 "$HASH_MANIFIESTO_PREVIO"
   ```

   Si falla salud, el controlador valida manifiesto y artefactos antes de revertir. El modo de solo sondeo es `bash sre/smoke-test-e2e.sh`.
2. Para ejecutar directamente una reversión ya diagnosticada:

   ```bash
   bash sre/auto-rollback.sh --manifest "$MANIFIESTO_PREVIO" --manifest-sha256 "$HASH_MANIFIESTO_PREVIO"
   ```

3. Exigir verificación posterior y conservar duración y salida. El modo `smoke` conserva salida 1 cuando falló la entrega aunque haya logrado revertirla; revisar el resultado de recuperación y las diez sondas posteriores. El modo directo `auto-rollback` solo devuelve cero si restauró sus tres componentes y las sondas posteriores pasan. Configuración inválida devuelve 2. Un artefacto ausente, un hash distinto o un timeout mantiene el incidente abierto; no declarar recuperación por haber iniciado una acción.

## Alcance y escalado

El smoke test comprueba D1, R2 y KV desde el portal, disponibilidad del consumidor y de su almacén de estado, base y almacén clave–valor de Zou, el ping de Immich por su dominio, disponibilidad del Gateway OpenClaw, los cuatro servicios GPU y Prometheus del NAS mediante TLS mutuo. Después de reiniciar el Gateway, comprobar también el broker de Sofía y su ingreso según el [procedimiento rb-03](../runbooks/rb-03-agent-loop-or-openai-degraded.md#recuperación-del-broker-de-sofía). Las pruebas de aplicaciones cubren el catálogo de agentes y MCP, la autenticación y los componentes auxiliares de Kitsu.

La reversión v2 coteja el despliegue activo del Worker y verifica el anterior por lectura. Para Compose usa imágenes locales inmutables y restaura únicamente los servicios enumerados. Para OpenClaw utiliza `config.patch` con revisión y verifica los nombres y el resto de entradas tras la recarga en caliente. [Revisión y recarga de OpenClaw](https://docs.openclaw.ai/gateway/configuration/config-rpc) · [Despliegues de Workers](https://developers.cloudflare.com/api/resources/workers/subresources/scripts/subresources/deployments/methods/create/)

Para activar la escritura de sesiones Google en un lote, despliega y verifica primero una versión que lea ambos formatos con `STUDIO_SESSION_BATCH_ENABLED=false`. Conserva esa versión como destino de reversión. Después de emitir cookies del formato nuevo, vuelve a ese lector si falla el retorno de Google. Comprueba `GET /api/auth/me` con las dos versiones de sesión y un cierre de sesión con revocación inmediata.

El ensayo de Fase 7 introdujo un fallo después de la sonda real con `--failure-drill edge`. Restauró la versión activa del Worker, un servicio Compose aislado y la etiqueta de Sofía en 40,316 s. La reverificación comprobó las diez sondas. Conserva en `ESTADO.md` el alcance de cada ensayo. La recuperación de datos, Pages, Windows y las demás clases de cambios de OpenClaw siguen sus procedimientos específicos.

El modo `smoke` con manifiesto v2 publica el resultado del intento de reversión en Prometheus mediante OTLP autenticado con el mismo certificado de la sonda del NAS. Alertmanager envía por SMTP un resumen con el identificador de entrega, las sondas fallidas, los componentes pendientes y el tiempo de recuperación. El asunto distingue un ensayo inyectado de una operación. La alerta se evalúa cada 15 s y vence a los tres minutos. El correo cubre la activación.

El campo `alerta_sre` del recibo consigna el acuse de Prometheus. El presupuesto del intento de recuperación se mide antes de publicar ese recibo. Si aparece `error_de_entrega` o el resultado sigue pendiente, consultar el [runbook del componente](../README.md) y escalar al responsable de plataforma con la salida JSON. La simulación local y la repetición directa con `auto-rollback.sh` prescinden de la publicación SMTP. La ejecución de CD en GitHub sigue pendiente de publicación autorizada.
