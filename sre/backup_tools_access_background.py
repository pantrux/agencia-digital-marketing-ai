"""Ejecuta el lector de ACL compartido sin abrir una consola en Windows."""

import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "backup_tools_access_worker", Path(__file__).with_name("backup_tools_access.py")
)
_access = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_access)


def _run_without_console(command, **options):
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        **options,
    )


if os.name == "nt":
    # La sustitución se aplica solo a esta instancia del lector.
    _access.subprocess = SimpleNamespace(run=_run_without_console)

check_access = _access.check_access
