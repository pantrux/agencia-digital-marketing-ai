# MoviePy 2.2.1 con Pillow 13

La rueda oficial de MoviePy 2.2.1 exige `pillow>=9.2.0,<12.0`. Los hallazgos de MoneyPrinterTurbo requieren ensayar Pillow 13. El preparador conserva MoviePy 2.2.1 y declara el cambio mediante la versión local `2.2.1+agency1`. Sólo cambia `Version` y el requisito de Pillow a `>=13,<14`. Renombra `.dist-info`, recalcula `RECORD` y conserva el contenido de los demás miembros, incluida la licencia MIT. [Metadatos de MoviePy 2.2.1](https://raw.githubusercontent.com/Zulko/moviepy/v2.2.1/pyproject.toml) · [Pillow 13.0.0](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html) · [Formato wheel](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)

## Derivación

El único insumo aceptado es `moviepy-2.2.1-py3-none-any.whl`, de 129.871 bytes y SHA256 `6b56803fec2ac54b557404126ac1160e65448e03798fa282bd23e8fab3795060`. Obtén ese archivo desde PyPI y conserva sus bytes originales. El preparador valida el pin, inspecciona el ZIP y comprueba todos los hashes de `RECORD`. Funciona sin instalar paquetes, consultar la red ni importar MoviePy. [Rueda original y procedencia](https://pypi.org/project/moviepy/2.2.1/#files)

```powershell
python -I -S -B docker/moneyprinter/derive_moviepy_wheel.py $source
python -I -S -B docker/moneyprinter/derive_moviepy_wheel.py $source --apply $derivedDirectory
```

El primer comando sólo calcula la salida y el recibo. Para publicar, usa un directorio nuevo, privado y vacío que ya exista. La escritura usa creación exclusiva. Conserva cualquier intento interrumpido para revisión y usa otro directorio para volver a publicar. La rueda se ordena por ruta, usa un timestamp fijo y se guarda sin compresión para reproducir los bytes. El resultado esperado tiene 382.772 bytes, 83 miembros, 81 contenidos preservados y SHA256 `0d912ce2121a7e409d385d7336f0911638af9285dc5ab8722155c90bd497e984`.

## Prueba local de texto y vídeo

Usa CPython 3.12 Windows amd64 y un directorio de prueba privado nuevo, fuera del repositorio. Prepara las ruedas de `requirements-moviepy-fixture-win.lock` en una wheelhouse privada, incluida la rueda derivada. Instala el conjunto completo por hash en un directorio `site` vacío. La rueda Windows de imageio-ffmpeg pertenece sólo a esta prueba y su ejecutable incorporado queda fuera de la ruta usada. `FFMPEG_BINARY` e `IMAGEIO_FFMPEG_EXE` apuntan al ejecutable explícito. Para Linux queda pendiente usar FFmpeg del sistema auditado y una rueda de imageio-ffmpeg preparada desde fuente sin binario incorporado. [Selección del ejecutable](https://raw.githubusercontent.com/imageio/imageio-ffmpeg/v0.6.0/imageio_ffmpeg/_utils.py) · [Distribución de imageio-ffmpeg](https://raw.githubusercontent.com/imageio/imageio-ffmpeg/v0.6.0/setup.py)

```powershell
python -I -m pip --isolated --disable-pip-version-check install --no-index `
  --no-deps --only-binary=:all: --no-compile --require-hashes `
  --find-links $wheelhouse --target $site -r $lock
```

Copia `fixture_moviepy_pillow.py` al directorio privado de la prueba y ejecútalo desde ahí. Usa un entorno sin credenciales, un FFmpeg local y una fuente ya verificados. Pasa sus rutas absolutas y hashes, así como un directorio de salida nuevo y vacío. Aplica un plazo externo de 45 s. La prueba deshabilita la carga dotenv y verifica que MoviePy no encontró ningún archivo dotenv. Crea un `TextClip` con dos líneas, comprueba que sólo aparezca en su intervalo y escribe un MP4 H.264 de un segundo. Vuelve a decodificarlo y comprueba resolución, velocidad, duración y presencia del texto en el cuadro activo.

```powershell
python -I -S -B ./fixture_moviepy_pillow.py --site $site `
  --font $font --font-sha256 $fontHash --ffmpeg $ffmpeg --ffmpeg-sha256 $ffmpegHash `
  --output-dir $renderDirectory
```

La prueba real en CPython 3.12.1 Windows cotejó 10 distribuciones y 11 relaciones activas de sus metadatos. MoviePy `2.2.1+agency1` y Pillow `13.0.0` cambiaron 3.081 píxeles en el cuadro activo. El MP4 H.264 se decodificó a 320 × 180, 12 fps y 1 s. Pesó 4.322 bytes, SHA256 `7c0beb507534146ed35d94a0e7bfa18fef97c64919bcab8d0b297e73fb52419b`, con FFmpeg `589e50b7…` y fuente local `edfbd22a…`. La composición verificada no necesitó cambios en el código de MoviePy.

Quedan por probar la clausura Linux cp311, la SCA de sus ruedas nativas y un vídeo completo de MoneyPrinterTurbo con audio, archivos locales y subtítulos Speaches.
