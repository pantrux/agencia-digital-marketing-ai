---
name: rubrica-qa-audiovisual
description: Audita un máster audiovisual con fotogramas de revisión, medición ebur128 y copy. Emite qa_verdict.json con puntaje sobre 100, decisión y correcciones por fotograma o tiempo.
---

# Rúbrica de QA audiovisual

Revisa la versión exacta del máster identificada por el trabajo y su SHA-256. Contrasta el brief, la escaleta y el plan con los fotogramas de revisión, las mediciones del archivo final y el copy. Un fotograma de ensayo describe sólo el instante observado. Si falta evidencia de una regla dura, deja el dictamen pendiente de esa verificación.

## Criterios de revisión

| Aspecto | Evidencia a contrastar |
| --- | --- |
| Imagen y montaje | Fotogramas QA con índice o tiempo. Encuadre vertical, rostro y barbilla según zonas seguras del proyecto, legibilidad, ritmo, cortes y ocultación de logos que el brief exige. Usa escala y desplazamiento de la tira graduada al comparar con el crudo. |
| Audio | Sonoridad integrada de `-14 ±1 LUFS` y pico verdadero `≤ -1 dBTP`, medidos en el render final con ebur128. Revisa voz inteligible y sincronizada con la imagen. |
| Copy y subtítulos | Gancho y texto ajustados al brief y a la voz. Ortografía, sincronización y tarjetas que aportan valor sin repetir la locución. |
| Entrega técnica | Identidad y SHA-256 del máster, resolución y formato autorizados, 30 fps, códec/color y marca de agua obligatoria en `Free`. Para ProRes 4444 alfa, coteja además la muestra de alfa decodificado. |

El dictamen usa un total de **0 a 100**. Justifica el total con los hallazgos observados y aplica la ponderación aprobada del encargo cuando exista. Aprueba con **85 o más** y todas las reglas duras satisfechas. Trátalas como condiciones de entrega: integridad e identidad del máster, conformidad técnica con el plan, audio medido dentro de tolerancia y restricciones obligatorias del brief. Una infracción acreditada implica rechazo con cualquier puntaje.

## Dictamen

Entrega `qa_verdict.json` en JSON válido, usando el esquema aprobado si está disponible. Incluye la referencia a la versión revisada, puntaje, decisión, reglas duras incumplidas o pendientes y observaciones trazables. Cada corrección debe dar tiempo o fotograma, defecto verificable, cambio solicitado a Valeria o Lucas y modo de comprobarlo. Distingue lo observado de la corrección propuesta.

Al rechazar, devuelve el dictamen a Valeria y Lucas. Solicita una nueva versión sólo dentro de las rondas del plan y conserva los recibos de las versiones anteriores. Compara la nueva versión con las correcciones solicitadas y vuelve a verificar las reglas duras.

## Anuncios de 30 segundos

Comprueba que la apertura demuestre el gancho, los subtítulos sean legibles a tamaño móvil y la marca y CTA confirmados permanezcan visibles en el cierre. Verifica el diálogo con audio. Si sólo hay fotogramas y medición, deja la inteligibilidad y la sincronía auditiva pendientes de escucha. Bloquea la exportación cuando falte un componente obligatorio.

## Evidencia del anuncio exportado

Examina el fotograma inicial y el del primer segundo. Si el gancho promete un producto que sólo aparece tras un paneo, devuelve el defecto con el tiempo y el encuadre que se necesita. Revisa en móvil la ubicación y agrupación de los subtítulos y que la CTA coincida con el brief. Registra por separado las mediciones de voz, música y pico verdadero, y la escucha musical cuando un revisor pueda reproducir la mezcla. La transcripción y la sonoridad por sí solas acreditan aspectos técnicos.

[Fase 5](../../../docs/specs/FASE-5-OPENCLAW-Y-MCP.md) · [Plan de implementación](../../../docs/PLAN-DE-IMPLEMENTACION.md) · [Arquitectura, secciones 2.4, 3.2 y 4.1](../../../docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md) · [Control de salida del renderer](../../../gpu-bridge-win11/render-worker/README.md#api-interna)
