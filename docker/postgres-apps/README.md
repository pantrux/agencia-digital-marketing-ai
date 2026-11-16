# PostgreSQL 16.15 para aplicaciones

## Candidato 16.16

[`Dockerfile.16.16`](Dockerfile.16.16) usa como contexto nombrado `pg16_16_base` el manifiesto oficial Linux amd64 `sha256:a2f7e20374cd8fd8ed4a588b79549e10589bea7b1686e77922f5ac741b80b8a1`. El OCI de entrada ya incluye las revisiones de `krb5-libs`, `libxslt` y `zlib` que se añadieron a 16.15. La construcción conserva el contrato `70:70`, retira `gosu` y `su-exec` del sistema de archivos final y mantiene el entrypoint original. [Imagen oficial Linux amd64](https://hub.docker.com/layers/library/postgres/16.16-alpine3.24/images/sha256-a2f7e20374cd8fd8ed4a588b79549e10589bea7b1686e77922f5ac741b80b8a1)

Con el layout OCI ya verificado en la VM, prepara un directorio nuevo que contenga sólo una copia de `Dockerfile.16.16` con el nombre `Dockerfile`. Pasa ese directorio como `CONTEXTO` y usa la ruta absoluta del layout en `BASE_OCI`.

```sh
BUILDX_GIT_INFO=false BUILDX_GIT_LABELS=false BUILDX_GIT_CHECK_DIRTY=false \
docker buildx build --builder default --platform linux/amd64 --network=none \
  --pull=false --provenance=false --sbom=false --load \
  --build-context "pg16_16_base=oci-layout://${BASE_OCI}@sha256:a2f7e20374cd8fd8ed4a588b79549e10589bea7b1686e77922f5ac741b80b8a1" \
  --tag media-platform/postgres-apps:16.16-agencia.1 "$CONTEXTO"
```

La imagen `400e83e5…`, activa para Kitsu y n8n, pasó dos arranques aislados con el mismo clúster, siete reglas SCRAM, checksums, rechazo sin contraseña, `uuid-ossp` y `pg_trgm`. La promoción requiere comprobar una copia recuperable y los índices afectados en cada clúster, actualizar sus referencias de salud y copia y validar de nuevo las aplicaciones. Cualquier extensión C compilada por fuera de la base oficial requiere su propia verificación para 16.16. [Notas de PostgreSQL](https://www.postgresql.org/docs/16/release-16-16.html)

## Historial de la imagen 16.15

Esta receta candidata deriva de la imagen oficial Alpine y ejecuta PostgreSQL como UID/GID `70:70`. Retira el archivo ejecutable `gosu` y el enlace `su-exec`, cuyo destino es `gosu`; conserva intactos el entrypoint, sus auxiliares, el inventario de paquetes y el historial heredado. No necesita descargar ni instalar paquetes durante la construcción.

La imagen derivada ya se construyó y ejecutó en aceptación aislada en `.85`, el 2026-11-06. Su sistema de archivos final pasó SCA sin HIGH/CRITICAL, con 45 paquetes Alpine detectados. Los resultados reales de autenticación, checksums, reinicio y permisos se describen al final. La base Alpine tiene cero hallazgos HIGH/CRITICAL del sistema operativo en el informe conservado; sus 25 HIGH y un CRITICAL corresponden al binario `gosu`. Retirar ese binario exige comprobar la imagen final con un escaneo real. Los bytes de capas antiguas siguen formando parte de la imagen base: esta receta elimina la herramienta del sistema de archivos final, sin afirmar que reescribe las capas históricas.

## Base y archivos comprobados

Consulta pública verificada el 6 de noviembre de 2026, para `linux/amd64`:

| Elemento | Valor |
| --- | --- |
| Referencia fijada | `docker.io/library/postgres:16.15-alpine3.24@sha256:ac838714b7d794185a866e5a6b479b954e7d8a070568aa0acac6fe07c3070bf5` |
| Índice multiplataforma | `sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139` |
| Configuración de la plataforma | `sha256:4278d7b1e3deb3193ddc803158f6532963f059fc25a2eefaee4b5a9e71741873` |
| Sistema / PostgreSQL | Alpine `3.24.4` / PostgreSQL `16.15` |
| Cuenta real | `postgres:x:70:70::/var/lib/postgresql:/bin/sh` |
| Grupo real | `postgres:x:70:postgres` |
| `PGDATA` | `/var/lib/postgresql/data` |
| Entrypoint / comando / parada | `["docker-entrypoint.sh"]` / `["postgres"]` / `SIGINT` |
| Marcador de la imagen derivada | `cl.e-scale.agencia.owner=agencia-digital-marketing-ai:postgres-apps:v4.1` |

| Archivo del digest base | SHA-256 |
| --- | --- |
| `/usr/local/bin/docker-entrypoint.sh`, conservado | `9c440299ae04a0a79d55b8bf03307036d890a40979d2fb698073c9050d4b20a5` |
| `/usr/local/bin/docker-ensure-initdb.sh`, conservado | `922ade6b23a312e65023349b36e873752b15ff38ae4c0ae461d74451e104f312` |
| `/usr/local/bin/gosu`, retirado | `52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0` |

La inspección verificó los SHA-256 y tamaños de las once capas OCI antes de leer sus archivos; no ejecutó sus binarios. Evidencias locales: `.tools/evidence/phase4-postgres-apps-base-inspection.json` y `.tools/evidence/phase4-postgres-16.15-alpine-trivy.json`. La base heredada conserva `GOSU_VERSION=1.19` como dato histórico; no se borra para alterar un resultado del escáner.

Fuentes primarias: [imagen oficial PostgreSQL](https://hub.docker.com/_/postgres), [repositorio oficial de la imagen](https://github.com/docker-library/postgres), [autenticación por contraseña de PostgreSQL 16](https://www.postgresql.org/docs/16/auth-password.html) y [archivo de contraseñas de libpq](https://www.postgresql.org/docs/16/libpq-pgpass.html). Para los scripts de esta construcción manda el contenido del digest inspeccionado, no la rama actual del repositorio.

## Contrato de ejecución

La construcción y la aceptación corresponden exclusivamente a `vm-media-01`, `192.168.0.85`, sobre Docker Linux `amd64`. No usar Windows ni el equipo `.7`. La base y los hashes de estas fuentes deben estar respaldados antes de descargar o construir; después de construir, respaldar el ID local de configuración de la imagen antes de crear contenedores. Un ID local `sha256:...` no es un `RepoDigest` publicado.

La imagen hereda el entrypoint oficial sin modificarlo. Con UID `70` no se ejecuta su rama de `gosu` ni su cambio de propietarios. Forzar `--user 0` incumple este contrato y puede fallar porque la herramienta fue retirada. El host debe preparar un directorio nuevo y propio, por ejemplo `/var/lib/media-platform/postgres-apps`, dueño `70:70`, modo `0700`, dentro de un padre administrado y protegido. No cambiar el UID/GID de usuarios del host. No reutilizar datos encontrados ni inicializar sobre NFS: la base permanece en el disco local NVMe de `.85`.

| Entrada | Condición |
| --- | --- |
| `PGDATA` | Fijo en `/var/lib/postgresql/data`; montaje bind explícito al directorio propio local. |
| `POSTGRES_PASSWORD_FILE` | `/run/secrets/postgres_password`, archivo privado de sólo lectura cuyo valor ya está respaldado en `.env`. No pasar la contraseña por argumentos, valores del entorno ni logs. |
| `POSTGRES_USER`, `POSTGRES_DB` | Identificadores elegidos y respaldados por la raíz antes de usarlos; el usuario de inicialización es superusuario. Las aplicaciones deberán recibir usuarios separados en otro incremento. |
| `POSTGRES_HOST_AUTH_METHOD` | `scram-sha-256`. |
| `POSTGRES_INITDB_ARGS` | `--data-checksums --auth-host=scram-sha-256 --auth-local=scram-sha-256`. Sólo se aplica al directorio vacío. |
| Archivo de contraseña de `psql` | Archivo distinto, privado y respaldado antes de usar, montado como `/run/secrets/postgres_pgpass`; `PGPASSFILE` apunta a esa ruta. Debe contener la misma contraseña con el formato y escapes de libpq, sin imprimirla. |

El secreto y el archivo libpq deben ser archivos regulares sin enlaces, montados en lectura y legibles por `70:70`, con modo `0400` y padres protegidos. Verificar sus modos reales: los secretos de Compose basados en archivos pueden conservar permisos del archivo origen. No depender de que los campos `uid`, `gid` o `mode` de Compose reescriban esos permisos.

La ejecución de aceptación no publica puertos, usa red `none`, raíz de sólo lectura, capacidades eliminadas, `no-new-privileges`, memoria compartida privada y `--shm-size=128m`. Necesita escritura únicamente en el bind de `PGDATA` y en tmpfs para `/var/run/postgresql` y `/tmp`, preparados para UID/GID `70:70`. Comprobar que `/var/run/postgresql` resuelve a su directorio canónico bajo `/run`. Cualquier fallo se diagnostica sin recurrir a modo privilegiado ni a permisos globales.

La base declara un `VOLUME` para `PGDATA`. Por ello, omitir el bind puede crear un volumen anónimo; la receta no puede impedirlo por sí sola. Crear el contenedor detenido, respaldar inmediatamente su ID y comprobar sus montajes antes de arrancarlo. Exigir que el único montaje de `PGDATA` sea el bind exacto de la ruta propia: no adoptar un volumen anónimo o un directorio ajeno.

## Matriz de aceptación real

La raíz debe ejecutar esta matriz en `.85`, guardar salida y código de cada paso y detenerse ante el primer incumplimiento. Cada directorio, secreto, nombre e ID nuevo se respalda antes de usarlo; se rechazan colisiones, enlaces y recursos sin marcador propio. Preparar una carpeta de aceptación separada de cualquier futura base de aplicación. No iniciar automáticamente la aplicación después de estas pruebas.

| Paso | Ejecución y evidencia exigida |
| --- | --- |
| Preflight | Confirmar hostname, IP, arquitectura, Docker Linux, disco local y espacio. Comparar SHA-256 de `Dockerfile` y `.dockerignore` con los respaldados; confirmar ausencia de secretos en el contexto. Registrar el digest de la base descargada. |
| Construcción | Con la base exacta ya descargada: `docker build --network=none --pull=false --tag agencia-postgres-apps:16.15-agencia.1 docker/postgres-apps`. Las comprobaciones de usuario y hashes incluidas deben pasar. Respaldar el ID resultante y usar ese ID en los pasos posteriores, sin confiar sólo en el tag mutable. |
| Inspección de imagen | Exigir `Config.User=70:70`, entrypoint/comando/`SIGINT` heredados, `PG_VERSION=16.15`, `PGDATA` y marcador exactos. Guardar inspección saneada. No inspeccionar contenedores con secretos para publicar el entorno completo. |
| Preparación | Crear sólo recursos propios comprobados. Preparar el bind vacío `70:70/0700`, los dos archivos secretos `70:70/0400` y tmpfs escribibles por ese usuario. `docker create` con los controles anteriores, etiqueta propia y sin puertos publicados; respaldar el ID devuelto y validar sus montajes estando detenido. |
| Inicialización | Arrancar ese ID. Esperar `pg_isready` con plazo máximo de 60 segundos; no basta con él para acreditar autenticación. Exigir salida de arranque sin errores, `PG_VERSION=16` en el bind, propietario `70:70` y modo `0700`. Dentro del contenedor, `id -u` e `id -g` deben devolver `70`; `test ! -e /usr/local/bin/gosu` y `test ! -L /usr/local/bin/su-exec` deben pasar. Comparar otra vez los hashes de ambos scripts oficiales. |
| SQL autenticado | Por TCP `127.0.0.1:5432`, con `PGPASSFILE=/run/secrets/postgres_pgpass`, ejecutar `psql --no-password --set=ON_ERROR_STOP=1` para el usuario/base de aceptación. Exigir `SHOW server_version` = `16.15`, `SHOW data_checksums` = `on`, autenticación SCRAM en las reglas local/host y usuario SQL esperado. Crear una tabla y una fila centinela propias con un valor público fijo. No consultar ni imprimir hashes de contraseñas. |
| Rechazo de contraseña ausente | En otra invocación `psql` contra el mismo TCP, sin `PGPASSWORD`, con `PGPASSFILE=/dev/null` y `--no-password`, exigir código distinto de cero por falta de contraseña. Si conecta, no acreditar el paso SQL aunque la consulta anterior haya funcionado. |
| Reinicio | Detener mediante `docker stop --time 30` y exigir salida limpia dentro del plazo, sin terminación forzada. Arrancar el mismo ID; exigir que omite la inicialización y que la misma fila centinela persiste por conexión autenticada. Confirmar que la propiedad y los hashes no cambiaron. |
| Rechazos de permisos | En contenedores y directorios de aceptación distintos, con sus IDs respaldados: un `PGDATA` nuevo sin escritura para UID `70` y un secreto sin lectura deben fallar con código no cero. No cambiar permisos de la prueba positiva ni reutilizar datos. No ampliar permisos ni cambiar a root para hacerlos pasar. |
| SCA de imagen final | Analizar el ID local construido con Trivy Linux y `--image-src docker`, `--scanners vuln`, `--severity HIGH,CRITICAL`, `--exit-code 1`; usar DB vigente y guardar JSON, stderr, versión y tiempos. No usar `--ignore-unfixed`, filtros por paquete o borrado de metadatos. Exigir cero HIGH/CRITICAL y confirmar que el sistema de archivos final ya no incluye `gosu`/`su-exec`. Si se analiza un archivo exportado, verificar primero su SHA y usar el modo `--input` del escáner, sin publicar la imagen ni abrir la API de Docker. |
| Cierre | Guardar resultados de init/reinicio/SQL, IDs y hashes, y detener sólo los contenedores propios identificados. Conservar datos/evidencia para revisión; ninguna limpieza global, eliminación automática o despliegue a producción. |

Ejemplo de conexión de aceptación, donde `ID_RESPALDADO`, `USUARIO_RESPALDADO` y `BASE_RESPALDADA` representan valores no secretos ya comprobados por la raíz:

```sh
docker exec --env PGPASSFILE=/run/secrets/postgres_pgpass ID_RESPALDADO \
  psql --host=127.0.0.1 --port=5432 --username=USUARIO_RESPALDADO \
  --dbname=BASE_RESPALDADA --no-password --set=ON_ERROR_STOP=1 \
  --command='SHOW server_version; SHOW data_checksums; SELECT current_user;'
```

No se añadieron pruebas estáticas que imiten el contenido de la receta. La matriz verifica comportamiento real, autenticación, persistencia y seguridad del artefacto final. La aceptación aislada completó los pasos positivos y los dos rechazos de permisos. No acredita todavía roles/bases de aplicaciones ni compatibilidad de extensiones de Immich.

El contenedor positivo `agency-acceptance-postgres-apps-20261106-02` verificó PostgreSQL 16.15, UID/GID 70, datos locales 0700, checksums activados, SCRAM local/TCP, contraseña ausente rechazada, scripts oficiales intactos y dos filas conservadas tras parada limpia y arranque. Duró 5,696 s. Dos contenedores distintos con datos o secreto inaccesibles terminaron con código 1 por permisos, sin OOM. Todos quedaron detenidos y sus IDs se respaldaron antes de iniciarlos.

La primera prueba `01`, conservada como evidencia inicial, acreditó SQL/TCP y persistencia, pero no activó checksums ni SCRAM local; no se presenta como la matriz completa. La prueba `02` utilizó datos nuevos y los parámetros completos, sin alterar la primera. Evidencias privadas: `phase4-postgres-apps-02-positive-runtime-acceptance.json`, `phase4-postgres-apps-02-data-permission-runtime-acceptance.json`, `phase4-postgres-apps-02-secret-permission-runtime-acceptance.json` y sus recibos de parada.

En Docker 29 con almacenamiento containerd, el ID devuelto corresponde al manifiesto OCI; Trivy identifica la configuración de la imagen. Se comprobó la relación índice → manifiesto → configuración → DiffIDs y los hashes de todas las capas del archivo exportado. `phase4-built-images-sca-receipt.json` liga ambos tipos de digest, archivo e informe. Ninguno de ellos es un RepoDigest publicado.
