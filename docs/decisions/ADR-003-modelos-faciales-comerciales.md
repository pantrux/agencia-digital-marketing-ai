# ADR-003: Selección facial YuNet y AuraFace con permisos comerciales declarados

## Estado

Aceptada como decisión técnica interna para sustituir la selección facial Buffalo_L de evaluación. Se conservan el protocolo de Immich y las identidades GPT de los cinco agentes. La elección de artefactos no acredita su inferencia CUDA ni constituye una garantía jurídica sobre toda su procedencia.

## Fecha

2026-11-05.

## Contexto

El endpoint Immich ML `192.168.0.6:3003` necesita detección y reconocimiento facial para una plataforma destinada a uso comercial. La investigación inicial fijó `immich-app/buffalo_l` en la revisión `d09715916a0778919a770c343533641e250b8699`, pero no acreditó una licencia comercial de esos pesos.

La [política primaria de InsightFace](https://github.com/deepinsight/insightface/blob/master/README.md#license) distingue el código MIT de los datos y modelos destinados a investigación no comercial, y remite las licencias de paquetes como Buffalo_L a un acuerdo separado. La licencia del código no basta para atribuir permiso comercial a los pesos. No se solicitó un acuerdo comercial ni se enviaron datos de clientes al proveedor como parte de esta decisión.

## Decisión

Registrar el modelo combinado propio **`agency_yunet_auraface_v1`**, con caché `face/agency_yunet_auraface_v1`, utilizando exclusivamente los dos pesos siguientes del [manifiesto facial](../../gpu-bridge-win11/face-artifacts.json):

| Función | Artefacto seleccionado | Revisión fijada | Licencia publicada |
|---|---|---|---|
| Detección y cinco puntos faciales | OpenCV YuNet 2023mar, `face_detection_yunet_2023mar.onnx`; copia local `detection.onnx` | `d4938dfc9d4ec5d098bfa33e98b3f3345a236586` en `opencv/opencv_zoo` | MIT para los archivos del directorio YuNet. |
| Reconocimiento | **Solo** `fal/AuraFace-v1/glintr100.onnx`; copia local `recognition.onnx` | `af6d057c9b0ec4071d4c49c80e3539258798b609` | Apache-2.0 publicada por fal. |

| Archivo local | Bytes fijados | SHA256 esperado |
|---|---:|---|
| `detection.onnx` | 232589 | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `recognition.onnx` | 260694151 | `a7933ea5330113b01c9b60351d8f4c33003f145d8470ac5f0e52ee2effe25c60` |

El [README YuNet fijado](https://huggingface.co/opencv/opencv_zoo/blob/d4938dfc9d4ec5d098bfa33e98b3f3345a236586/models/face_detection_yunet/README.md) declara MIT para todos los archivos de ese directorio, acompañado de su [licencia](https://huggingface.co/opencv/opencv_zoo/blob/d4938dfc9d4ec5d098bfa33e98b3f3345a236586/models/face_detection_yunet/LICENSE). La [tarjeta AuraFace fijada](https://huggingface.co/fal/AuraFace-v1/blob/af6d057c9b0ec4071d4c49c80e3539258798b609/README.md) publica Apache-2.0 y declara entrenamiento orientado al uso comercial; su [LICENSE.md](https://huggingface.co/fal/AuraFace-v1/blob/af6d057c9b0ec4071d4c49c80e3539258798b609/LICENSE.md) contiene esa licencia. Esta selección se basa en esas declaraciones del publicador, no en una auditoría independiente de todos los datos de entrenamiento o derechos de terceros.

No descargar ni activar el resto del paquete AuraFace como consecuencia de esta selección: `scrfd_10g_bnkps.onnx`, `1k3d68.onnx`, `2d106det.onnx` y `genderage.onnx` quedan fuera del manifiesto autorizado. El ejemplo genérico de descarga del paquete completo no representa este contrato.

## Alternativas consideradas

| Alternativa | Evaluación | Decisión |
|---|---|---|
| Mantener Buffalo_L para el SaaS comercial | Preserva el modelo inicial, pero no existe autorización comercial acreditada de sus pesos. | No utilizarlo como selección comercial de esta entrega; conservar su registro histórico de evaluación. |
| Obtener una licencia comercial separada de Buffalo_L | Podría resolver el alcance contractual de ese paquete. | No elegida para este incremento; no se ha contactado al proveedor ni contratado una licencia. |
| YuNet y solo el reconocedor AuraFace indicado | Ofrece permisos publicados compatibles con la intención comercial y mantiene un vector de 512 componentes. | Elegida con artefactos y avisos fijados; exige validación funcional propia. |
| Activar todos los pesos de AuraFace | Sigue el ejemplo general de su tarjeta. | No elegida: el alcance de esta revisión autoriza únicamente `glintr100.onnx` junto con YuNet. |

## Consecuencias técnicas y de distribución

- Mantener el protocolo original de peticiones y respuestas Immich. El cambio afecta al modelo facial interno y su identificador explícito; no se registra AuraFace con el nombre Buffalo_L ni se cambian los modelos `openai/gpt-6.1-sol` y `openai/gpt-6.1-terra` asignados a los agentes.
- Adaptar el preprocesamiento y la decodificación de YuNet, la alineación facial y la normalización de AuraFace conforme al manifiesto. La detección y el reconocimiento deben acreditar `CUDAExecutionProvider`; el procesamiento geométrico posterior permitido en CPU no constituye una alternativa silenciosa a la inferencia GPU.
- El vector de 512 componentes conserva el ancho esperado, pero **no implica el mismo espacio de embeddings**. No reutilizar vectores Buffalo_L, no reemplazar automáticamente un modelo existente y regenerar embeddings y calibrar umbrales antes de usar datos de clientes. La futura configuración del servidor Immich debe seleccionar el nuevo ID explícitamente.
- Conservar junto a los pesos los cuatro documentos fijados: `LICENSE.YuNet.txt`, `NOTICE.YuNet.md`, `LICENSE.AuraFace.md` y `NOTICE.AuraFace.md`. Mantener los avisos y documentar modificaciones conforme a las licencias aplicables. La descarga de pesos no exige enviar fotografías ni embeddings al publicador.
- Las licencias publicadas no acreditan por sí solas derechos sobre fotografías de clientes, ausencia de sesgos ni aptitud para un uso particular. La tarjeta AuraFace reconoce limitaciones de generalización; esas condiciones deben evaluarse con medios autorizados y pruebas propias.

## Verificación pendiente

El responsable coordina la descarga de los seis artefactos del manifiesto: dos pesos y cuatro documentos. Esta decisión no acredita que su descarga completa ni la nueva inferencia hayan terminado. La aceptación requiere comprobar bytes y hashes completos, cargar el nuevo ID, ejecutar detección y reconocimiento CUDA reales y verificar la compatibilidad de la respuesta Immich y sus consumidores.

La investigación de metadatos está registrada localmente en `.tools/evidence/face-commercial-candidates-research.json`. Ese registro no sustituye la aceptación funcional. Esta documentación no inicia inferencias, no publica fuentes ni transmite imágenes al proveedor.

## Documentos relacionados

- [Arquitectura vigente](../ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md).
- [Plan de implementación, Fases 3 y 4](../PLAN-DE-IMPLEMENTACION.md).
- [Manifiesto facial fijado](../../gpu-bridge-win11/face-artifacts.json).

