#!/bin/sh
# Compila FFmpeg para los archivos locales de Zou dentro del SDK Wolfi fijado.
set -eu
export PATH=/usr/bin:/bin LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=1789699562
umask 077

inputs=/ffmpeg-shared-inputs
fixes=/ffmpeg-reviewed-inputs
work=/outputs/ffmpeg-902-shared
deps="$work/deps"
source="$work/source/ffmpeg-9.0.2"
libass=/outputs/libass-95127/prefix
prefix=/opt/zou-media-v1
products=/products

check() {
    printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{64}$'
    test -f "$2" && test ! -L "$2"
    printf '%s  %s\n' "$1" "$2" | sha256sum -c -
}

test "$#" -eq 1
test "$(uname -s) $(uname -m)" = 'Linux x86_64'
test "$(id -u):$(id -g)" = '1000:1000'
test "$(printf '%s\n' /sys/class/net/*)" = /sys/class/net/lo
test -z "$(ls -A "$products")"
if command -v git >/dev/null 2>&1; then exit 1; fi
unset LD_PRELOAD LD_LIBRARY_PATH PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR
unset CC CFLAGS CPPFLAGS LDFLAGS MAKEFLAGS MFLAGS C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH

check 78434e74f631d99b4a250eef0f67a3da18f0c0fd5da788674c6497aed691ae97 "$inputs/INPUTS.sha256"
(cd "$inputs" && sha256sum -c INPUTS.sha256)
check 13a16197814d7f7c852da169fca3306213393e826d42f0963519c181c0488160 "$libass/lib/libass.a"
check 8bedea9988e89c6a69f7e19e825ce974abfca5e848a1404712f33aeffb36dacb "$libass/lib/pkgconfig/libass.pc"
check "$1" "$fixes/SHA256SUMS"
(cd "$fixes" && sha256sum -c SHA256SUMS)

mkdir "$work"
awk '
    function emit() { if (p != "" && v != "") print p "=" v; p = v = "" }
    /^P:/ { p = substr($0, 3) }
    /^V:/ { v = substr($0, 3) }
    /^$/ { emit() }
    END { emit() }
' /usr/lib/apk/db/installed > "$work/apk.unsorted"
sort "$work/apk.unsorted" > "$work/apk.actual"
sort "$inputs/apk-final.lock" > "$work/apk.expected"
cmp "$work/apk.actual" "$work/apk.expected"
mkdir "$work/source" "$work/tmp" "$deps" "$work/stage"
tar -xf "$inputs/dev-overlay.tar" -C "$deps"
ln -s /usr/lib/libz.so.1 "$deps/lib/libz.so"
/usr/bin/bash -o pipefail -c '/usr/bin/xz -dc "$1" | tar -xf - -C "$2"' -- \
    "$inputs/ffmpeg-9.0.2.tar.xz" "$work/source"
cd "$source"
test "$(cat VERSION)" = 9.0.2
check 5b281b249ad5ad859030b4da0a24ec105b5abff7b3d87a65b302df8bf5fd9833 configure
check da459d3113d590cffbcea6f14ad55de20a32869cf8979eea17ab3bf83e2641ba libavutil/encryption_info.c
check eac144115d4a26b613a4e0720257d7ba07b064824aaff64aae0c8b65ea2059f5 libavcodec/bsf/aac_adtstoasc.c
cp "$fixes/encryption_info.c" libavutil/encryption_info.c
cp "$fixes/aac_adtstoasc.c" libavcodec/bsf/aac_adtstoasc.c
cmp "$fixes/encryption_info.c" libavutil/encryption_info.c
cmp "$fixes/aac_adtstoasc.c" libavcodec/bsf/aac_adtstoasc.c

# Cada entrada tiene la huella del TAR firmado, la de la corrección y su destino.
revision=9.0.2-zou.1
extra_version=zou.1
set --
if test -f "$fixes/backports.tsv"; then
    tab=$(printf '\t')
    applied=
    while IFS="$tab" read -r before after destination; do
        case "$destination" in
            libavcodec/hevc/ps.c|libavcodec/flicvideo.c|libavcodec/exr.c|\
            libavcodec/encode.c|libavcodec/mpegaudiodec_template.c|libavcodec/mpegaudiodec_common.c) ;;
            *) exit 1 ;;
        esac
        check "$before" "$destination"
        replacement="$fixes/$(basename "$destination")"
        check "$after" "$replacement"
        cp "$replacement" "$destination"
        cmp "$replacement" "$destination"
        applied="$applied $destination"
    done < "$fixes/backports.tsv"
    case " $applied " in *' libavcodec/hevc/ps.c '*) ;; *) exit 1 ;; esac
    case " $applied " in *' libavcodec/exr.c '*) ;; *) exit 1 ;; esac
    case " $applied " in *' libavcodec/mpegaudiodec_'*) ;; *) exit 1 ;; esac
    revision=9.0.2-zou.2
    extra_version=zou.2
    # Se conservan PNG normal y OpenEXR de OpenCV, que usa su propia biblioteca.
    set -- --disable-decoder=flic,huffyuv,ffvhuff,imc,iac,cljr,vble,adpcm_g726,adpcm_g726le,g2m,webp,webp_anim \
        --disable-demuxer=flic,image_webp_pipe,image_jpegxl_pipe,jpegxl_anim \
        --disable-parser=webp,jpegxl --disable-encoder=apng,exr --disable-bsf=dovi_rpu
fi

export TMPDIR="$work/tmp"
export PKG_CONFIG_LIBDIR="$libass/lib/pkgconfig:$deps/lib/pkgconfig:/usr/lib/pkgconfig"
test "$(/usr/bin/pkgconf --path libass)" = "$libass/lib/pkgconfig/libass.pc"
test "$(/usr/bin/pkgconf --path dav1d)" = "$deps/lib/pkgconfig/dav1d.pc"
test "$(/usr/bin/pkgconf --modversion dav1d)" = '1.5.4'
"$inputs/nasm" -v | grep -Eq '^NASM version 3\.02([[:space:]]|$)'
# Se invoca con shell explícita para mantener el contrato de permisos del TAR.
/bin/sh ./configure --prefix="$prefix" --libdir="$prefix/lib" \
    --extra-version="$extra_version" \
    --disable-autodetect --enable-shared --disable-static --disable-ffplay \
    --disable-doc --disable-debug --disable-version-tracking --enable-gpl --enable-version3 \
    --disable-network --disable-protocols --enable-protocol=file,pipe \
    --enable-libass --enable-libx264 --enable-libmp3lame --enable-libdav1d \
    --disable-decoder=av1 --enable-zlib \
    --x86asmexe="$inputs/nasm" --pkg-config=/usr/bin/pkgconf --cc=/usr/bin/gcc \
    --extra-cflags="-I$deps/include" \
    --extra-ldflags="-L$deps/lib -Wl,-rpath-link,$deps/lib" "$@"
for feature in ASS_FILTER LAVFI_INDEV LIBDAV1D_DECODER LIBX264_ENCODER AAC_ENCODER \
    FILE_PROTOCOL PIPE_PROTOCOL MOV_DEMUXER MOV_MUXER PNG_ENCODER CONCAT_DEMUXER CONCAT_FILTER; do
    grep -Fx "#define CONFIG_$feature 1" config_components.h
done
if test "$extra_version" = zou.2; then
    for feature in FLIC_DECODER FLIC_DEMUXER HUFFYUV_DECODER FFVHUFF_DECODER IMC_DECODER IAC_DECODER \
        CLJR_DECODER VBLE_DECODER ADPCM_G726_DECODER ADPCM_G726LE_DECODER G2M_DECODER WEBP_DECODER \
        WEBP_ANIM_DECODER IMAGE_WEBP_PIPE_DEMUXER IMAGE_JPEGXL_PIPE_DEMUXER JPEGXL_ANIM_DEMUXER WEBP_PARSER \
        JPEGXL_PARSER APNG_ENCODER EXR_ENCODER DOVI_RPU_BSF; do
        grep -Fx "#define CONFIG_$feature 0" config_components.h
    done
fi
grep -Fx '#define CONFIG_NETWORK 0' config.h
sha256sum config.h config_components.h ffbuild/config.mak > "$products/configure.sha256"
/usr/bin/make -j2
/usr/bin/make -j2 DESTDIR="$work/stage" install
staged="$work/stage$prefix"
for soname in libavcodec.so.63 libavformat.so.63 libavdevice.so.63 libavfilter.so.12 \
    libavutil.so.61 libswscale.so.10 libswresample.so.7; do
    real=$(readlink -f "$staged/lib/$soname")
    test "$real" = "$staged/lib/$soname.1.102"
    /usr/bin/readelf -d "$real" > "$products/$soname.dynamic"
    grep -F "Library soname: [$soname]" "$products/$soname.dynamic"
    cp "$real" "$products/$soname"
done
for program in ffmpeg ffprobe; do
    /usr/bin/readelf -d "$staged/bin/$program" > "$products/$program.dynamic"
    LD_LIBRARY_PATH="$staged/lib:$deps/lib" "$staged/bin/$program" -version > "$products/$program.version"
    version=$(awk 'NR == 1 { print $1 " " $2 " " $3 }' "$products/$program.version")
    test "$version" = "$program version $revision"
    cp "$staged/bin/$program" "$products/$program"
done
if grep -Eh 'TEXTREL|\((RUNPATH|RPATH)\)|Shared library: \[(libass\.so|libssl\.so|libcrypto\.so)' "$products"/*.dynamic; then exit 1; fi
LD_LIBRARY_PATH="$staged/lib:$deps/lib" "$staged/bin/ffmpeg" -hide_banner -protocols > "$products/protocols.txt"
awk '/^  / { print $1 }' "$products/protocols.txt" | sort -u > "$products/protocols.actual"
printf '%s\n' file pipe > "$work/protocols.expected"
cmp "$work/protocols.expected" "$products/protocols.actual"
(cd "$products" && sha256sum ffmpeg ffprobe libavcodec.so.63 libavformat.so.63 \
    libavdevice.so.63 libavfilter.so.12 libavutil.so.61 libswscale.so.10 libswresample.so.7 > SHA256SUMS)
