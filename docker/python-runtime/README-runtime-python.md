# Factory de datos para runtime Python

Esta unidad prepara un contexto reproducible, sin construir ni arrancar imágenes. No usa SSH, Docker, SDK, consultas de vulnerabilidades o `.env`. El agente sólo ejecuta pruebas puras y el plan de lectura; Root revisa y respalda los pins antes de `--prepare`.

El alcance es Python 3.12.16 y su clausura auténtica sobre la base Wolfi fijada. No contiene las ruedas Flasgger, OpenCV o NumPy: se incorporarán en operaciones posteriores separadas. Tampoco copia el SDK89 como imagen, compiladores, CMake, Ninja, APKs `-dev` ni scripts de instalación.

## Fuentes y autenticación

[Los inputs](python-runtime-inputs.json) fijan el manifiesto aprobado OpenCV SHA256 `1207f5840f3d663775ee54fd377688ef00081681a1a50952df61c5d1ee01cd3a`, el índice Wolfi `9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd`, su clave pública `f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc` y cada APK local con su tamaño, SHA256, registro y recibo previo.

El [preparador](prepare_python_runtime.py) reutiliza por bytes/SHA los verificadores congelados `package_opencv_minimal.py` (`831a2804…`) y `native_inputs.py` (`b00b1a1a…`), el parser ELF `9e0a2829…` y el lector/ACL del relocalizador `b7b14e65…`. No modifica sus funciones ni acepta otra clave por defecto.

Vuelve a verificar RSA-SHA256 del índice, pertenencia del registro, Q1 del control, SHA256 del payload y hash completo de cada APK. Estos APK Wolfi no tienen firma individual; la cadena es **índice RSA256 → Q1 del control → SHA256 del payload**, además del pin SHA256 completo observado. Se cotejan `L` del índice y `license` de `.PKGINFO`. Los controles `.melange.yaml` y `.source.sha256` se catalogan como datos y no se ejecutan ni se copian como instrucciones.

El plan usa PyCA ya existente para la comprobación RSA, igual que el verificador congelado. No instala dependencias ni ejecuta nuevos binarios. Por eso el plan se invoca con `python -I -B`, sin `-S`; las pruebas puras sí admiten `-I -S -B` y no necesitan PyCA.

## Clausura finita

El resolvedor sólo utiliza los registros fijados, con preferencia por proveedores ya presentes en la base. Admite dependencias directas, virtuales y `so:` sin versión o con igualdad exacta, que son las formas observadas. Rechaza rangos, conflictos desconocidos, proveedores ambiguos, ausentes o cambios de clausura; no es un resolvedor APK general con fallback de red.

Los catorce payloads suman **18.711.242 bytes comprimidos**:

| Origen APK | Versión |
| --- | --- |
| python-3.12-base | 3.12.16-r0 |
| py3-pip-wheel | 26.2.2-r2 |
| gdbm | 1.26-r9 |
| libbz2-1 | 1.0.8-r30 |
| libexpat1 | 2.10.0-r1 |
| libffi | 3.8.0-r2 |
| libstdc++ | 16.2.0-r2 |
| libuuid | 2.42.4-r2 |
| mpdecimal | 4.0.1-r6 |
| ncurses y ncurses-terminfo-base | 6.6.20261031-r0 |
| readline | 8.3-r3 |
| sqlite-libs | 3.54.1-r0 |
| xz | 5.8.4-r1 |

Se conserva el payload Python completo, incluido `pyconfig.h`, que es dato del paquete base y no un compilador o APK `-dev`. `py3-pip-wheel` es dependencia real: permanece como dato, sin ejecutar pip/ensurepip ni instalar paquetes. Su código vendor embebido requiere cobertura separada; un resultado OS no certifica automáticamente esos vendors.

La base inmutable es `cgr.dev/chainguard/wolfi-base@sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72`, configuración `sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7`. Los diecinueve paquetes se cotejan uno a uno con el reporte físico existente SHA256 `419f999d5268db32a90b201ec9ec3a8dfba03fefab737f9cc4ac711c704da5fe`. Ese vínculo no sustituye un catálogo completo de archivos ni un SHA de la DB APK: ambos siguen pendientes y no se inventan.

## Extracción virtual y contexto

Los TAR de payload se analizan en memoria, con límites de nombres, archivos, tamaños y total. Se rechazan traversal, nombres duplicados/case collisions, dispositivos, FIFOs, setuid/setgid, permisos modificables por grupo/otros, xattrs, sparse, PAX desconocido y bytes ocultos después del terminador. El campo PAX `APK-TOOLS.checksum.SHA1` sólo se conserva como checksum declarado de cuarenta hexadecimales; no reemplaza la integridad SHA256 ni la firma del índice.

Los enlaces se resuelven en el espacio virtual del payload, sin seguir rutas del host. Se rechazan escapes y ciclos. Se comparte un directorio sólo si modo/propiedad/tipo coinciden; los archivos o enlaces que compiten por una ruta deben coincidir. Los mtimes originales compartidos se conservan como procedencia; los directorios compartidos se normalizan a cero y los archivos idénticos compartidos al menor mtime declarado.

Se preserva un único alias observado de ncurses como dato pendiente: `usr/lib/terminfo -> ../share/terminfo`. Su destino no está en estos catorce payloads; terminfo-base sí aporta `/etc/terminfo`. La ruta/target exactos se fijan en los inputs y no se reescriben ni se presentan como resueltos. Cualquier otro faltante, escape, ciclo o hardlink sin archivo conocido falla. La existencia/clasificación del alias se debe cotejar contra la base/final físicos antes del release.

El contexto es un **TAR**, con `Dockerfile` y `rootfs/` virtuales. No usa `extractall`, crea enlaces POSIX en Windows ni exige elevación del host. Emite directorios y archivos antes de enlaces; cada hardlink del contexto apunta al archivo regular final dentro de `rootfs`, conservando en catálogo su target original y la normalización. Conserva bytes, modo, uid/gid root, tiempos y checksums declarados. Los límites son 96 MiB por payload expandido, 16 MiB por archivo, 20.000 entradas y 128 MiB para el contexto.

Se prohíbe cualquier escritura del overlay bajo `lib/apk`, `etc/apk` o `usr/lib/apk`, y entradas de toolchain. Se conservan los SPDX que upstream incluye en `var/lib/db/sbom`; son documentos de origen del payload, no filas de paquetes instalados.

[El Dockerfile](Dockerfile.runtime-python) acepta únicamente `FROM`, `COPY`, configuración, usuario, directorio y un CMD base que requiere una operación explícita. No tiene `RUN`, frontend remoto, apk, red ni scripts. Declara carga sólo desde `/usr/lib64:/usr/lib`, porque libffi reside realmente en `/usr/lib64`; no ejecuta ldconfig ni modifica caches del cargador. El usuario de imagen es 1000:1000. Esto no acredita aún que las bibliotecas resuelvan: lo comprobará el gate físico.

## Inventarios y licencias

La salida separa dos BOM CDX 1.6:

- `base-os19.cdx.json`: diecinueve identidades instaladas observadas en el reporte de la base, más el sistema operativo.
- `manual-runtime14.cdx.json`: catorce componentes de **origen APK copiados manualmente**, más el contexto OS, con `apk_db_installed=false` en el inventario y propiedades explícitas de procedencia. Sus purl identifican artefactos APK auténticos, no una instalación realizada por apk.

No se agregan catorce filas a la DB ni se convierten treinta y tres componentes de cobertura en treinta y tres paquetes instalados. No se cambia `world`, la DB o el gestor de la base. Los hashes de esos archivos base/final todavía requieren captura física.

`inventory.json` conserva todos los archivos, enlaces, paquetes de origen, metadatos, controles, ELF y licencias. Se separan `L`/`.PKGINFO`, documentos SPDX físicos y candidatos a texto de licencia identificados por ruta. No se inventan textos ausentes ni se afirma aprobación jurídica universal. GPL, excepciones GCC, PSF, MIT y la licencia declarada de SQLite conservan sus identidades; no se etiquetan todos los payloads como MIT.

El plan real ha identificado 112 ELF y 1.401 entradas bajo `usr/lib/python3.12/`. Los parsers registran `PT_INTERP`, SONAME, `DT_NEEDED`, RPATH/RUNPATH, versiones y conteos/hashes de símbolos, sin ejecutar ELF. El intérprete está anclado a SHA256 `54bb87946dd0d8231507c0cef7a5745443a97411c406bf6f010f3ec75780a75b` y `libpython3.12.so.1.0` a `ad918e7753f89b9c256a6c40a17e48d080583cd25577d8634d737ab9a1e960b8`. La stdlib tiene catálogo/tree hash propio. Es identidad de bytes, no una prueba de importación ni un SCA de cero cobertura.

## Plan y preparación de Root

```powershell
python -I -B -W error::ResourceWarning docker/python-runtime/prepare_python_runtime.py
```

El comando lee, autentica y calcula el contexto en memoria; sólo imprime plan/hash. No crea el namespace. Root respalda las cinco fuentes, inputs, plan y estas identidades antes de preparar:

- `MEDIA_PYTHON_RUNTIME_V1_ROOT`: ruta absoluta `<workspace>\.tools\backup-source-build\python-runtime-v1`.
- `MEDIA_PYTHON_RUNTIME_V1_OWNER`: `agencia-digital-marketing-ai:python-runtime:v1`.
- Bajo el mismo prefijo: `INPUTS_PATH`, `INPUTS_SHA256`, `PREPARER_SHA256`, `PLAN_PATH`, `PLAN_SHA256`.

Sólo Root invoca:

```text
python -I -B -W error::ResourceWarning docker/python-runtime/prepare_python_runtime.py --prepare --expected-plan-sha256 SHA256_REAL_RESPALDADO
```

El padre debe tener ACL privada y el marcador canónico del árbol de compilación. La preparación crea el namespace nuevo por exclusión, comprueba ACL e identidad y relee los bytes antes del recibo positivo. Escribe `.owner.json`, `plan.json`, `inventory.json`, ambos BOM, `context.tar`, `runtime-image-gate.json` y `receipt.json`. No adopta ni sobrescribe una salida existente; si una escritura falla, conserva lo ya escrito para diagnóstico. Root respalda inmediatamente `PREPARED_PROOF_PATH/SHA256`, inventario y hashes de los outputs antes de construir. No se imprimen credenciales y nunca se llama al actualizador de bóveda.

## Gate de imagen y consumidor posterior

Esta unidad no tiene operación de build o start. `runtime-image-gate.json` exige, antes del primer intérprete/import:

1. Capturar OCI base/final y ligar manifiesto, configuración, archivo y capas reales.
2. Cotejar DB base/final con las mismas diecinueve filas y bytes; comprobar que las copias no alteran archivos base ajenos.
3. Cotejar cada payload físico, intérprete, libpython, stdlib, enlaces, modos y propietarios contra el catálogo esperado.
4. Resolver bibliotecas, símbolos/versiones y `PT_INTERP` contra proveedores físicos, y clasificar el alias de terminfo.
5. Obtener SCA con cobertura exacta de base, payloads manuales y vendors embebidos; conservar los avisos y límites reales.
6. Verificar configuración y aislamiento externo: UID/GID 1000, raíz RO, tmpfs propio, red `none`, una CPU, 512 MiB y 32 procesos.

Root respalda después los campos `MEDIA_PYTHON_RUNTIME_V1_{MANIFEST_SHA256,CONFIG_SHA256,ARCHIVE_PATH,ARCHIVE_SHA256,INVENTORY_PATH,INVENTORY_SHA256,ABI_RECEIPT_PATH,ABI_RECEIPT_SHA256,SCA_RECEIPT_PATH,SCA_RECEIPT_SHA256,ROOT_RELEASE_PATH,ROOT_RELEASE_SHA256}`. Los valores no se generan aquí.

[El consumidor Flasgger congelado](../kitsu-zou/README-flasgger-acceptance.md) requiere un `runtime-approval.json` físico específico de Root, ligado por el SHA en `linux-proof.json`. Su contrato usa `kind=flasgger-isolated-runtime-root-release-v1`, autorización del fixture, clausura verificada, manifiesto y SHA del recibo de seguridad, y el objeto Python exacto. La comprobación externa de libpython/stdlib/ABI debe preceder al arranque del intérprete; el check de Python desde el script no sustituye ese gate previo. La aceptación de Flasgger y el posterior overlay OpenCV/NumPy son unidades separadas, sin copiar toolchain.

Todos los resultados de esta preparación conservan `scripts_executed=false`, `sdk_executed=false`, `image_built=false` y `runtime_approved=false`. No se declara la base o runtime limpio por el reporte histórico, por ausencia de avisos o por cero paquetes detectados.

## Pruebas puras

```powershell
python -I -S -B -W error::ResourceWarning tests/test_python_runtime.py -v
python -m ruff check docker/python-runtime/prepare_python_runtime.py tests/test_python_runtime.py
python -m ruff format --check docker/python-runtime/prepare_python_runtime.py tests/test_python_runtime.py
```

Cubren clausura y ambigüedad, formatos no comprendidos, traversal/case collisions, privilegios y PAX, licencias/bytes, enlaces y alias pendiente, orden de hardlinks, contexto determinista, DB/toolchain protegidos y pin antes de efectos. No usan imágenes, SDK, SSH, red ni bibliotecas de los payloads.
