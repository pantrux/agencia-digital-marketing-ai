# Sala de Entrega de Kits

La [Fase 6](../PLAN-DE-IMPLEMENTACION.md) incorpora la sala de entrega al portal autenticado. La ruta usa un manifiesto en D1 por kit aprobado y el binding privado MEDIA para descargar su ZIP desde R2. La cuenta sólo ve los kits de sus pedidos de producción completados en el entorno actual. [Arquitectura de la plataforma](../ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md)

## Contrato del portal

| Solicitud | Resultado |
| --- | --- |
| `GET /api/kits` | `{"kits":[...]}` con hasta 50 entregas propias, ordenadas por fecha de publicación e ID descendentes. Cada fila contiene `id`, `order_id`, `title`, `bytes`, `created_at` en segundos UTC y `url` relativa. |
| `GET /api/kits/{kit_id}/download` | Flujo del ZIP como adjunto, con `Cache-Control: private, no-store`. |
| `HEAD /api/kits/{kit_id}/download` | Los metadatos de descarga y su tamaño, sin abrir el flujo del ZIP. |

El router obtiene la identidad de la sesión. El Worker valida la unión usuario–pedido–kit antes de consultar R2. Un ID que no pertenece a un pedido propio completado devuelve 404. Un objeto faltante o que cambió después de publicarse devuelve 409. La primera versión envía el ZIP completo y declara `Accept-Ranges: none`.

La sala de entregas usa el plan registrado en el pedido y cuenta el plazo desde la publicación del kit. Free dispone de 7 días, Pro de 30, Ultra de 90 e Ilimitado tiene acceso continuo. Al vencer el plazo, el kit deja de aparecer en la lista y su enlace devuelve 404 antes de consultar R2. El manifiesto se conserva para reconciliar los reintentos del productor. La eliminación física en R2 y NAS requiere su propio proceso.

Desde la raíz del proyecto, `python -I -B sre/audit_studio_kit_retention.py` coteja los manifiestos D1 con el tamaño, SHA-256 declarado y ETag en R2, y con el tamaño y SHA-256 del ZIP en la ruta canónica del NAS. El resultado indica cada vencimiento y el estado de ambas copias, sin mostrar credenciales ni IDs. Una ruta NAS ausente exige comprobar la procedencia del kit antes de cualquier limpieza. El primer kit piloto precedía a la ruta del publicador automático y su copia canónica ya fue cotejada.

En el modo de sólo lectura, si el manifiesto incluye ProRes, el informe añade el estado del MOV en R2 y Synology. En Synology usa el dictamen del ZIP cotejado para localizar la revisión aprobada y comprueba el tamaño y SHA-256 del MOV. Cuando no puede acreditar la revisión, informa `sin_dictamen`. Este cotejo completo se ejecuta a demanda para evitar releer todo el catálogo ProRes en cada retiro diario. El plan Ilimitado conserva el MOV con retención continua.

El retiro de contenido en R2 se ensayó con un objeto sintético. Una escritura `PutObject` con `If-Match` sustituye el ZIP vencido por un marcador de cero bytes cuando coincide su ETag. El marcador conserva el ETag y SHA-256 originales en metadatos. La clave ocupada hace que un reintento del publicador reciba HTTP 412 y evita que vuelva a cargar el ZIP. La auditoría reconoce el marcador. La operación se probó en el prefijo `health/`. [Escrituras condicionales de R2](https://developers.cloudflare.com/r2/api/s3/conditional-operations/)

En Synology, el retiro probado localmente en DSM comprueba la ruta canónica, un único enlace, tamaño y SHA-256. Mueve el ZIP con renombrado exclusivo al directorio privado del mismo volumen y vuelve a cotejarlo antes de quitarlo. Si el proceso se interrumpe tras el renombrado, coteja el ZIP pendiente y termina su retiro en la siguiente ejecución. La prueba usó medios sintéticos.

La tarea Windows `Agency-Studio-KitRetention` ejecuta el comando con `--retirar-vencidos --registrar` a las 06:15 de Santiago y al iniciar sesión. La cuenta debe tener abierta su sesión y poder leer la bóveda y el recurso Synology. El comando coteja D1, deja el marcador de R2 y luego invoca el retiro en DSM por SSH con la huella fijada. Si hay otro manifiesto vigente con ese ZIP en la misma carpeta, conserva la copia de Synology. El resultado se guarda en `.tools/evidence/studio-kit-retention-last-run.json`. Para reinstalar la tarea ejecuta `sre/Register-StudioKitRetention.ps1`. La primera prueba de la tarea encontró siete kits vigentes y cero candidatos.

La tarea revisa también los originales de cargas completadas tras la entrega, cancelación o fallo de un pedido. Aplica el plazo del plan del pedido desde el kit más reciente o, para pedidos sin manifiesto, desde la última actualización. Los pedidos en cola y en proceso conservan sus originales. Antes de retirar un crudo vencido, la tarea vuelve a consultar en D1 el estado del pedido y la identidad de la carga. Si cambiaron, pospone el retiro.

El retiro condiciona la escritura del marcador de R2 al tamaño, ETag, SHA-256 e identificador de carga. Luego coteja tamaño y SHA-256 del original en `01_raw` y completa el retiro en DSM. Si el pedido no llegó a Synology, el NAS informa ausencia cuando tampoco encuentra un archivo en cuarentena. Si encuentra un archivo pendiente con la carpeta del proyecto ausente, lo conserva para reconciliarlo. Para las cargas históricas con metadatos anteriores, lee el objeto completo con `If-Match` sobre el ETag observado y coteja el SHA-256 con D1. El marcador conserva el ETag original y el identificador del registro para reconocer reintentos. El informe distingue estos registros de los que coinciden con el formato actual.

## Publicación desde producción

La [migración aditiva](../../cloudflare-saas/migrations/0003_delivery_kits.sql) crea los manifiestos. Un productor autorizado realiza la aprobación y el ensamblado del ZIP fuera del portal. Usa un UUID propio del kit, distinto para cada revisión, y obtiene su clave exacta con `deliveryKitKey(mode, orderId, kitId)`. El formato es `kits/{sandbox|production}/{order_id}/{kit_id}.zip`. Calcula el SHA-256 del ZIP y lo publica en R2 privado con metadato personalizado `sha256`.

Tras publicar los bytes, el productor invoca `publishDeliveryKit(env, {id, orderId, userId, title, bytes, sha256})` desde un contexto de confianza del Worker. La función exige un pedido completado, verifica por HEAD la clave, el tamaño y el metadato y conserva la versión física de R2 y su ETag en D1. Repetir exactamente el mismo manifiesto devuelve la fila original. Si cambia el contenido, la revisión nueva requiere otro ID. El portal ofrece las rutas de lista y descarga.

La primera entrega de un pedido en proceso usa `POST /api/internal/kits/publish`. El operador publica el ZIP privado en R2, vuelve a leerlo y coteja su SHA-256 antes de firmar el manifiesto. El JSON incluye `id`, `orderId`, `userId`, `title`, `bytes`, `sha256`, `uploadId` y `sourceSha256`. `uploadId` y `sourceSha256` vienen de la carga original confirmada de ese pedido. El titular, la carga y la cuota consumida deben coincidir en D1. El Worker verifica la identidad física del ZIP y registra su manifiesto y el estado `completed` en el mismo lote transaccional. El reintento idéntico devuelve la entrega existente. [Transacciones D1](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch)

La primera entrega y las revisiones comparten el tope de almacenamiento de la ingesta. D1 suma los bytes del ZIP a las cargas activas, los originales retenidos y los kits vigentes del titular en el mismo entorno. El alta verifica ese espacio como parte de la escritura. Si se supera el tope, devuelve HTTP 409 con `ALMACENAMIENTO_AGOTADO`. El pedido en proceso conserva su estado y el productor puede reintentar cuando haya espacio. El ZIP preparado en R2 y Synology queda disponible para ese reintento.

El operador toma `STUDIO_KIT_PRODUCER_HMAC_SECRET` de la bóveda aprobada. La cabecera `timestamp` contiene segundos UTC y admite una diferencia de 120 segundos. La cabecera `signature` contiene `v1=` seguido del HMAC-SHA256 hexadecimal del siguiente arreglo JSON compacto en UTF-8. `entorno` corresponde al `BILLING_MODE` de la instancia.

```text
["studio-kit-v1","POST","/api/internal/kits/publish",entorno,timestamp,[id,orderId,userId,title,bytes,sha256,uploadId,sourceSha256]]
```

El publicador lee el dictamen aprobado del ZIP y añade `durationMs` al manifiesto. Usa `studio-kit-v2` como contexto de firma, añade el campo al final del arreglo y pone `v2=` en la cabecera. D1 guarda la duración junto al manifiesto y el cierre del pedido. La firma v1 sigue permitida para el formato histórico. Un reintento v2 con el mismo ZIP puede completar la duración de un manifiesto v1. Una duración distinta de la ya registrada devuelve conflicto.

Durante la descarga se cotejan clave, bytes, metadato SHA-256, ETag y versión con el manifiesto. Los encabezados se construyen en el Worker. El SHA-256 se coteja con el metadato del productor y la versión fijada. La descarga transmite el flujo sin recalcular un hash del ZIP dentro del Worker. [API oficial R2 sobre objetos, versiones y descargas](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)

La prueba local utiliza SQLite con la migración real y un binding R2 simulado. Comprueba el cierre transaccional, la repetición idempotente, la validación del crudo, el rechazo de firmas y la reversión ante un fallo de escritura. El piloto desplegado verificó el ZIP en R2 y Kitsu, el estado completado, una sola fila en D1 y el rechazo de la descarga anónima. El titular confirmó que descargó y abrió «Cuatro segundos para revisar» desde Entregas con su sesión de Google.

El temporizador de publicación inicia el servicio de entrega de Kitsu y espera a que termine. Cada orden piloto tiene un proyecto, una tarea y una revisión reservada en Kitsu, un UUID de kit respaldado en la bóveda y un título de entrega. El servicio toma el último dictamen, comprueba el brief confirmado y la huella del máster. El ZIP Free incluye `01-video.mp4` y `05-caption.txt`, con el texto del ángulo que eligió Valeria de la estrategia de Mateo. El listado SHA-256 incluye el caption. El servicio adjunta el MP4 y el ZIP a la revisión asignada y guarda un recibo privado. Una revisión rechazada queda pendiente de corrección.

OpenClaw también activa cada etapa mediante unidades `agency-production-*.path` que observan el directorio local de recibos de la etapa anterior. Las mismas unidades de servicio validan los recibos y preservan sus comprobaciones de idempotencia. Los temporizadores atienden los trabajos pendientes, los cambios en D1 y la recepción desde el montaje NFS. Para revertir la activación por archivos, deshabilita las seis unidades `.path` y comprueba los temporizadores existentes. [Activación por rutas en systemd](https://freedesktop.org/software/systemd/man/latest/systemd.path.html)

Mateo guarda por orden el número de fallos y el próximo intento en el subdirectorio privado `/var/lib/agency-production-mateo/reintentos`. Tras un fallo la unidad termina con error. El siguiente disparo omite esa orden mientras dure su espera y puede atender otra. La espera comienza en 30 segundos, se duplica en cada fallo consecutivo y queda limitada a cinco minutos. Al publicar la estrategia correctamente elimina el registro de reintento.

En Windows, `Agency-Kitsu-Pilot-Provision` ejecuta el comando con `--pendientes` al iniciar sesión y cada minuto mientras la sesión operativa está disponible. La consulta de D1 requiere una orden en pruebas, en proceso, con crudo recibido y brief confirmado. Admite los planes Pro, Ultra e Ilimitado. En Free admite al titular del piloto y el par exacto de orden y usuario sintéticos registrado para la verificación. Usa un título con la fecha UTC de confirmación y el prefijo de la orden. Si la asignación ya existe, coteja sus identificadores con la bóveda y conserva su título. El Programador omite el disparo si la tarea sigue activa y el bloqueo local impide que se ejecuten dos aprovisionamientos simultáneamente. `--listar-pendientes` devuelve sólo el número de órdenes elegibles y sirve para comprobar la consulta sin publicar asignaciones.

La tarea se instala desde la sesión del operador con `docker/kitsu-zou/Register-KitsuPilotProvision.ps1`. Cuando ya existe, el script verifica la acción y ajusta el intervalo conservando la cuenta y los permisos. D1 devuelve en una misma consulta la orden y su resolución. El aprovisionador respalda juntos los IDs de kit, vista previa y comentario y la huella de la asignación antes de escribir el registro de OpenClaw. Si se interrumpe la escritura, la siguiente corrida reutiliza el ID de kit respaldado.

La consulta avanza en páginas de 32 por fecha de confirmación e identificador. Si falla una orden, el comando continúa con las siguientes y devuelve código 1 al Programador al terminar. Una orden que siga siendo elegible se vuelve a revisar en la siguiente corrida. Los totales de consola muestran cuántas órdenes eran elegibles, cuántas se aprovisionaron y cuántas fallaron.

El operador también puede preparar la asignación con un título explícito desde la raíz del proyecto y el entorno Python con Paramiko. Ese modo admite una orden Free de pruebas en cola o en proceso del titular del piloto o de la prueba sintética designada. El aprovisionador usa una sesión administrativa temporal para crear el proyecto y asociar al bot. Tras verificar la revocación, usa al bot de revisión para crear la tarea y reservar la revisión. Respalda los identificadores en `.env` y publica la asignación en OpenClaw.

```powershell
& '.tools\venvs\synology-audit\Scripts\python.exe' -B 'docker/kitsu-zou/provision_order.py' '<uuid-orden>' 'Título de entrega'
```

El publicador valida el recibo contra la asignación y usa `If-None-Match: *` para crear el objeto sin reemplazar una versión existente. Acepta 412 sólo después de descargar el objeto y cotejar metadatos, tamaño y SHA-256. Con esa comprobación firma el manifiesto de Studio. [Escrituras condicionales en R2](https://developers.cloudflare.com/r2/api/s3/conditional-operations/)

El empaquetador escribe el ZIP directamente en Synology y comprueba el SHA-256 del máster mientras lo copia. El publicador carga el ZIP por bloques, con longitud y SHA-256 firmados, y comprueba esos datos en una lectura completa de R2 antes de confirmar el manifiesto en D1. Este tramo admite ZIP de hasta 3 GiB. En Kitsu, el adaptador y las rutas de carga de revisiones y adjuntos admiten cuerpos multipart de hasta 512 MiB, incluida la envoltura. Las demás rutas de la API conservan 128 MiB. Zou utiliza `/var/lib/zou/tmp` para los temporales de la biblioteca multipart y de la aplicación. La imagen de esta actualización deriva de la imagen Linux instalada y conserva el usuario y los montajes del servicio. [Configuración Nginx](../../docker/kitsu-ui/nginx.conf) · [Receta de imagen](../../docker/kitsu-zou/Dockerfile.upload-runtime)

En una orden ProRes, el adaptador de Kitsu prepara la vista previa del MP4 aprobado mientras el puente GPU genera la gráfica MOV de esa misma revisión. La vista previa puede quedar disponible durante la generación. La entrega adjunta el ZIP y emite el recibo sólo cuando termina la vista previa y se verifica el MOV. Ante un fallo, la siguiente ejecución coteja los recibos por revisión y los adjuntos de Kitsu antes de continuar. El publicador de Studio carga hasta tres partes del MOV a la vez y envía la siguiente cuando se libera un espacio. Calcula el SHA-256 de los bytes que envía y coteja la huella antes de completar condicionalmente la carga con las partes en orden. Descarga todo el objeto para verificar de nuevo su SHA-256 antes de confirmar el manifiesto en D1.

Si falla la preparación o el adjunto en Kitsu, la unidad de entrega registra el próximo intento del pedido en `/var/lib/agency-production-delivery/reintentos/entrega.sqlite3` y evalúa los siguientes pedidos elegibles. Registra el tipo de error en el journal y elimina la espera cuando escribe el recibo verificado. El publicador de Studio solicita que primero se ejecute la unidad de entrega y puede procesar los recibos existentes aunque esa unidad falle. Cada recibo se coteja con su asignación y con el ZIP preparado.

Si la publicación de un pedido falla, el publicador guarda el próximo intento en SQLite dentro de `/var/lib/agency-production-publish/reintentos` y revisa las siguientes entregas. En ambas etapas, el primer plazo es de 30 segundos y se duplica hasta un máximo de cinco minutos. La siguiente activación por temporizador o por cambio de archivos vuelve a evaluar los pedidos cuyo plazo haya vencido. El journal muestra `ALMACENAMIENTO_AGOTADO` si Studio rechazó el manifiesto por falta de espacio. El publicador conserva el ZIP preparado y elimina la espera cuando registra el recibo de publicación.

El listado de órdenes muestra si se está verificando la recepción o si el vídeo ya llegó y falta confirmar el brief. Tras confirmar el brief, el portal indica «En producción · con demora» si transcurren seis minutos y el pedido sigue en proceso. Studio consulta el estado hasta que el pedido sale de proceso. Las fechas de recepción y confirmación provienen de D1 y se exponen sólo al titular de cada pedido.

El comando de auditoría consulta los briefs confirmados en los últimos 30 días y mide el tiempo hasta el primer kit. Cuenta como muestra de 60 segundos la entrega cuyo dictamen aprobado registra una duración de 60 s con tolerancia de un fotograma a 30 fps. Para los nuevos manifiestos usa la duración firmada que conserva D1 después de vencer el ZIP. Para el histórico coteja el SHA-256 del ZIP en Synology con D1 antes de leer el dictamen. El informe incluye los pedidos pendientes con seis minutos o más y permite calcular las entregas sin cotejo a partir del recuento total y el verificado. D1 registra `portal_started_at` al crear un pedido desde la sesión validada de Studio. El informe distingue ese origen de los pedidos históricos y de las pruebas internas, cuya procedencia queda sin acreditar.

```powershell
& '.tools\venvs\synology-audit\Scripts\python.exe' -I -B sre/audit_studio_production_latency.py --registrar
```

### Reel por escenas

En Producción, **Montaje** permite pedir un reel de 30–45 segundos. Studio conserva la selección en la orden y Valeria genera de dos a seis cortes ordenados sobre el material recibido. Los rótulos tienen posición y límites por fotograma. El mismo montaje sincroniza el audio del MP4 y la gráfica transparente del MOV cuando se solicita ProRes.

Lucas guarda la vista previa de 1080p y su recibo antes de solicitar el máster. Elena revisa la apertura, el cierre y muestras por escena, con el rótulo esperado en cada punto. Un rechazo de vista previa sigue el circuito existente de revisiones y se vincula a la huella de la escaleta y del archivo revisado. El máster aprobado sigue el flujo normal de Kitsu y Entregas.

La activación del selector requiere aplicar primero la migración `0018_production_edit.sql` y tener el despachador, el receptor, Valeria, Lucas, Elena y el puente de render en la versión que admite el montaje.

### Medición continua de entregas

`agency-studio-delivery-metrics.timer` ejecuta la consulta cada cinco minutos desde `vm-media-01`. Lee la duración firmada que se guardó en D1 y envía recuentos y percentiles agregados por TLS mutuo al receptor OTLP de Prometheus. Las etiquetas identifican el origen del pedido y su entorno. Los recuentos de completados y de duraciones verificadas permiten detectar entregas históricas sin duración firmada. [Receptor OTLP de Prometheus](https://prometheus.io/docs/guides/opentelemetry/)

La ventana móvil empieza en la confirmación del brief. El informe calcula el p95 por rango más cercano para entregas de 60 s, con tolerancia de un fotograma a 30 fps. El p95 desde la reserva se publica por separado. Una entrega cuenta en plazo si tarda menos de 360 s desde el brief. Los pedidos cancelados, demorados y en revisión manual tienen sus propios recuentos.

El entorno se toma del pedido. Una cuenta cuyo correo termina en `.invalid` alimenta la serie de ensayo aunque su pedido figure en modo producción. Los pedidos de portal restantes se agrupan por `production` o `sandbox`. Los pedidos sin una reserva registrada desde el portal forman la serie de origen sin acreditar. El p95 se omite en una serie vacía.

El tablero de Grafana muestra la antigüedad del informe, el volumen, los p95 y la cobertura. El control por línea de comandos de arriba también coteja días históricos en Synology, por lo que puede acreditar más duraciones antiguas que la medición automática. [Studio · Latencia de entrega](https://grafana-lan.e-scale.cl:9444/grafana/d/agency-studio-delivery-slo/studio-c2b7-latencia-de-entrega)

```bash
systemctl list-timers agency-studio-delivery-metrics.timer
journalctl -u agency-studio-delivery-metrics.service --since today
```

## Anuncio guiado por guion

En una orden **Reel por escenas** con salida MP4, confirma el brief con una llamada a la acción de hasta 70 caracteres y el nombre de marca que aparecerá al cierre. Studio activa un anuncio de 30 segundos con planos del original, subtítulos de la voz y una placa de marca y CTA entre los segundos 26 y 30. La mezcla utiliza «Carefree» de Kevin MacLeod, con licencia CC BY 4.0, y atenúa la pista durante el diálogo. La biblioteca conserva la fuente original con huella fijada. El ZIP incluye el crédito para la publicación del anuncio. La revisión transcribe la mezcla final y mide la coincidencia temporal con los subtítulos antes de aprobar.

Aplica primero la migración D1 `0019_brief_ad_cta.sql`. Despliega el Worker, las recetas de los agentes y el renderer antes de publicar la interfaz. Las órdenes con el campo vacío siguen la ruta habitual. El máster sólo se genera tras el dictamen de vista previa y conserva los controles técnicos del perfil de la orden. La revisión comprueba encuadre de apertura, subtítulos por frases y el nivel de la música al final. Escuchar la mezcla sigue siendo una comprobación editorial independiente.
