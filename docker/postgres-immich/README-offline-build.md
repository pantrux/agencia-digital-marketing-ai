# Preflight offline del constructor musl

Esta unidad prepara **sólo** el contrato de un contenedor temporal y las etapas
`hold`, `sdk` y `graph`. No ejecuta Docker, SDK o compiladores durante su
preparación. No contiene la imagen runtime ni habilita `compile`. La ausencia
de `wasm-bindgen-shared` en el grafo real todavía está pendiente.

## Entradas verificadas y respaldo

El manifiesto original conserva SHA-256
`8d5a521ef8b2827f5198b9dcb5148c05f0aad743c7ce39912c75491139f5f745`.
El lock nativo es `a805dd0dd8bae497bf8326e48f1edecbce72e3588d857d314330b90638c1cd41`
y el lock vendor es `6bd7c4db4c0d297da3a073939f6e78c6d7837bc65d2a245053dac531f9199c57`.
No se alteran esas preparaciones.

La consulta local siguiente lee únicamente archivos públicos preparados y
comprueba sus hashes; no copia nada a infraestructura ni ejecuta herramientas
descargadas:

```powershell
python docker/postgres-immich/offline_recipe.py inventory > .tools/evidence/phase4-postgres-immich-offline-inputs.json
```

El resultado contiene los 78 archivos del kit, sus orígenes locales, tamaños,
SHA-256 y el texto exacto de `SHA256SUMS`. Incluye 65 APK, tres componentes Rust,
fuentes originales de VectorChord y pgvector, SQLite 3.54.1, vendor, locks y los
tres archivos operativos de esta receta. Los nombres destino son fijos. No hay
descargas por parte del contenedor, credenciales, NAS o PGDATA de producción.

La raíz debe respaldar los hashes del kit, script y parche, las rutas remotas,
el ID de construcción y el marcador antes de transferir o utilizar recursos.
El SHA de `SHA256SUMS` se inyectará como `AGENCY_INPUT_MANIFEST_SHA256`; es un
pin público, no un secreto. `AGENCY_BUILD_OWNER` debe ser exactamente
`agencia-digital-marketing-ai:postgres-immich-builder:v1`.

## Un contenedor temporal, con límites propios

`container_plan()` admite exclusivamente rutas hermanadas por el mismo ID
hexadecimal de 32 caracteres:

```text
/opt/media-platform/build/contexts/postgres-immich/<id>/inputs
/var/lib/media-platform/build/postgres-immich/<id>/outputs
```

El controlador debe acreditar previamente la propiedad y los marcadores de
esos directorios. La salida será root:0700 y su `.owner`, root:0600, con el
marcador exacto y sin enlaces. La entrada se monta en `/inputs` de sólo
lectura; la salida privada se monta en `/outputs`. `/work` pertenece a la capa
escribible desechable del contenedor y conserva SDK y fuentes entre etapas.

El plan usa la base amd64 PostgreSQL 16.15 Alpine fijada por digest, `--pull
never`, `--network none`, cuatro CPU, `--memory 12g`, `--memory-swap 12g` y
`--pids-limit 256`. Según la [documentación de Docker](https://docs.docker.com/engine/containers/resource_constraints/),
igualar memoria y memoria+swap deshabilita el uso adicional de swap; no otorga
24 GiB. No desactiva el OOM killer. Retira NET_RAW y activa
`no-new-privileges`, sin modo privilegiado, puertos, dispositivos o socket
Docker. Conserva las capacidades ordinarias necesarias para instalar APK en
su propia capa; no escribe en el sistema de la VM.

`docker create` [crea sin arrancar](https://docs.docker.com/reference/cli/docker/container/create/).
El controlador debe conservar el CID real, acreditar imagen, etiquetas,
montajes y límites mediante inspección y **respaldar el CID antes de start**.
No adopta nombres existentes ni ejecuta el conjunto de pasos automáticamente.
La limpieza posterior sólo podrá actuar sobre ese CID propio verificado.

El entrypoint fijo es `/bin/sh /inputs/offline-container.sh hold`. No contiene
servidores ni escucha de red. Tiene vida máxima de 7200 segundos. El
controlador debe imponer también plazos por etapa (SDK: 900 segundos; grafo:
180 segundos), drenar logs acotados y terminar el contenedor propio si vence
una etapa; un timeout del cliente Docker no acredita que sus procesos hayan
terminado. Estos controles pertenecen al controlador remoto, todavía separado
de esta receta.

## Etapas y evidencia

Tras el respaldo y revisión de la raíz, las invocaciones fijas dentro del
**mismo CID** serán `/bin/sh /inputs/offline-container.sh sdk` y luego
`/bin/sh /inputs/offline-container.sh graph`. No se han ejecutado aquí.

La etapa SDK valida `SHA256SUMS`, exige el hash original de la base APK y
ejecutará `apk add --no-network --no-cache` con los 65 archivos exactos.
Comparará las versiones de todos los paquetes base y rechazará paquetes
adicionales. No usa `--allow-untrusted`, `apk update`, upgrade, Git o gcompat.
Desempaqueta los tarballs fijados sólo dentro del contenedor y ejecutará los
tres instaladores oficiales con prefijo `/opt/agency-rust` y
`--disable-ldconfig`. Acreditará la versión efectiva de rustc y el commit de
Cargo; el paquete Cargo se publica como 0.101.0-beta.10 y el texto efectivo de
su CLI se conserva, sin inventar su correspondencia de versión.

VectorChord se desempaqueta en `/work/source`, el vendor en `/work/vendor` y
Cargo usa `/work/cargo-home` y `/work/target`. La configuración propia reemplaza
crates.io por ese vendor y fuerza offline. Se preservan los defaults upstream
`simd/init`, el destino `x86_64-unknown-linux-musl` y `--features pg16`.
`PG_CONFIG` y `PGRX_PG_CONFIG_PATH` son `/usr/local/bin/pg_config` de la base;
Clang/LLVM corresponden a la versión 21 ya auditada. No se instala otro
`postgresql16-dev`. Se eliminan runners y wrappers de compilador heredados.

El parche `xtask-offline.patch` cambia únicamente las cuatro llamadas Cargo:
metadata, compilación de biblioteca, generación SQL y clippy. SHA original:
`748e21eac842f629d236958227b2bcf35498dc1479f28f37f9825bf7c6a3f3fd`;
SHA modificado:
`9055ec9b313bcc827c52705f37bca6ceba4468de2073cb5de319e703684969b1`;
parche:
`295124d77f90169a58adbee3e2b8e98f1770de669e023738c1ba9098d3b5c500`.
La aplicación acotada exige ambos hashes y cuatro coincidencias exactas.
Sólo modifica la copia de trabajo; no altera vendor, Cargo.lock ni fuentes
originales descargadas. `--frozen` [equivale a locked y offline](https://doc.rust-lang.org/cargo/commands/cargo-vendor.html).

La etapa `graph` ejecutará dos árboles `cargo tree --frozen --target
x86_64-unknown-linux-musl --edges normal,build`: raíces vchord con pg16 y
xtask. Rechaza paquetes ajenos al lock, fuentes extrañas, salida incompleta,
controles de terminal y selección de `wasm-bindgen-shared`, `wasm-bindgen` o
git2. Exige rusqlite, libsqlite3-sys y pgrx en las dependencias normales/build
esperadas. Conserva además el árbol con `{f}` para revisar explícitamente
features pg16, simd y SQLite. No poda nodos para obtener una aprobación.

El [manual de Cargo](https://doc.rust-lang.org/cargo/commands/cargo-tree.html)
advierte que ese árbol aproxima la compilación, sin garantizar equivalencia
absoluta. El recibo acredita sólo la selección observada; la revisión de
fuentes y los controles de ejecución siguen siendo necesarios. Si aparece
wasm-bindgen-shared, se detiene antes de ejecutar su build.rs. Hará falta un
parche explícito independiente; no se modifica el vendor ni se falsea su
checksum.

Los recibos `sdk-receipt.json` y `graph-receipt.json` se crean sin sobrescribir.
Se conserva el hash de Cargo.lock, las versiones reales, los árboles y su
hash, el recibo SDK previo y el pin del kit. Un fallo deja la etapa marcada
como iniciada y exige diagnóstico; no se reintenta sobre estado parcial.
La raíz debe revisar las features y respaldar el hash del recibo del grafo
antes del incremento que habilite compilación.

## Siguiente incremento, aún no implementado

La compilación deberá crear SQLite 3.54.1 propia desde el ZIP fijado, con PIC,
SONAME estándar, FTS5 y las opciones de la ruta bundled revisadas. Se usará
pkg-config con `LIBSQLITE3_SYS_USE_PKG_CONFIG=1`, `SQLITE3_STATIC=0` y prefijo
`/opt/agency-sqlite`; la feature bundled puede seguir presente en Cargo sin
compilar su amalgamación 3.51.1. Habrá que probar versión, opciones, WAL,
bindings, `DT_NEEDED` y biblioteca realmente cargada. Nada de esto se acredita
por establecer variables. pgvector 0.8.5 usará PGXS de la base y `OPTFLAGS=`;
VectorChord 1.1.1 conservará el lock y sus procesos Cargo frozen.

La imagen final será otro incremento breve con COPY de outputs verificados,
licencias y recibos, retirada real de gosu y su alias con los mismos hashes de
`postgres-apps`, entrypoints oficiales intactos y `USER 70:70`. No incorporará
SDK, compiladores ni cachés. Quedan pendientes pruebas reales ABI, SQL,
recorder, reinicio y SCA final. Estructura válida y fixtures no equivalen a
una imagen lista para producción.
