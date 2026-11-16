# Contrato del adaptador Speaches para SupoClip

Estado: investigación de fuente fijada, sin adaptador ni despliegue. Esta nota
pertenece a Fase 4 y no modifica el puente GPU ya aceptado.

## Fuentes observadas

SupoClip queda fijado al commit `f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38`.
Se consultaron mediante la conexión GitHub, sin comandos Git:

- [Transcripción](https://github.com/FujiwaraChoki/supoclip/blob/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38/backend/src/media/transcription.py), blob `f2585bae0efb80fc66338128afad9adf3efb8c30`.
- [Configuración](https://github.com/FujiwaraChoki/supoclip/blob/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38/backend/src/config.py).
- [Dependencias](https://github.com/FujiwaraChoki/supoclip/blob/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38/backend/pyproject.toml), blob `888dc9a22018998590d9b8b14928c4ff2f5ec35f`.
- [Licencia AGPL-3.0](https://github.com/FujiwaraChoki/supoclip/blob/f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38/LICENSE), blob `8ad51035b039039b885c897ed54298637d1a4288`.

La licencia del componente se conserva separada de la MIT del código propio.
No se ejecutan sus scripts, instaladores ni proveedores durante esta lectura.

## Diferencias que requiere la integración

El proveedor `whisper` carga un modelo local mediante `_get_whisper_model`.
Cambiar una URL en el entorno no lo convierte en cliente del puente. El
adaptador necesita una selección explícita propia y debe fallar si el puente
no está disponible, sin cambiar a AssemblyAI, Whisper CPU o el video original.

La extracción upstream genera MP3 mono de 16 kHz/64 kbit/s y, si falla FFmpeg,
devuelve el video original. Ese fallback no es admisible en el proveedor remoto.
El puente fijado acepta como máximo 25 MiB de cuerpo HTTP, incluido multipart;
un audio de 90 minutos a esa tasa puede superar el límite. La implementación
debe definir fragmentación con offsets comprobados o rechazar el tamaño de
forma explícita antes de enviar; esta investigación no acredita videos largos.

La solicitud remota queda limitada a
`http://192.168.0.6:8000/v1/audio/transcriptions`, modelo
`Systran/faster-whisper-small`, Bearer desde archivo privado, `verbose_json`,
`timestamp_granularities[]=word` y `stream=false`. Debe cerrar los archivos y la
conexión incluso ante cancelación y limitar tamaño de respuesta y tiempos.
No admite URLs elegidas por una petición, redirecciones ni proxies heredados.

## Tiempos y caché

El resultado real del puente conservado en
`.tools/evidence/whisper-http-real.json` contiene `text`, `language`, `duration`,
`words` y tres `segments` con palabras, tiempos en segundos y probabilidades.
Es evidencia histórica del puente, no prueba del adaptador todavía inexistente.

SupoClip formatea los segmentos en segundos, pero su caché guarda milisegundos.
Hay una diferencia concreta en `_whisper_result_to_transcript_data`: multiplica
por mil los tiempos de palabra sólo cuando el tipo es `float`; un entero como
`2` queda en dos milisegundos. El adaptador deberá normalizar todo tiempo válido
a `float` antes de llamar a esa función, o proporcionar una conversión propia
comprobada. No debe detectar unidades por el tipo de número.

Debe conservar texto y todas las palabras sin inventar probabilidades,
segmentos, hablantes o tiempos. Requiere números finitos, límites de duración,
orden temporal y consistencia entre palabras planas y las de cada segmento.
Los offsets de fragmentos, si se implementan, se suman una sola vez antes de
convertir a milisegundos. El esquema de caché vigente se conserva.

## Aceptación pendiente

Una primera unidad de hasta cinco archivos contendrá cliente, pruebas,
integración por parche de bytes fijados y documentación. Las regresiones deben
incluir tiempos enteros y decimales, Unicode, silencio, respuesta inválida,
palabras ausentes, orden temporal, 401/429/413, desconexión y timeout. Una prueba
de compatibilidad debe utilizar las funciones puras del blob upstream fijado,
sin importar ni instalar su aplicación completa.

Después corresponde una transcripción real desde `.85` con el audio público de
control y comprobación de palabras, caché en milisegundos y ausencia de modelo
local. La construcción CPU y su análisis de dependencias e imagen son unidades
posteriores: el pyproject original también incluye Whisper/Torch y OpenCV, y
no se debe instalar indiscriminadamente para probar este cliente.
