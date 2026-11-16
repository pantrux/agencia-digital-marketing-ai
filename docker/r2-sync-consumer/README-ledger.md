# Ledger persistente local del consumidor R2

[sync_ledger.py](sync_ledger.py) prepara la reserva persistente e idempotencia del consumidor futuro con Python estándar, SQLite y `flock` Linux. No descarga objetos, sirve HTTP ni configura servicios. Usa los tipos y validación de [sync_storage.py](sync_storage.py); la publicación del asset y el ledger son dos operaciones distintas, no una transacción distribuida.

## Raíz y archivos propios

El operador debe precrear una raíz **local persistente**, del UID que ejecutará el consumidor y con modo exacto `0700`. Debe acreditar externamente dispositivo/inodo y un marcador regular mediante `RootProof(device, inode, marker_name, marker_sha256)` de `sync_storage`. El marcador debe tener modo `0600`, un único enlace, el mismo dispositivo y el SHA-256 fijado. No se crea ni adopta la raíz o el marcador automáticamente.

`Ledger(state_root, proof, max_db_bytes=64*1024*1024)` sólo consulta la raíz. Rechaza enlaces simbólicos en sus componentes, sustituciones de padres, identidad o marcador distintos y permisos/propietarios ajenos. Mantiene abiertos los descriptores de sus ancestros durante cada operación y los vuelve a contrastar con los nombres. La raíz privada y los padres administrados siguen siendo una precondición: esto no proporciona aislamiento frente a un administrador que cambie rutas mientras SQLite las abre.

El dispositivo de la raíz se correlaciona con `/proc/self/mountinfo`. Se admiten tipos locales conocidos (`ext2/3/4`, XFS, Btrfs, F2FS, ZFS y overlay); se rechazan NFS, CIFS, FUSE y tipos no acreditados, incluido tmpfs. El tipo no certifica SSD físico ni la persistencia de una capa overlay: el operador debe verificar ambas condiciones. El ledger **no puede colocarse en `/mnt/producciones` ni en otro montaje NFS**; el asset sí utiliza por separado el almacenamiento de medios.

Los nombres de estado reservados por este módulo son `sync-ledger.sqlite3`, `.sync-ledger.lock` y el journal transitorio `sync-ledger.sqlite3-journal`, además del marcador externo. Los archivos abiertos deben ser regulares, modo exacto `0600`, del UID actual, del mismo dispositivo y sin enlaces duros adicionales. Se rechazan sidecars WAL/SHM. Este incremento no enumera ni elimina archivos ajenos de la raíz.

`ledger.initialize()` es una acción explícita de primera inicialización. Crea el lock propio y una DB nueva sin sobrescribir archivos existentes; una DB previa, incluso vacía o inválida, bloquea la inicialización. Un lock existente sólo se reutiliza si conserva identidad, permisos y contenido exacto del propietario `agencia-digital-marketing-ai:r2-sync-ledger:v1`. Una inicialización interrumpida que deje una DB incompleta requiere diagnóstico del operador: no se borra ni reinicializa automáticamente.

`ledger.check()` comprueba una DB existente, esquema, propietario, integridad y archivos sin reservar trabajos ni crear estado. Toma el mismo lock sin esperar. `LOCK_TIMEOUT` distingue una operación en curso de un estado corrupto o mal configurado; el servidor debe tratar esa ocupación de forma separada y no reiniciar un trabajo sano por ello. La prueba nativa conserva los bytes completos de la DB y cero filas antes/después, verifica la ocupación real y rechaza una tabla ajena.

## API del orquestador

```python
ledger = Ledger(state_root, state_proof)
# Sólo en la preparación inicial, con la raíz ya acreditada:
# ledger.initialize()

with ledger.operation(
    verified_job,
    verified_body_sha256,
    max_object_bytes=object_size_limit,
    wait_seconds=0.5,
) as operation:
    reservation = operation.reserve()
    if reservation.state == "completed":
        result = reservation.receipt
    else:
        # Pseudocódigo: el futuro orquestador obtiene la fuente DESPUÉS de reservar.
        chunks = obtain_chunks_after_reservation()
        publication = storage.publish(verified_job, chunks)
        if not reservation.is_new:
            operation.reconcile(publication)
        result = operation.complete(publication)
```

`verified_job` conserva los siete campos cerrados del [contrato Edge](README-protocol.md). La operación toma un snapshot, valida UUIDs, ruta derivada, política de tamaño y demás campos, y vuelve a calcular el SHA-256 del JSON canónico en el orden del protocolo. Debe coincidir con `verified_body_sha256`. No verifica HMAC, timestamp ni procedencia HTTP: esos controles corresponden al futuro servidor antes de llamar al ledger.

`operation.reserve()` guarda `pending` con `job_id` y fingerprint en una transacción confirmada **antes de devolver el control**. No mantiene una transacción SQLite abierta mientras llega el asset. La misma ID con otro fingerprint produce conflicto sin cambiar la reserva original. Una reserva nueva permite completar sólo mediante `operation.complete(publication)` tras el éxito real de `storage.publish`.

Un `pending` previo se conserva tras error o caída. Reabrirlo exige reconciliar el asset mediante una nueva operación real del almacenamiento: ésta puede devolver `created=True` si no había archivo o `idempotent=True` si quedó publicado. `operation.reconcile(publication)` habilita completar únicamente ese mismo recibo; no hay reset, eliminación ni transición automática de un pendiente a completado. El ledger valida la correlación, pero no puede demostrar por sí solo que un objeto `Publication` fue obtenido inspeccionando el archivo: el orquestador que lo entrega es de confianza.

El recibo debe ser el tipo `Publication` exacto y conservar `job_id`, `asset_id`, ruta derivada, bytes y SHA-256 del job. Dispositivo/inodo y los booleanos `created`/`idempotent` también tienen tipos y límites cerrados. No se aceptan recibos HTTP arbitrarios, campos adicionales ni diccionarios con payloads. `completed` conserva el primer recibo inmutable: repetir el mismo devuelve el original; intentar sustituirlo produce conflicto. Ese recibo registra la comprobación de publicación y no acredita que otro escritor no haya modificado después el asset.

## Bloqueo, transacciones y límites

El contexto retiene el descriptor del lock y `flock(LOCK_EX|LOCK_NB)` durante toda la operación, incluida la publicación del asset. Serializa a todos los workers cooperantes que usen esa raíz. La espera solicitada está entre cero y **cinco segundos**, nunca infinita. Por defecto usa reloj monotónico y pausas cortas; `clock`/`sleep` sólo se inyectan como funciones de confianza para pruebas. Un número finito de intentos impide que un reloj simulado inmóvil provoque un bucle sin fin. Cerrar el contexto o morir el proceso libera el bloqueo del kernel; no se borran locks por PID ni por antigüedad.

SQLite usa `BEGIN IMMEDIATE`, `journal_mode=DELETE`, `synchronous=FULL`, espera de bloqueo SQLite de 500 ms, `trusted_schema=OFF`, temporales en memoria y carga de extensiones deshabilitada. La conexión usa una ruta propia normal con `mode=rw`; **no** abre la DB mediante `/proc/self/fd`, porque los journals necesitan su directorio real. Sólo se reabre el esquema exacto de dos tablas, propietario, `application_id`, versión y comprobación de integridad esperados. Se rechazan tablas, vistas, índices o triggers adicionales del usuario.

La tabla de trabajos guarda únicamente `job_id`, fingerprint SHA-256, estado y recibo cerrado; no guarda cuerpo HTTP, bytes del asset, secretos o credenciales. El máximo de DB es 64 MiB y puede reducirse hasta cuatro páginas de 4096 bytes. Se aplica mediante `max_page_count` en cada conexión y verificaciones de tamaño antes y después; el journal tiene un límite adicional acotado. Una DB llena produce error y rollback, sin borrar reservas ya confirmadas. Falta definir retención/archivo de trabajos antes de alcanzar ese límite en producción.

Los errores del ledger tienen códigos fijos sin SQL ni payloads. Un fallo posterior a un COMMIT puede dejar el cambio persistido aunque el llamador no reciba éxito: el reintento bajo lock debe consultar la reserva existente. Una caída entre publicación de asset y COMMIT de `completed` deja un pendiente recuperable. No se borra el asset publicado para compensar un fallo del ledger, ni se envía ACK antes de confirmar el recibo.

## Verificación y pendientes

```text
python -m unittest discover -s tests -p test_r2_sync_ledger.py -v
python -m ruff check docker/r2-sync-consumer/sync_ledger.py tests/test_r2_sync_ledger.py
python -m ruff format --check docker/r2-sync-consumer/sync_ledger.py tests/test_r2_sync_ledger.py
```

Las pruebas portables verifican fingerprint, límites, recibos cerrados, parser de mounts y rechazo de esquemas engañosos. Las pruebas Linux usan SQLite, `flock`, archivos y dos procesos reales: exclusión, espera, salida abrupta posterior a la reserva, reapertura pendiente, reconciliación, completado inmutable, conflictos, symlinks/hardlinks, modos, sustitución de padres, límite de páginas y cierre de descriptores. Windows omite explícitamente la parte Linux y no acredita estas garantías mediante mocks.

La suite Linux real cuenta con 22 pruebas aprobadas, cero omisiones, en 0,389 s tras añadir `check()`. La aceptación integrada previa verificó HMAC, origen R2 real, publicación NFS y recuperación en un proceso nuevo tras una salida 77 entre publicación y completed, usando el mismo inodo y un estado local propio. Los recibos locales son `phase4-r2-ledger-health-green.json` y `phase4-r2-sync-recovery-real.json`; este último conserva el hash de la versión anterior del ledger, antes de añadir la consulta de salud.

Quedan pendientes el servidor HTTP y ACK integrado, política de retención, manejo de parciales tras caída y despliegue con volumen local persistente verificado. `flock` sólo coordina a procesos cooperantes; no convierte un NFS en almacenamiento válido para SQLite ni sustituye permisos del sistema. Este incremento no declara servicio desplegado ni aptitud para producción.
