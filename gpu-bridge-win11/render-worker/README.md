# Renderizador nativo de Windows

Servicio propio `8090`, Node 24, Remotion 4.0.541 y FFmpeg 9.0.2. El contrato incorpora MP4 H.264 NVENC y MOV ProRes 4444 con alfa, en vertical 1080×1920 o 2160×3840 a 30 fps. MP4/1080p sigue siendo el valor predeterminado. Los formatos ya tienen pruebas operativas de tres segundos; la corrección de preparación de audio de este incremento está verificada en workspace/CPU y requiere publicación y nueva aceptación HTTP de uno y tres segundos. La elegibilidad Free License de Remotion está acreditada: el usuario confirmó que trabaja solo en la agencia, conforme a la [FAQ oficial](https://www.remotion.dev/docs/license/faq).

El límite común es 600 segundos por pieza y 20 minutos de procesamiento. Son límites operativos, no cuotas comerciales por plan. El consumidor interno autenticado controla cuenta, autorización de proyecto y cuota; el nombre del plan debe proceder de ese consumidor de confianza. Un solo proceso del worker comparte dos slots entre MP4, ProRes, capturas e inspección y rechaza el tercero con 429, sin crear cupos por formato o plan ni una cola ilimitada. El presupuesto de disco se calcula con píxeles y duración antes de renderizar: incluye PNG sin asumir compresión y hasta dos salidas ProRes, por lo que puede rechazar una pieza que exceda el espacio libre aunque cumpla los 600 segundos.

Para composiciones MP4 compatibles, Remotion genera la capa del título, subtítulo y marca del plan. FFmpeg compone esa capa sobre el crudo y codifica el máster con NVENC. Admite fuentes H.264 y HEVC SDR compatibles de hasta diez minutos. La reserva de disco incluye el crudo, las 13 imágenes de la capa y los intentos de codificación. La composición completa conserva el presupuesto para todos los fotogramas cuando el crudo queda fuera del perfil de capas.

| Plan | Resolución permitida | Formatos y presentación |
|---|---|---|
| Free | `1080p` | `mp4`, marca de agua obligatoria. |
| Pro | `1080p` | `mp4`, sin marca Free. |
| Ultra | `1080p`, `4k` | `mp4`, sin marca Free. |
| Ilimitado | `1080p`, `4k` | `mp4` o `prores4444`; ProRes activa composición transparente. |

## Configuración y arranque

El supervisor proporciona `GPU_RUNTIME_CONFIG_PATH`, JSON privado plano con strings `GPU_BRIDGE_BEARER_TOKEN`, `GPU_FFMPEG_PATH`, `GPU_FFPROBE_PATH`, `GPU_REMOTION_BROWSER_PATH`, `GPU_MODEL_CACHE_ROOT` y `GPU_SMB_DRIVE`. `GPU_RENDER_CACHE_ROOT` es opcional: selecciona una raíz local absoluta independiente para temporales y código compilado; si falta conserva la raíz `GPU_MODEL_CACHE_ROOT`. Los valores del JSON privado prevalecen sobre variables de entorno equivalentes. La ubicación prevista de render es `D:\Proyectos\Agencia Digital de Marketing AI\.tools\gpu-render-cache`; el responsable debe respaldarla y crear su ACL privada antes de inyectarla. Los modelos ML permanecen en su raíz de modelos. No se admiten rutas UNC, relativas o con escapes para la caché de render. `P:` se normaliza como `P:\`; su mapeo debe ser exactamente `\\192.168.0.8\producciones`, estado correcto y `RequirePrivacy=true`. La instalación y la configuración privada las gestiona el responsable del puente; este directorio no modifica la bóveda ni credenciales SMB.

El primer trabajo compila la composición estática una vez por proceso con `gitSource:null`; dos solicitudes concurrentes comparten esa misma promesa. El código queda en `render-worker/compiled-composition/process-*/bundle/`, fuera de los directorios `job-*` que se eliminan después de cada trabajo. Un reinicio compila de nuevo en un directorio exclusivo y conserva las carpetas anteriores; no adopta código compilado por otro proceso. Si compilar falla, el error permanece visible y no se oculta con recompilaciones automáticas.

Las props, duración y resolución siguen siendo dinámicas. Cada vídeo validado se copia una sola vez a `public/media/<UUID>/source.<ext>` del bundle; su nombre de origen no llega al navegador. El espacio libre se comprueba antes de copiar. El UUID y su marcador aíslan dos trabajos simultáneos. Al terminar, incluso con error de captura, se cierran el navegador y sus lecturas y se retiran sólo el vídeo y marcador de ese UUID mediante borrado de archivos y directorio vacío; un archivo extra o marcador ajeno bloquea la limpieza y se conserva para diagnóstico. Cada trabajo abre y cierra su propio navegador. Un cierre forzado del proceso puede dejar su caché privada pendiente de limpieza controlada.

El navegador fijado se extrae del ZIP verificado en `artifacts/chromium-headless-shell-win-x64-158.0.8127.2.zip`, relativo a este directorio. La ruta instalada acordada es `C:\GPU-Bridge\cache\remotion\chromium-headless-shell-win-x64-158.0.8127.2\chrome-headless-shell-win64\chrome-headless-shell.exe`. Su SHA256 de archivo ZIP, las integridades npm y los hashes FFmpeg/ffprobe están en [versions.json](versions.json). El lanzador compara ambos hashes de FFmpeg antes de ejecutarlos; un cambio de versión exige revisar el manifiesto. El SDK recibe una ruta explícita de navegador y este debe existir; no se solicita una descarga automática al arrancar.

Instalación propia desde el lock fijado, sin scripts de paquetes ni herramientas globales:

```powershell
npm ci --ignore-scripts
npm audit
npm run typecheck
npm test
```

Comando del supervisor, con directorio de trabajo `C:\GPU-Bridge\render-worker`:

```powershell
& $env:GPU_NODE_PATH --experimental-strip-types 'C:\GPU-Bridge\render-worker\src\start.ts'
```

No ejecutar el comando de arranque antes de instalar navegador, módulos y configuración privada. Escucha explícitamente `0.0.0.0:8090`, manteniendo Bearer y filtro por dirección real del socket en todas las rutas. Admite solo `.85`, `.7`, `.8` y loopback; ignora cabeceras de proxy. No se ejecutan órdenes proporcionadas por clientes. Importar módulos no abre puertos ni inicia procesos.

La salud `service=remotion`, `status=ok`, `ready=true`, `nvenc_available=true` se activa después de comparar hashes, verificar SMB, ejecutar tres fotogramas 320×240 con `h264_nvenc`, probar `prores_ks` con alfa y capturar una composición local con Chromium. El presupuesto común de inicialización es 120 segundos y comparte una señal de cancelación entre sondas y captura, dentro de la gracia de 180 segundos del supervisor. La primera captura prepara la caché compilada que reutilizan los trabajos posteriores. `prores_available` se acredita por la ejecución de CPU, independientemente de NVENC. Antes de ello responde 503. Esta comprobación inicial no sustituye el render final medido en cada formato/resolución ni la aceptación LAN.

## API interna

Todas las rutas exigen `Authorization: Bearer` con comparación de digest de longitud fija. El cuerpo JSON tiene un máximo64KiB. No admite campos desconocidos, código, URLs, rutas absolutas, `..`, unidades ajenas, dispositivos de Windows, flujos alternativos ni enlaces. Las fuentes pertenecen a `proyectos/<project_id>/{01_raw,03_assets,04_edicion,06_renders}/` o `_biblioteca_global/`. El proyecto ya debe existir. No se sobrescribe el área de salida de un trabajo existente.

| Ruta | Método | Resultado |
|---|---|---|
| `/health`, `/healthz` | GET | Salud real resumida;200 disponible o503 degradado. |
| `/stills` | POST | PNG a la resolución solicitada; modo alfa cuando `format=prores4444`. Por defecto segundo 2 o último fotograma si dura menos; hasta ocho fotogramas. |
| `/render` | POST | MP4 NVENC o MOV ProRes 4444 y medición final; exige pista de audio. |
| `/inspect` | POST | ffprobe saneado, ebur128 y PNG/tira SVG con regla normalizada `Y=0..1920`, también para 4K. Devuelve dimensiones originales, escala y desplazamiento. Hasta doce muestras. |

Ejemplo del cuerpo `/render` (sin token ni medios privados):

```json
{
  "project_id": "sansevieras",
  "job_id": "evaluacion-001",
  "composition": "AgencyVertical",
  "plan": "Free",
  "format": "mp4",
  "resolution": "1080p",
  "props": {
    "title": "Prueba sintética",
    "subtitle": "Puente GPU",
    "duration_seconds": 3,
    "background": "#102032",
    "accent": "#8fe3c5",
    "audio_path": "proyectos/sansevieras/03_assets/evaluacion.wav"
  }
}
```

`resolution` admite exactamente `1080p` o `4k`; su ausencia conserva 1080p. `format` admite `mp4` o `prores4444`; su ausencia conserva MP4. Dimensiones y transparencia se derivan en el servidor y no son campos que el cliente pueda imponer. Para el MOV alfa, enviar `plan=Ilimitado`, `format=prores4444` y la resolución elegida. `/stills` usa el mismo contrato de plan/formato para producir PNG transparentes y añade `frames`, opcional, con índices enteros dentro de la duración.

`video_path` es opcional. La composición local usa texto React escapado y animación por fotograma; no contiene código remoto. Sus dimensiones se calculan dinámicamente con Remotion y el contenido mantiene escala proporcional en 4K. En modo alfa se omiten fondo y gradiente; `<OffthreadVideo transparent>` conserva el canal de una fuente que lo tenga. Un vídeo fuente opaco puede cubrir el lienzo y hacer fallar la comprobación de transparencia. Las fuentes de vídeo se copian a un directorio público temporal exclusivo que Remotion expone por loopback. No se inyecta el token en el navegador. `/inspect` recibe `project_id`, `job_id`, `input_path` y `samples`, opcional por defecto seis.

Las salidas contienen rutas NAS relativas, SHA256 y bytes. El render final queda bajo `proyectos/<proyecto>/06_renders/<trabajo>/render/`. Las capturas y la inspección quedan bajo `05_revision/<trabajo>/{stills,inspect}/`. La regla QA mantiene el lienzo normalizado de 1080×1920: un original 2160×3840 tiene `source_scale=0.5` y `original_height=3840`, conservando `y_max=1920`. La respuesta incluye `original_width`, `original_height`, `source_scale`, `source_x` y `source_y` para transformar la fuente cuando se adapta con bandas.

El puente publica el recibo y los archivos juntos. Si el registro muestra `LIMPIEZA_PUBLICACION_FALLIDA`, el responsable puede revisar los directorios temporales `.render-*` y `.stills-*` junto al destino de ese trabajo. Para retirar un temporal huérfano, verificar que el puente esté detenido y que el directorio corresponda a una publicación fallida. El destino `render` o `stills` con su recibo debe conservarse para los reintentos.

Remotion produce PNG para capturas, ProRes y las secuencias que requieren ese formato. El MP4 de 1080p con vídeo de origen y una duración de al menos 30 s usa JPEG intermedio de calidad 95. FFmpeg codifica MP4 con `h264_nvenc`, sin alternativa CPU: H.264 High, `yuv420p`, AAC estéreo 48 kHz/256 kbps, BT.709 y rango limitado. 1080p usa nivel 4.2 y 6/8/12 Mbps de tasa/techo/buffer; 4K usa nivel 5.1 y 24/32/48 Mbps. ProRes usa explícitamente `prores_ks` en CPU, perfil `4444`, entrada `yuva444p10le`, alfa de 16 bits, etiqueta `ap4h`, PCM de 24 bits estéreo a 48 kHz y contenedor MOV. Su respuesta declara `hardware_acceleration=false`; no se atribuye a NVENC. Se aceptan los formatos decodificados `yuva444p10le`/`yuva444p12le` del perfil 4444, conservando BT.709.

Los nuevos recibos de `/render` incluyen `stage_ms` con cuatro tiempos en milisegundos. `preparacion_audio` abarca la reserva de espacio, la copia y la medición del audio. `fotogramas` incluye preparar la escena, generar las imágenes y limpiar los recursos de Remotion. `codificacion_y_control` cubre codificar y verificar el máster. `publicacion_nas` incluye volver a cotejar los insumos y escribir el artefacto en SMB. `elapsed_ms` también cuenta el trabajo previo a preparar el audio. Un reintento recupera el recibo original y sus tiempos, si los contiene.

La ruta por capas abarca MP4 de hasta 600 segundos. El mínimo es de 30 segundos en Free y de 13 fotogramas en los otros planes. El crudo debe tener BT.709 limitado, YUV 4:2:0 de 8 bits, dimensiones pares de hasta 3840 píxeles, lado corto de al menos 540 píxeles, píxeles cuadrados y relación 9:16 o 16:9. En HEVC se admite que falte la etiqueta de relación de píxel. El H.264 progresivo con 30 fps exactos conserva sus tiempos de presentación. Para HEVC y para H.264 de más de 60 segundos se admite normalización de fuentes nominalmente a 30 fps, con duraciones a no más de un segundo del pedido y un origen temporal a cero. La matriz de orientación, si existe, debe ser una rotación simple. Se verifican los tiempos del crudo antes de fijar la cadencia a 30 fps y se valida la secuencia del máster. FFmpeg puede prolongar el último fotograma del crudo hasta un segundo para completar la duración.

Remotion genera los 13 PNG de la transición del título, con degradado y marca del plan. FFmpeg recorta el vídeo al centro en YUV BT.709 y compone los PNG convertidos a YUVA, tanto en 1080p como en 4K. El último PNG se conserva hasta el final. Un fallo de sonoridad permite corregir el audio sin recodificar el MP4. El recibo registra `capas_remotion_nvenc` en `render_pipeline`. Los demás vídeos se procesan mediante `fotogramas_remotion`.

Después de convertir RGB a YUV, ambos comandos fijan también `setparams` de primarias, transferencia, matriz y rango en cada fotograma. Los PNG de Chromium pueden llegar sin primarias/transferencia; una reproducción real de FFmpeg 9/NVENC demostró que las opciones del códec por sí solas no las conservaban. El archivo sigue rechazándose si ffprobe no acredita las tres etiquetas BT.709. `VIDEO_FUERA_DE_CONTRATO` identifica los nombres de los campos incumplidos sin devolver rutas, argumentos ni contenido de los medios.

Ambos formatos preparan estéreo a 48 kHz antes de medir y antes de normalizar. Las dos pasadas comparten exactamente preparación, relleno y recorte a la duración solicitada; después se mide otra vez la salida final AAC o PCM. Así el presupuesto de pico refleja los canales finales y no limita el mono antes de una conversión posterior a estéreo. Sólo se publican cuando acreditan `-14 ±1 LUFS`, pico verdadero `<=-1 dBTP`, dimensiones, 30 fps, duración, códec y color correctos. Un segundo intento acotado aporta margen al limitador; si tampoco cumple, se rechaza la salida. Un archivo silencioso no acredita sonoridad. En ProRes se decodifica además el plano alfa de un fotograma en la mitad de la pieza; en PNG se comprueba cada captura pedida. La muestra se reduce a 64×64 y exige al menos un píxel de alfa cero; una etiqueta YUVA o un plano uniforme a 254 por redondeo no bastan. La respuesta informa mínimo, máximo, cantidad de píxeles transparentes y el instante del muestreo de vídeo. Es una muestra espacial/temporal, no una verificación exhaustiva de cada fotograma; zonas transparentes diminutas pueden perderse al reducir y provocar un rechazo conservador. Si no se acredita alfa, no se publica el archivo.

La caché reconstruible tiene marcador de propiedad y guardas antes de borrado; nunca se borra recursivamente en NAS.

## Diagnóstico de fallos

En Windows, `C:\GPU-Bridge\logs\render.log` registra los fallos de `/render` con hora UTC, fase, duración, código y una referencia corta derivada del proyecto y el trabajo. No registra títulos ni rutas de medios. Las fases distinguen la entrada o repetición de un recibo, audio, preparación de video, fotogramas de Remotion o HyperFrames, codificación, control y publicación en Synology.

Mientras Remotion genera una secuencia, cada fotograma nuevo renueva el intervalo de 90 segundos. Si el contador deja de avanzar, el puente solicita la cancelación al SDK y devuelve `FOTOGRAMAS_SIN_AVANCE` después de cerrar el navegador. El límite global de 20 minutos se conserva y devuelve `PLAZO_TRABAJO_AGOTADO` al agotarse. Los dos casos liberan la plaza tras cerrar los recursos del trabajo. [Progreso de fotogramas](https://www.remotion.dev/docs/renderer/render-frames#onframeupdate) · [Cancelación](https://www.remotion.dev/docs/renderer/make-cancel-signal)

## Evidencia y límites actuales

Las dependencias, el navegador y sus pines no cambian en este incremento. Las pruebas automáticas usan archivos temporales locales y procesos controlados; validan permisos por plan, metadatos, selección de codificador, rechazo de audio/alfa fuera de contrato, coordenadas QA, slots compartidos y compilación única concurrente con limpieza aislada de assets. Las pruebas unitarias no acreditan GPU ni NAS. La publicación y aceptación operativa coordinadas posteriormente sí registraron MP4 Free de tres segundos a -14,15 LUFS/-4,49 dBTP, ProRes de tres segundos en 1080p y 4K con alfa/audio válidos y concurrencia real `[200,200,429]`, con máximo de dos sesiones NVIDIA en 35 muestras. Esos resultados se conservan por separado; no sustituyen la nueva prueba HTTP después de este cambio de audio.

La reproducción aislada del rechazo de color, con PNG público y tono sintético en `.tools`, produjo un MP4 NVENC 1080×1920/30 fps de tres segundos con primarias y transferencia ausentes. Tras fijar las propiedades de fotograma, el mismo contrato aceptó la salida con las tres etiquetas BT.709 y audio de -14,05 LUFS / -13,13 dBTP. Su integración posterior permitió la aceptación operativa descrita arriba. La preparación de audio de un segundo es el cambio actual de workspace pendiente de publicación y nueva aceptación HTTP.

El fragmento de un segundo de la voz sintética mono/22050 Hz reprodujo un rechazo correcto de la salida anterior: -15,68 LUFS. Con las funciones actualizadas, los controles CPU de uno y tres segundos, AAC y PCM y ambos márgenes de pico cumplen el mismo criterio de aceptación: un segundo alcanza -14,00 LUFS/-2,85 dBTP en AAC y -14,00/-2,84 en PCM; tres segundos también cumplen. No se relaja `audioAccepted` ni se impone una duración mínima comercial nueva. El resultado CPU se registra en `.tools/evidence/render-short-audio-updated-code.json`; el responsable publicará el bundle y verificará de nuevo HTTP sin alterar el fixture fuente.

Referencias primarias revisadas el 2026-11-05: [bundle de Remotion](https://www.remotion.dev/docs/terminology/bundle), [directorio público](https://www.remotion.dev/docs/options/public-dir), [metadata dinámica](https://www.remotion.dev/docs/calculate-metadata), [PNG y ProRes transparentes](https://www.remotion.dev/docs/transparent-videos), [renderFrames](https://www.remotion.dev/docs/renderer/render-frames), [renderStill](https://www.remotion.dev/docs/renderer/render-still), [opciones ProRes de FFmpeg](https://ffmpeg.org/ffmpeg-codecs.html#ProRes), [alphaextract](https://ffmpeg.org/ffmpeg-filters.html#alphaextract) y [loudnorm](https://ffmpeg.org/ffmpeg-filters.html#loudnorm).
