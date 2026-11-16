# QuickJS corregido para el runner y el servidor n8n

El contexto Linux amd64 está preparado en `.tools/backup-source-build/n8n-quickjs-wasm-inputs-v1/context-linux-x64.tar.gz`. Contiene 212 archivos regulares y ocupa 6.946.122 bytes. El SHA-256 es `066d2e04ea3c5da743853e27a047febb932cc89d77ea3f5c08c798b151187c7d`. Los WASM publicados por el proveedor siguen en sus tarballs originales V5. El contexto genera salidas nuevas en una carpeta de trabajo independiente.

## Fuente y herramientas fijadas

- La descarga oficial de JITL se fija en `df4efb9ef2cb25c417ecb57986da462d11b244ed` y árbol `f353343a7637a8a132d278d74accb50e2e61aeba`. Se recalcularon las identidades de los directorios y los 653 blobs físicos, incluidos sus modos de ejecutable. El TAR original tiene SHA-256 `b6f342ad8e18bcff29ab4e78467d6e33c53c9e1a47986e58b91a1462beb31a8e`. El commit oficial es público y el proveedor lo publicó sin firma. [Fuente JITL fijada](https://github.com/justjake/quickjs-emscripten/tree/df4efb9ef2cb25c417ecb57986da462d11b244ed), [árbol oficial](https://api.github.com/repos/justjake/quickjs-emscripten/git/trees/f353343a7637a8a132d278d74accb50e2e61aeba?recursive=1).
- `bellard-841dd.patch` conserva los tres fragmentos de la corrección del proveedor y tiene SHA-256 `d019ef46398d2eedcfba892e272f14d6593bdad56f9fc72e6fb6cd410c440104`. La copia original de C se cotejó con SHA-256 `4f15a9e33819635474a9e43aa523f42ea3a6b452bfd8467223fcfbf6e447cc43`. El único C modificado es `vendor/quickjs/quickjs.c`, de 1.964.229 bytes y SHA-256 `0302bbac5694bc1435ff59896a3ba51de7c286d07606e9d0ed9c57ba1a0c2c29`. [Corrección de Bellard](https://github.com/bellard/quickjs/commit/841dd034c2fac02f9ee3c605d8b96a8f8b49c945).
- La clausura Linux amd64 del generador sigue el `yarn.lock` oficial. Contiene `tsx@4.6.2`, `esbuild@0.18.20`, `@esbuild/linux-x64@0.18.20`, `get-tsconfig@4.7.2`, `resolve-pkg-maps@1.0.0`, `fs-extra@10.0.1`, `graceful-fs@4.2.9`, `jsonfile@6.1.0`, `universalify@2.0.0` y `prettier@3.1.1`. Cada tarball se cotejó por SRI SHA-512, SHA-1 del registro y SHA-256 local. Se compararon las aristas del lock, los metadatos del registro y el `package.json` físico. Los binarios opcionales de otras plataformas quedan fuera. Los tarballs se leyeron como datos y se omitió la ejecución de sus scripts de instalación. [Lock del proveedor](https://github.com/justjake/quickjs-emscripten/blob/df4efb9ef2cb25c417ecb57986da462d11b244ed/yarn.lock).
- OSV respondió por los diez paquetes sin páginas pendientes. El único aviso del lote fue `GHSA-67mh-4wv8-2f99` en esbuild 0.18.20, de severidad moderada y relativo al servidor de desarrollo. La receta usa el binario para transformar el generador y carece de una ruta que arranque ese servidor. El informe bruto queda en `osv-response.json`, SHA-256 `10d788e70da720243b8054c031917b015e4c51a7465175d8f277b90375b3ecad`. Esta consulta cubre los paquetes npm de construcción. [Aviso de esbuild](https://github.com/evanw/esbuild/security/advisories/GHSA-67mh-4wv8-2f99).

El recibo de adquisición `acquisition.json` tiene SHA-256 `eb3e5cfdefc5dbb78d95a7410f67c0f292d8e84162bc23b0958908a77de07d8d`. El manifiesto interno `SHA256SUMS` tiene SHA-256 `f1dff1a53ab5b2efd14608705d29c08a3fa0c8f6ba32f99ab315139de0d38423`. El preparador requiere Python 3.12 y PyYAML 6.0.3. Sin `--output` sólo verifica y muestra el SHA-256 esperado. La opción explícita exige un destino nuevo.

Una prueba local extrajo a un temporal los archivos verificados que usa el generador. Transpiló sus dos fuentes TypeScript con la herramienta local y ejecutó en Node 24.13.0 los tres generadores para cada combinación de depuración y Asyncify. Los 12 resultados JSON válidos quedan en el recibo `generator-probe.json` (`bffe54980d61f63ac29e42d639dfd0983e3502fa87c56afc7d395323e64610f4`). Esta prueba cubre el generador y sus dependencias JS. La ejecución del `tsx` y el esbuild ELF fijados queda pendiente en Linux.

```powershell
python -I -B docker/n8n/prepare_quickjs_wasm_inputs.py --materials .tools/backup-source-build/n8n-quickjs-wasm-inputs-v1
```

## Compilación Linux sin red

El operador [run_quickjs_wasm_sdk_v1.py](run_quickjs_wasm_sdk_v1.py) permite
usar el SDK derivado V5 ya auditado, con Node 24.22.3, Emscripten 5.0.1 y
Binaryen 130. Monta el contexto original, el `tsconfig.json` de la fuente JITL
y Closure nativo `20240317.0.0` en `/inputs` de sólo lectura. Monta también
`llvm-dwarfdump` del archivo Linux de Emscripten en su ruta de herramienta,
`/emsdk/upstream/bin/llvm-dwarfdump`, de sólo lectura. El operador verifica
los hashes de los insumos, transpila el generador a CommonJS con el esbuild
fijado y compara sus tres inventarios de símbolos con la prueba previa.
Conserva Closure para las dos variantes de producción y los mapas WASM para
las de depuración. El recibo `quickjs-build.json` registra los 22 archivos.
[SDK derivado](README-WA-SQLITE-SDK-DERIVED.md).

La [sonda](verify-quickjs-wasm-sdk.mjs) se ejecuta sobre una copia del árbol
del servidor o del runner con las salidas incorporadas. Coteja los archivos
de los cuatro destinos y ejecuta los dos cargadores Node por variante.

1. Acreditar el SDK oficial `emscripten/emsdk:5.0.1` antes de iniciar el contenedor. El índice publicado es `sha256:c89732ef63a56de5a96395c5a8c1c7904f7420131a045406e6fedc4cbe1cc198` y el manifiesto Linux amd64 es `sha256:40a30b0e22be2b1fce570a5fcbf8124c4ccb22962b49850546863da07d374a51`. Quedan pendientes la captura de sus capas y el SCA del SDK, que son independientes del lote npm. La imagen oficial incluye Git y tiene un punto de entrada propio. La operación debe arrancar un intérprete explícito, excluir Git de la ruta de ejecución y usar un contexto sin `.git`. [Metadatos de la imagen](https://hub.docker.com/v2/namespaces/emscripten/repositories/emsdk/tags/5.0.1), [receta oficial del SDK](https://github.com/emscripten-core/emsdk/blob/14c18b569f55138fe4963924162244251f454fb0/docker/Dockerfile).
2. Verificar el SHA-256 del contexto antes de extraer. Extraer sólo sus archivos regulares a `/work/workspace` dentro de una raíz privada nueva, con UID y GID `1000`. Mantener las entradas originales selladas. Usar red `none`, sin credenciales ni sockets montados. El SDK se usa por su digest fijado, con raíz de sólo lectura y `/work` y los temporales propios como zonas de escritura.
3. Invocar `/bin/sh /work/workspace/build-quickjs-wasm-offline.sh` con entorno inicial mínimo. La receta comprueba el inventario y exige Emscripten 5.0.1, Node 22.16.0, Closure 20240317.0.0 y esbuild 0.18.20. Invoca el compilador por ruta fija y el generador por Node y `tsx` locales. `make` recibe sólo los objetivos `BROWSER MJS CJS CLOUDFLARE`, uno a uno por variante. Conserva las opciones de depuración y Asyncify del proveedor. [Makefile del proveedor](https://github.com/justjake/quickjs-emscripten/blob/df4efb9ef2cb25c417ecb57986da462d11b244ed/packages/variant-quickjs-wasmfile-release-sync/Makefile), [dependencias de Emscripten 5.0.1](https://github.com/emscripten-core/emscripten/blob/5.0.1/package.json).
4. Capturar los cuatro WASM y los cargadores `CJS`, `MJS`, navegador y Cloudflare. Conservar el inventario de salidas nuevas con sus hashes. Antes de incorporarlas, comprobar interfaces de importación y exportación, el caso de regresión de `TypedArray.prototype.with`, la carga de módulos n8n y la compatibilidad con el runner y el servidor. Publicar en un artefacto derivado nuevo y repetir el SCA.

Google publica un SDK nativo Windows para la revisión `bf32ae8b61ac8efeb7eca01b54c8307f992724f7`. La descarga anunciada pesa 625.943.568 bytes. En este equipo faltan `make` y `emcc` nativos y el SDK Windows sigue sin auditar. La salida de esta unidad es el contexto Linux, con compilación WASM y pruebas de los cargadores pendientes. [Catálogo oficial de Emscripten](https://github.com/emscripten-core/emsdk/blob/14c18b569f55138fe4963924162244251f454fb0/emscripten-releases-tags.json), [SDK Windows publicado](https://storage.googleapis.com/webassembly/emscripten-releases-builds/win/bf32ae8b61ac8efeb7eca01b54c8307f992724f7/wasm-binaries.zip).
