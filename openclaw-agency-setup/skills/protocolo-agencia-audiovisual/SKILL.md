---
name: protocolo-agencia-audiovisual
description: Coordina los relevos de un encargo audiovisual entre los cinco agentes de la agencia mediante el brief, la escaleta técnica y el dictamen QA. Usa esta skill al recibir material, preparar el montaje o resolver una iteración según el plan acreditado.
---

# Protocolo audiovisual de la agencia

Usa la identidad del proyecto, las referencias de medios autorizadas y el plan ya acreditado del encargo. Si falta alguno, registra el dato pendiente antes de pedir procesamiento o publicación. Conserva el original y las versiones previas del mismo trabajo.

## Relevos y archivos

1. **Sofía, `agency-account`**. Recoge objetivo, audiencia, entregables y restricciones del encargo. Identifica las fuentes y registra la inspección técnica y el ID de Kitsu, cuando esté disponible, en `brief.json`.
2. **Mateo, `agency-strategist`**. Propone tres ángulos con gancho y copy. Las tarjetas de valor complementan lo que dice la voz. Entrega sus opciones a Valeria junto con las referencias de B-roll pertinentes.
3. **Valeria, `agency-director`**. Elige un ángulo y entrega `edl_spec.json` y `tokens.ts`. La escaleta identifica fuentes, cortes con tiempos, B-roll, indicaciones de encuadre y elementos que deben ocultarse. Usa la tira `Y=0..1920` y los datos de escala y desplazamiento al traducir encuadres de la fuente. Conserva la relación entre el tiempo del original y el tiempo de salida.
4. **Lucas, `agency-editor`**. Lleva la escaleta a la composición permitida, genera fotogramas QA y el máster conforme al plan. Entrega la versión exacta, sus referencias y la evidencia de render a Elena. Si corresponde al encargo, registra esa revisión en Kitsu con los permisos del proyecto.
5. **Elena, `agency-critic`**. Revisa imagen, audio y copy de esa versión. Entrega `qa_verdict.json` con el puntaje de 0 a 100, hallazgos con evidencia y correcciones si hay rechazo. Un puntaje inferior a 85 o el incumplimiento de una regla dura devuelve la pieza a Valeria y Lucas. Conserva el dictamen anterior al iterar.

Los tres archivos `.json` contienen datos JSON. Usa el esquema aprobado si acompaña al encargo. Las descripciones anteriores indican el contenido mínimo del relevo. El contrato de transporte MCP y el cuerpo HTTP del renderer se validan por separado.

## Restricciones por plan

| Plan | Salida del renderer | Revisiones previstas |
| --- | --- | --- |
| `Free` | MP4 1080p con marca de agua | 1 pasada QA |
| `Pro` | MP4 1080p sin marca Free | Hasta 2 rondas autónomas (`v1` a `v2`) y 1 ajuste humano |
| `Ultra` | MP4 1080p o 4K sin marca Free | Hasta 3 rondas autónomas (`v1` a `v3`) y 3 ajustes humanos |
| `Ilimitado` | MP4 o ProRes 4444 alfa en 1080p o 4K | 3 rondas autónomas y ajustes humanos ilimitados |

Si se agotan las rondas, conserva el último dictamen y señala qué impide la entrega. La comprobación de las cuotas comerciales corresponde a Fase 6. El contrato del renderer ya fija 600 segundos por pieza y hasta 20 minutos de procesamiento.

## Encargo de anuncio corto

- **Sofía** confirma acción final, público, producto y fuentes autorizadas. Registra la CTA literal. Confirma con el cliente los contactos que se mostrarán.
- **Mateo** propone ganchos que una toma real pueda demostrar. Proporciona una progresión con apertura, prueba del producto y cierre, más el texto publicable. Conserva la CTA del cliente.
- **Valeria** asigna una función a cada plano. Usa frases completas de la transcripción temporizada, conserva su sentido y deja cuatro segundos de producto despejado para el cierre.

## Control editorial del anuncio

Valeria abre la vista previa en tamaño móvil y comprueba que el primer fotograma muestre el producto que promete el gancho. Selecciona otro punto de entrada si el encuadre revela el producto tarde. Lucas conserva un rótulo o una placa sólo durante su función editorial. Entrega la vista previa real a Elena antes del máster.

[Fase 5](../../../docs/specs/FASE-5-OPENCLAW-Y-MCP.md) · [Plan de implementación](../../../docs/PLAN-DE-IMPLEMENTACION.md) · [Arquitectura, secciones 3.2 y 4.1](../../../docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md) · [Contrato del renderer](../../../gpu-bridge-win11/render-worker/README.md)
