# Wheel OpenCV derivado por reubicación de RPATH

Esta unidad trata exclusivamente los nueve archivos ya capturados en `.tools/backup-source-build/opencv-relocation-inputs-v1`. No recompila, importa `cv2`, ejecuta ELF/SDK, usa Docker ni conecta al lab. El plan inspecciona bytes y ACL locales; todavía no crea el wheel ni acredita importación nativa o SCA.

El recibo de captura tiene SHA256 `0a7a51e8b6178de8fbe717df453710c1404b77cd59a1fa0819cef76961735a04`. Se fijan además el marcador privado `.owner.json` (`250051cd…`), su device/inode local y los nueve tamaños/hashes. La observación de CID detenido pertenece a esa captura; no representa una nueva consulta del contenedor. El árbol de entrada sólo admite esos nueve datos y los dos controles `.owner.json`/`receipt.json`.

La spec pública `opencv-relocation-spec.json` fija cuatro ELF, cache/config/modules, manifiesto aprobado y LICENSE. Core e imgproc se copian sin modificar. Sólo cambian:

| Archivo | Intervalo de archivo, final excluido | SHA256 derivado |
| --- | --- | --- |
| `stage/cv2.cpython-312-x86_64-linux-gnu.so` | `[58234, 58251)` | `e91ef1901f5567f385959046a57267a18dfb836dd795605d460bc95b46ae4f10` |
| `stage/lib/libopencv_imgcodecs.so.4.13.0` | `[21186, 21203)` | `cfc465b962b09320835466a3a7c7511b584b1499d3ea488787a58dc4a6bf7e82` |

En ambos intervalos se sustituyen únicamente los 17 bytes `:/overlay/usr/lib` por ceros. El primer cero termina la cadena antes; el NUL original se conserva. El resultado es `$ORIGIN/opencv_python_headless.libs:$ORIGIN`. No se desplazan ni redimensionan tablas, segmentos, símbolos o secciones. Se compara todo byte anterior y posterior al intervalo. El GNU build-id queda como dato heredado de la compilación: no se recalcula ni se presenta como identidad de los nuevos bytes. La identidad derivada es el nuevo SHA256 más el recibo de transformación.

## Rechazos y validación

El derivador exige ELF64 little-endian ET_DYN amd64, sección dinámica única, DT_RPATH único, ausencia de RUNPATH, límites válidos y correspondencia entre DT_STRTAB/DT_STRSZ, sección `.dynstr` y su mapeo PT_LOAD. La cadena debe coincidir completamente con el valor observado, empezando en un límite de cadena.

Se inventarían referencias de las etiquetas dinámicas de cadenas, todos los símbolos enlazados a `.dynstr` y nombres de archivos/versiones GNU verneed/verdef, incluidos sus auxiliares. Otra referencia que comparta cualquier parte de la cadena original provoca rechazo. Se rechazan secciones de semántica no cubierta enlazadas a `.dynstr`, etiquetas dinámicas desconocidas, ciclos, referencias fuera de límites y solapamiento físico de `.dynstr` con otras secciones o tablas. No se usa patchelf ni se ejecuta el ELF para resolver ambigüedades.

Los parsers congelados se compilan por bytes/SHA como módulos de datos, sin monkeypatch:

- `final_image_remote.py`, SHA256 `9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49`: comprueba metadata ELF antes/después. Sólo puede variar `rpath`.
- `package_opencv_minimal.py`, SHA256 `831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9`: comprueba configuración y cada ELF; la publicación llama directamente a su `write_wheel` intacto.

El plan real comprobó ambos intervalos en memoria: cv2 tiene 835 referencias inventariadas e imgcodecs 352. Ambos pasan el parser del empaquetador tras la transformación. Los otros dos ELF conservan exactamente sus bytes/hashes. Esto no acredita ejecución del resultado.

## Plan por defecto y publicación exclusiva de Root

```powershell
python -I -S -B docker/kitsu-zou/prepare_opencv_relocated_wheel.py
```

El plan no lee `.env`. Valida la raíz privada mediante el lector ACL fijado `ae1c6d5e…` y el marcador existente de `backup-source-build`. En Windows ese lector consulta metadatos con Windows PowerShell integrado; no ejecuta SDK ni los archivos capturados. Las lecturas rechazan symlinks, reparse, hardlinks y cambios, conservando comparaciones de ctime separadas por API. La spec cierra rutas y evita traversal, ADS, rutas absolutas y dispositivos Windows.

Root revisa las cuatro fuentes y respalda primero los campos sugeridos `MEDIA_ZOU_OPENCV_RELOCATED_WHEEL_V1_{ROOT,OWNER,SPEC_SHA256,CONTROLLER_SHA256,PLAN_SHA256}`. La raíz prevista, todavía ausente, es `.tools/backup-source-build/opencv-relocated-wheel-v1`; owner `agencia-digital-marketing-ai:opencv-relocated-wheel:v1`. El CLI usa el hash externo del plan y no escribe la bóveda:

```powershell
python -I -S -B docker/kitsu-zou/prepare_opencv_relocated_wheel.py --apply --expected-plan-sha256 '<hash revisado>'
```

Root invoca esta segunda forma después de la revisión. Recalcula el plan, exige salida ausente y parent privado/identidad iguales, crea una raíz nueva y su marcador, conserva el plan y escribe sólo archivos EXCL. Copia los nueve datos, aplicando la transformación únicamente a los dos ELF. No modifica el stage histórico ni el SDK. Relee todos los originales y copias antes del empaquetado, y vuelve a cotejar los originales después.

`write_wheel` mantiene nombre/versión `4.13.0.92+agency1`, metadatos, requisitos NumPy upstream, origen propio y licencias Apache/NumPy. Después se verifican los miembros cerrados, los cuatro ELF, ambas licencias y cada fila de RECORD. El recibo adicional conserva hashes de fuentes/entradas/wheel, intervalos, identidad de salida y flags de ejecución/SCA negativos. No se añade una modificación cosmética de versión ni se recalcula el build-id.

Una salida existente, archivo ajeno o parcial previo bloquea la operación. Un error conserva lo escrito para diagnóstico; no se borra ni adopta nada automáticamente. La raíz privada reduce acceso de otros usuarios, pero no promete aislamiento frente al mismo propietario o administradores. Una futura aceptación Linux y la auditoría del wheel siguen siendo pasos distintos.

## Pruebas

```powershell
python -I -S -B -W error::ResourceWarning tests/test_opencv_relocation.py -v
python -m ruff check docker/kitsu-zou/prepare_opencv_relocated_wheel.py tests/test_opencv_relocation.py
```

Las pruebas usan ELF sintéticos, parsers congelados y archivos temporales. Cubren bytes externos intactos, alias por símbolos/dynamic/versiones, tablas desconocidas, offsets/RUNPATH ambiguos, rutas, hardlinks, árbol cerrado, publicación EXCL y gates previos. No generan un wheel ni ejecutan un binario. Los datos reales se han usado sólo para plan y validación en memoria.
