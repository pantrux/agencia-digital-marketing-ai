#!/bin/sh
# Construye sólo el runner JS con el lock y el store previamente acreditados.
set -eu
set -C
umask 077
export PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C

fail() { printf '%s\n' "$1" >&2; exit 1; }

test "$#" -eq 0 || fail 'La etapa de construcción no recibe argumentos.'
test "$(id -u):$(id -g)" = 1000:1000 && test -f /lib/ld-musl-x86_64.so.1 || fail 'Se requiere UID/GID 1000 en Linux musl.'
set -- /sys/class/net/*
test "$#" -eq 1 && test "$1" = /sys/class/net/lo || fail 'Se requiere un contenedor sin red exterior.'
if command -v git >/dev/null 2>&1; then fail 'El entorno incluye Git.'; fi

workspace=/work/workspace
output=/work/task-runner-javascript
cache=/work/cache
pnpm=/inputs/tool/pnpm
node=/usr/local/bin/node
lock_sha=78a98fe18321e18e02160f7af0efaf2b30edd16b78f993e4771cb3935cb5f0ab
pnpm_sha=6c40f39d8af39c0011de4718f750d2a3d1a5eba5b89aba5178c597e79028c149
index_sha=a4936c77114841726d400be9c0607c333e6b1c62043b973f4e3db66fd5987b64

pin() { printf '%s  %s\n' "$1" "$2" | sha256sum -c - >/dev/null; }
check_inputs() {
    test "$(realpath "$workspace")" = "$workspace"
    test "$(realpath /store)" = /store
    test -d "$cache/pnpm/v11/metadata-full/https%3A+registry.npmjs.org"
    for suffix in -wal -shm -journal; do
        test ! -e "/store/v11/index.db$suffix"
        test ! -L "/store/v11/index.db$suffix"
    done
    awk '$5 == "/store" { if ($6 ~ /(^|,)ro(,|$)/) ro++; count++ } END { exit !(count == 1 && ro == 1) }' /proc/self/mountinfo
    pin "$pnpm_sha" "$pnpm"
    pin "$lock_sha" "$workspace/pnpm-lock.yaml"
    pin 465e2f9946938e6cb36da454f83dff7baf2b7faa2afbd41cc2146cf97050c137 "$workspace/pnpm-workspace.yaml"
    pin "$index_sha" /store/v11/index.db
}

check_inputs
# El supervisor exige exit 0 y N8N_JS_INSTALL_OFFLINE_VERIFICADO en el log del instalador.
test -d "$workspace/node_modules/.pnpm"
test -d "$workspace/packages/@n8n/task-runner/node_modules"
test ! -e "$output" && test ! -L "$output" || fail 'El destino de deploy ya existe.'
for path in /work/home /work/tmp /work/state; do
    test -d "$path" && test ! -L "$path" || fail 'Faltan las rutas privadas del instalador.'
done

clean() {
    env -i PATH="$PATH" LC_ALL=C HOME=/work/home XDG_CACHE_HOME="$cache" \
        XDG_STATE_HOME=/work/state TMPDIR=/work/tmp CI=1 TZ=UTC LIBC=musl GOMAXPROCS=2 "$@"
}

clean "$node" -e 'const a=require("node:assert/strict"); a.equal(process.version,"v26.12.1"); a.equal(process.arch,"x64"); a.equal(process.versions.modules,"147"); a.equal(process.report.getReport().header.glibcVersionRuntime,undefined);'
test "$(clean "$pnpm" --version)" = 12.4.2
cd "$workspace"

# Las rutas se resuelven desde los importers que declaran cada herramienta.
paths=$(clean "$node" <<'JS'
const a = require('node:assert/strict');
const { readFileSync, realpathSync } = require('node:fs');
const { dirname, join } = require('node:path');
const { createRequire } = require('node:module');
const root = process.cwd();
const at = (dir) => createRequire(join(dir, 'package.json'));
const packagePath = (from, specifier, version) => {
  const file = at(from).resolve(`${specifier}/package.json`);
  a.equal(JSON.parse(readFileSync(file, 'utf8')).version, version);
  return dirname(file);
};
const ts = packagePath(join(root, 'packages/@n8n/task-runner'), 'typescript', '7.0.2');
const tsc = join(packagePath(ts, '@typescript/typescript-linux-x64', '7.0.2'), 'lib/tsc');
const es = packagePath(join(root, 'packages/@n8n/expression-runtime'), 'esbuild', '0.28.1');
const esbuild = at(es).resolve('@esbuild/linux-x64/bin/esbuild');
const td = packagePath(join(root, 'packages/@n8n/utils'), 'tsdown', '0.22.8');
const rd = packagePath(td, 'rolldown', '1.1.5');
const rolldown = at(rd).resolve('@rolldown/binding-linux-x64-musl');
const alias = join(packagePath(root, 'tsc-alias', '1.8.10'), 'dist/bin/index.js');
for (const file of [tsc, esbuild, join(td, 'dist/run.mjs'), rolldown, alias]) {
  const resolved = realpathSync(file);
  a.ok(resolved.startsWith(`${root}/node_modules/.pnpm/`));
  console.log(resolved);
}
JS
)
{
    IFS= read -r tsc
    IFS= read -r esbuild
    IFS= read -r tsdown
    IFS= read -r rolldown
    IFS= read -r alias
    if IFS= read -r extra; then fail "Selección de herramientas con una línea adicional de ${#extra} caracteres."; fi
} <<EOF
$paths
EOF
pin 4f2de678286401759b3fb4475bafe35b8f32b4b3a07d92642bbf37eadc9b34a4 "$tsc"
pin 0c6588b092a2c291a72bab90659f3c9e0e25e0fe59c9ac12b4dae4d945e5548c "$esbuild"
pin 86bcac6023952131364d45bce9d4e3312b733044e8567f81d90ffe7587898797 "$rolldown"
test "$(clean "$tsc" --version)" = 'Version 7.0.2'
test "$(clean "$esbuild" --version)" = 0.28.1

in_project() (
    cd "$workspace/$1"
    shift
    clean "$@"
)

compile() {
    printf 'COMPILANDO %s\n' "$1"
    in_project "$1" "$tsc" -p tsconfig.build.json
}

for name in constants di errors permissions tournament config; do compile "packages/@n8n/$name"; done
in_project packages/@n8n/utils "NAPI_RS_NATIVE_LIBRARY_PATH=$rolldown" "$node" "$tsdown"
in_project packages/@n8n/expression-runtime "$tsc" --build tsconfig.build.esm.json tsconfig.build.cjs.json
in_project packages/@n8n/expression-runtime "ESBUILD_BINARY_PATH=$esbuild" "$node" esbuild.config.js
in_project packages/workflow "$tsc" --build tsconfig.build.esm.json tsconfig.build.cjs.json
for name in inbound-auth workflow-sdk api-types decorators backend-common backend-network blob-storage client-oauth2; do
    compile "packages/@n8n/$name"
done
for project in packages/core packages/@n8n/task-runner; do
    compile "$project"
    in_project "$project" "$node" "$alias" -p tsconfig.build.json
done

test -s packages/@n8n/task-runner/dist/start.js
test -s packages/@n8n/expression-runtime/dist/bundle/runtime.iife.js
clean "$pnpm" --dir "$workspace" --filter=@n8n/task-runner deploy "$output" --prod --no-optional \
    --offline --frozen-lockfile --ignore-scripts --config.frozen-store=true \
    --config.package-import-method=copy --config.ignore-pnpmfile=true \
    --config.manage-package-manager-versions=false --config.git-branch-lockfile=false \
    --config.verify-store-integrity=true --config.fetch-retries=0 \
    --registry https://registry.npmjs.org/ --store-dir /store --reporter=append-only
check_inputs
test -s "$output/dist/start.js"
test -s "$output/pnpm-lock.yaml"
for notice in LICENSE.md LICENSE_EE.md; do
    if test -e "$output/$notice" || test -L "$output/$notice"; then
        test ! -L "$output/$notice"
        cmp -s "$workspace/$notice" "$output/$notice"
    else
        cat "$workspace/$notice" > "$output/$notice"
    fi
done

cd "$output"
clean "$node" --check dist/start.js
clean "$node" --no-node-snapshot <<'JS'
const a = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readFileSync, readdirSync, realpathSync } = require('node:fs');
const { createRequire } = require('node:module');
const { basename, dirname, join } = require('node:path');
const root = process.cwd();
const fromRunner = createRequire(join(root, 'package.json'));
const fromWorkflow = createRequire(fromRunner.resolve('n8n-workflow'));
const fromExpression = createRequire(fromWorkflow.resolve('@n8n/expression-runtime'));
const entry = fromExpression.resolve('isolated-vm');
const native = createRequire(entry)('node-gyp-build').path(dirname(entry));
a.equal(basename(native), 'isolated-vm.abi147.musl.node');
a.ok(realpathSync(native).startsWith(`${root}/`));
a.equal(createHash('sha256').update(readFileSync(native)).digest('hex'), '41dd9d63868631c534066b4626bb818f59a4fc7c1d58ff33b9e759c49feb8cb9');
const ivm = fromExpression('isolated-vm');
const isolate = new ivm.Isolate({ memoryLimit: 16 });
try { a.equal(isolate.createContextSync().evalSync('6 * 7'), 42); } finally { isolate.dispose(); }
a.equal(fromRunner('moment').version, '2.30.1');
for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
  if (entry.isSymbolicLink()) a.ok(realpathSync(join(entry.parentPath, entry.name)).startsWith(`${root}/`));
}
JS
sha256sum package.json pnpm-lock.yaml dist/start.js
printf '%s\n' 'N8N_JS_DEPLOY_OFFLINE_LISTO'
