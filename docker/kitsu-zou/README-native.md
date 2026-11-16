# OpenCV mínimo propio: inputs y receta offline

Esta unidad prepara un build Linux GNU revisable de OpenCV 4.13.0 para Zou. **No ejecutó SDK, compilador, Docker ni código de OpenCV/Zou o de sus ruedas candidatas.** El manifiesto [opencv-minimal-artifacts.json](opencv-minimal-artifacts.json) mantiene `build_enabled=false`; la preparación y aceptación del SDK corresponden a un controlador posterior, con rutas/pins respaldados antes de usarlo.

## Identidades fijadas

- Fuente OpenCV 4.13.0, commit `fe38fc608f6acb8b68953438a62305d8318f4fcd`, tar oficial de 95.450.266 bytes, SHA-256 `6a7508554941c1a698c243b2212b2985ce59a65a3e0348d53c2158607d801e61`. Se conservó intacto el tar de investigación y se copió por hash al kit.
- Distribución propia **`opencv-python-headless==4.13.0.92+agency1`**, `cv2.__version__` objetivo **4.13.0**, tag **`cp312-cp312-linux_x86_64`**. `packaging.specifiers.SpecifierSet` comprobó realmente que `==4.13.0.92` admite esta versión local; no se cambió el pin de Zou ni el lock de 142 ruedas.
- Python release **3.12.16-r0**, binario y headers exactamente emparejados. El candidato anterior `3.12.16_git20261030-r0` permanece intacto y no se presenta como la misma procedencia. Headers del wheel NumPy **2.4.6**, SHA-256 `90f9849678c75fe7afa2d348ac842c168b0a4d3d61919687216dfc547976d853`, con 29 archivos inventariados.
- Base Wolfi amd64 por digest `sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72`; su informe Trivy fijado acredita cero HIGH/CRITICAL **sólo en la base**, no en los inputs añadidos. Sus 19 paquetes se conservan sin downgrade; incluye glibc 2.44 y no se fuerza la selección antigua glibc 2.43 de otros candidatos.

Los dos `Requires-Dist` NumPy de la rueda original se preservan literalmente. El wheel propio declara su origen y versión local, copia LICENSE de OpenCV y el aviso de NumPy, genera METADATA/WHEEL/RECORD nuevos y no reutiliza hashes del wheel PyPI. No se etiqueta manylinux: no se ha acreditado ese baseline GNU. Las licencias de APK de runtime se conservan en su inventario/imagen; este README no supone que Apache-2.0 describa por sí sola toda la imagen de Zou.

## Autenticación real de entradas Wolfi

El índice `9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd` coincide con el snapshot anterior. Se verificó su firma **RSA-SHA256** con la clave pública Wolfi SHA-256 `f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc` antes de consumir APK. [package_opencv_minimal.py](package_opencv_minimal.py) reutiliza sin modificar el verificador Alpine congelado [native_inputs.py](../postgres-immich/native_inputs.py), SHA-256 `b00b1a1a1822b13ed145128209664a15f3efa781b39cd850d0d63bcc0666192b`.

Los APK Wolfi observados empiezan por control gzip con `.PKGINFO` y `.melange.yaml`, sin una sección `.SIGN` individual. El wrapper nuevo exige ese formato y valida la cadena **índice RSA256 autenticado → Q1/SHA1 del control comprimido → datahash SHA256 del payload comprimido**, además de nombre, versión, arquitectura, tamaño, integridad gzip, ausencia de bytes extra y SHA-256 externo del archivo completo. No se atribuye a cada APK una firma RSA256 individual. Se mantiene visible el eslabón Q1 heredado del formato APK v2; el hash completo fijado añade un pin fuerte para los usos posteriores.

Se descargaron y verificaron **69 APK managed**, 248.453.262 bytes, y dos paquetes de headers. Sus SHA-256 completos están en el manifiesto y sus inventarios/recibos en la raíz privada ya respaldada `MEDIA_OPENCV_INPUTS_ROOT`. La clausura estática tiene **266 aristas explícitas**, sin omitir dependencias con `--no-deps`; la confirmación del solver APK nativo queda pendiente. La clausura de codecs de runtime es separada: diez paquetes y 44 aristas, sin compiler, CMake, FFmpeg, OpenSSL añadido o Python 3.13. La base ya contiene su TLS actual; no se afirma que la imagen completa carezca de TLS.

Si el futuro preparador APK necesita `--allow-untrusted` para archivos sin firma individual, debe hacerse explícitamente **después** de esta validación externa y del pin completo, con red cerrada, repositorios remotos ausentes, scripts deshabilitados y argv de archivos exactos del manifiesto, nunca un glob de todos los APK. No se ejecutó tal comando en esta unidad ni se cambió una política global de confianza.

## Headers Imath/OpenEXR sin instalar PyImath

`imath-dev=3.2.3-r2` y `openexr-dev=3.5.3-r0` se tratan como **direct SDK payloads**, no como APK instalados. Se seleccionaron 40 y 155 archivos regulares autenticados respectivamente: headers C++ y configuración CMake/pkg-config. No se instalaron su metadata ni sus scripts en la DB APK. La dependencia declarada de imath-dev a PyImath Python 3.13 no desaparece de la evidencia: la selección evita ese componente en lugar de falsear que el paquete completo quedó instalado con todas sus dependencias.

Se excluyen los archivos `PyImath*`. `ImathConfig.cmake` sólo incluye `PyImathTargets.cmake` al solicitar ese componente; OpenCV/OpenEXR usan el target C++ `Imath::Imath`. El futuro overlay debe combinar los headers seleccionados y los payloads de bibliotecas de codecs de la clausura, preservando las rutas relativas de sus targets. Toda copia/enlace deberá tener recibo de nombre, fuente y hash; **ese overlay/SDK aún no fue materializado**. No debe pedirse `find_package(Imath COMPONENTS PyImath)` ni usarse Python genérico.

## Receta y barreras

La receta [build-opencv-minimal.sh](build-opencv-minimal.sh) sólo imprime un plan por defecto. Su ejecución futura requiere manifiesto habilitado y respaldado, SCA del SDK verificada, work privado precreado con modo 0700, salidas nuevas, únicamente interfaz loopback y montajes de inputs/fuente/overlay/headers de sólo lectura. Rechaza Windows. Las rutas Linux de build usan únicamente ASCII seguro sin espacios y se normalizan en el compilador mediante `-ffile-prefix-map`; el entorno del SDK se vacía y se fija fecha/locale. No prepara ni instala por sí misma un SDK: el controlador deberá acreditar su imagen, herramientas, hashes, límites y procedencia antes de invocarla. La ruta Ninja observada es `/usr/lib/ninja-build/bin/ninja`, fijada en `CMAKE_MAKE_PROGRAM`; no se supone `/usr/bin/ninja`.

El conjunto es `core,imgproc,imgcodecs,python3`, PNG externo, Radiance HDR y OpenEXR externo obligatorio. Se deshabilitan GUI/VideoIO, FFmpeg, GStreamer, CUDA/OpenCL, IPP/ITT/TBB, LAPACK/Eigen, codecs no pedidos y detección de Git. Git debe estar ausente del PATH. La red del contenedor debe estar cerrada; cambiar `OPENCV_DOWNLOAD_PATH` no es una prohibición de descarga. No hay comandos Git en la receta.

Tras configurar, el empaquetador comprueba CMakeCache y los headers generados: módulos efectivos mínimos, PNG/OpenEXR detectados, `OpenEXR_DIR` externo y flags sin fallback. El wheel usa la extensión directa CPython 3.12 (`OPENCV_SKIP_PYTHON_LOADER=ON`) y tres bibliotecas OpenCV bajo sus SONAME en `opencv_python_headless.libs`, con RPATH relativo `$ORIGIN`. Se evita copiar un loader que conserve paths absolutos del builder. El lector ELF no ejecuta `ldd`: rechaza TLS/FFmpeg/GUI ajenos, arquitectura incorrecta y rutas absolutas; exige que imgcodecs enlace PNG/OpenEXR externos. Eso no sustituye importar el resultado ni escanear toda la clausura nativa.

Para revisar sin SDK:

```text
python docker/kitsu-zou/package_opencv_minimal.py --manifest docker/kitsu-zou/opencv-minimal-artifacts.json
python -m unittest discover -s tests -p test_opencv_minimal.py -v
```

La revalidación de bytes necesita el SHA externo del manifiesto y la raíz del kit, con `--manifest-sha256 HASH --inputs-root RUTA --verify-inputs`. Se comprobó realmente el kit completo; no se importó NumPy/OpenCV ni se ejecutó código de los APK. Las pruebas usan RSA256 real con claves efímeras de fixture sólo en memoria, alteraciones de control/payload/identidad/índice y bytes extra, ELF sintéticos no importables y validación de RECORD/metadatos/reproducibilidad del ZIP. No afirman compilación, rendimiento o inferencia.

## Aprobación de inputs y trabajo posterior

Campos propuestos para que root respalde, sin escribirlos aquí: `MEDIA_OPENCV_MANIFEST_PATH/SHA256`, `MEDIA_OPENCV_PACKAGER_SHA256`, `MEDIA_OPENCV_RECIPE_SHA256`, `MEDIA_OPENCV_INPUTS_RECEIPT_SHA256` y `MEDIA_OPENCV_SOURCE_SHA256`. Una futura raíz de build y su recibo SDK necesitan contrato/pin propios antes de crearse o ejecutarse; no se inventan valores. El owner y la raíz de inputs actuales ya estaban respaldados.

Pendientes: SCA del SDK añadido, preparación aislada del SDK/overlay, confirmación APK nativa, compilación acotada, import real CPython 3.12/NumPy 2.4.6, `cv2.__version__`, PNG/HDR/EXR reales, prueba de `thumbnail.py` sin alterar su clamp/gamma, wheel instalado con dependencias y SCA de la imagen/salida completa. No se habilita el build ni se sustituye el lock de 142 ruedas con estas pruebas estáticas.

Psycopg sigue separado: `psycopg-binary==3.3.4` incluye OpenSSL 3.5.4 según evidencia estática previa. La alternativa oficial Python/C con libpq del sistema necesita retirar `[binary]` mediante una decisión explícita de metadata de Zou; añadir el extra `prodpg` conserva el binary base. No se ejecutó esa propuesta ni se borraron bibliotecas bajo una rueda. La limpieza de OpenCV no acredita la de Psycopg ni reemplaza su SCA/aplicabilidad pendiente.
