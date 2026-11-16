#!/bin/sh
# Constructor temporal propio: sólo preparación del SDK y observación del grafo.
set -eu
umask 077

fallar() { printf '%s\n' "$1" >&2; exit 1; }
modo=${1:-}
case "$modo" in hold|sdk|graph) ;; *) fallar 'Modo no admitido; compilación aún deshabilitada.' ;; esac
[ "$#" -eq 1 ] || fallar 'Sólo se admite un modo fijo.'
[ "$(uname -s)" = Linux ] || fallar 'Sólo se admite Linux en el contenedor propio.'
[ "$(id -u)" = 0 ] || fallar 'La preparación requiere root dentro del contenedor aislado.'
[ -f /.dockerenv ] || fallar 'No se acredita el entorno contenedor.'
[ ! -S /var/run/docker.sock ] || fallar 'El constructor no admite socket Docker.'
[ "${AGENCY_BUILD_OWNER:-}" = 'agencia-digital-marketing-ai:postgres-immich-builder:v1' ] || fallar 'Propietario incorrecto.'
pin=${AGENCY_INPUT_MANIFEST_SHA256:-}
[ "${#pin}" -eq 64 ] || fallar 'Falta el pin respaldado del inventario.'
case "$pin" in *[!a-f0-9]*) fallar 'Pin de inventario inválido.' ;; esac
for ruta in /inputs /outputs /work; do
    [ -d "$ruta" ] && [ ! -L "$ruta" ] || fallar 'Directorio propio ausente o enlazado.'
done
[ "$(stat -c '%u:%a' /outputs)" = '0:700' ] || fallar 'La salida requiere root:0700.'
[ -f /outputs/.owner ] && [ ! -L /outputs/.owner ] || fallar 'Falta el marcador de salida.'
[ "$(stat -c '%u:%a:%h' /outputs/.owner)" = '0:600:1' ] || fallar 'El marcador requiere root:0600 sin enlaces.'
[ "$(cat /outputs/.owner)" = "$AGENCY_BUILD_OWNER" ] || fallar 'La salida pertenece a otro propietario.'
awk '$5 == "/inputs" && $6 ~ /(^|,)ro(,|$)/ {ok=1} END {exit !ok}' /proc/self/mountinfo || fallar 'Las entradas no están montadas en sólo lectura.'
[ -f /inputs/SHA256SUMS ] && [ ! -L /inputs/SHA256SUMS ] || fallar 'Inventario ausente o enlazado.'
[ "$(sha256sum /inputs/SHA256SUMS | cut -d ' ' -f 1)" = "$pin" ] || fallar 'Inventario diferente del respaldo.'
(cd /inputs && sha256sum -c SHA256SUMS) >/dev/null || fallar 'Una entrada no coincide con su hash.'
export PATH=/opt/agency-rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset RUSTC_WRAPPER RUSTC_WORKSPACE_WRAPPER RUNNER RUSTFLAGS CARGO_ENCODED_RUSTFLAGS
unset CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_RUNNER RUSTDOCFLAGS
if command -v git >/dev/null 2>&1; then fallar 'El constructor no admite un ejecutable Git.'; fi

if [ "$modo" = hold ]; then
    # El controlador también debe imponer un plazo externo y limpiar sólo su CID.
    trap 'exit 0' TERM INT
    segundos=7200
    while [ "$segundos" -gt 0 ]; do
        sleep 1
        segundos=$((segundos - 1))
    done
    exit 124
fi

export CARGO_HOME=/work/cargo-home
export CARGO_TARGET_DIR=/work/target
export CARGO_NET_OFFLINE=true
export CARGO_TERM_COLOR=never
export RUSTC_BOOTSTRAP=1
export PG_CONFIG=/usr/local/bin/pg_config
export PGRX_PG_CONFIG_PATH=/usr/local/bin/pg_config
export TARGET=x86_64-unknown-linux-musl
export PROFILE=release
export CC=clang-21
export CXX=clang++-21
export LIBSQLITE3_SYS_USE_PKG_CONFIG=1
export SQLITE3_LIB_DIR=/opt/agency-sqlite/lib
export SQLITE3_INCLUDE_DIR=/opt/agency-sqlite/include
export SQLITE3_STATIC=0

if [ "$modo" = sdk ]; then
    [ ! -e /outputs/sdk-receipt.json ] || fallar 'El recibo SDK ya existe; no se sobrescribe.'
    mkdir /work/.sdk-stage || fallar 'La etapa SDK ya se inició; no se reintenta parcialmente.'
    printf '%s  %s\n' 'af1be6395c8850e28ddc34c3de8839c47de7aee1611380fa75c380cb9f75a72f' /lib/apk/db/installed | sha256sum -c - >/dev/null
    cp /lib/apk/db/installed /work/base-installed.before
    set -- /inputs/apk/*.apk
    [ "$#" -eq 65 ] || fallar 'La clausura no contiene 65 APK exactos.'
    apk add --no-network --no-cache "$@"
    if command -v git >/dev/null 2>&1; then fallar 'La instalación incorporó Git.'; fi
    [ "$("$PG_CONFIG" --version)" = 'PostgreSQL 16.15' ] || fallar 'SDK PostgreSQL distinto.'
    mkdir /work/sdk-sources /work/source /work/vendor "$CARGO_HOME"
    for componente in rustc cargo rust-std; do
        tar -xJf "/inputs/rust/$componente.tar.xz" -C /work/sdk-sources --no-same-owner
        /bin/bash "/work/sdk-sources/$componente-beta-$TARGET/install.sh" --prefix=/opt/agency-rust --disable-ldconfig
    done
    tar -xzf /inputs/vectorchord-source.tar.gz -C /work/source --strip-components=1 --no-same-owner
    tar -xzf /inputs/vendor.tar.gz -C /work/vendor --no-same-owner
    python3 - <<'PY'
import hashlib, importlib.util, json, os, pathlib
base = pathlib.Path('/inputs')
spec = importlib.util.spec_from_file_location('recipe', base / 'offline_recipe.py')
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)
manifest = json.loads((base / 'musl-build-inputs.json').read_bytes())
lock = json.loads((base / 'native-sdk-lock.json').read_bytes())
before = pathlib.Path('/work/source/Cargo.lock').read_bytes()
if hashlib.sha256(before).hexdigest() != manifest['cargo']['lock_sha256']:
    raise ValueError('Cargo.lock no coincide con el respaldo.')
path = pathlib.Path('/work/source/crates/xtask/src/main.rs')
path.write_bytes(recipe.frozen_xtask(path.read_bytes()))
if recipe.checked_file(base / 'xtask-offline.patch')['sha256'] != recipe.PATCH_SHA:
    raise ValueError('Parche xtask distinto del revisado.')
# La comparación de versión de todos los paquetes base evita upgrades indirectos.
def records(text):
    result = {}
    for block in text.split('\n\n'):
        fields = dict(line.split(':', 1) for line in block.splitlines() if len(line) > 1 and line[1] == ':')
        if 'P' in fields:
            result[fields['P']] = fields
    return result
installed = records(pathlib.Path('/lib/apk/db/installed').read_text())
before_packages = records(pathlib.Path('/work/base-installed.before').read_text())
for name, item in before_packages.items():
    if installed.get(name, {}).get('V') != item['V']:
        raise ValueError('Un paquete de la base cambió de versión.')
for item in lock['packages']:
    if installed.get(item['name'], {}).get('V') != item['version']:
        raise ValueError('Un APK instalado difiere de la clausura.')
if set(installed) != set(before_packages) | {item['name'] for item in lock['packages']}:
    raise ValueError('La instalación incorporó paquetes ajenos a la clausura.')
PY
    mkdir /work/source/.cargo
    cat > /work/source/.cargo/config.toml <<'CONFIG'
[source.crates-io]
replace-with = "vendored-sources"
[source.vendored-sources]
directory = "/work/vendor"
[net]
offline = true
[unstable]
host-config = true
target-applies-to-host = true
[host]
rustflags = ["-Dwarnings", "-Ctarget-feature=-crt-static", "-Cpanic=unwind"]
[build]
rustflags = ["-Dwarnings", "-Ctarget-feature=-crt-static", "-Cpanic=unwind"]
CONFIG
    rustc --version > /work/rustc-version.txt
    cargo --version > /work/cargo-version.txt
    python3 - <<'PY'
import hashlib, json, os, pathlib
root = pathlib.Path('/work')
versions = {name: (root / (name + '-version.txt')).read_text().strip() for name in ('rustc', 'cargo')}
if not versions['rustc'].startswith('rustc 1.100.0-beta.10 (01ddda61f '):
    raise ValueError('La versión efectiva de rustc no coincide.')
if '72b77b8d9' not in versions['cargo'] or '2026-10-17' not in versions['cargo']:
    raise ValueError('El commit efectivo de Cargo no coincide con su paquete fijado.')
data = {'stage': 'sdk', 'verified': True, 'sdk_executed': True, 'compilation_performed': False,
        'versions': versions, 'input_manifest_sha256': os.environ['AGENCY_INPUT_MANIFEST_SHA256'],
        'cargo_lock_sha256': hashlib.sha256((root / 'source/Cargo.lock').read_bytes()).hexdigest(),
        'xtask_patched_sha256': hashlib.sha256((root / 'source/crates/xtask/src/main.rs').read_bytes()).hexdigest(),
        'compile_enabled': False}
with pathlib.Path('/outputs/sdk-receipt.json').open('x') as stream:
    json.dump(data, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
PY
    exit 0
fi

[ -f /outputs/sdk-receipt.json ] && [ ! -L /outputs/sdk-receipt.json ] || fallar 'Falta el recibo SDK.'
[ ! -e /outputs/graph-receipt.json ] || fallar 'El recibo del grafo ya existe.'
for ruta in /outputs/vchord-tree.txt /outputs/xtask-tree.txt /outputs/vchord-features.txt; do
    [ ! -e "$ruta" ] && [ ! -L "$ruta" ] || fallar 'La salida del grafo ya existe o es un enlace.'
done
mkdir /work/.graph-stage || fallar 'La etapa del grafo ya se inició; no se reintenta parcialmente.'
cd /work/source
cargo tree --frozen --target "$TARGET" -p vchord --features pg16 --edges normal,build --prefix none --format '{p}' > /outputs/vchord-tree.txt
cargo tree --frozen --target "$TARGET" -p xtask --edges normal,build --prefix none --format '{p}' > /outputs/xtask-tree.txt
cargo tree --frozen --target "$TARGET" -p vchord --features pg16 --edges normal,build --prefix none --format '{p}|{f}' > /outputs/vchord-features.txt
python3 /inputs/offline_recipe.py validate-graph --vchord /outputs/vchord-tree.txt --xtask /outputs/xtask-tree.txt --manifest /inputs/musl-build-inputs.json > /work/graph-result.json
python3 - <<'PY'
import hashlib, json, os, pathlib
root = pathlib.Path('/work'); output = pathlib.Path('/outputs')
sdk = output / 'sdk-receipt.json'
data = json.loads((root / 'graph-result.json').read_bytes())
features = output / 'vchord-features.txt'
if not 0 < features.stat().st_size <= 256 * 1024:
    raise ValueError('La observación de features excede su límite o está vacía.')
manifest = json.loads(pathlib.Path('/inputs/musl-build-inputs.json').read_bytes())
lock = hashlib.sha256((root / 'source/Cargo.lock').read_bytes()).hexdigest()
if lock != manifest['cargo']['lock_sha256']:
    raise ValueError('Cargo modificó el lock; se rechaza la etapa.')
data.update({'stage': 'graph', 'verified': True, 'sdk_receipt_sha256': hashlib.sha256(sdk.read_bytes()).hexdigest(),
    'features_sha256': hashlib.sha256(features.read_bytes()).hexdigest(),
    'features_review_required_before_compile': True,
    'cargo_lock_sha256': lock, 'input_manifest_sha256': os.environ['AGENCY_INPUT_MANIFEST_SHA256'],
    'sdk_executed': True, 'compilation_performed': False, 'requires_backup_before_compile': True})
with (output / 'graph-receipt.json').open('x') as stream:
    json.dump(data, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
PY
