# Dependencias de MoneyPrinterTurbo para Wolfi

Esta unidad fija la clausura de MoneyPrinterTurbo 1.3.9 con el overlay Speaches existente. El objetivo es Linux x86-64, CPython 3.12.16 y la base Wolfi `ba69f696…` ya probada en aislamiento. La selección usa ruedas compatibles con `x86_64-manylinux_2_28`. El [manifiesto](wolfi-cp312-lock-inputs.json) liga el TAR completo del commit `6992380…`, las seis preimágenes verificadas, los ocho archivos del overlay materializado y la prueba del intérprete. Se volvieron a cotejar los bytes del TAR y del overlay.

## Alcance del grafo

- El [input](requirements-wolfi-cp312.in) conserva los proveedores originales Edge, Azure, DashScope, OpenAI, Google y LiteLLM, además de Redis y la UI/API. TwelveLabs sigue siendo un extra upstream separado. El marcador upstream de `audioop-lts` se activa desde Python 3.13 y queda fuera de este destino 3.12.
- La inspección de 58 módulos y puntos de entrada encontró un único import directo de `faster_whisper`, en `app/services/subtitle.py`. El overlay reemplaza ese archivo por el cliente LAN. PyAV carecía de consumidores directos en el código inspeccionado y acompañaba a Whisper. El nuevo grafo prescinde de `faster-whisper`, PyAV, CTranslate2 y ONNX Runtime. Conserva `tokenizers`, `huggingface-hub` y `hf-xet` porque la cadena de LiteLLM todavía los requiere.
- El lock incluye las ruedas propias de MoviePy `2.2.1+agency1` e imageio-ffmpeg `0.6.0`. El pin de imageio-ffmpeg identifica la rueda pura derivada del sdist, ya que las ruedas de plataforma del registro tienen la misma versión e incluyen otro FFmpeg. [Derivación MoviePy](README-MOVIEPY-WHEEL.md) · [Derivación imageio-ffmpeg](README-IMAGEIO-FFMPEG-WHEEL.md)
- El informe anterior de Python tenía 52 registros HIGH o CRITICAL. En este grafo, 49 corresponden a versiones que alcanzan sus correcciones informadas. Los tres restantes corresponden a setuptools y wheel, ausentes de este lock. Esa comparación sólo cubre el informe anterior. La base, las 27 ruedas con tag de plataforma y la imagen completa requieren una auditoría actual por separado.

## Reproducir la resolución

`uv 0.11.12` resolvió 94 paquetes desde PyPI y las dos ruedas propias. Avisó que el intérprete local era 3.12.1. La resolución fijó `requires-python >=3.12.16` y habilitó únicamente ruedas. Los marcadores y requisitos de los metadatos se reevaluaron para Linux x86-64 con Python 3.12.16 y se confirmaron 151 relaciones activas, sin paquetes sobrantes.

En una carpeta temporal fuera del repositorio, prepara `bound.in` con las líneas del input. Sustituye sólo las dos ruedas propias por `nombre @ file:///ruta/absoluta`, después de verificar sus SHA256 según `wheels` en el manifiesto. Escribe `floors.txt` con `resolver.transitive_constraints`. Usa un entorno limpio, con caché propia y el ejecutable de `uv` cotejado con el SHA256 registrado.

```text
uv --no-config --no-python-downloads --cache-dir ./cache pip compile bound.in \
  --constraints floors.txt --default-index https://pypi.org/simple \
  --python-version 3.12.16 --python-platform x86_64-manylinux_2_28 \
  --only-binary :all: --format pylock.toml --output-file pylock.toml
```

Para repetir las versiones exactas después de que cambie PyPI, añade como restricciones las versiones de registro del lock, sin sus argumentos `--hash`. El [lock de instalación](requirements-wolfi-cp312.lock) fija un único SHA256 por distribución. El manifiesto identifica su nombre de archivo y origen. Se cotejaron los SHA256 de `METADATA` de las 92 ruedas de registro mediante PEP 658 y se leyeron los metadatos de las dos ruedas propias verificadas. La resolución inicial conserva la huella de su `pylock.toml`. [Documentación del resolver uv](https://docs.astral.sh/uv/reference/cli/#uv-pip-compile)

## Consumo previsto

Prepara primero una wheelhouse propia con los 94 archivos exactos y coteja cada SHA256. Usa el TAR original más los ocho archivos del overlay fijados para arrancar la aplicación con sus puntos de entrada existentes. Instala las ruedas del lock en el entorno Linux elegido mediante `--no-index --only-binary=:all: --require-hashes --find-links <wheelhouse>`. El `pyproject.toml` original queda como evidencia de las dependencias upstream que se evaluaron.

La wheelhouse completa, el FFmpeg nativo y sus bibliotecas, la auditoría nueva y el render integral de MoneyPrinter en Linux siguen pendientes. Este lock es una entrada para esos pasos.
