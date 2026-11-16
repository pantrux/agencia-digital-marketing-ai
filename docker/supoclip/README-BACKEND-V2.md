# Backend SupoClip con FFmpeg 9

`Dockerfile.backend-v2` conserva el entrypoint de API y ARQ de V1 y
usa el TAR de aplicación V2. Mantiene las rutas `/usr/bin/ffmpeg`,
`/usr/bin/ffprobe` y `/opt/supoclip-ffmpeg-ass/bin/ffmpeg` que usan
las fuentes de SupoClip. La última es un alias al FFmpeg 9 compartido
que incorpora el libass estático corregido. El
[Compose privado](compose.functional.yml) sólo cambia
`MEDIA_SUPOCLIP_TRIAL_BACKEND_IMAGE` tras aceptar el nuevo digest.

## Contextos para el build

Conservar juntos `Dockerfile.backend-v2`, `assemble_backend_v2.sh`,
`backend_runtime_v2.py`, `backend_runtime.py` de V1 y
`verify_ffmpeg_ass.py` en el contexto de receta. Usar el mismo
operador Buildx local que en el ensayo V1, con una salida nueva,
`--network=none`, `--pull=false` y las tres opciones NoGit.

| Contexto | Entrada acreditada |
| --- | --- |
| `wolfi` | OCI local Linux amd64 `ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72` |
| `stage` | `/opt/media-platform/supoclip-backend-stage-v1/inputs`, manifest `e850b85f8e00ef7727bdc5a01c2eb836ce997d27a85ac12d884ae38c8d8f8c71` |
| `products` | Directorio nuevo verificado con los siete nodos listados abajo |

Preparar `products` en una raíz propia con archivos ordinarios,
permisos de lectura para la prueba UID 1000 y huellas verificadas.
Mantener sus dos montajes de build como sólo lectura.

| Ruta en `products` | Origen |
| --- | --- |
| `backend-source.tar` | V2 local de 10.567.680 bytes y SHA `787707a963609f8e1f33b8c7ca6bd58a49924b59718c0412d5bd5a82ec069c2e` |
| `backend-source-receipt.json` | `receipt.json` vecino del TAR V2, SHA `9148e1efb0181241f1e0523de55d01533010f1068e87890731b45c82df84f7ca` |
| `ffmpeg-receipt.json` | Bytes canónicos del recibo físico `link-finish-status-v1.json`, SHA `f4cbdbac5879e485333b72a904195e74d502583cdcf692c2215e84ef14aeec4c` |
| `media/` | Los nueve archivos regulares que enumera `products` en ese recibo. Preservar rutas relativas `bin/` y `lib/` desde el prefijo FFmpeg 9 promovido. Los alias se generan durante el build. |
| `wheel/` | Los tres archivos de la captura `backend-wheel-v2`. El argumento `SUPOCLIP_WHEEL_MANIFEST_SHA256` fija el SHA físico de su `manifest.json`. |
| `verify_opencv_expanded.py` | Sonda V2 del contexto OpenCV, `inputs/fixtures/verify_opencv_expanded.py`, SHA `fb5a6c1a30b04bb2a0eaeb75e16ae2eff48a48a786482d74a3257c58fa229b1c` |
| `ass-fixture.ttf` | Fuente histórica `ass-fixture-v1.ttf` en `/var/lib/media-platform/build/supoclip-ffmpeg-sdk-v3`, SHA `559f38bdfcf12beead8a744579715dc7249d6978616e757de7d8cf06403aa531` |

El instalador verifica el contexto original y genera una vista
efímera del repositorio. Conserva intacto el índice firmado y sólo
ofrece los 320 APK seleccionados. Exige 19 paquetes antes de instalar
y 339 después. La vista efímera y los nueve APK FFmpeg 7 del contexto
histórico no pasan al OCI. Mantiene los 83 wheels originales e
instala la rueda OpenCV V2 como distribución número 84.

## Límites de aceptación

El SDK OpenCV necesitó un prefijo de desarrollo con headers,
`.pc`, alias de enlace y una copia de FriBidi. El runtime toma dos
CLI y siete ELF acreditados, instala los siete alias SONAME y usa
FriBidi 1.0.18-r2 y dav1d 1.5.4-r4 del inventario APK. La huella de
la DSO FriBidi debe coincidir con el proveedor autenticado. No se
instala el SDK ni sus archivos de desarrollo en esta imagen.

La sonda de build ejecuta sin red y con UID/GID 1000 el loader y los
CLI desde las rutas finales, sin un `LD_LIBRARY_PATH` adicional.
Comprueba los paquetes, los archivos y los SONAME activos y rechaza
FFmpeg 7. Ejecuta el render ASS sintético con prueba de píxeles,
H.264/AAC y extracción MP3, seguido de lectura y seek OpenCV con
Haar, YuNet y flujo óptico. La carga de libav debe venir de
`/usr/lib`. El archivo `/opt/supoclip/bin/media-runtime-v2.json`
registra las huellas de los binarios y la rueda instalados.

El dato aún pendiente para construir es la captura positiva de
`opencv-python==4.13.0.92+agency2` con su manifiesto. Tras construir,
respaldar el digest OCI, comprobar la proyección física y ejecutar
el ensayo de aplicación y la SCA de esta variante. El build no
concede aprobación de producción.
