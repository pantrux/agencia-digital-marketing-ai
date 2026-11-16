# Proveedor remoto de subtítulos de MoneyPrinterTurbo

Esta unidad prepara un overlay de fuentes para MoneyPrinterTurbo 1.3.9, commit
`6992380c5e82f07f3e1fc46b90927b6f1c655407`. No ejecuta la aplicación, construye
una imagen, instala dependencias ni llama a la GPU. El comportamiento
predeterminado del preparador es mostrar un plan sin escribir el overlay.

## Comportamiento preparado

`app.services.subtitle.create(audio_file, subtitle_file="", word_level=False,
log_details=True)` conserva su firma. Exige seleccionar `speaches` explícitamente
y usa el cliente MIT propio existente. Desaparecen el import de `faster_whisper`,
la inicialización del modelo, el directorio `models/whisper-*` y la ruta que podía
descargar un modelo local. No se importa Torch ni se añade otro modelo.

El adaptador valida `verbose_json` con los mismos módulos compartidos, exige
probabilidades reales de las palabras y conserva los tiempos normalizados en
segundos. En modo palabra entrega cada token reconocido; en modo oración concatena
los tokens originales y agrupa sólo por delimitadores finales. Conserva la
puntuación, incluidos decimales como `3.14`; no reemplaza palabras por el guion ni
llama a `subtitle.correct`. La única cuantización a milisegundos ocurre al
serializar SRT. No añade tiempo para alargar un cue de duración cero.

Hay tres resultados distintos:

- Transcripción: escribe un SRT completo mediante el escritor upstream y retorna
  `None`, compatible con los llamadores que comprueban el archivo.
- Silencio verificado: retorna el sentinel `silence`, no publica un SRT y no
  reutiliza el anterior. La tarea completa puede continuar sin subtítulos; una
  tarea cuyo objetivo exclusivo sea subtítulos conserva su fallo upstream por no
  tener un artefacto que entregar.
- Error: lanza `ProviderError` con un código cerrado. El llamador principal lo
  captura y ejecuta `_mark_task_failed` en la fase de subtítulos. No continúa con
  un vídeo completo como si una avería fuera silencio.

`generate_subtitle` permite `speaches` cuando no existe `sub_maker`, por lo que
puede transcribir audio preparado que no procede de TTS. `edge` mantiene su uso
explícito de tiempos de voz generada y no cambia de proveedor si esos tiempos
faltan. Valores vacíos, desconocidos o `whisper` se rechazan; para deshabilitar
subtítulos se usa su checkbox existente.

El llamador `transcribe_audio_bytes` también usa el proveedor remoto, crea su WAV
en un directorio temporal propio, limita los bytes antes de escribir y retira
sus temporales. No importa la aplicación completa desde las pruebas.

## Interfaz y configuración

El commit no tenía selector de proveedor. El parche añade uno al panel existente
`_render_subtitle_settings` de `webui/Main.py`, con dos etiquetas:

| Etiqueta | Identificador interno |
| --- | --- |
| Transcribir el audio | `speaches` |
| Usar tiempos de la voz generada | `edge` |

Usa `stable_selectbox` y `_set_runtime_config` del mismo commit. Rechaza una
configuración desconocida antes de mostrar una selección por defecto; no emplea
el helper upstream que reemplaza valores desconocidos silenciosamente.
Los cambios de configuración respetan el mecanismo existente que difiere su
aplicación mientras una tarea conserva el bloqueo de configuración.

El ejemplo de configuración propone `app.subtitle_provider = "speaches"` y un
`app.subtitle_speaches_token_file` vacío. El operador debe respaldar y preparar
ese archivo privado antes de usarlo. La UI no muestra tokens, rutas, endpoints ni
la arquitectura de la red. El apartado histórico `[whisper]` del ejemplo se
conserva como dato upstream sin uso en esta variante; no habilita un modelo local.

## Transporte reutilizado

Los dos módulos propios MIT se referencian por ruta y SHA en
[speaches-provider-artifacts.json](speaches-provider-artifacts.json). No se
modifican las fuentes congeladas de SupoClip ni se copia su fixture AGPL.
El plan propone copiarlos sin cambios como `speaches_client.py` y
`speaches_contract.py` en la raíz de la aplicación; el nuevo adaptador reside en
`app/services/agency_speaches.py`. La imagen futura deberá conservar esa disposición.

El contrato ya existente fija `192.168.0.6:8000/v1/audio/transcriptions`, modelo
`Systran/faster-whisper-small`, Bearer desde archivo privado y respuesta
`verbose_json` con palabras. El cliente no usa proxies/cookies, redirecciones,
reintentos ni fallback. Los límites son **24 MiB de MP3/WAV preparado**, multipart
de 25 MiB, JSON de 2 MiB, 300 s totales y 15 s sin actividad. La cancelación
cierra únicamente su socket y archivo. Esta unidad no convierte otros formatos,
fragmenta audio ni acredita 90 minutos. Tamaños mayores fallan explícitamente.

El servicio de transporte compartido tiene sus pruebas y aceptación anteriores,
descritas en [README-SPEACHES-CLIENT.md](../supoclip/README-SPEACHES-CLIENT.md).
Esas evidencias no acreditan todavía este overlay dentro de MoneyPrinterTurbo.

## Plan y publicación local posterior

El preparador contrasta SHA256 y blob SHA de las ocho fuentes completas,
incluida la UI, y los pins de ambos módulos MIT. Sólo transforma las tres fuentes
de aplicación y el ejemplo de configuración. Añade adaptador, módulos compartidos
y copia intacta de la licencia MIT de Harry (2024). No modifica originales,
`requirements.txt`, escritor SRT ni catálogo upstream de puntuaciones.

La raíz propuesta es `.tools/moneyprinter-speaches-overlay-v1`, propiedad lógica
`agencia-digital-marketing-ai:moneyprinter-speaches-overlay:v1`. Root respalda
raíz/dev/inode, marcador, recibo, manifiesto, fuentes y hash del plan antes de
publicar. El preparador sólo verifica esa identidad y ACL; no crea ni adopta la
raíz. El helper de acceso se ejecuta compilando sus bytes previamente verificados,
sin consultar un loader ni un `.pyc`.

```powershell
# Plan local, sin bóveda, HTTP, SDK, aplicación ni publicación.
python docker/moneyprinter/apply_speaches_provider.py

# Sólo Root después de revisión y respaldo de los hashes correspondientes.
python docker/moneyprinter/apply_speaches_provider.py --apply --manifest-sha256 HASH_MANIFIESTO --plan-sha256 HASH_PLAN
```

La publicación escribe un intent exclusivo antes de cualquier salida y sólo
admite el árbol esperado bajo `overlay/`. Usa archivos nuevos, hashes exactos y
recibo posterior; un reintento del mismo plan es idempotente. Rechaza enlaces,
archivos ajenos, cambios y planes obsoletos. No sobrescribe ni borra por patrón.
Un parcial propio queda retenido para diagnóstico y sólo se continúa si el
intent y todos los archivos presentes coinciden con el plan. El recibo deberá
respaldarse antes de utilizar las salidas.

La escritura del SRT durante una tarea sigue el staging existente y vuelve a
parsear sus cues antes de sustituir el destino. Las pruebas acreditan preservación
del destino ante fallo y silencio; no acreditan aún el ownership de directorios
de tareas de una instalación real ni ofrecen un publicador genérico de rutas.

## Pruebas y límites de aceptación

```powershell
python -m unittest discover -s tests -p test_moneyprinter_speaches.py -v
python -m ruff check docker/moneyprinter tests/test_moneyprinter_speaches.py
python -m ruff format --check docker/moneyprinter tests/test_moneyprinter_speaches.py
```

Se ejecutan funciones puras y AST de las funciones parcheadas con dobles
explícitos y fixtures MIT propios: palabras/oraciones, Unicode, tiempos,
decimales, probabilidad ausente, silencio, error sanitizado, marcado de fallo de
tarea, selección UI, ausencia del cargador local, temporales y publicación local.
No se importa Main, Streamlit, la aplicación, Whisper ni modelos en estas pruebas.

Quedan pendientes revisión de Root, publicación del overlay, Linux nativo y la
aceptación HTTP de la aplicación usando el audio sintético conocido de 28 palabras
y tres segmentos. La imagen CPU y su clausura de dependencias/SCA son otra unidad:
el requirements upstream todavía declara `faster-whisper` y no debe instalarse
como perfil CPU aprobado. Tampoco se acredita FFmpeg, extracción, UI real,
servicios, imagen final ni despliegue por esta preparación de fuentes.
