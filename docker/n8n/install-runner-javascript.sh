#!/bin/sh
# Prepara e instala el workspace fijado desde el almacén Linux musl acreditado.
set -efu
umask 077
export PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C

test "$(id -u):$(id -g)" = 1000:1000
test -f /lib/ld-musl-x86_64.so.1
if command -v git >/dev/null 2>&1; then
    printf '%s\n' 'El entorno incluye Git.' >&2
    exit 1
fi
/usr/local/bin/node -e '
const { version, versions, platform, arch } = process;
if (version !== "v26.12.1" || versions.modules !== "147" || platform !== "linux" || arch !== "x64") process.exit(1);
console.log(JSON.stringify({version, abi: versions.modules, platform, arch}));
'

test -d /work && test ! -L /work
mkdir /work/workspace /work/cache /work/state /work/home /work/tmp
cp -R /inputs/workspace/. /work/workspace/
chmod -R u+rwX /work/workspace
cp /inputs/evidence/normalized-pnpm-lock.yaml /work/workspace/pnpm-lock.yaml
mkdir /work/cache/pnpm
cp -R /inputs/cache/v11 /work/cache/pnpm/
chmod -R u+rwX /work/cache/pnpm

check_lock() {
    printf '%s  %s\n' '78a98fe18321e18e02160f7af0efaf2b30edd16b78f993e4771cb3935cb5f0ab' \
        /work/workspace/pnpm-lock.yaml | sha256sum -c -
}

printf '%s  %s\n' '6c40f39d8af39c0011de4718f750d2a3d1a5eba5b89aba5178c597e79028c149' \
    /inputs/tool/pnpm | sha256sum -c -
check_lock
test "$(/inputs/tool/pnpm --version)" = '12.4.2'
env -i PATH="$PATH" LC_ALL=C HOME=/work/home XDG_CACHE_HOME=/work/cache \
    XDG_STATE_HOME=/work/state TMPDIR=/work/tmp CI=1 \
    /inputs/tool/pnpm --dir /work/workspace install --offline --frozen-lockfile --ignore-scripts \
    --config.frozen-store=true --config.package-import-method=copy \
    --config.ignore-pnpmfile=true --config.manage-package-manager-versions=false \
    --config.git-branch-lockfile=false --config.verify-store-integrity=true \
    --registry https://registry.npmjs.org/ --store-dir /store --reporter=append-only
check_lock
test -d /work/workspace/node_modules/.pnpm
test -d /work/workspace/packages/@n8n/task-runner/node_modules
printf '%s\n' 'N8N_JS_INSTALL_OFFLINE_VERIFICADO'
