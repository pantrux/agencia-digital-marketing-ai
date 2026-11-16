# Proyección física mínima de libffi

El build V3 falló al copiar el directorio `rootfs/usr/lib64` sobre el enlace base `usr/lib64 -> lib`. El contraste de datos también encontró siete CLI manuales que sustituirían aliases base. Root autorizó omitir exactamente esas contribuciones accesorias y proyectar los tres nodos libffi. Esta unidad sólo deriva datos para una recuperación finita de Root. No modifica V3, sus intentos fallidos, el preparador, la base ni los inputs originales. No tiene CLI, red, procesos, instalaciones o escritura de archivos.

Las tres fuentes preliminares de proyección sólo libffi quedaron archivadas antes de esta ampliación en `.tools/evidence/phase4-python-libffi-projection-preliminary-v1.tar`, SHA256 `1a92048b2e44f3bec9de3fc0bcb56ed32224a6f659dbff97eef53bad062e5d1c`. Su aceptación de datos `660ae5eb…` mantiene las siete colisiones detectadas; no fue una aprobación del gate ni un build adoptado.

Son tres fuentes: [helper puro](project_libffi_context.py), [pruebas](../../tests/test_python_libffi_projection.py) y este documento.

## Entradas y cambio cerrado

El helper exige los bytes originales exactos:

| Entrada | SHA256 |
| --- | --- |
| Contexto original, 52.152.320 bytes | `f41fbd541699f2476db70ab3ae2d65c6cb369c3ce39b9070d86f9ed99b7c9d23` |
| Inventario original, 690.216 bytes | `4ccf6511e482a6656afde4e5310e380eaed505f3760c48cbe774e7125951a346` |
| Núcleo V3 | `cd320c4280fdd7b78ffa9ecc3c1b8cf9e8450af657ee8fb109e521b56c7a63b7` |

Las funciones reciben bytes por argumento; no buscan rutas o variables de entorno. El núcleo se compila desde los mismos bytes cotejados, sin `.pyc` ni modificación de globals.

`derive_context(contexto_original, fuente_core)` retorna `(tar_derivado, prueba)`. Primero reutiliza la derivación FROM congelada, cuyo contexto V3 es `e956a64f6c68b102b08fddd9b56747be1f4ad7240216272c07b64b6617fde5e1`; el Dockerfile queda con `FROM pythonbase`. Sobre ese contexto elimina el directorio `rootfs/usr/lib64`, omite los siete nodos explícitos de la siguiente sección y aplica:

| Origen del payload | Ruta física nueva |
| --- | --- |
| `usr/lib64/libffi.so.8.5.0` | `usr/lib/libffi.so.8.5.0` |
| `usr/lib64/libffi.so` | `usr/lib/libffi.so` |
| `usr/lib64/libffi.so.8` | `usr/lib/libffi.so.8` |

La biblioteca sigue teniendo 76.584 bytes, SHA256 `362c9592846776a36d5bb4b6deb85d5b031bbaa178699f7b73ed0d6f7f1a529b`, modo 0755 y UID/GID 0:0. Ambos enlaces conservan modo 0777 y destino relativo `libffi.so.8.5.0`. No cambian contenido, propietarios, permisos o destinos de los demás nodos retenidos. Una cabecera PAX de ruta, si existe, se proyecta al mismo nombre físico; los originales no se mutan. Se rechazan destinos ocupados y una rama lib64 distinta de esos cuatro nodos.

`derive_physical_inventory(inventario_original, fuente_core)` retorna un diccionario nuevo para el gate existente. Elimina la fila del directorio y las siete CLI omitidas, renombra las tres filas libffi de `files`, mueve la clave ELF de la biblioteca y elimina únicamente el ELF `usr/bin/clear`. Quedan 1.941 filas físicas y 111 ELF. Añade `physical_projection` con el mapa y los SHA de origen. Conserva el resto íntegro, incluidos los 14 materiales, paquetes, clausura, licencias, stdlib, load paths y flags de aprobación. Los registros APK/SPDX siguen describiendo el payload original: el mapa nuevo explica explícitamente la selección física. No se fabrican paquetes instalados ni una modificación de DB.

## Siete aportes manuales no proyectados

En la base, estos siete nodos son enlaces modo 0777, UID/GID 0:0, con destino literal `/bin/busybox`. Se conservan íntegros al excluir sus homónimos del COPY:

| Ruta | Contribución original que se omite |
| --- | --- |
| `usr/bin/clear` | Archivo ncurses, 17.208 bytes, 0755, SHA256 `47b7dca0f5b24e262cfae8016edfb5a7abf2c7f43d95ecd6a309f3072d55a7bb` |
| `usr/bin/reset` | Enlace ncurses 0777 hacia `tset` |
| `usr/bin/lzcat` | Enlace xz 0777 hacia `xz` |
| `usr/bin/lzma` | Enlace xz 0777 hacia `xz` |
| `usr/bin/unlzma` | Enlace xz 0777 hacia `xz` |
| `usr/bin/unxz` | Enlace xz 0777 hacia `xz` |
| `usr/bin/xzcat` | Enlace xz 0777 hacia `xz` |

Todas las contribuciones de origen tienen UID/GID 0:0. Un enlace no tiene un hash de archivo regular: su identidad es el destino literal y sus metadatos. El helper exige las siete identidades de origen; una ausencia, cambio de destino o hash distinto de clear bloquea la derivación. No omite `tset`, `xz`, bibliotecas, Python ni otro payload.

La prueba del contexto y el inventario incluyen `not_projected_manual_contributions` con los tipos, metadatos y hashes/destinos originales, `projected=false` y el alias base preservado. `complete_manual_payload_copied=false` evita atribuir una copia APK completa. Los `payload_complete` de los registros de materiales originales conservan su alcance de origen autenticado; no describen la selección final. No se altera su procedencia ni se presentan esos 14 aportes como paquetes instalados en la DB.

## Base y gate existentes

`check_base(tar_base, fuente_core, fuente_parser_9e0)` usa los lectores auténticos V3/9e0 y retorna su base OCI autenticada ce3d…/b70d…. Exige los enlaces base exactos `usr/lib64 -> lib`, `lib -> usr/lib` y `lib64 -> usr/lib`, más los siete aliases CLI hacia `/bin/busybox`, todos 0777 y UID/GID 0:0. También exige que los tres destinos libffi estén ausentes. No crea filas ficticias ni cambia esa base. Las rutas de carga originales `/usr/lib64:/usr/lib` siguen resolviendo la biblioteca a través del enlace base.

Root debe respaldar fuentes, hashes derivados, rutas y namespace propio nuevos antes de materializar o construir. Puede reutilizar los inputs/base positivos V3 como datos fijados; el build fallido y sus recibos quedan intactos. Esta unidad no es una factory V4 ni ejecuta una recuperación.

Tras el build y la captura nuevos, Root puede pasar directamente el inventario físico proyectado a `core.verify_image(base, final, inventario_proyectado, parser)`, con el núcleo cd320 intacto. Para leer el final debe usar `core.read_oci(tar_final, manifiesto_real, parser, base_raw=tar_base)`; se conserva la autoridad completa de las once capas base. No debe usar el gate V3 original con el inventario antiguo ni alterar sus recibos para simular una proyección.

La comprobación física conserva las 19 filas instaladas, los 14 componentes manuales, los archivos base, el alias y la ABI real. Una proyección de datos no acredita esa comprobación final, una imagen construida, SCA ni runtime. Los nuevos resultados mantienen `runtime_approved=false` hasta el cierre independiente de Root.

## Validación acotada

```text
python -I -S -B -W error::ResourceWarning tests/test_python_libffi_projection.py -v
python -m ruff check docker/python-runtime/project_libffi_context.py tests/test_python_libffi_projection.py
python -m ruff format --check docker/python-runtime/project_libffi_context.py tests/test_python_libffi_projection.py
```

Las pruebas autónomas usan fixtures de datos para cambios de ruta, preservación, PAX, colisiones y la omisión cerrada de siete CLI con sus negativos; llaman al resolver auténtico cd320 sin parches. No presentan esos fixtures como una biblioteca ELF aprobada o imagen final. La derivación de los inputs físicos f41/4ccf y lectura d51b se verifica por separado como datos, sin ejecutar su contenido.
