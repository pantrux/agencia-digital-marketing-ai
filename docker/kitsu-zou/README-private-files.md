# Preparación privada de archivos para la base Kitsu

Esta unidad prepara exclusivamente archivos y directorios nuevos para PostgreSQL apps, Valkey y Zou. No genera ni rota credenciales, no lee `.env`, no conecta por SSH, no ejecuta Docker/SQL, no inicia servicios ni modifica Compose. Root prepara y respalda el plan antes de invocar la operación. Importar los dos módulos no produce efectos.

## Dos namespaces directos y ningún recurso adoptado

| Destino fijo | Propiedad |
| --- | --- |
| `/opt/media-platform/kitsu-base-v1` | Root `0:0/0700`, configuración privada y subdirectorio `secrets` `0:0/0700`. |
| `/var/lib/media-platform/kitsu-base-v1` | Root `0:0/0700`, padre privado de los datos. |
| Datos `postgres-apps` | `70:70/0700`, **completamente vacío** para `initdb`. |
| Datos `valkey` | `1000:1000/0700`, completamente vacío. |
| Datos `zou` y sus hijos `previews`, `tmp` | `1000:1000/0700`. |

Los dos padres existentes se acreditan mediante los bytes del helper `host_layout.py` SHA `3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9` y el recibo positivo físico de Root, cotejando dispositivo/inodo, marcador y recibo. No se crean padres `/config` o `/data`, ni se adopta un directorio por su mera existencia.

La operación abre los padres por los descriptores del helper fijado y comprueba que **ambos** hijos finales están ausentes antes de la primera escritura. `mkdir`/`openat` exclusivos rechazan colisiones, symlinks y replays. Los dos namespaces nuevos reciben `.agency-kitsu-files-owner.json` `0:0/0600`, que liga owner, rol, ruta fija, dispositivo/inodo y SHA del plan. No hay marcadores ni recibos dentro de PGDATA o Valkey. Los hijos de datos se acreditan desde el recibo de la operación, no añadiendo archivos que impedirían `initdb`.

Owner fijo: `agencia-digital-marketing-ai:kitsu-base-files:v1`. No hay chmod, chown o limpieza recursiva. Sólo se cambia el dueño/modo de nodos recién creados por esta operación, mediante sus FD abiertos. Un fallo conserva los parciales y sus identidades disponibles; nunca se repite automáticamente ni se borra para volver a intentar.

## Plan público y entrada secreta separados

[prepare_private_files.py](prepare_private_files.py) ofrece funciones puras `make_plan(identifiers, files_metadata)`, `validate_plan`, `pgpass`, `valkey_config` y `validate_bundle`. Su CLI predeterminada recibe por stdin **sólo el plan público** y devuelve SHA/metadata, sin escribir archivos. El plan tiene campos cerrados:

```text
schema=1, owner, targets={config,data}, identifiers, files,
runtime_verified=false, production_approved=false, secrets_in_plan=false
```

`identifiers` contiene únicamente `postgres_admin_user`, `postgres_admin_database`, `zou_database_user`, `zou_database_name`: identificadores PostgreSQL ASCII en minúsculas de hasta 63 caracteres. El rol Zou debe diferir del administrador PostgreSQL. Cada fila de `files` tiene exactamente `sha256`, `bytes`, `uid`, `gid`, `mode`, `path`; rutas y permisos proceden de la política fija, no de datos entrantes. Los valores secretos no aparecen en el plan. Root calcula/persiste estos hashes y su plan antes de invocar `--prepare`.

| Archivo bajo config | Dueño / modo | Origen y guarda |
| --- | --- | --- |
| `secrets/postgres_password` | `70:70/0400` | Contraseña administrativa PG separada de la aplicación, generada y respaldada por Root antes del uso. |
| `secrets/postgres_pgpass` | `70:70/0400` | Línea exacta `127.0.0.1:5432:ADMIN_DB:ADMIN_USER:PASSWORD` + LF; escapa `:` y `\` según libpq. |
| `secrets/valkey_password` | `1000:1000/0400` | Credencial Valkey respaldada por Root. |
| `secrets/zou_db_password` | `1000:1000/0400` | `KITSU_DB_PASSWORD` existente; no rotarlo. |
| `secrets/zou_kv_password` | `1000:1000/0400` | Bytes idénticos a `valkey_password`. |
| `secrets/zou_secret_key` | `1000:1000/0400` | `ZOU_SECRET_KEY` existente, sin rotación. |
| `secrets/zou_admin_password` | `1000:1000/0400` | Contraseña administrativa Zou existente; sólo se montará en el one-off correspondiente. |
| `valkey.conf` | `1000:1000/0400` | Debe ser legible por el UID 1000 del futuro bind; el padre del host sigue siendo Root/0700. |

Las contraseñas admiten UTF-8 literal de al menos 12 caracteres, máximo 4096 bytes por archivo, con un LF final opcional. Se rechazan control/CR/NUL, líneas extra y reservas pendientes. No se usa evaluación de shell. `pgpass` y `valkey.conf` se recalculan en memoria para cotejar sus bytes completos con los ya aprobados; no se generan credenciales. Las comillas y barras invertidas de `requirepass` se escapan literalmente. No hay sustitución de comandos.

La configuración Valkey cerrada exige `bind 0.0.0.0`, `protected-mode yes`, puerto 6379, `daemonize no`, `logfile ""`, `pidfile /tmp/valkey.pid`, `dir /data`, AOF activado con `appendfsync everysec`, `maxmemory 128mb`, política `noeviction` y `requirepass` entre comillas. El bind sirve a la red bridge prevista; Compose no publica el puerto. Esta unidad no afirma que Valkey ya haya leído la configuración ni que la persistencia esté aceptada.

Root puede reutilizar `KITSU_DB_NAME`, `KITSU_DB_USER`, `KITSU_DB_PASSWORD`, `KITSU_ADMIN_EMAIL`, `KITSU_ADMIN_PASSWORD` y `ZOU_SECRET_KEY` ya respaldados, sin duplicar la bóveda ni crear un JSON de credenciales en disco. El email no se necesita para escribir estos archivos: se usará en el futuro one-off `create-admin`.

## Operación Root explícita

[private_files_remote.py](private_files_remote.py) se entrega por bytes/hash a Root. Su modo predeterminado valida una solicitud de plan `{core_b64,plan}`; no carga el layout, consulta el host ni escribe. El core se compila directamente de sus bytes cuyo SHA está fijado en el remoto, sin importlib ni caché `.pyc`.

Para preparar, Root invoca `--prepare --plan-sha256 SHA_RESPALDADO` y entrega un único documento JSON UTF-8 por stdin, con exactamente:

```text
core_b64, layout_b64, layout_proof, plan, files_b64
```

`files_b64` contiene exactamente los ocho archivos anteriores, codificados en base64; nunca se imprime, guarda como bundle ni pasa por argv/variables de entorno. La entrada total no supera 256KiB; Root debe cerrar stdin tras transmitirla. No pasar `.env` entero. El JSON rechaza claves duplicadas; SHA/bytes, reservas, correlación PGpass, contraseña administrativa separada y coincidencia KV se verifican antes de crear nodos.

La escritura requiere Linux, UID/GID `0:0`, hash de plan externo exacto, helper/layout auténticos y dos targets ausentes. Plazo global 30s, archivos de tamaño cerrado, fsync, relectura por FD, modo/UID/GID/tamaño/hash/nlink y nombres cotejados antes/después. Las escrituras cortas se completan; se abren archivos con `O_EXCL|O_NOFOLLOW`. Root recibe un solo JSON saneado con éxito/error de tipo, identidades de targets/hijos/archivos, SHA de marcadores, plan y contenido; nunca valores secretos. `runtime_verified=false` y `production_approved=false` se conservan en todos los resultados.

Root debe respaldar las identidades y el recibo positivo antes de que Compose monte estos archivos o datos. Para Compose, apuntar las seis variables `*_FILE` a los secretos permanentes y `MEDIA_VALKEY_CONFIG_FILE` a `valkey.conf`; `zou_admin_password` queda fuera de los servicios permanentes. Los binds de datos apuntan a los hijos concretos, no a su padre con marcador. Esto no constituye aprobación SCA, bootstrap de esquema/admin, aceptación de login ni despliegue a producción.

## Comprobaciones

```sh
python -W error::ResourceWarning -m unittest discover -s tests -p test_kitsu_private_files.py
```

Las pruebas portables verifican contratos, alteraciones, escapes, duplicados, modo plan sin escritura, rechazo previo al host, colisiones sin escrituras y escrituras cortas con flags exclusivos. La prueba POSIX de FD/UID/árbol físico requiere Linux Root y se omite honestamente en Windows: valida archivos privados, PGDATA/Valkey vacíos y rechazo de replay en una fixture temporal propia, sin layout/credenciales reales. Su omisión no acredita esa ejecución Linux. Root conserva la autoridad de probarla o verificar los resultados físicos al preparar la fixture real.
