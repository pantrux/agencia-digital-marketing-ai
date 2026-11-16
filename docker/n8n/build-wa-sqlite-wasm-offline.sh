#!/bin/sh
# Ejecutar en el SDK Linux fijado, sin red y con /work nuevo de UID/GID 1000.
set -eu
umask 077

workspace=/work/wa-sqlite
emroot=/emsdk/upstream/emscripten
emcc=$emroot/emcc
node=/emsdk/node/22.16.0_64bit/bin/node
config=/emsdk/.emscripten
tool_path=/work/wa-sqlite-bin

test "$#" -eq 0
test "$(/usr/bin/id -u)" = 1000
test "$(/usr/bin/id -g)" = 1000
test "$(/usr/bin/realpath "$workspace")" = "$workspace"
test -x "$emcc" && test -x "$node" && test -f "$config"
# emcc --version usa Git si encuentra .git junto a su fuente.
test ! -e "$emroot/.git" && test ! -L "$emroot/.git"
cd "$workspace"
test -f SHA256SUMS && test ! -L SHA256SUMS
printf '%s\n' '6cefd1eacb3f3c8fa6b202ed9ff5569c94130ece82d3e9f64eb7e496a93defb1  SHA256SUMS' \
    | /usr/bin/sha256sum --strict --quiet -c -
/usr/bin/sha256sum --strict --quiet -c SHA256SUMS
for target in tmp dist cache; do
    test ! -e "$target" && test ! -L "$target"
done

/usr/bin/mkdir "$tool_path" /work/wa-sqlite-home /work/wa-sqlite-cache /work/wa-sqlite-tmp
for tool in basename cat cp dirname env grep mkdir python3 readlink sed uname; do
    test -x "/usr/bin/$tool"
    /usr/bin/ln -s "/usr/bin/$tool" "$tool_path/$tool"
done

clean() {
    /usr/bin/env -i PATH="$tool_path" HOME=/work/wa-sqlite-home TMPDIR=/work/wa-sqlite-tmp \
        LC_ALL=C TZ=UTC PYTHONDONTWRITEBYTECODE=1 EM_CONFIG="$config" \
        EM_CACHE=/work/wa-sqlite-cache "$@"
}

test "$(clean "$node" --version)" = v22.16.0
version=$(clean "$emcc" --version)
printf '%s\n' "$version" | /usr/bin/grep -Eq ' 5\.0\.1([[:space:]]|$)'
clean /usr/bin/make -C "$workspace" -j1 SHELL=/bin/sh \
    -o deps/version-3.54.1/sqlite3.c -o deps/version-3.54.1/sqlite3.h \
    -o deps/extension-functions.c SQLITE_VERSION=version-3.54.1 "EMCC=$emcc" dist

for variant in wa-sqlite wa-sqlite-async wa-sqlite-jspi; do
    for ext in mjs wasm; do
        file=dist/$variant.$ext
        test -f "$file" && test -s "$file" && test ! -L "$file"
        /usr/bin/sha256sum "$file"
    done
    clean "$node" --check "dist/$variant.mjs"
done
