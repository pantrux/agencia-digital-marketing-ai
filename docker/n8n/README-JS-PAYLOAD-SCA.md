# Auditoría del runner JavaScript de n8n

El sujeto es el árbol Linux x86-64 musl de `@n8n/task-runner@2.46.5`, compilado desde n8n 2.46.6. Su inventario físico canónico tiene SHA-256 `ae680f933cd93f9343d33960246d0d051be8d86d67d2820a063138b1db23b6ba`. El informe npm de ese árbol cubre **422 de 422** paquetes de registro. Los 18 paquetes locales y el runner principal se cotejaron con el lock y las fuentes de compilación. Su código requiere revisión separada. Queda pendiente la aceptación de seguridad del artefacto completo.

## Resultado de npm

Trivy 0.76.2 evaluó los 422 identificadores PURL exactos el 7 de noviembre de 2026 a las 08:50 UTC. La base se había actualizado el 6 de noviembre a las 12:55 UTC y su próxima actualización estaba prevista para el 7 a las 12:55 UTC. Dio cinco avisos `MEDIUM` y cero de las otras severidades. El análisis directo del lock había cubierto sólo nueve paquetes porque el lock contiene referencias `file:`. El informe completo usó un CycloneDX derivado de los manifests físicos, el lock y sus integridades SHA-512. Trivy emitió dos advertencias sobre SBOM externos. La verificación posterior cotejó uno a uno los 422 PURL y conservó las advertencias y el informe bruto.

| Paquete instalado | Aviso | Primera corrección reportada |
| --- | --- | --- |
| `moment@2.30.1` | [CVE-2026-17495](https://github.com/moment/moment/security/advisories/GHSA-4p3w-j4w9-5jqw) | `2.31.0` |
| `qs@6.16.0` | [CVE-2026-110816](https://github.com/ljharb/qs/security/advisories/GHSA-6pq8-jhm2-2vcw) | `6.16.1` |
| `stream-json@1.9.1` | [CVE-2026-104182](https://github.com/uhop/stream-json/security/advisories/GHSA-hqr4-qq8f-hg3x) | `3.6.0` |
| `stream-json@1.9.1` | [CVE-2026-104183](https://github.com/uhop/stream-json/security/advisories/GHSA-mjw6-4jj6-33hc) | `3.6.0` |
| `stream-json@1.9.1` | [CVE-2026-71429](https://github.com/uhop/stream-json/security/advisories/GHSA-528h-pc64-c93x) | `3.5.0` |

El runner declara `moment` directamente y `@n8n/backend-network` usa `qs.stringify`. Para `stream-json`, `@n8n/backend-common` importa el parser JSON y `Assembler`. El tarball fijado contiene la asignación ordinaria al objeto que describe CVE-2026-104183. El inventario del tarball carece de los módulos JSONC citados por CVE-2026-104182 y la ruta revisada de n8n carece de los filtros citados por CVE-2026-71429. El informe bruto conserva los tres avisos. Una migración a `3.6.0` necesita pruebas de las importaciones y del deserializador de n8n, incluidas referencias circulares, errores y claves propias. [Versión 3.6.0 de stream-json](https://github.com/uhop/stream-json/releases/tag/3.6.0).

El mismo inventario confirma `follow-redirects@1.16.2`, `undici@7.30.1` e `isolated-vm@7.0.1`, las versiones corregidas en los avisos examinados. [Aviso de follow-redirects](https://github.com/advisories/GHSA-mrwp-f3q8-3g9p), [versión 7.30.1 de Undici](https://github.com/nodejs/undici/releases/tag/v7.30.1), [aviso de isolated-vm](https://github.com/laverdet/isolated-vm/security/advisories/GHSA-864f-rcv7-6rh4).

## Código incorporado y binarios

- **QuickJS.** Los cuatro módulos WASM `@jitl` instalados, todos `0.32.0`, coinciden byte por byte con sus tarballs autenticados por SRI. El proveedor declara Bellard QuickJS `f1139494d18a2053630c5ed3384a42bb70db3c53` en la versión `df4efb9ef2cb25c417ecb57986da462d11b244ed`. Se recalculó la identidad de blob `e32f0f3d096cebecb7566053ddf53e6575979487` del archivo C vendorizado de 1.964.029 bytes. La función `js_typed_array_constructor_ta` conserva la copia por longitud del destino sin el límite del origen que añadió Bellard al corregir la lectura fuera de límites. Es evidencia de código fuente y procedencia del proveedor. La explotación en WASM y su alcance operativo en el runner siguen sin verificarse. Para cerrar el caso, fijar una versión o compilación con la corrección presente en los módulos distribuidos y probar el motor seleccionado. [Declaración del proveedor](https://github.com/justjake/quickjs-emscripten/blob/df4efb9ef2cb25c417ecb57986da462d11b244ed/packages/variant-quickjs-wasmfile-release-sync/README.md), [blob exacto](https://api.github.com/repos/justjake/quickjs-emscripten/git/blobs/e32f0f3d096cebecb7566053ddf53e6575979487), [problema de QuickJS](https://github.com/bellard/quickjs/issues/492), [corrección de Bellard](https://github.com/bellard/quickjs/commit/841dd034c2fac02f9ee3c605d8b96a8f8b49c945).
- **Flatted copiado en n8n.** El archivo fijado `packages/@n8n/backend-common/src/utils/flatted-async.ts` (`86ef3a641fde6231eaf4d8b70eb8298aed0a71f7e1bbf5f094353b002ccfc20e`) declara que su resolución de referencias procede de `flatted@3.2.7`. Conserva la indexación por cadenas arbitrarias y la recursión sin cota descritas en dos avisos `HIGH`. `parse-flatted.ts` selecciona la copia asíncrona para entradas a partir de 1.048.576 unidades de texto. El paquete separado `flatted@3.4.2` cubre la ruta síncrona y deja esta copia pendiente. Capturar del árbol ya identificado sólo los dos archivos compilados y sus hashes, confirmar su presencia y llamadas efectivas, y revisar la resolución segura de referencias antes de aprobar. La evaluación identifica patrones vulnerables en la fuente fijada. Quedan pendientes el cotejo con los dos archivos compilados y la exposición en este runner. [CVE-2026-33228](https://github.com/WebReflection/flatted/security/advisories/GHSA-rf6f-7fwh-wjgh), [CVE-2026-32141](https://github.com/WebReflection/flatted/security/advisories/GHSA-25h7-pfq9-p65f).
- **Bibliotecas nativas.** Se cotejaron 87 archivos físicos contra ocho tarballs con SHA-256 y SRI correctos. Se distribuyen en 4 WASM de QuickJS, 35 binarios de `@sentry/node-cpu-profiler`, 35 de `@sentry/node-native-stacktrace`, 12 de `isolated-vm` y `ssh2/util/pagent.exe`. La procedencia a nivel de paquete y archivo queda acreditada. La cobertura de avisos para código C/C++ incorporado, ABI y bibliotecas del sistema requiere evidencia adicional, al igual que la imagen base y el Node de ejecución.

## Repetición y evidencia

Desde la raíz del proyecto, usar Python 3.12 y PyYAML 6.0.3. El verificador es de sólo lectura salvo la opción explícita `--sbom`, que exige un archivo nuevo. Usa las capturas del lock, manifiestos y árbol, el manifiesto de fuentes V3 y el recibo de tarballs V5. Genera una lista de componentes npm para Trivy y registra por separado los paquetes locales y la procedencia nativa.

```powershell
python -B .tools/evidence/n8n_js_payload_sca_v1.py
python -B .tools/evidence/n8n_js_payload_sca_v1.py --sbom <archivo-CDX-nuevo>
python -B .tools/evidence/n8n_js_payload_sca_v1.py --report <informe-JSON>
```

Para un nuevo escaneo, usar `trivy sbom` con una base vigente y registrar los hashes del ejecutable, base, metadatos y entradas antes y después. La captura original fija el comando completo en `receipt.json`. Usó caché en memoria, modo desconectado, actualización desactivada, configuración y archivo de exclusiones vacíos, sólo detección de vulnerabilidades de bibliotecas, listado de paquetes y las cinco severidades. La ejecución estuvo acotada por el supervisor local de Windows. Una cobertura menor a 422 o un PURL distinto hacen fallar el verificador.

| Archivo de evidencia | SHA-256 |
| --- | --- |
| `.tools/evidence/n8n_js_payload_sca_v1.py` | `dbc20ebec1472692f6f4aa21ef045ca217167aef5135b04d020f92feef0c8732` |
| `.tools/evidence/phase4-n8n-js-payload-manifests-v1.json` | `e423f37943ca0eea0dfbedecdcf968fcd1119a57f85fb3f077233fb460653f8f` |
| `.tools/backup-source-build/n8n-js-payload-cdx-sca-v1/runtime.cdx.json` | `d779d30e1cd6f86ad7f7444b6ffa89bbf8b8c092329d16d6bec1d7c16d638450` |
| `.tools/backup-source-build/n8n-js-payload-cdx-sca-v1/report.json` | `aa5eeff03ffebdad5c37dfe93b812abcb6f4d490c722aeeb7b22ff3dd731ec0c` |
| `.tools/backup-source-build/n8n-js-payload-cdx-sca-v1/receipt.json` | `a46a2c25ae43e62a9dedc80e4cf8ad6744d26c67a75932e627ca2676c33e7199` |
| `.tools/evidence/phase4-n8n-js-payload-sca-verification-v1.json` | `db13a4d17aad52df842b12f2fa355d51631e24c11c9a0f1077f14f0824e09c56` |
