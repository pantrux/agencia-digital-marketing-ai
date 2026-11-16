# Aceptación SQL aislada de PostgreSQL Immich

Esta unidad prepara una prueba real sobre datos sintéticos en NVMe de `vm-media-01` (`192.168.0.85`). Sus fuentes y pruebas locales no acreditan todavía la ejecución de SQL, ni autorizan usar la imagen candidata mientras queden hallazgos de seguridad no resueltos. No hay un perfil definitivo generado para la candidata inicial.

La imagen se obtiene del recibo de aprobación de raíz, con manifiesto y configuración dinámicos. El controlador no contiene el identificador de una candidata concreta. El perfil histórico de compilación y sus fallos permanecen como evidencia; no se reconstruyen para añadir SQL ni se presentan como un recibo de compilación positivo.

## Entradas que raíz debe respaldar antes de preparar

El CLI lee la bóveda con el parser canónico fijado y un límite propio de 8 MiB. Reutiliza el transporte SSH de Paramiko 5.1.0, con identidad y clave de host ya fijadas; no prueba otras claves ni acepta un pin nuevo. Importar los módulos o pedir `plan` sin `--output` no lee la bóveda ni abre la red.

| Campo | Contrato |
| --- | --- |
| `MEDIA_POSTGRES_IMMICH_FINAL_SCA_RECEIPT_PATH` y `..._SHA256` | JSON privado de aprobación final, por ruta absoluta bajo `.tools/evidence` y SHA de sus bytes |
| `MEDIA_POSTGRES_IMMICH_FINAL_IMAGE_ID` | Manifiesto local aprobado, `sha256:` y 64 hexadecimales |
| `MEDIA_POSTGRES_IMMICH_SQL_ROOT` | `RECORD/sql-acceptance-v1`, donde `RECORD` es el directorio runtime propio ya preparado |
| `MEDIA_POSTGRES_IMMICH_SQL_DATA_PATH` | `SQL_ROOT/data` |
| `MEDIA_POSTGRES_IMMICH_SQL_SECRETS_PATH` | `SQL_ROOT/secrets` |
| `MEDIA_POSTGRES_IMMICH_SQL_OWNER` | `agencia-digital-marketing-ai:postgres-immich-sql-acceptance:v1` |
| `MEDIA_POSTGRES_IMMICH_SQL_CONTAINER_NAME` | Nombre propio `agencia-pg-immich-sql-` seguido de un sufijo revisado |
| `MEDIA_POSTGRES_IMMICH_SQL_PASSWORD` | Contraseña generada y persistida por raíz antes de usarla: 32 a 256 caracteres URL seguros, sin `:` ni salto de línea |
| `MEDIA_POSTGRES_IMMICH_SQL_PROFILE_B64` y `..._SHA256` | Perfil canónico revisado, generado sólo después de la aprobación SCA |
| `MEDIA_POSTGRES_IMMICH_SQL_LOCAL_SHA256`, `..._REMOTE_SHA256`, `..._SQL_SHA256` | Las tres fuentes ejecutables exactas |
| `MEDIA_POSTGRES_IMMICH_SQL_RECEIPTS_B64` | Mapa canónico de recibos por etapa; el CLI de raíz añade cada resultado sin sobrescribir una etapa |

Se reutilizan el plan SDK V4 y los datos de SSH/layout ya respaldados. El plan SDK sólo proporciona comprobaciones del host y el cliente Docker privado; esta unidad no inicia ni ejecuta el SDK. El padre de SQL debe coincidir en ruta, marcador, dispositivo e inodo con el recibo runtime `PREPARED_B64`.

## Contrato de aprobación de seguridad

La aprobación de raíz tiene `schema_version: 1`, `decision: "approved_for_isolated_sql"`, `coverage_complete: true`, `unresolved_high_critical: 0` entero y `unresolved_advisories: []`. Estos conteos son posteriores a clasificaciones específicas documentadas. No se exige que los reportes brutos carezcan de hallazgos: se conservan, por ejemplo, los avisos de mantenimiento o un hallazgo del CLI SQLite ausente del runtime junto con su evidencia de ausencia.

`image` contiene `id`, `config_digest`, `user: "70:70"`, `entrypoint: ["docker-entrypoint.sh"]`, `cmd: ["postgres"]`, `env` público de la imagen y sus `labels`. `finalization` contiene `success: true`, los mismos `image_id` y `config_digest`, `receipt_sha256` y `vault_field`, restringido a la familia `MEDIA_POSTGRES_IMMICH_*_RECEIPT_B64`. El CLI verifica el recibo físico duradero de ese campo; no basta declarar una imagen en el JSON.

`reports` contiene exactamente cuatro objetos, uno por `kind`: `os`, `cargo`, `sqlite` y `mimalloc`. Cada objeto incluye `path` absoluto privado, `sha256` de sus bytes y `components` entero mayor que cero. El CLI relee los cuatro archivos JSON y contrasta sus hashes antes de cada etapa que pueda crear, iniciar o consultar. El informe de evaluación debe vincular las clasificaciones a los binarios, fuentes e imagen acreditados; el CLI no sustituye esa evaluación con un escáner inventado.

La aprobación incluye `sqlite_version: "3.54.1"`, `db_updated_at_utc` y `expires_at_utc` con zona UTC. Se exige que el momento actual esté dentro de esa ventana y que dure como máximo 48 horas. Si cambia la imagen o una clasificación, raíz emite otra aprobación revisada y otro perfil antes de usarla. Este esquema queda sujeto al cotejo final con el recibo real que raíz produzca tras las correcciones.

## Recursos y secretos de la prueba

`prepare` crea exclusivamente el subdirectorio nuevo bajo el record propio, con creación exclusiva y sin adoptar enlaces o contenido previo. El control es UID/GID 0 y modo 0700. `data` y `secrets` son UID/GID 70 y modo 0700; los archivos `db-password`, `pgpass` y `acceptance.sql` son UID/GID 70, modo 0400 y enlace único. El directorio de datos no se recorre para imponer permisos después de `initdb`; sólo se comprueban su identidad y modo.

La contraseña se envía exclusivamente dentro de la petición SSH por stdin. No forma parte de argv, de `Docker Config.Env`, del perfil ni del informe. La imagen oficial usa `POSTGRES_PASSWORD_FILE`; su entrypoint puede exportarla internamente durante la inicialización. No se lee ese entorno de proceso. El cliente de prueba usa `PGPASSFILE` y nunca pasa una contraseña a `psql` por argumentos.

El contenedor ejecuta como `70:70`, con rootfs de sólo lectura, `network=none`, sin puertos publicados, sin capacidades añadidas, `cap-drop=ALL` y `no-new-privileges`. Usa exactamente dos binds propios: PGDATA de lectura/escritura y secretos de sólo lectura. El bind de PGDATA cubre el `VOLUME` heredado para evitar un volumen anónimo. `/tmp` es tmpfs de 64 MiB y `/var/run/postgresql` tmpfs de 8 MiB; ambos son `nosuid,nodev,noexec`. El límite es 2 CPU, 2 GiB RAM, swap adicional cero, 128 procesos y `/dev/shm` de 128 MiB. El controlador acredita también los límites reales del cgroup, UID 70, capacidades efectivas cero y la presencia de `vchord.so` y `libsqlite3.so.0` en los mapas del postmaster.

## Secuencia explícita de raíz

Primero se puede consultar el plan puro:

```powershell
python docker/postgres-immich/sql_acceptance.py plan
```

Después de corregir y aprobar la seguridad, respaldar rutas/contraseña y revisar las fuentes, raíz genera el perfil en un archivo nuevo de la raíz privada existente:

```powershell
python docker/postgres-immich/sql_acceptance.py plan --output .tools/postgres-immich-build/sql-acceptance-v1-profile.json
```

Raíz respalda el perfil y sus tres fuentes antes de ejecutar una sola etapa. Para cada etapa se usa el mismo perfil canónico y su SHA externo revisado:

```powershell
python docker/postgres-immich/sql_acceptance.py prepare --profile .tools/postgres-immich-build/sql-acceptance-v1-profile.json --expected-profile-sha256 $shaRevisado --root-update-vault
```

La secuencia es `prepare`, `create`, `start1`, `verify1`, `stop1`, `start2`, `verify2`, `stop2`. Cada invocación termina y respalda su recibo antes de la siguiente. `create` devuelve el CID completo sin iniciarlo; `start1` rechaza un CID que no esté respaldado o que ya se haya iniciado. El segundo arranque exige además los recibos positivos de la primera verificación y su parada. Ninguna etapa hace replay automático.

| Etapa | Presupuesto total | Resultado esperado |
| --- | --- | --- |
| `prepare` | 30 s | Directorios y tres archivos propios, sin contenedor |
| `create` | 15 s | CID creado, nunca iniciado, PID 0 |
| `start1` / `start2` | 90 s | SCRAM negativo sin contraseña, positivo con pgpass, versión/checksums/cluster/límites |
| `verify1` | 180 s | Crear datos sintéticos, índice y oráculo; ejecutar consultas reales |
| `verify2` | 60 s | Repetir consultas sin recrear tablas y acreditar persistencia |
| `stop1` / `stop2` | 20 s | Detener únicamente el CID respaldado, PID 0; registrar si fue necesario `kill` |

El watchdog reserva 10 segundos dentro del presupuesto para limpieza. La captura de cada comando se limita a 4 MiB sin truncar silenciosamente. Las consultas largas llevan latido; la pérdida del cliente o el plazo agotado termina el grupo propio del CLI y detiene el CID acreditado, porque terminar `docker exec` por sí solo no garantiza detener su hijo dentro del contenedor.

La parada no requiere contraseña, aprobación SCA vigente ni lectura de la base o de los archivos de secretos. Conserva las comprobaciones del host, perfil, marcadores y configuración exacta del CID. No borra el contenedor ni PGDATA. Si el respaldo local de un resultado falla después de arrancar, raíz puede usar `stop1` con los recibos `prepare/create` ya duraderos antes de investigar; no debe repetir la etapa cuyo resultado quedó remoto.

## Criterios SQL y límites de lo demostrado

La primera verificación exige PostgreSQL 16.15, checksums activos, SCRAM, precarga de `vchord.so`, `vector 0.8.5`, `vchord 1.1.1`, `cube` y `earthdistance`. Genera 4096 vectores de 512 dimensiones y guarda un oráculo de búsqueda exacta antes de crear el índice propio. Para cinco consultas exige un plan que use ese índice, diez resultados distintos y ordenados, distancias finitas dentro del umbral del oráculo con tolerancia de 1e-6, y distancia cero para el propio vector. También prueba L2 y una distancia geográfica conocida.

El segundo ciclo comprueba el mismo identificador de cluster, el marcador sintético persistido, las tablas, el índice y las cinco consultas, sin volver a ejecutar los `CREATE`. La prueba usa el protocolo SQL real y no llama al puente GPU ni toca NAS o bases de aplicaciones.

Las opciones `lists=[]` y `rerank_in_table=false` proceden de `tests/vchordrq/filter_rerank_in_index.slt` de VectorChord 1.1.1; la combinación de 512 dimensiones y coseno se debe acreditar con esta prueba real. Las pruebas Python usan proveedores controlados para las fronteras de archivos/transporte/Docker; no presentan mocks como ejecución de PostgreSQL. Un resultado positivo de esta unidad tampoco inicia Immich ni prueba migraciones de su servidor.

Los fallos, logs y recibos quedan preservados sin sobrescritura. Un intento fallido no se reinicia ni se limpia de forma automática; cualquier recuperación necesita revisar los hechos y preparar una unidad acotada con nombres y respaldos nuevos.
