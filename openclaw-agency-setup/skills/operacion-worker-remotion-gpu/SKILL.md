---
name: operacion-worker-remotion-gpu
description: Opera mcp-remotion-gpu para obtener fotogramas de revisión y un máster audiovisual, con validación de plan, entradas NAS, artefactos, códec, audio y transparencia.
---

# Operación de Remotion GPU

Invoca `mcp-remotion-gpu` con el proyecto ya acreditado y el plan del encargo. El adaptador usa `192.168.0.6:8090` y el Bearer del puente desde su archivo protegido. Mantén la credencial fuera de los argumentos de la herramienta, los artefactos y el dictamen. Usa las herramientas anunciadas por el MCP. Las rutas y los campos siguientes pertenecen al worker.

1. Comprueba `GET /health` autenticado cuando el adaptador lo exponga. Continúa con `ready=true` y el codificador necesario disponible.
2. Pide `POST /stills` para validar encuadre, textos y marca Free. Usa `project_id`, `job_id`, `composition: AgencyVertical`, `plan`, `format`, `resolution` y `props`. `frames` admite hasta ocho índices enteros distintos dentro de la composición, desde cero. Por defecto se captura el segundo 2 o el último fotograma si la pieza es más corta.
3. Pide `POST /render` con los campos comunes y `props.audio_path` verificable. Las props admitidas son `title`, `subtitle`, `duration_seconds`, `background`, `accent`, `video_path` y `audio_path`. Usa sólo rutas NAS relativas dentro de las áreas admitidas del proyecto o referencias autorizadas de `_biblioteca_global/`. Usa un `job_id` que identifique inequívocamente la operación.
4. Coteja las rutas relativas, tamaños y SHA-256 que devuelve el worker. Las capturas quedan en `proyectos/<project_id>/05_revision/<job_id>/stills/` y el máster en `proyectos/<project_id>/06_renders/<job_id>/render/`. Pasa a QA el máster de esa versión, sus fotogramas y su medición final de audio.

| Plan | Formato y resolución |
| --- | --- |
| `Free` | `mp4`, `1080p`, marca de agua obligatoria |
| `Pro` | `mp4`, `1080p`, sin marca Free |
| `Ultra` | `mp4`, `1080p` o `4k`, sin marca Free |
| `Ilimitado` | `mp4` o `prores4444`, `1080p` o `4k` |

El MP4 vertical usa `h264_nvenc`, 30 fps, BT.709 y AAC estéreo a 48 kHz. ProRes 4444 alfa usa `prores_ks` en CPU y PCM estéreo de 24 bits a 48 kHz. Requiere evidencia de alfa decodificado. En ambos casos, la salida final debe medir `-14 ±1 LUFS` y pico verdadero `≤ -1 dBTP`.

El worker admite 600 segundos por pieza y hasta 20 minutos de procesamiento. Las tres operaciones comparten dos puestos. Un `429` informa saturación. Un `409` informa que ya existe el área del trabajo. Ante un timeout o un `409`, concilia primero la identidad y el estado del artefacto de esa operación. Si el backend da 503 o el artefacto incumple el contrato, conserva la evidencia y devuelve el fallo al flujo de revisión.

## Montaje de anuncio corto

Monta las escenas en el orden de la escaleta y comprueba su correspondencia con la voz original. Proyecta los subtítulos en el reloj de salida y valida que el MP4 final tenga la duración de la escaleta. Mantén la música por debajo de la voz y mide la mezcla terminada. Verifica gancho, subtítulos y CTA en la vista previa antes de generar el máster.

## Música de catálogo y montaje

Para un anuncio comercial, usa una pista instrumental publicada en el catálogo aprobado, con autor, licencia y huella. Reserva la voz como referencia de nivel. Comprueba fundidos y volumen de la música durante el cierre del MP4. Usa las marcas de Whisper para construir subtítulos por frases y conserva uniones como «mi marido» o «de interior». Para ajustes de oído, abre la copia exacta del MP4 en un entorno de reproducción o en una línea de tiempo de edición.

[Fase 5](../../../docs/specs/FASE-5-OPENCLAW-Y-MCP.md) · [Plan de implementación](../../../docs/PLAN-DE-IMPLEMENTACION.md) · [Arquitectura, sección 4.1](../../../docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md) · [API del renderer](../../../gpu-bridge-win11/render-worker/README.md#api-interna)
