"""Perfil puro del compilador aislado: sólo planifica; no inicia Go ni crea rutas.

La utilidad windows_build_job es genérica. Antes de usarla, el futuro controlador
debe comprobar hashes/ACL, preparar la raíz propia respaldada y aprobar la
operación. Esta unidad no resuelve módulos ni implementa un lanzador del SDK.
"""

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path, PureWindowsPath

OWNER = "agencia-digital-marketing-ai:backup-build-job:v1"
BUILD_ROOT = PureWindowsPath(
    r"D:\Proyectos\Agencia Digital de Marketing AI\.tools\backup-source-build"
)
SDK_MAP_SHA = "c88ef844b9f215c43406e106606f8a65b6e234672947cdd6daf84d5b3bc830ae"
GO_SHA = "83b7ba66215e696414fa49462d0db2da4f4cf15c7bebd42f0b189fa04866999d"
LIMITS = {
    "processes": 8,
    "memory_bytes": 2 * 1024**3,
    "output_bytes": 65536,
    "env_timeout_seconds": 30,
    "prepare_timeout_seconds": 180,
    "build_timeout_seconds": 600,
}
ENV_QUERY = [
    "GOVERSION",
    "GOOS",
    "GOARCH",
    "GOROOT",
    "GOPATH",
    "GOCACHE",
    "GOMODCACHE",
    "GOVCS",
    "GOTOOLCHAIN",
    "CGO_ENABLED",
    "GOPROXY",
    "GOSUMDB",
    "GOENV",
    "GOWORK",
]
FIELDS = {
    "schema_version",
    "owner",
    "build_root",
    "build_marker_name",
    "build_marker_value",
    "run_root",
    "run_marker_value",
    "sdk_version",
    "sdk_root",
    "sdk_executable",
    "sdk_executable_sha256",
    "sdk_tools_map_path",
    "sdk_tools_map_sha256",
    "windows_root",
    "allowed_operations",
    "limits",
}


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != FIELDS:
        raise ValueError("Perfil incompleto o con campos ajenos.")
    expected = {
        "schema_version": 1,
        "owner": OWNER,
        "build_marker_name": ".agency-backup-build-owner",
        "build_marker_value": "agencia-digital-marketing-ai:backup-source-build:v1",
        "run_marker_value": "agencia-digital-marketing-ai:backup-build-run:v1",
        "sdk_version": "go1.27.3",
        "sdk_executable_sha256": GO_SHA,
        "sdk_tools_map_sha256": SDK_MAP_SHA,
        "windows_root": r"C:\Windows",
        "allowed_operations": ["env"],
        "limits": LIMITS,
    }
    if type(profile["schema_version"]) is not int or any(
        profile[key] != value for key, value in expected.items()
    ):
        raise ValueError("El perfil amplía operaciones, límites o identidad del SDK.")
    if any(type(profile["limits"][key]) is not int for key in LIMITS):
        raise ValueError("Los límites requieren enteros exactos.")
    if not isinstance(profile["build_root"], str):
        raise ValueError("La raíz requiere una ruta literal.")
    root = PureWindowsPath(profile["build_root"])
    if (
        not root.is_absolute()
        or root.drive.upper() != "D:"
        or root != BUILD_ROOT
        or ".." in root.parts
    ):
        raise ValueError("Raíz de build fuera de la ubicación privada prevista.")
    sdk = root / "sdk/go1.27.3/go"
    paths = {
        "sdk_root": sdk,
        "sdk_executable": sdk / "bin/go.exe",
        "run_root": root / "runs/go-sdk-env-v1",
        "sdk_tools_map_path": root.parent / "evidence/backup-go-sdk-executable-map.json",
    }
    if any(
        not isinstance(profile[key], str) or PureWindowsPath(profile[key]) != path
        for key, path in paths.items()
    ):
        raise ValueError("Las rutas derivadas no coinciden con la raíz y el SDK.")
    return profile


def environment(profile):
    """Crea desde cero; no consulta ni combina os.environ ni la bóveda."""
    validate_profile(profile)
    run, sdk, windows = (
        PureWindowsPath(profile[key]) for key in ("run_root", "sdk_root", "windows_root")
    )
    return {
        "SystemRoot": str(windows),
        "WINDIR": str(windows),
        "PATH": str(sdk / "bin") + ";" + str(windows / "System32"),
        "TEMP": str(run / "tmp"),
        "TMP": str(run / "tmp"),
        "GOTMPDIR": str(run / "tmp"),
        "USERPROFILE": str(run / "profile"),
        "LOCALAPPDATA": str(run / "profile/local"),
        "APPDATA": str(run / "profile/roaming"),
        "GOROOT": str(sdk),
        "GOPATH": str(run / "gopath"),
        "GOCACHE": str(run / "cache"),
        "GOMODCACHE": str(run / "module-cache"),
        "GOENV": "off",
        "GOWORK": "off",
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
        "GOVCS": "*:off",
        "GOFLAGS": "",
        "CGO_ENABLED": "0",
        "GOMAXPROCS": "2",
        "GOPROXY": "https://proxy.golang.org",
        "GOSUMDB": "sum.golang.org",
        "GOPRIVATE": "",
        "GONOPROXY": "",
        "GONOSUMDB": "",
        "GOINSECURE": "",
    }


def command(profile, operation):
    validate_profile(profile)
    if operation != "env":
        raise ValueError("Esta unidad sólo prepara la consulta de entorno.")
    return profile["sdk_executable"], ["env", "-json", *ENV_QUERY]


def make_plan(profile):
    validate_profile(profile)
    return {
        "mode": "plan",
        "owner": OWNER,
        "sdk_version": profile["sdk_version"],
        "sdk_executable": profile["sdk_executable"],
        "sdk_executable_sha256": GO_SHA,
        "sdk_tools_map_sha256": SDK_MAP_SHA,
        "run_root": profile["run_root"],
        "allowed_operations": ["env"],
        "limits": LIMITS.copy(),
        "environment_names": sorted(environment(profile)),
        "sdk_executed": False,
        "sdk_declared_clean": False,
        "paths_created": False,
        "execution_implemented_by_this_controller": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", type=Path, default=Path(__file__).with_name("backup-go-build-profile.json")
    )
    parser.add_argument("--profile-sha256")
    arguments = parser.parse_args(argv)
    try:
        info = arguments.profile.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 16384
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Se requiere un perfil regular único y acotado.")
        descriptor = os.open(
            arguments.profile,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                info.st_dev,
                info.st_ino,
                info.st_size,
            ):
                raise ValueError("El perfil cambió al abrirse.")
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError("Perfil excesivo.")
        digest = hashlib.sha256(data).hexdigest()
        if arguments.profile_sha256 is not None and digest != arguments.profile_sha256:
            raise ValueError("El perfil difiere del hash externo.")

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Perfil ambiguo por claves duplicadas.")
                value[key] = item
            return value

        result = make_plan(json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique))
        result.update(
            profile_sha256=digest,
            external_profile_hash_verified=arguments.profile_sha256 is not None,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "mode": "plan",
                    "verified": False,
                    "sdk_executed": False,
                    "error": "Perfil rechazado por contrato o integridad.",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
