# PostgreSQL dedicado de Immich y primera captura

Esta receta conserva el ensayo `acceptance` de PostgreSQL 16.15. La instancia LAN activa usa PostgreSQL 16.16 y `vector 0.8.7`. [Despliegue LAN y reversión](../immich-server/README-LAN.md#corte-postgresql-y-reversión)

El Compose usa el manifiesto local `a00b7ac5…`, configuración `60331f93…`, y
el comando de precarga probado con PostgreSQL 16.15, pgvector 0.8.5 y
VectorChord 1.1.1. La imagen ya superó la prueba funcional SQL en dos
arranques. El destino de este incremento es una base propia persistente y
una captura lógica real, con la autorización funcional que corresponda. La
migración de Immich y la aprobación de producción siguen separadas.

## Preparar y arrancar

1. Root coteja imagen/configuración, fuentes y capacidad del NVMe. Prepara un
   PGDATA local nuevo bajo `/var/lib/media-platform` con UID/GID `70:70` y
   modo `0700`. Acredita ruta, sistema de archivos e identidad. Mantiene los
   datos fuera de NFS y de los directorios del ensayo SQL y postgres-apps.
2. Root prepara una red bridge externa propia con `Internal=true`, sin puertos
   de base publicados. Respalda su ID y los cuatro valores de referencia que
   requiere el YAML. `MEDIA_POSTGRES_IMMICH_BACKUP_DATA_PATH` apunta a PGDATA,
   `MEDIA_POSTGRES_IMMICH_BACKUP_NETWORK` a esa red y los sufijos
   `PASSWORD_FILE` y `PGPASS_FILE` a archivos privados del operador. Ambos
   archivos deben ser regulares, de un enlace, `70:70`, y modo `0400` o `0600`.
   El pgpass autentica `postgres` por `127.0.0.1:5432` para todas las bases,
   incluida `immich`. Los valores de credenciales permanecen en archivos.
3. Validar `docker compose --env-file /dev/null -f compose.backup.yml config --quiet`
   con las cuatro referencias en el entorno explícito de Root. Usar `create`
   para `postgres-immich`, comprobar CID, digest, comando, UID, mounts y red,
   respaldar el CID y ejecutar `start` sólo para ese servicio. El proyecto es
   `agency-media-immich-db`. La salud ejecuta SQL autenticado sobre `immich`.

Una vez saludable, Root pasa por stdin el siguiente SQL a `psql -X -w -h
127.0.0.1 -p 5432 -U postgres -d immich --set=ON_ERROR_STOP=1`, mediante
`docker exec -i --user 70:70` sobre el CID acreditado. `PGPASSFILE` ya apunta
al archivo montado. Las cuatro extensiones siguen la prueba SQL existente.

```sql
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.5';
CREATE EXTENSION IF NOT EXISTS vchord VERSION '1.1.1';
CREATE EXTENSION IF NOT EXISTS cube;
CREATE EXTENSION IF NOT EXISTS earthdistance;
COMMIT;
```

Comprobar las versiones físicas de `pg_extension`, `shared_preload_libraries`
y `server_version_num` antes del dump. Esto inicializa la base y sus extensiones.
Para acreditar respaldo de Immich con datos, preparar su rol propio, ejecutar
las migraciones originales con el servidor fijado y repetir la captura sobre
la base migrada. Las tablas de prueba ANN del ensayo SQL no se incorporan a
la base persistente.

## Captura y restauración

En `.85`, Root invoca la fuente fijada con un staging nuevo bajo un padre
Root `0700`. Reservar hasta 2 GiB en ese sistema de archivos para la captura.

```bash
bash capture-backup.sh "$cid" "$pgdata_host" "$staging_nuevo"
```

El capturador valida hostname/IP, proyecto/servicio/owner, manifiesto, salud,
usuario y bind de PGDATA. Ejecuta `pg_dumpall --clean --if-exists` por TCP local
en el contenedor con pgpass, en un máximo de 300 s para el dump. Comprime por
stream, verifica el frame y las marcas de inicio y fin, y coteja la época del
CID antes de publicar `postgres-immich.sql.zst` de modo `0600`. El JSON de
salida contiene CID, ruta, tamaño y SHA. Cualquier fallo conserva el staging
para inspección. Las credenciales y el texto SQL se mantienen privados.

Root respalda el recibo y pasa ese archivo con su SHA al set existente de
`sre-backup-321.sh`. Para validar la restauración, usar otro clúster temporal
propio con el mismo digest y precarga y un administrador de recuperación de
nombre distinto al de origen. Verificar el SHA y descomprimir por un pipe a
`psql -X --set ON_ERROR_STOP=1` en el clúster aislado. Comparar PostgreSQL,
precarga, extensiones y objetos de la base recuperada. Cuando existan tablas
de Immich, cotejar también sus datos. NAS/R2 y RPO/RTO requieren sus recibos
de restauración propios.
