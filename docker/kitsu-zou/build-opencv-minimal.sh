#!/usr/bin/env bash
# Receta Linux offline. Plan por defecto; el controlador acredita primero el SDK.
set -euo pipefail

if [[ $# -eq 0 || ${1:-} == --plan ]]; then
  printf '%s\n' '{"plan_only":true,"sdk_preparation_required":true,"build_enabled":false,"network_required":false}'
  exit 0
fi
if [[ $# -ne 9 || $1 != --execute ]]; then
  printf '%s\n' 'Uso: build-opencv-minimal.sh --execute MANIFEST SHA INPUTS WORK SOURCE OVERLAY NUMPY_HEADERS CONTROLLER_PYTHON' >&2
  exit 2
fi
[[ $(uname -s) == Linux ]] || { printf '%s\n' 'La compilación requiere Linux GNU.' >&2; exit 2; }
manifest=$2; manifest_sha=$3; inputs=$4; work=$5; source_root=$6; overlay=$7; numpy_headers=$8; controller_python=$9
recipe_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

# Esta versión entrega inputs para aprobación; el manifiesto permanece cerrado.
"$controller_python" - "$manifest" "$manifest_sha" "$inputs" "$work" "$source_root" "$overlay" "$numpy_headers" <<'PY'
import hashlib,json,os,stat,sys
import re
from pathlib import Path
path,digest,inputs,*roots=sys.argv[1:]
raw=Path(path).read_bytes()
if hashlib.sha256(raw).hexdigest()!=digest:raise SystemExit('Manifiesto diferente del pin externo.')
value=json.loads(raw)
if value.get('build_enabled') is not True or value.get('acceptance',{}).get('native_sdk_sca_verified') is not True or value.get('acceptance',{}).get('root_pins_backed_for_new_manifest') is not True:raise SystemExit('Build cerrado hasta aprobar inputs y SDK auditado.')
if {item.name for item in Path('/sys/class/net').iterdir()}!={'lo'}:raise SystemExit('El controlador debe aislar la red: sólo loopback permitido.')
for name in roots:
    root=Path(name)
    if not root.is_absolute() or '..' in root.parts or not re.fullmatch(r'/[A-Za-z0-9_./-]+',name):raise SystemExit('Raíz de build Linux no permitida.')
    for part in (*reversed(root.parents),root):
        info=part.lstat()
        if stat.S_ISLNK(info.st_mode):raise SystemExit('No se admiten raíces enlazadas.')
work=Path(roots[0]);info=work.stat()
if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:raise SystemExit('Work requiere propietario actual y modo0700.')
for name in (inputs,roots[1],roots[2],roots[3]):
    if not os.statvfs(name).f_flag & os.ST_RDONLY:raise SystemExit('Fuente y entradas deben estar en montajes de sólo lectura.')
if any((work/name).exists() for name in ('build','stage','wheels','home','tmp')):raise SystemExit('No se reutiliza una salida anterior.')
PY
"$controller_python" "$recipe_dir/package_opencv_minimal.py" --manifest "$manifest" --manifest-sha256 "$manifest_sha" --inputs-root "$inputs" --verify-inputs

# SDK ya preparado y atestiguado por el controlador: no instalar ni resolver aquí.
export PATH=/usr/bin:/bin
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH PYTHONHOME CMAKE_PREFIX_PATH GIT_EXECUTABLE
[[ ! -x /usr/bin/git && ! -x /bin/git ]] || { printf '%s\n' 'Git no puede estar disponible en el SDK.' >&2; exit 2; }
mkdir -m 700 -- "$work/build" "$work/stage" "$work/wheels" "$work/home" "$work/tmp"
sandbox_env=(/usr/bin/env -i PATH=/usr/bin:/bin "HOME=$work/home" "TMPDIR=$work/tmp" PYTHONDONTWRITEBYTECODE=1 LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=0)
flags=(
  '-DBUILD_LIST=core,imgproc,imgcodecs,python3' -DBUILD_opencv_python3=ON -DBUILD_opencv_python2=OFF
  -DBUILD_opencv_world=OFF -DBUILD_opencv_videoio=OFF -DBUILD_opencv_highgui=OFF
  -DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_V4L=OFF -DWITH_QT=OFF -DWITH_GTK=OFF -DWITH_OPENGL=OFF
  -DWITH_OPENCL=OFF -DWITH_CUDA=OFF -DWITH_IPP=OFF -DWITH_ITT=OFF -DWITH_TBB=OFF -DWITH_LAPACK=OFF -DWITH_EIGEN=OFF
  -DWITH_PNG=ON -DBUILD_PNG=OFF -DBUILD_ZLIB=OFF -DWITH_OPENEXR=ON -DBUILD_OPENEXR=OFF -DWITH_IMGCODEC_HDR=ON
  -DWITH_IMGCODEC_GIF=OFF -DWITH_IMGCODEC_PXM=OFF -DWITH_IMGCODEC_SUNRASTER=OFF -DWITH_IMGCODEC_PFM=OFF
  -DWITH_JPEG=OFF -DWITH_TIFF=OFF -DWITH_WEBP=OFF -DWITH_OPENJPEG=OFF -DWITH_JASPER=OFF -DWITH_AVIF=OFF -DWITH_JPEGXL=OFF
  -DWITH_GDAL=OFF -DWITH_GDCM=OFF -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_opencv_apps=OFF -DBUILD_opencv_java=OFF
  -DPYTHON3_LIMITED_API=OFF -DCMAKE_DISABLE_FIND_PACKAGE_Git=ON -DOPENCV_VCSVERSION=4.13.0+agency1
  -DOPENCV_SKIP_PYTHON_LOADER=ON -DOPENCV_LIB_INSTALL_PATH=lib -DOPENCV_FORCE_PYTHON_LIBS=OFF
  "-DCMAKE_INSTALL_RPATH=\$ORIGIN/opencv_python_headless.libs;\$ORIGIN" -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=OFF
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DENABLE_BUILD_HARDENING=ON
  -DCMAKE_C_COMPILER=/usr/bin/gcc -DCMAKE_CXX_COMPILER=/usr/bin/g++
  -DCMAKE_MAKE_PROGRAM=/usr/lib/ninja-build/bin/ninja
  "-DCMAKE_C_FLAGS=-ffile-prefix-map=$work=/agency-build -ffile-prefix-map=$source_root=/agency-source"
  "-DCMAKE_CXX_FLAGS=-ffile-prefix-map=$work=/agency-build -ffile-prefix-map=$source_root=/agency-source"
  -DPYTHON3_EXECUTABLE=/usr/bin/python3.12 -DPYTHON_DEFAULT_EXECUTABLE=/usr/bin/python3.12
  -DPYTHON3_NUMPY_VERSION=2.4.6 "-DPYTHON3_NUMPY_INCLUDE_DIRS=$numpy_headers"
  "-DCMAKE_PREFIX_PATH=$overlay/usr" "-DOpenEXR_DIR=$overlay/usr/lib/cmake/OpenEXR"
  "-DImath_DIR=$overlay/usr/lib/cmake/Imath" "-DCMAKE_INSTALL_PREFIX=$work/stage"
  "-DOPENCV_PYTHON3_INSTALL_PATH=$work/stage" "-DOPENCV_DOWNLOAD_PATH=$work/forbidden-download-cache"
)
# El contenedor del controlador debe tener network=none y entradas montadas RO.
"${sandbox_env[@]}" /usr/bin/cmake -S "$source_root" -B "$work/build" -G Ninja "${flags[@]}"
"$controller_python" "$recipe_dir/package_opencv_minimal.py" --manifest "$manifest" --manifest-sha256 "$manifest_sha" --cache "$work/build/CMakeCache.txt" --cvconfig "$work/build/opencv2/cvconfig.h" --modules "$work/build/opencv2/opencv_modules.hpp" --openexr-directory "$overlay/usr/lib/cmake/OpenEXR"
"${sandbox_env[@]}" /usr/bin/cmake --build "$work/build" --parallel 2
"${sandbox_env[@]}" /usr/bin/cmake --install "$work/build"
"$controller_python" "$recipe_dir/package_opencv_minimal.py" --manifest "$manifest" --manifest-sha256 "$manifest_sha" --cache "$work/build/CMakeCache.txt" --cvconfig "$work/build/opencv2/cvconfig.h" --modules "$work/build/opencv2/opencv_modules.hpp" --openexr-directory "$overlay/usr/lib/cmake/OpenEXR" --package --staging "$work/stage" --output "$work/wheels" --license "$source_root/LICENSE"
