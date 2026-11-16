"""Entrada fija del backend SRT revisado; sólo se ejecuta tras apply del operador."""

import importlib.metadata
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def deny_audit_event(event, args):
    if event == "import" and args and (args[0] == "pip" or args[0].startswith("pip.")):
        raise RuntimeError("SRT_BUILD_PIP_FORBIDDEN")
    if event in {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.startfile",
        "socket.__new__",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.bind",
    }:
        raise RuntimeError("SRT_BUILD_PROCESS_OR_NETWORK_FORBIDDEN")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise RuntimeError("SRT_BUILD_ARGUMENTS")
    source, output = map(Path, args)
    if (
        not source.is_absolute()
        or not output.is_absolute()
        or not source.is_dir()
        or not output.is_dir()
    ):
        raise RuntimeError("SRT_BUILD_PATHS")
    if any(output.iterdir()) or any(
        name == "pip" or name.startswith("pip.") for name in sys.modules
    ):
        raise RuntimeError("SRT_BUILD_NOT_EMPTY")
    for name, version in {"setuptools": "84.2.1", "wheel": "0.48.0", "packaging": "26.3"}.items():
        if importlib.metadata.version(name) != version:
            raise RuntimeError("SRT_BUILD_TOOL_VERSION")
    sys.addaudithook(deny_audit_event)
    os.chdir(source)
    from setuptools.build_meta import __legacy__

    filename = __legacy__.build_wheel(str(output), config_settings={})
    print(
        json.dumps(
            {
                "wheel": filename,
                "backend": "setuptools.build_meta:__legacy__",
                "python_network_allowed": False,
                "child_processes_allowed": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
