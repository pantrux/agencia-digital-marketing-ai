# Procedimiento rb-02: migración D1 fallida

**Responsable:** equipo de plataforma / SRE · **Severidad:** crítica si impide autenticación, cuotas o pedidos.  
**Última revisión:** 2026-11-16 · **Frecuencia:** ante migración fallida · **Último diagnóstico en producción y restauración aislada:** 2026-11-16.

## Propósito y síntomas

Detener una entrega cuyo esquema D1 falla o resulta incompatible con el Worker. Señales: comando de migración con salida distinta de cero, excepción SQL en `/api/healthz`, cuotas inconsistentes o pedidos rechazados tras el cambio.

## Prerrequisitos

- Acceso autenticado a la cuenta Cloudflare y a `studio-saas-db`. Cotejar su ID con `CF_D1_DATABASE_ID` y con el binding D1 del Worker activo.
- Wrangler 4.162.0 para el ensayo local y el token de plataforma respaldado en `.env` si se usa la API REST de D1.
- Exportación previa verificada, identificador de versión estable del Worker y procedimiento de restauración ensayado en una base aislada.

## Diagnóstico rápido

En la consola de la base D1 identificada, ejecutar cada consulta por separado:

```sql
PRAGMA quick_check;
PRAGMA foreign_key_check;
SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'd1_migrations';
PRAGMA table_info(upload_sessions);
PRAGMA table_info(brand_profiles);
PRAGMA table_info(production_manual_adjustments);
```

**Resultado esperado**

Las consultas devuelven `ok` en integridad, cero incidencias de claves foráneas, la columna `abort_reason` de `0014` y las tablas incorporadas en `0015` y `0016`. Tras registrar el historial inicial el 16 de noviembre, `d1_migrations` contiene las 18 entradas de `0000` a `0017`. Su campo `applied_at` corresponde al registro del historial, pues estas migraciones se ejecutaron antes por el procedimiento operativo. [Registro del cotejo](../../../ESTADO.md#primera-producción-del-portal) · [Consultas SQL de D1](https://developers.cloudflare.com/d1/sql-api/sql-statements/)

Si la tabla existe, cotejar su contenido con los archivos del release y consultar los pendientes con Wrangler sobre la configuración que identifica la base de producción.

```sql
SELECT id,name,applied_at FROM d1_migrations ORDER BY id;
```

## Mitigación

1. Detener el despliegue y el consumo de operaciones que escriban tablas afectadas. No reintentar automáticamente un SQL que modifica datos sin analizar su estado.
2. Distinguir autenticación (`401`/`403`, token caducado o permisos insuficientes), conectividad y error SQL. Corregir permisos desde la bóveda sin cambiar el esquema por ensayo y error.
3. Reproducir el SQL y validar el esquema sobre D1 local desde la raíz del proyecto antes de preparar una corrección.

   ```bash
   npm exec --yes --package=wrangler@4.162.0 -- python scripts/ci/check_d1_migrations.py
   ```

   El control debe registrar todas las migraciones en la base temporal, comprobar integridad y claves foráneas y obtener el mismo registro en una segunda aplicación. La recuperación de la base remota se verifica después con el procedimiento operativo.

4. Si el esquema coincide y falta el historial de Wrangler, revisar los recibos de aplicación y recuperar una copia verificada en un entorno aislado antes de adoptar un registro de migraciones. El ensayo local con 4.162.0 comprobó que aplicar sobre un esquema existente sin historial falla. Con el registro reconstruido en la base desechable, dos aplicaciones consecutivas conservaron los 45 objetos y las filas de las 19 tablas. Las fechas de ese registro corresponden al ensayo. Para producción, consultar el historial por SQL y conservar los recibos de las migraciones ejecutadas por el procedimiento operativo. [Registro del ensayo](../../../ESTADO.md#primera-producción-del-portal) · [Historial de migraciones de Wrangler](https://developers.cloudflare.com/d1/reference/migrations/)

## Reversión

La [reversión coordinada](../recipes/sre-rollback.md) restaura la versión previa del Worker; no revierte automáticamente datos D1, Pages ni migraciones. Antes de revertir código, comprobar compatibilidad con el esquema que quedó aplicado. Si hay pérdida o corrupción, seleccionar un punto de recuperación anterior al incidente y restaurarlo primero en una base aislada; reconciliar pedidos y pagos posteriores antes de sustituir producción. No borrar tablas ni restaurar datos en caliente para resolver una falla de código.

Consulta Time Travel en modo lectura para confirmar si existe un marcador anterior al incidente. En la API REST usa una fecha UTC con sufijo `Z`. Cada marcador pertenece a la base de origen. Para un ensayo aislado, importa el SQL verificado a una base temporal propia o usa un marcador creado en esa misma base temporal. El endpoint de restauración reemplaza el estado de la base indicada. [Procedimiento ensayado](../../../sre/README-d1-export.md#ensayo-de-time-travel--2026-11-13) · [Time Travel de D1](https://developers.cloudflare.com/d1/reference/time-travel/).

## Traslado del primario a otra región

Trasladar el primario implica importar el contenido en una base con otro UUID. El ensayo aislado confirmó que Cloudflare rechazó crear otra base con el mismo nombre y renombrarla mediante `PATCH`. Si se evalúa otra base, acordar su nombre físico con el titular y conservar la base original para poder revertir. [API de D1](https://developers.cloudflare.com/api/resources/d1/subresources/database/) · [Ubicaciones de D1](https://developers.cloudflare.com/d1/configuration/data-location/)

1. Confirmar la ubicación asignada al destino, el nombre y los UUID de ambas bases. Preparar las configuraciones de la vinculación `DB` del Worker, el capturador de copias, los colectores de métricas y Wrangler. Conservar juntos los identificadores y archivos de la configuración anterior.
2. En la ventana acordada, detener la admisión de escrituras y comprobar que han terminado las operaciones en curso. Incluir los callbacks de Google OAuth, los webhooks, el cron, las colas y cualquier cliente directo de la API D1. Conservar las filas de historial y de reintentos agotados. La exportación bloquea las demás solicitudes a D1 mientras corre. [Importación y exportación de D1](https://developers.cloudflare.com/d1/best-practices/import-export-data/#known-limitations)
3. Exportar el estado final, verificar la copia e importarla en el destino. Comparar el catálogo, las filas y el historial de migraciones. Comprobar integridad y claves foráneas. Validar las sondas y la conexión de los respaldos y colectores antes de reabrir las escrituras.
4. Antes de la primera escritura en el destino, la reversión puede volver a la configuración anterior. Si el destino ya aceptó escrituras, detener la admisión y conciliar esas operaciones antes de volver al origen. Repetir las sondas y confirmar la recepción de pedidos y eventos de pago.

## Verificación

1. Cotejar el catálogo afectado, `quick_check` y `foreign_key_check` con el resultado anterior al cambio.
2. Ejecutar desde `.85` el comando `bash "$HOME/.local/share/agency-sre-v1/sre/smoke-test-e2e.sh"` con el entorno protegido de [sondas](../recipes/sre-rollback.md#prerrequisitos-para-una-reversión-real). Exigir salud del Worker y sus dependencias.
3. Verificar sesión Google y consulta de cuotas de los cuatro planes con cuentas de prueba. Conciliar las filas afectadas y los identificadores de pedidos sin duplicar cobros.

## Resolución de problemas

| Síntoma | Causa probable | Acción |
|---|---|---|
| CLI rechaza credenciales | Token o cuenta incorrectos | Verificar nombres de variables y permisos; no imprimir el token. |
| Tabla o columna ausente | Migración incompleta o configuración distinta | Contrastar identidad de D1, catálogo real y recibos de aplicación. |
| Integridad correcta pero API falla | Contrato código/esquema | Revertir código compatible o preparar migración aditiva validada. |
| Pruebas intermitentes | Entorno compartido o datos no aislados | Aislar la base de prueba; conservar el fallo y corregirlo antes de repetir. |

## Registros, escalado e historial

Consultar registros del despliegue y trazas del Worker con datos personales ocultos. Escalar al responsable de datos y plataforma ante corrupción, migración parcialmente aplicada o incompatibilidad no resuelta. Registrar migración, versión, punto de recuperación y conciliación; ningún secreto en el incidente.

| Fecha | Ejecutor | Resultado |
|---|---|---|
| 2026-11-05 | Preparación de Fase 0 | Procedimiento documentado; base de producción pendiente. |
| 2026-11-13 | Ensayo de Fase 4 | Marcador anterior disponible y recuperación de una fila verificada en D1 temporal en 1,781 s. Producción consultada en lectura. |
| 2026-11-15 | Plataforma | El SQL recuperado de R2 restauró 37 objetos y 181 filas en una D1 temporal en 23,890 s. En producción se verificaron integridad, claves foráneas, columna `abort_reason`, binding y despliegue respaldado. [Restauración](../../../sre/README-d1-export.md#restauración-después-de-la-migración-0013) · [Registro de la consulta en producción](../../../ESTADO.md#primera-producción-del-portal) |
| 2026-11-16 | Plataforma | Tras cotejar el esquema y los datos iniciales con `0000`–`0017`, se guardó el punto de Time Travel y se registró el historial mediante un lote SQL. Se conservaron los 45 objetos de aplicación y el recuento de las 19 tablas. Wrangler 4.162.0 confirmó cero migraciones pendientes. En la base aislada el lote inválido se revirtió y una migración de ensayo posterior se aplicó una sola vez. [Registro de la intervención](../../../ESTADO.md#primera-producción-del-portal) |
