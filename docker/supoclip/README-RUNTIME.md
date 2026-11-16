# Integración privada de SupoClip

El runtime se divide en backend API, worker ARQ y frontend Next. El API y el
worker comparten la imagen, el espacio local de medios y una base PostgreSQL y
un Valkey exclusivos del ensayo. Valkey usa la DB 0 y claves ARQ de nombre fijo.
El worker hace comprobaciones de almacenamiento y limpieza desde el arranque,
por lo que el ensayo debe usar directorios vacíos propios.

## Entradas físicas disponibles

| Entrada | Archivo | SHA256 |
| --- | --- | --- |
| Backend y adaptador Speaches, 177 archivos | `supoclip-runtime-source-v1/backend-source.tar` | `075be34ef695acd38c9a02b27a50c18445b715ed9fa6a91fb62b7f5c98e641c8` |
| Layout y tres fuentes locales con OFL, 7 archivos | `supoclip-runtime-source-v1/frontend-font-overlay.tar` | `4af3e14ed7a614ac3e8c067d04f536d6713b4916d25c13105fb1ff99bb0fe778` |
| Ruedas verificadas, 82 de registro y SRT propio | `supoclip-backend-wheelhouse-v1/wheelhouse-preopencv-v2.tar` | `bcea6fecdfd66b0142567f21a2732d8f9f39cdbfd7d89333fc131400b300a200` |

Los tres TAR viven bajo `.tools/backup-source-build`. Los `receipt.json` de
cada raíz contienen ruta, tamaño y SHA por miembro. El formato PAX conserva
el nombre largo original de la rueda charset-normalizer.

`prepare_runtime_source.py` calcula el plan por defecto. Publica en una raíz
privada nueva con `--publish SHA256_DEL_PLAN`. El upstream es
`f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38`. El backend mantiene las fuentes y
transiciones upstream para el ensayo privado. La distribución de fuentes
incluidas por upstream requiere acreditar sus licencias individuales.

## Build del backend

Usar `Dockerfile.backend` con un contexto de receta que contenga
`assemble_backend.sh`, `backend_runtime.py` y
`verify_opencv_expanded.py`. El operador fija y acredita tres contextos
nombrados antes de construir.

| Contexto | Entrada |
| --- | --- |
| `wolfi` | OCI local Linux amd64 con manifiesto `sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72` |
| `stage` | Directorio recibido en .85 con `INPUTS.sha256` fijado en `e850b85f8e00ef7727bdc5a01c2eb836ce997d27a85ac12d884ae38c8d8f8c71` |
| `products` | Dos archivos ordinarios con las salidas verificadas de OpenCV y FFmpeg ASS |

En `products` la rueda se llama
`opencv_python-4.13.0.92+agency1-cp312-cp312-linux_x86_64.whl`,
tiene 13.132.554 bytes y SHA256
`c1d04e99d5abebba09668def83943b36f0b242d6b7792d10515aa4d2d15dd9f7`.
El binario ASS se llama `ffmpeg` y su SHA256 es
`e8fb8dc1d20240de4017bbe0f56c06067cb96e62a4a859426ca129ee1fd905eb`.
Usar Buildx sin red ni pull, con `--build-context wolfi=oci-layout://…`,
`--build-context stage=…` y `--build-context products=…`.
Los dos directorios se montan de sólo lectura durante la instalación.

El build comprueba el inventario APK antes y después de instalar, con 19
paquetes de base y 329 adiciones exactas. Instala 84 distribuciones con pip
aislado, `--no-index --no-deps --require-hashes`, incluida
`opencv-python==4.13.0.92+agency1` con GUI deshabilitada. Verifica los 177
miembros de la fuente y los 84 miembros del archivo de ruedas previo a OpenCV.

Después de instalar, ejecuta como UID/GID 1000 una sonda sintética sin
red. Comprueba la carga de los FFmpeg stock y ASS, Node y Tesseract, y
VideoIO con acceso aleatorio a cuadros, DNN YuNet, Haar y flujo óptico.
El FFmpeg ASS sólo se selecciona en las cuatro funciones que lo usan.
El FFmpeg stock sigue disponible en `/usr/bin` para descarga, extracción
de audio y decodificación. La prueba de píxeles de la salida ASS consta
por separado en el recibo de aceptación de ese binario.

## Configuración del ensayo privado

El mismo entrypoint acepta `api` o `worker`. Usar un contenedor de
cada rol con UID/GID 1000 y directorios propios `/state`,
`/state/outputs`, `/state/temp` y `/opt/supoclip/backend/logs`, con
modo 0700. Los dos roles comparten output y temporales. Montar logs por
rol para separar sus archivos. El API sirve el puerto 8000 en la red
privada. El worker usa la cola `supoclip_tasks` con un trabajo simultáneo.

Fijar `REDIS_HOST` y, si aplica, `REDIS_PORT`. Montar los secretos
como archivos de sólo lectura UID/GID 1000 con modo 0400 o 0600.

- `/run/secrets/supoclip-database-url` con URL `postgresql+asyncpg`.
- `/run/secrets/supoclip-redis-password`.
- `/run/secrets/supoclip-backend-auth` con el valor compartido con el frontend.
- `/run/secrets/supoclip-speaches` con el bearer del puente.

El lanzador deshabilita dotenv, exige autenticación firmada y selecciona
Speaches y almacenamiento local. Verificar `/health`, `/health/db`,
`/health/redis` y `/health/worker` desde la red propia. Las pruebas
de aplicación y SCA se registran por separado del build.

## Frontend y orden de arranque

1. Usar el árbol y lock preparados en la [receta del frontend](README-FRONTEND.md).
   El árbol conserva `output: "standalone"` y usa el cliente Prisma con
   `engineType = "client"` y `@prisma/adapter-pg@6.19.3`. El lock congelado
   pasó su validación offline. Falta construir con el almacén completo en Linux.
2. Instalar con pnpm fijado, lock congelado, store offline y scripts de
   instalación deshabilitados. Ejecutar explícitamente la generación Prisma,
   typecheck y build Next sin red. Verificar el compilador de consultas WASM
   y la compatibilidad del engine de esquema necesario para migraciones.
   Empaquetar `.next/standalone`, `.next/static` y `public`.
3. En una DB SupoClip vacía, ejecutar primero `prisma migrate deploy` en el
   ensayo privado. La migración inicial crea `users`, `tasks` y `sources` con
   `CREATE TABLE` y el API también modela esas tablas. Después arrancar
   una instancia API, comprobar DB y Redis, iniciar worker y frontend.
   Ejecutar el flujo auth BetterAuth y una transcripción sintética por
   Speaches. Verificar por separado un render con ASS y el origen de la
   salida OpenCV. El entorno del frontend recibe valores públicos en build
   y secretos sólo en runtime mediante el operador.
