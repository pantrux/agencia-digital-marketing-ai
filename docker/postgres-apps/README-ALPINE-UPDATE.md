# Actualización Alpine de PostgreSQL apps

El escaneo del 13 de noviembre de 2026 detectó 19 HIGH y 7 MEDIUM en la imagen
PostgreSQL apps. Los tres paquetes corregidos están publicados para `v3.24`,
repositorio `main`, arquitectura `x86_64`. La actualización conserva la base
de 45 APK, el usuario `70:70` y los scripts de arranque originales.
[Índice Alpine firmado](https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/APKINDEX.tar.gz).

| Paquete | Instalada | Corregida | Hallazgos que cubre |
| --- | --- | --- | --- |
| `krb5-libs` | `1.22.2-r1` | `1.22.3-r0` | 16 HIGH y 5 MEDIUM |
| `libxslt` | `1.1.43-r3` | `1.1.43-r4` | 3 HIGH y 1 MEDIUM |
| `zlib` | `1.3.2-r1` | `1.4-r0` | 1 MEDIUM |

## Materializar y verificar los insumos

En el workspace están los APK, el índice firmado y el recibo en
`.tools/backup-source-build/postgres-apps-alpine-update-20261113-v1/`.
El verificador compara las firmas con la clave RSA oficial `6165ee59`, las
identidades `Q1` con el índice y el `datahash` de cada payload. Confirmó que
los tres APK carecen de scripts de instalación y que los 17 requisitos
afectados siguen resueltos con los paquetes instalados. La comprobación
incluye los SONAME que consume `.postgresql-rundeps`.
[Clave oficial Alpine](https://alpinelinux.org/keys/alpine-devel@lists.alpinelinux.org-6165ee59.rsa.pub).

Para repetir la preparación en el workspace con Python y `cryptography`,
copiar `main-APKINDEX.tar.gz` y los tres APK a una carpeta nueva. El
verificador espera los bytes exactos de la captura. Los nombres de archivo
coinciden con los paquetes del directorio oficial de la rama.

```powershell
python -I -B docker/postgres-apps/prepare_alpine_update.py `
  --base .tools/cache/phase4-built-images/postgres-apps-c908ada03479b540.tar `
  --inputs $captura `
  --key '.tools/postgres-immich-build/apk-sdk-inspection/alpine-devel@lists.alpinelinux.org-6165ee59.rsa.pub' `
  --output "$captura/apk-inputs"
```

El manifiesto `apk-inputs/SHA256SUMS` debe dar
`667a7af496086dec9e19c61d490eb2dd426b547b6c2cdc5b6bb86e02cc7fa9e5`.

## Construcción y comprobación en Linux

Verificar que `postgres_apps_base` apunta a la imagen local de manifiesto
`sha256:c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a` y
configuración `sha256:22fdd47fd045fefc8c744ff00f801c6efe0e99cab9eb67e1751712760974316c`.
`context_dir` contiene `Dockerfile.alpine-update` y `.dockerignore`.
`apk_inputs` apunta al directorio preparado. Fijar `candidate_tag` a un nombre
local nuevo y mantener la imagen anterior para verificar el cambio.

```bash
docker build --platform linux/amd64 --network=none --pull=false \
  --build-arg "POSTGRES_APPS_BASE=$postgres_apps_base" \
  --build-context "apk_inputs=$apk_inputs" \
  --file "$context_dir/Dockerfile.alpine-update" \
  --tag "$candidate_tag" "$context_dir"
```

La receta ejecuta `apk verify` y la instalación local de los tres archivos
con `--no-network`, sin scripts ni hooks. Compara los inventarios previo y
posterior, los dos scripts de arranque y la versión 16.15 del servidor. Tras
construir, repetir Trivy sobre el OCI exacto, exigir cero HIGH/CRITICAL y
comprobar la aceptación SQL con un directorio de datos nuevo antes de
seleccionarlo para SupoClip. El análisis local sólo verificó los insumos y las
dependencias. La construcción, el escaneo de la imagen nueva y el arranque
PostgreSQL quedan por ejecutar.

## Transporte con poco espacio en el builder

El paquete `transport-v2.tar.gz` contiene sólo la receta, el operador y los
insumos APK. Tiene 951.350 bytes y SHA256
`13fc47701904ca6b30d8336aaf65f19989be6ae43b0457201e8684d326ef8994`.
Comprobar ese SHA256 antes de extraerlo en el directorio nuevo del builder
Linux. Conservar el archivo `TRANSFER.sha256` junto a
los directorios `context` y `apk-inputs`. El builder Docker predeterminado
debe ser el Linux amd64 existente y usar el socket local. Revisar el espacio
disponible en el staging y en `DockerRootDir` antes del build. El operador
registra `df` y guarda el log y el ID de la imagen en `result/`.

```bash
/bin/sh "$stage/run_alpine_update.sh" build "$stage" \
  media-platform/postgres-apps:fase4-postgres-20261106-01 "$candidate_tag"
```

Si el builder existente carece de la base, verificar primero el SHA256 del
archivo `postgres-apps-c908ada03479b540.tar`,
`a4da25a508d05ee0f3b90511915446d81c8f75c3dfe74c8cb9b0cf1e1da8a71d`.
`docker --context default image load` acepta el TAR por stdin, sin guardar
una copia en el builder. Este TAR no contiene tag. Después de cargarlo,
inspeccionar el ID del manifiesto o configuración indicados arriba y usar
`docker image tag` con una referencia nueva que el operador pueda validar.
La carga necesita espacio para materializar la base en el almacén Docker.
[Docker image load](https://docs.docker.com/reference/cli/docker/image/load/).

Para traer el resultado, ejecutar el modo `export` sobre una conexión SSH
binaria sin terminal. Envía `docker image save` a stdout. El cliente guarda
el TAR en su disco local y recibe además `result/candidate.id`,
`result/build-metadata.json`, `result/build.log` y `result/free-space.txt`.
El modo `export` vuelve a comprobar el ID antes de emitir los bytes. El
build conserva sus capas en Docker, pero la exportación no necesita otro
TAR de imagen en la VM. [Docker image save](https://docs.docker.com/reference/cli/docker/image/save/).

```bash
/bin/sh "$stage/run_alpine_update.sh" export "$stage" \
  media-platform/postgres-apps:fase4-postgres-20261106-01 "$candidate_tag"
```

En Windows, guardar stdout con un cliente que maneje bytes, como Python
`subprocess` con un archivo abierto en `xb` para `stdout`, y conservar
stderr por separado. Cerrar y validar el TAR antes de escanearlo. Evitar
redirección de binarios mediante PowerShell 5.1.

## Reescaneo local del OCI exportado

El siguiente ejemplo usa Trivy 0.76.2 y la DB del 13 de noviembre preservada
en el workspace. `$resultado` es el directorio local nuevo que contiene
`candidate.tar` y `candidate.id`. La opción `--list-all-pkgs` permite cotejar
el inventario completo. Para repetir el análisis en otra fecha, fijar la DB
vigente y sus metadatos en un recibo nuevo.

```powershell
$cache = (Resolve-Path .tools/trivy-cache-current-20261113).Path
$env:TMP = Join-Path $cache 'tmp'
$env:TEMP = $env:TMP
& .tools/bin/trivy.exe --cache-dir $cache image --input "$resultado/candidate.tar" `
  --offline-scan --skip-db-update --skip-java-db-update --cache-backend memory `
  --scanners vuln --format json --list-all-pkgs --output "$resultado/trivy.json"
if ($LASTEXITCODE -ne 0) { throw 'Falló el escaneo local' }

python -I -B docker/postgres-apps/verify_alpine_candidate.py `
  --base .tools/cache/phase4-built-images/postgres-apps-c908ada03479b540.tar `
  --candidate "$resultado/candidate.tar" --candidate-id "$resultado/candidate.id" `
  --report "$resultado/trivy.json" --db "$cache/db/trivy.db" `
  --db-metadata "$cache/db/metadata.json" `
  --expected .tools/backup-source-build/postgres-apps-alpine-update-20261113-v1/apk-inputs/after.lock `
  --receipt "$resultado/receipt.json"
```

El verificador coteja las capas OCI, la configuración de ejecución, el ID
recibido y las 45 identidades APK. Conserva el recibo y devuelve código 1 si
aparecen HIGH o CRITICAL. La aceptación SQL se ejecuta después, en una base
desechable propia.
