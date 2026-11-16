#!/bin/sh
set -eu

# Construye sólo el addon Linux amd64, a partir de entradas verificadas.
export LC_ALL=C
test "$(uname -m)" = x86_64
test "$(node --version)" = v26.12.1
test "$(cat /etc/alpine-release)" = 3.24.3
test "$(ls /sys/class/net)" = lo

cd /work
sha256sum -c /inputs/SHA256SUMS
while IFS= read -r name; do
    case "$name" in
        zstd-libs-1.5.7-r2.apk) path="/inputs/$name" ;;
        *.apk) path="/alpine/$name" ;;
        *) exit 1 ;;
    esac
    set -- "$@" "$path"
done < /inputs/APKS
test "$#" -eq 15
apk verify "$@"
apk --no-network --repositories-file /dev/null add "$@"

mkdir sqlite node napi binding
unzip -p /inputs/sqlite.zip sqlite-amalgamation-3540100/sqlite3.c > sqlite/sqlite3.c
unzip -p /inputs/sqlite.zip sqlite-amalgamation-3540100/sqlite3.h > sqlite/sqlite3.h
tar -xzf /inputs/node.tar.gz -C node
tar -xzf /inputs/napi.tgz -C napi
tar -xzf /inputs/binding.tgz -C binding
cmp node/node-v26.12.1/include/node/node_api.h /usr/local/include/node/node_api.h

# Conserva las funciones del SQLite de node-sqlite3 y fija la amalgamación.
gcc -O2 -fPIC -fvisibility=hidden -DSQLITE_THREADSAFE=1 -DHAVE_USLEEP=1 \
    -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS4 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE \
    -DSQLITE_ENABLE_DBSTAT_VTAB=1 -DSQLITE_ENABLE_MATH_FUNCTIONS \
    -c sqlite/sqlite3.c -o sqlite/sqlite3.o
g++ -O2 -std=c++17 -fPIC -shared -DNAPI_VERSION=6 -DNAPI_DISABLE_CPP_EXCEPTIONS=1 \
    -I node/node-v26.12.1/include/node -I napi/package -I sqlite \
    binding/package/src/backup.cc binding/package/src/database.cc \
    binding/package/src/node_sqlite3.cc binding/package/src/statement.cc \
    sqlite/sqlite3.o -pthread -lm -ldl -o node_sqlite3.node

readelf -d node_sqlite3.node > /result/dynamic.txt
readelf -V node_sqlite3.node > /result/versions.txt
readelf -Ws node_sqlite3.node > symbols.txt
grep -q 'napi_register_module_v1' symbols.txt
grep -q 'libc.musl-x86_64.so.1' /result/dynamic.txt
if grep -E 'libsqlite3|RPATH|RUNPATH' /result/dynamic.txt || grep -q 'GLIBC_' /result/versions.txt; then
    exit 1
fi
install -m 0555 node_sqlite3.node /result/node_sqlite3.node
mkdir -p binding/package/build/Release
ln -s /result/node_sqlite3.node binding/package/build/Release/node_sqlite3.node
NODE_PATH=/binding-deps/sqlite3@5.1.7_bluebird@3.7.2_supports-color@8.1.1/node_modules \
    node /inputs/probe.cjs /work/binding/package/lib/sqlite3.js > /result/probe.json
sha256sum /result/node_sqlite3.node
