#!/bin/sh
# Las etapas se ejecutan por separado en el mismo SDK Linux propio sin red.
set -eu
export PATH=/usr/bin:/bin LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=0

fail() { printf '%s\n' "$1" >&2; exit 1; }

if test "$#" -eq 0 || test "$1" = --plan; then
    printf '%s\n' '{"sdk":false,"configure":false,"build":false,"plan_only":true}'
    exit 0
fi
test "$#" -ge 2 && test "$#" -le 3 || fail "Uso: sdk|configure SHA o build SHA SHA_CONFIGURE."
stage=$1
pin=$2
printf '%s\n' "$pin" | grep -Eq '^[a-f0-9]{64}$' || fail "SHA del contexto no válido."
test "$(uname -s) $(uname -m)" = 'Linux x86_64' || fail "Se requiere Linux x86_64."
test "$(printf '%s\n' /sys/class/net/*)" = /sys/class/net/lo || fail "El SDK requiere network=none."
awk '$5 == "/inputs" && $6 ~ /(^|,)ro(,|$)/ { found=1 } END { exit !found }' /proc/self/mountinfo || fail "Entradas sin montaje RO."
command -v git >/dev/null 2>&1 && fail "El SDK incluye Git."
cd /inputs
printf '%s  %s\n' "$pin" INPUTS.sha256 | sha256sum -c -

apk_state() {
    awk '
        function record() {
            if (p == "" && v == "") return
            if (p == "" || v == "") exit 1
            print p "=" v
            p = v = ""
        }
        /^P:/ { if (p != "") exit 1; p = substr($0, 3) }
        /^V:/ { if (v != "") exit 1; v = substr($0, 3) }
        /^$/ { record() }
        END { record() }
    ' /usr/lib/apk/db/installed > /sdk-data/apk.actual
    sort /sdk-data/apk.actual > /sdk-data/apk.sorted
    sort "$1" > /sdk-data/apk.expected
    cmp -s /sdk-data/apk.sorted /sdk-data/apk.expected || fail "Inventario APK distinto de $1."
}

sdk() {
    test "$(id -u):$(id -g)" = '0:0' || fail "La instalación requiere root del contenedor propio."
    umask 022
    mkdir -m 0755 /sdk-data
    sha256sum -c INPUTS.sha256 > /sdk-data/inputs-verification.log
    apk_state apk-base.lock
    set --
    while IFS= read -r package; do set -- "$@" "$package"; done < apk-add.lock
    test "$#" -eq 138 || fail "El SDK exige 138 APK adicionales exactos."
    /sbin/apk --no-network --no-scripts --no-commit-hooks --no-cache \
        --repositories-file /inputs/repositories --keys-dir /inputs/keys add "$@"
    apk_state apk-final.lock
    /usr/bin/ldconfig
    mkdir -m 0755 /sdk-data/openexr3 /sdk-data/numpy /sdk-data/ffmpeg-stock-dev
    tar -xf openexr3-overlay.tar -C /sdk-data/openexr3
    tar -xf numpy-headers.tar -C /sdk-data/numpy
    tar -xf ffmpeg-stock-dev-overlay.tar -C /sdk-data/ffmpeg-stock-dev
    while IFS= read -r path; do
        source=/sdk-data/ffmpeg-stock-dev/$path
        target=/$path
        test ! -e "$target" && test ! -L "$target" || fail "Archivo de desarrollo ocupado: $target"
        parent=$(dirname "$target")
        mkdir -p "$parent"
        test "$(readlink -f "$parent")" = "$parent" || fail "Padre de desarrollo ambiguo."
        if test -L "$source"; then
            ln -s "$(readlink "$source")" "$target"
        else
            cp -p "$source" "$target"
        fi
    done < ffmpeg-dev.paths
    while IFS= read -r path; do test -f "/$path" || fail "Falta el destino FFmpeg /$path."; done < ffmpeg-dev.paths
    /usr/bin/python3.12 -I -S -B /inputs/opencv_expanded.py source
    test "$(/usr/bin/protoc --version)" = 'libprotoc 29.5' || fail "protoc distinto del pin."
    printf '%s\n' "$pin" > /sdk-data/inputs.sha256
    chmod 0444 /sdk-data/inputs.sha256
    printf '%s\n' 'SUPOCLIP_OPENCV_SDK_157_APK_VERIFICADO'
}

work_guard() {
    test "$(id -u):$(id -g)" = '1000:1000' || fail "CMake requiere UID/GID 1000."
    test "$(cat /sdk-data/inputs.sha256)" = "$pin" || fail "Falta un SDK positivo para estos bytes."
    test ! -L /work && test "$(stat -c '%u:%g:%a' /work)" = '1000:1000:700' || fail "Falta /work privado y propio."
    umask 077
    unset PYTHONPATH PYTHONHOME CMAKE_PREFIX_PATH GIT_EXECUTABLE LD_PRELOAD LD_LIBRARY_PATH
    unset CC CXX CFLAGS CXXFLAGS CPPFLAGS LDFLAGS MAKEFLAGS MFLAGS PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR
    export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig TMPDIR=/work/tmp PYTHONDONTWRITEBYTECODE=1
}

configure() {
    work_guard
    mkdir /work/build /work/tmp
    set --
    while IFS= read -r flag; do set -- "$@" "$flag"; done < /inputs/cmake.flags
    if ! /usr/bin/cmake -S /sdk-data/source -B /work/build -G Ninja "$@" > /work/configure.log 2>&1; then
        tail -n 80 /work/configure.log >&2
        fail "CMake no pudo configurar OpenCV ampliado."
    fi
    /usr/bin/python3.12 -I -S -B /inputs/opencv_expanded.py configure > /work/configure.json
    sha256sum /work/configure.json
    printf '%s\n' 'SUPOCLIP_OPENCV_CONFIGURE_VERIFICADO'
}

build() {
    work_guard
    printf '%s\n' "$1" | grep -Eq '^[a-f0-9]{64}$' || fail "SHA de configure no válido."
    printf '%s  %s\n' "$1" /work/configure.json | sha256sum -c -
    /usr/bin/python3.12 -I -S -B /inputs/opencv_expanded.py configure > /work/configure-recheck.json
    cmp -s /work/configure-recheck.json /work/configure.json || fail "CMake cambió tras configure."
    mkdir /work/stage /work/wheels
    /usr/bin/cmake --build /work/build --parallel 2
    /usr/bin/cmake --install /work/build
    /usr/bin/python3.12 -I -S -B /inputs/opencv_expanded.py wheel > /work/wheel.json
    sha256sum /work/wheel.json
    printf '%s\n' 'SUPOCLIP_OPENCV_WHEEL_EMPAQUETADO'
}

case "$stage:$#" in
    sdk:2) sdk ;;
    configure:2) configure ;;
    build:3) build "$3" ;;
    *) fail "Etapa o argumentos OpenCV no admitidos." ;;
esac
