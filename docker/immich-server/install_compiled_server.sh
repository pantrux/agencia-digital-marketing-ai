#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

cd /immich-payload
sha256sum -c PAYLOAD.sha256
sha256sum -c SOURCE-MANIFEST.sha256
server=/usr/src/app/server
plugin_expected="$server/node_modules/.pnpm/@immich+plugin-sdk@file++++usr+src+app+packages+plugin-sdk/node_modules/@immich/plugin-sdk"
plugin=$(readlink -f -- "$server/node_modules/@immich/plugin-sdk")
test "$plugin" = "$plugin_expected"
test -d "$server/dist" && test ! -L "$server/dist"
test -d "$plugin/dist" && test ! -L "$plugin/dist"
test -f "$server/dist/main.js"
test -x "$server/bin/start.sh" && test -x "$server/bin/immich-healthcheck"
test -f "$server/helmet.json" && test -d /build/www
test -d /build/plugins/immich-plugin-core/dist && test -d /usr/src/app/cli
test "$(node --version)" = 'v24.22.1'
printf '%s  %s\n' \
  '800875439c4fd4bfe5e08745b932696d3f4aba8af4c966e0c6c2580cf7e98a0b' "$server/bin/start.sh" \
  'd2a5761be3ce41b096ee29a504d1dd854b83db8c5a3362385df0ce3918984639' "$server/bin/immich-healthcheck" \
  | sha256sum -c -

# Sustituir los dos árboles generados para que la base no oculte archivos faltantes.
rm -rf -- "$server/dist" "$plugin/dist"
install -d -m 0755 "$server/dist" "$plugin/dist"
cp -R --no-preserve=ownership -- ./server/dist/. "$server/dist/"
cp -R --no-preserve=ownership -- ./packages/plugin-sdk/dist/. "$plugin/dist/"
# El contexto TAR omite entradas de directorio y BuildKit puede crearlas con modo 0700.
find "$server/dist" "$plugin/dist" -type d -exec chmod 0755 {} +
sed "s|  server/dist/|  $server/dist/|; s|  packages/plugin-sdk/dist/|  $plugin/dist/|" \
  RUNTIME.sha256 | sha256sum --quiet -c -
test "$(find "$server/dist" -type f | wc -l)" -eq 1378
test "$(find "$plugin/dist" -type f | wc -l)" -eq 7
node --check "$server/dist/main.js"
