# Ingesta multipart de medios

El portal comprueba en el navegador que el archivo MP4, MOV o WebM tenga pista de vídeo y dure como máximo diez minutos. Luego reserva una orden de producción. D1 guarda la orden y su cuota en el mismo lote. El slug de la orden asigna el proyecto y el ambiente de facturación delimita el acceso.

Durante el piloto, `GET /api/plan` devuelve `production_available` para indicar si la cuenta tiene acceso al formulario. En el entorno de ensayo, el titular puede iniciar pedidos Free y una cuenta con suscripción vigente de ensayo puede iniciar pedidos Pro, Ultra o Ilimitado. `POST /api/orders` responde `403 PRODUCCION_NO_HABILITADA` antes de reservar una orden fuera de ese alcance. Las consultas de pedidos y entregas conservan el acceso por propietario.

`POST /api/orders` acepta `output_resolution` con `1080p` (valor predeterminado) o `4k`. D1 fija la selección al reservar. Un plan limitado a 1080p recibe `422 RESOLUCION_FUERA_DEL_PLAN` si solicita 4K. Los reintentos requieren la resolución original. El despacho conserva el campo para órdenes de 4K y los receptores interpretan su ausencia como 1080p, incluidos los sobres que ya se firmaron. Valeria lleva el valor a la escaleta, Lucas coteja las dimensiones de las capturas y del máster, y Elena mide la resolución del MP4 antes de aprobarlo. [Migración 0009](../migrations/0009_production_resolution.sql)

La selección de archivos admite `output_format` con `mp4` como opción predeterminada. `prores4444` solicita incluir la gráfica transparente en MOV junto al video MP4 y requiere el plan Ilimitado. Los planes sin esa capacidad reciben `422 FORMATO_FUERA_DEL_PLAN` antes de reservar cuota. La representación de las solicitudes MP4 conserva su huella anterior y el despacho añade el campo al sobre firmado cuando se solicita ProRes. La migración fija el formato desde la reserva. [Migración 0011](../migrations/0011_production_output_format.sql)

Lucas vincula la escaleta con el recibo del MP4. Tras el dictamen aprobado de Elena, el publicador solicita al puente una gráfica transparente con la misma escaleta y pista de audio. Kitsu recibe el ZIP de campaña y R2 conserva el MOV como objeto privado. La firma `v3` incorpora los campos `proresBytes` y `proresSha256` después de `durationMs`. D1 registra ambos objetos en la misma transacción que cierra la orden y aplica la cuota a la suma de sus tamaños. `GET /api/kits` incluye `prores` con los bytes y URL de descarga cuando existe el MOV. `GET` y `HEAD /api/kits/{id}/prores` exigen la sesión del titular y cotejan el objeto en R2. [Migración 0012](../migrations/0012_delivery_prores.sql)

La publicación directa de Pages adjunta `pages-worker.js` como `_worker.js` y `pages-routes.json` como `_routes.json`. La vista previa debe devolver 200 en `/api/healthz` y 401 en `/api/auth/me` antes de promover el portal. Estos archivos conectan `/api/*` con el servicio Studio API.

Cada orden admite un archivo y usa las siguientes operaciones.

| Operación | Solicitud y resultado |
| --- | --- |
| Crear o recuperar | `POST /api/uploads` con `order_id`, `project_slug`, `extension`, `bytes`, `sha256` y `content_type`. Devuelve `id`, `uploadId`, `key`, `partSize`, `partCount` y `status`. La operación que abre una sesión nueva en R2 incluye `newlyOpened: true`. Repetir con datos idénticos recupera la misma sesión sin ese campo. |
| Firmar parte | `POST /api/uploads/<id>/parts/<n>/sign`. Usar el método PUT con la URL, el `Content-Type` devuelto y el segmento de exactamente `partSize` bytes, salvo el último. El navegador fija `Content-Length` al transmitir el Blob. |
| Consultar partes | `GET /api/uploads/<id>/parts`. Devuelve `parts` con `PartNumber`, `ETag` y `Size` acreditados por R2. |
| Completar | `POST /api/uploads/<id>/complete` con `{"parts":[{"PartNumber":1,"ETag":"…"}]}`. Enviar todas las partes en orden. La respuesta final incluye `hash_verified` y `etag`. |
| Abortar | `DELETE /api/uploads/<id>`. Al abortar antes de completar se cancela la orden y se libera la reserva. Un reintento de un aborto confirmado devuelve 204. |

Para firmar o listar partes, el Worker verifica la cookie y consulta en un solo lote del primario de D1 la sesión y la carga del mismo titular y entorno. Para ambas operaciones comprueba la reserva y el vencimiento. Para firmar comprueba también el número de parte antes de emitir la URL. [Lotes en D1](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch)

La sesión dura 24 horas. Cada URL firmada dura hasta cinco minutos. El Worker acepta un objeto de hasta 1 GiB y usa partes de 16 MiB. El navegador debe leer el encabezado `ETag` en la respuesta de R2. El origen del portal necesita una regla CORS en el bucket que permita PUT con `Content-Type` y exponga `ETag`. [CORS en R2](https://developers.cloudflare.com/r2/buckets/cors/) · [URLs firmadas](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)

El portal omite la consulta inicial de partes cuando recibe `newlyOpened: true`. Si falta el campo, consulta a R2 y conserva los ETag acreditados. Después firma y envía hasta tres partes pendientes a la vez. Si falla el PUT de una parte por red o con HTTP 403, 408, 429 o 5xx, solicita una URL nueva para la misma parte. Hace hasta dos reintentos, con esperas de 250 ms y 1 s. Cuando una parte agota sus reintentos o devuelve un error definitivo, el portal detiene la cola y espera las transferencias activas. El intento siguiente consulta las partes confirmadas y reutiliza la sesión. El progreso cuenta los ETag confirmados y la solicitud de cierre los envía en orden de parte. [Carga multipart en R2](https://developers.cloudflare.com/r2/objects/upload-objects/#multipart-upload)

El cron cancela hasta 20 reservas vencidas por ejecución en el entorno del Worker. Una orden sin sesión de carga vence a las 24 horas de su creación. Si existe una sesión, el cron comprueba también su vencimiento y cualquier operación activa. Las cargas en confirmación, las completadas y las órdenes con mensaje de despacho conservan la reserva. La cancelación libera el cupo en el mismo lote de D1. Al reutilizar la clave de una orden cancelada, Studio pide iniciar la carga de nuevo.

Al abrir una sesión multipart, el Worker verifica la cookie y agrupa en un lote D1 la vigencia de la sesión de Studio, el cotejo del pedido, la reserva de espacio y la lectura de la sesión multipart. La reserva es una sola sentencia condicionada por el titular autenticado, el entorno, el pedido y la cuota. El cálculo suma las cargas activas, los originales retenidos y los kits vigentes del mismo usuario y entorno. Las copias de un mismo archivo en R2 y NAS representan un solo archivo lógico. Free dispone de 2 GB, Pro de 25 GB y Ultra de 100 GB. Ilimitado carece de tope de almacenamiento y todos los planes conservan el límite técnico de 1 GiB por carga. Un exceso devuelve HTTP 409 con `ALMACENAMIENTO_AGOTADO` antes de crear una carga en R2. La apertura nueva registra el identificador de R2 en D1. La petición idéntica recupera la sesión existente con un solo lote. [Lotes en D1](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch)

La finalización revisa tamaño y ETag de cada parte, luego lee el objeto completo y verifica su SHA-256 sin cargarlo en memoria. El Worker confirma en un lote D1 el estado de la sesión y su mensaje de outbox. El payload conserva el contrato `SyncJob` existente y el reenvío usa `order_id` como identificador idempotente. Se pueden reintentar transiciones al expirar el reclamo de diez minutos. R2 retira multipart incompletos mediante su política de expiración de cargas. [Multipart en Workers](https://developers.cloudflare.com/r2/api/workers/workers-multipart-usage/) · [DigestStream](https://developers.cloudflare.com/workers/runtime-apis/web-crypto/#constructors)

Al cerrar una sesión abortada, D1 guarda `abort_reason` en el mismo lote que cancela la orden y libera el cupo. `cancelled` corresponde a la cancelación solicitada desde el portal, `expired` al vencimiento, `metadata_mismatch` al rechazo de los metadatos y `sha256_mismatch` a una huella distinta. Los abortos históricos permanecen sin motivo conocido. Los PUT directos que fallan antes de confirmar no generan un motivo de aborto por sí solos. [Migración 0014](../migrations/0014_upload_abort_reason.sql)

Tras confirmar el lote, el Worker intenta encolar esa orden en segundo plano con `waitUntil`. El cron conserva el barrido de recuperación. Ambos intentos usan la misma reserva de D1 y los mismos controles del consumidor. La respuesta de la carga depende del lote confirmado, mientras que el seguimiento del despacho se consulta en la orden. [Límites de Cloudflare Workers y Queues](https://developers.cloudflare.com/queues/platform/limits/)

Cuando el consumidor confirma la copia a Synology, D1 consume la cuota y registra la orden para despacho a producción. El flujo n8n consulta cada 15 segundos `POST /api/internal/production/dispatch` con una credencial dedicada. Da prioridad a los planes con mayor prioridad y confirma cada orden con su reserva de dos minutos. En `vm-media-01`, el receptor vuelve a cotejar tamaño y SHA-256 del archivo NFS antes de escribir el pedido en SQLite. Una repetición con los mismos datos conserva la misma fila. La confirmación registra la recepción del crudo para producción. En OpenClaw, FFprobe vuelve a verificar la pista de vídeo y el máximo de diez minutos sobre el archivo real. [Migración 0004](../migrations/0004_production_dispatch.sql) · [Flujo n8n](../../docker/n8n/production_dispatch_workflow.py)

Elena puede aprobar el máster en cualquiera de las rondas permitidas. Free tiene una, Pro dos, Ultra e Ilimitado tres. Ante defectos corregibles de título o subtítulo, Valeria usa el dictamen y el máster de la revisión anterior. Puede acortar los textos o ampliar el subtítulo de 50 a 64 píxeles en la plantilla base de 1080p. Conserva el ángulo, el plano y las pistas. Cuando Elena rechaza la última ronda, conserva el dictamen antes de enviar la acción `quality_rejected` al despacho. D1 verifica titular, entorno, recepción, brief, carga y número de ronda del plan. El pedido pasa a `failed` y conserva el cupo consumido. El mismo dictamen es idempotente. Un dictamen distinto o una orden ya entregada devuelve `409`. La lista de pedidos del titular expone `production_issue: "qa_rechazado"` y la huella del máster queda en D1. [Migración 0008](../migrations/0008_production_qa_rejection.sql)

Cuando Valeria guarda una escaleta que requiere intervención en el montaje, envía `quality_manual_review` con la huella de esa escaleta y la ronda. D1 conserva el pedido en `processing` y el cupo consumido. Studio muestra «Requiere revisión manual», conserva el brief y deja de esperar la entrega automática de esa orden. El despacho deja de suministrar el brief a los agentes y D1 bloquea la publicación de kits mientras exista la marca de revisión. El proyecto continúa en Kitsu para que el operador pueda revisar el material y el motivo queda registrado en la escaleta privada. [Migración 0010](../migrations/0010_production_manual_review.sql)

La comprobación local con Node 24 y SQLite puede ejecutarse desde la raíz del proyecto:

```powershell
.\node_modules\.bin\vitest.cmd run tests-ts/studio-ingest.test.ts tests-ts/r2-signing.test.ts --no-coverage
python -m pytest -q tests/test_d1_saas_schema.py tests/test_d1_resource_owner.py tests/test_d1_export.py
```

## Duración por ruta en Workers Analytics Engine

El enlace opcional `STUDIO_HTTP_METRICS` registra la duración observable dentro del Worker para el inicio y retorno de Google OAuth, la consulta y el cierre de sesión, la apertura multipart y la firma de partes. Cada punto contiene una etiqueta fija de ruta (`index1`), el entorno (`blob1`), los milisegundos (`double1`) y el estado HTTP (`double2`). La firma se registra como `multipart_firma`. El Worker escribe un punto sin incluir identificadores, parámetros de URL, cabeceras ni cuerpos. La ruta conserva su respuesta si falla el registro. [Escritura de métricas](https://developers.cloudflare.com/workers/examples/analytics-engine/)

Consulta el conjunto `studio_worker_http_v1` con el permiso de lectura de análisis de cuenta. La consulta agrupa por estado para que las respuestas anónimas 401 no reduzcan el p95 de las operaciones exitosas. La columna `muestras` estima el tráfico con el factor de muestreo y `observaciones` cuenta las filas efectivamente disponibles. [Consulta SQL](https://developers.cloudflare.com/analytics/analytics-engine/sql-api/)

```sql
SELECT index1 AS ruta, double2 AS estado,
       sum(_sample_interval) AS muestras, count() AS observaciones,
       quantileExactWeighted(0.95)(double1, _sample_interval) AS p95_ms
FROM studio_worker_http_v1
WHERE blob1 = 'produccion' AND timestamp >= NOW() - INTERVAL '30' DAY
GROUP BY ruta, estado
ORDER BY ruta, estado
```

El reloj del Worker avanza tras operaciones de entrada o salida. Una respuesta inmediata puede registrarse con 0 ms. El TTFB del navegador requiere su propia medición de red y se evalúa aparte. [Reloj de Workers](https://developers.cloudflare.com/workers/runtime-apis/performance/)

## Tiempo hasta el primer byte desde el portal

El portal observa `responseStart - requestStart` en peticiones `fetch` exitosas de sesión, panel y apertura o firma multipart. La medida empieza con el envío de la solicitud HTTP. La conexión se clasifica por los tiempos `connectStart` y `connectEnd`. El observador descarta respuestas de caché y recursos de otros orígenes. El inicio y el retorno de Google siguen disponibles como métricas internas del Worker. [Resource Timing de W3C](https://www.w3.org/TR/resource-timing/)

El portal envía tandas de hasta doce muestras al receptor de su propio origen, que exige una sesión de Studio vigente. Analytics Engine guarda la ruta normalizada, el estado, los milisegundos redondeados y la clase de conexión en `studio_browser_http_v1`. Cada muestra tiene un máximo de 120.000 ms. Las cuentas sintéticas van a la serie `ensayo`. Las muestras prescinden de identificadores, direcciones de correo, parámetros de URL y datos del contenido.

```sql
SELECT index1 AS ruta, blob2 AS conexion,
       sum(_sample_interval) AS muestras, count() AS observaciones,
       quantileExactWeighted(0.95)(double1, _sample_interval) AS p95_ms
FROM studio_browser_http_v1
WHERE blob1 = 'produccion' AND timestamp >= NOW() - INTERVAL '30' DAY
GROUP BY ruta, conexion
ORDER BY ruta, conexion
```
