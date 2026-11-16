#!/bin/sh
# Ejecutar en el SDK Linux fijado, sin red y con /work propio de esta corrida.
set -eu
umask 077

workspace=/work/workspace
emcc=/emsdk/upstream/emscripten/emcc
node=/emsdk/node/22.16.0_64bit/bin/node
config=/emsdk/.emscripten
tool_path=/work/quickjs-bin
esbuild=$workspace/node_modules/@esbuild/linux-x64/bin/esbuild
variants='debug-asyncify debug-sync release-asyncify release-sync'

test "$(/usr/bin/id -u)" = 1000
test "$(/usr/bin/id -g)" = 1000
test "$(/usr/bin/realpath "$workspace")" = "$workspace"
test -x "$emcc" && test -x "$node" && test -r "$config" && test -x "$esbuild"
cd "$workspace"
/usr/bin/sha256sum --strict --quiet -c SHA256SUMS
for variant in $variants; do
    project=$workspace/packages/variant-quickjs-wasmfile-$variant
    test -f "$project/Makefile" && test ! -L "$project/Makefile"
    test ! -e "$project/build" && test ! -L "$project/build"
    test ! -e "$project/dist" && test ! -L "$project/dist"
done

/usr/bin/mkdir "$tool_path" /work/quickjs-cache /work/quickjs-home /work/quickjs-tmp
for tool in basename cat cp dirname env grep java mkdir python3 readlink sed uname; do
    test -x "/usr/bin/$tool"
    /usr/bin/ln -s "/usr/bin/$tool" "$tool_path/$tool"
done

clean() {
    /usr/bin/env -i PATH="$tool_path" HOME=/work/quickjs-home TMPDIR=/work/quickjs-tmp \
        LC_ALL=C TZ=UTC PYTHONDONTWRITEBYTECODE=1 EM_CONFIG="$config" \
        EM_CACHE=/work/quickjs-cache ESBUILD_BINARY_PATH="$esbuild" "$@"
}

test "$(clean "$node" --version)" = v22.16.0
test "$(clean "$esbuild" --version)" = 0.18.20
version=$(clean "$emcc" --version)
printf '%s\n' "$version" | /usr/bin/grep -Eq ' 5\.0\.1([[:space:]]|$)'
closure=$(clean "$node" --print 'require("/emsdk/upstream/emscripten/node_modules/google-closure-compiler/package.json").version')
test "$closure" = 20240317.0.0

for variant in $variants; do
    project=$workspace/packages/variant-quickjs-wasmfile-$variant
    # La expansión de Make conserva ASYNCIFY/DEBUG en el generador original.
    # shellcheck disable=SC2016
    clean /usr/bin/make -C "$project" -j1 SHELL=/bin/sh "EMCC=$emcc" \
        'GENERATE_TS=$(GENERATE_TS_ENV) /emsdk/node/22.16.0_64bit/bin/node ../../node_modules/tsx/dist/cli.mjs ../../scripts/generate.ts' \
        BROWSER MJS CJS CLOUDFLARE
    for module in emscripten-module.wasm emscripten-module.mjs emscripten-module.cjs \
        emscripten-module.browser.mjs emscripten-module.cloudflare.cjs; do
        file=$project/dist/$module
        test -s "$file" && test ! -L "$file"
        /usr/bin/sha256sum "$file"
    done
done
