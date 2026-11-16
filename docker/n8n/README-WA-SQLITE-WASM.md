# SQLite del editor de n8n

El editor de n8n 2.46.6 importa `wa-sqlite/dist/wa-sqlite.mjs` desde `packages/frontend/editor-ui/src/app/workers/data/worker.ts`. Registra `AccessHandlePoolVFS` y abre la base con `open_v2`. Los tres WASM del paquete sellado contienen SQLite 3.50.1 y su identificador de fuente. El delta mantiene las tres variantes y conserva el API JavaScript y la licencia MIT de wa-sqlite. [Fuente de wa-sqlite fijada](https://github.com/rhashimoto/wa-sqlite/tree/779219540f66cecaa159da32b3b8936697ba10a7).

## Avisos e insumos

- SQLite atribuye CVE-2025-6965 a consultas SQL controladas por el atacante y registra la corrección en 3.50.2. CVE-2025-7709 requiere una base FTS5 manipulada y se corrigió en 3.50.3. CVE-2026-11822 y CVE-2026-11824 afectan FTS5 en las condiciones que describe SQLite y se corrigieron en 3.53.2. CVE-2025-70873 afecta el módulo `zipfile` de la CLI y se corrigió en 3.52.0. El Makefile de esta distribución omite la carga dinámica, no habilita FTS5 y no incluye el módulo `zipfile`. La versión anterior por sí sola no demuestra que cada aviso sea explotable en el editor. Verificar las opciones reales mediante `PRAGMA compile_options` después del build. [Avisos de SQLite](https://www.sqlite.org/cves.html), [Makefile fijado](https://github.com/rhashimoto/wa-sqlite/blob/779219540f66cecaa159da32b3b8936697ba10a7/Makefile).
- La amalgamación oficial 3.54.1 mide 2.990.966 bytes y tiene SHA-256 `0a9c8e34352dad46006aa82836d5383bff9dc6bc9ce5a60553c05aa30e6ad1f9`. El `SQLITE_SOURCE_ID` oficial es `2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76`. La extensión matemática se conserva con SHA3-256 `ee39ddf5eaa21e1d0ebcbceeab42822dd0c4f82d8039ce173fd4814807faabfa`, declarado en el Makefile. [SQLite 3.54.1](https://www.sqlite.org/releaselog/3_54_1.html), [amalgamación oficial](https://www.sqlite.org/2026/sqlite-amalgamation-3540100.zip).
- El contexto de datos ya existe bajo `.tools/backup-source-build/n8n-server-source-v1/wa-sqlite-rebuild-3.54.1.tar.gz`. Tiene 45 archivos regulares, mide 2.749.039 bytes y su SHA-256 es `e9152893ca882710fa5e4c494a7a19356391b214bc1c331f7d9b7b259785d413`. El tarball npm original `wa-sqlite-1.0.9-upstream-nogit.tgz` tiene 57 miembros y SHA-256 `16dcd4442cb7073182c1eb2951e495c4415f7de8924d188fafb670a8798da662`. El empaquetador vuelve a cotejar ambos archivos. La cápsula de transferencia y el lock de referencia quedan intactos.

## Compilación sin red

1. Usar la ruta del [SDK derivado para wa-sqlite](README-WA-SQLITE-SDK-DERIVED.md). El alcance de la aceptación se liga a una imagen y su auditoría por digest. La CI de wa-sqlite usó Emscripten 3.1.61. En 5.0.1, el analizador transforma `JSPI` en `ASYNCIFY=2`, conserva `ASYNCIFY_IMPORTS` y lleva `JSPI_EXPORTS` a la lista de funciones que devuelven promesas. El puente wa-sqlite declara `__async` y usa el ABI legalizado con `WASM_BIGINT=0`. La compatibilidad con el SDK derivado requiere la prueba operativa. [CI del proveedor](https://github.com/rhashimoto/wa-sqlite/blob/779219540f66cecaa159da32b3b8936697ba10a7/.github/workflows/ci.yml), [opciones Emscripten 5.0.1](https://github.com/emscripten-core/emscripten/blob/5.0.1/tools/cmdline.py), [adaptadores](https://github.com/rhashimoto/wa-sqlite/blob/779219540f66cecaa159da32b3b8936697ba10a7/src/libadapters.js).
2. El lanzador autentica el TAR en `/inputs` de sólo lectura y materializa sus 45 archivos en `/work/wa-sqlite`. El CID usa raíz de sólo lectura, UID/GID 1000, red `none` y un límite de 900 segundos antes de la limpieza. La sonda coteja el Node fijado y recorre el árbol Emscripten para rechazar una entrada `.git` antes de invocar Make.
3. GNU Make genera `dist` con las tres fuentes externas fijadas y sus reglas de descarga desactivadas mediante `-o`. La sonda Node instancia las tres parejas JS/WASM, registra sus huellas ABI y comprueba versión, identificador de fuente, opciones de compilación, una consulta parametrizada y el VFS asíncrono de Asyncify y JSPI. Se cotejan de nuevo los hashes físicos de las seis salidas al leer el CID detenido. OPFS sigue en la aceptación de navegador.

## Preparar el paquete derivado

```powershell
python -B docker/n8n/pack_wa_sqlite_wasm_delta.py --materials .tools/backup-source-build/n8n-server-source-v1
```

Ese comando es de lectura y muestra los insumos verificados. Tras capturar y cotejar `dist` con el recibo de build, repetirlo con `--dist <dist-privado>` para calcular el delta. Agregar `--output <raiz-privada>/wa-sqlite-1.0.9-sqlite3.54.1.tgz` sólo al publicar en una carpeta de trabajo propia. La creación es exclusiva. El recibo registra el SHA anterior y nuevo de las seis entradas, el SRI nuevo y las 51 entradas conservadas. El nombre y versión del paquete en `package.json` se mantienen. La verificación de cabecera, marcadores de SQLite y cargadores es estática.

Integrar sólo en una copia nueva del servidor. Cambiar la entrada local de wa-sqlite en `packages/frontend/editor-ui/package.json`, regenerar el lock sin red con los metadatos autenticados y comprobar que las únicas diferencias semánticas del lock sean la referencia local y su integridad. Ejecutar de nuevo la política offline. El lock de referencia y la cápsula quedan como insumos inmutables.

## Delta SQLite verificado

La aceptación del SDK con Node 24.22.3 terminó con código 0 para SQLite 3.54.1. Verificó las tres variantes y sus seis salidas. `export_wa_sqlite_dist_vm.py plan` fija la transferencia al CID detenido y a los tamaños y SHA-256 del recibo de aceptación. `export` lee por stdin el DTO privado con `ssh` y `daemon` y escribe las seis salidas en un directorio nuevo.

Con `dist` disponible, ejecutar el empaquetador y validar el lock derivado en un directorio nuevo.

```powershell
python -B .tools/evidence/prepare_n8n_wa_sqlite_server_delta.py `
  --dist .tools/backup-source-build/n8n-server-source-v1/wa-sqlite-output-delta-v5/dist `
  --output .tools/backup-source-build/n8n-server-source-v1/wa-sqlite-server-delta-v1
```

El resultado conserva 51 entradas del paquete original y cambia seis. pnpm 12.4.2 verificó 3.476 entradas de política en los 80 proyectos sin conexión. La resolución y la comprobación congelada conservaron el grafo de dependencias de registro. El lock resultante tiene SHA-256 `f49f5522634b6e5eaf742cd31b50c0eb35489142cf1cd3c65e6ed0fc8d56e392`. El paquete `wa-sqlite-1.0.9-sqlite3.54.1.tgz` tiene SHA-256 `f777c87f5809534b0e1ae79abb8a5f7656614063da7599c82e926085ec5b458b`.

Para el ensayo Linux, transferir `wa-sqlite-server-overlay-v1.tar.gz` desde el directorio de salida. Tiene 1.635.622 bytes y SHA-256 `578ce1e81213224e3028759340a8a32be6e060f23f082d84a8a704eb51e078f8`. Cotejar el archivo y su `SHA256SUMS` antes de sustituir el lock, el manifiesto del editor y el tarball del vendor en un workspace nuevo. Reusar los insumos del baseline y su almacén de contenido con una vista de escritura privada. El índice SQLite del store original tiene 3.125 entradas y SHA-256 `9f134672f2ec03d43bfd7809e837b83154ad4f1c2ccc4e2410d5aa35a3c42d14`. Sembrar únicamente el tarball nuevo con pnpm en esa vista privada, conservando la referencia `file:vendor/wa-sqlite-1.0.9-sqlite3.54.1.tgz`. Verificar que el índice sólo añade la entrada local esperada y que la instalación completa usa el lock congelado, se realiza sin red externa y registra cero descargas después de la siembra.

## Prueba en navegador

Chrome headless ejecutó el worker dedicado de n8n 2.46.6 con sus módulos originales de consultas, esquema y versión. La sonda transpila TypeScript y reescribe los imports para servirlos en un origen loopback con perfil propio. Sustituye `loadNodeTypes` por un módulo que falla si se invoca, porque la prueba prescinde de la API del servidor. Sirve el paquete SQLite 3.54.1 derivado y Comlink 4.4.1 desde insumos fijados.

```powershell
python -I -B .tools/evidence/probe_n8n_wa_sqlite_browser.py `
  --chrome 'C:/Program Files/Google/Chrome/Application/chrome.exe' `
  --node 'C:/Program Files/nodejs/node.exe' `
  --output <directorio-nuevo>
```

El navegador cotejó el SHA-256 del WASM síncrono, la versión y el identificador de fuente de SQLite y las opciones de compilación. El primer worker escribió una fila mediante parámetros enlazados. Tras cerrar la base y terminar ese worker, un segundo worker leyó la fila mediante `AccessHandlePoolVFS` de OPFS. El recibo `wa-sqlite-browser-opfs-v1/receipt.json` tiene SHA-256 `3054ad67229272ae5728afb19e6863a39ff4a636946d706c5ea9b7f6c38a0e95`.

Quedan pendientes el flujo del editor compilado y la publicación. La prueba funcional en Node cubrió Asyncify y JSPI. La prueba de navegador cubrió la variante síncrona que usa el editor.
