#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive

cd /immich-debs
sha256sum --quiet -c PAYLOAD.sha256
printf '%s  %s\n' \
  'aefc2961708f62374f37150dae31c8e86bc8a2cde7c12538673d2caf24295aca' /var/lib/dpkg/status \
  | sha256sum -c -

inventory() {
  dpkg-query -W -f='${Package}|${Architecture}|${Version}|${db:Status-Status}\n' | sort
}

test "$(inventory)" = "$(cat BEFORE.lock)"
test "$(node --version)" = v24.22.1
dpkg --unpack -- ./*.deb
dpkg --configure -- apt libapt-pkg7.0 libbrotli1 libopenexr-3-1-30 zlib1g
test "$(inventory)" = "$(cat AFTER.lock)"
test -z "$(dpkg --audit)"
test "$(node --version)" = v24.22.1

# Confirm the OpenEXR ABI is present and the pre-existing media tool runs.
ldconfig -p | grep -F 'libOpenEXR-3_1.so.30 ' >/dev/null
ffmpeg -version >/dev/null
ffprobe -version >/dev/null
