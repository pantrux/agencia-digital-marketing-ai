# Immich persistente en la LAN

`compose.lan.yml` define el proyecto `agency-media-immich-lan` en `vm-media-01` con cuatro imágenes locales fijadas. El ensayo de Wolfi V3 verificó Immich 3.3.6, login, búsqueda semántica y revocación de sesión. El nuevo proyecto usa bases, colas, archivos y redes propios. La aceptación del nuevo despliegue, del cierre y de los medios externos requiere recibos nuevos. [Ensayo Wolfi](../../.tools/evidence/phase4-immich-wolfi-acceptance-run-20261113-v2.json)

## Rutas e identidades

| Entrada | Contrato |
| --- | --- |
| PostgreSQL | `/var/lib/media-platform/immich-lan-v1/postgres`, NVMe, `70:70`, `0700`. PostgreSQL `16.16`, `vector 0.8.7`, `vchord 1.1.1`, `cube 1.5` y `earthdistance 1.2`. |
| Valkey | `/var/lib/media-platform/immich-lan-v1/valkey`, NVMe, `1000:1000`, `0700`. AOF con `appendfsync everysec`, límite `96mb` y política `noeviction`. |
| `/data` de Immich | Por defecto `/var/lib/media-platform/immich-lan-v1/data`, `1000:1000`, `0700`, vacío para inicializar. Aquí se escriben cargas, miniaturas, transcodificaciones y marcadores. |
| Biblioteca externa | Un subárbol explícito de `/mnt/producciones/_biblioteca_global`, montado como `/external/producciones` de sólo lectura. Usar para el ensayo una carpeta propia vacía o con medios públicos de control. |
| Configuración | Copia cotejada en `/opt/media-platform/immich-lan-v1/immich.lan.json`, `root:root 0644` o `0444` dentro de la raíz `0700`, montada de sólo lectura. |

Verifica con `findmnt -T` el origen `192.168.0.8:/volume2/producciones`, destino `/mnt/producciones`, NFS4.1 y TCP. Registra dispositivo, inodo y ancestros sin enlaces para cada bind. Verifica acceso de lectura desde el contenedor como UID `1000`. Confirma que `/external/producciones` es RO con la inspección de mounts y un intento de escritura sin contenido en la carpeta de control. Examina `df -B1` y `df -i` antes del alta. Con poco espacio local, limita el primer ensayo a una fixture pequeña y comprueba el crecimiento de `/data` y de PostgreSQL durante la prueba. [Export Synology](../../docs/ci/evidence/2026-11-05-fase-2.md)

Para usar espacio NAS en `/data`, prepara un subárbol propio distinto y vacío, por ejemplo `/mnt/producciones/_apps/immich-lan-v1/data`. La exportación `all_admin` mapea los clientes a `1024:100`. Antes de elegirla, comprueba en una carpeta de control separada que UID `1000` puede crear un directorio y un archivo exclusivos, sincronizar los datos, renombrar dentro del directorio y volver a leer el contenido. Comprueba el propietario efectivo y los permisos desde el contenedor. Ajusta `MEDIA_IMMICH_LAN_DATA_PATH` únicamente tras esa verificación. Conserva PGDATA y AOF en NVMe. Registrar una biblioteca externa permite catalogar las rutas originales sin duplicarlas en `/data`; sus derivados sí ocupan espacio.

## Configuración privada

Usa una ruta de control root `0700` bajo `/opt/media-platform`. Pasa únicamente referencias a los archivos previamente acreditados mediante el entorno privado del operador. Comprueba tipo regular, un enlace, propietario del consumidor y modo `0400` o `0600`. `.pgpass` requiere `0600` y credencial válida para `127.0.0.1:5432:immich:postgres`. En Compose local los archivos se montan por bind y conservan la propiedad del host.

| Variable | Consumidor |
| --- | --- |
| `MEDIA_IMMICH_LAN_CONFIG_FILE` | Configuración `immich.lan.json`, UID `1000` |
| `MEDIA_IMMICH_LAN_LIBRARY_PATH` | Ruta acreditada de la biblioteca externa |
| `MEDIA_IMMICH_LAN_DATA_PATH` | Ruta de `/data` cuando se usa una ubicación distinta del valor por defecto |
| `MEDIA_IMMICH_LAN_CPU_CORES` | Valor opcional de `CPU_CORES`; por defecto `2` |
| `MEDIA_IMMICH_LAN_PG_PASSWORD_FILE` | Contraseña administrativa de PostgreSQL, UID `70` |
| `MEDIA_IMMICH_LAN_PGPASS_FILE` | `.pgpass` administrativo, UID `70` |
| `MEDIA_IMMICH_LAN_DB_PASSWORD_FILE` | Contraseña exclusiva del rol `immich`, UID `1000` |
| `MEDIA_IMMICH_LAN_VALKEY_PASSWORD_FILE` | Contraseña del usuario `immich` de Valkey, UID `1000` |
| `MEDIA_IMMICH_LAN_VALKEY_ACL_FILE` | ACL de la instancia de Valkey, UID `1000` |
| `MEDIA_IMMICH_LAN_GPU_BEARER_FILE` | Bearer vigente del puente GPU, UID `65532` |

La ACL de Valkey usa `user default reset off` y `user immich on #HASH ~* &* +@all`, con SHA-256 de la contraseña sin el salto final. Comprueba `NOAUTH` sin secreto y `PONG` mediante el healthcheck. El AOF contiene las colas de Immich y requiere su propio espacio y control de permisos. [Contrato Valkey del ensayo](README-functional.md#identidades-y-preparación)

## Red y cierre

Prepara las cuatro redes externas antes de crear. Fija los IDs, subredes sin solapamiento y el nombre del bridge real en el recibo. Las cuatro redes son IPv4. La configuración no publica los puertos de PostgreSQL, Valkey ni el proxy.

| Red | Tipo y miembros |
| --- | --- |
| `agency-immich-lan-data-v1` | `172.31.241.0/28`. Bridge interno para servidor, PostgreSQL y Valkey |
| `agency-immich-lan-ml-v1` | `172.31.241.16/28`. Bridge interno para servidor y proxy ML |
| `agency-immich-lan-ml-egress-v1` | `172.31.241.32/28`. Bridge `br-immich-eg` para el proxy. Salida por `media0` sólo a `192.168.0.6:3003/TCP` |
| `agency-immich-lan-ingress-v1` | `172.31.241.48/28`. Bridge `br-immich-lan` sólo para el servidor. Publicación `192.168.0.85:2283/TCP` |

Antes de arrancar, instala y acredita el filtro del host con las identidades reales. Para LAN, exige origen `192.168.0.0/24` por `media0`, destino original conntrack `192.168.0.85:2283/TCP` y bridge de salida `br-immich-lan`. Rechaza el resto de conexiones a ese puerto y el acceso directo hacia el bridge. Permite las respuestas de conexiones establecidas, y bloquea las conexiones iniciadas desde el bridge LAN y el acceso al host desde él. Para ML, replica las nueve reglas IPv4/IPv6 del ensayo sobre `br-immich-eg`, incluyendo el retorno establecido, la prohibición del resto de egress/ingress y las reglas de `INPUT`. Añade bloqueos IPv6 acotados al bridge LAN. Verifica si la publicación usa DNAT, `docker-proxy` o ambos y cubre `FORWARD` e `INPUT` donde corresponda. Usa reglas específicas del proyecto con prioridad comprobada respecto de las reglas existentes. Conserva un snapshot anterior y un rollback de las reglas propias. [Reglas ML del ensayo](trial_operator.py)

El Compose fija `restart: "no"` para que el daemon espere a que se comprueben el montaje y el filtro. `lan_guard.py` verifica el prefijo de `FORWARD`, conserva los bloques de observabilidad, MoneyPrinter, SupoClip y n8n, e instala sus reglas después de esos bloques en `INPUT` y `DOCKER-USER`. Usa transacciones `--noflush` y coteja las reglas y políticas IPv4 e IPv6 antes y después. La unidad `agency-immich-lan.service` espera Docker, `network-online.target`, `/mnt/producciones` y el orden de las unidades vecinas. Arranca el proyecto con `up --no-recreate --no-build --pull never` y coteja CIDs y salud. Antes de dar por completado el arranque, exige además el mensaje de disponibilidad de microservicios en los logs de ese inicio y vuelve a cotejar `StartedAt`.

Si el servidor está detenido, la guarda consulta la salud del puente ML con el Bearer privado de Compose. Exige HTTP 200, `ready=true` y `cuda_available=true` del servicio `immich-ml` antes de modificar el filtro o iniciar contenedores. Cuando el puente aún no está disponible, devuelve `GPU_ML_NO_DISPONIBLE` y systemd reintenta en 45 segundos. Consulta los intentos con `journalctl -u agency-immich-lan.service`.

En IPv4, la guarda preserva la regla del receptor privado de n8n si está al inicio de `INPUT`. Exige que coincidan por completo el bridge, las dos direcciones, el puerto 3160, la etiqueta y la acción. También comprueba que exista una sola copia antes de validar el bloque de observabilidad.

En `/opt/media-platform/immich-lan-v1`, guarda `compose.lan.yml` como `compose.yml`, el JSON de Immich como `immich.lan.json` y el operador como `lan_guard.py`. Usa `compose.env` con referencias a archivos de secretos. Escribe `guard-plan.json` como `root:root 0600`, con estas entradas exactas:

| Campo | Valor |
| --- | --- |
| `networks` | Objeto con `data`, `ml`, `ml_egress` y `lan`. Cada valor es un ID Docker completo de 64 hexadecimales. |
| `containers` | Objeto con `postgres-immich`, `immich-valkey`, `immich-ml-auth` e `immich-server`. Cada valor es un CID completo de 64 hexadecimales. |
| `library_path`, `data_path` | Rutas absolutas efectivas de los binds. |
| `compose_sha256`, `compose_env_sha256`, `config_sha256` | SHA-256 de `compose.yml`, `compose.env` e `immich.lan.json`. |

Después de `create`, instala el filtro con `/usr/bin/python3 -I -S -B /opt/media-platform/immich-lan-v1/lan_guard.py apply` y guarda el recibo. `check` valida el filtro vigente. Tras preparar PostgreSQL, instala la unidad y usa `systemctl start agency-immich-lan.service`. Prueba `systemctl stop agency-immich-lan.service` y una nueva activación con los mismos CIDs antes de habilitarla al arranque. La parada conserva el filtro. Para retirar sólo estas reglas, usa la acción `remove` mientras los cuatro CIDs siguen presentes y detenidos.

La parada del proxy usa `init: true`, `SIGINT` y 135 s. Su plazo de transferencia es de 120 s. El ensayo anterior terminó el proxy con 137 sin OOM, mientras que una parada posterior con `SIGINT` terminó en 0. Verifica la nueva parada con `docker compose stop`, sin `--timeout` más corto, y confirma que el proxy termina en 0 con `OOMKilled=false`. El servidor se detiene antes de sus dependencias. [Parada anterior](../../.tools/evidence/phase4-immich-proxy-graceful-stop-v1.json)

## Alta y aceptación

1. Coteja las cuatro imágenes locales, la configuración, la capacidad, la red y los archivos privados. Ejecuta `docker compose --project-name agency-media-immich-lan --env-file compose.env -f compose.yml config --quiet` desde la raíz de control. Crea los servicios con `create --pull never`, verifica los CIDs, montajes, permisos, IPs y límites, y aplica la guarda antes de arrancar.
2. Arranca sólo PostgreSQL y espera salud autenticada. Para una base nueva, el administrador instala en `immich` `vector VERSION '0.8.7'`, `vchord VERSION '1.1.1'`, `cube VERSION '1.5'` y `earthdistance VERSION '1.2'`, en ese orden. Crea `immich` como rol de aplicación `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, configura su contraseña por un canal privado y transfiere la propiedad de la base. Verifica autenticación TCP, checksums, precarga y `CREATE` sobre `public` antes de iniciar el servidor. Conserva las extensiones de propietario administrativo.
3. Con el filtro acreditado, arranca la unidad. Comprueba migraciones, `/api/server/ping`, versión `3.3.6`, acceso desde un cliente LAN y denegación fuera del ámbito. Usa una cuenta administrativa privada y verifica autenticación, sesión y búsqueda `smart` sintética. Correlaciona `POST /predict` del proxy con salud CUDA. Crea y respalda por canal privado la futura clave MCP limitada a los permisos necesarios.
4. Registra `/external/producciones` como biblioteca externa con sólo los medios autorizados para aceptación. `scan` y `watch` automáticos quedan desactivados. Ejecuta primero un escaneo manual acotado y verifica originales intactos, acceso al catálogo y crecimiento de derivados. El modelo facial fijado es `agency_yunet_auraface_v1`. Su activación queda pendiente de calibración con medios públicos de control, validación de umbrales y regeneración de embeddings si se importan datos de un sistema anterior. OCR permanece desactivado. [ADR-003](../../docs/decisions/ADR-003-modelos-faciales-comerciales.md)
5. Comprueba cierre y reactivación, incluidos el AOF, los archivos de `/data` y el estado PostgreSQL. Captura la base definitiva con el proyecto `agency-media-immich-lan`, servicio `postgres-immich`, label `agencia-digital-marketing-ai:postgres-immich-lan:v1` y el CID acreditado. Conserva el recibo y verifica la restauración aislada. [Flujo 3-2-1](../../sre/README-backup-321-orchestrator.md)

## Corte PostgreSQL y reversión

El corte a PostgreSQL 16.16 mantuvo el bind canónico y los otros tres CID. El recibo del 14 de noviembre de 2026 UTC acredita los cuatro contenedores saludables, la prueba de API y la captura lógica con el CID nuevo. El capturador diario valida imagen, versión, checksums y las cuatro extensiones para `production`. [Recibo de promoción](../../.tools/evidence/phase4-immich-1616-promotion-20261113-v1.json)

La copia fría de PostgreSQL 16.15 y `vector 0.8.5` quedó en `/var/lib/media-platform/immich-lan-v1/postgres-1615-rollback-v1`. Para volver a ese punto, detén escritores, identifica los cambios posteriores al corte que deban conservarse y usa la copia fría con la imagen y los archivos de control anteriores al corte. Si se recrea PostgreSQL, acredita el nuevo CID en el plan de la guarda y el perfil de respaldo antes de arrancar. [Ensayo histórico de PostgreSQL y captura](../postgres-immich/README-backup.md)
