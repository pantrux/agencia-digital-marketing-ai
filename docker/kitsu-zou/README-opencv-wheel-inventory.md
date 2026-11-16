# Inventario del wheel OpenCV propio

Esta unidad produce datos verificables antes de importar código nativo. El sujeto es `opencv_python_headless-4.13.0.92+agency1-cp312-cp312-linux_x86_64.whl`, de 6.320.026 bytes y SHA256 `1c03ca69b42b10ca1ff75bdc64113ca2885d7f502edbba369b9779f2da1b51cd`. Es una distribución propia con versión local; no se identifica como el wheel oficial publicado en PyPI.

El plan es de sólo lectura. No consulta OSV, no ejecuta Trivy, no instala ni importa `cv2`, no ejecuta ELF ni SDK, y no accede al laboratorio. La publicación explícita de Root crea exclusivamente JSON nuevos; tampoco realiza esos ensayos.

## Entradas y comprobaciones

[La especificación](opencv-wheel-inventory-spec.json) fija los diez miembros del ZIP por nombre, tamaño y SHA256, así como el wheel, su recibo de relocalización, manifiesto de compilación, archivo fuente OpenCV, tres fuentes auxiliares congeladas y los recibos previos del SDK. El controlador rechaza ZIP incompleto o duplicado, traversal, `RECORD` incoherente y cambios de identidad o requisitos NumPy.

La fuente es OpenCV 4.13.0, commit `fe38fc608f6acb8b68953438a62305d8318f4fcd`; el archivo fuente tiene SHA256 `6a7508554941c1a698c243b2212b2985ce59a65a3e0348d53c2158607d801e61`. Se comprueban sus bytes existentes. La configuración se vuelve a validar sobre las capturas de `CMakeCache.txt`, `cvconfig.h` y `opencv_modules.hpp`, ligadas al manifiesto aprobado SHA256 `1207f5840f3d663775ee54fd377688ef00081681a1a50952df61c5d1ee01cd3a`.

Se reutilizan por bytes y SHA256 los verificadores de [relocalización](prepare_opencv_relocated_wheel.py), [empaquetado](package_opencv_minimal.py) y ELF del archivo `docker/postgres-immich/final_image_remote.py`. No se parchean ni se sustituyen sus funciones. Se cotejan además el wheel y los dos ELF derivados con los datos originales y la transformación ya aprobada.

Los cuatro ELF del wheel conservan `DT_RPATH=$ORIGIN/opencv_python_headless.libs:$ORIGIN`, sin `DT_RUNPATH`. Se registran `DT_NEEDED`, SONAME, requisitos/definiciones de versión, tamaños, SHA256 y conteos/hashes de símbolos dinámicos. El GNU build-id es un dato heredado de la compilación y permanece sin recalcular; la identidad de los bytes derivados corresponde al nuevo SHA256 y al recibo de transformación.

## Proveedores externos y alcance del SDK

Los nueve proveedores externos comprobados por bytes/SONAME corresponden a Imath, Iex, IlmThread, OpenEXR, OpenEXRCore, libdeflate, OpenJPH, libpng y libstdc++. Son candidatos del entorno Linux, no archivos contenidos en el wheel ni prueba de que el cargador los encuentre en el futuro runtime.

Se leen diez APK existentes, incluido Python, sin extraerlos al sistema de archivos. Para cada uno se cotejan tamaño/SHA256 con el manifiesto y el recibo de autenticación previo; luego se verifican identidad de `.PKGINFO`, Q1 del control y SHA256 del payload comprimido. La cadena original corresponde a índice Wolfi autenticado con RSA256 → Q1 del control → SHA256 del payload. No se afirma firma individual de estos APK ni una verificación RSA nueva en esta unidad.

Cinco SONAME quedan declarados por el manifiesto de base, sin bytes del proveedor en estas entradas: `ld-linux-x86-64.so.2`, `libc.so.6`, `libm.so.6`, `libgcc_s.so.1` y `libz.so.1`. Se conservan nombre, versión, arquitectura, licencia y Q1 del paquete declarado. Este hueco impide afirmar cierre ABI. Una futura captura finita del export SDK existente puede completarlo; no forma parte de esta unidad.

La aprobación de Root SHA256 `666f51e18365eb14a4d1c386fdf8c803e2b01076db75e9e296e4f5898a78ecca` cubrió 89 APK instalados, cinco entradas parciales y NumPy: 95 identidades. Conservó diez MEDIUM y autorizó sólo el build offline de esa época. El inventario liga ese recibo a su SBOM, recibo SCA, base de datos, atestación de herramientas y artefacto SDK. No transforma esa aprobación en una aprobación del wheel o del runtime ni la declara vigente por omisión.

Tampoco están cerrados los terceros compilados estáticamente o de cabeceras: no se capturaron mapa de enlace ni `build.ninja`. La configuración mínima reduce componentes, pero no prueba por sí sola qué código de terceros quedó incorporado ni su versión. El inventario conserva este límite incluso ante una respuesta vacía de un servicio de avisos.

## Plan y publicación de Root

Desde la raíz del proyecto, el comando predeterminado imprime sólo el plan y su hash; no crea la salida:

```powershell
python -I -S -B -W error::ResourceWarning docker/kitsu-zou/inventory_opencv_wheel.py
```

El plan incluye identidad del padre privado, hashes de entradas y de los tres documentos que se producirían. Root debe revisar y respaldar ruta, propietario, fuentes/especificación y SHA del plan antes de la siguiente operación. Campos propuestos, todavía sin escribir por el agente:

- `MEDIA_ZOU_OPENCV_WHEEL_INVENTORY_V1_ROOT`: ruta absoluta terminada en `.tools\backup-source-build\opencv-wheel-inventory-v1`.
- `MEDIA_ZOU_OPENCV_WHEEL_INVENTORY_V1_OWNER`: `agencia-digital-marketing-ai:opencv-wheel-inventory:v1`.
- `MEDIA_ZOU_OPENCV_WHEEL_INVENTORY_V1_CONTROLLER_SHA256`, `SPEC_SHA256` y `PLAN_SHA256` bajo el mismo prefijo.

Sólo Root publica, sustituyendo el marcador de texto por el SHA256 real respaldado del plan:

```powershell
python -I -S -B -W error::ResourceWarning docker/kitsu-zou/inventory_opencv_wheel.py --publish --expected-plan-sha256 SHA256_REAL_DEL_PLAN_RESPALDADO
```

La salida fija debe estar ausente. Su padre exige ACL privada y el marcador canónico del árbol de compilación. Se crea un directorio nuevo con `.owner.json`, `plan.json`, `inventory.json`, `wheel-source.cdx.json`, `osv-requests.json` y `receipt.json`, mediante exclusión. Se comprueban ACL e identidades y se releen los datos antes del recibo positivo. No hay adopción, reemplazo ni borrado automático; un fallo conserva lo ya escrito. La protección se basa en el árbol privado y sus padres administrados, sin prometer aislamiento frente a otro proceso con la misma identidad privilegiada.

Todos los resultados mantienen `scan_performed`, `queries_performed`, `security_approved`, `runtime_verified`, `elf_executed` y `sdk_executed` en `false`. El CDX 1.6 describe la fuente OpenCV y cuatro archivos ELF; no fabrica paquetes OS para bibliotecas manuales ni presenta cero componentes detectados como una certificación.

## Operaciones posteriores separadas

`osv-requests.json` propone una consulta exacta `POST https://api.osv.dev/v1/query`, cuerpo `{"commit":"fe38fc608f6acb8b68953438a62305d8318f4fcd"}`. La [documentación primaria de OSV](https://google.github.io/osv.dev/post-v1-query/) define la consulta por commit y la paginación: Root deberá conservar cada respuesta, reenviar `next_page_token` como `page_token`, rechazar ciclos y agotar todas las páginas, aunque una página no contenga avisos. Este controlador no ejecuta la consulta. El resultado por commit no cubre los proveedores externos ni terceros estáticos, y ausencia de avisos no equivale a ausencia de vulnerabilidades.

El posterior smoke debe ejecutarse en Linux Wolfi/glibc compatible con el runtime real, después de cerrar sus proveedores y recibir aprobación específica. La versión comprobada por manifiesto, APK, SBOM y atestación SDK es **Python 3.12.16**, paquete **python-3.12-base 3.12.16-r0**, ejecutable `/usr/bin/python3.12` SHA256 `54bb87946dd0d8231507c0cef7a5745443a97411c406bf6f010f3ec75780a75b`. La etiqueta CPython 3.12 no autoriza a sustituirlo por otra revisión ni a importar en el host Ubuntu suponiendo compatibilidad.

El ensayo deberá verificar importación, `cv2.__version__`, configuración efectiva y lectura de fixtures PNG/Radiance HDR/OpenEXR con escritura PNG, además del resultado de dependencias y ausencia de enlaces hacia VideoIO/FFmpeg/GUI/TLS prohibidos. Estas pruebas, SCA posterior, aprobación de seguridad y aceptación de Zou permanecen pendientes; aquí no se ejecutan.

## Pruebas locales

```powershell
python -I -S -B -W error::ResourceWarning tests/test_opencv_wheel_inventory.py -v
python -m ruff check docker/kitsu-zou/inventory_opencv_wheel.py tests/test_opencv_wheel_inventory.py
python -m ruff format --check docker/kitsu-zou/inventory_opencv_wheel.py tests/test_opencv_wheel_inventory.py
```

Las pruebas usan ZIP/TAR/APK sintéticos y directorios temporales: rechazan modificaciones, duplicados, traversal, identidad falsa, cobertura confundida y publicación sin pin o sobre un destino existente. También exigen que una relectura fallida impida escribir un recibo positivo. No importan `cv2`, no ejecutan binarios ni consultan servicios externos.
