#!/bin/sh
# Prepara el SDK y compila en un contenedor temporal con salida privada.
set -efu
export PATH=/usr/bin:/bin LC_ALL=C TZ=UTC

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

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
    ' /usr/lib/apk/db/installed > /tmp/ffmpeg-apk-state.unsorted
    sort /tmp/ffmpeg-apk-state.unsorted > /tmp/ffmpeg-apk-state
    sort "$1" > /tmp/ffmpeg-apk-expected
    cmp -s /tmp/ffmpeg-apk-state /tmp/ffmpeg-apk-expected || fail "El inventario APK difiere del perfil fijado."
}

prepare() {
    test "$(id -u):$(id -g)" = '0:0' || fail "La preparación requiere el UID/GID del SDK."
    umask 022
    cd /inputs
    sha256sum -c INPUTS.sha256
    apk_state /inputs/apk-base.lock
    set --
    while IFS= read -r package; do set -- "$@" "$package"; done < /inputs/apk-add.lock
    test "$#" -eq 50 || fail "El plan debe incluir exactamente 50 APK adicionales."
    /sbin/apk --no-network --no-scripts --no-commit-hooks --no-cache \
        --repositories-file /inputs/repositories --keys-dir /inputs/keys add "$@"
    apk_state /inputs/apk-final.lock
    /usr/bin/ldconfig

    overlay=$(mktemp -d /tmp/ffmpeg-dev.XXXXXX)
    tar -xf /inputs/ffmpeg-render-dev-overlay.tar -C "$overlay"
    while IFS= read -r path; do
        from="$overlay/$path"
        to="/$path"
        test ! -e "$to" && test ! -L "$to" || fail "Ruta de desarrollo ocupada: $to"
        parent=$(dirname "$to")
        mkdir -p "$parent"
        test "$(readlink -f "$parent")" = "$parent" || fail "Directorio de desarrollo ambiguo: $parent"
        if test -L "$from"; then
            ln -s "$(readlink "$from")" "$to"
        else
            cp -p "$from" "$to"
        fi
    done < /inputs/overlay.paths
    while IFS= read -r path; do
        test -f "/$path" || fail "Archivo o destino de desarrollo ausente: /$path"
    done < /inputs/overlay.paths
    command -v git >/dev/null 2>&1 && fail "El SDK incluye Git."
    printf '%s\n' 'SDK_FFMPEG_69_APK_VERIFICADO'
}

link_probe() {
    export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig
    test "$(/usr/bin/pkgconf --modversion libass)" = '0.17.5'
    test "$(/usr/bin/pkgconf --modversion x264)" = '0.165.3222'
    test "$(/usr/bin/pkgconf --modversion lame)" = '4.0'
    printf '%s  %s\n' '46f500867c13c047932b4413e271e191e3e484fb3b831d08933d1692c74e44f2' \
        /usr/lib/libass.a | sha256sum -c -
    cat > probe.c <<'EOF'
#include <ass/ass.h>

int main(void) {
    ASS_Library *library = ass_library_init();
    if (!library) return 1;
    ASS_Renderer *renderer = ass_renderer_init(library);
    if (!renderer) {
        ass_library_done(library);
        return 2;
    }
    ass_renderer_done(renderer);
    ass_library_done(library);
    return 0;
}
EOF
    cflags=$(/usr/bin/pkgconf --cflags libass)
    libs=$(/usr/bin/pkgconf --libs libass)
    # El manifiesto fija los .pc y todas sus rutas. Separar sus flags es deliberado.
    /usr/bin/gcc -fPIE -pie $cflags probe.c -Wl,-Map,libass-link.map $libs -o probe
    grep -Eq '(^|/)libass\.a\(' libass-link.map || fail "El enlazador no acredita libass.a."
    /usr/bin/readelf -d probe > libass-probe-dynamic.txt
    if grep -Eq 'TEXTREL|libass\.so' libass-probe-dynamic.txt; then
        fail "El enlace de libass contiene una dependencia dinámica o una relocación de texto."
    fi
    ./probe
    printf '%s\n' 'LIBASS_0_17_5_ENLACE_EJECUTADO'
}

build() {
    test "$(id -u):$(id -g)" = '1000:1000' || fail "La compilación requiere UID/GID 1000."
    test "$(uname -m)" = x86_64
    test -d /outputs && test ! -L /outputs || fail "Falta el directorio de salida propio."
    command -v git >/dev/null 2>&1 && fail "El SDK incluye Git."
    umask 077
    mkdir /outputs/build
    cd /outputs/build
    mkdir tmp source bin
    export TMPDIR=/outputs/build/tmp SOURCE_DATE_EPOCH=1781922496
    unset LD_PRELOAD LD_LIBRARY_PATH PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR
    unset CC CFLAGS CPPFLAGS LDFLAGS MAKEFLAGS MFLAGS C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH
    link_probe

    source=/opt/ffmpeg-input/ffmpeg-7.1.5.tar.xz
    printf '%s  %s\n' 'de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f' "$source" | sha256sum -c -
    /usr/bin/bash -o pipefail -c '/usr/bin/xz -dc "$1" | tar -xf - -C "$2"' -- "$source" /outputs/build/source
    cd source/ffmpeg-7.1.5
    test "$(cat VERSION)" = '7.1.5'
    revision=7.1.5 ./configure --prefix=/opt/supoclip-ffmpeg-ass \
        --disable-autodetect --disable-shared --enable-static --disable-ffplay \
        --disable-doc --disable-debug --disable-version-tracking --enable-gpl \
        --enable-libass --enable-libx264 --enable-libmp3lame \
        --x86asmexe=/usr/bin/yasm --pkg-config=/usr/bin/pkgconf --cc=/usr/bin/gcc
    grep -Fx '#define CONFIG_LIBASS 1' config.h
    for feature in SUBTITLES_FILTER LIBX264_ENCODER LIBMP3LAME_ENCODER AAC_ENCODER; do
        grep -Fx "#define CONFIG_$feature 1" config_components.h
    done
    /usr/bin/make -j2 REVISION=7.1.5 ffmpeg ffprobe
    for binary in ffmpeg ffprobe; do
        /usr/bin/readelf -d "$binary" > "/outputs/build/$binary-dynamic.txt"
        if grep -Eq 'TEXTREL|\((RUNPATH|RPATH)\)|Shared library: \[(libav|libsw|libpostproc|libass)' "/outputs/build/$binary-dynamic.txt"; then
            fail "El binario $binary incumple el cierre ELF privado."
        fi
        cp -p "$binary" /outputs/build/bin/
    done
    cd /outputs/build/bin
    ./ffmpeg -hide_banner -filters > ../filters.txt
    grep -Eq '^[[:space:]]*[.TSC]{2,3}[[:space:]]+subtitles[[:space:]]+V->V' ../filters.txt
    ./ffmpeg -hide_banner -encoders > ../encoders.txt
    for encoder in libx264 libmp3lame aac; do
        grep -Eq "^[[:space:]]*[AV][A-Z.]+[[:space:]]+$encoder[[:space:]]" ../encoders.txt
    done
    ./ffprobe -version > ../ffprobe-version.txt
    sha256sum ffmpeg ffprobe > ../binaries.sha256
    printf '%s\n' 'FFMPEG_7_1_5_BUILD_VERIFICADO'
}

test "$#" -eq 1 || fail "Indique prepare o build."
case "$1" in
    prepare) prepare ;;
    build) build ;;
    *) fail "Etapa FFmpeg desconocida." ;;
esac
