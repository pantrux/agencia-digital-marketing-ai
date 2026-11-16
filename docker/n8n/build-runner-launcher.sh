#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 3 ]]; then
    printf '%s\n' 'Uso: bash build-runner-launcher.sh CONTEXTO GO_SDK_ROOT SALIDA_NUEVA' >&2
    exit 2
fi
context=$(/usr/bin/realpath -e -- "$1")
sdk=$(/usr/bin/realpath -e -- "$2")
output=$(/usr/bin/realpath -m -- "$3")
for path in "$context" "$sdk" "$output"; do
    [[ $path =~ ^/[A-Za-z0-9_./-]+$ ]] || { printf '%s\n' 'Ruta fuera del contrato.' >&2; exit 2; }
done
[[ $output != "$context" && $output != "$context/"* && $output != "$sdk" && $output != "$sdk/"* ]]
[[ -x $sdk/bin/go && -f $sdk/VERSION && ! -L $sdk/bin/go ]]
IFS= read -r version < "$sdk/VERSION"
[[ $version == go1.27.3 ]]
(cd -- "$context" && /usr/bin/sha256sum --check --strict --status SHA256SUMS)
/usr/bin/mkdir -- "$output"
/usr/bin/mkdir -- "$output/home" "$output/cache" "$output/modcache" "$output/tmp" "$output/path"
go_args=(
    /usr/bin/env -i "PATH=$output/path" "HOME=$output/home" "TMPDIR=$output/tmp"
    "GOROOT=$sdk" "GOCACHE=$output/cache" "GOMODCACHE=$output/modcache"
    "GOPROXY=file://$context/proxy" GOSUMDB=off GOENV=off GOWORK=off GOAUTH=off
    GOTOOLCHAIN=local GOVCS='*:off' GOTELEMETRY=off CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOMAXPROCS=2
    "$sdk/bin/go"
)
cd -- "$context/source"
"${go_args[@]}" build -mod=readonly -buildvcs=false -trimpath -p=2 -o "$output/task-runner-launcher" ./cmd/launcher
"${go_args[@]}" version -m "$output/task-runner-launcher" > "$output/buildinfo.txt"
/usr/bin/sha256sum -- "$output/task-runner-launcher" "$output/buildinfo.txt"
