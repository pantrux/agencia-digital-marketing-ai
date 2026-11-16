# Entradas de compilación WASM del servidor n8n

La cápsula `n8n-server-wasm-sdk-v1.tar.gz` está en `.tools/backup-source-build/n8n-server-source-v1`. Mide 9.689.036 bytes y tiene SHA-256 `c4e0a34f048f53bd18fa2ee06b76616dc0319bc5f24d7489cb4afa3529ecc69a`. Conserva los dos archivos TAR originales. Se cotejaron sus manifiestos y los SHA de 45 archivos de wa-sqlite y 212 de QuickJS JITL. La cápsula incluye el operador, la receta de wa-sqlite y el inventario `wasm-sdk-inputs.json`. La receta JITL ya forma parte de su contexto sellado.

El comando local `python -B docker/n8n/stage_server_wasm_sdk.py` reproduce el plan y calcula el SHA sin escribir. `--output <ruta-privada-nueva>/n8n-server-wasm-sdk-v1.tar.gz` permite una publicación nueva. El lock, el store y la cápsula Linux del baseline quedan intactos.

## Adquirir y acreditar el SDK

El registro oficial devolvió el índice `sha256:c89732ef63a56de5a96395c5a8c1c7904f7420131a045406e6fedc4cbe1cc198`, el manifiesto Linux amd64 `sha256:40a30b0e22be2b1fce570a5fcbf8124c4ccb22962b49850546863da07d374a51` y la configuración `sha256:f42d26a27107adf3340b637bd53d6bedd3f8cf55e6fa45d505d843dbdbeea00f`. Se verificaron los SHA de las tres respuestas. Las cinco capas comprimidas declaran 816.517.067 bytes. Los descriptores y los cinco `rootfs.diff_ids` se registran en `.tools/evidence/phase4-n8n-emscripten-sdk-metadata-v1.json`, SHA-256 `0caa3838e229b04832040b2e0a07bb903d9ca6d98a4fe641f28d2fc13c41f8ac`. La lectura de metadatos prescindió de descargar capas. [API del registro](https://distribution.github.io/distribution/spec/api/).

1. Respaldar manifiesto, configuración y recibo. Con espacio libre acreditado en el volumen del daemon, descargar por digest y plataforma exactos.

   ```sh
   ref=docker.io/emscripten/emsdk@sha256:40a30b0e22be2b1fce570a5fcbf8124c4ccb22962b49850546863da07d374a51
   docker image pull --platform linux/amd64 "$ref"
   docker image inspect "$ref"
   ```

   Exigir Linux amd64, `Id` igual al digest de configuración, referencia en `RepoDigests` y `RootFS.Layers` igual a la lista `rootfs_diff_ids` del recibo. Respaldar el ID físico y la prueba de identidad. [Descarga de imágenes por digest](https://docs.docker.com/reference/cli/docker/image/pull/).
2. Auditar por el ID local con el Trivy Linux fijado y una base vigente. Usar `--image-src docker --platform linux/amd64 --cache-backend memory --skip-db-update --skip-java-db-update --offline-scan --scanners vuln --pkg-types os,library --list-all-pkgs --format json` y un reporte nuevo. Conservar el inventario y cobertura de Ubuntu y herramientas SDK. La auditoría del SDK y cualquier decisión de uso se separan de las imágenes productivas.
3. Transferir la cápsula como datos, cotejar el SHA y extraer los cinco miembros regulares en un directorio privado nuevo. Montarlo de sólo lectura en `/inputs`. Crear un `/work` 0700 UID/GID 1000 por etapa. Arrancar el ID acreditado sin red, con raíz de sólo lectura, sin credenciales ni sockets y con `/usr/bin/python3` como punto de entrada explícito. El Dockerfile del proveedor declara GNU Make, Python y Java 11. La receta verifica Emscripten 5.0.1, el Node integrado y la clausura de herramientas de cada etapa. [Dockerfile del SDK fijado](https://github.com/emscripten-core/emsdk/blob/14c18b569f55138fe4963924162244251f454fb0/docker/Dockerfile).

   ```sh
   /usr/bin/python3 -I -B /inputs/stage_server_wasm_sdk.py --stage sqlite
   /usr/bin/python3 -I -B /inputs/stage_server_wasm_sdk.py --stage quickjs
   ```

   Usar un CID nuevo por comando y guardar su recibo antes de arrancar. Capturar las seis salidas de SQLite en `/work/wa-sqlite/dist` y las 20 salidas JITL bajo los cuatro `packages/variant-quickjs-wasmfile-*/dist` de `/work/workspace`. Después aplicar las pruebas de API, OPFS y TypedArray de las [recetas SQLite](README-WA-SQLITE-WASM.md) y [QuickJS](README-QUICKJS-WASM.md). La UI y el sidecar requieren los artefactos corregidos y un lock derivado.

## Materiales pendientes del runtime

- El addon `sqlite3@5.1.7` ya se compiló para Linux amd64 musl mediante `build-sqlite3-native.sh`. Usa los encabezados oficiales de Node 26.12.1, 15 APK verificados y la amalgamación SQLite 3.54.1. Conserva las opciones FTS, RTREE, DBSTAT y funciones matemáticas de upstream. El ELF no requiere `libsqlite3` externa ni glibc. La sonda `verify-sqlite3-native.cjs` valida a través del envoltorio npm real `sqlite_source_id()`, FTS5, parámetros y el rechazo por defecto de `load_extension()` desde SQL. Recibos `377ea3fa…` y `73eb483d…`. [Configuración upstream del addon](https://github.com/TryGhost/node-sqlite3/blob/v5.1.7/binding.gyp).
- El addon `@confluentinc/kafka-javascript@1.9.1` se compiló para Linux amd64 musl y ABI 147 con `BUILD_LIBRDKAFKA=0` y `librdkafka` 2.14.2. Se verificaron las dependencias ELF y la ausencia de glibc y `RPATH`. La sonda de carga abrió y cerró un productor y un consumidor con dos brokers de prueba de librdkafka en loopback. El archivo de salida y su inventario están respaldados. Recibos `92fc43d5…`, `3a2e4b1a…` y `8793a950…`. [Dockerfile upstream n8n](https://github.com/n8n-io/n8n/blob/bb72d756104542adb549b6966a57e21459bd52a2/docker/images/n8n/Dockerfile).
- `isolated-vm@7.0.1` trae un binario Linux x64 musl ABI 147 de SHA-256 `41dd9d63868631c534066b4626bb818f59a4fc7c1d58ff33b9e759c49feb8cb9`. El runner ya acreditó ese prebuild. La imagen servidor deberá seleccionar y probar el mismo ABI en su propio proceso. `agent-browser@0.26.0` trae su ELF musl x64 de SHA-256 `6adf44364db0de3186c562328318ddc21ab388bf19380015c938c418608fe7d3`. Faltan su SCA binario y la prueba con un Chromium local compatible con musl, identificado y auditado. El adaptador acepta `executablePath`. Playwright 1.62.1 declara Chromium revisión 1234, versión 151.0.7922.34, en `browsers.json`. Su versión declarada por sí sola no acredita un navegador compatible con el runtime elegido.
- El grafo productivo del servidor incluye `pac-proxy-agent@7.2.0` a través de Puppeteer. Este paquete alcanza `@tootallnate/quickjs-emscripten@0.23.0` además del JITL 0.32.0 de expresiones. Su WASM inline de 456.202 bytes tiene SHA-256 `64a0922ad271e4b20a4bc238bf1a9fdc0561113789257813b2bfadb3378b10f7` y sello `2021-03-27`. El motor de Bellard incorporó `TypedArray.prototype.with` en 2024-01-13. Este artefacto anterior se conserva como sujeto separado de atribución y SCA. [Changelog QuickJS](https://bellard.org/quickjs/Changelog).

La instalación offline, las pruebas WASM de SQLite, la compilación del editor y del servidor y el arranque privado de n8n con PostgreSQL quedaron acreditados. La receta `Dockerfile.server-runtime` fija el manifiesto de Node para Linux amd64 y repite en la imagen las sondas de npm, SQLite, Canvas, Image, QuickJS y Kafka.

La imagen corregida usa la aplicación V5, con Markdown-it 14.3.3 y el editor recompilado, y el tarball autenticado de npm 11.21.2. Para regenerar su contexto, pasar el archivo V5 y su SHA256 a `prepare_server_runtime_context.py` mediante `--application` y `--application-sha256`. En la VM, se construyó sin red, se archivó y volvió a cargar por el manifiesto `085d1277…`. Trivy examinó 30 paquetes Alpine, 1.916 Node y 94 Cargo, con cero altos o críticos. Una sonda posterior comprobó el render de Markdown, las tres versiones corregidas y los hashes de la interfaz servida por la imagen. Queda repetir el flujo completo con los runners definitivos.
