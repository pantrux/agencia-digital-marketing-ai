#!/bin/sh
# Instala la clausura exacta de Node desde el repositorio local sellado.
set -efu
export LC_ALL=C
umask 022
cd /sdk-inputs
printf '%s  %s\n' '26bc56cae8f1eee5afb769c89c643947d83430fd2273d98bebb6e2dfc41dbdd7' INPUTS.sha256 |
    sha256sum -c -
sha256sum -c INPUTS.sha256

inventory() {
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
    ' /usr/lib/apk/db/installed > /tmp/frontend-inventory
    sort /tmp/frontend-inventory
}

inventory > /tmp/frontend-before
sort apk-base.lock > /tmp/frontend-base
cmp -s /tmp/frontend-before /tmp/frontend-base
set -- c-ares=1.34.9-r0 icu79-data-full=79.1-r1 libicu79=79.1-r1 \
    libnghttp2-14=1.71.0-r0 libstdc++=16.2.0-r2 libuv=1.53.0-r0 \
    nodejs-24-minimal=24.22.2-r0
/sbin/apk --no-network --no-scripts --no-commit-hooks --no-cache \
    --repositories-file /sdk-inputs/repositories --keys-dir /sdk-inputs/keys add "$@"
inventory > /tmp/frontend-after
{ cat apk-base.lock; printf '%s\n' "$@"; } | sort > /tmp/frontend-expected
cmp -s /tmp/frontend-after /tmp/frontend-expected
test "$(wc -l < /tmp/frontend-after)" -eq 26
/usr/bin/ldconfig
printf '%s  /usr/bin/node\n' 'c9f0030334c1d8f0d84e57ce49ce5afe1381126b14065f279e7e369248e04bb0' |
    sha256sum -c -
test "$(/usr/bin/node --version)" = v24.22.2
printf '%s\n' 'SUPOCLIP_FRONTEND_RUNTIME_26_APK_VERIFICADO'
