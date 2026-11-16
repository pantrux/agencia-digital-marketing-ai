# Entradas CPU y plan de construcción de SupoClip

Esta unidad cierra una **propuesta de entradas**, con fuentes y metadatos fijados.
No ejecuta SupoClip, SDK, compiladores, instaladores ni contenedores. El plan
mantiene `build_enabled=false` y `deployment_enabled=false`; no autoriza un
build ni acredita una imagen final. El cliente Speaches y su proveedor tienen
su propia aceptación previa, descrita en [cliente](README-SPEACHES-CLIENT.md) y
[proveedor](README-provider.md).

Los cinco archivos de esta unidad son el preparador, su prueba, este documento,
[manifiesto](cpu-build-inputs.json) y [lock de registro](requirements-cpu.lock).
El preparador sólo lee datos locales y devuelve un resumen; no contiene una
acción de instalación o ejecución. Exige `packaging==26.2`, fijado en las
dependencias de desarrollo del repositorio.

## Fuente completa y límites de la propuesta

Se fija el commit `f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38` de
[SupoClip](https://github.com/FujiwaraChoki/supoclip/tree/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38).
El TAR HTTPS tiene 31.704.580 bytes y SHA256
`be5429721b58f1b477226dd186e7fcbec280802ec8a768bf5e3ce99e9f079605`.
Se contrastaron sus 1.025 archivos y 41.952.865 bytes sin comprimir contra el
árbol API completo de 1.211 entradas, mediante el identificador de cada blob.
No se extrajo ni ejecutó la fuente. El manifiesto registra el TAR, árbol API,
Dockerfiles, pyproject, uv.lock, package.json, pnpm-lock y el overlay Speaches.

El código upstream y los cambios derivados conservan su
[licencia AGPL-3.0](https://github.com/FujiwaraChoki/supoclip/blob/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38/LICENSE).
Los archivos de licencia de YuNet, OpenCV, FFmpeg y las fuentes tipográficas
deben acompañar los materiales y salidas correspondientes. Esta propuesta no
aplica todavía los cambios CPU, YouTube, fuentes locales o Prisma al árbol
upstream. Cada cambio necesita un parche por SHA y sus pruebas en otra unidad.

## Grafo Python CPU

El extra `cpu` upstream sigue incluyendo Torch. Se evaluó el lock original con
los marcadores completos de CPython 3.12.16, Linux x86_64, y todos los extras
transitivos activos; no se usaron los marcadores de Windows del equipo local.
El baseline sin Whisper/Torch contiene 176 dependencias y queda preservado en
la evidencia. La propuesta seleccionada contiene **84 dependencias y 129
relaciones METADATA activas**, contrastadas con METADATA de PyPI y PKG-INFO de
SRT. No se ha ejecutado su compatibilidad de imports.

Los cambios de dependencias propuestos son explícitos:

- Eliminar `openai-whisper`, el paquete distinto `whisper` y `setuptools-rust`.
- Sustituir el metapaquete `pydantic-ai` por
  `pydantic-ai-slim[anthropic,google,openai]==1.107.10`. Es la misma distribución
  de implementación que usa el metapaquete; se conservan las rutas OpenAI,
  Anthropic, Google, OpenRouter y Ollama utilizadas por `ai.py`. La aceptación
  posterior importará `Agent` y probará cada ruta con dobles, sin claves ni
  llamadas cobrables. No se eliminan proveedores para ocultar dependencias.
- Añadir `python-multipart==0.0.33` como dependencia directa necesaria para
  `UploadFile`/`Form`, en vez de depender del extra CLI que la aportaba.
- Cambiar Mako 1.3.12 a 1.4.3. La primera auditoría y el aviso
  [GHSA-5639-2j2p-m4mx](https://github.com/advisories/GHSA-5639-2j2p-m4mx)
  quedan conservados; no se usa una excepción para ocultarlos.

El lock publicado contiene **82 ruedas de registro con versión y SHA256**.
Es deliberadamente un subconjunto: faltan dos ruedas de salida propias. No
debe ejecutarse como instalación completa ni permitir resolver lo que falta
por Internet. En el futuro se combinará con los dos locks de salidas verificadas
y se instalará sólo desde una wheelhouse propia, sin resolver dependencias ni
aceptar fuentes como fallback.

| Salida pendiente | Entrada fijada | Requisito antes de instalar |
| --- | --- | --- |
| SRT 3.5.3 | sdist PyPI, SHA256 `4884315043a4f0740fd1f878ed6caa376ac06d70e135f306a6dc44632eed0cc0` | Rueda universal real mediante setuptools, manifiesto/SHA de salida; sin empaquetador manual |
| OpenCV | fuente 4.13.0, commit `fe38fc608f6acb8b68953438a62305d8318f4fcd`, SHA256 `6a7508554941c1a698c243b2212b2985ce59a65a3e0348d53c2158607d801e61` | Rueda `opencv-python==4.13.0.92+agency1`, cierre ELF, licencia, pruebas y SHA real de salida |

El SDK Python separado propone build 1.6.1, setuptools 84.2.1, wheel 0.48.0,
pyproject-hooks 1.3.3 y packaging 26.3. Sus cinco versiones y ruedas están
fijadas y su METADATA forma una clausura propia. Packaging 26.3 de ese SDK no
cambia el 26.2 de la herramienta de inspección. La revisión de código
vendorizado y hooks de construcción sigue pendiente antes de ejecutarlo.

## OpenCV conserva video y rostros

El mínimo preparado para Zou no sirve a SupoClip. Su fuente usa VideoCapture,
búsqueda por frame/tiempo, flujo óptico, transformación afín, Haar,
`FaceDetectorYN` y DNN, además de operaciones de imagen. La lista concreta
propuesta es `core,imgproc,imgcodecs,videoio,video,calib3d,features2d,flann,objdetect,dnn,python3`.

El build deberá usar FFmpeg externo y protobuf 29.5-r6 con el protoc y Abseil
20250127.2-r7 correspondientes: `WITH_PROTOBUF=ON`, `BUILD_PROTOBUF=OFF`,
`PROTOBUF_UPDATE_FILES=ON` y C++17. No debe caer al protobuf antiguo incluido
en OpenCV. Los headers NumPy serán de la misma rueda 2.4.4 que el runtime.
CUDA, OpenCL, GUI, IPP, ITT y TBB quedan deshabilitados. No se permiten descargas
desde CMake, ni una caída a bibliotecas incluidas si falta una dependencia.
El CMakeCache y el enlace efectivos se validarán tras el build autorizado.

Debe conservarse `cv2.data.haarcascades` junto a sus XML/licencias; copiar sólo
la extensión nativa rompe esa API. Se conserva el modelo YuNet MIT ya incluido
en la fuente, SHA256
`8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`.
No se descargan pesos nuevos ni se elimina la función facial. La rueda PyPI
upstream se conserva como referencia de METADATA, pero no se autoriza su
instalación ni se confunde su análisis PyPI con el de sus bibliotecas nativas.

## Materiales Wolfi, FFmpeg y YouTube

Base amd64 candidata:
`cgr.dev/chainguard/wolfi-base@sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72`.
El índice oficial de [Wolfi](https://packages.wolfi.dev/os/x86_64/APKINDEX.tar.gz)
está fijado con SHA256
`9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd`
y su firma RSA256 se comprobó con la clave fijada. El manifiesto conserva
registros, dependencias, proveedores elegidos y checksum de control Q1.
Los campos `D`/`p` se presentan como listas de tokens para facilitar su revisión;
unidos por espacios reconstruyen el registro original cuyo SHA256 se conserva.
Todavía no hay verificación de los payloads APK de esta nueva clausura ni
confirmación por el solver nativo; no se fabrica un SHA256 de payload.

Los pins propuestos son Python 3.12.16-r3, FFmpeg 7.1.5-r8, Tesseract y sus
datos ingleses 5.5.2-r8 y Node 24.22.2-r0. Las rutas candidatas son
`/usr/bin/python3.12`, `/usr/bin/ffmpeg`, `/usr/bin/ffprobe`,
`/usr/bin/tesseract` y `/usr/bin/node`; sus archivos/ELF reales aún se deben
verificar en los payloads. Se conservan Noto, Noto CJK, emoji y fontconfig para
no perder las funciones tipográficas presentes en el Dockerfile original.

El inventario V3 reúne **386 APK únicos**: materiales candidatos del backend
321 + base 19, SDK nativo 140 + base y frontend Node 7 + base, con solapamientos.
Es un superset de materiales, no una imagen runtime mínima. En particular,
Python requiere `py3-pip-wheel` y la clausura contiene `openssl-4.0-dev`.
La selección final de archivos debe acreditar la ausencia de pip/ensurepip,
headers, compiladores y gestores innecesarios, preservando procedencia e
inventario del subconjunto real. No se borran metadatos para silenciar Trivy.

El candidato inicial Deno 2.8.2-r0 presentó un HIGH y su reporte queda intacto.
Se propone Node 24 fijado y el paquete local `yt-dlp-ejs==0.8.0` ya presente en
la clausura Python. [yt-dlp documenta Node como runtime EJS compatible](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
El parche posterior seleccionará la ruta fija de Node y `remote_components=[]`;
faltando Node fallará de forma explícita. Esta unidad no toca
`youtube_proxy.py`, no ejecuta EJS y no permite el instalador remoto de Deno.

## Frontend y Prisma: cambios separados antes del build

El pnpm-lock original fija **920 paquetes**, con identidades npm y SRI SHA512,
sin resoluciones VCS o tarballs alternativos. Incluye dependencias de desarrollo,
build y plataformas opcionales. pnpm es 10.27.0, Next 15.5.28 y Prisma/client
**6.19.3**; `^6.19.2` es el rango de package.json, no la versión resuelta.

La ruta elegida para la siguiente unidad es `engineType="client"` y
`@prisma/adapter-pg==6.19.3`, conservando imports y queries actuales. Debe quitar
`binaryTargets`, adaptar la única fábrica de Prisma usada en producción y
congelar el lock nuevo. La
[documentación de Prisma v6](https://www.prisma.io/docs/orm/v6/prisma-client/setup-and-configuration/no-rust-engine)
requiere un driver adapter y advierte que el generador antiguo
`prisma-client-js` está menos probado con esa ruta. Por eso se exige typecheck,
BetterAuth y transacciones sobre PostgreSQL temporal propio. **No se ejecuta
`e2e/global-setup.ts` sobre una base real.**

El compilador de consultas WASM y el SDK/schema-engine nativo requieren
inventario, procedencia y SCA Rust/ELF/TLS separados. Una auditoría npm sin
avisos no certifica esos componentes. Tampoco se presume que un engine
`debian-openssl-3.0.x` funcione con OpenSSL 4 de Wolfi.

Las familias Geist, Geist Mono y Syne se proponen como WOFF2 locales de
Fontsource-variable 5.3.0, normal/latin, con las mismas variables y pesos
(100–900, 100–900 y 400–800). Se verificaron SRI de los tres TAR, SHA de los
archivos y licencias OFL-1.1. Sus miembros/licencias están en el manifiesto;
no se añaden esos paquetes al lock de 920 ni se ejecuta código de sus TAR.
El parche posterior de `next/font/local` evita descargar fuentes al compilar.

Antes de cualquier SDK siguen pendientes las llamadas a procesos/telemetría de
Next, pnpm, Prisma y sus hooks. El perfil partirá de entorno vacío, `CI=1`,
`NEXT_TELEMETRY_DISABLED=1`, sin credenciales PostHog, sin binario Git en PATH
y con artefactos offline fijados. Esas variables no sustituyen la lectura y
prueba de los hooks. No se ejecutarán scripts de ciclo de vida indiscriminados.

## Auditoría efectuada y alcance exacto

| Inventario | Cobertura observada | Límite |
| --- | --- | --- |
| Python CPU baseline | 176 consultas OSV, 0 tras corregir Mako | Baseline preservado; no ejecución |
| Python Slim elegido | 84 consultas OSV, 0 avisos; 129 aristas METADATA | No acredita bibliotecas embebidas de OpenCV |
| SDK Python propuesto | 5 consultas OSV, 0 avisos | Pendiente código vendorizado/hooks y ejecución aislada |
| Frontend baseline | 920 consultas OSV, 0 avisos | No cubre el lock futuro, WASM, Rust o standalone real |
| APK candidato V3 | 386/386 registros, 0 avisos de todas las severidades | SBOM declarado, no payloads ni imagen |

Los requests/responses y sus SHA constan en el manifiesto, con reportes privados
en `.tools/evidence/`. Trivy usa el snapshot fijado: `UpdatedAt`
2026-11-05 13:09 UTC y `NextUpdate` 2026-11-06 13:09 UTC, ya vencido al cierre
de esta propuesta. No se actualizó la caché compartida. La aprobación final
necesita DB vigente, cobertura completa de la imagen y sus archivos reales.
Se conservan los reportes iniciales de Mako/Deno; no hay ignores ni VEX.

## Secuencia concreta para el controlador de raíz

1. Respaldar pins y raíces nuevas propias; materializar el TAR fijado y verificar
   todos los blobs antes de aplicar el overlay Speaches ya congelado. Copiar
   sólo código/assets/licencias permitidos, nunca `.git`, secretos, cachés,
   uploads, clips o logs. El preparador no crea esa raíz ni adopta árboles.
2. Entregar parches fijados y probados para CPU, Node/EJS, fuentes y Prisma JS.
   Congelar nuevos pyproject/locks y repetir su cobertura exacta/SCA.
3. Materializar y verificar APK, fuentes, headers y ruedas como datos; cerrar
   el solver nativo y revisar hooks NoGit. Respaldar un perfil SDK acotado
   antes de ejecutar el build offline de OpenCV y SRT. Congelar cada salida.
4. En SDK frontend separado, usar store congelado/offline sin lifecycle scripts;
   invocar sólo Prisma/Next previamente auditados. Probar las APIs/imports y
   la base temporal propia. Producir standalone y catálogo de archivos.
5. Construir imágenes CPU con allowlist pequeña y entradas offline. Buildx
   fijado, variables NoGit después de sudo, `--network=none`,
   `--provenance=false`, sin frontend remoto ni contexto VCS. Exportar,
   verificar manifiestos/capas/diffIDs/modos y ejecutar SCA final con DB vigente.
6. Respaldar IDs antes de crear contenedores de aceptación propios. Backend
   propone `uvicorn src.main:app` en 8000; worker usa la misma imagen con
   `arq src.workers.tasks.WorkerSettings`; frontend standalone usa
   `/usr/bin/node server.js` en 3107. Controles de acceso, volúmenes y secretos
   por archivo serán parte del despliegue posterior.

La aceptación incluirá UI/API/auth/jobs, video y rostros CPU, OCR/fuentes,
YouTube con Node/EJS local y la fixture remota de 28 palabras/3 segmentos con
caché en milisegundos. No carga modelos Whisper/Torch locales. Los audios de
más de 24 MiB fallan explícitamente hasta una unidad de fragmentación; esta
propuesta no promete transcripción de 90 minutos.

## Comprobación local de datos

Estos comandos sólo inspeccionan entradas o ejecutan las pruebas de esta unidad:

```powershell
python docker/supoclip/prepare_cpu_inputs.py
python docker/supoclip/prepare_cpu_inputs.py --source-archive .tools/cache/phase4-sources/supoclip.tar.gz --source-tree .tools/evidence/supoclip-cpu-upstream-tree-f6.json
python -m unittest tests.test_supoclip_cpu_inputs
```

La segunda comprobación requiere los dos artefactos privados ya fijados; CI no
debe fingir que existen ni convertir su ausencia en aceptación de un build.
Las pruebas normales usan fixtures locales y el manifiesto público, sin red,
instalaciones, SDK de la aplicación, contenedores ni claves.
