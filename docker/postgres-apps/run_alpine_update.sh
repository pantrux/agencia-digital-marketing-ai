#!/bin/sh
# Operates on a verified local Docker builder and the sealed input directory.
set -eu
umask 077

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

[ "$#" -eq 4 ] || fail 'Usage: run_alpine_update.sh build|export transport-dir base-tag candidate-tag'
mode=$1
root=$(realpath -e -- "$2")
base=$3
candidate=$4
base_manifest=sha256:c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a
base_config=sha256:22fdd47fd045fefc8c744ff00f801c6efe0e99cab9eb67e1751712760974316c
[ "$base" != "$candidate" ] || fail 'The base and candidate references must differ'
case "$base:$candidate" in
    *[!a-zA-Z0-9_.:/-]*|:*|*:) fail 'Invalid local image reference' ;;
esac

[ -z "${DOCKER_HOST:-}${DOCKER_CONTEXT:-}${BUILDX_BUILDER:-}" ] ||
    fail 'Select the local default Docker context without overrides'
case "$(docker --context default info --format '{{.OSType}}/{{.Architecture}}')" in
    linux/x86_64|linux/amd64) ;;
    *) fail 'Expected a Linux amd64 Docker engine' ;;
esac
[ "$(docker context inspect default --format '{{.Endpoints.docker.Host}}')" = 'unix:///var/run/docker.sock' ] ||
    fail 'Expected the existing local Docker context'

inspect() {
    docker --context default image inspect --format \
        '{{.Id}}|{{.Os}}/{{.Architecture}}|{{.Config.User}}|{{json .Config.Entrypoint}}|{{json .Config.Cmd}}|{{.Config.StopSignal}}' "$1"
}

contract='linux/amd64|70:70|["docker-entrypoint.sh"]|["postgres"]|SIGINT'
base_before=$(inspect "$base")
case "$base_before" in
    "$base_manifest|$contract"|"$base_config|$contract") ;;
    *) fail 'The PostgreSQL base does not match the sealed identity' ;;
esac

case "$mode" in
    build)
        cd "$root"
        sha256sum -c TRANSFER.sha256
        if docker --context default image inspect "$candidate" >/dev/null 2>&1; then
            fail 'The candidate tag already exists'
        fi
        mkdir result
        docker_root=$(docker --context default info --format '{{.DockerRootDir}}')
        df -Pk -- "$root" "$docker_root" > "$root/result/free-space.txt"
        docker --context default buildx build --builder default --platform linux/amd64 --network none \
            --pull=false --provenance=false --sbom=false \
            --build-arg "POSTGRES_APPS_BASE=$base" \
            --build-context "apk_inputs=$root/apk-inputs" \
            --metadata-file "$root/result/build-metadata.json" \
            --file "$root/context/Dockerfile" --tag "$candidate" "$root/context" \
            > "$root/result/build.log" 2>&1
        [ "$(inspect "$base")" = "$base_before" ] || fail 'The base reference changed'
        result=$(inspect "$candidate")
        case "$result" in
            sha256:*"|$contract") ;;
            *) fail 'The built image does not preserve the PostgreSQL runtime contract' ;;
        esac
        printf '%s\n' "${result%%|*}" > "$root/result/candidate.id"
        printf '%s\n' "$result"
        ;;
    export)
        result=$(inspect "$candidate")
        [ "${result%%|*}" = "$(cat "$root/result/candidate.id")" ] ||
            fail 'The candidate reference changed after the build'
        docker --context default image save --platform linux/amd64 "$candidate"
        ;;
    *) fail 'Unknown operation' ;;
esac
