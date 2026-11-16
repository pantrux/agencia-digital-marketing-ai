#!/bin/bash
# Compila únicamente en el SDK V4 respaldado; no instala sobre PostgreSQL activo.
set -euo pipefail
umask 077
fallar() { printf '%s\n' "$1" >&2; exit 1; }
[[ $# = 1 && $1 = compile ]] || fallar 'Sólo se admite compile.'
[[ $(id -u) = 0 && $(uname -s) = Linux && -f /.dockerenv ]] || fallar 'Se exige root Linux del contenedor propio.'
[[ ${AGENCY_BUILD_OWNER:-} = agencia-digital-marketing-ai:postgres-immich-builder:v1 ]] || fallar 'Owner incorrecto.'
[[ ${AGENCY_INPUT_MANIFEST_SHA256:-} = 03107096f926423b08a91f58269628bcd99232c5d89ba348246e65d3bd62bb4a ]] || fallar 'Kit incorrecto.'
[[ ${AGENCY_COMPILE_PROFILE_SHA256:-} =~ ^[a-f0-9]{64}$ && ${AGENCY_COMPILE_VERIFIER_SHA256:-} =~ ^[a-f0-9]{64}$ ]] || fallar 'Faltan los pins compile.'
work=/work/.compile-agency-v2
out=/outputs/compile-v2
verifier=$work/verifier.py
[[ -d $work && ! -L $work && $(stat -c '%u:%g:%a' "$work") = 0:0:700 ]] || fallar 'Directorio de receta no acreditado.'
printf '%s  %s\n' "$AGENCY_COMPILE_VERIFIER_SHA256" "$verifier" | sha256sum -c -
[[ ! -e $out && ! -L $out && ! -e /opt/agency-sqlite-v2 && ! -L /opt/agency-sqlite-v2 ]] || fallar 'Compile ya tiene estado previo.'
mkdir -m 700 "$work/.started" "$out"
(cd /inputs && sha256sum -c SHA256SUMS) >/dev/null
[[ $(sha256sum /outputs/vchord-features.txt | cut -d ' ' -f 1) = 21034e95a883742864ca39d5fa594435ba2a9223e70bdf79eff429cf1d9201c0 ]] || fallar 'Features cambiaron.'
[[ $(sha256sum /work/source/crates/xtask/src/main.rs | cut -d ' ' -f 1) = 9055ec9b313bcc827c52705f37bca6ceba4468de2073cb5de319e703684969b1 ]] || fallar 'xtask no conserva el parche congelado.'
python3 - <<'PY'
import hashlib, json, pathlib
expected = json.loads(pathlib.Path('/inputs/musl-build-inputs.json').read_bytes())['cargo']['lock_sha256']
if hashlib.sha256(pathlib.Path('/work/source/Cargo.lock').read_bytes()).hexdigest() != expected:
    raise ValueError('Cargo.lock cambió.')
PY
export PATH=/opt/agency-rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
if command -v git >/dev/null 2>&1; then fallar 'El SDK no admite un ejecutable Git.'; fi
unset RUSTC_WRAPPER RUSTC_WORKSPACE_WRAPPER RUNNER CARGO_ENCODED_RUSTFLAGS
unset CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_RUNNER RUSTDOCFLAGS
unset SQLITE3_STATIC PKG_CONFIG_ALL_STATIC
export PG_CONFIG=/usr/local/bin/pg_config PGRX_PG_CONFIG_PATH=/usr/local/bin/pg_config
[[ $($PG_CONFIG --version) = 'PostgreSQL 16.15' ]] || fallar 'PostgreSQL SDK distinto.'
[[ $($PG_CONFIG --pkglibdir) = /usr/local/lib/postgresql && $($PG_CONFIG --sharedir) = /usr/local/share/postgresql ]] || fallar 'Prefijos PostgreSQL distintos.'
export CARGO_HOME=/work/cargo-home CARGO_TARGET_DIR="$work/target" CARGO_NET_OFFLINE=true
export CARGO_BUILD_JOBS=2 RUSTC_BOOTSTRAP=1 TARGET=x86_64-unknown-linux-musl
export CC=clang-21 CXX=clang++-21
export SQLITE3_DYNAMIC=1 LIBSQLITE3_SYS_USE_PKG_CONFIG=1
export SQLITE3_LIB_DIR=/opt/agency-sqlite-v2/lib SQLITE3_INCLUDE_DIR=/opt/agency-sqlite-v2/include
export PKG_CONFIG_PATH=/opt/agency-sqlite-v2/lib/pkgconfig LD_LIBRARY_PATH=/opt/agency-sqlite-v2/lib
export RUSTFLAGS='-Dwarnings -Ctarget-feature=-crt-static -Cpanic=unwind -Clink-arg=-Wl,-rpath,$ORIGIN'
export CARGO_PROFILE_RELEASE_STRIP=none
python3 "$verifier" inside-inputs
clang-21 -O2 -fPIC -shared -Wl,-soname,libsqlite3.so.0 \
  -DSQLITE_CORE -DSQLITE_DEFAULT_FOREIGN_KEYS=1 -DSQLITE_ENABLE_API_ARMOR \
  -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_DBSTAT_VTAB \
  -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS3_PARENTHESIS -DSQLITE_ENABLE_FTS5 \
  -DSQLITE_ENABLE_JSON1 -DSQLITE_ENABLE_LOAD_EXTENSION=1 \
  -DSQLITE_ENABLE_MEMORY_MANAGEMENT -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_STAT4 \
  -DSQLITE_SOUNDEX -DSQLITE_THREADSAFE=1 -DSQLITE_USE_URI \
  -DHAVE_USLEEP=1 -DHAVE_ISNAN -D_POSIX_THREAD_SAFE_FUNCTIONS \
  "$work/sqlite/sqlite3.c" -o /opt/agency-sqlite-v2/lib/libsqlite3.so.0 -lm -ldl -lpthread
ln -s libsqlite3.so.0 /opt/agency-sqlite-v2/lib/libsqlite3.so
[[ $(pkg-config --modversion sqlite3) = 3.54.1 && $(pkg-config --variable=libdir sqlite3) = /opt/agency-sqlite-v2/lib ]] || fallar 'pkg-config no apunta al prefijo custom.'
pkg-config --libs --cflags sqlite3
python3 "$verifier" inside-sqlite
# PROFILE pertenece a PGXS como flags C; Cargo recibe --profile explícito.
unset PROFILE
make -C "$work/pgvector" -j2 all PG_CONFIG=/usr/local/bin/pg_config OPTFLAGS=''
make -C "$work/pgvector" install PG_CONFIG=/usr/local/bin/pg_config OPTFLAGS='' DESTDIR="$out/runtime-root"
cd /work/source
cargo run --frozen -p xtask -- build --target x86_64-unknown-linux-musl --profile release --output "$work/vchord-build"
[[ -f $work/vchord-build/pkglibdir/vchord.so && ! -L $work/vchord-build/pkglibdir/vchord.so ]] || fallar 'Falta vchord.so real.'
cp -R "$work/vchord-build/pkglibdir/." "$out/runtime-root/usr/local/lib/postgresql/"
cp -R "$work/vchord-build/sharedir/." "$out/runtime-root/usr/local/share/postgresql/"
cp /opt/agency-sqlite-v2/lib/libsqlite3.so.0 "$out/runtime-root/usr/local/lib/postgresql/libsqlite3.so.0"
mkdir -p "$out/runtime-root/usr/local/share/licenses/agency-postgres-immich"
cp "$work/pgvector/LICENSE" "$out/runtime-root/usr/local/share/licenses/agency-postgres-immich/pgvector-LICENSE"
cp /work/source/LICENSE "$out/runtime-root/usr/local/share/licenses/agency-postgres-immich/VectorChord-LICENSE"
cp /work/source/licenses/LICENSE.AGPLv3 "$out/runtime-root/usr/local/share/licenses/agency-postgres-immich/VectorChord-AGPLv3"
cp /work/source/licenses/LICENSE.ELv2 "$out/runtime-root/usr/local/share/licenses/agency-postgres-immich/VectorChord-ELv2"
head -n 25 "$work/sqlite/sqlite3.c" > "$out/runtime-root/usr/local/share/licenses/agency-postgres-immich/SQLite-NOTICE"
python3 - <<'PY'
import hashlib, json, pathlib
expected = json.loads(pathlib.Path('/inputs/musl-build-inputs.json').read_bytes())['cargo']['lock_sha256']
if hashlib.sha256(pathlib.Path('/work/source/Cargo.lock').read_bytes()).hexdigest() != expected:
    raise ValueError('Cargo.lock cambió durante build.')
PY
[[ $(sha256sum /work/source/crates/xtask/src/main.rs | cut -d ' ' -f 1) = 9055ec9b313bcc827c52705f37bca6ceba4468de2073cb5de319e703684969b1 ]] || fallar 'xtask cambió durante build.'
python3 "$verifier" inside-catalog
