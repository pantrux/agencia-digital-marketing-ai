# Rueda pura de imageio-ffmpeg 0.6.0

PyPI publica seis ruedas de plataforma para `0.6.0`, con FFmpeg incorporado. El sdist oficial de 25.210 bytes excluye esos ejecutables. El empaquetador conserva los siete archivos del paquete, `PKG-INFO` como `METADATA` y la licencia BSD-2-Clause byte por byte. La salida `imageio_ffmpeg-0.6.0-py3-none-any.whl` conserva la versión y el requisito Python `>=3.9`. [Distribuciones originales](https://pypi.org/project/imageio-ffmpeg/0.6.0/#files) · [Empaquetado upstream](https://raw.githubusercontent.com/imageio/imageio-ffmpeg/v0.6.0/setup.py)

## Reproducción

Descarga `imageio_ffmpeg-0.6.0.tar.gz` desde PyPI y comprueba su SHA256 `e2556bed8e005564a9f925bb7afa4002d82770d6b08825078b7697ab88ba1755`. El empaquetador vuelve a comprobar ese pin, lee el TAR en memoria y admite sólo los miembros esperados. Construye `WHEEL` y `RECORD`, con orden y fecha fijos y sin compresión. No necesita red, instalador ni ejecutar `setup.py`.

```powershell
python -I -S -B docker/moneyprinter/build_imageio_ffmpeg_wheel.py $source
python -I -S -B docker/moneyprinter/build_imageio_ffmpeg_wheel.py $source --apply $derivedDirectory
```

El primer comando calcula el resultado sin escribir. Para publicar, el segundo requiere un directorio nuevo, privado y vacío. La creación es exclusiva y vuelve a leer la rueda publicada. El resultado tiene 45.492 bytes, 11 miembros y SHA256 `3bfb7cac65e7a1038b082d812f737174448283ae306ca9354be61446afee35a1`. El lock debe admitir sólo ese hash para `imageio-ffmpeg==0.6.0`, porque otras ruedas conservan la misma versión e incluyen binarios.

## Prueba del ejecutable explícito

Para la prueba, usa un directorio `site` nuevo y un lock con una sola línea `imageio-ffmpeg==0.6.0 --hash=sha256:3bfb7cac65e7a1038b082d812f737174448283ae306ca9354be61446afee35a1`. Coloca sólo la rueda derivada en la wheelhouse. Pasa la ruta absoluta y el SHA256 del FFmpeg aprobado para ese host.

```powershell
python -I -m pip --isolated --disable-pip-version-check install --no-index `
  --no-deps --no-compile --only-binary=:all: --require-hashes `
  --find-links $wheelhouse --target $site -r $lock
python -I -S -B docker/moneyprinter/fixture_imageio_ffmpeg.py --site $site `
  --ffmpeg $ffmpeg --ffmpeg-sha256 $ffmpegHash
```

La prueba instala la rueda por hash, comprueba que `binaries` sólo contiene `README.md` y `__init__.py`, y verifica `IMAGEIO_FFMPEG_EXE` con `get_ffmpeg_exe()`. `get_ffmpeg_version()` lanza el binario indicado una vez, con `-version` y un plazo de 8 s. En la ejecución local de Windows respondió FFmpeg `9.0.2-full_build-www.gyan.dev`, SHA256 `589e50b7…`. El selector offline de pip también admitió la rueda para Linux x86-64 CPython 3.11. Esa selección comprueba los tags y metadatos. El ejecutable, sus bibliotecas y la aplicación se verificarán por separado en el runtime Linux de destino. [Implementación del selector de FFmpeg](https://raw.githubusercontent.com/imageio/imageio-ffmpeg/v0.6.0/imageio_ffmpeg/_utils.py)
