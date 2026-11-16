#!/bin/sh
set -eu

# Sólo se escribe en el constructor privado y en los montajes de esta corrida.
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C TZ=UTC
unset ENV BASH_ENV NODE_OPTIONS NODE_PATH LD_PRELOAD LD_LIBRARY_PATH || true
umask 022

node=/usr/local/bin/node
gyp=/usr/local/lib/node_modules/npm/node_modules/node-gyp/bin/node-gyp.js
prefix=/work/rdkafka-stage/usr
project=/work/npm/node_modules/@confluentinc/kafka-javascript

check_pin() {
    printf '%s  %s\n' "$1" "$2" | sha256sum -c -
}

compile() {
    test "$(id -u):$(id -g)" = 1000:1000
    test "$(ls /sys/class/net)" = lo
    export HOME=/work/home TMPDIR=/work/tmp
    mkdir -p "$HOME" "$TMPDIR" /work/source "$prefix"
    tar -xzf /inputs/librdkafka-v2.14.2.tar.gz -C /work/source
    cd /work/source/librdkafka-2.14.2
    test ! -e .git

    # Los flags exigen TLS, SASL y las mismas familias de compresión del runtime.
    /bin/bash ./configure --no-download --prefix=/usr --enable-ssl --enable-gssapi \
        --enable-curl --enable-zlib --enable-zstd --enable-lz4-ext > /result/configure.log 2>&1
    for feature in WITH_SSL WITH_SASL_CYRUS WITH_CURL WITH_ZLIB WITH_ZSTD WITH_LZ4_EXT WITH_OAUTHBEARER_OIDC; do
        grep -Eq "^[[:space:]]*$feature[[:space:]]*=[[:space:]]*y[[:space:]]*$" Makefile.config
    done
    cp Makefile.config config.h /result/
    cp LICENSE /result/librdkafka-LICENSE
    cp LICENSES.txt /result/librdkafka-LICENSES.txt
    make -j2 libs > /result/librdkafka-build.log 2>&1
    make -C src install DESTDIR=/work/rdkafka-stage >> /result/librdkafka-build.log 2>&1
    make -C src-cpp install DESTDIR=/work/rdkafka-stage >> /result/librdkafka-build.log 2>&1

    for package in kafka-javascript nan bindings file-uri-to-path; do
        case "$package" in
            kafka-javascript) target=$project ;;
            *) target=/work/npm/node_modules/$package ;;
        esac
        mkdir -p "$target"
        tar -xzf "/inputs/npm/$package.tgz" --strip-components=1 -C "$target"
    done
    cd "$project"
    test ! -e build
    # El gyp usa bibliotecas externas. Las rutas sólo se aplican durante la compilación.
    BUILD_LIBRDKAFKA=0 CPLUS_INCLUDE_PATH="$prefix/include/librdkafka" \
        LIBRARY_PATH="$prefix/lib" "$node" "$gyp" rebuild --release \
        --nodedir=/usr/local --python=/usr/bin/python3 -j2 > /result/addon-build.log 2>&1

    addon=confluent-kafka-javascript.node
    install -m 0555 "build/Release/$addon" "/result/$addon"
    for library in librdkafka.so.1 librdkafka++.so.1; do
        install -m 0555 "$prefix/lib/$library" "/result/$library"
    done
    cd /result
    for binary in "$addon" librdkafka.so.1 librdkafka++.so.1; do
        readelf -h "$binary" > "$binary.elf.txt"
        readelf -d "$binary" > "$binary.dynamic.txt"
        readelf -V "$binary" > "$binary.versions.txt"
        grep -Eq 'Machine:.*(X86-64|x86-64)' "$binary.elf.txt"
        grep -Fq '[libc.musl-x86_64.so.1]' "$binary.dynamic.txt"
        if grep -Eq 'RPATH|RUNPATH|libc\.so\.6' "$binary.dynamic.txt" || \
            grep -q 'GLIBC_' "$binary.versions.txt"; then
            echo "Enlace no portable: $binary" >&2
            exit 1
        fi
    done
    grep -Fq '[librdkafka.so.1]' "$addon.dynamic.txt"
    # El enlazador puede omitir librdkafka++ del addon si no hay referencias C++.
    # La biblioteca C++ se conserva y debe enlazar a la biblioteca C fijada.
    grep -Fq '[librdkafka.so.1]' 'librdkafka++.so.1.dynamic.txt'
    for library in librdkafka.so.1 librdkafka++.so.1; do
        grep -F 'SONAME' "$library.dynamic.txt" | grep -Fq "[$library]"
    done
    for needed in libssl.so.3 libsasl2.so.3 libcurl.so.4 libz.so.1 libzstd.so.1 liblz4.so.1; do
        grep -Fq "[$needed]" librdkafka.so.1.dynamic.txt
    done
    cmp "$project/build/Release/$addon" "/result/$addon"
    LD_LIBRARY_PATH=/result "$node" /inputs/verify-kafka-native.cjs "$project" > probe.json
    sha256sum "$addon" librdkafka.so.1 librdkafka++.so.1 ./*.txt ./*.log \
        Makefile.config config.h probe.json librdkafka-LICENSE > SHA256SUMS
}

if [ "$#" -eq 1 ] && [ "$1" = --compile ]; then
    compile
    exit 0
fi
test "$#" -eq 0
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
test "$(ls /sys/class/net)" = lo
test "$(cat /etc/alpine-release)" = 3.24.3
test "$(id -u node):$(id -g node)" = 1000:1000
test -z "$(ls -A /work)" && test -z "$(ls -A /result)"
test "$("$node" --version)" = v26.12.1
test "$("$node" -p process.versions.modules)" = 147
check_pin 36e129e95284c47a28366e83f85c33109c1f8ef06bb33b787f518d57dd01fc68 /lib/apk/db/installed
check_pin c6fa3c4a915da799b59b908e74a34f8f3307b7ec3251b211bc88d452558b3351 "$node"
check_pin c70f453d9d2db656ee023d2d99e4c0179ec926a403367c8d5a2e76c247e5fcbc /usr/local/include/node/config.gypi
check_pin 83185475268b1b2d9f5ffdbec1bb2062ebe70674869dde96fe6cd526dc9b2a79 /usr/local/include/node/node.h
check_pin 733bdbf2080c31a67d94cb8f7123110f44b0a3e2be24b8e575754af550eee6bf "$gyp"

cd /inputs
sha256sum -c SHA256SUMS
check_pin d7eec9c31c817fa44402f679c252dfbf97e4c338a849a25c3579a31fd127beb8 librdkafka-v2.14.2.tar.gz
check_pin c7d484bb4424a2a5682a02702bf2882db659f91c28cb29008a400eb96b192586 npm/kafka-javascript.tgz
check_pin 691429ff5de71779cd8efbec7f0c3bef6e87b142345dfb3376d7652771ef67bb npm/nan.tgz
check_pin d77781178c5bd89a91b1f6c5556acd511b1b5927eb13e2ad8189cac29eeb0907 npm/bindings.tgz
check_pin 5440cdf67e75ab96f36a6be63c1d4c3d54255b1d0970273710fecfebfab06fb3 npm/file-uri-to-path.tgz
while IFS= read -r name; do
    case "$name" in
        *[!a-zA-Z0-9_.+-]*|.*|-*) exit 1 ;;
        *.apk) ;;
        *) exit 1 ;;
    esac
    test -f "apk/$name" && test ! -L "apk/$name"
    set -- "$@" "/inputs/apk/$name"
done < APKS
test "$#" -eq 57
test "$(printf '%s\n' "$@" | sort -u | wc -l)" -eq 57
apk verify "$@" > /result/apk-verify.log 2>&1
apk --no-network --repositories-file /dev/null add --simulate "$@" > /result/apk-simulate.log 2>&1
apk --no-network --repositories-file /dev/null add "$@" > /result/apk-install.log 2>&1
apk info -v | sort > /result/packages.txt
grep -Fxq 'zlib-1.4-r0' /result/packages.txt
chown 1000:1000 /work /result
chmod 0755 /work /result
exec /bin/busybox su -s /bin/sh -c '/bin/sh /inputs/build-kafka-native.sh --compile' node
