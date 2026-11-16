#!/usr/bin/env bash
# La huella externa del manifiesto es obligatoria antes de cualquier cambio remoto.
set -euo pipefail
directorio_sre="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${directorio_sre}/sre_control.py" rollback "$@"
