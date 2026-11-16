# Procedimiento rb-03: bucle de agentes o proveedor degradado

**Responsable:** equipo de plataforma / SRE · **Severidad:** alta por órdenes detenidas o consumo repetido.  
**Última revisión:** 2026-11-15 · **Frecuencia:** ante alerta o reinicio del Gateway · **Última recuperación en producción:** 2026-11-15.

## Propósito y síntomas

Detener iteraciones repetidas, llamadas superiores a 120 segundos o degradación del proveedor de modelos sin alterar los demás equipos de OpenClaw. El alcance de reconciliación y recuperación se limita a los cinco IDs `agency-*`.

## Prerrequisitos

- Acceso SSH LAN a `jandrade@192.168.0.7` y registros de la orden afectada.
- Configuración previa con hash, respaldo de memorias y token del gateway en el entorno protegido.
- Límites por plan del documento maestro. Los modelos exactos se conservan; no sustituirlos por otro modelo para ocultar una falla.

## Diagnóstico rápido

1. En `.7`, comprobar procesos y listener sin volcar la configuración completa:

   ```bash
   systemctl --user is-active openclaw-gateway.service
   systemctl --user show -p ActiveState -p Result -p NRestarts -p ExecStartPre agency-sofia-owner-broker.service
   ss -ltn '( sport = :18789 or sport = :3107 )'
   ss -xl | grep -E '/(sofia-owner|mateo)\.sock'
   ```

   **Resultado esperado**

   El Gateway está activo en `18789`, la preparación firmada de `ExecStartPre` terminó con código cero, el broker escucha en `127.0.0.1:3107` y ambos sockets Unix aceptan conexiones. Si sube `NRestarts` o el broker continúa inactivo, seguir la recuperación de Sofía.

2. Revisar únicamente IDs y modelos declarados, sin exponer tokens:

   ```bash
   python3 - <<'PY'
   import json
   from pathlib import Path
   datos = json.loads((Path.home() / ".openclaw/openclaw.json").read_text())
   esperados = {"agency-account", "agency-strategist", "agency-director", "agency-editor", "agency-critic"}
   agentes = datos["agents"]
   entradas = agentes.get("list", agentes.get("entries", []))
   if isinstance(entradas, dict):
       entradas = [{**valor, "id": clave} for clave, valor in entradas.items()]
   encontrados = [agente for agente in entradas if agente.get("id") in esperados]
   if {agente["id"] for agente in encontrados} != esperados or len(encontrados) != len(esperados):
       raise SystemExit("El inventario de los cinco agentes difiere")
   for agente in encontrados:
       modelo = agente["model"]
       print(agente["id"], modelo.get("primary") if isinstance(modelo, dict) else modelo)
   PY
   ```

   Si el inventario difiere, preservar el archivo y usar el reconciliador de Fase 5 sobre las entradas de la agencia.

## Recuperación del broker de Sofía

La unidad del broker ejecuta la preparación firmada en `ExecStartPre` con un identificador de solicitud nuevo. Si falla, systemd reintenta el arranque tras cinco segundos y aumenta la espera hasta un minuto. La preparación exige un Gateway activo. [Política de reinicio de systemd](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html)

Si el Gateway está activo, el socket `sofia-owner.sock` escucha, el broker quedó inactivo y no hay un reintento en curso, confirmar que ningún proceso ocupe `3107`. Desde la sesión del operador en `.7`, iniciar la unidad propia del broker:

```bash
systemctl --user start agency-sofia-owner-broker.service
systemctl --user is-active agency-sofia-owner-broker.service
systemctl --user show -p Result -p ExecStartPre -p NRestarts agency-sofia-owner-broker.service
```

Se espera `active`, `Result=success` y código cero en `ExecStartPre`. Si falla, conservar el estado del broker y los registros sin credenciales. Revisar el socket y el perfil de OAuth nativo. La guarda del plugin recupera un socket propio obsoleto cuando confirma `ECONNREFUSED` y coteja el inodo. Mantener el estado de SQLite para conservar los turnos y su idempotencia.

Confirmar desde una estación de administración que la ruta pública devuelve HTTP `401` y `{"error":"SOFIA_MTLS_REJECTED"}` con un objeto JSON vacío y una firma ficticia bien formada. Ese formato permite que el proxy reenvíe la solicitud al broker, que rechaza la firma. Un error de transporte o HTTP `5xx` requiere revisar la cadena Cloudflare, NPM, mTLS y broker. La preparación firmada acredita el socket y el perfil.

```bash
curl --silent --show-error --max-time 8 --write-out '\n%{http_code}\n' \
  --header 'Content-Type: application/json' \
  --header "timestamp: $(date +%s)" --header "signature: v1=$(printf '%064d' 0)" \
  --data-binary '{}' \
  https://api-studio.e-scale.cl/v1/sofia/chat
```

El recolector Edge ejecuta este POST cada cinco minutos desde `vm-media-01`. En Prometheus, `studio_edge_sofia_ingress_healthy{job="agency-studio-edge",instance="vm-media-01"}` vale `1` si obtuvo el rechazo esperado y `0` si la comprobación falló. La serie `studio_edge_sofia_probe_unixtime_seconds` indica la hora de la última comprobación.

- `AgenciaStudioSofiaIngresoDegradado` avisa después de dos comprobaciones fallidas consecutivas. Revisar la ruta pública, el listener del broker y `NRestarts`.
- `AgenciaStudioSofiaSondaDesactualizada` avisa si caducan las muestras. En `.85`, revisar `systemctl status agency-studio-edge-metrics.timer` y `journalctl -u agency-studio-edge-metrics.service`. Cotejar el acuse OTLP y la consulta de Prometheus.

## Mitigación

1. Suspender el consumo de la orden afectada y conservar su identificador de idempotencia; no duplicarla.
2. Detener una llamada al superar 120 segundos. Aplicar reintento con espera exponencial y límite de intentos; respetar `Retry-After` cuando exista.
3. Comprobar en la orden la ronda actual: Free 1 revisión; Pro hasta 2 rondas autónomas; Ultra e Ilimitado hasta 3. Al alcanzar el límite, entregar fallo explícito a la orquestación en lugar de volver a invocar al equipo.
4. Identificar si falla proveedor, contrato JSON o MCP. Restaurar solo entradas `agency-*` cuando el cambio corresponda a ese equipo y comprobar que otros equipos permanezcan presentes.

## Reversión

Usar la [receta coordinada](../recipes/sre-rollback.md) con un manifiesto y hashes previos verificados. La restauración de `openclaw.json` requiere que coincida el hash de la configuración actual esperado por el manifiesto; si otros equipos modificaron el archivo después, el script debe rechazar la sustitución y se requiere reconciliar únicamente las entradas de agencia. No borrar memorias para romper un bucle.

## Verificación

Desde `.85`, con el entorno protegido de las sondas:

```bash
bash "$HOME/.local/share/agency-sre-v1/sre/smoke-test-e2e.sh"
```

Exigir salud autenticada del gateway. Después invocar por separado los cinco agentes y los cuatro MCPs con una orden de prueba: la sonda de salud no demuestra disponibilidad efectiva de cada modelo ni salida correcta. Verificar límites de rondas, ausencia de pedidos duplicados y preservación de equipos existentes.

## Resolución de problemas

| Síntoma | Causa probable | Acción |
|---|---|---|
| `429` del proveedor | Cuota o capacidad temporal | Respetar espera indicada y límite de intentos; revisar consumo. |
| `401` del proveedor | Credencial caducada | Sincronizar desde `.env`; nunca cambiar tokens solo en el contenedor. |
| Elena y Lucas repiten idéntica salida | Dictamen no aplicado o ronda sin incrementar | Detener orden y corregir contrato/estado persistido. |
| Gateway sano y Studio no puede conectar a Sofía | Preparación fallida o broker inactivo | Aplicar la recuperación del broker y repetir la prueba de ingreso. |
| Gateway sano y otro agente falla | Modelo o MCP no disponible | Probar agente y dependencia de forma aislada y conservar el modelo exacto. |

## Registros, escalado e historial

Consultar trazas por identificador de orden, duración por llamada y rondas; ocultar el contenido privado del cliente. Escalar al responsable de agentes ante un bucle o indisponibilidad persistente del proveedor. Registrar hora, agente, modelo, código de error y número de intentos.

| Fecha | Ejecutor | Resultado |
|---|---|---|
| 2026-11-05 | Preparación de Fase 0 | Procedimiento documentado; invocaciones reales pendientes. |
| 2026-11-15 | Plataforma | El broker quedó inactivo al reiniciar Gateway. Arrancar su unidad completó la preparación firmada y recuperó el ingreso HTTP 401 con el PID del Gateway conservado. [Registro de la recuperación](../../../ESTADO.md#primera-producción-del-portal) |
| 2026-11-15 | Plataforma | El identificador fijo devolvió HTTP 409 al repetirse. Se instaló una preparación con identificador por intento y reintento de systemd. La prueba aislada pasó tras dos fallos y el broker real volvió a arrancar con preparación firmada. [Registro del ensayo y de la instalación](../../../ESTADO.md#primera-producción-del-portal) |
| 2026-11-15 | Plataforma | La comprobación de ingreso quedó programada cada cinco minutos. Prometheus recibió una muestra saludable del primer disparo programado y las dos alertas quedaron inactivas. Se comprobaron fallos, recuperación, ausencia de muestras y rutas SMTP con datos de prueba. [Registro del temporizador y las alertas](../../../ESTADO.md#primera-producción-del-portal) |
