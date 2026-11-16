# Ensayo privado de Immich 3.3.6

Este ensayo usa el servidor oficial de Immich `3.3.6` en Linux amd64, el
PostgreSQL dedicado ya inicializado, una instancia Valkey descartable y
el proxy autenticado existente. La aceptación cubre migraciones, acceso
autenticado y una búsqueda textual sintética. El servidor escucha sólo en
el loopback del contenedor. Ningún servicio publica puertos.

## Identidades y preparación

- Servidor oficial `ghcr.io/immich-app/immich-server` con manifiesto amd64
  `c0a8e008…`, índice `e1981983…` y fuente `587f549f…`. Verificar el
  manifiesto/configuración efectivos antes de crear. El Compose conserva
  el arranque oficial, que carga `DB_PASSWORD_FILE` y
  `REDIS_PASSWORD_FILE`. Usa UID/GID `1000:1000` y un directorio local
  nuevo, vacío y privado para `/data`, donde Immich genera sus marcadores.
- PostgreSQL existente `agency-media-immich-db/postgres-immich` del
  [Compose dedicado](../postgres-immich/compose.backup.yml), manifiesto
  `a00b7ac5…`, y red `MEDIA_POSTGRES_IMMICH_BACKUP_NETWORK`. Conservar
  CID, identidad del PGDATA, backup inicial y puertos del servicio.
- Valkey `de6fbe96…` en una instancia exclusiva, con datos en tmpfs y ACL
  propia. El proxy `077a815f…` conserva el UID `65532:65532` y la salida
  fija a `192.168.0.6:3003`. Cotejar ambas imágenes locales y sus
  configuraciones. El YAML prohíbe descargas implícitas. [Proxy](../immich-ml-auth/README.md)

Root respalda rutas, identidades y secretos antes de la primera operación.
Para este ensayo, requiere capacidad para 2 GiB de RAM del servidor, más
PostgreSQL, Valkey y el proxy, y espacio local para el almacenamiento
vacío de Immich. El ensayo deja intactas las aplicaciones existentes.

La red `data` debe ser el bridge interno acreditado de PostgreSQL. Admite
sólo el CID existente de esa base y los nuevos CIDs del servidor y Valkey.
Preparar otras dos redes bridge propias y sin recursos previos. La red
`ml` es interna y conecta sólo el servidor y el proxy. La red
`ml_egress` proporciona la ruta LAN del proxy y contiene sólo ese
contenedor. Verificar membresías, rutas e identidad antes de arrancar.
La salida de aplicación se limita al destino fijo por el código del
proxy. Para este ensayo no montar librerías NAS, sockets del host ni
dispositivos GPU, y no añadir rutas al servidor.

Pasar al Compose las siguientes referencias privadas. Los archivos son
regulares, de un enlace, bajo un padre privado y con modo `0400` o
`0600`. En Compose local la legibilidad depende de la propiedad real
del archivo montado.

| Referencia | Contenido o identidad |
| --- | --- |
| `MEDIA_IMMICH_TRIAL_MEDIA_PATH` | Directorio local propio, vacío, `1000:1000` y `0700`. |
| `MEDIA_IMMICH_TRIAL_CONFIG_FILE` | Copia acreditada de `immich.functional.json`, legible por UID 1000. |
| `MEDIA_IMMICH_TRIAL_DB_PASSWORD_FILE` | Contraseña exclusiva del rol SQL `immich_trial`, UID 1000. |
| `MEDIA_IMMICH_TRIAL_REDIS_PASSWORD_FILE` | Contraseña exclusiva de Valkey, UID 1000. |
| `MEDIA_IMMICH_TRIAL_REDIS_ACL_FILE` | ACL del Valkey del ensayo, UID 1000. |
| `MEDIA_IMMICH_TRIAL_GPU_BEARER_FILE` | Bearer ya respaldado para el puente, UID 65532. |
| `MEDIA_IMMICH_TRIAL_ML_NETWORK` | Bridge interno nuevo con ID respaldado. |
| `MEDIA_IMMICH_TRIAL_ML_EGRESS_NETWORK` | Bridge de salida nuevo con ID respaldado. |

El archivo ACL consta de `user default reset off` y
`user immich_trial on #HASH ~* &* +@all`. Sustituir `HASH` por el
SHA256 hexadecimal de los bytes de la contraseña sin LF final. Su alcance
es esa instancia propia y efímera. Confirmar desde ella `NOAUTH` sin
secreto y `PONG` con el usuario y secreto fijados, sin imprimir su valor.
El `reset` elimina `nopass` del usuario por defecto. Esto permite al
usuario nombrado autenticarse desde el servidor sin desactivar el modo
protegido. [ACL y modo protegido](https://raw.githubusercontent.com/valkey-io/valkey/29173b67508b2de4c0f0ed551f3b47cf19fc3ca8/src/networking.c)

## Rol y migraciones

1. Arrancar sólo el CID de PostgreSQL ya acreditado y comprobar salud.
   Confirmar `immich`, PostgreSQL `16.15`, precarga `vchord.so`,
   `vector 0.8.5` y `vchord 1.1.1` instaladas y disponibles con igual
   versión, más `cube` y `earthdistance`. Confirmar dueño actual
   `postgres`, ausencia de `public.kysely_migrations` y del rol
   `immich_trial`. Si hay deriva, preservar la base y diagnosticar.
2. En esa base y con el administrador existente, crear el rol propio
   `CREATE ROLE immich_trial LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;`.
   Fijar la contraseña mediante `psql \password immich_trial` o el
   canal privado equivalente que use el operador. Transferir sólo la
   propiedad de esta base con `ALTER DATABASE immich OWNER TO immich_trial;`.
   Verificar autenticación TCP del rol y `CREATE` sobre el esquema
   `public`, sin crear objetos de prueba persistentes. Conservar
   las extensiones y su propietario administrativo. Los backups
   `pg_dumpall` siguen usando la cuenta administrativa existente.
3. Usar `docker compose --env-file /dev/null -f compose.functional.yml
   config --quiet` con sólo las referencias aprobadas. Crear los tres
   servicios con ese archivo, sin arrancarlos. Inspeccionar y respaldar
   cada CID, imagen, usuario, límites, mounts, redes y puertos. Arrancar
   primero Valkey y el proxy y exigir salud autenticada. Después arrancar
   sólo el CID acreditado del servidor. Acotar el arranque a 240 s.
4. Exigir salud oficial y el mensaje de fin de migración. Por SQL
   comprobar las filas de `public.kysely_migrations`, las tablas
   `public."user"`, `public.asset` y `public.smart_search`, y su
   propiedad por `immich_trial`. Registrar el total y el nombre de la
   última migración, la comprobación de deriva y las cuatro versiones de
   extensión. Conservar el log privado de arranque si la aplicación
   falla. El servidor ejecuta el control de extensiones y las
   migraciones al iniciar. Una extensión ausente o con actualización
   pendiente requiere intervención administrativa separada.

Fuentes fijadas: [PostgreSQL sin superusuario](https://raw.githubusercontent.com/immich-app/immich/587f549f46f1cd1822a06e16dcccecef04ea4565/docs/docs/administration/postgres-standalone.md),
[arranque y secretos](https://raw.githubusercontent.com/immich-app/immich/587f549f46f1cd1822a06e16dcccecef04ea4565/server/bin/start.sh),
[bootstrap de base](https://raw.githubusercontent.com/immich-app/immich/587f549f46f1cd1822a06e16dcccecef04ea4565/server/src/services/database.service.ts)
y [esquema de configuración](https://raw.githubusercontent.com/immich-app/immich/587f549f46f1cd1822a06e16dcccecef04ea4565/server/src/dtos/config.dto.ts).

## Comprobación privada

Ejecutar el cliente por stdin dentro del CID acreditado del servidor
como UID 1000, usando `docker exec -i --user 1000:1000 CID node -e CODIGO`.
El operador pasa por stdin un JSON con una contraseña desechable y
respalda el valor antes del alta. El cliente rechaza redirecciones, acota
cada petición y conserva los cuerpos y cabeceras fuera de los registros.

1. `GET http://127.0.0.1:2283/api/server/version` responde 200 y acredita
   `3.3.6`. `GET /api/users/me` sin token responde 401.
2. `POST /api/auth/admin-sign-up` con `{email,name,password}` crea el
   administrador desechable. Usar un correo bajo `example.invalid`.
   `POST /api/auth/login` con `{email,password}` y contraseña
   incorrecta devuelve 401. La misma ruta con el secreto correcto
   devuelve 201, `accessToken` e `isAdmin: true`. Cerrar aquí la
   primera operación. El token se entrega sólo al operador por la
   sesión SSH privada. Respaldarlo en la bóveda y su referencia en la
   plantilla antes de utilizarlo.
3. En la segunda operación, el operador pasa el token respaldado por
   stdin, y el cliente lo conserva en memoria. Comprobar
   `GET /api/users/me` con ese Bearer, mismo usuario y estado 200.
   `POST /api/search/smart` con
   `{"query":"ensayo de color azul","size":1}` devuelve 200 y
   `assets.items` vacío. Usar texto sintético único por corrida. El
   servidor obtiene un embedding textual antes de consultar PostgreSQL
   incluso en una base vacía. Correlacionar con `POST /predict` exitoso
   en el proxy o en el puente y confirmar la salud `ready`/CUDA.
4. `POST /api/auth/logout` devuelve 200 con `successful: true`. El
   mismo Bearer debe recibir 401 en `GET /api/users/me`. Si falla el
   paso de búsqueda después del login, intentar logout una vez y
   conservar el resultado saneado. Parar sólo los CIDs del ensayo
   y el CID dedicado de PostgreSQL si ya se completó el dump migrado.
   Preservar PGDATA, el directorio de ensayo y sus recibos.

Fuentes fijadas: [rutas de autenticación](https://raw.githubusercontent.com/immich-app/immich/587f549f46f1cd1822a06e16dcccecef04ea4565/server/src/controllers/auth.controller.ts),
[usuario autenticado](https://raw.githubusercontent.com/immich-app/immich/587f549f46f1cd1822a06e16dcccecef04ea4565/server/src/controllers/user.controller.ts)
y [búsqueda semántica](https://raw.githubusercontent.com/immich-app/immich/587f549f46f1cd1822a06e16dcccecef04ea4565/server/src/services/search.service.ts).

## Alcance posterior

El informe candidato de Immich server registra **130 HIGH y 8 CRITICAL**.
La primera ejecución es una prueba funcional privada y acotada. La
producción requiere evaluación y correcciones de seguridad, validación
de precisión y umbrales faciales con medios públicos de control, alta de
la librería NAS delimitada, cuenta/API key MCP de privilegios mínimos,
y un nuevo respaldo con restauración de la base migrada. El ensayo
mantiene fijos el modelo facial propio y CLIP, con detección facial y OCR
deshabilitados. [ADR-003](../../docs/decisions/ADR-003-modelos-faciales-comerciales.md)

El operador conserva la hora de arranque de Immich entre el login y las
consultas autenticadas. La verificación del volcado calcula el SHA256
por partes sobre un descriptor privado. El proxy usa SIGINT para que
su proceso Python salga de forma controlada al detener el ensayo.
