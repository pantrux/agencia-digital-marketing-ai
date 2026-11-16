# n8n privado en vm-media-01

`compose.private.yml` ejecuta el servidor n8n 2.46.6 y los dos runners acreditados con sus IDs OCI locales Linux amd64. El servidor publica sólo `https://192.168.0.85:5678/`. El operador `.6` y el consumidor `.7` entran por ese puerto. El broker usa su propia IP en una red interna y escucha en `5679` sin publicarla. PostgreSQL y los puertos de salud de los runners tampoco se publican.

El techo adicional de memoria de los tres contenedores es **2.432 MiB**, distribuidos en 1.280 MiB para n8n, 640 MiB para JavaScript y 512 MiB para Python. Los tmpfs consumen memoria dentro de esos límites. La concurrencia inicial es una tarea por runner y el plazo es 60 segundos. Verificar memoria disponible, presión de los servicios existentes y espacio para base, WAL, datos n8n y backups antes de comenzar. La aceptación de esta configuración y de sus límites requiere el ensayo real. El operador de arranque mantiene `restart: "no"` hasta aplicar y verificar las guardas LAN.

## PostgreSQL 16 compartido con Kitsu

Reutilizar el `postgres-apps` vivo del proyecto `agency-media` por CID completo, propietario `70:70`, PGDATA de Kitsu y manifiesto local `sha256:400e83e5e7aef5630f51e818b190327062420590c3cff4228c543921f64922de`. El `pg_dumpall` del clúster compartido incluye la base `n8n` y su rol. Al sustituir `postgres-apps`, detener primero la unidad n8n, restablecer el endpoint del nuevo contenedor en `agency-n8n-postgres` con la misma IP y alias y actualizar `deployment.json` y `private_lifecycle.py` antes de reanudarla. Confirmar conexiones autenticadas desde la IP de n8n. [Contrato PostgreSQL apps](../postgres-apps/README.md) · [Captura existente](../../sre/capture-postgres-backup.sh)

1. Reservar tres subredes IPv4 distintas y libres frente a las rutas de `.85` e IPAM Docker. Crear únicamente `agency-n8n-postgres` como bridge Docker `--internal --ipv6=false` en la subred reservada para PostgreSQL. Conectar el CID existente con alias `postgres-n8n`. Antes y después, conservar las redes originales, la ruta de salida, PID y hora de inicio de PostgreSQL. Exigir que los únicos miembros de la red nueva sean ese PostgreSQL y el servidor n8n, con la IP fija `AGENCY_N8N_DB_CLIENT_IP`. En el servidor, comprobar que el alias resuelve exclusivamente a la IP del clúster en esa red.
2. Examinar las reglas activas con `pg_hba_file_rules`, incluidos `include*`, reglas `host*` anteriores y el archivo real devuelto por `SHOW hba_file`. Respaldar la preimagen. Anteponer este bloque a las reglas IP generales, conservando sus líneas y orden. Usar IP y subred reservadas reales. `host` cubre tanto conexiones SSL como sin SSL, y SCRAM es obligatorio en la regla de acceso.

   ```conf
   host n8n n8n <IP_N8N>/32 scram-sha-256
   host all all <SUBRED_DB_N8N> reject
   host all n8n 0.0.0.0/0 reject
   host all n8n ::/0 reject
   ```

3. Recargar mediante `SELECT pg_reload_conf()` sin reiniciar PostgreSQL. Confirmar que no hay errores en `pg_hba_file_rules` y que Kitsu conserva salud. Si existe una regla `local` general que permite usar el rol n8n desde un socket local, recordar que ese socket sólo está en el contenedor PostgreSQL. La app y los runners carecen de ese montaje.
4. Comprobar que `n8n` no colisiona con ningún rol, base ni tablespace existente. Crear un rol de login con `NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS` y sin membresías. Proporcionar su contraseña respaldada al cliente `psql` mediante un canal privado con echo desactivado. Crear la base `n8n` propiedad de ese rol y ejecutar `REVOKE ALL ON DATABASE n8n FROM PUBLIC` y `GRANT CONNECT, TEMPORARY ON DATABASE n8n TO n8n`. Verificar que el dueño puede crear las tablas de migración en `public`. No cambiar ACL, esquemas ni datos de Kitsu. Comprobar que `n8n` llega a su base desde su IP y que se rechazan otras bases desde la red dedicada y ese rol desde otras redes.

## Rutas y secretos

El directorio de datos `AGENCY_N8N_DATA_PATH` debe ser físico, local a `.85`, propiedad `1000:1000`, modo `0700` y estar bajo un padre administrado. Su bind conserva los archivos de instancia y de ejecución. Incluirlo en la captura y restauración n8n junto con el volcado PostgreSQL y el respaldo cifrado de la clave. No reutilizar el perfil del ensayo. PostgreSQL captura las tablas en `pg_dumpall`; la copia de este directorio requiere su propio recibo.

El editor genera sus archivos estáticos bajo `/home/node/.cache/n8n/public`. El servidor dispone de un tmpfs de 128 MiB en `/home/node/.cache` para regenerarlos en cada arranque y aloja allí también la caché de npm. Los datos persistentes y la configuración siguen en `/home/node/.n8n`.

Configurar estas variables de rutas e IP mediante un entorno acotado con `--env-file /dev/null`. Las variables sólo contienen rutas públicas o privadas, nunca valores de secretos. Los archivos de contraseña, cifrado, token y clave TLS son regulares, sin enlaces, `1000:1000/0400`, ya respaldados. Certificado y CA son públicos y legibles por UID 1000. En Compose basado en archivos, los permisos efectivos proceden del origen.

| Variable | Entrada |
| --- | --- |
| `AGENCY_N8N_INGRESS_SUBNET`, `AGENCY_N8N_INGRESS_IP` | Subred de entrada e IP fija del servidor, sin solapamientos. |
| `AGENCY_N8N_BROKER_SUBNET`, `AGENCY_N8N_BROKER_IP` | Subred interna e IP fija del listener `5679`. |
| `AGENCY_N8N_DB_CLIENT_IP` | IP fija del servidor en la red privada de la base. |
| `AGENCY_N8N_DATA_PATH` | Datos locales de instancia. |
| `AGENCY_N8N_DB_PASSWORD_FILE` | Contraseña del rol `n8n`. |
| `AGENCY_N8N_ENCRYPTION_KEY_FILE` | Clave de cifrado de credenciales n8n. |
| `AGENCY_N8N_BROKER_TOKEN_FILE` | Token compartido entre servidor y runners. |
| `AGENCY_N8N_TLS_CERT_FILE`, `AGENCY_N8N_TLS_KEY_FILE` | Cadena PEM del servidor y su clave privada. El certificado requiere SAN IP `192.168.0.85` y propósito servidor TLS. |
| `AGENCY_N8N_TLS_CA_FILE` | CA pública específica de n8n, no la CA de observabilidad. |

El servidor carga certificado y clave desde rutas `N8N_SSL_CERT` y `N8N_SSL_KEY`. `N8N_SECURE_COOKIE=true` marca la sesión `Secure` y `HttpOnly`. Usar una CA configurada sólo en el cliente, por ejemplo `curl --cacert <ca-pública> https://192.168.0.85:5678/healthz/readiness`. El healthcheck interno conecta a la IP del listener, valida la CA y exige el SAN `192.168.0.85`. También comprueba el broker en su IP interna. La disponibilidad de ambos launchers y una ejecución de cada lenguaje se verifican con el ensayo, ya que la salud del launcher es de proceso.

En Windows se verificó la cadena privada con `ssl.create_default_context(cafile=...)` en Python. El `curl.exe` del sistema usa Schannel y en esta VM rechazó la CA privada por estado de revocación desconocido. Para una sonda desde Windows usa un cliente que valide la CA y el SAN sin depender de ese proveedor de revocación.

## Arranque y cierre

1. Cotejar las tres imágenes Linux amd64 y el estado previo de Docker. Usar `docker compose --env-file /dev/null -f compose.private.yml config --quiet`, `create --pull never` y registrar los CID antes de iniciarlos. Inspeccionar redes, `restart:no`, `init`, puertos, límites y montajes sin publicar secretos. Compose requiere `gw_priority` y el entorno de `.85` tiene un recibo de Compose 2.40.3.
2. Aplicar `python3 -I -B private_ingress.py apply --interface <interfaz-LAN-confirmada>` antes de arrancar cualquier contenedor n8n. El operador usa una transacción `iptables-restore --noflush`, preserva el prefijo de observabilidad y las reglas de MoneyPrinter y SupoClip, y confirma el salto de `FORWARD` a `DOCKER-USER`. La guarda `INPUT` cubre el tráfico local del host y la de `DOCKER-USER` cubre DNAT al bridge n8n. La IP de publicación y el bridge son sólo IPv4. Ejecutar `check` antes y después de publicar y después de cualquier reload de Docker o del firewall. Probar `.6` y `.7` con CA, un origen LAN distinto rechazado y el broker/DB inaccesibles desde LAN.
3. Iniciar el servidor con `docker compose … start n8n`, esperar salud HTTPS y luego `start javascript python`. El primer propietario debe configurarse desde `.6` con su credencial respaldada. Ejecutar un flujo sintético con ambos runners y comprobar cookie, cifrado de credenciales y que los datos persisten al reiniciar los mismos CID. Capturar una copia y validar restauración aislada antes de declarar la protección operativa.
4. Para parar, cerrar o suspender entradas de trabajo y esperar que no haya ejecuciones activas. Enviar la parada a servidor y runners en paralelo con sus respectivos plazos 90/65 s, para que el broker y ambos launchers puedan cerrar sin espera serial. Verificar salida `0`, sin OOM ni PID, y conservar `restart:no`, guardas, red privada, CID y datos. El PostgreSQL compartido sigue en servicio. No usar `down -v` ni reiniciar PostgreSQL durante un cierre de n8n.

## Arranque persistente

Instalar `private_lifecycle.py` y `private_ingress.py` como archivos root bajo `/opt/media-platform/n8n-private-v1`. En ese mismo directorio crear `deployment.json`, archivo regular `root:root/0600`, con esta estructura. Sustituir todos los CID e ID de red de ejemplo por los valores de 64 dígitos hexadecimales ya respaldados.

```json
{
  "interface": "<interfaz IPv4 LAN de .85>",
  "containers": {"n8n": "<CID>", "javascript": "<CID>", "python": "<CID>", "postgres": "<CID postgres-apps>"},
  "database_network": "<ID de agency-n8n-postgres>",
  "database_client_ip": "172.31.240.34",
  "database_postgres_ip": "172.31.240.35"
}
```

Instalar la unidad `agency-n8n-private.service`, comprobarla con `systemd-analyze verify`, ejecutar `daemon-reload` y realizar la primera aceptación por esa unidad. Habilitarla al confirmar pruebas HTTPS, SQL y de tareas. El arranque verifica el contrato Docker y la red privada antes de aplicar y comprobar el firewall. Inicia el servidor por su CID, espera HTTPS y broker, y arranca ambos runners. El operador detiene sólo los tres contenedores propios si falla el arranque. `systemctl stop agency-n8n-private.service` conserva las guardas y deja PostgreSQL en servicio. Rehacer el recibo ante cualquier sustitución autorizada de un CID.

Para una sustitución aislada del servidor, detener la unidad y comprobar que n8n y sus runners quedaron detenidos. Validar el Compose nuevo con `config --quiet`, cotejar el CID y los montajes del servidor anterior y retirar únicamente ese contenedor con `docker container rm <CID-anterior>`. Usar `docker compose … create --pull never n8n`, que en Compose 2.40.3 no admite `--no-deps`. Verificar que se conservan las identidades de los runners, las redes, el bind de datos y los archivos de secretos. Respaldar el CID nuevo y actualizar `deployment.json` antes de volver a iniciar la unidad.
