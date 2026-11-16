"""Retira pip y comprueba los dos intérpretes del runner Python de n8n."""

import hashlib
import importlib.metadata
import importlib.util
import os
import shutil
import ssl
import sys
from pathlib import Path

LOCAL = Path("/usr/local")
LIB = LOCAL / "lib/python3.13"
SITE = LIB / "site-packages"
VENV = Path("/opt/runners/task-runner-python/.venv")
PIP = SITE / "pip"
PIP_METADATA = SITE / "pip-26.2.2.dist-info"
ENSUREPIP = LIB / "ensurepip"
PINNED = {
    LOCAL / "bin/pip3": "33bb4cb56b66f09b314e6082d3d0132569367bfd760348bef710bd3ea8faed79",
    LOCAL / "bin/pip3.13": "33bb4cb56b66f09b314e6082d3d0132569367bfd760348bef710bd3ea8faed79",
    PIP / "_vendor/vendor.txt": "2b44b4bc5b7da9dcda38099dc92c52cc3dda2a714cd189b439c0668401418630",
    PIP_METADATA / "METADATA": "fe157a60a970b288770fad79a124001f538a325b0a2270c594792b6a475eb798",
    ENSUREPIP
    / "_bundled/pip-26.2.2-py3-none-any.whl": "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70",
}
SHIMS = ("pip", "pip3", "pip3.13")
DISTRIBUTIONS = {
    "task-runner-python": "0.1.0",
    "certifi": "2026.10.23",
    "sentry-sdk": "2.35.2",
    "urllib3": "2.8.0",
    "websockets": "15.0.1",
}


def digest(path):
    assert path.is_file() and path.resolve(strict=True) == path, str(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def remove_build_tools():
    assert (os.geteuid(), os.getegid()) == (0, 0)
    assert sys.version_info[:3] == (3, 13, 18)
    assert Path(LOCAL / "bin/pip").readlink() == Path("pip3")
    assert {path: digest(path) for path in PINNED} == PINNED
    for path in (PIP, PIP_METADATA, ENSUREPIP):
        assert path.resolve(strict=True) == path and path.is_dir(), str(path)
        shutil.rmtree(path)
    for name in SHIMS:
        (LOCAL / "bin" / name).unlink()


def verify():
    assert (os.geteuid(), os.getegid()) == (1000, 1000)
    assert sys.version_info[:3] == (3, 13, 18)
    assert Path(sys.base_prefix) == LOCAL
    assert ssl.create_default_context().get_ca_certs()
    assert not any(os.path.lexists(path) for path in (PIP, PIP_METADATA, ENSUREPIP))
    assert not any(SITE.glob("pip-*.dist-info"))
    for name in SHIMS:
        assert not os.path.lexists(LOCAL / "bin" / name) and shutil.which(name) is None
    for name in ("pip", "ensurepip", "setuptools", "msgpack"):
        assert importlib.util.find_spec(name) is None, name
    packages = {
        row.metadata["Name"].lower().replace("_", "-"): row.version
        for row in importlib.metadata.distributions()
    }
    if Path(sys.prefix) == VENV:
        import urllib3
        import websockets.speedups  # noqa: F401  # Comprueba que la extensión nativa carga.

        assert packages == DISTRIBUTIONS
        assert Path(urllib3.__file__).is_relative_to(VENV) and urllib3.__version__ == "2.8.0"
        assert importlib.util.find_spec("src.main") is not None
    else:
        assert Path(sys.prefix) == LOCAL and not packages
        assert importlib.util.find_spec("urllib3") is None
    print(f"Intérprete verificado: {sys.executable}")


if __name__ == "__main__":
    (action,) = sys.argv[1:]
    {"remove-build-tools": remove_build_tools, "verify": verify}[action]()
