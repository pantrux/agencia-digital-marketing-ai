# Remediación propuesta de n8n y runners 2.46.6

El lock del runner Python quedó preparado y verificado localmente el 7 de noviembre de 2026. El build y el despliegue siguen bloqueados hasta completar las demás remediaciones y la SCA final. `remediation-inputs.json` conserva cada fila HIGH/CRITICAL de los dos informes originales, su procedencia, el cambio propuesto y los SHA de las entradas examinadas.

## Versión e inventario de partida

La [última versión estable oficial de n8n](https://github.com/n8n-io/n8n/releases/tag/n8n%402.46.6) es 2.46.6, publicada el 4 de noviembre. Se fijó el commit `bb72d756104542adb549b6966a57e21459bd52a2`, en lugar de depender del nombre mutable del tag. El TAR HTTPS está en `.tools/n8n-remediation/n8n-bb72d756104542adb549b6966a57e21459bd52a2.tar.gz`: 59.602.956 bytes y SHA256 `ed4976ca4a4a8647f7c2f58cc6c93182031214e55f5985ce33bdf3961f5f19b6`. Se leyeron miembros regulares en memoria; no se extrajo ni ejecutó el código descargado.

Los informes completos de entrada son `.tools/evidence/phase4-n8n-candidate-trivy.json` y `.tools/evidence/phase4-n8n-runners-candidate-trivy.json`. Sus hashes están en el inventario. n8n tiene 28 HIGH y 1 CRITICAL: cinco filas Alpine y 24 Node. Runners tiene 25 HIGH: cinco Node, siete Python y trece Go. Las 54 filas se conservan por posición de origen; dos copias o un CVE repetido no se deduplican para aparentar cobertura.

El [Dockerfile oficial de n8n](https://github.com/n8n-io/n8n/blob/bb72d756104542adb549b6966a57e21459bd52a2/docker/images/n8n/Dockerfile) usa Node 26.12.1, una clausura `compiled` y tres módulos nativos: sqlite3, isolated-vm y Kafka. El de [runners](https://github.com/n8n-io/n8n/blob/bb72d756104542adb549b6966a57e21459bd52a2/docker/images/runners/Dockerfile) combina la clausura JavaScript, un entorno Python 3.13 y task-runner-launcher 1.5.1. Cambiar solamente el tag o instalar paquetes encima no recompone esas piezas.

## Correcciones de Node y Alpine

Todas las versiones propuestas existen en los registros oficiales. Se descargaron sus archivos para comprobar SHA e integridad del registro; no se instalaron ni ejecutaron. Estos números son candidatos que corrigen los avisos de entrada, sujetos a comprobar el grafo nuevo completo.

| Componente observado | Candidato exacto | Cambio previsto |
| --- | --- | --- |
| brotli-libs 1.2.0-r1 | 1.2.1-r0 | Sustituir el APK de la base Alpine 3.24. |
| libcurl 8.22.0-r0 | 8.23.0-r0 | Sustituir APK y validar su clausura ELF. |
| libexpat 2.9.0-r1 | 2.10.0-r0 | Sustituir APK y validar su clausura ELF. |
| @azure/msal-node 5.1.5 y 5.6.0 | 7.1.0 | Overrides por consumidor, después de las pruebas descritas abajo. |
| vue-i18n y @intlify 11.4.5 | 11.4.14 | Actualizar catálogo frontend y resolver coherentemente core-base, shared, message-compiler y devtools-types. Recompilar UI. |
| @modelcontextprotocol/node 2.0.0 | 2.1.0 | Actualizar catálogo; su peer server `^2.1.0` admite el 2.3.0 fijado. |
| @modelcontextprotocol/sdk 1.31.0 | 1.32.0 | Actualizar catálogo y todas sus instancias del cierre. |
| body-parser 2.3.0 | 2.4.0 | Reemplazar el override anterior, preservando Express 5. |
| follow-redirects 1.16.1 | 1.16.2 | Reemplazar el override; afecta n8n y runner JS. |
| markdown-it 13.0.2 | 14.3.2 | Actualizar catálogo, override transitivo y tipos 14.1.2. Recompilar UI y revisar imports internos. |
| multer 2.4.0 | 2.5.0 | Reemplazar el override y regenerar lock. |
| toml 4.2.0 | 5.0.1 | Override de snowflake-sdk 3.3.0, después de probar su configuración. |
| undici 6.29.0 | 6.29.1 | Actualizar catálogo `undici-v6`, conservando esa rama. |
| undici 7.29.1 | 7.30.1 | Actualizar catálogo `undici-v7`, conservando esa rama; también runner JS. |

Los tres APK candidatos se descargaron sin instalar y sus SHA están en el inventario. El índice de Alpine examinado coincide con esas versiones. La siguiente unidad debe verificar además las firmas APK y fijar la clausura completa, incluida la herramienta que los instala: la base oficial elimina `apk-tools`. No se propone traer un instalador sin pin, copiar tres `.so` a mano ni ejecutar un `apk upgrade` abierto. La base final y el resultado ensamblado necesitan escaneo completo nuevo.

### MSAL 5 a 7: alcance y pruebas obligatorias

El lock identifica dos consumidores afectados: `@microsoft/agents-hosting@1.6.1` fija 5.1.5 y `@microsoft/teams.apps@2.0.16` resuelve 5.6.0. Azure Identity 4.13.0 usa **MSAL 3.8.4**, que no figura en esas filas del informe. Por ello, los selectores candidatos son:

```yaml
'@microsoft/agents-hosting@1.6.1>@azure/msal-node': 7.1.0
'@microsoft/teams.apps@2.0.16>@azure/msal-node': 7.1.0
```

La inspección del paquete publicado 7.1.0 confirma las clases y métodos consumidos: ConfidentialClientApplication, ManagedIdentityApplication, LogLevel, NodeSystemOptions, acquireTokenByClientCredential, acquireTokenOnBehalfOf y acquireToken. Conserva clientAssertion como cadena/callback, clientSecret, managedIdentityIdParams, resource y forceRefresh. `clientCertificate.thumbprint` continúa admitido, aunque obsoleto; no se cambiará por un campo supuesto. Su requisito Node >=20 permite el Node 26.12.1 de partida. **Esta comprobación de declaraciones no acredita el comportamiento de autenticación.**

Antes de aprobar los overrides se debe compilar cada consumidor con los tipos 7.1.0 y ejecutar pruebas con transporte falso: secreto de cliente, OBO, aserción federada callback, identidad administrada asignada por usuario/sistema, caché, error de autoridad y certificado sintético. Se deben inspeccionar los cuerpos enviados y la respuesta accessToken sin usar cuentas, credenciales ni endpoints Microsoft reales. Si alguna ruta rompe, se corrige el consumidor mediante un parche hash-verificado; no se habilita el override por el mero resultado del scanner.

### TOML 4 a 5: contrato de Snowflake

El único consumidor del lock es snowflake-sdk 3.3.0. Su archivo `dist/lib/configuration/connection_configuration.js` usa `toml.parse(str)` al leer `connections.toml`. Los paquetes oficiales 4.2.0 y 5.0.1 conservan CommonJS y esa función. En 5.0.1, `bigint` y Temporal son opciones explícitas; los enteros que no caben con precisión en Number fallan por defecto. Ese endurecimiento puede cambiar un caso límite y requiere regresión.

Se propone retirar el override previo `toml@<4.2.0` y usar `'snowflake-sdk@3.3.0>toml': 5.0.1`. Las pruebas deben entrar por el lector real de configuración Snowflake con HOME temporal: varias conexiones, tablas, cadenas escapadas, puertos/tiempos seguros, fechas, documento inválido, claves peligrosas y entero fuera del rango seguro. La salida normal ha de conservar tipos y selección de conexión; el extremo inseguro debe dar un error controlado, sin red ni conexión Snowflake.

### Código ya incluido en la UI

Vite empaqueta vue-i18n/Intlify y Markdown desde los paquetes frontend. Actualizar carpetas `node_modules` de una imagen que contiene la UI anterior no modifica los bytes de los recursos enviados al navegador. Deben reconstruirse `@n8n/i18n`, design-system, chat, editor-ui, los frontend de módulos y los bundles de agentes que correspondan a la clausura nueva; después se produce otra vez `compiled`.

Markdown 14 requiere revisar al menos los imports internos de `N8nMarkdown/youtube.ts` y `useChatHubMarkdownOptions.ts`: los tipos nuevos usan subrutas `.mjs`. No se aplicará una sustitución textual sin compilar. Las pruebas cubren renderizado, HTML peligroso, enlaces, emoji, task lists, notas al pie, KaTeX, bloque YouTube y locales con interpolación/plurales. Se debe registrar un metafile o inventario del bundle que atribuya los recursos nuevos a las versiones corregidas y comprobar que el cierre no conserva las versiones vulnerables. Buscar únicamente nombres de paquetes en archivos minificados no constituye esa prueba.

## Python: venv y dependencias vendorizadas son cierres distintos

La venv original fija `urllib3==2.7.0`. `prepare_runner_urllib3.py` usa el inventario existente y comprueba por SHA256 el TAR, los dos miembros originales y la rueda local. Prepara `pyproject.toml` y `uv.lock` con **2.8.0**, incluidas las restricciones de ambos archivos y los artefactos del [registro PyPI](https://pypi.org/pypi/urllib3/2.8.0/json). Compara ambos TOML completos para rechazar cambios ajenos.

```bash
python docker/n8n/prepare_runner_urllib3.py \
  --wheel .tools/cache/zou-runtime-linux-cp312-v1/urllib3-2.8.0-py3-none-any.whl \
  --output .tools/n8n-remediation/runner-urllib3-v1
```

La preparación requiere un directorio nuevo y emite `receipt.json` con los hashes del resultado. Omitir `--output` para comprobar las entradas y el cambio en memoria. Al preparar el build, incorporar ambos archivos al mismo árbol del runner antes de las dos copias que hace su Dockerfile. El productor del contexto debe conservar también la rueda verificada.

La exportación congelada y sin red con uv 0.11.12 acreditó `certifi==2026.10.23`, `sentry-sdk==2.35.2`, `urllib3==2.8.0` y `websockets==15.0.1`, con el hash esperado de la rueda. Desde el directorio generado se reproduce con:

```bash
uv export --offline --no-config --no-cache --frozen --all-extras --no-dev \
  --no-emit-project --no-python-downloads --format requirements.txt
```

Se cargó la rueda fijada en Python 3.12.1 y se comprobó en memoria un stream HTTP normal, el rechazo de una línea de tamaño de chunk superior a 65536 bytes y la separación de la configuración TLS de un proxy HTTPS. Estas dos últimas comprobaciones cubren las rutas corregidas por [CVE-2026-97689](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw) y [CVE-2026-97687](https://github.com/urllib3/urllib3/security/advisories/GHSA-8988-9cw3-xx77). Quedan pendientes el entorno Linux musl con Python 3.13, la ejecución del runner con broker controlado y el escaneo final de la imagen.

Las otras filas Python tienen `AnalyzedBy=sbom` y proceden de la capa de construcción de CPython, no del COPY de esa venv. No se conoce por el informe una ruta importable para cada una. La inspección reproducible de ruedas oficiales explica un candidato concreto: **pip 26.2.2**, incluido por [ensurepip de CPython 3.13.18](https://github.com/python/cpython/blob/v3.13.18/Lib/ensurepip/__init__.py), contiene msgpack 1.1.2, urllib3 2.7.0 y setuptools 70.3.0 en su inventario vendorizado. pip 26.3 actualiza msgpack a 1.2.4 y urllib3 a 2.8.0, pero mantiene setuptools 70.3.0. Los SHA de ambas ruedas y de sus `vendor.txt` están registrados.

Por tanto, instalar msgpack 1.2.3, setuptools 84.2.1 y urllib3 2.8.0 en la venv **no corrige las copias dentro de pip/ensurepip**. La siguiente unidad debe localizar los miembros físicos y la atribución SBOM en las capas OCI para cerrar esa procedencia. No se ha declarado falso positivo ni se ha eliminado metadato para ocultar un aviso.

La propuesta preferida es un runtime inmutable con el intérprete y la venv necesarios, sin pip, ensurepip, uv ni pnpm en la imagen final. Esas herramientas quedan en etapas de construcción con su propio lock/SCA. Se retiraría el código de herramientas innecesarias, no sólo su SBOM; el inventario final se generaría a partir de los archivos efectivamente publicados, preservando licencias y trazabilidad del origen. Es una decisión pendiente que reduce la posibilidad de extender la imagen instalando paquetes dentro del contenedor. Si se exige conservar pip, hace falta una unidad separada para actualizar sus componentes vendorizados y sus hashes/RECORD, probar pip completo y emitir un inventario honesto; una actualización ordinaria de pip no basta. Los candidatos independientes corregidos msgpack 1.2.3, urllib3 2.8.0 y setuptools 84.2.1 quedan fijados en el JSON para esa opción, sin fingir que ya sustituyen los componentes internos.

## Launcher Go

El candidato escaneado incluye launcher 1.5.1 construido con Go 1.25.11 y x/text 0.39.0. La [última versión estable del launcher](https://github.com/n8n-io/task-runner-launcher/releases/tag/1.6.0) observada es 1.6.0: actualiza a Go 1.26.10 y x/text 0.43.0, además de cambiar reintentos/configuración. No sustituye automáticamente al launcher 1.5.1 fijado por n8n 2.46.6.

Para limitar cambios de comportamiento se propone reconstruir el commit 1.5.1 `871cb76bc35c990f368eaa788f0b4ce7b145e143` con **Go 1.27.3**, x/text **0.41.0** y go.mod/go.sum/vendor revisados. El inventario contiene SHA del TAR fuente, sus archivos de módulo, el SDK Linux amd64 oficial y la respuesta SumDB de x/text. El binario deberá demostrar esas versiones con metadatos Go y SCA, y probarse con la configuración exacta `/etc/n8n-task-runners.json` del release.

La compilación propuesta usa CGO desactivado, target linux/amd64, `-trimpath`, `-buildvcs=false` y `-mod=vendor`; fija `GOTOOLCHAIN=local`, `GOVCS=*:off`, `GOPROXY=off` y `GOSUMDB=off` **sólo después** de verificar/stagear el grafo y sus checksums. Se ejecuta directamente el SDK fijado sobre la entrada `cmd/launcher/main.go`; no se ejecutan Makefiles ni descargas implícitas del toolchain. Las pruebas del launcher requieren broker falso, selección JS/Python, token inválido, salud, límites, terminación, procesos descendientes y pérdida del broker. Cambiar a 1.6.0 es una alternativa que añade pruebas de backoff/default-env; no es necesario para el arreglo mínimo propuesto.

## Entradas y secuencia de reconstrucción sin Git

1. Respaldar antes de usar: commit/TAR y SHA, todos los parches, manifests y locks nuevos, APK y firmas, base OCI amd64, Node, pnpm y sus artefactos por plataforma, SDK Go, módulos vendorizados, ruedas Python y directorios propios. El inventario actual no sustituye ese respaldo ni autoriza apply.
2. Descomprimir únicamente a un árbol nuevo y privado con validación de rutas, enlaces, tamaños y colisiones. El contexto final no contiene `.git`, bóveda, credenciales, cachés ni archivos del usuario. Conservar avisos de licencia y marcar la modificación.
3. Aplicar cambios a pnpm-workspace.yaml/catálogos y parches sobre SHA de fuente esperados. Regenerar el lock con pnpm **12.4.2** fijado, `--lockfile-only --ignore-scripts`, en una unidad controlada; rechazar resoluciones Git, SSH, remoto VCS o paquetes sin integridad. No usar el lock antiguo con `--frozen-lockfile` después de cambiar un catálogo. Verificar los dos documentos YAML del lock y el cierre transitivo completo.
4. Preparar el store y todas las piezas binarias por HTTPS/SHA en un paso separado. La instalación de compilación será offline, frozen e ignorará lifecycle scripts. Se revisan y ejecutan de forma explícita sólo los generadores/módulos nativos necesarios. El paquete npm pnpm 12 tiene scripts de instalación: hay que fijar también su distribución por plataforma; no se presupone resuelta por descargar el wrapper.
5. No ejecutar `pnpm build` de la raíz: llama Turbo. Tampoco `build:n8n` sin adaptar: ese wrapper vuelve a invocar Turbo, ni `dockerize-n8n.mjs`: usa Bake. Se propone un orden topológico explícito de los scripts de la clausura n8n y task-runner, sin incluir raíz ni herramientas de publicación. `CI=true`/`DOCKER_BUILD=true` evitan el prepare de desarrollo, pero no son por sí solos una garantía universal contra procesos Git. No definir RELEASE, SENTRY_AUTH_TOKEN ni CODECOV_TOKEN durante Vite para no activar plugins de publicación; la minificación puede fijarse directamente en el parche comprobado.
6. Reconstruir UI, paquetes y artefactos nativos en una etapa de toolchain cerrada. Producir `compiled/` y `dist/task-runner-javascript/` mediante el deploy de pnpm a partir del lock nuevo, conservando el tratamiento upstream de bindings musl. No repetir el `pnpm add moment --no-lockfile` del Dockerfile de runners: moment 2.30.1 debe estar incluido en el cierre congelado. Auditar subprocesos y fuentes de las herramientas antes de ejecutar; los SDK no reciben secretos.
7. Entradas COPY de n8n: `compiled/`, entrypoint fijado y los tres `.node` nuevos contra la base elegida. Entradas de runners: `dist/task-runner-javascript/`, entorno Python reproducible en la misma ruta absoluta `/opt/runners/task-runner-python`, launcher nuevo y `n8n-task-runners.json` fijado. No copiar entornos virtuales con un shebang de `/app` que deje de existir; crear o reinstalar el entorno final en su ruta canónica. Conservar UID/GID 1000 y los entrypoints/protocolo originales.
8. Hacer el build local con red desactivada, contexto whitelist y las variables NoGit de Buildx después de sudo: `BUILDX_GIT_INFO=false`, `BUILDX_GIT_LABELS=false`, `BUILDX_GIT_CHECK_DIRTY=false`, junto con `--provenance=false`. No Bake, contexto remoto ni source VCS. Estos ajustes cubren Buildx; no sustituyen la auditoría de los comandos anteriores ni de Go/lifecycle scripts.

El control de procesos debe acreditar **cero ejecuciones Git**, incluidas comprobaciones de versión y fallbacks. No se usa un ejecutable Git falso ni se acepta que un intento falle como cumplimiento. La imagen contiene funciones opcionales de control de fuentes y nodos Git: el uso interno inicial debe mantener esas funciones inactivas y comprobar que el arranque no las invoca. No se ejecutó esa prueba en esta investigación.

## Contexto offline del runner Python

`stage_runner_python.py` verifica el TAR del release y toma los 42 archivos Python de `packages/@n8n/task-runner-python/src/`, su README, la licencia y la configuración original del launcher. Usa el `pyproject.toml` y `uv.lock` derivados ya fijados. Selecciona tres ruedas universales y la rueda `websockets` CPython 3.13 musllinux x86_64 exacta del lock. Verifica por tamaño y SHA-256 las siete ruedas locales. El contexto nuevo se publica sin sobrescribir y deja `SHA256SUMS` para cotejar sus entradas antes de transferirlo. [Dockerfile y configuración upstream fijados](https://github.com/n8n-io/n8n/tree/bb72d756104542adb549b6966a57e21459bd52a2/docker/images/runners)

```powershell
python -B docker/n8n/stage_runner_python.py --archive <tar-original> --patched-dir <runner-urllib3-v1> --wheel-dir <wheelhouse> --output <contexto-nuevo>
```

Se puede repetir `--wheel-dir` para cachés distintas. Los tres pines exclusivos de construcción son `packaging==26.3`, `setuptools==84.2.1` y `wheel==0.48.0`. Las cuatro ruedas de ejecución proceden del lock. [packaging](https://pypi.org/project/packaging/26.3/) · [setuptools](https://pypi.org/project/setuptools/84.2.1/) · [wheel](https://pypi.org/project/wheel/0.48.0/)

Tras respaldar los pines y preparar en el constructor la base oficial Python 3.13.18 Alpine 3.24 para amd64 fijada en el Dockerfile, comprobar `sha256sum -c SHA256SUMS` desde el contexto. Ejecutar la construcción como operación posterior autorizada en el host Linux con la configuración privada de Docker y las variables del paso 8. [Metadatos oficiales de la base](https://hub.docker.com/v2/namespaces/library/repositories/python/tags/3.13.18-alpine3.24)

```sh
BUILDX_GIT_INFO=false BUILDX_GIT_LABELS=false BUILDX_GIT_CHECK_DIRTY=false \
  docker buildx build --platform linux/amd64 --network=none --provenance=false \
  --target payload --output type=local,dest=<salida-nueva> <contexto-verificado>
```

`Dockerfile.runner-python` instala las ruedas con hashes y sin índice en una etapa de construcción. Empaqueta `task-runner-python` mediante el backend de la fuente original, crea el entorno en `/opt/runners/task-runner-python/.venv` y prueba su inventario, el descubrimiento de `src.main` y la extensión nativa de `websockets`. Exporta únicamente ese entorno y la licencia. La etapa de salida requiere una base final con ABI, rutas y biblioteca estándar compatibles con la base fijada. El sidecar integrado requiere además Node, el runner JavaScript, el launcher Go y su configuración original. Quedan pendientes la construcción Linux, la prueba con el broker y la auditoría de la imagen completa.

## Aceptación pendiente y límites de licencia

Antes de habilitar una receta final son bloqueantes: locks resueltos y hasheados, contratos MSAL/TOML, regresiones HTTP multipart y redirecciones, UI compilada y probada, JS/Python runners con broker controlado, launcher y cierre de hijos, importación de sqlite3/isolated-vm/Kafka en Linux musl, SCA de herramientas y runtime por separado y Trivy de la imagen completa con cero HIGH/CRITICAL. No basta escanear package.json, cambiar una etiqueta de versión, excluir el frontend ni eliminar un SBOM. Los resultados reales se conservarán asociados al ID de imagen.

La [licencia de la fuente fijada](https://github.com/n8n-io/n8n/blob/bb72d756104542adb549b6966a57e21459bd52a2/LICENSE.md) limita el uso gratuito a la actividad interna propia o a usos no comerciales; el código Enterprise tiene términos separados. Este plan es para orquestación interna. No autoriza ofrecer n8n al público como SaaS ni dar por licenciadas sus funciones Enterprise. Deben conservarse avisos y añadirse el aviso de modificación. Los permisos de la plataforma comercial no se deducen de que el contenedor pueda arrancar.

Esta unidad no modifica .env, Compose, capacidades, ESTADO ni infraestructura. La fase declarada sigue en 3 y CD permanece deshabilitado.
