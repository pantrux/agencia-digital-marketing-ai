# Proveedor Speaches exclusivo de SupoClip

Estado: overlay de fuente y pruebas controladas. No se construyó ni desplegó
SupoClip, no se llamó al puente GPU y no se ejecutó FFmpeg real en esta unidad.
El [cliente congelado](README-SPEACHES-CLIENT.md) conserva sus bytes; esta unidad
añade selección del proveedor y extracción CPU, sin importar la aplicación
completa durante las pruebas. No acredita videos de 90 minutos.

## Fuentes y alcance del parche

El manifiesto [source-patches.json](source-patches.json) conserva los originales
completos, los fragmentos antes/después, sus hashes y la licencia AGPLv3 íntegra
de [SupoClip f6cb7e2](https://github.com/FujiwaraChoki/supoclip/tree/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38).
Se verificaron por el conector GitHub y por el hash SHA1 del formato de blob,
calculado en Python; no se ejecutó ningún comando Git.

| Archivo upstream completo | Blob | SHA256 |
| --- | --- | --- |
| backend/src/config.py | `b6d2a53ddad6055d4a6d461981c96611526e8ec7` | `1f5fe8f0f78568429c8e58c2cadce251adb612538ddd0ee4e58388af9c1694ca` |
| backend/src/media/common.py | `9bc6c082cb6599ed91c6fca5a93874ac6a4333a4` | `50f14a1ccac20833c253e09d250fbc8675660800346b9ddfff6a4982f96f3713` |
| backend/src/media/transcription.py | `f2585bae0efb80fc66338128afad9adf3efb8c30` | `19c7dc73a165f51d7c2bd96fcbdeee5db7e1be1629caf471794fefa37189cae2` |

Se leyó además
[media/ffmpeg.py](https://github.com/FujiwaraChoki/supoclip/blob/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38/backend/src/media/ffmpeg.py),
blob `8f1a9b98cb803d921f98f886b5f33b950f3a7b68`, SHA256
`79c22744a852f767b8f996fe7a391a30f5b7aed50e577dc1d06267c8b8f068a5`.
Su wrapper captura salida sin límite, no permite cancelación durante la llamada
y registra argumentos/stderr al fallar. Por eso este proveedor usa supervisión
propia y conserva su criterio de selección de pista: idioma, descarte de pistas
secundarias, disposición original/default y posición. La prueba compara ambos
selectores con las funciones puras fijadas, sin importar módulos de SupoClip.

Los cambios son cerrados:

- `Config` exige `TRANSCRIPTION_PROVIDER=speaches`. Un selector ausente,
  desconocido, local o de pago falla; ya no se transforma en AssemblyAI.
- `SPEACHES_BEARER_TOKEN_FILE` es una ruta absoluta Linux del operador. No se
  introduce una credencial literal ni un endpoint configurable. La lectura del
  secreto conserva las guardas NoFollow/UID/modos del cliente.
- `common.py` fija `_whisper=None` y `_WHISPER_AVAILABLE=False` y elimina su
  intento de importar Whisper al cargar el módulo.
- `get_video_transcript` despacha exclusivamente al proveedor remoto. Conserva
  los tres argumentos posicionales existentes y añade `cancel_event` opcional
  por nombre. `speech_model` y `source_url` no seleccionan otro proveedor.
- El resultado válido vuelve al formateador y escritor de caché originales,
  con segundos `float`. El esquema real de la fuente fijada sigue siendo 2.

Esto acredita la ruta de transcripción modificada. La construcción posterior
debe cerrar y auditar el grafo CPU; no se debe instalar indiscriminadamente el
pyproject upstream, que declara Whisper/Torch y otras dependencias. No se ha
ejecutado la aplicación completa para acreditar sus demás rutas.

## Extracción y cancelación

El proveedor abre el video regular por descriptor con las guardas del cliente y
el máximo de subida ya declarado por `Config.max_video_upload_bytes`; no inventa
otra cuota. Pasa ese descriptor a los hijos mediante `pass_fds` y los comandos
leen `/proc/self/fd/N`, sin reabrir una ruta suministrada al proceso.

Cada petición posee un `TemporaryDirectory` nuevo, `0700` en Linux. No usa ni
sobrescribe el `.transcription.mp3` que pudiera existir junto al video.
FFprobe y FFmpeg son rutas fijas `/usr/bin/ffprobe` y `/usr/bin/ffmpeg`, sin shell,
con argv cerrado y entorno desde cero. La imagen futura deberá fijar y acreditar
esos binarios; este overlay no los instala ni ejecuta como parte del parche.

Los demuxers admitidos son `mov,matroska,webm,avi,mpegts` y los protocolos,
`file,pipe`. Un playlist no puede abrir URLs ni referencias arbitrarias de
archivo. Los demás contenedores fallan hasta una ampliación explícita. Se
selecciona una pista de audio existente: un probe inválido o sin pista falla,
sin asumir una pista por defecto ni fabricar una transcripción vacía.

FFmpeg produce MP3 mono de 16 kHz/64 kbit/s con `-n`; nunca retorna el video
original cuando falla. El límite kernel `RLIMIT_FSIZE=24 MiB+1` acota el archivo
generado y deshabilita core dumps. No se usa `-fs` para aceptar silenciosamente
una transcripción parcial. Un archivo mayor de 24 MiB se rechaza antes de HTTP,
también si FFmpeg devuelve error al alcanzar el límite.

El presupuesto monotónico de **300 s abarca probe, extracción y HTTP**; el probe
tiene como máximo 15 s y cada fase recibe el tiempo restante. El cliente conserva
inactividad máxima de 15 s. La limpieza puede añadir hasta 4 s para TERM/KILL.
No hay reintento o fallback ante timeout, cancelación, 401/429/413 o error de
extracción. Los audios superiores a 24 MiB siguen sin fragmentarse.

El supervisor es Linux/PIDFD y sólo controla estos hijos fijos. Primero crea un
intérprete Python aislado que espera autorización por stdin; únicamente después
de obtener su PIDFD envía el argv y el hijo ejecuta FFprobe/FFmpeg en el mismo
proceso. Si falta el PIDFD, cierra stdin y el gate sale por EOF sin ejecutar la
herramienta. La lectura de stdout/stderr está limitada a 64 KiB por stream y no
se registra su contenido. Cancelación/plazo envía TERM, espera hasta 2 s y luego
KILL mediante el PIDFD retenido, nunca por nombre, PID suelto o grupo ajeno.

Si el kernel no acredita la muerte del hijo, devuelve un error de limpieza,
retiene su identidad y bloquea nuevas herramientas hasta poder recolectarlo.
La eliminación automática sólo alcanza el directorio temporal de esa llamada;
no busca ni borra archivos por patrón. No es un ejecutor genérico de procesos
con descendientes arbitrarios. La imagen, los límites del contenedor y su
supervisor externo siguen siendo una unidad posterior; un proceso bloqueado en
el kernel no recibe una promesa de terminación instantánea.

## Generar un overlay nuevo

El controlador no ejecuta ni importa el código upstream. Lee sólo los tres
archivos fijados y los tres helpers propios, verifica antes/después y compila
su AST para comprobar sintaxis. No copia árboles completos, `.git`, secretos,
archivos del operador o dependencias.

```powershell
# Inspección por defecto: no crea el destino.
python docker/supoclip/apply_remote_transcription.py --source-root <fuente-fijada> --destination <overlay-nuevo>

# Sólo después de revisar y respaldar el plan y sus pins.
python docker/supoclip/apply_remote_transcription.py --source-root <fuente-fijada> --destination <overlay-nuevo> --apply --plan-sha256 <hash-del-plan>
```

`--source-root` contiene el árbol original del commit, obtenido por archivo/API
HTTPS sin Git. El destino debe estar ausente, fuera de la fuente y bajo un padre
propio; se rechazan colisiones, enlaces y reparse points. Un cambio de fuente,
helper, manifiesto, identidad de entrada o destino invalida el hash del plan.
El apply crea sólo una raíz nueva marcada y siete archivos:

1. `backend/src/config.py`, `backend/src/media/common.py` y
   `backend/src/media/transcription.py`, modificados por fragmentos únicos.
2. `backend/speaches_client.py`, `backend/speaches_contract.py` y
   `backend/speaches_provider.py`.
3. `LICENSE-SupoClip-AGPL-3.0.txt`.

Los archivos publicados son `0444` en POSIX; marcador y recibo son privados. El
recibo enlaza owner, dev/inode, plan y hashes. No se adopta un destino existente,
incluso si el nombre coincide con un intento anterior. Si la publicación falla,
el error devuelve la identidad de la raíz nueva y la conserva para revisión,
sin borrar recursos ni reanudar parcialmente de forma implícita.

Los helpers permanecen a nivel `backend/` para mantener los imports y bytes del
cliente congelado. La futura imagen debe usar esa carpeta fija como app-dir
(`--app-dir /app/backend` si se publica allí), con código de sólo lectura y sin
añadir rutas de importación elegibles por una petición. El overlay se aplicará
durante la preparación reproducible de la imagen; no es por sí solo una imagen
ejecutable ni modifica una instalación existente.

## Pruebas y aceptación pendiente

```powershell
python -m unittest discover -s tests -p test_supoclip_provider_patch.py -v
python -m ruff check docker/supoclip/speaches_provider.py docker/supoclip/apply_remote_transcription.py tests/test_supoclip_provider_patch.py
python -m ruff format --check docker/supoclip/speaches_provider.py docker/supoclip/apply_remote_transcription.py tests/test_supoclip_provider_patch.py
```

Las pruebas portables usan archivos temporales propios y dobles de extracción y
HTTP. Comprueban el centinela ajeno, ausencia de caché/fallback en fallo, tamaño,
selección de pista, probabilidades, formato original, versión de caché y
milisegundos, inputs alterados, colisiones y plan obsoleto. Las pruebas Linux
añaden enlaces y procesos **Python fixture**, con PIDFD, cancelación, límite de
salida, EOF de gate y centinela de proceso independiente. No ejecutan FFmpeg ni
realizan inferencia; sus omisiones en Windows quedan explícitas.

La selección de pista deriva de AGPLv3, copyright 2025 Sami Hindi. Se conserva
su atribución, el commit y la licencia original íntegra en el manifiesto y el
overlay. La aceptación posterior debe comprobar FFmpeg real sobre un video
público, HTTP desde `.85`, caché generada y el grafo/imagen CPU sin herramientas
o modelos locales innecesarios. El cierre de esta unidad no acredita ese
despliegue ni modifica las capacidades de fase declaradas.

Por separado, el cliente congelado ya tuvo aceptación HTTP desde `.85` al puente:
28 palabras y tres segmentos en 1,950 s, recibo privado
`phase4-supoclip-client-http-real-v2.json`. Esa prueba independiente no acredita
la extracción ni la aplicación del overlay de este proveedor.
