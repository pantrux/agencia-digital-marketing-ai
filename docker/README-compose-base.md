# Base Compose funcional de aplicaciones

Esta unidad prepara PostgreSQL de aplicaciones, Valkey, Zou API y Zou events para `.85`. El verificador de CI conserva el contrato de estos cuatro roles. La [verificación del inventario operativo de la Fase 4](README-phase4-runtime.md) comprueba los proyectos independientes desplegados y sus supervisores.

El archivo [docker-compose.yml](docker-compose.yml) sólo utiliza imágenes locales por referencias obligatorias y `pull_policy: never`. No contiene `build`, descargas, bootstrap de Zou, migraciones, creación del administrador ni contraseñas. No debe ejecutarse desde Windows. Este incremento no creó redes, archivos de credenciales, directorios de datos ni contenedores, y no ejecutó Compose en `.85`.

## Contrato que Root respalda antes de usar

Todas las variables son obligatorias, sin valores predeterminados. Los nombres y rutas siguientes son entradas propuestas para la bóveda, cuya escritura corresponde a Root; este archivo no acredita que ya existan.

| Variable | Requisito |
| --- | --- |
| `MEDIA_COMPOSE_PROJECT_NAME` | Namespace propio, nuevo y respaldado; comprobar colisiones y etiquetas Compose antes de adoptar cualquier contenedor. |
| `MEDIA_POSTGRES_APPS_IMAGE_ID` | Manifiesto activo `sha256:ad9f053cedad8b00ea7536eddec5f19c3a8a48f88c496b6066d3251f4e9b81fc`. Cotejar la imagen local y su [aceptación SQL y de seguridad](postgres-apps/README-ALPINE-UPDATE.md). |
| `MEDIA_VALKEY_OCI_CAPTURE_V2_MANIFEST_DIGEST` | Manifiesto local `sha256:de6fbe9626fea9f6f297b6867d2c8cf8f0394ee5ed171613166fc093cb49d0a6`. Su captura de datos no implica aprobación SCA ni funcional de todo este stack. |
| `MEDIA_ZOU_APPLICATION_IMAGE_MANIFEST` | Manifiesto real que devuelva el build de la aplicación, ligado a su configuración y recibo. No hay un ID provisional en Compose. API y events usan los mismos bytes. |
| `MEDIA_PLATFORM_DATA_NETWORK_NAME` | Red externa propia, bridge e **internal**, con ID respaldado antes del uso; sólo PostgreSQL/Valkey/Zou. Root debe comprobar `Internal=true` físicamente: Compose no administra esa red externa. |
| `MEDIA_PLATFORM_APP_NETWORK_NAME` | Segunda red externa propia, bridge y distinta de data, con ID respaldado; sólo Zou API/events en esta base. Reservada para futuros frontend/consumidores LAN. |
| `MEDIA_POSTGRES_APPS_DATA_PATH` | Bind local SSD/NVMe preexistente bajo `/var/lib/media-platform`, dueño `70:70`, modo `0700`, padre administrado; jamás NFS, enlaces, volumen anónimo o datos ajenos. |
| `MEDIA_VALKEY_DATA_PATH` | Bind propio preexistente bajo `/var/lib/media-platform`, `1000:1000/0700`; persistencia y backups se aceptan por separado. |
| `MEDIA_ZOU_STATE_PATH` | Bind propio bajo `/var/lib/media-platform`, `1000:1000/0700`, con `previews/` y `tmp/` preparados por Root; API y events comparten este directorio. |
| `MEDIA_VALKEY_CONFIG_FILE` | Archivo regular privado bajo `/opt/media-platform`, lectura para UID 1000, montado RO. Configuración autenticada: `daemonize no`, `dir /data`, logs a stdout, persistencia elegida por Root y memoria útil inferior al límite de 256MiB dejando margen a buffers/AOF. El `requirepass`/ACL debe coincidir con el secreto respaldado; no pasar valor por argv. |
| `MEDIA_POSTGRES_APPS_ADMIN_USER`, `MEDIA_POSTGRES_APPS_ADMIN_DATABASE` | Identificadores del administrador PostgreSQL y base administrativa. Usuario separado de Zou. La salud autentica como este administrador por TCP local. |
| `MEDIA_ZOU_DATABASE_USER`, `MEDIA_ZOU_DATABASE_NAME` | Rol y base propios de la aplicación, creados explícitamente por Root antes de iniciar Zou. No usar el superusuario administrativo como rol de la aplicación. |
| `MEDIA_ZOU_DOMAIN_NAME`, `MEDIA_ZOU_DOMAIN_PROTOCOL` | Dominio y esquema públicos reales para el launcher fijado; no utilizar el dominio sintético de CI. |

Docker 29 puede identificar una imagen por manifiesto OCI mientras otra herramienta informa el digest de configuración. Root debe conservar ambos y su correlación, sin tratarlos como iguales. Un digest aislado no acredita disponibilidad local, ABI, licencias ni seguridad global.

## Archivos secretos y permisos físicos

Los secretos de Compose proceden únicamente de archivos. Los campos siguientes contienen **rutas**, nunca valores. Usar padres privados administrados bajo `/opt/media-platform`; no montar la bóveda completa, claves de Root ni el socket Docker.

| Variable de ruta | Ruta dentro del contenedor | Dueño y uso |
| --- | --- | --- |
| `MEDIA_POSTGRES_APPS_PASSWORD_FILE` | `/run/secrets/postgres_password` | `70:70/0400`, contraseña del administrador PostgreSQL. |
| `MEDIA_POSTGRES_APPS_PGPASS_FILE` | `/run/secrets/postgres_pgpass` | `70:70/0400`, formato/escapes libpq, mismo usuario/base/contraseña que la salud TCP. Archivo separado, sin imprimirlo. |
| `MEDIA_VALKEY_PASSWORD_FILE` | `/run/secrets/valkey_password` | `1000:1000/0400`, mismo valor que la autenticación configurada en Valkey. |
| `MEDIA_ZOU_DB_PASSWORD_FILE` | `/run/secrets/zou_db_password` | `1000:1000/0400`, contraseña del rol Zou. |
| `MEDIA_ZOU_KV_PASSWORD_FILE` | `/run/secrets/zou_kv_password` | `1000:1000/0400`, autenticación Valkey respaldada. |
| `MEDIA_ZOU_SECRET_KEY_FILE` | `/run/secrets/zou_secret_key` | `1000:1000/0400`, clave persistente de la aplicación. |

Los seis deben ser archivos regulares únicos, sin enlaces, con permisos físicos ya comprobados y lectura para el UID indicado. No se confía en que Compose cambie UID/GID/modo de un secreto basado en archivo. El launcher `launch_zou.py` SHA `6650af089fa8985e40457f6e6eb574615bcd27580ac4b2f6f50d6ab454b9f472` lee las tres rutas Zou fijas y adapta la configuración en memoria; Compose no guarda sus valores en `Config.Env` ni en argv.

Root genera/persiste cualquier credencial antes de escribir sus archivos privados. Los usuarios `70` y `1000` son del contenedor; no cambiar usuarios del host ni ampliar permisos globalmente para hacerlos funcionar. Los bind usan `create_host_path: false`, de modo que una ruta ausente falla sin crear una carpeta silenciosamente.

## Aislamiento, salud y arranque separado

| Servicio | Memoria / CPU / procesos | Acceso |
| --- | --- | --- |
| PostgreSQL apps | 768MiB / 1 / 128 | Red data, sin puertos publicados; datos RW y tmpfs `/tmp` + `/var/run/postgresql`. Memoria compartida 128MiB incluida en el presupuesto. |
| Valkey | 256MiB / 0,5 / 64 | Red data, sin puertos publicados; configuración RO, datos RW. |
| Zou API | 768MiB / 1 / 64 | Ambas redes; único puerto publicado `127.0.0.1:5000:5000`. |
| Zou events | 256MiB / 0,5 / 64 | Ambas redes, puerto 5001 sólo interno, sin publicación. |

Los máximos agregados de estos cuatro procesos son 2GiB y 3 CPU; se reserva margen en el host de 16GB para Docker, filesystem, otros servicios y operación. No son mediciones de consumo ni una garantía de capacidad total del stack. Todos usan raíz RO, capacidades eliminadas, no-new-privileges, swap adicional deshabilitado, tmpfs acotados y logs `local`, `10m`, `max-file=2`. Revisar consumo real antes de ajustar estos límites. El archivo no configura modo privilegiado, host PID/network, dispositivos, rootfs ni socket Docker.

La salud PostgreSQL ejecuta `SELECT 1` mediante `psql -w` y `PGPASSFILE`, no sólo `pg_isready`. Valkey exporta `REDISCLI_AUTH` en el proceso de la sonda, lo lee del archivo con `IFS= read -r` y ejecuta `valkey-cli -e ... ping`; no hay sustitución de comandos ni contraseña en argumentos. `-e` conserva el código de error de comandos fallidos.

La rueda Zou derivada `41ead8c1a92730d43331be9095ae218f677cd17247c7d922568a0b3d988185c0` conserva las rutas públicas originales: `blueprints/index/__init__.py` registra `GET /`, e `IndexResource.get` devuelve `api` y `version` sin JWT ni consulta de base. Events llama primero a `create_app()`, que registra ese blueprint, y `event_stream.py:193–195` declara después otro `GET /`, con `name` terminado en ` Event stream`. Su sonda admite únicamente esas dos formas originales para respetar el orden de las rutas; la API exige `api` y `version`. Las sondas stdlib usan loopback, timeout de 2s y cuerpo máximo de 4096 bytes; no crean sesiones Socket.IO. **No usan `/status`**: su comprobación de base puede llamar a `get_or_create_project_status`, y también consulta workers/indexer aún no integrados.

Estas sondas sólo acreditan disponibilidad parcial de los procesos. Login, authenticated, refresh, logout, rechazo de credenciales y el protocolo Socket.IO se aceptan externamente con las credenciales respaldadas por Root; no se añadió un JWT de salud que caduque. `depends_on: service_healthy` ordena el arranque inicial de Zou respecto de PG/Valkey; una marca `unhealthy` no reinicia automáticamente la aplicación. `restart: unless-stopped` actúa ante salida del proceso, y no sustituye diagnóstico ni observabilidad.

Root debe validar primero los IDs/etiquetas de recursos, redes, padres, montajes y secretos sin publicar su contenido. Después inicia sólo PostgreSQL/Valkey propios, verifica autenticación y persistencia, crea el rol/base de Zou con permisos acotados y ejecuta **por separado** `init-db` y `create-admin` del launcher original. `create-admin` requiere además el archivo privado `/run/secrets/zou_admin_password` y el email respaldado, que no se montan en los servicios permanentes. El entrypoint de PostgreSQL puede inicializar un cluster propio vacío al primer arranque con checksums/SCRAM; no inicializa esquemas Zou. No ejecutar Zou en bucle para intentar suplir ese bootstrap. Sólo tras esos pasos se inicia API/events y se acepta la aplicación. Toda operación real y su parada por identidad corresponden a Root en otra unidad.

## Validación sintética para CI

Con las dependencias Python de desarrollo ya instaladas:

```sh
python scripts/ci/check_compose_base.py
python -m unittest discover -s tests -p test_compose_base.py
```

El primer comando exige Docker Compose y ejecuta realmente `docker compose --project-directory <temporal> --env-file <compose.synthetic.env> -f <docker-compose.yml> config --quiet` con plazo de 30s. Utiliza un entorno nuevo, HOME/DOCKER_CONFIG temporales y `COMPOSE_DISABLE_ENV_FILE=true`; no hereda `MEDIA_*`, credenciales ni `DOCKER_HOST`, ni consulta la bóveda. No inicia contenedores ni requiere el daemon. Los IDs, nombres y rutas de [compose.synthetic.env](compose.synthetic.env) son exclusivamente sintéticos y no se usan para desplegar. Los secretos referenciados son rutas de ejemplo, sin contenido.

`--structure-only` permite verificar únicamente el contrato cerrado y la cobertura de variables sin Docker. Su salida declara explícitamente `compose_config_verified=false` y `runtime_verified=false`. Si no hay Docker/Compose, el comando normal falla; no convierte esa ausencia en una aprobación. En este equipo Windows Docker no está disponible, por lo que la ejecución real de `config --quiet` queda pendiente de Root/CI. No hay workflow modificado en esta unidad: integrar el comando en los siete gates es un incremento posterior, manteniendo fase declarada y CD deshabilitado.

Fuentes primarias: [servicios Compose](https://docs.docker.com/reference/compose-file/services/), [secretos por archivo](https://docs.docker.com/reference/compose-file/secrets/), [interpolación obligatoria y precedencia](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/) y [CLI oficial de Valkey](https://valkey.io/topics/cli/). Los contratos locales de imagen están en [PostgreSQL apps](postgres-apps/README.md) y [contexto de Zou](kitsu-zou/README-application-context.md). Ninguna validación de sintaxis sustituye SCA, backups/restauración, seguridad de red ni aceptación real.
