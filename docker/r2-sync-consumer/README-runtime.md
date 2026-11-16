# Servicio HTTP del consumidor R2

Esta unidad conecta el [protocolo firmado](README-protocol.md), el cliente R2,
la publicación segura y el ledger SQLite. Es código preparado para Linux; no
crea una imagen, contenedor, puerto, secreto, proyecto ni montaje. El despliegue
y la aceptación HTTP real quedan para un incremento posterior del operador.

## Código y configuración del operador

La carpeta instalada debe contener exactamente estos seis archivos, sin
`__pycache__`, enlaces, archivos adicionales ni permisos de escritura:
`sync_http.py`, `sync_runtime.py`, `sync_protocol.py`, `r2_client.py`,
`sync_storage.py` y `sync_ledger.py`. Los archivos deben ser regulares, de un
solo enlace y propiedad de root o del UID del servicio; la carpeta también debe
pertenecer a ese UID o a root. Ningún ancestro puede ser un enlace simbólico,
pertenecer a otro UID o permitir escritura de grupo/otros.
El operador acredita previamente las rutas y los SHA de las dos fuentes nuevas.
El runtime verifica además los cuatro SHA heredados antes de importar los
helpers en la entrada aislada.

La CLI exige `python -I -B -S`: no incorpora automáticamente el directorio del
script ni paquetes del usuario, no procesa `site`/`.pth` y no escribe bytecode.
Después de validar el paquete incorpora únicamente su carpeta fija. El Python
ejecutado por el supervisor es la ruta absoluta resuelta de su propio
intérprete, regular, propiedad de root o del UID actual y sin escritura para
grupo/otros. Estas opciones se describen en la
[documentación de CPython](https://docs.python.org/3.12/using/cmdline.html).

`--config` señala un JSON UTF-8 cerrado, de hasta 16 KiB, sin claves duplicadas.
Los campos obligatorios son:

| Campo | Contrato |
| --- | --- |
| `schema_version` | Entero `1` para proyectos ya existentes, o `2` para preparar el proyecto en Synology. |
| `provision_on_nas` | Obligatorio y `true` para el esquema `2`. |
| `media_root`, `state_root` | Rutas Linux absolutas acreditadas por el operador. |
| `media_proof`, `state_proof` | Objetos cerrados con `device`, `inode`, `marker_name` y `marker_sha256` observados; nunca identidades inventadas. |
| `account_id` | Los 32 caracteres hexadecimales minúsculos de la cuenta R2. |
| `max_object_bytes` | Política obligatoria, entero positivo seguro; no tiene valor de reserva. |
| `hmac_secret_file` | Ruta al archivo de 32–128 bytes de la clave HMAC, sin decodificación ni eliminación de saltos. |
| `r2_access_key_file`, `r2_secret_key_file` | Rutas a credenciales R2 ASCII; no se recortan ni normalizan. |
| `listen_host`, `listen_port` | IPv4 canónica y puerto entero 1024–65535 elegidos por el operador. |

La configuración y los tres secretos deben ser archivos regulares, de un solo
enlace, propiedad del UID efectivo y modo `0400` o `0600`. La lectura mantiene
descriptores `NoFollow` de los ancestros, comprueba tamaño, identidad y cambios
durante la lectura. No se lee la bóveda general ni se generan claves. Las raíces
de almacenamiento y estado mantienen las guardas de sus helpers: el ledger
requiere almacenamiento local compatible, raíz `0700` y archivos `0600`.

El padre conserva un snapshot en memoria. Cada worker vuelve a leer los mismos
archivos y debe coincidir con ese snapshot antes de verificar el HMAC. Una
rotación requiere reiniciar coordinadamente el servicio; no se adopta una clave
nueva por petición. El identificador del snapshot sólo viaja por el stdin
privado al worker; no aparece en respuestas ni logs. Los `repr` de configuración,
runtime y credenciales están redactados. Esto no promete borrar todas las
copias de secretos que pueda mantener el intérprete.

## Inicialización explícita y arranque posterior

Los siguientes comandos son una interfaz para el operador, con rutas de
ejemplo que debe sustituir por las acreditadas y respaldadas antes de usarlas:

```sh
/usr/bin/python3 -I -B -S /ruta/operador/codigo/sync_runtime.py init-db --config /ruta/operador/config.json
/usr/bin/python3 -I -B -S /ruta/operador/codigo/sync_http.py serve --config /ruta/operador/config.json
```

`init-db` es una acción explícita previa; conserva las reglas de propiedad e
idempotencia del ledger. `serve` comprueba el estado existente antes de abrir el
listener y falla si falta o está dañado. No inicializa SQLite implícitamente.
Un flock ocupado permite iniciar y anunciar `busy`; no se trata como corrupción.
El modo interno `worker` sólo admite el envelope acotado por stdin, sin rutas,
URLs ni comandos elegibles por la petición.

## Preparación de proyectos en Synology

El esquema `2` reenvía la orden original a `192.168.0.8:3148` tras verificarla y reservar el trabajo. La unidad `agency-nas-r2-project.service` corre como UID 1024 y GID 100. Sólo acepta el POST `/v1/provision` desde `192.168.0.85` y verifica la firma Edge y su vigencia otra vez. El perfil privado fija la identidad del directorio `proyectos`, el marcador del volumen, el límite en bytes y las huellas de los tres módulos auxiliares. En el filesystem local de Synology, el directorio se publica con `RENAME_NOREPLACE` y se cotejan las ACL por descriptor. [Semántica de `renameat2`](https://www.man7.org/linux/man-pages/man2/renameat2.2.html)

El perfil de DSM V5 guarda `filesystem_id`, el `f_fsid` observado en el volumen ext4 acreditado. Tras un reinicio, el servicio toma el `st_dev` actual y comprueba el `f_fsid` por descriptor antes de preparar el proyecto. Conserva los controles de inodos, marcador, propietario y ACL. Los perfiles previos sin `filesystem_id` siguen exigiendo el `device` registrado. [Diagnóstico tras reinicios](../../docs/ci/runbooks/rb-04-r2-nas-sync-backlog.md#dsm-rechaza-el-aprovisionamiento-después-de-reiniciar-el-host)

El NAS firma el recibo de huella e inode. El consumidor lo verifica y coteja el mismo inode por NFS antes de descargar de R2 y durante la publicación. La reserva completada sigue devolviendo su recibo previo. Si se interrumpe el procesamiento, una orden válida puede reconciliar su carpeta por el marcador y el mismo inode. El servicio conserva los stages ajenos o sin marcador para inspección.

## HTTP y saturación

Sólo se admite HTTP/1.1 con una petición por conexión y `Connection: close`.
`POST /v1/sync` exige `Host`, `Content-Length`, `timestamp` y `signature`; las
tres últimas cabeceras son únicas sin distinción de mayúsculas. Rechaza
`Transfer-Encoding`, `Expect`, longitud ambigua, cabeceras plegadas, caracteres
de control, cuerpos mayores de 4096 bytes, rutas con query, métodos distintos y
peticiones concatenadas que ya estén disponibles. No admite redirecciones ni
elige endpoint, bucket o archivo desde la petición. La firma mantiene el
[contrato v1](README-protocol.md) y la tolerancia de 300 segundos, incluidos sus
límites exactos.

Hay como máximo ocho conexiones atendidas y un worker activo. La lectura de
cabeceras y cuerpo comparte un plazo absoluto de 10 segundos y hasta 8192 bytes
de cabeceras/64 líneas. Al vencer, un temporizador cierra únicamente ese socket.
Al validar la petición se cancela ese temporizador. La respuesta tiene un plazo
de envío de tres segundos. El primer POST aceptado devuelve `202` y
`{"status":"processing"}`. El worker sigue copiando tras el cierre de la conexión.
Un POST firmado con la misma huella sondea el trabajo y devuelve `200` al obtener
el recibo. Una huella distinta para el mismo pedido devuelve `409`.

Los errores son JSON con códigos fijos, sin HTML ni valores recibidos:
`400 BAD_REQUEST`, `401 UNAUTHORIZED`, `404 NOT_FOUND`,
`405 METHOD_NOT_ALLOWED`, `409 CONFLICT`, `413 TOO_LARGE`, `503 BUSY` o
`SYNC_FAILED`, y `504 TIMEOUT`. Saturación devuelve `Retry-After: 1`.
No se registran cabeceras, cuerpo, URL firmada, claves ni stderr externo.

`GET /healthz` devuelve exclusivamente `ready` y `busy`. Un worker local activo
o `Ledger.check()` con `LOCK_TIMEOUT` devuelve HTTP 200 y
`{"ready":false,"busy":true}`. El estado válido devuelve 200 con
`{"ready":true,"busy":false}`; un error de estado devuelve 503 con ambos
valores `false`. La sonda no espera un flock ni reserva trabajos. El supervisor
posterior debe distinguir ocupado de averiado y evitar reinicios por ocupación.

## Worker, publicación y recuperación

El padre valida la petición antes de reservar su slot. Envía como máximo 8192
bytes por stdin: cuerpo Base64, `timestamp`, `signature` y snapshot interno.
El hijo vuelve a validar el esquema, el snapshot y el protocolo. Con el flock
global del ledger mantenido ejecuta `reserve → download → publish → complete`;
un `pending` anterior exige `reconcile` antes de completar. `completed` devuelve
el recibo persistido sin descargar ni escribir de nuevo. La respuesta pública
cerrada contiene sólo `status`, `job_id`, `asset_id`, `relative_path`, `bytes` y
`sha256`; no expone dispositivo/inodo, flags internos ni rutas absolutas.

El iterador de descarga se encierra en `contextlib.closing`, incluso si falla
la publicación. Se conservan el origen fijo R2 y los límites de los helpers:
descarga de hasta 280 segundos, inactividad de 15 segundos, tamaño/SHA exactos,
`NoFollow`, archivo nuevo y publicación sin sobrescribir. Los directorios de
proyecto deben existir; el consumidor no los adopta o crea por nombre.

El proceso se inicia sin shell ni `preexec_fn`, con entorno nuevo que sólo fija
`LANG`/`LC_ALL` a `C.UTF-8`. No hereda tokens, proxies, `PATH`, `PYTHONPATH` ni
otros secretos. El supervisor conserva un
[PIDFD del hijo propio](https://docs.python.org/3.12/library/os.html#os.pidfd_open)
y lo usa con
[`pidfd_send_signal`](https://docs.python.org/3.12/library/signal.html#signal.pidfd_send_signal):
plazo de trabajo 300 segundos, salida combinada stdout/stderr de hasta 8 KiB,
lectura y escritura sin bloqueo. Una cancelación,
timeout o exceso de salida envía SIGTERM, espera dos segundos y después SIGKILL
con hasta dos segundos de espera adicional. No usa señales por número de PID,
nombre de proceso ni grupo. Si el hijo sigue vivo o no se pudo obtener un
PIDFD, retiene su referencia y el slot: no libera trabajo concurrente a ciegas.

Dentro del hijo se reducen los
[límites de recursos](https://docs.python.org/3.12/library/resource.html):
memoria virtual 512 MiB, 64 descriptores, 240 segundos de CPU, core dumps
deshabilitados, archivo hasta el mayor de la política del objeto/128 MiB y
`RLIMIT_NPROC=0`. Nunca aumenta límites existentes. `RLIMIT_NPROC` no constituye
una barrera para root: el alcance es este worker fijo y los cuatro helpers
auditados, que no lanzan descendientes; no es un ejecutor genérico ni una caja
de aislamiento. Presupuestos de contenedor y política de red se añaden después.

SIGTERM en el worker desenrolla sus contextos y limpia su parcial conocido.
SIGTERM del servidor cancela el worker y cierra sus conexiones propias. SIGKILL
puede dejar `pending` y un parcial huérfano: el reintento reconcilia el destino
publicado si corresponde, pero la revisión de huérfanos sigue siendo manual.
No se borran archivos por patrones ni se adopta un archivo ajeno.

## Comprobación de esta unidad

```sh
python -B -m unittest discover -s tests -p test_r2_sync_runtime.py -v
python -m ruff check docker/r2-sync-consumer/sync_runtime.py docker/r2-sync-consumer/sync_http.py tests/test_r2_sync_runtime.py
python -m ruff format --check docker/r2-sync-consumer/sync_runtime.py docker/r2-sync-consumer/sync_http.py tests/test_r2_sync_runtime.py
```

Las pruebas portables usan sockets conectados controlados, sin listener ni R2.
Cubren autenticación, framing, límites, estado ocupado y cierre del iterador.
Las pruebas nativas requieren Linux con PIDFD y almacenamiento local: comprueban
permisos/enlaces, snapshot, SQLite/publicación/replay, recuperación de `pending`,
worker CLI aislado real, slot único, timeout/escalada, salida acotada y
preservación de un proceso ajeno. Windows las omite de forma explícita y no
acredita sus garantías POSIX. Esta entrega todavía no acredita un servicio
Linux desplegado ni transferencia HTTP real de extremo a extremo.
