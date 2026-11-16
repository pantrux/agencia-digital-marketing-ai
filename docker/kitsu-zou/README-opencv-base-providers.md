# Captura de cinco proveedores OpenCV como datos

Esta unidad lee bibliotecas del export SDK existente y conserva sus bytes en un
namespace local privado nuevo. No inicia el SDK, no crea/exporta contenedores,
no instala paquetes ni ejecuta ELF. No añade bibliotecas a la rueda OpenCV ni
acredita que estén instaladas en un runtime. La captura de `build.ninja` y la
revisión de terceros estáticos quedan fuera de esta unidad.

El sujeto es el TAR propio de 680.871.936 bytes, SHA256
`52ecd3e45a5ea7477ed07bd12ba4ada56347ea84690b672174f7bf8b450c260c`.
La atestación histórica se reutiliza por su código congelado y se ejecuta como
lectura antes y después. Comprueba marcadores, recibos, árboles de entradas,
identidad del SDK, índice firmado y hash completo del TAR. Se exige SDK detenido
con PID 0, la misma identidad del archivo de la captura original y una atestación
sin cambios. No se convierte ese resultado histórico en un build nuevo.

La lectura adicional abre el control por descriptores sin seguir enlaces y
comprueba propietario root, permisos, inode, marcador y archivo regular sin
enlaces duros. Revalida el SHA completo por el descriptor usado para leer las
bibliotecas, y coteja sus metadatos antes/después. El TAR se interpreta en
memoria: enlaces simbólicos y duros sólo se resuelven dentro de su espacio
virtual; nunca se extraen al disco. Se rechazan duplicados, traversal, ciclos,
objetivos no regulares, terminadores incompletos y límites excedidos.

La selección cerrada contiene `ld-linux-x86-64.so.2`, `libc.so.6`, `libm.so.6`,
`libgcc_s.so.1` y `libz.so.1`. La respuesta admite como máximo 32 MiB; cada
biblioteca admite 8 MiB y el conjunto, 16 MiB. Root verifica SHA/tamaño y SONAME
con el parser ELF congelado antes de publicar un recibo positivo. Ese recibo
mantiene `sdk_executed=false`, `elf_executed=false` y `runtime_approved=false`.

## Operaciones de Root

El modo predeterminado sólo lee entradas locales y sus permisos:

```powershell
python -I -B docker/kitsu-zou/capture_opencv_runtime_materials.py
```

Root respalda primero la fuente local/remota, plan, referencias, owner y ruta
propuestos en `.env`; la plantilla conserva `CHANGE_ME`. Después ejecuta
`prepare` con los pins externos `--expected-source-sha256` y
`--expected-plan-sha256`. El destino fijo es
`.tools/backup-source-build/opencv-base-providers-v1`, bajo el padre privado y
marcador canónico ya acreditados. La preparación crea exclusivamente marcador,
plan y prueba de identidad. No abre SSH.

Root respalda inmediatamente el recibo `prepared.json`, su SHA e inode/device;
luego puede ejecutar `capture` con los mismos dos pins y el pin adicional
`--expected-root-proof-sha256`. El namespace debe contener exactamente los tres
archivos de preparación. No se adopta ni repite un intento anterior. Los fallos
conservan sus archivos propios. La bóveda sólo la actualiza Root.

La conexión usa el conector SSH ya congelado, con identidad de host fijada y
lectura de bóveda acotada a 8 MiB. El programa remoto sólo lee datos e inspecciona
el SDK propio a través del auditor previo. Tiene alarma de 150 segundos; el
padre limita la espera a 175 segundos y cierra su canal. No se transmiten claves
de la bóveda en el protocolo de captura. El contenido de los archivos fuente o
los documentos externos no se interpreta como instrucciones.

## Verificación

```powershell
python -I -B -W error::ResourceWarning tests/test_opencv_runtime_materials.py -v
python -m ruff check docker/kitsu-zou/capture_opencv_runtime_materials.py docker/kitsu-zou/capture_opencv_runtime_materials_remote.py tests/test_opencv_runtime_materials.py
python -m ruff format --check docker/kitsu-zou/capture_opencv_runtime_materials.py docker/kitsu-zou/capture_opencv_runtime_materials_remote.py tests/test_opencv_runtime_materials.py
```

Las pruebas portables usan TAR sintéticos y dobles sólo en las fronteras de
entrada/operaciones. Cubren alias virtuales, enlaces absolutos y duros, escapes,
ciclos, duplicados, truncado, presupuestos y pins antes de efectos. No afirman
haber ejecutado la captura Linux real. La comprobación ABI completa, terceros
estáticos, consultas de vulnerabilidades e importación de OpenCV son pasos
posteriores separados.
