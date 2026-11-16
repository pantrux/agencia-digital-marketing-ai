# SDK derivado para wa-sqlite

La imagen oficial Emscripten 5.0.1 Linux amd64 quedó descartada. Trivy registró 46 críticos y 855 altos en Ubuntu y 3 críticos y 49 altos en el inventario Node. El informe físico tiene SHA-256 `5da664192b50bb751be697bf63132403b171a77f816fbf7e380b49ed756ba09e`. Las copias críticas de `tar` 6.2.1 y 7.4.3 están bajo el npm de Node 22.16.0. `minimatch` 3.1.2, `picomatch`, `brace-expansion` y las dependencias de Babel aparecen también en el árbol propio de Emscripten. El borrador `n8n_server_wasm_vm.py` permite consultar el plan local. Su entrada operativa está deshabilitada.

## Material físico

- La fuente binaria del proveedor es el XZ Linux x64 de Emscripten 5.0.1, 342.144.156 bytes, SHA-256 `bda6917b630fcc604cca39af433180adfd7195e378075331ceac926553684249`. La generación GCS 1771012553422981 coincide con el recibo de transferencia. El DEPS del release enlaza Emscripten `8c5f43157a3f069ade75876e23061330521eabde`, LLVM `b447f5d9763010f8c6806c578533291aef2bd484` y Binaryen `18ba06162272bc8bbf1fc29b5cb0832dd54becac`. [Asignación de release](https://raw.githubusercontent.com/emscripten-core/emsdk/14c18b569f55138fe4963924162244251f454fb0/emscripten-releases-tags.json), [DEPS fijado](https://chromium.googlesource.com/emscripten-releases/+/bf32ae8b61ac8efeb7eca01b54c8307f992724f7/DEPS), [Archivo Linux por generación](https://storage.googleapis.com/webassembly/emscripten-releases-builds/linux/bf32ae8b61ac8efeb7eca01b54c8307f992724f7/wasm-binaries.tar.xz?generation=1771012553422981).
- El APK ya verificado de Node es `nodejs-24-minimal 24.22.2-r0`, SHA-256 `5c9aa9a5886e5df0bbb226a53da0b4821ebebfd8dc092375e7fa5874e6f50aad`. El ELF `/usr/bin/node` tiene SHA-256 `c9f0030334c1d8f0d84e57ce49ce5afe1381126b14065f279e7e369248e04bb0`. El recibo real `phase4-immich-js-seed-sdk-v1.json` registra su clausura sobre la base Wolfi glibc y Python 3.12.16-r3. El APK GNU Make 4.4.1-r15 ya está local, SHA-256 `543f3d920be217aefc2e423301699bf59e524ff336c01491634f1aab6591ce4d`.
- La compilación sellada genera tres variantes `.mjs` con `-Oz -flto`. Los umbrales de navegador y Node por defecto mantienen desactivada la rama de Babel. El optimizador de esa ruta importa Acorn y Terser vendorizado. Acorn 8.15.0 del XZ debe conservar su manifiesto, módulos y licencia. El lock oficial lo liga al SRI `sha512-NZyJarBfL7nWwIq+FDL6Zp/yHEhePMNnnJ0y3qfieCrmNvYct8uvtiV41UvlSe6apAfk0fY1FbWx+NwfmpvtTg==`. Terser 5.18.2 lleva parches de Emscripten y requiere atribución separada con su licencia y SHA. [Ruta de enlace](https://raw.githubusercontent.com/emscripten-core/emscripten/8c5f43157a3f069ade75876e23061330521eabde/tools/link.py), [Objetivos por defecto](https://raw.githubusercontent.com/emscripten-core/emscripten/8c5f43157a3f069ade75876e23061330521eabde/src/settings.js), [Importaciones del optimizador](https://raw.githubusercontent.com/emscripten-core/emscripten/8c5f43157a3f069ade75876e23061330521eabde/tools/acorn-optimizer.mjs), [Terser vendorizado](https://raw.githubusercontent.com/emscripten-core/emscripten/8c5f43157a3f069ade75876e23061330521eabde/third_party/terser/README.md).

## Ensamblado y verificación

1. Usar la base Wolfi amd64 ya fijada `sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72` con repositorio APK local y firmado. El contexto conserva los 20 APK de la semilla Immich y añade GNU Make. La imagen deberá registrar 40 APK exactos. Los ocho ELF del compilador y Binaryen sólo añaden referencias a bibliotecas de glibc, `libgcc_s` y zlib que ya están en la base. La instalación comprueba físicamente los applets necesarios y la sintaxis `timeout -s TERM -k` de BusyBox.
2. Proyectar bajo `/emsdk/upstream` los binarios seleccionados, los recursos de Clang, los drivers, `src`, `system`, `tools`, sus componentes vendorizados y sólo Acorn en `node_modules`. Conservar SHA y licencias. Excluir el caché de fábrica, bytecode Python, tests, las otras dependencias npm y los programas LLVM/Binaryen ajenos a la etapa. El inventario local `phase4-n8n-emscripten-wa-sqlite-projection-v1.json` registra archivos, modos, enlaces internos y requisitos ELF. Usar la configuración literal de la proyección para `LLVM_ROOT`, `BINARYEN_ROOT` y `NODE_JS`. `EM_CACHE` apunta al directorio de trabajo nuevo.
3. Derivar la receta de SQLite mediante sustituciones cerradas. Cambiar la ruta de Node a `/usr/bin/node`, exigir `v24.22.2` y cotejar el encabezado `5.0.1-git` con la revisión de Emscripten. El XZ conserva ese marcador. El instalador oficial lo reescribe a `5.0.1` al instalar un release. En Wolfi, enlazar el Python 3.12 fijado como `python3` únicamente en el directorio privado de herramientas. El lanzador verifica estrictamente el manifiesto de los 45 archivos antes de extraerlos. La receta vuelve a comprobarlos mediante `sha256sum -c` de BusyBox. La receta Wolfi tiene SHA-256 `81009d527ef0c85d8a97afb5a8b58686d119a2a1a1a2cce437d4fe5e288d6535`. [Versión del compilador](https://raw.githubusercontent.com/emscripten-core/emscripten/8c5f43157a3f069ade75876e23061330521eabde/tools/cmdline.py), [Reescritura durante instalación](https://raw.githubusercontent.com/emscripten-core/emsdk/14c18b569f55138fe4963924162244251f454fb0/emsdk.py).
4. Capturar el manifiesto, la configuración y las capas reales del derivado. Auditar sus APK, Node, Acorn, Terser y los binarios que carezcan de metadatos de paquete mediante inventario explícito y fuentes primarias. La identidad y la cobertura de la auditoría se deben comprobar antes de respaldar la decisión. El inventario estático no ejecuta el cargador ni acredita un arranque.
5. Reutilizar el transporte de la cápsula con SSH fijado, entrada por stdin y recepción EXCL. La operación futura de `create sqlite` exige digest del derivado, ID de configuración, DiffIDs y recibo de auditoría ligados al plan. Respaldar el CID antes de `start sqlite`. Dentro del CID, recorrer físicamente `/emsdk/upstream/emscripten` sin seguir enlaces y rechazar toda entrada `.git` antes de ejecutar `emcc`. Usar sólo loopback, raíz e inputs de sólo lectura, área de trabajo nueva UID 1000, dos CPU, 5 GiB, 128 PID y un máximo de 900 segundos con 15 de cierre. Exigir salida cero y seis artefactos físicos con hashes. Después verificar API, persistencia OPFS y las rutas síncrona, Asyncify y JSPI.

## Contexto y ejecución

El contexto y el recibo de identidad están bajo `.tools/backup-source-build/n8n-server-source-v1/emscripten-toolchain-v1/wa-sqlite-sdk-context-v1`. El archivo `context.tar.gz` mide 193.873.635 bytes y tiene SHA-256 `bef668f43b50f31c7256293a5cc1c5a546423cbee86b6fa6868779aec4b29273`. Se cotejaron de nuevo 6.542 hojas y 379 directorios directamente desde el archivo. El recibo SHA-256 `0e3ae9ef4131a539ed20d27161bec7b2069583652cb591513da37386aa5a5c0d` conserva los hashes de los miembros ajenos a la proyección y vincula la proyección por su inventario. GNU Make se autenticó por el índice Wolfi RSA-SHA256, Q1 y SHA-256 de su payload.

El operador [n8n_wa_sqlite_sdk_build_vm.py](n8n_wa_sqlite_sdk_build_vm.py) muestra el plan sin red. `receive` y `build` requieren ese SHA-256 y un recibo local nuevo. Ambos leen un JSON privado por stdin. `receive` acepta `ssh` y `daemon`. Antes de `build`, respaldar el campo `received` devuelto por la primera etapa. Enviar ese objeto junto con `ssh` y `daemon`. La ruta remota guarda un único archivo comprimido de sólo lectura. La compilación lee ese mismo descriptor y conserva las identidades y logs en el subdirectorio `build`.

```text
python -I -B docker/n8n/n8n_wa_sqlite_sdk_build_vm.py plan
python -I -B docker/n8n/n8n_wa_sqlite_sdk_build_vm.py receive --plan-sha256 <sha> --receipt <recibo-local-nuevo>
python -I -B docker/n8n/n8n_wa_sqlite_sdk_build_vm.py build --plan-sha256 <sha> --receipt <recibo-local-nuevo>
```

Buildx recibe el contexto por stdin. `$wolfi_oci` es el layout OCI propio con manifiesto y configuración de la base ya comprobados. Los tres controles de procedencia se fijan en el entorno efectivo de Buildx.

```sh
BUILDX_GIT_INFO=false BUILDX_GIT_LABELS=false BUILDX_GIT_CHECK_DIRTY=false \
  docker buildx build --network=none --pull=false --platform linux/amd64 \
  --provenance=false --sbom=false --load --metadata-file "$metadata_new" \
  --build-context "wolfi=oci-layout://$wolfi_oci@sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72" \
  --tag "$tag_new" --file Dockerfile - < "$context_tar_gz"
```

Capturar y auditar la imagen por su ID y manifiesto antes de crear el CID de aceptación. `$input_dir` contiene el archivo sellado `wa-sqlite-rebuild-3.54.1.tar.gz` de 2.749.039 bytes y SHA-256 `e9152893ca882710fa5e4c494a7a19356391b214bc1c331f7d9b7b259785d413`. `$work_dir` es un directorio propio vacío de UID/GID 1000 y modo 0700. Su marcador de propiedad queda fuera del bind. El launcher verifica la ausencia física de `.git`, los montajes y el Node exacto y deja el recibo `.sdk-preflight.json` antes de entrar a Make.

```sh
docker create --name "$name_new" --platform linux/amd64 --pull=never \
  --network=none --read-only --init --cap-drop ALL \
  --security-opt no-new-privileges:true --user 1000:1000 \
  --cpus=2 --memory=5g --memory-swap=5g --pids-limit=128 --restart=no \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,uid=1000,gid=1000,mode=0700 \
  --mount "type=bind,src=$input_dir,dst=/inputs,readonly" \
  --mount "type=bind,src=$work_dir,dst=/work" "$sdk_config_id"
```

Respaldar el CID e inspeccionar la imagen, los dos binds y los límites antes de `docker start "$cid"`. Conservar el estado terminal, el log acotado, el preflight y los hashes de las seis salidas. El timeout interno es de 900 segundos con un máximo adicional de 15 para el cierre. La imagen sólo contiene herramientas de construcción. QuickJS queda para una etapa posterior con Closure y sus propias pruebas y auditoría.
