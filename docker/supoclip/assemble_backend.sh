#!/bin/sh
# Instalación APK desde la proyección local fijada. Ejecutar dentro del build.
set -eu
export LC_ALL=C
umask 022

fail() { printf '%s\n' "$1" >&2; exit 1; }

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
        /^P:/ { if (name != "") exit 1; name=substr($0,3) }
        /^V:/ { if (version != "") exit 1; version=substr($0,3) }
        /^$/ { if (name != "" || version != "") {
            if (name == "" || version == "") exit 1
            print name "=" version
            name=version=""
        } }
        END { if (name != "" || version != "") {
            if (name == "" || version == "") exit 1
            print name "=" version
        } }
    ' /usr/lib/apk/db/installed > /tmp/supoclip-apk.actual
    sort /tmp/supoclip-apk.actual > /tmp/supoclip-apk.actual.sorted
    sort "$expected" > /tmp/supoclip-apk.expected.sorted
    cmp -s /tmp/supoclip-apk.actual.sorted /tmp/supoclip-apk.expected.sorted \
        || fail "Inventario APK instalado distinto del lock."
}

apk_state /inputs/apk-base.lock 19
test "$(wc -l < /inputs/apk-add.lock)" -eq 329 || fail "Cantidad de APK añadidos inválida."
set --
while IFS= read -r package; do
    set -- "$@" "$package"
done < /inputs/apk-add.lock
/sbin/apk --no-network --no-scripts --no-commit-hooks --no-cache \
    --repositories-file /inputs/repositories --keys-dir /inputs/keys add "$@"
apk_state /inputs/apk-final.lock 348
/usr/bin/ldconfig
printf '%s\n' 'SUPOCLIP_APK_348_VERIFIED'
exec /usr/bin/python3.12 -I -S -B /opt/supoclip/bin/backend_runtime.py install
