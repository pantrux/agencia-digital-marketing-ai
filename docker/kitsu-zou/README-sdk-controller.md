# Controlador del SDK OpenCV mínimo de Zou

## Revisión 2: prerequisitos y permisos de construcción

La revisión anterior, freeze `4bd2fcc6…`, y sus cinco archivos se preservaron
por bytes en `.tools/evidence/phase4-zou-opencv-sdk-before-prerequisite-umask-v2/`.
El kit `311121ecc9daa44e9ad69409c1fff6fefc11c309e5f34d31e06bf9e5ae67ec9e`
permanece intacto. La nueva revisión crea otro kit y otras rutas derivadas;
Root debe respaldar sus pins antes de cualquier transferencia o recurso remoto.

Las etapas previas deben ser envelopes canónicos, con hash correcto, nombre
de etapa exacto y `success is True`, además de coincidir literalmente con la
bóveda. Un resultado negativo de SDK conservado como evidencia no habilita
export, approve o build. `stop` exige sólo prepare/create positivos y no depende
del resultado SDK. Cuando create sólo produjo un CID no verificado, la limpieza
exige prepare positivo, el intent físico exacto y el respaldo explícito
`create_unverified`; rechaza identidades divergentes y no sustituye un recibo
create positivo perdido por un intent. El CID todavía pasa por NoForeign antes
de detenerse.

El build invoca un wrapper de argv fijo dentro del contenedor:
`/usr/bin/bash -c 'umask 077; exec /usr/bin/bash "$@"' opencv-private-build …`.
Así fija la máscara del proceso que realmente ejecuta CMake, sin confiar en
la máscara del cliente Docker o del helper host. La receta y el empaquetador
originales conservan sus hashes. El cache nuevo se espera con modo 0600 y la
lectura estricta se conserva; no se amplía la lista de permisos admitidos.

Las regresiones se ejecutaron en Windows: verifican el argv constante y los
contratos de recibos/CID mediante fixtures, sin ejecutar Bash, Docker, CMake
o SDK. **No acreditan un modo POSIX real.** La comprobación de que el wrapper
crea un archivo 0600 partiendo de umask 022 queda pendiente de un fixture Linux
autorizado por Root antes del SDK. No se presenta Git Bash como Linux nativo.

Esta unidad prepara un controlador local, un ejecutor POSIX, un perfil fijado
y pruebas. Se materializaron únicamente **datos locales** en la raíz privada
autorizada. No se ejecutaron SSH, Docker, APK, CMake, compiladores o código de
OpenCV/NumPy. No acredita todavía un SDK instalado, una rueda compilada, su
import nativo o una imagen runtime. `build_enabled` y `deployment_enabled`
siguen cerrados en el perfil inicial.

El alcance es exclusivamente `opencv-python-headless==4.13.0.92+agency1` para
Zou: `core,imgproc,imgcodecs,python3`, PNG, HDR y OpenEXR externos. No incluye
VideoIO/DNN/rostros de SupoClip ni cambia sus fuentes o locks. Conserva las
cinco fuentes congeladas descritas en [README-native.md](README-native.md),
incluido el manifiesto original `c4445291…` con `build_enabled=false`.

## Materiales derivados y procedencia

La base es Wolfi amd64 por manifiesto
`ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72` y
configuración `b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7`.
Se comprueban ambas identidades, arquitectura, usuario, entorno y etiquetas
de la imagen local. No se permite pull automático o un tag mutable.

Los 69 APK originales, dos paquetes de headers, fuente OpenCV, wheel NumPy,
índice/clave y recibos previos se conservan por bytes. La receta usa arrays
Bash, mientras que la clausura inicial no incluía ese intérprete. Root eligió
añadir **bash 5.3-r14** en un derivado separado, conservando la receta ejecutable.
No se tradujeron sus arrays/flags a Python.

El APK Bash ocupa 784.966 bytes; SHA256
`28274b1abce489dd947e0b517935992a3b069647b7f3a4d8eb95bb262c0751d1`.
Su ejecutable `usr/bin/bash` tiene SHA256
`5f8861f3c785052ebd21d05b776958a5605f7ebb10eb9ee4d9d5995c60420f1c`.
Se verificó la cadena índice RSA256 fijado → Q1 del control → SHA256 del payload
y el SHA completo. No se atribuye al APK una firma individual inexistente.
Su clausura reutiliza dependencias presentes: **70 APK managed, dos headers
directos y 19 de la base; 271 aristas estáticas**. Bash conserva GPL-3.0-or-later
como herramienta del SDK; no se añade a la rueda o al runtime de Zou.

La SCA de esas **91 identidades APK y NumPy 2.4.6** usó la DB vigente de
2026-11-06. Cobertura exacta, cero HIGH/CRITICAL y diez MEDIUM conservados de
Python r0. Los reportes y sus SHA constan en el perfil. Es auditoría de entradas
autenticadas, no del futuro SDK instalado ni de la rueda. La DB debe seguir
vigente al autorizar `sdk`; no se omiten findings ni se utilizan ignores/VEX.

El [perfil](opencv-minimal-sdk-profile.json) fija el inventario completo del kit,
su SHA y `build_id` derivado de sus primeros 32 caracteres, imagen, nombres,
rutas, herramientas, recursos y procedencia de código. No contiene credenciales.
El SDK usa la receta y empaquetador congelados, el verificador nativo y extractor
fijados. Las primitivas de FD/proceso se copiaron explícitamente del controlador
PostgreSQL V4 acreditado, sin cambiarlo ni interceptar sus funciones.

## Raíces y propiedad

Root respaldó antes de crearla la raíz local
`D:\Proyectos\Agencia Digital de Marketing AI\.tools\kitsu-zou-opencv-sdk-v1`,
owner `agencia-digital-marketing-ai:opencv-zou-minimal-sdk:v1`. Su marcador liga
ruta, dev/inode y owner; ACL restringida al usuario, SYSTEM y Administradores.
Cada kit vive en `kit-<sha16>` y se publica con creación exclusiva. Un cambio
de código crea otro kit; no sobrescribe ni adopta una copia anterior.

Los destinos remotos se derivan del `build_id` del perfil:

| Recurso | Ruta |
| --- | --- |
| Staging jandrade:0700 | `/home/jandrade/.agencia-opencv-zou-stage-<build_id>` |
| Namespace de entradas root:0700 | `/opt/media-platform/build/contexts/opencv-zou-minimal/<build_id>` |
| Inputs | `<namespace>/inputs` |
| Fuente, overlay y headers | `<namespace>/source`, `overlay`, `numpy-headers` |
| Trabajo/salidas root:0700 | `/var/lib/media-platform/build/opencv-zou-minimal/<build_id>/outputs` |
| Control y logs, fuera del SDK | `/var/lib/media-platform/build/opencv-zou-minimal/<build_id>/control` |

El layout de `/opt/media-platform` y `/var/lib/media-platform` debe coincidir
con sus identidades, marcadores y recibos respaldados. No se adoptan directorios
por nombre ni se siguen enlaces o sistemas de archivos cruzados. Los archivos
de entradas/control son root:0600; se conservan estados parciales para revisar
un fallo, sin borrar árboles por patrón.

`prepare` verifica los bytes del staging antes de promoverlos. Extrae la fuente
completa sin enlaces, selecciona los 29 headers NumPy y los 195 headers/configs
Imath/OpenEXR, y construye el overlay de bibliotecas autenticadas. Los enlaces
SONAME sólo pueden apuntar a otro nombre seleccionado del mismo directorio.
No instala los dos APK de headers ni PyImath/Python 3.13. Se respaldan pruebas
de árbol por identidad/hash de fuente, overlay y headers, que se vuelven a
comprobar antes de las etapas reales.

## Aislamiento y plazos

El contenedor temporal usa UID/GID `0:0` sólo para instalar el SDK offline.
Límites: 2 CPU, 6 GiB sin swap, 128 procesos; network none, sin puertos,
dispositivos, privilegios o capacidades añadidas, cap-drop ALL y NNP. El rootfs
del SDK es escribible deliberadamente; inputs/fuente/overlay/headers se montan
RO y sólo `/work` es RW. El control host y sus logs nunca se montan en el SDK.
No hay volúmenes anónimos ni montaje de NAS, Docker socket o credenciales.

El contenedor se crea detenido. Su CID se devuelve y persiste antes de iniciar.
Si create devuelve un CID y la inspección falla, el CID no verificado se
conserva y Root lo respalda antes de cualquier otra operación.

El presupuesto de comando es SDK 600 s y build 1.800 s, con 4 MiB **combinados**
de logs por etapa. Los comandos se ejecutan en un grupo propio cuyo líder se
retiene hasta cerrar el grupo, sin limpiar procesos por nombre. Heartbeat de
un segundo y timeout de cinco detectan pérdida del CLI; el cierre de un canal
o error detiene únicamente el CID acreditado. Los plazos externos incluyen
reserva para metadata y cleanup: 720/1.920 s. No garantizan resolver un proceso
en estado kernel D; un fallo de cierre queda visible.

`stop` sólo verifica layout/control local, recibo create/CID y configuración
del contenedor. No depende de volver a abrir el kit, árbol SDK, inputs o NAS.
No borra el contenedor, el contexto ni artefactos de otros roles. Root debe
coordinar este SDK con otros builders para evitar picos conjuntos de recursos.

## Sin Git ni descargas de construcción

El SDK recibe exactamente los archivos APK del manifiesto, sin glob. La
instalación utiliza repositorios vacíos, `--no-network`, `--no-scripts` y el
solver completo, **sin `--no-deps`**. `--allow-untrusted` es necesario por el
formato Wolfi sin firma individual y se usa sólo después de la cadena externa
autenticada y los pins completos; no cambia confianza global.

Se leyó la fuente CMake fijada: `CMakeLists.txt` pide Git sin `REQUIRED`,
`ocv_git_describe` y el mirror sólo ejecutan comandos si `GIT_FOUND` es verdadero.
La receta aplica `CMAKE_DISABLE_FIND_PACKAGE_Git=ON` desde un cache nuevo y
`OPENCV_VCSVERSION=4.13.0+agency1`. Esa
[variable de CMake 3.31](https://cmake.org/cmake/help/v3.31/variable/CMAKE_DISABLE_FIND_PACKAGE_PackageName.html)
deshabilita búsquedas opcionales; no se reutiliza un cache con Git detectado.
No se encontraron llamadas ExternalProject/FetchContent en los archivos CMake
del TAR revisados. El recibo estático enumera archivos/líneas y sus límites;
no se presenta como una traza real de configuración.

La atestación exige Git ausente y verifica hashes de CMake, Ninja, GCC/G++,
Python y Bash instalados, más la DB APK exacta, sin upgrades de la base ni
dependencias omitidas. Los módulos de descargas opcionales quedan desactivados
por el mínimo congelado; network none es otra barrera. No se cambia la receta,
se ejecutan hooks de red ni se usa Buildx/Git para estos pasos.

## CLI de Root y respaldos

El modo predeterminado sólo lee perfil/código local: **no lee `.env` ni abre
SSH**. Cada acción real exige `--root-update-vault`, usa Paramiko 5.1.0 aislado,
una única identidad Ed25519 y pin de host exactos, sin agente, claves buscadas,
contraseña alternativa o Ignore. Se verifican la pública cliente y huella antes
de conectar a `jandrade@192.168.0.85:22`.

El lector propio admite la bóveda completa hasta **8 MiB**, con FD regular,
NoFollow, un solo enlace, tamaño/identidad estables, ACL privada y parser
canónico. No reutiliza el límite de 2 MiB del builder congelado. Nunca imprime
la bóveda, claves, valores de credenciales o diagnósticos de parsing con datos.

```powershell
python docker/kitsu-zou/run_opencv_minimal_sdk.py
# Los pasos siguientes corresponden sólo a Root después de revisar y respaldar pins.
python docker/kitsu-zou/run_opencv_minimal_sdk.py plan --root-update-vault
python docker/kitsu-zou/run_opencv_minimal_sdk.py prepare --root-update-vault
python docker/kitsu-zou/run_opencv_minimal_sdk.py create --root-update-vault
python docker/kitsu-zou/run_opencv_minimal_sdk.py start --root-update-vault
python docker/kitsu-zou/run_opencv_minimal_sdk.py sdk --root-update-vault
python docker/kitsu-zou/run_opencv_minimal_sdk.py export --root-update-vault
python docker/kitsu-zou/run_opencv_minimal_sdk.py approve --root-update-vault
python docker/kitsu-zou/run_opencv_minimal_sdk.py build --root-update-vault
python docker/kitsu-zou/run_opencv_minimal_sdk.py stop --root-update-vault
```

Cada transición es independiente; no inicia la siguiente. `prepare` respalda
el staging antes de promoverlo. El CLI de Root respalda plan, prepared, CID y
recibos en campos `MEDIA_ZOU_OPENCV_SDK_{CONTROLLER,PREPARED,CONTAINER,STAGE_RECEIPTS}_B64`.
También se exigen ROOT/OWNER/IDENTITY, PROFILE/CONTROLLER/REMOTE/KIT SHA256 y los
paths remotos del perfil, todos fijados antes de usarse. No se sobrescriben
recibos históricos ni se reintenta automáticamente una etapa fallida.

`export` produce sólo el rootfs del SDK propio en el control host, máximo 3 GiB,
y devuelve su ruta/hash/tamaño para recepción y SCA acotadas por Root. Antes
de `approve`, Root debe auditar el SDK real, incluidas herramientas/bibliotecas
embebidas. La prueba externa `MEDIA_ZOU_OPENCV_SDK_SDK_SCA_B64` es un objeto
cerrado con `kind=opencv-minimal-sdk-sca-v1`, `verified=true`, `kit_sha256`,
`base_manifest`, `sdk_receipt_sha256`, `high_critical=0`, `report_sha256` y
`db_next_update_utc` vigente. No basta el informe de inputs o la base sola.

`approve` construye un derivado que sólo habilita los tres campos de aceptación
necesarios para la receta. Root respalda su B64/SHA **antes** de publicarlo en
inputs; el original c444 y el derivado SDK cerrado permanecen intactos. Build
exige ese recibo previo idéntico; la receta mantiene sus validaciones de cache,
PNG/OpenEXR, módulos efectivos, ELF y generación de rueda.

Después de build, Root respalda ruta/SHA de la rueda antes de usarla. Siguen
pendientes su SCA final, import real CPython 3.12/NumPy 2.4.6, PNG/HDR/EXR y
la prueba de thumbnail de Zou. El controlador no certifica esos resultados ni
modifica el lock de las 142 ruedas o la fase declarada de la plataforma.

Las pruebas de esta unidad usan fixtures/dobles y no arrancan contenedores:

```powershell
python -m unittest tests.test_opencv_minimal_sdk
```
