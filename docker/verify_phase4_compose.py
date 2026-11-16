"""Planifica y valida los proyectos Compose ya acreditados de vm-media-01."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

if __package__:
    from . import verify_phase4_runtime as inventory
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import verify_phase4_runtime as inventory

REPO = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).resolve().with_name("phase4-runtime.json")
HOST = "vm-media-01"
INSTALL_ROOT = PurePosixPath("/opt/media-platform")
SUPOCLIP = "agency-supoclip-trial-nov13"
OTEL = "agency-observability-nas-v1"
READ_ONLY_ENV = "/opt/media-platform/moneyprinter-lan-v1/compose.env"
MIGRATE_IMAGE = "MEDIA_SUPOCLIP_TRIAL_MIGRATE_IMAGE"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "COMPOSE_DISABLE_ENV_FILE": "true"}
PIN = re.compile(r"[a-f0-9]{64}")
NAME = re.compile(r"[a-z][a-z0-9.-]+\.ya?ml")
VARIABLE = re.compile(r"(?<!\$)\$\{([A-Z_][A-Z_0-9]*)[}:]")
ENV_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}|/[A-Za-z0-9._/-]{1,511}")
MAX_FILE = 1024**2


def require(condition, code):
    if not condition:
        raise ValueError(code)


def decode(raw):
    value = json.loads(raw, object_pairs_hook=inventory.unique_fields)
    require(isinstance(value, dict), "JSON_ESQUEMA")
    return value


def load_manifest(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    require(0 < len(raw) <= MAX_FILE, "MANIFIESTO_COTA")
    return decode(raw)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_pin(pin):
    return isinstance(pin, str) and PIN.fullmatch(pin) is not None


def source_bytes(relative):
    source = REPO / relative
    require(source.resolve(strict=True).is_relative_to(REPO) and source.is_file(), "FUENTE_RUTA")
    with source.open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    require(0 < len(raw) <= MAX_FILE, "FUENTE_COTA")
    return raw


def observed_rows(paths, spec):
    rows = [
        {
            "service": role,
            "files": ",".join(str(paths / name) for name in group["files"]),
            "workdir": str(paths),
            "env": spec["env_file"],
        }
        for group in spec["groups"]
        for role in group["roles"]
    ]
    return sorted(rows, key=lambda row: row["service"])


def validate(data, *, sources=True):
    require(
        set(data) == {"schema", "host", "evidence", "projects", "engine"}
        and type(data["schema"]) is int
        and data["schema"] == 1
        and data["host"] == HOST,
        "MANIFIESTO_ESQUEMA",
    )
    require(set(data["projects"]) == set(inventory.PROJECTS), "PROYECTOS")
    require(
        data["engine"]
        == {
            "r2": {
                "owner": inventory.R2_OWNER,
                "source": "docker/r2_service.py",
                "roles": {"serve": "running", "init": "optional_exited"},
            }
        },
        "ENGINE_R2",
    )
    require(
        set(data["evidence"]) == {"compose_paths", "compose_hashes", "runtime_inventory"},
        "EVIDENCIAS",
    )
    for record in data["evidence"].values():
        require(
            set(record) == {"path", "sha256"}
            and record["path"].startswith(".tools/evidence/")
            and file_pin(record["sha256"]),
            "EVIDENCIA",
        )
    installed = {}
    for project, spec in data["projects"].items():
        require(
            set(spec)
            == {
                "workdir",
                "env_file",
                "profiles",
                "variables",
                "files",
                "groups",
                "supervisor",
                "replacement_overlay",
            },
            "PROYECTO_ESQUEMA",
        )
        workdir = PurePosixPath(spec["workdir"])
        require(
            workdir != INSTALL_ROOT
            and workdir.is_relative_to(INSTALL_ROOT)
            and ".." not in workdir.parts
            and str(workdir) == spec["workdir"],
            "PROYECTO_RUTA",
        )
        scoped = project in (SUPOCLIP, OTEL)
        require(
            spec["env_file"] == ("/dev/null" if scoped else str(workdir / "compose.env")),
            "PROYECTO_ENV",
        )
        require(
            spec["profiles"] == (["migration"] if project == SUPOCLIP else []), "PROYECTO_PERFILES"
        )
        roles = [role for group in spec["groups"] for role in group["roles"]]
        require(sorted(roles) == sorted(inventory.PROJECTS[project]), "PROYECTO_ROLES")
        files = spec["files"]
        require(
            set(files) == {name for group in spec["groups"] for name in group["files"]},
            "PROYECTO_ARCHIVOS",
        )
        require(
            all(
                set(group) == {"files", "roles"}
                and group["files"]
                and group["roles"]
                and len(set(group["files"])) == len(group["files"])
                for group in spec["groups"]
            ),
            "PROYECTO_GRUPOS",
        )
        require(
            spec["supervisor"] is None or spec["supervisor"] in inventory.UNITS,
            "PROYECTO_SUPERVISOR",
        )
        variables = spec["variables"]
        require(
            variables == sorted(set(variables)) and bool(variables) == scoped, "PROYECTO_VARIABLES"
        )
        source_text = []
        for name, record in files.items():
            require(
                NAME.fullmatch(name)
                and set(record) == {"source", "source_sha256", "installed_sha256", "installed_mode"}
                and record["source"].startswith("docker/")
                and record["installed_mode"] in {"400", "444", "600"}
                and all(file_pin(record[key]) for key in ("source_sha256", "installed_sha256")),
                "PROYECTO_ARCHIVO",
            )
            installed[str(workdir / name)] = (
                record["installed_sha256"],
                "0o" + record["installed_mode"],
            )
            if sources:
                raw = source_bytes(record["source"])
                require(sha(raw) == record["source_sha256"], "FUENTE_SHA")
                source_text.append(raw.decode("utf-8"))
        overlay = spec["replacement_overlay"]
        if project == SUPOCLIP:
            require(
                overlay["source"] == "docker/supoclip/compose.boot.yml"
                and file_pin(overlay["sha256"])
                and "compose.boot.yml" not in files,
                "SUPOCLIP_OVERLAY",
            )
            if sources:
                require(sha(source_bytes(overlay["source"])) == overlay["sha256"], "FUENTE_SHA")
        else:
            require(overlay is None, "PROYECTO_OVERLAY")
        if sources and scoped:
            names = set(VARIABLE.findall("\n".join(source_text)))
            if project == SUPOCLIP:
                names.discard(MIGRATE_IMAGE)
            require(set(variables) == names, "PROYECTO_VARIABLES")
    require(
        {spec["supervisor"] for spec in data["projects"].values()} - {None} == set(inventory.UNITS),
        "SUPERVISORES",
    )
    if sources:
        evidence = {}
        for name, record in data["evidence"].items():
            raw = source_bytes(record["path"])
            require(sha(raw) == record["sha256"], "EVIDENCIA_SHA")
            evidence[name] = decode(raw)
        for project, spec in data["projects"].items():
            paths = PurePosixPath(spec["workdir"])
            require(
                observed_rows(paths, spec)
                == sorted(evidence["compose_paths"][project], key=lambda row: row["service"]),
                "EVIDENCIA_COMPOSE",
            )
        require(
            installed
            == {
                key: (record["sha256"], record["mode"])
                for key, record in evidence["compose_hashes"].items()
            },
            "EVIDENCIA_HASHES",
        )
        old_inventory = evidence["runtime_inventory"]
        mandatory = {key for key, contract in inventory.SERVICES.items() if not contract.optional}
        observed = set(old_inventory["containers"])
        require(
            old_inventory["success"] is True
            and len(mandatory) <= old_inventory["images_pinned"] <= len(observed)
            and mandatory <= observed <= set(inventory.SERVICES),
            "EVIDENCIA_INVENTARIO",
        )


def compose_args(project, spec, group):
    args = [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "compose",
        "--ansi",
        "never",
        "--project-name",
        project,
        "--project-directory",
        spec["workdir"],
        "--env-file",
        spec["env_file"],
    ]
    for profile in spec["profiles"]:
        args.extend(("--profile", profile))
    for filename in group["files"]:
        args.extend(("-f", str(PurePosixPath(spec["workdir"]) / filename)))
    return args + ["config", "--quiet", *group["roles"]]


def scoped_env(data, raw):
    profiles = decode(raw)
    required = {
        project: set(spec["variables"])
        for project, spec in data["projects"].items()
        if spec["variables"]
    }
    require(set(profiles) == set(required), "VARIABLES_PROYECTOS")
    for project, values in profiles.items():
        optional = {MIGRATE_IMAGE} if project == SUPOCLIP else set()
        require(
            isinstance(values, dict)
            and required[project] <= set(values) <= required[project] | optional
            and all(
                isinstance(value, str) and ENV_VALUE.fullmatch(value) for value in values.values()
            ),
            "VARIABLES_CAMPOS",
        )
    return profiles


def checked_file(path, mode, *, content=False):
    require(path.is_absolute() and path.resolve(strict=True) == path, "ARCHIVO_RUTA")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and (info.st_uid, info.st_gid) == (0, 0)
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == mode
            and 0 < info.st_size <= MAX_FILE,
            "ARCHIVO_IDENTIDAD",
        )
        raw = stream.read(MAX_FILE + 1) if content else None
        after = path.lstat()
        require(
            (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            == (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ),
            "ARCHIVO_CAMBIO",
        )
    return raw


def check(data, variables):
    require(
        sys.platform == "linux"
        and os.uname().nodename == HOST
        and os.geteuid() == os.getegid() == 0,
        "HOST_IDENTIDAD",
    )
    envs = scoped_env(data, checked_file(variables, 0o600, content=True))
    result = {}
    for project, spec in data["projects"].items():
        try:
            for filename, record in spec["files"].items():
                raw = checked_file(
                    Path(spec["workdir"]) / filename, int(record["installed_mode"], 8), content=True
                )
                require(sha(raw) == record["installed_sha256"], "COMPOSE_INSTALADO_SHA")
            if spec["env_file"] != "/dev/null":
                env_mode = 0o400 if spec["env_file"] == READ_ONLY_ENV else 0o600
                checked_file(Path(spec["env_file"]), env_mode)
        except (OSError, ValueError) as error:
            code = str(error) if type(error) is ValueError else "ARCHIVO_NO_DISPONIBLE"
            result[project] = [
                {"roles": group["roles"], "config_verified": False, "error": code}
                for group in spec["groups"]
            ]
            continue
        result[project] = []
        for group in spec["groups"]:
            child = subprocess.run(
                compose_args(project, spec, group),
                env={**ENV, **envs.get(project, {})},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            result[project].append(
                {"roles": group["roles"], "config_verified": child.returncode == 0}
            )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "check"), nargs="?", default="plan")
    parser.add_argument(
        "--variables", type=Path, help="JSON root:root 0600 de interpolación de SupoClip y OTel"
    )
    args = parser.parse_args(argv)
    try:
        data = load_manifest(MANIFEST)
        validate(data, sources=args.action == "plan")
        if args.action == "plan":
            require(args.variables is None, "VARIABLES_INNECESARIAS")
            plan = {
                project: [compose_args(project, spec, group) for group in spec["groups"]]
                for project, spec in data["projects"].items()
            }
            output = {
                "success": True,
                "action": "plan",
                "commands": plan,
                "engine": data["engine"],
                "evidence": data["evidence"],
                "runtime_verified": False,
            }
        else:
            require(args.variables is not None, "VARIABLES_REQUERIDAS")
            groups = check(data, args.variables)
            output = {
                "success": all(row["config_verified"] for rows in groups.values() for row in rows),
                "action": "check",
                "compose": groups,
                "engine": data["engine"],
                "runtime_verified": False,
            }
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ) as error:
        code = str(error) if type(error) is ValueError else "VERIFICACION_FALLIDA"
        output = {
            "success": False,
            "error": code if re.fullmatch(r"[A-Z0-9_]+", code) else "VERIFICACION_FALLIDA",
        }
    print(json.dumps(output, sort_keys=True))
    return 0 if output["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
