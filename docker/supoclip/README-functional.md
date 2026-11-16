# Ensayo integrado privado de SupoClip

El ensayo usa las imágenes locales de SupoClip CPU, Next, PostgreSQL de
aplicaciones, Valkey y Nginx de Kitsu. El resultado funcional se registra por
separado de la aprobación de producción.

## Entradas del operador

Exportar `MEDIA_SUPOCLIP_TRIAL_{BACKEND,FRONTEND,POSTGRES,VALKEY,NGINX}_IMAGE`
con los digests Linux amd64 físicos que correspondan al intento. Para Next
están los recibos locales `phase4-supoclip-frontend-runtime-build-v2.json` y
`phase4-supoclip-frontend-runtime-trial-exercise-v3.json`. Elegir el backend
con el recibo de build y sonda de medios vigente. El operador fija además
`MEDIA_SUPOCLIP_TRIAL_ROOT`, `MEDIA_SUPOCLIP_TRIAL_PG_ADMIN`,
`MEDIA_SUPOCLIP_TRIAL_PRISMA_SOURCE`, `MEDIA_SUPOCLIP_TRIAL_PRISMA_BIN` y
`MEDIA_SUPOCLIP_TRIAL_SPEACHES_NETWORK`.
`MEDIA_SUPOCLIP_TRIAL_HOST` debe coincidir con el origen HTTP compilado en Next,
`192.168.0.85` para el artefacto descrito en [el build Linux](README-FRONTEND-LINUX.md).
El único puerto publicado es TCP 3080 de ese host. Acreditar antes el acceso
privado desde el operador autorizado y la salida exclusiva del worker al puente
en `192.168.0.6:8000`. La red externa de Speaches debe ser propia del ensayo.

Preparar una raíz de ensayo nueva en NVMe local, con rutas canónicas
sin enlaces y con identidad y permisos respaldados. Ubicar juntos
`compose.functional.yml`, `compose.integration.override.yml`,
`launch-trial.sh` y `nginx.functional.conf` en el directorio de configuración
verificado. Los ejemplos usan Bash en la VM, con `set -euo pipefail` y
`umask 077`.

| Ruta bajo la raíz | Propietario | Contrato |
| --- | --- | --- |
| `postgres/` | 70:70 | 0700, datos propios vacíos al iniciar |
| `valkey/` | 1000:1000 | 0700, AOF propia |
| `state/`, `state/outputs/`, `state/temp/` | 1000:1000 | 0700, compartidos por API y worker |
| `logs/api/`, `logs/worker/` | 1000:1000 | 0700, uno por rol |
| `secrets/postgres-password`, `secrets/postgres-pgpass` | 70:70 | Archivos ordinarios 0400 o 0600. En el pgpass usar entradas para `127.0.0.1:5432`, el admin del ensayo y las bases `postgres` y de aplicación |
| `secrets/database-node`, `secrets/database-url` | 1000:1000 | La misma base y rol dedicado mediante `postgresql://` y `postgresql+asyncpg://`, respectivamente. Host privado `postgres`; credenciales de URL codificadas |
| `secrets/backend-auth`, `secrets/better-auth` | 1000:1000 | HMAC compartido entre Next y Python, y secreto distinto de sesión de Next |
| `secrets/redis-password`, `secrets/redis-acl` | 1000:1000 | Contraseña propia y ACL Valkey con `user default reset on #<sha256> ~* &* +@all`. El cliente ARQ autentica con contraseña sola. El acceso queda limitado a la red de datos y al Valkey exclusivo del ensayo |
| `secrets/speaches` | 1000:1000 | Bearer del puente autorizado, archivo privado con los bytes exactos del token y sin salto de línea final |
| `prisma-capsule/prisma-source/` y `prisma-capsule/prisma-bin/` | 0:0 | Árboles materializados a partir de la cápsula, con directorios 0555, archivos 0444 y `schema-engine` 0555. Montajes de sólo lectura del one-off |

Todos los secretos de UID 1000 son archivos ordinarios de un enlace, 0400
o 0600, de una sola línea. Sólo se montan en el rol que los consume.
El launcher carga los valores dentro del proceso. Mantener privados
los logs del contenedor y los resultados de sesión, subida y cola.

## Materializar Prisma

La cápsula local `.tools/backup-source-build/supoclip-integration-v1/prisma-capsule-v1.tar.gz`
tiene SHA256 `f6191359c1cabadf13562923a07a984bef77575a6b6b2890924402c3a96dc220`.
Contiene el CLI de Prisma 6.19.3 y 41 paquetes del lock original, las 27
migraciones SQL, el motor Linux amd64 y dos bibliotecas OpenSSL 3 de los APK
verificados. `prepare_integration_prisma.py --out <ruta-nueva>` la reconstruye
con los insumos locales, los hashes fijados y Python con PyYAML. La recepción
usa sólo la biblioteca estándar y valida el contenido de cada entrada antes
de escribir. Ejecutar como root en la VM, con `artifact_dir` y `config_dir`
iguales a las rutas físicas verificadas de insumos y configuración.

```bash
python3 -I -B "$config_dir/receive_integration_prisma.py" \
  "$artifact_dir/prisma-capsule-v1.tar.gz" \
  --destino "$MEDIA_SUPOCLIP_TRIAL_ROOT/prisma-capsule"
export MEDIA_SUPOCLIP_TRIAL_PRISMA_SOURCE="$MEDIA_SUPOCLIP_TRIAL_ROOT/prisma-capsule/prisma-source"
export MEDIA_SUPOCLIP_TRIAL_PRISMA_BIN="$MEDIA_SUPOCLIP_TRIAL_ROOT/prisma-capsule/prisma-bin"

compose=(docker compose --profile migration
  -f "$config_dir/compose.functional.yml"
  -f "$config_dir/compose.integration.override.yml")
"${compose[@]}" config --quiet
```

Antes de usar la base, comprobar el motor con el `LD_LIBRARY_PATH` que usará
`migrate`. Ejecutarlo con la imagen Next seleccionada, en aislamiento de red
y con un único bind de sólo lectura.

```bash
docker run --rm --pull=never --platform linux/amd64 --network none \
  --read-only --user 1000:1000 --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --mount "type=bind,src=$MEDIA_SUPOCLIP_TRIAL_PRISMA_BIN,dst=/run/supoclip/prisma-bin,readonly" \
  -e LD_LIBRARY_PATH=/run/supoclip/prisma-bin \
  --entrypoint /run/supoclip/prisma-bin/schema-engine \
  "$MEDIA_SUPOCLIP_TRIAL_FRONTEND_IMAGE" --version
```

## Orden y verificación en Linux

1. Resolver la configuración con entradas del operador. Comprobar ausencia de
   colisiones, digests, redes, montajes y límites. Crear los contenedores
   detenidos con `"${compose[@]}" create` y respaldar los siete CID exactos,
   sus identidades y montajes antes de arrancarlos. Acreditar los comandos
   de Nginx, Next y Python contra sus imágenes. Usar los CID de este intento
   para todo `docker start`, `docker exec` y `docker inspect` posterior.
2. Arrancar PostgreSQL y Valkey. Comprobar SQL autenticado, rechazo sin
   contraseña en Valkey y `PONG` con el archivo correcto. Crear con el admin
   el rol de aplicación y su base exclusiva. El rol es propietario de su
   base y esquema, sin privilegios de superusuario, administración de roles,
   creación de bases ni replicación. Revisar disponibilidad de `uuid-ossp`
   en esa imagen y base. La extensión `uuid-ossp` debe estar disponible
   para Prisma y el backend.
3. Arrancar el CID de `migrate` y exigir salida 0. Verificar en
   `_prisma_migrations` los 27 nombres esperados y ninguna fila con fallo
   pendiente. Arrancar después la API y verificar las 26 migraciones
   backend en `schema_migrations` contra el TAR del intento. Consultar
   `/health/db` y `/health/redis` por loopback del CID API y exigir `200`,
   `status=healthy` y `database=connected` o `redis=connected`.
4. Arrancar el worker y exigir `/health/worker` saludable después de su
   primer latido. Corroborar en sus logs privados la ejecución real de
   `cron:cleanup_media` de arranque en la cola `supoclip_tasks`, con la base
   vacía. Arrancar Next, esperar un sitemap válido y arrancar Nginx.
   Ejecutar `nginx -t -e stderr -c /etc/nginx/supoclip.conf` dentro del CID proxy.
5. Desde el origen autorizado, comprobar `/healthz`, `/sign-in` y un
   estático real de Next. Confirmar `401` en
   `POST /api/upload/authorization` sin sesión y `401` en `POST /upload`
   sin firma. Verificar mediante `docker inspect` que únicamente el proxy
   tiene un puerto publicado. La sonda HTTP da de alta y autentica una cuenta
   sintética, comprueba su cookie con `GET /api/auth/get-session` y usa una
   sesión anónima separada como control. Tras cerrar sesión repite ambos
   controles de sesión y autorización.
6. Con sesión válida, solicitar autorización por `POST /api/upload/authorization`
   y comprobar `directUpload:true` y URL en el mismo origen `:3080/upload`.
   Enviar el MP4 sintético autorizado como multipart `video` con los headers
   HMAC devueltos. Comprobar `video_path` de tipo `upload://` y los bytes
   reales del archivo guardado en `/state/temp/uploads` desde ambos CID.
   Confirmar la fila `completed` correspondiente al usuario sintético en
   `media_upload_sessions`. El ensayo limita el archivo a 64 MiB y permite
   el overhead multipart en el proxy. Las rutas `/api/*` y los estáticos
   pertenecen a Next.
7. Comprobar la transcripción desde el CID worker con
   `probe_integration_media.py`. Llama a `get_video_transcript` de la imagen,
   usando Speaches y una copia propia del MP4 persistido. Correlacionar
   el resultado con la petición real del puente. El JSON de la sonda guarda
   SHA del fixture, duración, número de palabras y tiempos en milisegundos.

## Operador del ensayo privado

Colocar `trial_operator.py`, los cuatro archivos de configuración y el
`migration-manifest.json` verificado en un directorio físico del host Linux.
Usar la cápsula Prisma ya comprobada, una raíz nueva con padre existente y
los cinco IDs físicos de imagen del intento. La creación recibe por stdin
un JSON privado con seis campos `postgres_password`, `database_password`,
`redis_password`, `backend_auth`, `better_auth` y `speaches`. El operador
usa los valores para materializar los archivos 0400 y deriva el DSN, pgpass
y ACL. El JSON viaja por stdin y sólo se conserva donde ya se gestiona.

```bash
python3 -I -B "$config_dir/trial_operator.py" create \
  --root "$trial_root" --config "$config_dir" --prisma "$prisma_dir" \
  --project "$trial_project" --pg-admin "$pg_admin" --app-db "$app_db" \
  --app-role "$app_role" --backend "$backend_id" --frontend "$frontend_id" \
  --postgres "$postgres_id" --valkey "$valkey_id" --proxy "$nginx_id" \
  < "$private_secret_dto"
python3 -I -B "$config_dir/trial_operator.py" verify --root "$trial_root"
python3 -I -B "$config_dir/trial_operator.py" start --root "$trial_root"
```

`create` deja los siete CID detenidos, registra las identidades y activa
reglas etiquetadas sólo para el origen publicado y el bridge Speaches del
ensayo. El worker puede iniciar conexiones TCP hacia `192.168.0.6:8000`
por su bridge IPv4 y usa DNS interno para resolver los servicios Compose.
La API y el resto de los roles usan sus redes Compose. El modo predeterminado
bloquea conexiones nuevas externas a `192.168.0.85:3080`. El argumento
opcional `--lan-source 192.168.0.X/32` admite una sola IP LAN cuando se
autorice una prueba exterior. `start` verifica `/healthz` desde la VM contra
su propia IP publicada. Un `200` ahí permite correr la sonda HTTP en la VM,
con un DTO separado de cuenta sintética, y deja pendiente la prueba desde
otra máquina.

El operador registra la creación, verifica las migraciones exactas y
comprueba la salud de los servicios en orden. Los archivos de evidencia
quedan dentro de la raíz de modo 0700. Tras un fallo o al cerrar el ensayo,
`stop` detiene únicamente los CID etiquetados de este proyecto, elimina
sus reglas exactas y conserva contenedores, redes, datos y archivos para
diagnóstico. Un arranque posterior vuelve a instalar y comprobar las reglas
antes de iniciar servicios.

```bash
python3 -I -B "$config_dir/trial_operator.py" stop --root "$trial_root"
```

## Sondas reproducibles

Con PostgreSQL autenticado mediante su `PGPASSFILE`, comparar el resultado
ordenado de estas consultas con el manifiesto local `migration-manifest.json`
(SHA256 `5be6e0901b3b9534950581be148ede142eebabfd193ce2e7c3000a3c9fe18336`).
Las primeras dos consultas se ejecutan tras Prisma. La tercera se ejecuta
tras arrancar la API. Los archivos del manifiesto están ordenados por nombre.
`pg_cid` es el CID PostgreSQL registrado y `app_db`, el nombre de base del
ensayo. Pasar una consulta SQL por stdin en cada llamada.

```bash
trial_sql() {
  docker exec -i "$pg_cid" /bin/sh -c \
    'exec psql -X -q -A -t -w -h 127.0.0.1 -p 5432 -U "$POSTGRES_USER" -d "$1" --set=ON_ERROR_STOP=1' \
    psql "$app_db"
}
```

```sql
SELECT migration_name || '|' || checksum FROM _prisma_migrations
WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL ORDER BY migration_name;
SELECT count(*) FROM _prisma_migrations WHERE finished_at IS NULL OR rolled_back_at IS NOT NULL;
SELECT version FROM schema_migrations ORDER BY version;
```

Se esperan 27 pares Prisma `nombre|sha256`, cero filas fallidas o revertidas
y los 26 nombres backend. Para comprobar `/health/worker`, consultar por
loopback del CID API y exigir `200`, `status=healthy`, `worker=connected`.

Desde el operador privado, guardar un JSON de una cuenta sintética nueva
en un archivo 0600 con claves `email` y `password`. Usar el patrón
`supoclip-prueba-<id-único>@example.invalid` y una contraseña de al menos 16
caracteres. Usar el fixture `synthetic-speech-v2.mp4` del mismo directorio que
la cápsula, fijado en las dos sondas por SHA256 y tamaño.

```bash
python3 -I -B "$config_dir/probe_integration_http.py" \
  "$artifact_dir/synthetic-speech-v2.mp4" < "$account_dto" > "$http_receipt"
upload_leaf=$(python3 -I -B -c 'import json,sys; print(json.load(sys.stdin)["upload_leaf"])' < "$http_receipt")
docker exec --user 1000:1000 "$api_cid" \
  /usr/bin/sha256sum "/state/temp/uploads/$upload_leaf"
docker exec -i --user 1000:1000 "$worker_cid" /usr/bin/python3.12 -I -S -B - "$upload_leaf" \
  < "$config_dir/probe_integration_media.py" > "$media_receipt"
```

El SHA leído desde ambos CID debe ser
`499d9bde244c4f294e103634da2cb42e5c60090257eab755d68af916483e5f00`.
Verificar que una sola fila de `media_upload_sessions` con `video_path`
igual a `upload://` más el nombre de la sonda tenga `state='completed'`,
`size=89894`, `content_sha256` igual al del fixture y `user_id` igual al `id`
del email sintético en `users`. El worker exige 28 palabras en español con
tiempos válidos y confirma que retiró su copia temporal.

Para acreditar clips generados desde la UI, hace falta otra prueba con una
tarea real, un analizador LLM autorizado y clips reproducibles. La red del
ensayo limita la salida del worker a Speaches. Para verificar persistencia,
parar y reiniciar los mismos CID del ensayo y volver a comprobar por SQL el
usuario y la carga, además de una clave sintética de Valkey. Usar solamente
fixtures sintéticos en el origen HTTP privado.

### Generación de un clip desde la ruta de la interfaz

Para conservar el modelo entre ensayos, usar el
[overlay persistente de llama.cpp](README-LOCAL-MODEL.md).
Configurar en el worker de un ensayo privado `LLM=ollama:<modelo>` y
`OLLAMA_BASE_URL=http://ollama:11434/v1`. Usar un modelo previamente
instalado en una red Docker interna compartida con el worker y sin puertos
publicados. La red dedicada de Speaches sigue siendo la salida LAN del
worker. El modelo local analiza el texto y Speaches transcribe el audio.
Con una carga local, `SELF_HOST=true` y B-roll desactivado, el ensayo
prescinde de suscripciones y de llamadas a proveedores de pago.

El fixture `synthetic-clips-speech-v2.mp4` está junto a la cápsula Prisma.
Mide 694.534 bytes y dura 31,083 segundos. Su SHA256 es
`b0ec158e3d73c41d9ba4c2cf070a8400382b5f7b50ab7fc0f6722c18eed40606`.
Se generó con una voz española local y un patrón sintético. La extracción
local comprobó que el selector de Speaches acepta la pista AAC etiquetada
`spa` y que FFmpeg produce MP3 mono de 16 kHz con su contrato.

```bash
python3 -I -B "$config_dir/probe_integration_http.py" \
  "$artifact_dir/synthetic-clips-speech-v2.mp4" --clip-output "$new_mp4" \
  < "$account_dto" > "$generation_receipt"
```

La sonda crea la tarea con la sesión de la interfaz, espera `completed`
y descarga un clip seleccionado por el analizador. Comprueba el rechazo
anónimo, el MP4 vertical H.264/AAC, su decodificación completa y su SHA256.
Solicita subtítulos. Comprobarlos visualmente al reproducir el MP4 porque
el render los incrusta en los fotogramas.

Para una prueba previa de transcripción, usar la opción `--clip-fixture`
de la sonda HTTP con una cuenta sintética nueva. Carga el vídeo y devuelve
`upload_leaf`. Ejecutar luego la sonda de medios con ese identificador.
Exige transcripción española y al menos veinte segundos entre la primera
y la última palabra.

```bash
python3 -I -B "$config_dir/probe_integration_http.py" \
  "$artifact_dir/synthetic-clips-speech-v2.mp4" --clip-fixture \
  < "$account_dto" > "$upload_receipt"
upload_leaf=$(python3 -I -B -c 'import json,sys; print(json.load(sys.stdin)["upload_leaf"])' < "$upload_receipt")
docker exec -i --user 1000:1000 "$worker_cid" /usr/bin/python3.12 -I -S -B - \
  "$upload_leaf" --clip-fixture < "$config_dir/probe_integration_media.py" > "$media_receipt"
```
