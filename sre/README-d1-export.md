# Exportación D1 privada y verificación estructural

Unidad de Fase 4 con exportación e importación reales verificadas el 2026-11-06. Las 21 pruebas de desarrollo usan transporte controlado y no consultaron Cloudflare. El propietario ya creó y verificó la tabla `_agency_resource_owner`; ese antecedente no sustituye el preflight de cada captura. La exportación puede bloquear otras consultas mientras captura y se cancela automáticamente si deja de consultarse su progreso: coordinar su ventana antes del `--apply`.

## Configuración y staging previamente propios

`d1_export.py` lee literalmente la bóveda con el parser canónico, sin ejecutar valores. Utiliza únicamente `CF_ACCOUNT_ID`, `CF_D1_DATABASE_ID`, `CF_PLATFORM_API_TOKEN` y `CF_D1_EXPORT_STAGING_PATH`. El token se usa sólo en el cliente API, no se representa al imprimir configuración ni se añade a argumentos. HTTPX **0.28.1** ya está fijado en las dependencias CPU existentes del proyecto; esta unidad no instala ni añade dependencias.

Antes de aplicar, el responsable respalda la ruta de staging con los tres comentarios obligatorios en `.env` y la plantilla `CHANGE_ME`, crea la carpeta privada fuera de la aplicación y acredita sus permisos. El exportador sólo verifica esa carpeta; no la crea, cambia de dueño ni adopta un directorio por nombre. Debe estar vacía salvo `.agency-d1-export-owner.json`, archivo privado regular, sin enlaces ni hardlinks, con este contenido observado:

```json
{
  "owner": "agencia-digital-marketing-ai:d1-export:v1",
  "account_id": "ACCOUNT_ID_RESPALDADO",
  "database_id": "DATABASE_UUID_RESPALDADO",
  "resource": "studio-saas-db",
  "directory": {"device": 0, "inode": 0}
}
```

Los ceros y textos son espacios documentales; deben reemplazarse por los IDs respaldados y dispositivo/inodo reales. La función `stage_marker(settings)` devuelve el documento esperado para una carpeta existente. La carpeta debe ser privada del usuario efectivo: `0700` POSIX; marcador, SQL y recibo `0600`. En Windows se comprueba la ACL mediante el lector existente `backup_tools_access.py`, admitiendo únicamente la identidad actual, SYSTEM y administradores. Los ancestros no pueden contener enlaces o puntos de reanálisis. Las pruebas automatizadas simulan esa consulta ACL y no certifican permisos Windows reales.

Una captura usa un staging nuevo y vacío. El script rechaza un SQL/recibo anterior, un marcador de otra base, un inodo distinto o cualquier entrada adicional. No sobrescribe ni limpia datos ajenos. Un fallo durante descarga elimina sólo su parcial nuevo si conserva tipo, inodo y enlace único. Si cambió su identidad, se detiene sin eliminarlo. No se renombra un recurso ajeno para hacer sitio.

## Plan y aplicación explícita

Desde la raíz del workspace y el entorno Python fijado:

```powershell
python sre/d1_export.py
python sre/d1_export.py --apply
```

El primer comando valida configuración/staging y devuelve plan **sin red**. No consulta permisos Cloudflare ni acredita exportación. `--vault` permite la ruta local del archivo de configuración. `--staging`, si se indica, debe coincidir exactamente con la ruta respaldada; no admite escoger otro destino para una captura. El segundo comando inicia el preflight y la exportación reales únicamente cuando el responsable decida ejecutarlo.

El preflight autenticado consulta el registro de propiedad exacto —id 1, proyecto `agencia-digital-marketing-ai`, recurso `studio-saas-db`, versión 1— y después `PRAGMA table_list`. Rechaza tipos `virtual`/`shadow` y resultados desconocidos antes de iniciar el export. No elimina ni recrea tablas para sortear esa limitación. La URL API está fijada a Cloudflare y se construye sólo con cuenta hexadecimal y UUID validados, sin un endpoint configurable.

## Protocolo y límites

El cliente API es nuevo, `trust_env=false`, TLS verificado y redirecciones desactivadas. Publica a `/accounts/{account_id}/d1/database/{database_id}/export` con `output_format=polling` y `dump_options.compatibility=d1`. Mientras no haya estado, exige un `at_bookmark` opaco válido y estable y lo reenvía como `current_bookmark`. Sólo termina correctamente con `status=complete`, éxito del envelope y ausencia de éxito falso/error en el resultado. `status=error`, un estado desconocido, un bookmark perdido o cambiado y una respuesta inválida detienen la captura.

El preflight de lectura, el seguimiento con el mismo marcador y la descarga tienen hasta dos reintentos ante fallos de transporte o HTTP 502, 503 y 504. Los intervalos son 0,5 s y 1 s dentro del mismo plazo de 180 s. Si falla el primer POST de exportación, la ejecución termina con su código de error. Una descarga interrumpida retira sólo el parcial propio y vuelve a descargar desde el primer byte de la misma URL firmada.

La operación dispone de **180 s** compartidos para preflight, polling y descarga, con timeout por intercambio y comprobación por bloque. Son hasta 360 consultas de progreso, separadas por 0,5 s cuando sigue pendiente. El API limita cada respuesta JSON a 1 MiB. Una URL firmada sólo se acepta con HTTPS, autoridad DNS bajo `.r2.cloudflarestorage.com`, puerto predeterminado/443, sin usuario, fragmento ni caracteres de control. Otros dominios o un cambio de contrato futuro quedan rechazados para revisión.

La descarga usa un **segundo cliente nuevo**, sin autenticación ni cookies de API, con `trust_env=false`, sin redirecciones, sin Referer y `Accept-Encoding: identity`. No se aceptan respuestas comprimidas ni parciales. El SQL tiene máximo **64 MiB**, bloques de 64 KiB y comprobación de Content-Length cuando existe. Si falta longitud, el límite, EOF y la validación SQL siguen siendo obligatorios; un cierre en una frontera SQL válida no demuestra por sí mismo que estén todos los datos de la base original.

No se registran URL firmada, bookmarks, token, mensajes remotos ni contenido SQL. HTTPX emite URLs a nivel INFO. Durante la operación se suspenden sólo las trazas HTTPX/HTTPCore, con restauración de su estado al cerrar y contador para operaciones concurrentes. El recibo de captura y el journal de la unidad D1 conservan códigos seguros como `HTTP_API_503` o `TRANSPORTE_DESCARGA_D1`. La salida de éxito incluye únicamente rutas locales, identidad del recurso, tamaño, SHA256, cantidad de polls, duración y resultado estructural. No activar tracing de peticiones mediante un cliente personalizado fuera del contrato.

## Publicación y restauración SQLite efímera

El archivo parcial se abre de forma exclusiva y privada dentro del staging. Se sincroniza y relee para comprobar su hash. Antes de publicarlo, se ejecuta en SQLite `:memory:` con autorizador: deniega ATTACH/DETACH, LOAD_EXTENSION, funciones de archivos, tablas virtuales/temporales y PRAGMA ajenos a `foreign_keys`/`defer_foreign_keys`. Estas dos opciones son válidas en el dump. No reescribe el SQL para evitar rechazos.

El validador impone memoria de DB acotada mediante máximo 32768 páginas de 4 KiB, valores/sentencias limitados al máximo SQL, temp en memoria y progress handler con plazo de hasta 10 s dentro del presupuesto restante y tope de instrucciones. Exige sentencias completas, transacción cerrada, `quick_check`, `foreign_key_check` y el marcador propio restaurado. Un formato SQL incompatible con el SQLite disponible falla, sin ocultarlo como éxito.

Sólo después se publica el archivo nuevo `studio-saas-db.sql` mediante creación sin reemplazo y se añade `d1-export-receipt.json`, ambos privados. La publicación utiliza hardlink temporal dentro del mismo filesystem y retira exclusivamente su parcial; requiere que el staging soporte esa operación. Si se interrumpe después de publicar SQL y antes del recibo, no se declara éxito ni se reutiliza el staging automáticamente: el responsable investiga y conserva la captura disponible.

El recibo conserva `sqlite_integrity_verified=true`, `owner_verified=true` y siempre **`cloud_restore_verified=false`**. SQLite local es un escalón estructural; la recuperación D1 completa requiere importación real a una base temporal propia y comprobación de sus datos, en otra unidad. Cifrar el SQL antes de enviarlo a NAS/R2 y verificar recuperación del repositorio corresponden al flujo 3-2-1; este exportador no realiza esos envíos ni acredita RPO/RTO.

## Pruebas de esta unidad

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_d1_export.py -v
python -m ruff check sre/d1_export.py tests/test_d1_export.py
python -m ruff format --check sre/d1_export.py tests/test_d1_export.py
```

Los tests usan `httpx.MockTransport`, clientes nuevos y reloj controlado, además de SQLite y carpetas temporales. Comprueban protocolo sin estado inicial, bookmark, errores/plazo, descarga sin Bearer/cookies, hosts maliciosos, redirección/truncado/tamaño, logging activado, cancelación y preservación de archivos ajenos. No consultan Cloudflare, no hacen una copia real ni modifican la bóveda. Siguen pendientes la ACL real del staging y el export/import cloud coordinados por el responsable.

Fuentes primarias: [API oficial export D1](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/export/), [limitaciones de importación/exportación](https://developers.cloudflare.com/d1/best-practices/import-export-data/) y [PRAGMA table_list en D1](https://developers.cloudflare.com/d1/sql-api/sql-statements/#pragma-table_list). Investigación previa: `.tools/evidence/fase4-backup-research.md`, sección 4.

## Aceptación operativa inicial — 2026-11-06

El responsable exportó los metadatos iniciales de `studio-saas-db`: 588 bytes, SHA256 `a18f48f722ad735d40aba0bc4cf9ddbe9ca6e3a0a3535c87955e6d5ad9ddcf95`, un poll, 11,047 s de operación y 24,313 s totales incluyendo comprobaciones locales. La carpeta y los archivos privados se verificaron realmente en Windows. El SQL pasó integridad SQLite y comprobación del propietario.

La captura se importó por REST en una base D1 nueva y separada, `studio-saas-db-restore-20261106-01`, cuyo ID se respaldó antes del uso. Una tabla propia y su fila completa coincidieron con el origen, incluidas todas las columnas; `quick_check` devolvió `ok` y `foreign_key_check`, cero errores. El origen se consultó nuevamente y conserva los mismos datos. La base de prueba queda retenida para revisión.

El primer verificador utilizó `PRAGMA integrity_check`, que D1 rechazó con HTTP400/código7500 después de haber importado correctamente. Se conservó ese fallo y se completó una verificación sólo de lectura con `PRAGMA quick_check`, [admitido por D1](https://developers.cloudflare.com/d1/sql-api/sql-statements/#pragma-quick_check). No se recreó la base ni se repitió la importación para ocultar el fallo.

Evidencias privadas: `phase4-d1-export-real.json`, `phase4-d1-cloud-restore-created.json`, `phase4-d1-cloud-restore-first-failure.json` y `phase4-d1-cloud-restore.json`. El recibo del exportador conserva correctamente `cloud_restore_verified=false`: la recuperación cloud está acreditada en un recibo posterior separado. Esta prueba contiene sólo metadatos iniciales, no datos de clientes ni esquema SaaS productivo; no acredita copias cifradas NAS/R2, programación, retención ni objetivos RPO/RTO.

## Ensayo de Time Travel — 2026-11-13

La consulta de marcadores de `studio-saas-db` confirmó el backend `production` y fechas de hasta siete días. La API REST aceptó `YYYY-MM-DDTHH:MM:SSZ` y rechazó el desplazamiento `+00:00`. Para la base de 7,59 días, los puntos de ocho y 29 días precedían su creación. La verificación guardó códigos HTTP y tiempos, sin guardar marcadores. [API de marcadores](https://developers.cloudflare.com/api/resources/d1/subresources/database/subresources/time_travel/methods/get_bookmark/).

El ensayo de recuperación usó la base temporal `studio-saas-db-restore-321-20261113-01`, separada y registrada para las copias 3-2-1. Tras comprobar una fila sintética alterada, se recuperó su estado anterior con el marcador previo y se verificó la lectura en 1,781 segundos. Luego se retiró la tabla de prueba y se cotejaron el esquema y los metadatos de ambas bases. La operación de recuperación se hizo sólo sobre la base temporal. Este tiempo empieza con el incidente confirmado y la base accesible. [API de restauración](https://developers.cloudflare.com/api/resources/d1/subresources/database/subresources/time_travel/methods/restore/).

## Exportación independiente de las 03:00 de Santiago

`studio-d1-backup` usa los disparadores `0,2,5 6 * * *` y `0,2,5 7 * * *` de Cloudflare y convierte cada hora programada a `America/Santiago`. A las 03:00, 03:02 y 03:05 locales intenta publicar la misma orden HMAC, fechada a las 03:00, en `d1-clock/v1/AAAA-MM-DD.json` de `studio-backups-bucket`. R2 sólo escribe si ese objeto aún no existe. El Worker registra el resultado de cada intento y usa la clave HMAC `BACKUP_D1_CLOCK_TRIGGER_KEY_HEX` y un binding privado de R2. La credencial que autoriza la exportación reside en `vm-media-01`. [Programación de Cloudflare](https://developers.cloudflare.com/workers/configuration/cron-triggers/) · [Escritura condicional en R2](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)

`agency-backup-d1-clock.timer` consulta la orden entre las 03:00 y las 03:12 de Santiago. El servicio limita su tamaño y valida la firma, la fecha y la ventana. Si R2 responde 404 entre las 03:08 y las 03:12, el temporizador ejecuta la copia con el mismo identificador diario y registra `order_origin=temporizador-local`. Una orden válida registra `order_origin=r2-firmada`. Los errores de transporte, acceso o firma detienen ese intento. Ambos recorridos comparten el bloqueo de copia con el respaldo integral y cotejan la propiedad e integridad SQLite antes de sellar `d1.sql.zst` y `SHA256SUMS`. Restic copia el conjunto a `/volume2/producciones/_backups/d1` y al prefijo `d1-restic-v1` de `studio-backups-bucket`.

Los snapshots llevan las etiquetas `agency-d1-clock` y `run:d1-clock-AAAA-MM-DD`. El diario privado en `/var/lib/media-platform/backup-d1-clock-v1/journal` guarda los dos recibos y utiliza `d1_backup_complete` para la copia independiente. Ante una corrida interrumpida, comprueba el resultado y los snapshots ya creados con la etiqueta de esa fecha antes de volver a enviar datos. El servicio acepta una captura local confirmada de la misma corrida y se detiene si un envío previo quedó pendiente de conciliación.

El temporizador también ejecuta el servicio al arrancar y recupera activaciones de calendario omitidas. Antes de las 03:00 espera la ventana habitual. Si arranca después de las 03:12, captura la base vigente con la fecha local actual y registra `order_origin=temporizador-recuperacion`. Un recibo completo evita duplicar esa captura. Las fechas anteriores sin copia siguen apareciendo como huecos en el historial.

```bash
systemctl list-timers agency-backup-d1-clock.timer
journalctl -u agency-backup-d1-clock.service --since today
```

Para verificar una corrida, coteja el día y la hora con el journal. Cuando el origen sea `r2-firmada`, comprueba también la orden de R2 y el evento del Worker si está disponible. Cuando el origen sea `temporizador-local`, coteja el disparo con el intervalo de las 03:08 a las 03:12. Lee `SHA256SUMS` y `d1.sql.zst` de cada snapshot Restic por el ID de su recibo. Compara la huella del manifiesto, la huella del archivo comprimido y la del SQL descomprimido con el recibo de captura. El ensayo manual usa el identificador `d1-clock-acceptance-2026-11-15`.

### Alertas de la copia independiente de D1

`agency-d1-backup-metrics.timer` coteja el diario cada cinco minutos y envía el resultado por mTLS a Prometheus. A partir de las 03:27 de Santiago exige el recibo del día. Hasta esa hora verifica el día anterior para dar tiempo al último disparo de las 03:12. El informe exige el resultado completo y los dos recibos coincidentes de Synology y R2, con identificadores de snapshot distintos.

`AgenciaD1CopiaDiariaSinConfirmar` indica que llegó un informe reciente y faltan recibos válidos. `AgenciaD1MonitorCopiaSinMuestras` indica que el informe dejó de llegar. En el primer caso revisa el journal de `agency-backup-d1-clock.service` y los recibos de la corrida. En el segundo comprueba `agency-d1-backup-metrics.timer`, el journal de `agency-d1-backup-metrics.service` y el receptor mTLS. La recuperación de los archivos sigue el procedimiento de lectura de Restic. [Reglas de Prometheus](../docker/observability/prometheus.nas.rules.yml)

## Restauración después de la migración 0013

La segunda aceptación manual guardó el esquema de reintentos de webhooks de Mercado Pago. Restic recuperó el SQL acreditado desde Synology en 2,328 s y desde R2 en 10,973 s. Se importó el SQL de R2 en una base temporal vacía. La API D1 inició la carga con el MD5 del archivo, devolvió una URL de R2 de corta duración y completó la importación tras cotejar el ETag de la carga. El recibo retiene el SHA-256 del archivo como prueba de integridad. [API de importación masiva](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/import/)

La verificación comparó cada fila y el DDL del catálogo con el SQL acreditado. Recuperó las dos columnas y el índice de reintentos, incluidas las cinco filas pendientes con sus intentos. La importación y el registro del marcador tardaron 7,500 s. La lectura de ambas copias, la restauración y el cotejo tardaron 23,890 s con la base temporal preparada. Se retiró esa base y el Worker siguió vinculado a la base de Studio. [Evidencia operativa](../ESTADO.md#verificación-de-fase-4)

## Restauración después de la migración 0016

La captura cifrada de `0016` quedó en los repositorios D1 de Synology y R2. El cotejo usó los identificadores respaldados en `BACKUP_D1_MANUAL_ADJUSTMENTS_NAS_SNAPSHOT_ID` y `BACKUP_D1_MANUAL_ADJUSTMENTS_R2_SNAPSHOT_ID`, junto con los recibos del orquestador. Restic devolvió los mismos 136.681 bytes de SQL desde ambos destinos tras cotejar `SHA256SUMS` y el volcado comprimido. La restauración fue en SQLite en memoria y recuperó 45 objetos del catálogo activo, 19 tablas, 29 órdenes y dos ajustes del titular. La lectura, restauración y verificación tomaron 11,346 s. [Evidencia operativa](../ESTADO.md#verificación-y-seguimientos-de-fase-4)

El ensayo posterior importó ese SQL en una D1 temporal y cotejó el DDL y las 242 filas de las 19 tablas con el volcado. Pasaron la comprobación de integridad y las claves foráneas. La importación tardó 2,159 s y la lectura, importación y cotejo, 17,535 s. Se retiró el destino temporal tras verificar la versión y el binding activos. [API oficial de importación](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/import/) · [Evidencia operativa](../ESTADO.md#verificación-y-seguimientos-de-fase-4)

## Restauración del historial de Wrangler

Tras registrar `0000`–`0017`, se hizo una copia fuera del horario diario. Los dos snapshots cifrados devolvieron los mismos 140.969 bytes de SQL. La importación en una D1 temporal recuperó las 18 entradas de `d1_migrations` con su orden y marca de tiempo, además de los 45 objetos de aplicación. Se cotejaron las 260 filas de las 20 tablas con el SQL del respaldo. `quick_check` devolvió `ok` y las claves foráneas no presentaron incidencias.

Desde que comenzó la preparación del destino temporal hasta completar el cotejo pasaron 21,391 s. El destino quedó retirado tras verificar que el Worker seguía vinculado a `studio-saas-db`. La comprobación automática del diario conservó su referencia a la copia del 16 de noviembre. [Evidencias](../ESTADO.md#verificación-y-seguimientos-de-fase-4) · [API de importación de D1](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/import/)
