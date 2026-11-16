# Contrato de transcripción remota de MoneyPrinterTurbo

Estado: investigación de fuentes; no se ha parcheado, construido ni desplegado
esta aplicación. Versión 1.3.9, commit
`6992380c5e82f07f3e1fc46b90927b6f1c655407`, licencia MIT, copyright 2024 Harry.
Los archivos completos se obtuvieron mediante la API de GitHub de sólo lectura,
sin Git. Se recalculó el SHA del blob como datos, sin ejecutar el programa.
Los dos recibos privados `phase4-moneyprinter-transcription-source-receipt-v1.json`
y `phase4-moneyprinter-subtitle-callers-source-v1.json` conservan rutas, hashes,
tamaños y URLs de las fuentes originales sin modificarlas.

## Comportamiento observado en la fuente

[`app/services/subtitle.py`](https://github.com/harry0703/MoneyPrinterTurbo/blob/6992380c5e82f07f3e1fc46b90927b6f1c655407/app/services/subtitle.py)
importa `faster_whisper` al cargar el módulo, si está instalado. Su función
`create(audio_file, subtitle_file="", word_level=False, log_details=True)` carga
un modelo local mediante `_ensure_model_loaded()`, y puede descargarlo cuando
no existe el directorio esperado. El valor predeterminado es `large-v3` en CPU.
No hay un proveedor HTTP remoto ni una URL que sustituya esta operación.

`model.transcribe` devuelve segmentos/objetos de palabra con tiempos en segundos.
En modo palabra se conserva una entrada por palabra no vacía; en modo oración
se concatenan palabras hasta signos de puntuación finales del catálogo original.
No se separan los signos internos de cifras como 3.14. `create` entrega su
resultado escribiendo SRT; su retorno `None` también puede significar éxito,
por lo que el llamador comprueba el archivo y sus cues. Una prueba de retorno
distinto de `None` no acreditaría la integración.

[`app/services/task.py`](https://github.com/harry0703/MoneyPrinterTurbo/blob/6992380c5e82f07f3e1fc46b90927b6f1c655407/app/services/task.py)
selecciona `config.app["subtitle_provider"]`: `edge`, `whisper` o vacío. El
proveedor predeterminado es `edge`; no cambia automáticamente a Whisper cuando
Edge falla. Sin `sub_maker`, sólo permite actualmente `whisper`. Por tanto,
añadir sólo una función remota no permitiría subtitular un audio personalizado.

La tarea publica primero un SRT temporal junto al destino y sólo sustituye
`subtitle.srt` después de volver a parsearlo. El modo oración de Whisper llama
además a `subtitle.correct`, que alinea el texto con el guion. La integración
remota debe conservar los términos realmente reconocidos y sus tiempos; no
debe activar esa corrección del guion para la nueva rama Speaches.

El escritor upstream conserva el destino si no hay cues, pero permite reemplazar
un SRT existente una vez validado. Su área temporal y la ruta de publicación
pertenecen a la tarea; la integración debe comprobar su propiedad y no aceptar
rutas arbitrarias desde el proveedor remoto.

| Fuente | SHA256 completo |
| --- | --- |
| subtitle.py | `6567613fb3fe5f360861f9a2505e894980b8026014d2e5c07142abc6ddeb709b` |
| task.py | `daa859e689e41dd3098842e1d4161b07cb018d765395ec637a991dafa512387c` |
| subtitle_writer.py | `b5c845ac19ed7d284a757b0c211bfe2a588fdc2f6a5f7d23e8bb19144b87b82f` |
| const.py | `0d084175f7554ab65df5355b893d2da915b4af3fbbb9c05ab7f95418bf4a4ca2` |
| config.example.toml | `aa46dc6d77edce071977fe0d9802171e78089acd99994106d565a7335bd3faf6` |
| requirements.txt | `bd838cac4ec8f442327ea2db8af6927edc0753306a72d952cd5054b47973a15a` |
| LICENSE | `87dd19b6a0779e14a91c1b06423ef251df37e89666160c031130a26bae68c229` |

## Integración prevista

Reutilizar los bytes fijados del cliente stdlib ya aceptado en Linux y contra
la GPU desde `.85`, descrito en el
[contrato del cliente](../supoclip/README-SPEACHES-CLIENT.md). Su destino único es
`.6:8000/v1/audio/transcriptions`, modelo `Systran/faster-whisper-small`, Bearer
desde archivo privado y respuesta `verbose_json` con palabras. No crear otro
servidor, clave ni modelo. La futura imagen copiará explícitamente ambos módulos
propios por hash; no necesita importar SupoClip ni su fixture AGPL.

El adaptador debe aceptar audio MP3/WAV preparado, validar la respuesta completa
y convertir los datos a la representación SRT requerida, con una sola conversión
de segundos a milisegundos. Los errores de transporte, autenticación, tamaño o
formato deben fallar la tarea con códigos propios, sin descargar modelos,
reintentar, cambiar a CPU/Edge ni publicar subtítulos previos como resultado
nuevo. El silencio se distingue de un error y no fabrica cues.

La selección explícita `speaches` deberá funcionar sin `sub_maker`, impedir
cualquier importación/carga local de Whisper y rechazar proveedores desconocidos.
La configuración privada del archivo Bearer se respaldará en la bóveda antes
de usarla. El cliente admite hasta 24 MiB de audio; esta investigación no
acredita fragmentación ni soporte de 90 minutos.

La siguiente unidad implementará adaptador, parche de fuentes por SHA, pruebas
de comportamiento, manifiesto antes/después y documentación, hasta cinco
archivos. Probará palabras/oraciones, puntuación decimal, texto Unicode,
timestamps, silencio, publicación del SRT y preservación de un archivo ajeno
ante fallo. Las pruebas iniciales usarán el conversor extraído de la fuente y
dobles explícitos sin importar toda la aplicación ni llamar servicios de pago.
Después corresponderán Linux/FFmpeg reales, imagen CPU con dependencias fijadas,
SCA y aceptación desde la aplicación. Nada de ello se acredita con este documento.
