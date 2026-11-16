# Procedimiento rb-04: acumulación de sincronización R2–NAS

**Responsable:** equipo de plataforma / SRE · **Severidad:** alta si impide ingestión o entrega.  
**Última revisión:** 2026-11-16 · **Frecuencia:** ante alerta · **Último ensayo en producción:** 2026-11-16.

## Propósito y síntomas

Recuperar consumo y sincronización entre R2 y `/mnt/producciones` sin perder originales ni confirmar trabajos incompletos. Síntomas: edad creciente del mensaje más antiguo, DLQ con mensajes, reintentos repetidos o discrepancia de tamaños y checksum.

## Prerrequisitos

- Acceso a `.85` y Synology `.8`, recursos R2/Queues identificados en `.env` y montajes de Fase 2 verificados.
- Acceso a métricas y registros del consumidor con valores de credenciales ocultos.
- Origen vigente `192.168.0.8:/volume2/producciones`, según [ADR-001](../../decisions/ADR-001-volumen-producciones.md). Preservar el datastore ajeno `/volume2/NFS`; la ausencia de VMs registradas asociadas no acredita que esté vacío.
- Token operativo de Cloudflare con acceso a D1 y a las dos colas, y los ID `CF_QUEUE_ID` y `CF_DLQ_ID` respaldados en `.env`.

## Diagnóstico rápido

En `.85`:

```bash
findmnt -T /mnt/producciones -o TARGET,SOURCE,FSTYPE,OPTIONS
df -h /mnt/producciones
df -i /mnt/producciones
curl --silent --show-error --max-time 5 --output /dev/null --write-out '%{http_code}\n' http://192.168.0.85:3010/healthz
```

**Resultado esperado:** origen exacto `192.168.0.8:/volume2/producciones`, NFS montado y espacio/inodos suficientes. La evidencia inicial de Fase 2 no sustituye esta comprobación durante el incidente; tampoco los ~2753,63 GiB libres de `/volume2` observados el 2026-11-05 sustituyen una lectura actual. Una respuesta `401` solo comprueba que existe un listener protegido; usar el verificador autenticado para salud. Si el montaje falta o apunta a otro origen, detener escrituras del consumidor antes de continuar para evitar llenar el disco local de `.85` o escribir en un recurso incorrecto.

Desde el directorio de despliegue `/opt/media-platform`, cuando Compose esté instalado:

```bash
docker compose ps r2-sync-consumer
```

**Resultado esperado:** consumidor activo. Consultar sus registros en el sistema protegido de observabilidad y correlacionarlos por ID de pedido/mensaje, sin publicar URLs firmadas.

### DSM rechaza el aprovisionamiento después de reiniciar el host

El consumidor puede responder a la sonda de salud mientras los pedidos nuevos reciben `503 SYNC_FAILED`. Si el POST firmado de la misma orden desde `.85` a `192.168.0.8:3148/v1/provision` devuelve `422` y el original en R2 conserva su huella, comprueba la identidad que usa el aprovisionador. En DSM, identifica la unidad y su perfil efectivo.

```bash
systemctl show agency-nas-r2-project.service -p ActiveState -p ExecStart --no-pager
```

En la unidad V5, ejecuta esta comprobación de solo lectura en DSM con una cuenta administradora. Si se volvió temporalmente a V4, usa la ruta de perfil que devuelve `ExecStart`.

```bash
sudo /usr/bin/python3 -I -B - <<'PY'
import hashlib
import json
import os
from pathlib import Path

profile = Path('/usr/local/etc/agency-r2-project-v5/profile.json')
data = json.loads(profile.read_bytes())
media = Path('/volume2/producciones')
assert data['media_root'] == str(media)
for path, key in ((media, 'media_proof'), (media / 'proyectos', 'projects_proof')):
    assert path.resolve(strict=True) == path
    proof = data[key]
    observed = path.stat()
    print(path, 'dispositivo', proof['device'], observed.st_dev,
          'inodo', proof['inode'], observed.st_ino,
          'filesystem', data.get('filesystem_id'), os.statvfs(path).f_fsid)
marker = media / data['media_proof']['marker_name']
print('Marcador coincide:', hashlib.sha256(marker.read_bytes()).hexdigest() == data['media_proof']['marker_sha256'])
PY
```

Con V5, los dos resultados de `filesystem` deben coincidir con el valor respaldado en el perfil. El servicio acepta un `st_dev` distinto cuando coinciden el identificador del filesystem, ambos inodos, el marcador y las ACL. Si falla alguno de esos controles, conserva el mensaje y escala al responsable de almacenamiento para cotejar la identidad del volumen.

Con V4, la recuperación manual aplica si solo cambió `st_dev`. Confirma el volumen, ambos inodos y la huella del marcador. Conserva el perfil anterior y respalda la huella del reemplazo en la bóveda. Ajusta únicamente los dos campos `device` mediante reemplazo atómico del perfil, preservando propietario `root:root` y modo `0444`. Reinicia `agency-nas-r2-project.service` y comprueba que sigue `active`. Si el arranque falla debido al cambio, restaura la copia del perfil y reinicia la unidad.

Reprocesa el mensaje de la orden según el procedimiento de DLQ. Comprueba el avance en D1 y la coincidencia de la huella en R2, NFS y SMB antes de retirar la referencia de la DLQ.

### Una parte se rechaza antes de llegar a la cola

1. Coteja la sesión en D1 y el mensaje de despacho. Si el vídeo todavía no está verificado y no hay despacho, revisa las solicitudes PUT multipart de Studio antes de intervenir el consumidor.
2. En las herramientas de red del navegador, registra el estado HTTP y la hora del PUT. Una excepción de red en Studio puede corresponder a un rechazo de R2 sin cabecera CORS. El portal solicita otra firma y reintenta la misma parte en ese caso.
3. Si necesitas distinguir la causa, haz un ensayo acotado con una carga de prueba y el firmante de Studio desde un cliente HTTP autorizado. Registra sólo el estado HTTP, el código S3 y si el encabezado CORS permite su lectura. `ExpiredRequest` indica que debe emitirse una firma nueva. `SignatureDoesNotMatch` exige cotejar el método, destino y encabezados de la solicitud con los firmados.
4. Usa la analítica de R2 como señal complementaria. En el ensayo, la consulta por objeto, por bucket y por cuenta mostró el `404 NoSuchUpload` de control. Los `403` por firma vencida y firma distinta quedaron fuera de las tres consultas. Un total de cero rechazos en esa fuente deja abierta la causa de los PUT sin respuesta útil en el portal.
5. En el panel de ingesta de Studio en Grafana, revisa las sesiones con señales `http_403` y `sin_respuesta`. La primera requiere que el navegador pueda leer el estado HTTP. La segunda también se registra ante errores de red y rechazos que el navegador no puede leer por CORS. Cada señal cuenta una vez por sesión aunque se repita el envío del informe. El ensayo del portal publicado comprobó un `403` de R2 sin CORS, un segundo PUT válido y una sola marca `sin_respuesta` en D1 y Prometheus, en la cohorte de ensayo.

[CORS en R2](https://developers.cloudflare.com/r2/buckets/cors/) · [Códigos de la API S3](https://developers.cloudflare.com/r2/api/error-codes/) · [Operaciones en la analítica de R2](https://developers.cloudflare.com/r2/platform/metrics-analytics/)

### Alertas de ingesta

- `AgenciaStudioR2ServidorDegradado` indica un `internalError` o HTTP 5xx registrados por Cloudflare para `CreateMultipartUpload`, `UploadPart` o `CompleteMultipartUpload` del bucket `studio-media-bucket` en una ventana de 15 minutos. Revisa el recuento, el estado de R2 y si las partes se recuperaron al reintentar. La analítica se consulta cada cinco minutos y deja tres minutos para la llegada de datos.
- `AgenciaStudioR2AnaliticaInaccesible` indica que pasaron 12 minutos sin una consulta válida a R2. Revisa el journal de `agency-studio-edge-metrics.service` en `vm-media-01`, el temporizador y el acceso de la credencial existente. Una consulta válida puede informar cero operaciones.
- `AgenciaStudioIntegridadIngestaFueraObjetivo` indica que los rechazos confirmados por SHA-256 o metadatos superan el 0,1 % de las cargas completas o evaluadas por integridad del portal de producción en 30 días. Revisa el motivo de aborto en D1 y la huella esperada. Las señales de red comunicadas por el navegador se consultan por separado.

## Mitigación

1. Identificar la primera causa: montaje ausente, espacio, permiso R2/Queue, checksum o consumidor caído. No incrementar réplicas ante un NAS inaccesible.
2. Recuperar el montaje y validar lectura/escritura compartida desde `.85`, `.7` y `P:` en `.6` siguiendo Fase 2. No ejecutar borrados de bibliotecas para liberar espacio.
3. Si hay `401`/`403`, verificar expiración y permisos mínimos de la credencial respaldada; persistir cualquier rotación en `.env`, actualizar `.env.example` y recién entonces sincronizar el destino.
4. Reanudar gradualmente. El consumidor debe confirmar el mensaje solo tras copia durable y comprobación de integridad. Reprocesar DLQ por su clave de idempotencia después de corregir la causa; no vaciarla ni confirmar mensajes en bloque.

### Reprocesar un mensaje de la cola de fallos

Cloudflare conserva los mensajes de `studio-jobs-dlq` durante cuatro días. El consumidor de la cola principal tiene cinco reintentos configurados.

1. Ejecuta `POST /accounts/{account_id}/queues/{dlq_id}/messages/peek` con `{"batch_size":10}`. Usa el ID de la DLQ de la bóveda. La API devuelve los mensajes y una referencia opaca sin reservarlos. Para un cuerpo con tipo JSON, decodifica Base64 antes de leerlo. Identifica el mensaje por `job_id`.
2. Consulta en D1 el pedido, la sesión multipart, la reserva y `job_outbox`. Comprueba que el pedido sigue en `queued`, la carga está en `completed` y la cuota en `reserved`. Coteja el cuerpo con `job_outbox.payload`, y la clave, tamaño y SHA-256 del original retenido en R2. Si el mensaje es incompatible o no corresponde a la fila, conserva el diagnóstico para corregir el contrato. Si el pedido ya avanzó, coteja el recibo antes de cerrar el caso.
3. Tras corregir la causa, publica sólo la fila cotejada en `POST /accounts/{account_id}/queues/{queue_id}/messages` de la cola principal. Usa `content_type: "json"` y el objeto de `job_outbox.payload` como `body`. Conserva el `job_id`. Si se pierde el acuse de publicación, consulta el estado en D1 antes de enviar otra vez.
4. Comprueba en D1 que el pedido avanzó a `processing` y la cuota a `consumed`, o que ya completó la entrega. Confirma la copia de NAS con el mismo tamaño y SHA-256. Después retira sólo la referencia del mensaje cotejado mediante `POST /accounts/{account_id}/queues/{dlq_id}/messages/purge` con `{"refs":[{"ref":"<referencia del mensaje>"}]}`. Consulta `peek` de nuevo para confirmar la retirada individual.

[Configuración de la DLQ](https://developers.cloudflare.com/queues/configuration/dead-letter-queues/) · [Inspección de mensajes](https://developers.cloudflare.com/api/resources/queues/subresources/messages/methods/peek/) · [Publicación](https://developers.cloudflare.com/api/resources/queues/subresources/messages/methods/push/) · [Retirada individual](https://developers.cloudflare.com/api/resources/queues/subresources/messages/methods/purge/)

## Reversión

Si la acumulación empezó con una entrega del consumidor, ejecutar la [receta de reversión](../recipes/sre-rollback.md) con Compose previo y su hash. No revertir datos ni borrar objetos R2: el artefacto de código previo debe poder continuar desde las mismas claves de idempotencia.

## Verificación

1. Repetir `findmnt`, capacidad y salud autenticada mediante `bash "$HOME/.local/share/agency-sre-v1/sre/smoke-test-e2e.sh"` desde `.85`.
2. En una orden de prueba, comparar tamaño y checksum de origen y destino, verificar publicación del kit y confirmar un único procesamiento.
3. Comprobar que disminuye la edad del mensaje más antiguo y que una nueva falla lleva a reintento o DLQ, sin pérdida silenciosa. Registrar métricas antes/después; HTTP 200 por sí solo no demuestra drenaje de la cola.

## Resolución de problemas

| Síntoma | Causa probable | Acción |
|---|---|---|
| Disco `.85` lleno, NAS libre | Escritura bajo montaje caído | Detener consumidor, restaurar NFS y reconciliar archivos locales antes de moverlos. |
| Descarga empieza y falla | Firma expirada, red o tamaño | Renovar acceso mediante flujo autorizado y reintentar transferencia idempotente. |
| DLQ crece con un mismo error | Fallo persistente o mensaje incompatible | Aislar mensaje, corregir contrato y ensayar un reproceso individual. |
| Cola vacía pero kit incompleto | Confirmación prematura | Detener entrega, reconstruir estado y corregir confirmación tras persistencia. |

## Registros, escalado e historial

Consultar métricas de cola/DLQ y trazas del consumidor, NAS y R2. Escalar al responsable de almacenamiento si falla NFS o integridad, y al de plataforma si hay confirmaciones prematuras. Registrar IDs y checksums, nunca tokens ni enlaces firmados vigentes.

| Fecha | Ejecutor | Resultado |
|---|---|---|
| 2026-11-05 | Preparación de Fase 0 | Procedimiento documentado; transferencia real pendiente. |
| 2026-11-15 | SRE | Un mensaje sintético incompatible llegó a la DLQ sin crear pedidos. Se verificó su retirada individual y pasaron las diez sondas. |
| 2026-11-15 | SRE | Una orden de ensayo con un objeto ausente en R2 llegó a la DLQ. Se repuso el MP4, se reenvió el mismo mensaje y se verificaron la copia al NAS y el consumo de cuota. Se retiraron la referencia de la DLQ y los datos de ensayo. |
| 2026-11-16 | SRE | DSM cambió el identificador de dispositivo después del reinicio de ESXi. Se cotejaron el marcador y los inodos, se actualizó el perfil y se reenvió un solo mensaje. La huella del original coincidió por R2, NFS y SMB. Se retiró la referencia de la DLQ y Studio publicó un único kit. |
| 2026-11-16 | SRE | La unidad V5 usa el identificador del filesystem por descriptor. En DSM, el mismo pedido firmado se pudo conciliar con el `st_dev` anterior y se rechazó un identificador de filesystem distinto. El servicio activo devolvió 200 firmado y pasaron las diez sondas. |
