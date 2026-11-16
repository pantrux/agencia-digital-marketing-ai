# Muestra sintética ControlFace10K para Immich

ControlFace10K contiene identidades sintéticas generadas con SIG, que combina
Stable Diffusion Absolute Reality y ControlNet. Cita a Kassi Nzalasse, Rishav
Raj, Eli Laird y Corey Clark, *SIG: A Synthetic Identity Generation Pipeline
for Generating Evaluation Datasets for Face Recognition* (2024). Atribuye las
imágenes a los autores y conserva la licencia CC BY 4.0. La muestra selecciona
archivos y cambia sus nombres sin alterar los bytes de los PNG. [Dataset](https://huggingface.co/datasets/HuMInGameLab/ControlFace10K) · [Artículo](https://arxiv.org/abs/2409.08345) · [Licencia](https://creativecommons.org/licenses/by/4.0/).

La tarjeta de datos publica CC BY 4.0. El artículo identifica Absolute Reality,
sin fijar la revisión de sus pesos. Antes de extraer, registra la decisión de
uso de los PNG publicados y su procedencia. Este flujo no ejecuta los modelos
generadores. [Tarjeta oficial Absolute Reality 1.0](https://huggingface.co/Lykon/absolute-reality-1.0) · [Tarjeta oficial 1.81](https://huggingface.co/Lykon/absolute-reality-1.81).

```powershell
$python = (Get-Command python.exe).Source
& $python -I -S -B gpu-bridge-win11/immich_face_controlface_corpus.py plan
& $python -I -S -B gpu-bridge-win11/immich_face_controlface_corpus.py extract '<ruta absoluta nueva>' --plan-sha256 '<plan_sha256>'
```

El plan toma la primera identidad por UUID en cada combinación de cuatro grupos
del dataset, dos géneros y tres edades. Alterna las 24 identidades entre
calibración y validación. Cada identidad aporta las orientaciones `o3`, `o4` y
`o5`. El plan lee sólo 2,21 MB de metadatos. La extracción requiere respuestas
HTTP `206` y un máximo total de 30 MB. Si falla antes de publicar `fixtures.json`,
conserva el directorio parcial y usa un destino nuevo para otro intento.

## Medición del 13 de noviembre de 2026

La extracción acreditó 72 PNG de 512 × 512 y 24 identidades. La calibración usó 12 identidades y seleccionó `minScore=0.60`, `maxDistance=0.475` por el mayor número de componentes puros de tres vistas entre candidatos sin aristas entre identidades. El desempate favorece más aristas genuinas y una distancia menor. La [sonda](immich_face_controlface_probe.py) valida la partición y registra los recuentos sin guardar vectores.

Con los umbrales congelados, la validación CUDA de las otras 12 identidades detectó las 36 caras, registró 13 cruces en 594 pares de identidades distintas y dos componentes mixtos. Cuatro de las 12 identidades quedaron conectadas en componentes puros de tres vistas. Los componentes describen el grafo de pares. La [sonda de Immich](../docker/immich-server/face_manifest_accept_probe.js) mide por separado los grupos que produce la aplicación. Recibos `8aeb93bd…` y `332dc1e4…`.

Immich cotejó los 36 originales y detectó las 36 caras en cada partición. La corrida de calibración registró una fusión y dos identidades sin grupo. La validación en una base nueva, con el orden de carga inverso, registró dos fusiones y cuatro identidades sin grupo. El umbral quedó rechazado para producción. Recibos `6058f8b8…` y `5a9addcd…`.

## Efecto de las vistas previas

En la partición de calibración, la comparación de los PNG con los JPEG exactos que usó Immich reprodujo la única arista entre las identidades fusionadas. La [sonda de codificación](../docker/immich-server/probe_preview_quality.mjs) ejecuta el repositorio de medios de la imagen fijada, coteja los JPEG de calidad 80 por SHA-256 y genera otra serie a calidad 100. Ejecuta Node con entrada estándar en un contenedor sin red, con el corpus en `/source`, las vistas exactas y su manifiesto en `/exact` y una salida nueva en `/out` escribible por UID 1000. [Ruta de detección en Immich 3.3.6](https://github.com/immich-app/immich/blob/v3.3.6/server/src/services/person.service.ts#L390-L403)

La sonda reprodujo las 36 imágenes de cada calidad. Calidad 100 pasó de 1,15 MB a 7,81 MB para los mismos 36 retratos. En CUDA, aumentó los aciertos genuinos de 29 a 31 de 36 y eliminó el único cruce entre 594 pares. Immich agrupó 11 de las 12 identidades de calibración sin fusiones. En CPU persistió el cruce. La validación aislada de Immich con las 12 identidades reservadas produjo dos fusiones y tres identidades sin grupo. Recibos `b2cb76bf…`, `b06ea976…`, `5c6cfcf6…` y `6b44baee…`.

El modo `webp-lossless` de la misma sonda conserva la comprobación JPEG 80 y genera un WebP con un solo bloque VP8L por imagen. Compara sus píxeles decodificados con un PNG que pasa por el mismo remuestreo y perfil de color. La ejecución sin red en la imagen fijada reprodujo los 36 JPEG y verificó los 36 WebP, que ocuparon 7,84 MB. Recibo `3c9e0f56…`.

La comparación posterior en CUDA, con el umbral congelado, obtuvo las mismas 630 distancias para el PNG original y el WebP. Ambos acertaron 29 de 36 pares genuinos, sin cruces entre 594 pares de identidades distintas. Diez de doce identidades formaron un componente puro de tres vistas y `cf-01-o4` quedó aislada. La prueba se limitó a la calibración. Recibo `378ad95d…`.

## Agrupamiento por vecinos recíprocos

La [sonda de agrupamiento](immich_face_controlface_consensus.py) construye un grafo para un lote acotado. Compara componentes de al menos tres vistas con radios de `0.475` y `0.55`, y con un tercer brazo que conserva sólo las aristas recíprocas entre los dos vecinos más cercanos dentro de `0.55`. Si hay empate en el corte del segundo vecino, descarta las aristas empatadas. Usa `minScore=0.60`, aplica las mismas reglas a 18 órdenes y usa las etiquetas para puntuar el resultado una vez creados los grupos. Los vectores quedan en memoria. Esta sonda mide el algoritmo por lotes. La cola de reconocimiento de Immich asigna caras con su propia lógica.

En calibración CUDA, el radio `0.475` asignó 30 de 36 vistas en diez grupos puros. El radio `0.55` asignó todas las vistas y produjo un grupo mixto. Los vecinos recíprocos recuperaron 12 grupos puros y las 36 vistas, con el mismo resultado en los 18 órdenes. Recibo `f3bc6b71…`.

El plan `second-uuid-holdout-v1` fijó 24 identidades nuevas y tres vistas por identidad antes de descargar los PNG. Selecciona el segundo UUID completo de cada estrato de la revisión fijada. La sonda verifica el manifiesto histórico y rechaza cualquier UUID o huella de imagen compartidos. El plan `a9f04384…` y el manifiesto extraído `8d5a44eb…` fijan la cohorte.

En la cohorte nueva, CUDA detectó una cara en cada una de las 72 vistas. La regla de vecinos recíprocos asignó 50 vistas, dejó 22 sin grupo y produjo ocho grupos mixtos. Recuperó cinco de las 24 identidades completas y puras. El radio `0.475` recuperó seis y produjo tres grupos mixtos. Los resultados fueron idénticos en 18 órdenes y no hubo reintentos. Ninguna de las reglas cumplió los criterios de validación. Recibo `f9814a0b…`.

## Reflejo horizontal del recorte alineado

La [sonda](immich_face_controlface_flip.py) compara el vector habitual con la suma normalizada de dos inferencias AuraFace del mismo recorte alineado, una original y otra reflejada. Usa el mismo detector y umbral `0.60 / 0.475` en ambos brazos. La calibración CUDA pasó de 29 a 32 pares genuinos y de diez a once grupos completos y puros, con cero cruces. En CPU el reflejo produjo un cruce. Recibos `a870e2e5…` y `98ab88b6…`.

La cohorte `third-uuid-holdout-v1` eligió el tercer UUID completo de cada estrato. Su plan fijado se cotejó antes de descargar 72 vistas de 24 identidades. La sonda verificó que ninguna identidad ni imagen coincidiera con los dos manifiestos previos. Con CUDA y el umbral congelado, el reflejo elevó los pares genuinos aceptados de 51 a 61, con 29 cruces frente a 16 del brazo original. Hubo cuatro grupos mixtos en ambos brazos. La variante quedó descartada. Recibos `1192855e…` y `da27d84e…`.
