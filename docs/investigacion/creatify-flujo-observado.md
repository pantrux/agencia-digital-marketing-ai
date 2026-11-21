# Creatify: observación del flujo de anuncio

Observación de la conversación existente del usuario en Creatify Ad Agent, 17 de noviembre de 2026. La inspección se realizó en la interfaz de Chrome, en modo lectura; no se envió ningún mensaje ni se inició una producción nueva.

## Comportamiento observado

- Una navegación lateral ofrece acceso a Ad Agent, aplicaciones, proyectos, marca y estudio; una segunda barra muestra conversaciones y permite iniciar una nueva.
- El encargo reúne un video adjunto, instrucciones libres y el enlace de un perfil social en el mismo mensaje.
- Durante la conversación el asistente formula preguntas cortas: acción final deseada, uso exclusivo del material o una toma generada y foco del anuncio. Las opciones aparecen como respuestas concretas; también existe una entrada libre.
- Aparecen indicadores de trabajo y, en la misma conversación, un recurso de audio reproducible, un resumen del resultado y una tarjeta para acceder al video generado. El resumen identifica la función del inicio, desarrollo y cierre, e indica si se utilizó generación de video.
- Después de entregar el resultado, propone solicitudes concretas de iteración, como crear una versión más corta o cambiar el foco. También ofrece editar el recurso con IA.
- En la sesión observada el sistema muestra créditos agotados: las acciones que consumen créditos no se probaron.

## Implicaciones para Studio

La conversación debe conservar el contexto del proyecto, hacer preguntas comprensibles y dejar que el cliente redacte una alternativa propia. El avance, las limitaciones y el video aprobado deben aparecer en el hilo sin simular resultados. Las sugerencias de cambios pueden ayudar a comenzar una revisión, pero la disponibilidad de una revisión debe corresponder al estado real del servicio.

## Alcance y límites

Esto acredita el comportamiento visible de una sola conversación, no el diseño interno, los modelos, el código ni la calidad de todas las producciones. Las duraciones mostradas por Creatify son datos de esa sesión, no una medición comparativa. No se capturaron credenciales ni tráfico privado.

La skill `reverse-engineer-anything` está instalada, pero el diagnóstico `rea doctor --client codex --json` indica que el servidor REA no está registrado en Codex. Esta observación se realizó con la interfaz de usuario y no constituye evidencia producida por REA.
