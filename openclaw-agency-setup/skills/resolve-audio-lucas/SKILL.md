---
name: resolve-audio-lucas
description: Edita y mezcla entregables de Studio en DaVinci Resolve Studio y Fairlight mediante el MCP davinci-resolve. Úsala para revisar una línea de tiempo, ajustar voz y música o preparar un máster para Elena.
---

# Fairlight para Lucas

Basada en el [flujo Fairlight de davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp/blob/e4bcd1d9eeefe59ebda9acc9dd17059e1358af02/.agents/skills/resolve-audio/SKILL.md).

## Preparar la revisión

1. Usa `resolve_control(get_version)` y `timeline(get_current)`. Confirma que el nombre del proyecto y la línea de tiempo corresponden al traspaso de Studio. Si Resolve está cerrado o hay otro proyecto activo, comunica el bloqueo y detén la edición.
2. Duplica la línea de tiempo con un nombre de revisión. Vuelve a leer su ID antes de editar. Usa las acciones de inspección y simulación del MCP antes de cambiar cortes, pistas o niveles. Conserva el material fuente y la entrega anterior.
3. Para un anuncio, comprueba en vista previa que el producto se vea con claridad al abrir, que los subtítulos se agrupen por frases y que ningún rótulo o subtítulo cubra el detalle que se está mostrando. Revisa también el último cuadro del CTA y la duración pactada.

## Mezclar y entregar

1. Identifica las pistas de voz y música con `timeline(probe_audio_track)`. Escucha la mezcla completa y los cambios de toma en Fairlight. Usa una pista de catálogo con licencia registrada. Comprueba la inteligibilidad en pausas y frases débiles, y ajusta voz y música por separado.
2. Comprueba los parámetros que admite `timeline(probe_audio_item)` antes de usar `timeline(safe_set_audio_properties)`. Verifica cada cambio con una nueva lectura. Si hacen falta automatización de volumen o plugins de Fairlight, usa la interfaz de Resolve y comprueba el resultado audible.
3. Renderiza la revisión y comprueba el archivo terminado. Para el perfil web de Studio, verifica sonoridad entre −15 y −13 LUFS y pico verdadero de −1 dBTP o inferior. Si falta una medición o la voz sigue perdiéndose, reporta la incidencia antes de pedir aprobación.
4. Pasa a Elena el archivo renderizado y su SHA-256, el alcance de la edición, los tiempos exactos de las tomas y subtítulos corregidos, la licencia musical y el informe de audio. Pídele revisar el archivo completo y registrar sus observaciones con marcas de tiempo.
