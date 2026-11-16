# Corrección de la copia asíncrona de Flatted en n8n

La captura del runner compilado confirma que `flatted-async.js` indexa el arreglo con una cadena sin validación y resuelve las referencias mediante recursión. `parse-flatted.js` selecciona ese módulo para entradas de al menos 1.048.576 unidades UTF-16. El recibo `phase4-n8n-js-deploy-flatted-capture-v1.json` (`19dad16dd4d978e70e59db65e8ab3f7e44b81d29d559aa694789a4ea42ba52fc`) liga ambos archivos al inventario físico `ae680f93…`. Los hashes de `flatted-async.js` y `parse-flatted.js` son `c3b9a667bbdf61834ff813d734e62d5fc16a46b72131f597a5decb3945d2624b` y `732daf05ee0c3310fb958967d6691cdaea5068f69f2cf420ad719b656387be8a`, respectivamente.

El fragmento de corrección adopta la resolución iterativa de Flatted 3.4.0 y el acceso numérico de 3.4.2. Exige además un índice decimal canónico, seguro y propio del arreglo. Una referencia inválida queda como `undefined`. Los objetos y arreglos compartidos, los ciclos y los valores primitivos válidos se conservan. La preparación de cadenas también se hace de forma iterativa. El fragmento incluye el aviso ISC original de Andrea Giammarchi. [CVE-2026-32141 y corrección 3.4.0](https://github.com/WebReflection/flatted/security/advisories/GHSA-25h7-pfq9-p65f), [fuente 3.4.0](https://github.com/WebReflection/flatted/blob/v3.4.0/esm/index.js), [CVE-2026-33228 y corrección 3.4.2](https://github.com/WebReflection/flatted/security/advisories/GHSA-rf6f-7fwh-wjgh), [fuente 3.4.2](https://github.com/WebReflection/flatted/blob/v3.4.2/esm/index.js).

## Derivación y reconstrucción

El generador valida fuente y fragmento por SHA-256. Preserva los bytes de la cabecera y de la función de lectura en flujo y exige que el archivo de salida sea nuevo. Omitir `--output` sólo verifica las entradas y muestra el resultado esperado.

```powershell
python -I -B docker/n8n/apply_flatted_async_security.py --source-root <arbol-fijado> --output <archivo-ts-nuevo>
node docker/n8n/test_flatted_async_security.mjs <archivo-ts-nuevo>
```

| Material | SHA-256 |
| --- | --- |
| Fuente n8n original, 4.556 bytes | `86ef3a641fde6231eaf4d8b70eb8298aed0a71f7e1bbf5f094353b002ccfc20e` |
| `flatted-async-resolver-v1.ts`, 2.512 bytes | `c6105456d189f94252eda827ea68a6ed47f277040f6615723f88a3b85eeb55fd` |
| `flatted-async.ts` corregido, 4.217 bytes | `09ef205769d914b509ad625daf532cfb02900ada03ce930feed4047d01cd3b6f` |

1. Crear un árbol de compilación nuevo para el runner y otro para el servidor a partir de sus fuentes fijadas. Generar el archivo en una carpeta de preparación y verificar el SHA-256 resultante.
2. Aplicar el archivo sólo en `packages/@n8n/backend-common/src/utils/flatted-async.ts` de cada árbol nuevo. Registrar el cambio en el manifiesto derivado. El lock y el almacén de dependencias siguen siendo las entradas fijadas.
3. En el runner, compilar en el mismo orden del procedimiento existente, que compila `backend-common` antes de `core` y del runner. Ejecutar `deploy` hacia un destino nuevo y capturar de nuevo ambos JavaScript junto al inventario. En el servidor, aplicar el mismo cambio antes de compilar `backend-common` y sus consumidores y comprobar los archivos publicados en su imagen propia.
4. Verificar regresiones y repetir el SCA del artefacto final. La compilación y la aceptación de los nuevos artefactos quedan pendientes.

En Node 24.13.0, la fuente original falló tres de cinco pruebas. La corregida pasó las cinco y dos pruebas adicionales del analizador de flujo real con `stream-json@1.9.1` y `stream-chain@2.2.5` extraídos en temporal de los tarballs fijados. Para incluir ambas pruebas de flujo, pasar como segundo argumento de la prueba el directorio padre de `node_modules` que contiene esas dos versiones verificadas. Se verificaron cadenas y anidamiento de 16.384 niveles, ciclos, Unicode BMP a través de varios bloques y errores de sintaxis. El lector de flujo heredado puede separar pares sustitutos UTF-16 en un límite de bloque, observado con un emoji en una prueba aislada de Node. Este hallazgo queda separado de las dos correcciones de Flatted y del aviso de `stream-json` relativo a `Assembler`.

## Fuentes oficiales de n8n posteriores

Las versiones oficiales `2.47.2` y `3.1.0` y la revisión de `master` examinada contienen el mismo blob `f3bd1d20ad597b124e96c123d37254865ad797bb` para la copia asíncrona. Los dos patrones vulnerables siguen presentes en esas revisiones. La versión estable publicada seguía siendo `2.46.6` al consultar. [Estable oficial](https://github.com/n8n-io/n8n/releases/tag/stable), [fuente 2.47.2](https://github.com/n8n-io/n8n/blob/626a58e55ccc2078dfc7a32486c09134fd059fa3/packages/@n8n/backend-common/src/utils/flatted-async.ts), [fuente 3.1.0](https://github.com/n8n-io/n8n/blob/e2eb08d404d168411b3575e5de37064a1ba3b423/packages/@n8n/backend-common/src/utils/flatted-async.ts), [revisión de master](https://github.com/n8n-io/n8n/blob/3895cb31de5731c8425e56e56ff6f1dad1e2aa50/packages/@n8n/backend-common/src/utils/flatted-async.ts).

## Siguiente corrección de QuickJS

Los tres fragmentos de la corrección de Bellard encajan de forma única en el `vendor/quickjs/quickjs.c` que publica `quickjs-emscripten` en `df4efb9ef2cb25c417ecb57986da462d11b244ed`. Se aplicaron como datos en memoria. El original tiene SHA-256 `4f15a9e33819635474a9e43aa523f42ea3a6b452bfd8467223fcfbf6e447cc43`. El derivado tiene 1.964.229 bytes y SHA-256 `0302bbac5694bc1435ff59896a3ba51de7c286d07606e9d0ed9c57ba1a0c2c29`. [Corrección de Bellard](https://github.com/bellard/quickjs/commit/841dd034c2fac02f9ee3c605d8b96a8f8b49c945), [fuente del proveedor](https://api.github.com/repos/justjake/quickjs-emscripten/git/blobs/e32f0f3d096cebecb7566053ddf53e6575979487).

La receta oficial fija Emscripten 5.0.1. El registro oficial declara índice `sha256:c89732ef63a56de5a96395c5a8c1c7904f7420131a045406e6fedc4cbe1cc198` y manifiesto Linux amd64 `sha256:40a30b0e22be2b1fce570a5fcbf8124c4ccb22962b49850546863da07d374a51` (816.517.067 bytes según el registro). Son candidatos. Antes de operar, verificar el manifiesto y sus capas, el SDK, Closure y la caché Emscripten con sus hashes y auditoría. [Metadatos del registro](https://hub.docker.com/v2/namespaces/emscripten/repositories/emsdk/tags/5.0.1), [Makefile fijado](https://github.com/justjake/quickjs-emscripten/blob/df4efb9ef2cb25c417ecb57986da462d11b244ed/packages/variant-quickjs-wasmfile-release-sync/Makefile).

La ruta candidata usa fuentes como archivos HTTPS fijados, árboles sin `.git`, compilador por ruta absoluta con entorno cerrado, y un entorno sin red. Ejecutar sólo los targets explícitos `BROWSER MJS CJS CLOUDFLARE` de cada una de las cuatro variantes de Bellard, con paralelismo interno uno. Evitar `clean`, `prepareVariants.ts` y el wrapper de compilación que puede iniciar Docker. El generador de símbolos se puede invocar con Node y `tsx` de la clausura offline fijada del `yarn.lock`, sin `npx`. Conservar `$(GENERATE_TS_ENV)` para las marcas de depuración y Asyncify. Las dependencias directas revisadas son `tsx@4.6.2`, `fs-extra@10.0.1` y `prettier@3.1.1`. Falta cerrar sus transitivas y confirmar que el SDK preinstalado y Closure carecen de invocaciones a Git en ese camino. [Generador de símbolos](https://github.com/justjake/quickjs-emscripten/blob/df4efb9ef2cb25c417ecb57986da462d11b244ed/scripts/generate.ts), [lock del proveedor](https://github.com/justjake/quickjs-emscripten/blob/df4efb9ef2cb25c417ecb57986da462d11b244ed/yarn.lock).

Los resultados deben incluir los módulos WASM y sus cargadores Emscripten derivados. Cotejar importaciones, exportaciones, carga de Node y ejecución de expresiones, y repetir el inventario y el SCA para el runner y el servidor. Esta revisión confirmó el parche de C como datos y la disponibilidad del SDK candidato. La compilación y la prueba de WASM siguen pendientes.
