# Procedimiento rb-05: recuperación de PostgreSQL de Kitsu e Immich

**Responsable:** equipo de plataforma / SRE · **Severidad:** crítica ante corrupción o pérdida de datos.  
**Última revisión:** 2026-11-15 · **Frecuencia:** ante incidente y simulacro de restauración · **Última restauración aislada desde copia de producción:** 2026-11-15.

## Propósito y síntomas

Recuperar las bases de Kitsu e Immich desde una copia verificada manteniendo los archivos de medios del NAS. Objetivos de arquitectura: RPO menor a 24 horas y RTO menor a 15 minutos; deben medirse en un simulacro real antes de declararlos cumplidos.

## Prerrequisitos

- Acceso a `.85`, al respaldo local del NAS y al repositorio R2 cifrado; `RESTIC_PASSWORD` disponible en entorno protegido.
- Recibo y manifiesto del snapshot 3-2-1. Seleccionar el origen Synology o R2 desde el catálogo y verificar la huella de cada volcado. [Procedimiento de recuperación de archivos](../../../sre/README-backup-321-orchestrator.md#verificar-archivos-desde-ambos-repositorios)
- Compose previo, versión exacta de PostgreSQL y extensiones usadas por Immich, y credenciales respaldadas en `.env`.
- Respaldo con fecha, checksum y prueba de integridad; destino de restauración aislado de las aplicaciones activas.

## Diagnóstico rápido

En `.85`, como operador con acceso a Docker, identificar cada contenedor por sus etiquetas de Compose. PostgreSQL de aplicaciones atiende Kitsu y n8n. Immich tiene su propio proyecto.

```bash
sondear_postgres() {
  local proyecto=$1 servicio=$2 ids
  ids=$(docker ps --filter "label=com.docker.compose.project=$proyecto" \
    --filter "label=com.docker.compose.service=$servicio" --format '{{.ID}}') || return
  if [[ -z $ids || $ids == *$'\n'* ]]; then
    printf '%s\n' 'Se esperaba un único contenedor activo del servicio' >&2
    return 1
  fi
  docker inspect --format '{{.State.Health.Status}}' "$ids"
  docker exec "$ids" sh -c 'pg_isready -q -t 5 -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}
sondear_postgres agency-media postgres-apps
sondear_postgres agency-media-immich-lan postgres-immich
```

Se espera `healthy` y salida cero de `pg_isready` en ambos contenedores. Si difieren, revisar almacenamiento NVMe local, permisos y registro de PostgreSQL antes de restaurar.

## Mitigación

1. Detener escrituras de las aplicaciones afectadas y conservar el estado averiado para diagnóstico. No ejecutar `docker compose down -v` ni reinicializar bases.
2. Si el problema es un proceso detenido y no hay corrupción, recuperar únicamente su servicio y verificar dependencias antes de considerar restauración.
3. Si se requiere restauración, seleccionar la última copia anterior al incidente, comprobar checksum contra el catálogo de copias y validar el comprimido:

   ```bash
   : "${RESPALDO_PG:?Seleccionar ruta absoluta de la copia .sql.zst}"
   zstd -t -- "$RESPALDO_PG"
   ```

   **Resultado esperado:** copia íntegra. Esta comprobación detecta daño del comprimido; no demuestra consistencia lógica de PostgreSQL.

4. Restaurar primero en clústeres aislados sin red, con los mismos digests de imagen, versión y extensiones que los orígenes. Los volcados de `pg_dumpall` del snapshot se procesan con `psql`. Usar un superusuario temporal distinto de los roles del volcado, de manera que sus sentencias `CREATE ROLE` se ejecuten con `ON_ERROR_STOP`. El siguiente flujo requiere un contenedor de recuperación creado y aislado previamente:

   ```bash
   : "${CONTENEDOR_RECUPERACION:?Falta contenedor aislado de recuperación}"
   : "${USUARIO_RECUPERACION:?Falta el superusuario temporal del destino aislado}"
   case "$CONTENEDOR_RECUPERACION" in
     recuperacion-*) ;;
     *) printf '%s\n' 'El destino debe ser un contenedor aislado recuperacion-*' >&2; exit 1 ;;
   esac
   set -o pipefail
   zstd -dc -- "$RESPALDO_PG" | docker exec -i "$CONTENEDOR_RECUPERACION" psql -X --set ON_ERROR_STOP=on --username "$USUARIO_RECUPERACION" --dbname postgres
   ```

   **Si falla:** detener el procedimiento y conservar el error; no continuar hacia producción. El prefijo es una protección adicional, no sustituye verificar que el contenedor y sus volúmenes estén aislados.

5. Validar usuarios, proyectos, activos, referencias NAS y extensiones sobre el destino aislado. Volver a exportar las tablas cubiertas por el volcado y cotejar los recuentos y huellas de las filas de cada bloque COPY. Reconciliar modificaciones posteriores al punto de copia antes de preparar el cambio de conexión de las aplicaciones.

## Reversión

Conservar el directorio de datos y la configuración originales hasta completar la aceptación de la recuperación. Si la aplicación falla sobre el destino restaurado, detener sus escrituras y volver a la configuración anterior compatible sin mezclar dos ramas de datos. La [reversión del despliegue](../recipes/sre-rollback.md) cambia código/Compose; no restaura PostgreSQL ni garantiza compatibilidad con una migración destructiva.

## Verificación

1. Repetir `pg_isready` y ejecutar consultas de integridad y recuentos acordados antes del incidente.
2. Ejecutar `bash "$HOME/.local/share/agency-sre-v1/sre/smoke-test-e2e.sh"` desde `.85` con el entorno protegido de las sondas. Comprobar login en Kitsu e Immich y abrir un activo existente para validar su archivo NAS.
3. Probar una operación nueva controlada; medir RPO real desde la fecha de copia y RTO desde el inicio del incidente.
4. Ejecutar una nueva copia verificada cuando el sistema sea estable. Conservar la evidencia del simulacro y la retención previa.

## Resolución de problemas

| Síntoma | Causa probable | Acción |
|---|---|---|
| PostgreSQL no inicia | Disco lleno, permisos o corrupción | Conservar registro, revisar NVMe y evitar reinicializar volúmenes. |
| Immich falla tras restaurar | Extensión/vector o versión incompatible | Restaurar con imagen y extensiones exactas de la copia. |
| Base sana y medios ausentes | Montaje NAS o rutas | Aplicar [rb-04](rb-04-r2-nas-sync-backlog.md); no recrear registros a ciegas. |
| Copia local dañada | Error de escritura o retención | Recuperar copia cifrada R2 y validar su integridad antes de usarla. |

## Registros, escalado e historial

Consultar PostgreSQL, aplicaciones y catálogo de copias en almacenamiento protegido. Escalar al responsable de datos ante cualquier pérdida o si la copia no restaura; registrar RPO/RTO observados y pedidos afectados. No adjuntar SQL con datos de clientes ni contraseñas.

| Fecha | Ejecutor | Resultado |
|---|---|---|
| 2026-11-05 | Preparación de Fase 0 | Procedimiento documentado; simulacro de restauración pendiente. |
| 2026-11-15 | Plataforma | El snapshot automático de R2 devolvió ambos volcados en 9,308 s. La restauración y el cotejo desde los archivos preparados tomaron 39,132 s en aplicaciones y 45,827 s en Immich. Se verificaron 254 y 72 bloques COPY, respectivamente. La prueba usó clústeres aislados y no mide el cambio de conexión de producción. [Registro de las restauraciones](../../../ESTADO.md#verificación-y-seguimientos-de-fase-4) · [Registro de salud en producción](../../../ESTADO.md#primera-producción-del-portal) |
