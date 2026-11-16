#!/bin/sh
# Instala en un SDK aislado los intérpretes exactos para hidratar el almacén npm.
set -eu
umask 022
export PATH=/usr/bin:/bin LC_ALL=C

test "$#" -eq 1
test "$(id -u):$(id -g)" = 0:0
test "$(uname -s):$(uname -m)" = Linux:x86_64
test "$(printf '%s\n' /sys/class/net/*)" = /sys/class/net/lo
awk '$5 == "/sdk-inputs" && $6 ~ /(^|,)ro(,|$)/ {ok=1} END {exit !ok}' /proc/self/mountinfo
cd /sdk-inputs
printf '%s  INPUTS.sha256\n' "$1" | sha256sum -c -
sha256sum -c INPUTS.sha256

apk_inventory() {
    awk '
        /^P:/ { p=substr($0,3) }
        /^V:/ { v=substr($0,3) }
        /^$/ && p != "" && v != "" { print p "=" v; p=v="" }
        END { if (p != "" && v != "") print p "=" v }
    ' /usr/lib/apk/db/installed | sort
}

test "$(apk_inventory)" = "$(sort apk-base.lock)"
set --
while IFS= read -r package; do set -- "$@" "$package"; done < apk-add.lock
test "$#" -eq 20
/sbin/apk --no-network --no-scripts --no-commit-hooks --no-cache \
    --repositories-file /sdk-inputs/repositories --keys-dir /sdk-inputs/keys add "$@"
test "$(apk_inventory)" = "$(sort apk-final.lock)"
/usr/bin/ldconfig
printf '%s  /usr/bin/node\n' 'c9f0030334c1d8f0d84e57ce49ce5afe1381126b14065f279e7e369248e04bb0' | sha256sum -c -
test "$(/usr/bin/node --version)" = v24.22.2
/usr/bin/python3.12 -I -S -B -c 'import os, sqlite3, sys; assert sys.version_info[:3] == (3, 12, 16) and os.confstr("CS_GNU_LIBC_VERSION").startswith("glibc "); assert sqlite3.connect(":memory:").execute("select 1").fetchone() == (1,)'
printf '%s\n' 'IMMICH_JS_SEED_SDK_39_APK_VERIFICADO'
