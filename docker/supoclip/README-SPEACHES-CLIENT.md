# Cliente remoto de transcripción para SupoClip

Estado: fuente y pruebas del cliente, sin integración del proveedor ni despliegue.
La [investigación inicial](README-REMOTE-TRANSCRIPTION.md) conserva las decisiones
de la fuente fijada. Esta unidad usa sólo la biblioteca estándar de Python 3.12;
no importa SupoClip completo, Torch, Whisper ni modelos y no instala dependencias.

## Interfaz y transporte

Con `docker/supoclip` en la ruta de importación del futuro proveedor:

```python
from speaches_client import transcribe_audio

result = transcribe_audio(
    audio_preparado,
    archivo_bearer_privado,
    language="es",  # opcional, ASCII lowercase de 2–3 letras
    cancel_event=evento_cancelacion,  # opcional, threading.Event
    total_seconds=300,
    idle_seconds=15,
)
```

Los dos archivos son rutas locales del operador. No hay argumento para URL,
host, puerto, modelo o cabeceras. El destino único es
`http://192.168.0.6:8000/v1/audio/transcriptions` y el modelo es
`Systran/faster-whisper-small`. El multipart fija `response_format=verbose_json`,
`timestamp_granularities[]=word` y `stream=false`, con `Content-Length` exacto.
No transforma un video en audio: acepta únicamente un archivo MP3/WAV preparado.
La extracción y validación multimedia corresponden a la integración posterior.

Cada llamada crea una conexión nueva al IPv4 literal mediante
[`http.client`](https://docs.python.org/3.12/library/http.client.html), sin DNS,
proxies del entorno, cookies, redirecciones o reintentos. El nombre enviado es
`audio.mp3` o `audio.wav`; la ruta local no viaja al puente. El Bearer se agrega
únicamente a esa petición. El HTTP de la LAN sigue el contrato del puente; este
cliente no añade una garantía de cifrado al enlace.

| Recurso | Límite aplicado |
| --- | --- |
| Audio | 24 MiB, archivo regular con un único enlace |
| Cuerpo multipart completo | 25 MiB, incluida toda la envoltura |
| Envío de audio | bloques de hasta 64 KiB, sin cargar el audio completo |
| Respuesta JSON | 2 MiB, `Content-Length` único y canónico |
| Cabeceras aceptadas de respuesta | 32 campos y 16 KiB después del parser acotado de stdlib |
| Tiempo total de red | hasta 300 s, reloj monotónico; progreso no renueva el plazo |
| Inactividad de socket | hasta 15 s por operación; presupuesto nunca superior al restante |
| Árbol JSON | profundidad 64 y 100.000 nodos |

La respuesta debe ser HTTP 200 con `application/json`, sin `Transfer-Encoding`
ni `Content-Encoding`. Los cuerpos de error no se leen ni se incorporan a
excepciones. El tamaño no acredita duración: un audio de más de 24 MiB falla
antes de abrir la conexión. **No se acredita soporte de 90 minutos**; requiere
una unidad separada de fragmentación y offsets.

## Archivos, cancelación y errores

El Bearer se lee únicamente en Linux desde archivo regular, sin enlaces
simbólicos en su ruta, con un único enlace, propietario igual al UID efectivo y
modo `0400` o `0600`. Sus bytes ASCII deben medir entre 32 y 256 y cumplir la
sintaxis de token; no se recortan saltos de línea. No se genera, busca en el
entorno ni escribe una clave. Los ancestros se abren con descriptores
`O_DIRECTORY|O_NOFOLLOW` y la hoja con `O_NOFOLLOW|O_NONBLOCK` antes de leerla.
Se comparan identidad, tamaño y tiempos del archivo abierto; un audio cambiado
durante el envío no devuelve éxito ni se reintenta. La ruta del secreto debe ser
un bind/archivo privado del operador; el cliente no cambia sus permisos.

El vigilante del plazo posee sólo el socket de su solicitud. Ante cancelación o
plazo agotado ejecuta
[`shutdown` y cierre](https://docs.python.org/3.12/library/socket.html#socket.socket.shutdown),
incluso cuando `HTTPConnection` ya soltó su referencia después de las cabeceras.
Al desenrollar se cierran respuesta, conexión, socket retenido y archivo, y se
retira el hilo vigilante antes de devolver control. No mata procesos ni toca
sockets de otras llamadas. Una lectura de disco bloqueada en el kernel queda
fuera de una garantía de interrupción desde Python; el supervisor del futuro
proveedor deberá conservar su presupuesto externo.

`SpeachesError.code` y su texto son códigos propios: `SPEACHES_AUTH_REJECTED`,
`SPEACHES_BUSY`, `SPEACHES_AUDIO_TOO_LARGE`, `SPEACHES_TIMEOUT`,
`SPEACHES_CANCELLED`, `SPEACHES_REDIRECT_REJECTED`, `SPEACHES_UPSTREAM_REJECTED`,
`SPEACHES_TRANSPORT_FAILED`, `SPEACHES_RESPONSE_TRUNCATED`,
`SPEACHES_INVALID_RESPONSE`, `SPEACHES_RESPONSE_TOO_LARGE`,
`SPEACHES_INPUT_REJECTED`, `SPEACHES_INPUT_CHANGED`,
`SPEACHES_SECRET_FILE_REJECTED`, `SPEACHES_SECRET_FILE_UNSUPPORTED` y
`SPEACHES_BUDGET_REJECTED`. No contienen credenciales, transcripción, respuesta
del proveedor ni rutas locales. El llamador debe registrar sólo el código; no
debe registrar sus argumentos ni variables locales de una traza. No se promete
borrado completo de secretos de la memoria de Python.

## Palabras, tiempos y compatibilidad AGPL

`speaches_contract.py` exige UTF-8 válido y JSON sin claves duplicadas, NaN,
infinito o caracteres de control C0/C1, incluso escapados. Conserva texto y
metadatos reales; valida tiempos finitos ordenados, duración y correspondencia
exacta entre palabras planas y las de cada segmento. Cada palabra debe traer
una probabilidad real finita entre 0 y 1. Si falta, falla: no inventa confianza,
hablantes, palabras o segmentos. El silencio verdadero admite texto vacío,
palabras vacías y segmentos vacíos, conservando la duración recibida.

Todos los tiempos válidos de palabra y segmento, incluidos los números enteros,
se convierten a `float` **en segundos**, sin modificar el objeto de entrada.
El conversor upstream recibe después esos floats y hace una sola conversión a
milisegundos. La regresión reproduce que el upstream trata `2` como 2 ms y
verifica que `2.0` termina como 2000 ms, con las mismas probabilidades.

La [fixture pura](../../tests/fixtures/supoclip-transcription-pure-f6.py) conserva
únicamente `_whisper_result_to_transcript_data` de
[SupoClip, commit f6cb7e2](https://github.com/FujiwaraChoki/supoclip/blob/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38/backend/src/media/transcription.py),
blob `f2585bae0efb80fc66338128afad9adf3efb8c30`. No importa la aplicación ni carga
modelos. La prueba inyecta un valor de prueba para el global de versión de
caché; la integración deberá usar el global real, sin inventar su versión.

- SHA256 del archivo original: `19c7dc73a165f51d7c2bd96fcbdeee5db7e1be1629caf471794fefa37189cae2`.
- SHA256 del AST de la función: `e89eacdfb96606e9be7d07ada32e51586dbb60e4813bee0872908dca52dd8a53`.
- [Licencia original AGPLv3](https://github.com/FujiwaraChoki/supoclip/blob/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38/LICENSE), copyright indicado 2025 Sami Hindi;
  SHA256 `2d57b88304ae76aa40722515c6f740ee8f6473e9d057c3e29b8ba08eeebd4e38`.

El texto íntegro de esa licencia se conserva en la fixture y se verifica por
SHA256. La atribución y licencia del extracto se mantienen separadas del código
propio. El AST se comprueba contra el original; las exclusiones locales de estilo
en la fixture conservan sus anotaciones y el global inyectado.

## Verificación y siguiente incremento

```powershell
python -m unittest discover -s tests -p test_supoclip_speaches.py -v
python -m ruff check docker/supoclip/speaches_client.py docker/supoclip/speaches_contract.py tests/test_supoclip_speaches.py tests/fixtures/supoclip-transcription-pure-f6.py
python -m ruff format --check docker/supoclip/speaches_client.py docker/supoclip/speaches_contract.py tests/test_supoclip_speaches.py tests/fixtures/supoclip-transcription-pure-f6.py
```

Las pruebas de transporte usan HTTP de stdlib sobre `socketpair` propio, sin
listener ni tráfico al puente GPU. Comprueban multipart, endpoint, cierre,
centinela ajeno preservado, progreso lento, cancelación, timeout, truncado,
duplicados, tamaños y códigos 301/401/413/429/500. En Windows se omiten
explícitamente dos pruebas de UID/modos/NoFollow: requieren Linux real y una de
ellas root; no se presentan como acreditadas allí.

La respuesta histórica del puente contiene 28 palabras y tres segmentos; su
validación offline sólo acredita compatibilidad con esos datos previos. Quedan
pendientes una llamada real desde `.85`, el parche de selección de proveedor
contra SHA de fuente fijada, la comprobación de caché real y la construcción/SCA
del componente CPU. Ningún fallo debe activar Whisper local, AssemblyAI, CPU o
el retorno del video original usado como fallback por la extracción upstream.
