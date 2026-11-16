#!/bin/sh
set -eu

fail() {
    printf '%s\n' 'Entrada de arranque SupoClip inválida.' >&2
    exit 1
}

secret() {
    path="/run/secrets/supoclip-$1"
    [ -f "$path" ] && [ ! -L "$path" ] || fail
    case "$(stat -c '%u:%g:%a:%h' "$path")" in
        1000:1000:400:1|1000:1000:600:1) ;;
        *) fail ;;
    esac
    bytes="$(wc -c < "$path")"
    [ "$bytes" -gt 0 ] && [ "$bytes" -le 8192 ] || fail
    value="$(head -c 8193 "$path")" || fail
    [ -n "$value" ] || fail
    case "$value" in
        [[:space:]]*|*[[:space:]]|*[[:cntrl:]]*) fail ;;
    esac
    printf '%s' "$value"
}

[ "$#" -eq 1 ] || fail
[ "$(id -u):$(id -g)" = "1000:1000" ] || fail
case "$1" in frontend|migrate) ;; *) fail ;; esac
DATABASE_URL="$(secret database-node)" || fail
case "$DATABASE_URL" in postgresql://*|postgres://*) ;; *) fail ;; esac
export DATABASE_URL

case "$1" in
    frontend)
        BETTER_AUTH_SECRET="$(secret better-auth)" || fail
        BACKEND_AUTH_SECRET="$(secret backend-auth)" || fail
        export BETTER_AUTH_SECRET BACKEND_AUTH_SECRET
        exec /usr/bin/node /opt/supoclip/frontend/server.js
        ;;
    migrate)
        root=/run/supoclip/prisma
        native=/run/supoclip/prisma-bin
        cli="$root/node_modules/prisma/build/index.js"
        [ -f "$cli" ] && [ -f "$root/prisma/schema.prisma" ] &&
            [ -x "$native/schema-engine" ] &&
            [ -f "$native/libssl.so.3" ] && [ -f "$native/libcrypto.so.3" ] || fail
        PRISMA_SCHEMA_ENGINE_BINARY="$native/schema-engine"
        LD_LIBRARY_PATH="$native"
        CHECKPOINT_DISABLE=1
        export PRISMA_SCHEMA_ENGINE_BINARY LD_LIBRARY_PATH CHECKPOINT_DISABLE
        exec /usr/bin/node "$cli" migrate deploy --schema "$root/prisma/schema.prisma"
        ;;
esac
