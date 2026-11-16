# Publicación local de assets en Linux

Este incremento prepara únicamente la escritura y publicación de un asset ya entregado como iterador de bytes. [sync_storage.py](sync_storage.py) usa Python estándar y mantiene el contrato de siete campos del [productor Edge](../../src/media/sync-contract.ts). No descarga objetos ni inicia un servidor, procesos o conexiones. El contrato HTTP y su firma permanecen en el [protocolo R2](README-protocol.md).

## Configuración exigida

Un operador de confianza debe acreditar previamente la raíz absoluta de medios y entregar `RootProof(device, inode, marker_name, marker_sha256)`. La identidad procede de una comprobación externa; obtenerla automáticamente de cualquier ruta entrante anularía esta guarda. El marcador debe existir, ser regular, tener un solo enlace y coincidir por SHA-256; su nombre es un componente fijo y su contenido se limita a 64 KiB. Este módulo no crea ni adopta la raíz, marcadores o proyectos.

`SyncStorage(media_root, proof, *, max_object_bytes)` exige Linux, `/proc/self/fd`, las primitivas `dir_fd` y un límite positivo obligatorio de bytes. Rechaza rutas relativas, componentes `.`/`..`, enlaces simbólicos y cambios de filesystem dentro de la raíz. El constructor verifica raíz y marcador sin escribir; cada publicación vuelve a comprobarlos. Todos los descriptores se cierran al terminar cada operación.

La raíz y el árbol de proyectos necesitan control de acceso externo. Las comprobaciones de nombre/inodo detectan sustituciones, pero no proporcionan aislamiento frente a otro escritor con la misma identidad privilegiada entre la última comprobación y una operación de nombre. El módulo no modifica propietarios, ACL ni permisos de directorios existentes.

## Entrada y destino

`publish(job, chunks)` toma una copia inmutable de los siete campos exactos: `job_id`, `asset_id`, `project_slug`, `extension`, `bytes`, `sha256` y `object_key`. Exige ambos UUIDv4 en minúsculas, slug ASCII de hasta 63 caracteres sin nombres DOS reservados, extensión alfanumérica de 1 a 10 caracteres, SHA-256 hexadecimal en minúsculas y tamaño positivo entero seguro de JavaScript, sin superar `max_object_bytes`. Rechaza campos adicionales, booleanos como tamaños y normalizaciones implícitas.

La clave R2 debe ser exactamente `ingest/<job_id>/<asset_id>.<extension>`, pero nunca se usa como ruta de disco. El único destino posible es `proyectos/<project_slug>/01_raw/<asset_id>.<extension>` bajo la raíz acreditada. Los tres directorios deben existir previamente. El recorrido de la ruta absoluta y de esos directorios usa descriptores retenidos con `O_DIRECTORY|O_NOFOLLOW`; se comprueba su vinculación antes y después de publicar. Una sustitución de un padre no redirige la escritura al nuevo árbol.

## Escritura, publicación y conflictos

Los chunks deben ser objetos `bytes` inmutables. El módulo crea un parcial aleatorio propio `.sync-<aleatorio>.partial` con `O_EXCL|O_NOFOLLOW` y modo solicitado `0600` dentro del mismo `01_raw`. Completa escrituras cortas, limita bytes antes de escribir, calcula el SHA-256 y exige tamaño y hash exactos. Hace `fsync` del archivo antes de publicar.

La publicación usa `os.link('/proc/self/fd/<fd>', nombre_final, dst_dir_fd=rawfd, follow_symlinks=True)`: el origen es el descriptor ya abierto, aunque cambie el nombre del parcial. El enlace no sobrescribe un destino existente. No se usa `rename`, copia entre filesystems ni borrado previo del destino. Tras retirar el nombre parcial propio se cierra su descriptor de escritura, se hace `fsync` del directorio y se comprueba el destino por un nuevo descriptor, tamaño, hash e identidad estable. El cierre se administra con un objeto de archivo idempotente; la limpieza final no cierra un número de descriptor que haya sido reutilizado.

Un destino preexistente sólo da resultado idempotente si es regular, no es un enlace simbólico, tiene un único enlace duro y sus bytes y SHA-256 coinciden exactamente. Un archivo distinto, FIFO, enlace simbólico o archivo con otros enlaces produce conflicto y permanece intacto. Se elimina únicamente el nombre parcial si sigue siendo regular y conserva el dispositivo/inodo propios; una sustitución se conserva para revisión. No se busca ni elimina un parcial movido por otro actor.

La consulta del nombre anterior a la apertura comprueba tipo, filesystem e identidad; el número de enlaces se exige sobre el descriptor abierto y de nuevo al terminar. NFS puede mostrar temporalmente `nlink=2` en su caché tras retirar el parcial, mientras el descriptor de escritura permanece abierto. Se cierra antes de abrir el destino para permitir la revalidación de atributos. Ese valor preliminar no provoca un conflicto por sí solo. Un segundo enlace real sigue siendo un conflicto, incluso si contiene exactamente los bytes esperados.

El resultado `Publication` contiene IDs, ruta relativa, bytes, SHA-256, dispositivo/inodo y los indicadores `created`/`idempotent`. `StorageError` expone un código fijo sin contenido del asset. Si un fallo sucede después de crear el enlace final, `published=True`; el destino se conserva incluso si falla un `fsync` o una comprobación posterior. Ese indicador exige reconciliación del futuro consumidor y no acredita durabilidad tras un corte ni autoriza borrar el destino.

## Verificación y límites

Desde la raíz del proyecto:

```text
python -m unittest discover -s tests -p test_r2_sync_storage.py -v
python -m ruff check docker/r2-sync-consumer/sync_storage.py tests/test_r2_sync_storage.py
python -m ruff format --check docker/r2-sync-consumer/sync_storage.py tests/test_r2_sync_storage.py
```

Las pruebas Linux usan `TemporaryDirectory` y las operaciones nativas de archivos: enlace desde `/proc/self/fd`, inodo conservado, idempotencia, colisiones, enlaces/FIFO, sustitución de padres/parcial, chunks truncados o excesivos, hash incorrecto, escrituras cortas, limpieza y cierre de descriptores. Los puntos de fallo controlados envuelven las llamadas reales; no simulan que Windows tenga estas garantías. En Windows sólo se ejecutan los validadores portables y las pruebas Linux quedan explícitamente omitidas. El 2026-11-06 se ejecutaron realmente las 21 pruebas en `.85`: cero omisiones, fallos o errores; 0,084 s.

La aceptación aislada en el NFSv4.1 real descargó dos veces una muestra pública R2 de 43 bytes. La primera publicación creó el archivo; el reintento conservó el mismo inodo y devolvió idempotencia. Se rechazó un hash incorrecto y no quedaron parciales; 0,645 s. La raíz de prueba tiene marcador e identidad respaldados y conserva sus archivos para revisión. El recibo local es `phase4-r2-sync-nfs-real-v4.json`. Se conservaron los intentos previos que revelaron la caché de enlaces NFS, junto con dos regresiones rojas y verdes. Esto verifica `linkat` y `fsync` durante la ejecución observada; no acredita recuperación tras caída del NAS.

Quedan pendientes el ledger transaccional, `flock` y la coordinación entre consumidores, recuperación/reanudación y limpieza de parciales tras caída, presupuesto temporal de la fuente de bytes, servidor HTTP, verificación HMAC/replay y ACK R2. El resultado idempotente del archivo no equivale a una garantía de procesamiento exactamente una vez. No se acredita despliegue ni aptitud para producción con este incremento.
