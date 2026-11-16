#!/usr/bin/bash
# Genera la prueba sintética para que el host compruebe los JSON de FFprobe.
set -euo pipefail
set -C
umask 077
export PATH=/usr/bin:/bin LC_ALL=C

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

test "$#" -eq 2 || fail BINARIO_O_PIN
test "$(id -u)" = 1000 && test "$(pwd -P)" = /outputs || fail SDK_UID_O_SALIDA
ffmpeg=/built/ffmpeg
ffprobe=/built/ffprobe
font=/fixture-font.ttf
font_name=LiberationSans-Regular.ttf
font_sha=559f38bdfcf12beead8a744579715dc7249d6978616e757de7d8cf06403aa531

verify() {
    local path=$1 pin=$2 limit=$3 bytes
    [[ $pin =~ ^[a-f0-9]{64}$ ]] || fail BINARIO_O_PIN
    test -f "$path" && test ! -L "$path" || fail BINARIO_IRREGULAR
    bytes=$(wc -c < "$path")
    test "$bytes" -gt 0 && test "$bytes" -le "$limit" || fail BINARIO_IRREGULAR
    printf '%s  %s\n' "$pin" "$path" | sha256sum -c - >/dev/null || fail BINARIO_O_FUENTE_SHA
}

verify "$ffmpeg" "$1" 268435456
verify "$ffprobe" "$2" 268435456
verify "$font" "$font_sha" 4194304
mkdir ass-probe
cd ass-probe

run() {
    local code=$1 out=$2 err=$3
    shift 3
    if ! (ulimit -f 1024; timeout -k 2s 20s env -i LC_ALL=C "$@") </dev/null >| "$out" 2>| "$err"; then
        fail "$code"
    fi
    test "$(( $(wc -c < "$out") + $(wc -c < "$err") ))" -le 1048576 || fail FFMPEG_SALIDA_EXCESIVA
}

run FILTROS_FALLO filters.txt diagnostic.stderr "$ffmpeg" -hide_banner -filters
grep -Eq '^[[:space:]]*[.TSC]{2,3}[[:space:]]+subtitles[[:space:]]+V->V([[:space:]]|$)' filters.txt || fail SIN_FILTRO_ASS
cat "$font" > "$font_name"
cat > captions.ass <<'EOF'
[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Liberation Sans,26,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,10,10,12,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,0:00:00.25,0:00:00.90,Default,,0,0,0,,Cámara y acción
EOF

run QUEMADO_ASS_FALLO diagnostic.stdout render.stderr "$ffmpeg" \
    -hide_banner -loglevel info -nostdin -n \
    -f lavfi -i 'color=c=black:s=320x180:r=12:d=1.5' \
    -f lavfi -i 'sine=frequency=440:sample_rate=48000:duration=1.5' \
    -filter_threads 1 -vf 'subtitles=filename=captions.ass:fontsdir=.' \
    -c:v libx264 -threads 1 -pix_fmt yuv420p -c:a aac -shortest clip.mp4
if ! grep -Fq 'libass API version' render.stderr || ! grep -Fq "$font_name" render.stderr; then
    fail LIBASS_FUENTE_NO_ACREDITADA
fi

for instant in 0.0 0.5 1.1; do
    run DECODIFICACION_FALLO "frame-$instant.gray" diagnostic.stderr "$ffmpeg" \
        -v error -ss "$instant" -i clip.mp4 -frames:v 1 -an -threads 1 -pix_fmt gray -f rawvideo pipe:1
    test "$(wc -c < "frame-$instant.gray")" -eq 57600 || fail TAMANO_DE_CUADRO
done

pixels() {
    od -An -v -tu1 "$1" | awk '{ for (i = 1; i <= NF; i++) if ($i > 32) total++ } END { print total + 0 }'
}

before=$(pixels frame-0.0.gray)
during=$(pixels frame-0.5.gray)
after=$(pixels frame-1.1.gray)
test "$before" -eq 0 && test "$during" -ge 100 && test "$after" -eq 0 || fail ASS_NO_VISIBLE
printf '{"0.0":%s,"0.5":%s,"1.1":%s}\n' "$before" "$during" "$after" > visible-pixels.json

streams=stream=codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels
run FFPROBE_FALLO clip.streams.json diagnostic.stderr "$ffprobe" -v error -show_entries "$streams" -of json clip.mp4
run EXTRACCION_MP3_FALLO diagnostic.stdout diagnostic.stderr "$ffmpeg" \
    -v error -nostdin -n -i clip.mp4 -vn -ac 1 -ar 16000 -b:a 64k -c:a libmp3lame audio.mp3
run FFPROBE_FALLO audio.streams.json diagnostic.stderr "$ffprobe" -v error -show_entries "$streams" -of json audio.mp3
run VERSION_FALLO ffmpeg-version.txt diagnostic.stderr "$ffmpeg" -version
test -s ffmpeg-version.txt || fail VERSION_FALLO
rm -- diagnostic.stdout diagnostic.stderr
sha256sum "$font_name" captions.ass filters.txt render.stderr clip.mp4 \
    frame-0.0.gray frame-0.5.gray frame-1.1.gray visible-pixels.json clip.streams.json \
    audio.mp3 audio.streams.json ffmpeg-version.txt > SHA256SUMS
printf '%s\n' 'ASS_CUADROS_VERIFICADOS_Y_STREAMS_PENDIENTES_DEL_HOST'
