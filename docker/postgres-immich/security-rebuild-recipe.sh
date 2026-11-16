#!/bin/bash
# Sólo orquesta el verificador fijado; no instala ni recompila SQLite/pgvector.
set -euo pipefail
umask 077
work=/work/.security-rebuild-v1
[[ $# = 1 ]] || exit 2
case "$1" in materialize|graph|regression|build|catalog) ;; *) exit 2 ;; esac
[[ $(id -u) = 0 && $(uname -s) = Linux && -f /.dockerenv ]] || exit 2
[[ ${AGENCY_REBUILD_PROFILE_SHA256:-} =~ ^[a-f0-9]{64}$ ]] || exit 2
[[ ${AGENCY_REBUILD_VERIFIER_SHA256:-} =~ ^[a-f0-9]{64}$ ]] || exit 2
[[ -d $work && ! -L $work && $(stat -c '%u:%g:%a' "$work") = 0:0:700 ]] || exit 2
printf '%s  %s\n' "$AGENCY_REBUILD_PROFILE_SHA256" "$work/profile.json" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$AGENCY_REBUILD_VERIFIER_SHA256" "$work/verifier.py" | sha256sum -c - >/dev/null
exec /usr/bin/python3 -B "$work/verifier.py" "inside-$1"
