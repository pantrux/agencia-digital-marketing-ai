# Corrida integral de copias 3-2-1

`backup_321_orchestrator.py` coordina una copia al NAS y otra a R2 a partir del mismo staging sellado. Usa `sre-backup-321.sh` como ejecutor de cada destino. La preparación de archivos, las pruebas de restauración y la programación diaria son pasos independientes.

## Preparación

1. Usa `vm-media-01` con Python 3.12 y un directorio privado root `0700` dentro de `/var/lib/media-platform`. La corrida recibe un staging nuevo, de propietario root, con exactamente `postgres-apps.sql.zst`, `postgres-immich.sql.zst`, `d1.sql.zst`, `openclaw.tar.zst` y `SHA256SUMS`. Cada archivo tiene modo `0600`, un enlace y contenido. El tamaño agregado de los cuatro archivos de datos se limita a 2 GiB en este incremento. El manifiesto consta de una línea por payload, con la huella SHA-256, dos espacios, nombre y salto de línea. Registra el SHA-256 de `SHA256SUMS` al terminar la captura.
2. Usa capturas frescas de los productores PostgreSQL, D1 y OpenClaw. [PostgreSQL Immich](../docker/postgres-immich/README-backup.md), [D1](README-d1-export.md), [OpenClaw](README-openclaw-backup.md). El orquestador falla con `OPENCLAW_FALTANTE` cuando el payload está ausente. Los cinco agentes propios se registraron para la corrida de producción de Fase 4. Esa captura contiene sus configuraciones. Los archivos sintéticos y los respaldos previos de ensayo conservan su alcance en sus recibos.
3. Instala ambos scripts juntos en una ruta propia de root y coteja sus huellas. Verifica los repositorios restic existentes, sus identificadores y el montaje NFS. Prepara el directorio padre de recibos como root `0700`, dentro de `/var/lib/media-platform` y fuera del staging, con al menos 64 MiB libres. Configura las credenciales mediante el procedimiento privado vigente. R2 usa las dos variables de plataforma y cada destino un `password_file` propio. Cuando NFS proyecta `0777`, el ejecutor necesita `BACKUP_NAS_DSM_RECEIPT_FILE` con el recibo DSM acreditado. La solicitud contiene rutas de archivos y referencias de repositorio.

## Interfaz

Ejecuta `python3 -I -B backup_321_orchestrator.py plan` y entrega por stdin un JSON con `staging` y `manifest_sha256`. Esta operación comprueba el conjunto y sus huellas. El recibo de `plan` mantiene `backup_set_complete=false`.

Para ejecutar, usa `python3 -I -B backup_321_orchestrator.py run` con estos campos JSON adicionales.

| Campo | Contenido |
| --- | --- |
| `run_id` | Identificador nuevo y estable, de hasta 64 caracteres alfanuméricos, guion, punto o guion bajo |
| `journal` | Ruta absoluta del directorio privado padre de recibos |
| `restic` y `restic_sha256` | Ruta absoluta y huella del ejecutable Linux acreditado |
| `targets` | Objeto con `nas` y `r2`. Cada destino recibe `repository`, `id` y `password_file` |
| `timeout_seconds` | Límite entero por destino entre 30 y 3600 segundos |

El proceso toma un bloqueo exclusivo común para la VM. Guarda `intent.json`, seguido del intento y el resultado de cada destino. Acredita el recibo persistido del NAS antes de invocar R2. Cada destino obtiene el mismo manifiesto y la misma identidad del staging. La evidencia `backup_set_complete=true` requiere los dos snapshots acreditados. La restauración conserva `restoration_verified=false` hasta realizarla y verificarla en una operación independiente.

Tras un timeout, interrupción o recibo inconsistente, revisa los archivos del directorio de la corrida y coteja el tag `run:<run_id>` en el repositorio afectado. Conserva las copias ya verificadas. El mismo `run_id` no vuelve a ejecutar una corrida con directorio existente. Usa un identificador nuevo sólo tras reconciliar las operaciones de resultado incierto. Si el proceso termina antes de guardar `result.json`, los archivos `*.intent.json` identifican las operaciones que requieren revisión.

## Programación diaria

La [entrada de cron](cron.d/agency-backup-321-daily) comprueba la hora de `America/Santiago` al minuto 30 y activa `agency-backup-321-daily.service` a las 03:30. `backup_321_daily.py` usa la fecha de Santiago para el identificador de la corrida. Captura PostgreSQL, D1 y OpenClaw, ensambla un staging nuevo y llama al orquestador para copiar el mismo conjunto a NAS y R2. La captura de OpenClaw usa una [identidad SSH dedicada](README-openclaw-transport.md).

En la VM, `systemctl is-active cron.service` y `/etc/cron.d/agency-backup-321-daily` identifican el programador vigente. `journalctl -u agency-backup-321-daily.service` muestra el resultado del servicio. El perfil privado de la instalación define el directorio de recibos del runner. Tras un fallo, comprueba los recibos y los tags de restic antes de ejecutar una nueva corrida. El servicio necesita el punto de montaje completo `/mnt/producciones` en su namespace de escritura para que el ejecutor valide la identidad NFS. El ejecutor comprueba por separado la ruta y el ID del repositorio.

La prueba de la VM terminó las seis etapas y creó ambos snapshots en el sandbox de systemd. La entrada de cron se instaló el 13 de noviembre de 2026 con propietario `root:root` y modo `0644`. La restauración desde NAS y R2 se verificó por separado con el conjunto `fase4-produccion-20261113-v1`, incluidas las comprobaciones lógicas de PostgreSQL y D1.

## Medir la cobertura diaria

En `vm-media-01`, ejecuta como root la auditoría de sólo lectura desde la instalación del runner. Indica el intervalo inclusivo en fechas de Santiago.

```sh
/usr/bin/python3 -I -B ./backup_321_audit_daily_coverage.py --from 2026-11-13 --to 2026-11-13 \
  --include-acceptance daily-acceptance-20261113-v2
```

El informe coteja los recibos privados de captura y ambos destinos, mide los tiempos entre capturas verificadas y distingue los días pendientes, fallidos, incompletos, ausentes y sin evidencia suficiente. La opción `--include-acceptance` muestra un ensayo fuera del cálculo diario. Para medir el tiempo desde una captura hasta confirmar ambas copias, usa el intervalo del informe. El primer extremo es el final de la última captura y el segundo, la confirmación de la corrida completa. Para atribuir una corrida a cron, coteja su fecha y hora con el journal. La auditoría del 13 de noviembre confirmó la aceptación y el fallo del ensayo previo a la corrección del sandbox, antes de la primera ejecución programada.

### Verificar archivos desde ambos repositorios

Ejecuta como root en `vm-media-01`, una vez terminada la corrida y su retención.

```sh
/usr/bin/python3 -I -B /opt/media-platform/backup-321-runtime-v3b/backup_321_verify_daily_restore.py \
  --run-id daily-2026-11-15
```

El comando coteja los recibos de la corrida diaria, confirma los dos IDs de repositorio y lee `SHA256SUMS` y los cuatro archivos desde cada snapshot. Usa `restic dump` sin bloqueo ni caché, calcula las huellas en memoria y comprueba el inventario al terminar cada lectura. Devuelve el tiempo y los archivos verificados por destino. También acepta un identificador `daily-acceptance-*` para verificar por separado un ensayo manual. Las pruebas de recuperación lógica de las aplicaciones se ejecutan con sus procedimientos de restauración. [Opciones de restic](https://restic.readthedocs.io/en/stable/manual_rest.html)

Para medir el espacio físico de las corridas, ejecuta en la misma VM y entre capturas activas:

```sh
/usr/bin/python3 -I -B ./backup_321_audit_daily_footprint.py --from 2026-11-13 --to 2026-11-13 \
  --acceptance daily-acceptance-20261113-v2
```

La sonda cuenta los bloques asignados a los productores, al staging y a los recibos de cada corrida, una vez por dispositivo e inodo. Informa también el espacio libre del dispositivo. La aceptación ocupa 38.379.520 bytes y el ensayo fallido 38.322.176 bytes. La retención de R2 actúa sobre los snapshots remotos. Los datos locales de ambas corridas siguen presentes. Recibo `09c14c6c…`.

## Retención en R2

`backup_321_retention.py` simula `forget --dry-run --host vm-media-01 --tag agency-backup-321 --group-by host --keep-within 30d --keep-last 1` con el restic Linux 0.19.1 `agency2` instalado y fijado por SHA-256. La ventana se calcula en UTC desde la fecha del snapshot completo más reciente. Se conservan los snapshots posteriores al límite de 30 días y al menos el último. `newest_age_seconds` permite detectar si la última copia es antigua. La agrupación por host reúne las corridas cuyos cinco paths cambian con cada staging.

Ejecuta la primera simulación como root en `vm-media-01`, entre corridas, desde la instalación que contenga este script y los módulos `backup_321_daily.py` y `backup_321_orchestrator.py` ya verificados.

```sh
/usr/bin/python3 -I -B ./backup_321_retention.py
```

El comando abre el perfil diario privado y su pin en `/var/lib/media-platform/backup-321-daily-config-v1/`. Usa únicamente las credenciales del destino R2 del perfil. Coteja el prefijo exacto `studio-backups-bucket/media-platform-v1/8d9af3d5-7911-4145-8676-064454780857/repository` y el ID del repositorio. Valida las etiquetas `agency-backup-321` y `run:<id>`, el host y los cinco archivos de staging. Con `--no-lock --no-cache` lee la configuración y el inventario de snapshots, simula la selección y compara un segundo inventario completo. El recibo `plan_sha256` fija la política, el binario, el repositorio, el perfil, todo el inventario y los IDs de la selección. El resultado incluye la edad de la última copia y el número de snapshots ajenos a la selección, incluidos los ensayos parciales.

`apply` ejecuta una simulación sin tomar un bloqueo ni escribir cuando la selección está vacía. Si hay candidatos, revisa el plan y verifica desde R2 la restauración de un snapshot conservado de menos de 30 días. Guarda el recibo de esa restauración en un directorio privado root `0700` bajo `/var/lib/media-platform`, con modo `0400` o `0600`. Debe identificar el snapshot R2 restaurado, acreditar `SHA256SUMS` y comprobar las cuatro huellas contra ese manifiesto. Usa esta estructura para el recibo generado a partir de la verificación.

| Campo | Valor |
| --- | --- |
| `schema`, `success`, `destination` | `1`, `true`, `"r2"` |
| `repository_id`, `snapshot_id`, `manifest_sha256` | ID íntegro del repositorio R2 y del snapshot restaurado, y SHA-256 de `SHA256SUMS` |
| `verified_payloads` | Lista con `postgres-apps.sql.zst`, `postgres-immich.sql.zst`, `d1.sql.zst` y `openclaw.tar.zst` |
| `completed_at_utc` | Fecha UTC de la restauración verificada en formato RFC 3339 |

Para generar el recibo, instala `backup_321_verify_r2_restore.py` junto a `backup_321_retention.py` y los módulos del runner. Pasa el ID completo del snapshot diario más reciente entre los conservados, obtenido de la simulación, y una ruta de recibo nueva en el directorio privado.

```sh
/usr/bin/python3 -I -B ./backup_321_verify_r2_restore.py \
  --snapshot-id "$SNAPSHOT_ID" --receipt "$RESTORE_RECEIPT"
```

El verificador coteja el snapshot y los recibos privados de ambos destinos, lee `SHA256SUMS` y las cuatro cargas comprimidas desde R2 mediante `restic dump --no-lock --no-cache` y calcula los hashes en memoria. Las cargas admiten 2 GiB agregados. El proceso escribe el recibo si coinciden el manifiesto, las cuatro huellas y el inventario final. Usa `receipt_sha256` de su salida como `RESTORE_RECEIPT_SHA256`. Esta prueba verifica los bytes restaurados de R2. Las pruebas de recuperación de cada aplicación se ejecutan por separado.

La restauración lógica del volcado de Immich 16.16 posterior al cambio tomó 34,321 s con el archivo ya disponible en la VM. La prueba verificada de D1 en Cloudflare tomó 2,22 s con una base de ensayo y los datos iniciales de la plataforma. Los objetivos de continuidad se miden desde un fallo hasta recuperar el servicio y requieren también descargar el snapshot, recuperar las dependencias y verificar la aplicación. Las dos duraciones son muestras de etapas individuales. La cobertura diaria requiere comparar capturas de fechas consecutivas. [Objetivos de continuidad](../docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md#25-continuidad-operativa-estrategia-de-backups-3-2-1-y-retención-automática)

```sh
/usr/bin/python3 -I -B ./backup_321_retention.py apply \
  --plan-sha256 "$PLAN_SHA256" \
  --restore-receipt "$RESTORE_RECEIPT" \
  --restore-receipt-sha256 "$RESTORE_RECEIPT_SHA256"
```

La aplicación toma el bloqueo local de las copias, recalcula el plan y exige el mismo SHA-256. Restic toma su propio bloqueo y ejecuta `forget` con los IDs completos revisados. El proceso comprueba que el inventario final sólo perdió esos IDs. Si aparece `reconciliation_required`, guarda el resultado y revisa el inventario y los recibos antes de repetir.

La unidad diaria ejecuta `backup_321_daily.py --retention apply`. El runner guarda el recibo de la copia completa en los dos destinos y libera el bloqueo diario antes de iniciar `backup_321_daily_retention.py`. El paso valida la corrida del día en Santiago, simula el plan y comprueba que su snapshot R2 es el diario conservado más reciente. Si hay candidatos, verifica los cinco archivos de ese snapshot y revalida el plan bajo el bloqueo compartido antes de ejecutar `forget`. Con selección vacía, el paso termina correctamente sin ejecutar `forget`. El resultado del respaldo permanece en `result.json` y los del paso posterior, en `retention-apply*.json`. Un fallo de retención devuelve código de salida no cero a systemd. El archivo de intención permite detectar una interrupción y reconciliar el inventario antes de repetir.

En modo `audit`, el paso verifica la restauración de un snapshot diario existente y compara el inventario antes y después, incluso con selección vacía. Los archivos de auditoría quedan en `retention-audit*.json` dentro de la corrida elegida.

```sh
/usr/bin/python3 -I -B ./backup_321_daily_retention.py --run-id "$RUN_ID" --mode audit
```

Los objetos de restic comparten packs entre snapshots. Una regla de expiración por edad de objetos R2 rompería las referencias a packs aún necesarios. `forget` retira las referencias a los snapshots seleccionados. La recuperación de espacio con `prune` abarca todo el repositorio y se evalúa por separado.

## Recolección fresca por componente

1. **PostgreSQL.** `backup_321_capture_postgres.py capture` acepta por stdin JSON con `root`, `run_id`, `scope` y los objetos `postgres-apps` y `postgres-immich`, cada uno con `cid`, `image` y `pgdata`. Instala el Python y su módulo `backup_321_orchestrator.py` junto a los dos capturadores shell fijados. Para esa instalación, el shell de Immich se llama `capture-immich-backup.sh`. El operador valida ambos contenedores saludables por CID completo, imagen de ejecución, proyecto, servicio, UID, bind de PGDATA y época estable. Ejecuta los shell en un directorio nuevo, con un límite de 420 segundos por captura. Los volcados admiten hasta 2 GiB agregados y cada escritura se limita al espacio disponible menos 64 MiB de reserva. Los recibos registran el ámbito validado, la procedencia y los nombres de intercambio `postgres-apps.sql.zst` y `postgres-immich.sql.zst`. El resultado de esta unidad lleva `backup_set_complete: false`.
2. **Fuentes de producción.** Para `scope: "production"`, acredita los dos CID instalados y verifica sus etiquetas y montajes. Immich usa el proyecto `agency-media-immich-lan`, el propietario `agencia-digital-marketing-ai:postgres-immich-lan:v1`, el PGDATA `/var/lib/media-platform/immich-lan-v1/postgres` y el manifiesto `4e2f960d…` con PostgreSQL 16.16 y `vector 0.8.7`. El alcance `acceptance` de Immich conserva el manifiesto `a00b7ac5…`. `postgres-apps` usa el manifiesto `400e83e5…` y el PGDATA `/var/lib/media-platform/kitsu-base-v1/postgres-apps`. Al reemplazar PostgreSQL, actualizar en una ventana con el respaldo diario inactivo la huella del capturador, la fuente en `profile.json` y `profile.sha256`. Ejecutar `backup_321_daily.py --check --retention apply` y verificar una captura con el nuevo contenedor saludable antes del siguiente disparo. [Corte PostgreSQL de Immich](../docker/immich-server/README-LAN.md#corte-postgresql-y-reversión)
3. **D1.** `backup_321_capture_d1.py capture` usa el exportador existente y prepara `d1.sql.zst` en un directorio nuevo. Su recibo acredita el origen, la ventana de captura y las huellas del SQL y del archivo comprimido. La aceptación inicial de D1 contenía los metadatos de propiedad disponibles. [Captura D1 para 3-2-1](#captura-d1-para-3-2-1)
4. **OpenClaw.** Acredita en `.7` la proyección de los cinco agentes propios y las memorias disponibles en el inventario. `openclaw_backup.py capture … agency-state` empaqueta esa proyección y emite su recibo. Acredita el staging de origen y el transporte privado a la VM antes de sumar `openclaw.tar.zst` al conjunto. El contrato `synthetic` tiene ámbito y nombre de archivo diferentes.
5. **Conjunto integral.** `backup_321_staging.py assemble` reúne las cuatro entradas en un nuevo staging privado y genera `SHA256SUMS`. Con la ruta y el SHA del manifiesto de su recibo, pasa `plan` y después `run` del orquestador. [Ensamblado](#ensamblado-del-staging)

### Captura D1 para 3-2-1

Instala `backup_321_capture_d1.py`, `backup_321_orchestrator.py` y `d1_export.py` juntos en una ruta propia de root en `vm-media-01`. Verifica sus huellas. Usa Python 3.12 con HTTPX 0.28.1 y los ejecutables del sistema `/usr/bin/zstd` y `/usr/bin/prlimit`. Prepara un padre de capturas local root `0700` bajo `/var/lib/media-platform` y reserva 193 MiB. Configura el token con el procedimiento privado vigente en un archivo root `0400` o `0600`, de un solo enlace, en otro directorio root `0700` bajo esa misma raíz. El capturador lee un token por archivo, con salto de línea final opcional. Conserva el archivo de credenciales fuera del área de capturas.

Entrega por stdin la solicitud a `python3 -I -B backup_321_capture_d1.py capture`. Este comando inicia una exportación real y comparte el bloqueo exclusivo de las copias 3-2-1. Programa una ventana para el export, que puede bloquear consultas D1. Usa el identificador de cuenta ya respaldado y el UUID de `studio-saas-db` acreditado en el [recibo de exportación](../.tools/evidence/phase4-d1-export-real.json). El UUID de la base temporal de restauración se rechaza antes de consultar Cloudflare.

| Campo | Valor |
| --- | --- |
| `root` | Padre privado de capturas ya preparado |
| `run_id` | Identificador nuevo de hasta 64 caracteres alfanuméricos, guion, punto o guion bajo |
| `account_id` | `CF_ACCOUNT_ID` respaldado, 32 caracteres hexadecimales minúsculos |
| `database_id` | `CF_D1_DATABASE_ID` respaldado de `studio-saas-db` |
| `token_file` | Ruta absoluta al archivo privado del token Cloudflare con acceso a D1 |

La corrida conserva `intent.json`, el SQL y el recibo de exportación privados en `export/`, y el `result.json` de integración. El exportador exige el propietario propio y prueba la restauración SQLite en memoria. El adaptador coteja el recibo, el SHA y el tamaño del SQL, comprime con Zstandard y comprueba que la descompresión reproduce los bytes de origen. Sólo un `result.json` con `success=true` habilita la incorporación de `d1.sql.zst` al staging integral. Revalida los bytes y el SHA del frame al copiarlo y registra `export_receipt_sha256`, `started_at_utc` y `completed_at_utc` para la evidencia conjunta. Si se interrumpe una captura, conserva el directorio para diagnóstico y usa un `run_id` nuevo tras revisar el resultado.

La captura D1 deja `backup_set_complete=false` y `cloud_restore_verified=false`. La aceptación operativa requiere una captura fresca real en la VM, una copia del conjunto de cuatro entradas a ambos destinos y una restauración D1 a una base temporal acreditada con las verificaciones del [procedimiento de D1](README-d1-export.md#aceptación-operativa-inicial--2026-11-06).

### Ensamblado del staging

Instala `backup_321_staging.py` junto a `backup_321_orchestrator.py`, `backup_321_capture_d1.py`, `d1_export.py` y `openclaw_backup.py` en la ruta propia de la VM. Se reutilizan Python 3.12, HTTPX 0.28.1 y los binarios Linux de Zstandard y `prlimit`. Ejecuta `python3 -I -B backup_321_staging.py assemble` como root en `vm-media-01`, con la solicitud JSON por stdin. El comando opera sobre capturas locales y toma el bloqueo compartido con el orquestador.

| Campo | Valor |
| --- | --- |
| `root` | Padre local root `0700` bajo `/var/lib/media-platform`, con espacio para copiar las cuatro entradas más 64 MiB libres |
| `run_id` | Identificador común nuevo para el ensamblado y las capturas de esta corrida |
| `sources` | `postgres-apps` y `postgres-immich` con los `cid`, `image` y `pgdata` definitivos acreditados. `d1_account_id` contiene la cuenta respaldada |
| `postgres` | `receipt` del `result.json` agregado y `sha256` obtenido de forma independiente |
| `d1` | `receipt` del `result.json` del capturador D1 y `sha256` obtenido de forma independiente |
| `openclaw` | `receipt` JSON de `openclaw_backup.py capture`, `sha256` del recibo, `manifest_sha256` del manifiesto acreditado en origen, `hostname` igual a `openclaw-ia`, y `started_at_utc` y `completed_at_utc` de esa operación |

Cada `receipt` es una ruta absoluta bajo `/var/lib/media-platform`, dentro de su propio directorio `<run_id>`, con padre root `0700` y archivo root `0600` de un enlace. Conserva las huellas de los recibos en el canal de evidencia del responsable. Para OpenClaw registra las horas UTC en la operación autenticada de captura y conserva el recibo y el archivo juntos al transportarlos a la VM. El ensamblador coteja el ámbito `agency-state` del recibo con el TAR descomprimido y el manifiesto acreditado. La ventana aceptada va desde 24 horas antes del ensamblado hasta el inicio de éste.

El recibo PostgreSQL definitivo conserva el formato agregado del capturador, con `scope: "production"`, `step: "captured"`, `run_directory` y `run_id`. Dentro de `captures`, la `source` de cada servicio identifica `cid`, `image`, `pgdata`, `pid` y `started`. Los dos payloads mantienen las rutas relativas del capturador original, `postgres-apps/postgres.sql.zst` y `postgres-immich/postgres-immich.sql.zst`. Mantén los recibos anteriores con su alcance original.

El directorio `<root>/<run_id>` conserva `intent.json`, `result.json` y `staging/`. El staging se publica tras verificar bytes, SHA-256 y `SHA256SUMS` con el validador del orquestador. Un fallo previo a la publicación deja `.staging.partial` para diagnóstico. Si hay interrupción, confirma `result.json` con `success=true` antes de usar el staging y conserva el directorio para reconciliar. El ensamblado mantiene intactas las capturas y deja `backup_set_complete=false`. El éxito de la copia depende de ambos destinos, y la recuperación exige restauraciones verificadas.
