#!/usr/bin/env bash
# El controlador comparte un plazo de 85 s entre sondas, reversión y reverificación.
set -euo pipefail
directorio_sre="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${directorio_sre}/sre_control.py" smoke "$@"
