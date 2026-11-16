#!/bin/sh
# Proyecta sólo los APK de runtime antes de instalar los productos nativos.
set -eu
export LC_ALL=C
umask 022

fail() { printf '%s\n' "$1" >&2; exit 1; }
test "$#" = 1 && printf '%s\n' "$1" | grep -Eq '^[a-f0-9]{64}$' || fail "Falta la huella del wheel."
wheel_pin=$1

for mount in /inputs /products; do
    awk -v target="$mount" '$5 == target && $6 ~ /(^|,)ro(,|$)/ { found=1 } END { exit !found }' \
        /proc/self/mountinfo || fail "El contexto $mount debe estar montado RO."
done
cd /inputs
printf '%s  %s\n' \
    e850b85f8e00ef7727bdc5a01c2eb836ce997d27a85ac12d884ae38c8d8f8c71 INPUTS.sha256 \
    | sha256sum -c -
sha256sum -c INPUTS.sha256 > /tmp/supoclip-inputs.log

apk_state() {
    expected=$1
    count=$2
    test "$(wc -l < "$expected")" -eq "$count" || fail "Cantidad APK fijada inválida."
    awk '
        function row() {
            if (name == "" && version == "") return
            if (name == "" || version == "") exit 1
            print name "=" version
            name = version = ""
        }
        /^P:/ { if (name != "") exit 1; name = substr($0, 3) }
        /^V:/ { if (version != "") exit 1; version = substr($0, 3) }
        /^$/ { row() }
        END { row() }
    ' /usr/lib/apk/db/installed > /tmp/supoclip-apk.actual
    sort /tmp/supoclip-apk.actual > /tmp/supoclip-apk.actual.sorted
    sort "$expected" > /tmp/supoclip-apk.expected.sorted
    cmp -s /tmp/supoclip-apk.actual.sorted /tmp/supoclip-apk.expected.sorted \
        || fail "Inventario APK instalado distinto del lock."
}

for kind in add final; do
    awk '!/^ffmpeg-7(-[^=]+)?=7\.1\.5-r8$/' "apk-$kind.lock" > "/tmp/apk-$kind-v2.lock"
done
printf '%s  %s\n' \
    d32da9aaee336a4a5ca442fbff2b76e880f0d21712fb6ade1d814891c4fc0edc /tmp/apk-add-v2.lock \
    a21aa2cafc3f72bdfd5b7761d5275108a4ccfc8cdee2f23d9b9e035fcea2f2c2 /tmp/apk-final-v2.lock \
    | sha256sum -c -
apk_state /inputs/apk-base.lock 19
repo=/tmp/supoclip-apk-v2
mkdir -m 0755 "$repo" "$repo/x86_64"
ln -s /inputs/repo/x86_64/APKINDEX.tar.gz "$repo/x86_64/APKINDEX.tar.gz"
printf '%s\n' "$repo" > /tmp/supoclip-repositories-v2
set --
while IFS= read -r package; do
    filename="${package%%=*}-${package#*=}.apk"
    source="/inputs/repo/x86_64/$filename"
    test -f "$source" && test ! -L "$source" || fail "APK fijado ausente o irregular."
    ln -s "$source" "$repo/x86_64/$filename"
    set -- "$@" "$package"
done < /tmp/apk-add-v2.lock
test "$#" = 320 || fail "Cantidad de APK de runtime inválida."
/sbin/apk --no-network --no-scripts --no-commit-hooks --no-cache \
    --repositories-file /tmp/supoclip-repositories-v2 --keys-dir /inputs/keys add "$@"
apk_state /tmp/apk-final-v2.lock 339
/usr/bin/ldconfig
printf '%s\n' 'SUPOCLIP_APK_339_VERIFIED'
exec /usr/bin/python3.12 -I -S -B /opt/supoclip/bin/backend_runtime_v2.py install "$wheel_pin"
