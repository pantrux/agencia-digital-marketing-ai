"""SDK nuevo de OpenEXR: contrato explícito, entradas RO y aprobación externa."""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import signal
import stat
import struct
import sys
import tarfile
import time
import types
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

OWNER = "agencia-digital-marketing-ai:opencv-openexr3-sdk:v1"
NAME = "agency-opencv-openexr3-sdk-v1"
MARKER = ".openexr3-sdk-owner.json"
NORMAL_SHA = "77f442612a9c14df8f0685474ce5e3f47c8c0da16e828888ee8250dc1d7e002b"
LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"
AUDIT_SHA = "747a33d04bf5238ccc374cc3ac7cd3b1f49050bf9df5cb668d4191cc851001e1"
HOST_SHA = "b7e79bb8f5364c4eae0d7f36cea4e8850532c158ee245b2a071be6559ea78bb5"
APPROVED_SHA = "1207f5840f3d663775ee54fd377688ef00081681a1a50952df61c5d1ee01cd3a"
PREFIX_SHA = "7ef4c7b3ade622abf647e96e4b276396f7a34e50dbb4d373048500d820b5a53c"
PREFIX_BYTES = 11448320
PREFIX_RECEIPT_SHA = "3ab93de9840369236c9550d94b9aeb62fc2b2c117c0263b43b72330a8246e60c"
DB_SHA = "ae74c96e67281cf18134c2b18db8b35f59b2e9136237845839054b2bb17dc1a8"
INPUT_SCA_SHA = "42b066e199e3b148444dc21f9c40f9b32f3deb9b0bb7ac3ac56142ab56726a52"
INPUT_REPORT_SHA = "8f76eafbae4cfa4dbc54979fab8427b9c790edc0f7c119e254fc5a2e8ee99768"
SBOM_SHA = "9538e95604d10a89cfe4b40337569e67139dc829088be754603bf3fa464141e6"
SCA_DB_SHA = "7520aa80bfb8db4a230514baa510ec6b1d7de9099b74f7879be65450407a6f1f"
BASE_MANIFEST = "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72"
BASE_CONFIG = "sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7"
BASE_REFERENCE = "cgr.dev/chainguard/wolfi-base@" + BASE_MANIFEST
BASE_ENV = [
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin",
    "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
]
BASE_LABELS = {
    "dev.chainguard.image.title": "wolfi-base",
    "dev.chainguard.package.main": "",
    "org.opencontainers.image.authors": "Chainguard Team https://www.chainguard.dev/",
    "org.opencontainers.image.created": "2026-11-05T19:11:38Z",
    "org.opencontainers.image.source": "https://github.com/chainguard-images/images/tree/main/images/wolfi-base",
    "org.opencontainers.image.title": "wolfi-base",
    "org.opencontainers.image.url": "https://images.chainguard.dev/directory/image/wolfi-base/overview",
    "org.opencontainers.image.vendor": "Chainguard",
}
OLD_ROOT = "/opt/media-platform/build/contexts/opencv-zou-minimal/755ea032b52a767fc6ed7e844161407c"
PATHS = {
    "code": "/opt/media-platform/opencv-openexr3-sdk-v1",
    "prefix": "/opt/media-platform/opencv-openexr3-sdk-v1/prefix",
    "state": "/var/lib/media-platform/opencv-openexr3-sdk-v1",
    "control": "/var/lib/media-platform/opencv-openexr3-sdk-v1/control",
    "outputs": "/var/lib/media-platform/opencv-openexr3-sdk-v1/outputs",
    "staging": "/home/jandrade/.agency-openexr3-stage-v1",
    "inputs": OLD_ROOT + "/inputs",
    "source": OLD_ROOT + "/source",
    "numpy_headers": OLD_ROOT + "/numpy-headers",
}
MODES = ("receive", "prepare", "create", "start", "sdk", "approve", "build", "stop")
MAX_OUTPUT = 4 * 1024**2
SHA = re.compile(r"[a-f0-9]{64}")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "JSON ambiguo.")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def minimum_profile():
    return {
        "owner": OWNER,
        "paths": dict(PATHS),
        "prefix_archive_sha256": PREFIX_SHA,
        "runtime_applied": False,
        "name": NAME,
    }


def validate_profile(plan):
    require(
        plan.get("owner") == OWNER
        and plan.get("paths") == PATHS
        and plan.get("prefix_archive_sha256") == PREFIX_SHA
        and plan.get("runtime_applied") is False
        and plan.get("name") == NAME,
        "Perfil fuera del namespace y del prefijo fijados.",
    )


def labels(plan):
    validate_profile(plan)
    return {
        **BASE_LABELS,
        "agencia.owner": OWNER,
        "agencia.role": "opencv-openexr3-sdk",
        "agencia.prefix-sha256": PREFIX_SHA,
    }


def container_environment(plan):
    validate_profile(plan)
    return [*BASE_ENV, "AGENCY_BUILD_OWNER=" + OWNER, "AGENCY_INPUT_MANIFEST_SHA256=" + PREFIX_SHA]


def hold_arguments():
    return ["-c", "trap 'exit 0' TERM INT; while :; do sleep 1; done"]


def mount_contract(plan):
    validate_profile(plan)
    return [
        {"Source": PATHS[key], "Target": target, "ReadOnly": ro}
        for key, target, ro in [
            ("inputs", "/inputs", True),
            ("source", "/source", True),
            ("prefix", "/overlay", True),
            ("numpy_headers", "/numpy-headers", True),
            ("outputs", "/work", False),
        ]
    ]


def require_receipt(stage, actual, backed):
    value = actual.get(stage)
    receipt = value.get("receipt", {}) if isinstance(value, dict) else {}
    require(
        isinstance(receipt, dict)
        and value == backed.get(stage)
        and value == envelope(receipt)
        and receipt.get("stage") == stage
        and receipt.get("success") is True,
        "Falta recibo positivo y respaldado: " + stage,
    )
    return receipt


def cleanup_cid(actual, backed, plan, intent=None):
    require_receipt("prepare", actual, backed)
    if "create" in actual:
        cid = require_receipt("create", actual, backed).get("cid")
    else:
        failed = backed.get("create_unverified", {})
        require(
            intent is not None
            and failed == intent
            and failed.get("verified") is False
            and failed.get("plan_sha256") == plan.get("plan_sha256"),
            "CID no verificado sin intención física y respaldo.",
        )
        cid = failed.get("cid")
    require(SHA.fullmatch(str(cid)) is not None, "CID propio completo requerido.")
    return cid


def archive_payloads(raw, expected):
    require(len(raw) <= 16 * 1024**2, "TAR del prefijo excesivo.")
    result = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            name = member.name
            require(
                name in expected and name not in result and member.uid == member.gid == 0,
                "Miembro de prefijo ajeno o repetido.",
            )
            row = expected[name]
            if row["kind"] == "link":
                require(
                    member.issym()
                    and member.linkname == row["target"]
                    and "/" not in member.linkname
                    and member.linkname not in {".", ".."},
                    "Enlace de prefijo diferente.",
                )
                result[name] = None
            else:
                require(
                    member.isreg()
                    and member.mode == 0o444
                    and member.size == row["size"] <= 64 * 1024**2,
                    "Archivo de prefijo diferente.",
                )
                with archive.extractfile(member) as stream:
                    data = stream.read(member.size + 1)
                require(
                    len(data) == row["size"] and digest(data) == row["sha256"],
                    "Contenido de prefijo no autenticado.",
                )
                result[name] = data
    require(set(result) == set(expected), "Faltan entradas del prefijo.")
    return result


def validate_approval(proof, plan, sdk, observed):
    expected = {
        "kind": "openexr3-sdk-sca-approval-v1",
        "plan_sha256": plan["plan_sha256"],
        "prefix_archive_sha256": PREFIX_SHA,
        "sdk_receipt_sha256": sdk["receipt_sha256"],
        "sdk_artifact_sha256": sdk["receipt"]["sdk_artifact_sha256"],
        "database_sha256": DB_SHA,
        "tools": plan["sdk_entrypoints"],
        "input_sca_receipt_sha256": INPUT_SCA_SHA,
        "input_sca_report_sha256": INPUT_REPORT_SHA,
        "sbom_sha256": SBOM_SHA,
        **{key: observed[key] for key in ("cid", "created", "pid", "started_at")},
    }
    require(
        proof.get("verified") is True
        and proof.get("runtime_approved") is False
        and all(proof.get(key) == value for key, value in expected.items()),
        "La aprobación no acredita esta época SDK/prefijo/SCA.",
    )
    wanted = {
        "installed_apk": 89,
        "partial_apk": 5,
        "numpy": 1,
        "total": 95,
        "high_critical": 0,
        "unknown_or_unscored": 0,
    }
    require(
        proof.get("coverage") == wanted and all(type(v) is int for v in proof["coverage"].values()),
        "Cobertura SCA diferente de 95 identidades positivas.",
    )
    database = proof.get("db", {})
    require(database.get("sha256") == SCA_DB_SHA, "DB distinta del análisis fijado.")
    now = datetime.now(UTC)
    updated = datetime.fromisoformat(database["updated_at"].replace("Z", "+00:00"))
    next_update = datetime.fromisoformat(database["next_update"].replace("Z", "+00:00"))
    require(
        updated.tzinfo is not None
        and next_update.tzinfo is not None
        and updated <= now < next_update,
        "DB sin vigencia positiva.",
    )


def check_window(docker, own_cid=None):
    raw = docker.capture(["ps", "--no-trunc", "--format", "{{.ID}}"], timeout=5)
    ids = raw.decode("ascii").splitlines()
    require(len(ids) <= 128 and len(ids) == len(set(ids)), "Inventario de procesos Docker ambiguo.")
    for cid in ids:
        require(SHA.fullmatch(cid) is not None, "CID de inventario inválido.")
        if cid == own_cid:
            continue
        value = docker.inspect(cid)
        require(value.get("Id") == cid, "Inspect de ventana no corresponde al CID.")
        values = (value.get("Config", {}).get("Labels") or {}).values()
        own_build = any(
            isinstance(v, str)
            and v.startswith("agencia-digital-marketing-ai:")
            and ("sdk" in v or "builder" in v)
            for v in values
        )
        require(not own_build, "Hay otro SDK propio activo; raíz debe serializar la ventana.")


def create_arguments(profile):
    validate_profile(profile)
    result = [
        "create",
        "--pull=never",
        "--name",
        NAME,
        "--user=0:0",
        "--network=none",
        "--cpus=2",
        "--memory=" + str(6 * 1024**3),
        "--memory-swap=" + str(6 * 1024**3),
        "--pids-limit=128",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=67108864,mode=1777",
        "--entrypoint=/bin/sh",
    ]
    for name, value in labels(profile).items():
        result.extend(["--label", name + "=" + value])
    for value in container_environment(profile):
        result.extend(["--env", value])
    for row in mount_contract(profile):
        result.extend(
            [
                "--mount",
                f"type=bind,source={row['Source']},target={row['Target']}"
                + (",readonly" if row["ReadOnly"] else ""),
            ]
        )
    return [*result, BASE_REFERENCE, *hold_arguments()]


def check_container(value, cid, profile):
    validate_profile(profile)
    if (
        not re.fullmatch(r"[a-f0-9]{64}", cid)
        or value.get("Id") != cid
        or value.get("Image") not in {BASE_MANIFEST, BASE_CONFIG}
        or value.get("Name") != "/" + NAME
    ):
        raise ValueError("El CID o su imagen no son propios.")
    config, host = value.get("Config", {}), value.get("HostConfig", {})
    if (
        config.get("Image") != BASE_REFERENCE
        or config.get("User") != "0:0"
        or config.get("Labels") != labels(profile)
        or config.get("Env") != container_environment(profile)
        or config.get("Entrypoint") != ["/bin/sh"]
        or config.get("Cmd") != hold_arguments()
        or config.get("Volumes") not in (None, {})
    ):
        raise ValueError("La configuración del SDK difiere del contrato propio.")
    required = {
        "NanoCpus": 2_000_000_000,
        "Memory": 6 * 1024**3,
        "MemorySwap": 6 * 1024**3,
        "PidsLimit": 128,
        "NetworkMode": "none",
        "Privileged": False,
        "ReadonlyRootfs": False,
    }
    if any(host.get(key) != expected for key, expected in required.items()):
        raise ValueError("Límites o namespaces del SDK no acreditados.")
    if (
        host.get("CapAdd") not in (None, [])
        or host.get("CapDrop") not in (["ALL"], ["CAP_ALL"])
        or host.get("SecurityOpt")
        not in (["no-new-privileges"], ["no-new-privileges=true"], ["no-new-privileges:true"])
        or any(
            host.get(key) not in (None, [], {})
            for key in ["Binds", "PortBindings", "Devices", "DeviceRequests", "Links", "ExtraHosts"]
        )
    ):
        raise ValueError("El SDK tiene privilegios, puertos, dispositivos o binds ajenos.")
    if (
        any(host.get(key) not in (None, "") for key in ["PidMode", "UTSMode", "UsernsMode"])
        or host.get("IpcMode") not in (None, "", "private")
        or host.get("CgroupnsMode") not in (None, "", "private")
        or host.get("PublishAllPorts", False) is not False
        or host.get("VolumesFrom") not in (None, [])
        or host.get("Runtime") not in (None, "", "runc")
    ):
        raise ValueError("El SDK comparte namespaces o recursos del host.")
    expected = {(row["Source"], row["Target"], row["ReadOnly"]) for row in mount_contract(profile)}
    declared = host.get("Mounts", [])
    if (
        len(declared) != 5
        or {(r.get("Source"), r.get("Target"), r.get("ReadOnly", False)) for r in declared}
        != expected
        or any(
            r.get("Type") != "bind"
            or set(r) - {"Type", "Source", "Target", "ReadOnly", "BindOptions", "Consistency"}
            for r in declared
        )
    ):
        raise ValueError("Los mounts declarados no coinciden semánticamente.")
    for row in declared:
        options = row.get("BindOptions") or {}
        if (
            set(options)
            - {
                "Propagation",
                "NonRecursive",
                "CreateMountpoint",
                "ReadOnlyNonRecursive",
                "ReadOnlyForceRecursive",
            }
            or options.get("Propagation", "rprivate") != "rprivate"
            or any(
                options.get(key, False) is not False
                for key in [
                    "NonRecursive",
                    "CreateMountpoint",
                    "ReadOnlyNonRecursive",
                    "ReadOnlyForceRecursive",
                ]
            )
            or row.get("Consistency", "") not in ("", "default")
        ):
            raise ValueError("Opciones de bind no autorizadas.")
    mounted = value.get("Mounts", [])
    if (
        len(mounted) != 5
        or len({r.get("Destination") for r in mounted}) != 5
        or {(r.get("Source"), r.get("Destination"), not r.get("RW", False)) for r in mounted}
        != expected
        or any(r.get("Type") != "bind" or r.get("Propagation") != "rprivate" for r in mounted)
    ):
        raise ValueError("Los mounts efectivos no corresponden a las raíces propias.")
    if host.get("Tmpfs") != {"/tmp": "rw,nosuid,nodev,size=67108864,mode=1777"}:
        raise ValueError("Tmpfs del SDK diferente.")
    state = value.get("State", {})
    return {
        "cid": cid,
        "running": state.get("Running") is True,
        "pid": state.get("Pid"),
        "started_at": state.get("StartedAt"),
        "created": value.get("Created"),
        "status": state.get("Status"),
    }


def module_bytes(name, raw, expected):
    require(len(raw) <= 256 * 1024 and digest(raw) == expected, "Fuente raw distinta del pin.")
    module = types.ModuleType(name)
    module.__file__ = "<" + name + ">"
    sys.modules[name] = module
    exec(compile(raw, module.__file__, "exec"), module.__dict__)
    return module


def host_request(request):
    plan = request["plan"]
    require(plan["sources"]["layout"] == LAYOUT_SHA, "Helper del layout distinto del pin.")
    return {
        "plan": {"layout": plan["layout"], "layout_helper_sha256": LAYOUT_SHA},
        "layout_source_b64": request["layout_b64"],
    }


def validate_attestation(value, plan):
    require(
        value.get("database_sha256") == DB_SHA
        and value.get("tools") == plan["sdk_entrypoints"]
        and value.get("interfaces") == ["lo"],
        "DB física/herramientas/aislamiento distintos del snapshot.",
    )


ATTEST = r"""import hashlib,json,os,stat
from pathlib import Path
def read(path,limit):
 with open(path,'rb') as f:
  before=os.fstat(f.fileno())
  if not stat.S_ISREG(before.st_mode) or before.st_size>limit:raise ValueError('Archivo SDK irregular')
  raw=f.read(limit+1);after=os.fstat(f.fileno())
  if len(raw)!=before.st_size or (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('SDK cambió')
 return raw
sdk=json.loads(read('/work/sdk-receipt.json',262144))
tools=[{'path':r['path'],'sha256':hashlib.sha256(read(r['path'],268435456)).hexdigest()} for r in sdk['tools']]
if any(os.path.lexists(p) for p in ('/usr/bin/git','/bin/git','/usr/local/bin/git','/usr/sbin/git','/sbin/git')):raise ValueError('Git presente')
if not all(os.statvfs(p).f_flag&os.ST_RDONLY for p in ('/inputs','/source','/overlay','/numpy-headers')):raise ValueError('Mounts no RO')
print(json.dumps({'database_sha256':hashlib.sha256(read('/usr/lib/apk/db/installed',4194304)).hexdigest(),'tools':tools,'interfaces':sorted(p.name for p in Path('/sys/class/net').iterdir())}))
"""


def build_arguments(cid):
    require(SHA.fullmatch(str(cid)) is not None, "CID completo requerido.")
    return [
        "exec",
        cid,
        "/usr/bin/bash",
        "-c",
        'umask 077; exec /usr/bin/bash "$@"',
        "openexr3-private-build",
        "/overlay/agency/recipe.sh",
        "--execute",
        "/inputs/data/manifest-approved.json",
        APPROVED_SHA,
        "/inputs/data",
        "/work/openexr3-attempt-v2",
        "/source",
        "/overlay",
        "/numpy-headers",
        "/usr/bin/python3.12",
    ]


def bounded_stage(docker, arguments, budget, sink, cleanup):
    require(20 < budget <= 1800, "Presupuesto de etapa inválido.")
    try:
        result = docker.command(arguments, timeout=budget - 20, sink=sink)
        require(result["returncode"] == 0, "El proceso terminó con error.")
        return result
    except BaseException:
        cleanup()
        raise


def stop_owned(docker, cid, plan):
    before = check_container(docker.inspect(cid), cid, plan)
    stop_error = None
    if before["running"]:
        try:
            docker.command(["stop", "--time", "5", cid], timeout=7)
        except (OSError, TimeoutError, ValueError) as error:
            stop_error = type(error).__name__
    after = check_container(docker.inspect(cid), cid, plan)
    killed = False
    if after["running"]:
        result = docker.command(["kill", "--signal", "KILL", cid], timeout=3)
        require(result["returncode"] == 0, "No se pudo detener el CID propio.")
        killed = True
        after = check_container(docker.inspect(cid), cid, plan)
    require(not after["running"] and after["pid"] == 0, "El CID propio sigue activo.")
    return {
        "stopped": True,
        "killed": killed,
        "cid": cid,
        "observation": after,
        "stop_error_type": stop_error,
    }


def check_plan(plan):
    validate_profile(plan)
    require(
        plan.get("bound_to_vault") is True and plan.get("external_sca_approval_required") is True,
        "Falta respaldo del plan y aprobación externa obligatoria.",
    )
    unsigned = {k: v for k, v in plan.items() if k != "plan_sha256"}
    require(digest(canonical(unsigned)) == plan.get("plan_sha256"), "Plan no canónico.")
    expected_names = {
        "prefix.tar",
        "prefix-receipt.json",
        "audit.json",
        "normal.py",
        "host-proof.json",
        "input-sca.json",
        "launcher.py",
    }
    require(
        set(plan["transfer_files"]) == expected_names and len(plan["prefix_entries"]) == 248,
        "Kit de transferencia distinto del conjunto cerrado.",
    )
    for row in plan["transfer_files"].values():
        require(
            type(row["size"]) is int
            and 0 < row["size"] <= 16 * 1024**2
            and SHA.fullmatch(row["sha256"]) is not None,
            "Input de transferencia inválido.",
        )
    pins = {
        "prefix.tar": PREFIX_SHA,
        "prefix-receipt.json": PREFIX_RECEIPT_SHA,
        "audit.json": AUDIT_SHA,
        "normal.py": NORMAL_SHA,
        "host-proof.json": HOST_SHA,
        "input-sca.json": INPUT_SCA_SHA,
        "launcher.py": plan["sources"]["remote"],
    }
    require(
        all(plan["transfer_files"][k]["sha256"] == v for k, v in pins.items()),
        "Kit contiene una fuente no fijada.",
    )
    for name in plan["prefix_entries"]:
        require(
            re.fullmatch(r"[A-Za-z0-9_.+/-]+", name) is not None
            and not name.startswith("/")
            and all(p not in {"", ".", ".."} for p in name.split("/")),
            "Ruta de prefijo no canónica.",
        )


def receipt_context(value, stage, plan):
    receipt = value.get("receipt", {}) if isinstance(value, dict) else {}
    require(
        value == envelope(receipt)
        and receipt.get("stage") == stage
        and receipt.get("plan_sha256") == plan["plan_sha256"]
        and receipt.get("source_sha256") == plan["sources"]["remote"]
        and receipt.get("owner") == OWNER
        and type(receipt.get("success")) is bool,
        "Recibo ajeno a la etapa, fuente o plan.",
    )
    return receipt


def marker_directory(normal, path, plan, *, create=False, uid=0):
    parent = normal.open_directory(str(PurePosixPath(path).parent), uid=uid)
    fd = None
    try:
        leaf = PurePosixPath(path).name
        if create:
            os.mkdir(leaf, 0o700, dir_fd=parent)
        fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        normal.checked_node(os.fstat(fd), uid, directory=True)
        marker = {
            "owner": OWNER,
            "path": path,
            "directory": normal.identity(os.fstat(fd)),
            "plan_sha256": plan["plan_sha256"],
        }
        if create:
            normal.write_file(fd, MARKER, canonical(marker), uid=uid)
        else:
            require(
                normal.read_file(fd, MARKER, uid=uid) == canonical(marker),
                "Marcador propio diferente.",
            )
        require(
            normal.identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False))
            == normal.identity(os.fstat(fd)),
            "Directorio cambió al abrirlo.",
        )
        return fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        os.close(parent)


def verify_original(normal, plan, audit):
    old = audit["sdk_plan"]
    require(
        old["plan_sha256"] == plan["original_plan_sha256"] and old["remote_sha256"] == NORMAL_SHA,
        "Plan de entradas original diferente.",
    )
    require(
        plan["sdk_entrypoints"]
        == [{"path": row["path"], "sha256": row["sha256"]} for row in old["sdk_entrypoints"]]
        and len(plan["sdk_entrypoints"]) == 6,
        "Herramientas del plan distintas del catálogo autenticado.",
    )
    prepared = audit["backed"]["prepare"]["receipt"]
    for key in ("source", "numpy_headers"):
        require(
            normal.tree_proof(PATHS[key]) == prepared["material_proofs"][key],
            "Fuente o NumPy originales cambiaron.",
        )
    fd = normal.marked_directory(PATHS["inputs"])
    try:
        node = prepared["directories"]["inputs"]
        require(
            normal.identity(os.fstat(fd)) == node["directory"]
            and digest(normal.read_file(fd, normal.MARKER)) == node["marker_sha256"],
            "Identidad del input original diferente.",
        )
        normal.exact_tree(fd, set(old["files"]) | {normal.MARKER, "data/manifest-approved.json"}, 0)
        for name, row in old["files"].items():
            normal.file_digest(fd, name, row)
        parent, leaf = normal.open_parent(fd, "data/manifest-approved.json")
        try:
            approved = normal.read_file(parent, leaf, maximum=2 * 1024**2)
        finally:
            os.close(parent)
        require(digest(approved) == APPROVED_SHA, "Manifiesto aprobado original cambió.")
    finally:
        os.close(fd)
    return old


def verify_prefix(normal, directory, plan):
    expected = plan["prefix_entries"]
    # exact_tree del kit original admite sólo archivos. Este prefijo sí contiene
    # once enlaces basename acreditados por el TAR y no sigue ninguno al caminar.
    wanted = set(expected) | {MARKER}
    parents = {str(p) for name in wanted for p in PurePosixPath(name).parents if str(p) != "."}
    found = set()

    def walk(fd, prefix=""):
        for leaf in os.listdir(fd):
            name = prefix + leaf
            info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                require(name in parents, "Directorio adicional en prefijo.")
                normal.checked_node(info, 0, directory=True)
                child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, name + "/")
                finally:
                    os.close(child)
            else:
                require(name in wanted and name not in found, "Nodo adicional en prefijo.")
                found.add(name)

    walk(directory)
    require(found == wanted, "Prefijo materializado incompleto.")
    for name, row in expected.items():
        parent, leaf = normal.open_parent(directory, name)
        try:
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if row["kind"] == "link":
                require(
                    stat.S_ISLNK(info.st_mode)
                    and info.st_uid == info.st_gid == 0
                    and os.readlink(leaf, dir_fd=parent) == row["target"],
                    "Enlace del prefijo cambió.",
                )
            else:
                normal.file_digest(parent, leaf, row)
        finally:
            os.close(parent)


def materialize_prefix(normal, directory, plan, raw):
    require(
        len(raw) == PREFIX_BYTES and digest(raw) == PREFIX_SHA, "TAR preparado distinto del pin."
    )
    payloads = archive_payloads(raw, plan["prefix_entries"])
    for name, row in sorted(plan["prefix_entries"].items()):
        parent, leaf = normal.open_parent(directory, name, create=True)
        try:
            if row["kind"] == "link":
                os.symlink(row["target"], leaf, dir_fd=parent)
            else:
                normal.write_file(parent, leaf, payloads[name])
        finally:
            os.close(parent)
    verify_prefix(normal, directory, plan)


def receive(request, normal, stream):
    plan = request["plan"]
    normal.verify_host(host_request(request), root=False)
    fd = marker_directory(normal, PATHS["staging"], plan, create=True, uid=1000)
    try:
        for name, row in sorted(plan["transfer_files"].items()):
            raw = normal.exact_read(stream, row["size"])
            require(digest(raw) == row["sha256"], "Transferencia alterada.")
            normal.write_file(fd, name, raw, uid=1000)
        require(not stream.read(1), "Bytes fuera del kit.")
        return envelope(
            {
                "stage": "transfer",
                "success": True,
                "owner": OWNER,
                "plan_sha256": plan["plan_sha256"],
                "source_sha256": plan["sources"]["remote"],
                "directory": normal.identity(os.fstat(fd)),
                "marker_sha256": digest(normal.read_file(fd, MARKER, uid=1000)),
                "files": len(plan["transfer_files"]),
                "path": PATHS["staging"],
            }
        )
    finally:
        os.close(fd)


class Store:
    def __init__(self, normal, plan, *, create=False, control_only=False):
        self.normal, self.plan, self.fds = normal, plan, {}
        if not create:
            for key in (
                ("state", "control")
                if control_only
                else ("code", "state", "control", "outputs", "prefix")
            ):
                self.fds[key] = marker_directory(normal, PATHS[key], plan)

    def close(self):
        for fd in self.fds.values():
            os.close(fd)

    def snapshot(self):
        n = self.normal
        return {
            key: {
                "path": PATHS[key],
                "directory": n.identity(os.fstat(fd)),
                "marker_sha256": digest(n.read_file(fd, MARKER)),
            }
            for key, fd in self.fds.items()
        }

    def put(self, stage, fields):
        receipt = {
            "stage": stage,
            "owner": OWNER,
            "plan_sha256": self.plan["plan_sha256"],
            "source_sha256": self.plan["sources"]["remote"],
            "runtime_applied": False,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            **fields,
        }
        result = envelope(receipt)
        self.normal.write_file(self.fds["control"], stage + ".json", canonical(result))
        return result

    def receipts(self):
        result = {}
        for name in ("prepare", "create", "start", "sdk", "approve", "build", "stop"):
            try:
                raw = self.normal.read_file(self.fds["control"], name + ".json", maximum=1024**2)
            except FileNotFoundError:
                continue
            value = json_object(raw)
            require(raw == canonical(value), "Recibo físico no canónico.")
            receipt_context(value, name, self.plan)
            result[name] = value
        return result

    def verify(self, backed, *, control_only=False):
        prepared = require_receipt("prepare", self.receipts(), backed)
        snapshot = self.snapshot()
        require(
            all(prepared["directories"].get(k) == v for k, v in snapshot.items()),
            "Raíces propias cambiaron desde prepare.",
        )
        if not control_only:
            normal = self.normal
            require(
                set(os.listdir(self.fds["code"]))
                == set(self.plan["transfer_files"]) | {MARKER, "prefix"},
                "Archivos adicionales en código propio.",
            )
            for name, row in self.plan["transfer_files"].items():
                normal.file_digest(self.fds["code"], name, row)
            audit = json_object(
                normal.read_file(self.fds["code"], "audit.json", maximum=2 * 1024**2)
            )
            verify_original(normal, self.plan, audit)
            verify_prefix(normal, self.fds["prefix"], self.plan)

    def docker(self):
        normal = self.normal
        path = PATHS["control"] + "/docker-config"
        fd = normal.open_directory(path)
        try:
            require(
                normal.read_file(fd, "config.json") == b"{}" and os.listdir(fd) == ["config.json"],
                "Configuración Docker propia alterada.",
            )
        finally:
            os.close(fd)
        return normal.Docker(path)

    def prepare(self, request):
        normal, plan = self.normal, self.plan
        transfer = require_receipt("transfer", request["backed"], request["backed"])
        receipt_context(request["backed"]["transfer"], "transfer", plan)
        staging = marker_directory(normal, PATHS["staging"], plan, uid=1000)
        try:
            require(
                normal.identity(os.fstat(staging)) == transfer["directory"]
                and digest(normal.read_file(staging, MARKER, uid=1000))
                == transfer["marker_sha256"],
                "Staging no coincide con respaldo.",
            )
            normal.exact_tree(staging, set(plan["transfer_files"]) | {MARKER}, 1000)
            for name, row in plan["transfer_files"].items():
                normal.file_digest(staging, name, row, uid=1000)
            audit = json_object(
                normal.read_file(staging, "audit.json", maximum=2 * 1024**2, uid=1000)
            )
            verify_original(normal, plan, audit)
            for key in ("code", "state", "control", "outputs", "prefix"):
                self.fds[key] = marker_directory(normal, PATHS[key], plan, create=True)
            for name, row in plan["transfer_files"].items():
                normal.copy_file(staging, self.fds["code"], name, row)
            raw = normal.read_file(self.fds["code"], "prefix.tar", maximum=16 * 1024**2)
            materialize_prefix(normal, self.fds["prefix"], plan, raw)
            for name in ("openexr3-attempt-v2", ".build-host-verify-v1"):
                os.mkdir(name, 0o700, dir_fd=self.fds["outputs"])
            fd = normal.open_directory(PATHS["outputs"] + "/.build-host-verify-v1")
            try:
                proof = normal.read_file(self.fds["code"], "host-proof.json")
                require(digest(proof) == HOST_SHA, "Prueba host original alterada.")
                normal.write_file(fd, "host-proof.json", proof)
            finally:
                os.close(fd)
            os.mkdir("docker-config", 0o700, dir_fd=self.fds["control"])
            fd = normal.open_directory(PATHS["control"] + "/docker-config")
            try:
                normal.write_file(fd, "config.json", b"{}")
            finally:
                os.close(fd)
            result = self.put(
                "prepare",
                {
                    "success": True,
                    "directories": self.snapshot(),
                    "files": 7,
                    "prefix_entries": 248,
                    "docker_executed": False,
                },
            )
            self.verify({"prepare": result})
            return result
        finally:
            os.close(staging)


def validate_sdk_bytes(raw, expected_sha):
    require(
        len(raw) <= 262144 and digest(raw) == expected_sha,
        "Los bytes del artefacto SDK cambiaron desde su respaldo.",
    )


def attest(normal, docker, cid, plan, timeout=20):
    require(0 < timeout <= 20, "Atestación sin presupuesto de cierre.")
    result = docker.command(
        ["exec", cid, "/usr/bin/python3.12", "-I", "-S", "-B", "-c", ATTEST], timeout=timeout
    )
    require(result["returncode"] == 0, "Atestación física del SDK fallida.")
    value = json_object(result["output"])
    validate_attestation(value, plan)
    return value


def execute(request, normal):
    mode, plan = request["mode"], request["plan"]
    stage_deadline = time.monotonic() + (1800 if mode == "build" else 600)
    check_plan(plan)
    require(
        mode in MODES and plan["sources"]["remote"] == request["executing_source_sha256"],
        "Etapa o ejecutor no fijados.",
    )
    normal.verify_host(host_request(request))
    store = Store(normal, plan, create=mode == "prepare", control_only=mode == "stop")
    lock = None
    docker = None
    own_cid = None
    cleanup_allowed = False
    fields = {"success": False, "compiler_executed": False}
    try:
        if mode == "prepare":
            return {"success": True, "result": store.prepare(request)}
        store.verify(request["backed"], control_only=mode == "stop")
        import fcntl

        lock = os.open(
            ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=store.fds["control"]
        )
        normal.checked_node(os.fstat(lock), 0)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        actual = store.receipts()
        docker = store.docker()
        docker.heartbeat_fd = request.get("heartbeat_fd")
        if mode == "stop":
            intent = None
            if "create" not in actual:
                intent = json_object(normal.read_file(store.fds["control"], "created-cid.json"))
            cid = cleanup_cid(actual, request["backed"], plan, intent)
            stopped = stop_owned(docker, cid, plan)
            if "stop" in actual:
                return {"success": True, "result": actual["stop"], "observation": stopped}
            return {"success": True, "result": store.put("stop", {"success": True, **stopped})}
        require(mode not in actual, "Etapa existente: no se repite ni reemplaza.")
        prerequisites = {
            "create": ["prepare"],
            "start": ["prepare", "create"],
            "sdk": ["prepare", "create", "start"],
            "approve": ["prepare", "create", "start", "sdk"],
            "build": ["prepare", "create", "start", "sdk", "approve"],
        }[mode]
        for stage in prerequisites:
            require_receipt(stage, actual, request["backed"])
        require("stop" not in actual, "SDK detenido: no se reinicia en este controlador.")
        os.mkdir("." + mode + "-started", 0o700, dir_fd=store.fds["control"])
        if mode == "create":
            image = json_object(docker.capture(["image", "inspect", BASE_REFERENCE], timeout=5))
            require(isinstance(image, list) and len(image) == 1, "Base local no única.")
            normal.check_base_image(image[0])
            result = docker.command(create_arguments(plan), timeout=20)
            cid = result["output"].decode("ascii").strip()
            require(
                result["returncode"] == 0 and SHA.fullmatch(cid), "Create no devolvió CID completo."
            )
            request["created_cid"] = cid
            intent = {"cid": cid, "verified": False, "plan_sha256": plan["plan_sha256"]}
            normal.write_file(store.fds["control"], "created-cid.json", canonical(intent))
            observed = check_container(docker.inspect(cid), cid, plan)
            require(
                not observed["running"]
                and observed["pid"] == 0
                and observed["status"] == "created",
                "El CID nuevo no quedó sin iniciar.",
            )
            return {
                "success": True,
                "result": store.put(
                    "create",
                    {"success": True, "cid": cid, "verified": True, "observation": observed},
                ),
            }
        cid = actual["create"]["receipt"]["cid"]
        before = check_container(docker.inspect(cid), cid, plan)
        require(
            before["created"] == actual["create"]["receipt"]["observation"]["created"],
            "Nacimiento del CID cambió.",
        )
        if mode == "start":
            require(
                not before["running"] and before["pid"] == 0 and before["status"] == "created",
                "Sólo se inicia un CID nuevo, nunca iniciado.",
            )
            check_window(docker)
            memory = Path("/proc/meminfo").read_text(encoding="ascii")
            available = re.findall(r"^MemAvailable:\s+([0-9]+) kB$", memory, re.M)
            require(
                len(available) == 1 and int(available[0]) >= 8 * 1024**2,
                "Menos de 8 GiB disponibles para la ventana SDK.",
            )
            cleanup_allowed, own_cid = True, cid
            result = docker.command(["start", cid], timeout=10)
            after = check_container(docker.inspect(cid), cid, plan)
            require(
                result["returncode"] == 0 and after["running"] and after["pid"] > 0,
                "No se acreditó el inicio propio.",
            )
            fields.update(
                success=True, cid=cid, observation=after, kernel_limits=docker.limits(after)
            )
            return {"success": True, "result": store.put(mode, fields)}
        started = actual["start"]["receipt"]["observation"]
        require(
            before["running"]
            and all(before[k] == started[k] for k in ("cid", "created", "pid", "started_at")),
            "La época SDK cambió desde su respaldo.",
        )
        check_window(docker, cid)
        docker.limits(before)
        if mode == "approve":
            validate_sdk_bytes(
                normal.read_file(store.fds["outputs"], "sdk-receipt.json", maximum=262144),
                actual["sdk"]["receipt"]["sdk_artifact_sha256"],
            )
            proof_raw = base64.b64decode(request["approval_b64"], validate=True)
            require(
                len(proof_raw) <= 1024**2
                and digest(proof_raw) == request["approval_source_sha256"],
                "Prueba externa diferente del SHA respaldado.",
            )
            proof = json_object(proof_raw)
            validate_approval(proof, plan, actual["sdk"], before)
            attest(normal, docker, cid, plan)
            return {
                "success": True,
                "result": store.put(
                    mode,
                    {
                        "success": True,
                        "cid": cid,
                        "proof": proof,
                        "proof_sha256": digest(canonical(proof)),
                        "proof_source_sha256": digest(proof_raw),
                        "observation": before,
                    },
                ),
            }
        cleanup_allowed, own_cid = True, cid
        deadline = stage_deadline
        fields.update(cid=cid, observation=before, kernel_limits=docker.limits(before))
        if mode == "build":
            validate_sdk_bytes(
                normal.read_file(store.fds["outputs"], "sdk-receipt.json", maximum=262144),
                actual["sdk"]["receipt"]["sdk_artifact_sha256"],
            )
            validate_approval(actual["approve"]["receipt"]["proof"], plan, actual["sdk"], before)
            proof_raw = base64.b64decode(request["approval_b64"], validate=True)
            require(
                len(proof_raw) <= 1024**2
                and digest(proof_raw)
                == request["approval_source_sha256"]
                == actual["approve"]["receipt"]["proof_source_sha256"]
                and json_object(proof_raw) == actual["approve"]["receipt"]["proof"],
                "La aprobación externa cambió desde approve.",
            )
            fields["attestation_before"] = attest(
                normal, docker, cid, plan, timeout=min(20, deadline - time.monotonic() - 20)
            )
            work = normal.open_directory(PATHS["outputs"] + "/openexr3-attempt-v2")
            try:
                require(not os.listdir(work), "No se adoptan salidas de compilación parciales.")
            finally:
                os.close(work)
        fd = os.open(
            mode + ".log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=store.fds["control"],
        )
        try:
            with os.fdopen(fd, "wb", buffering=0, closefd=False) as stream:
                sink = normal.LimitedSink(stream)
                arguments = (
                    [
                        "exec",
                        cid,
                        *normal.apk_arguments(
                            json_object(
                                normal.read_file(
                                    store.fds["code"], "audit.json", maximum=2 * 1024**2
                                )
                            )["sdk_plan"]["managed_packages"]
                        ),
                    ]
                    if mode == "sdk"
                    else build_arguments(cid)
                )
                fields["compiler_executed"] = mode == "build"
                result = bounded_stage(
                    docker,
                    arguments,
                    deadline - time.monotonic(),
                    sink,
                    lambda: fields.update(cleanup=stop_owned(docker, cid, plan)),
                )
                fields["process"] = result["process"]
                if mode == "sdk":
                    audit = json_object(
                        normal.read_file(store.fds["code"], "audit.json", maximum=2 * 1024**2)
                    )
                    old = audit["sdk_plan"]
                    command = [
                        "exec",
                        cid,
                        "/usr/bin/python3.12",
                        "-I",
                        "-B",
                        "/inputs/code/docker/kitsu-zou/opencv_minimal_sdk_remote.py",
                        "--inside-sdk",
                        "--kit-sha256",
                        old["kit_sha256"],
                        "--manifest-sha256",
                        old["sdk_manifest_sha256"],
                    ]
                    verified = bounded_stage(
                        docker,
                        command,
                        deadline - time.monotonic(),
                        sink,
                        lambda: fields.update(cleanup=stop_owned(docker, cid, plan)),
                    )
                    fields["verification_process"] = verified["process"]
                    sdk = normal.read_file(store.fds["outputs"], "sdk-receipt.json", maximum=262144)
                    value = json_object(sdk)
                    require(
                        value.get("verified") is True
                        and len(value.get("installed_packages", {})) == 89
                        and value.get("tools") == plan["sdk_entrypoints"],
                        "Artefacto SDK sin 89 APK y 6 herramientas.",
                    )
                    fields.update(
                        sdk_artifact_sha256=digest(sdk),
                        installed_packages=89,
                        tool_bytes_verified=6,
                        attestation=attest(
                            normal,
                            docker,
                            cid,
                            plan,
                            timeout=min(20, deadline - time.monotonic() - 20),
                        ),
                    )
                else:
                    fields.update(
                        normal.check_build_output(
                            types.SimpleNamespace(
                                paths={"outputs": PATHS["outputs"] + "/openexr3-attempt-v2"}
                            )
                        )
                    )
                    fields["attestation_after"] = attest(
                        normal, docker, cid, plan, timeout=min(20, deadline - time.monotonic() - 20)
                    )
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        after = check_container(docker.inspect(cid), cid, plan)
        require(
            all(after[k] == before[k] for k in ("cid", "created", "pid", "started_at")),
            "Instancia cambió durante etapa.",
        )
        store.verify(request["backed"])
        raw = normal.read_file(store.fds["control"], mode + ".log", maximum=MAX_OUTPUT)
        fields.update(
            success=True,
            log_sha256=digest(raw),
            log_bytes=len(raw),
            log_path=PATHS["control"] + "/" + mode + ".log",
        )
        return {"success": True, "result": store.put(mode, fields)}
    except BaseException as error:
        fields.update(
            error_type=type(error).__name__,
            detail=str(error)[:240],
            process=getattr(error, "process", fields.get("process")),
        )
        if (
            docker is not None
            and cleanup_allowed
            and fields.get("cleanup", {}).get("stopped") is not True
        ):
            try:
                fields["cleanup"] = stop_owned(docker, own_cid, plan)
            except BaseException as cleanup_error:
                fields["cleanup"] = {"stopped": False, "error_type": type(cleanup_error).__name__}
        if "control" in store.fds and mode in {"start", "sdk", "build"}:
            try:
                raw = normal.read_file(store.fds["control"], mode + ".log", maximum=MAX_OUTPUT)
                fields.update(
                    log_sha256=digest(raw),
                    log_bytes=len(raw),
                    log_path=PATHS["control"] + "/" + mode + ".log",
                )
            except FileNotFoundError:
                pass
            return {"success": False, "result": store.put(mode, fields)}
        raise
    finally:
        if lock is not None:
            os.close(lock)
        store.close()


def exact_frame(stream, maximum):
    def exact(size):
        out = bytearray()
        while len(out) < size:
            data = stream.read(size - len(out))
            require(data, "Marco truncado.")
            out.extend(data)
        return bytes(out)

    size = struct.unpack(">I", exact(4))[0]
    require(0 < size <= maximum, "Marco excesivo.")
    return exact(size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, required=True)
    args = parser.parse_args()
    request = {}

    def expired(_signal, _frame):
        raise TimeoutError("Plazo externo de la etapa agotado.")

    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, expired)
    signal.alarm(1840 if args.mode == "build" else 660)
    try:
        stream = sys.stdin.buffer.raw
        normal = module_bytes("openexr3_normal_raw", exact_frame(stream, 256 * 1024), NORMAL_SHA)
        request = json_object(exact_frame(stream, 2 * 1024**2))
        require(request.get("mode") == args.mode, "Etapa del frame diferente del argv.")
        request["executing_source_sha256"] = globals().get("_SOURCE_SHA256")
        check_plan(request["plan"])
        if args.mode == "receive":
            value = {"success": True, "result": receive(request, normal, stream)}
        else:
            request["heartbeat_fd"] = sys.stdin.fileno()
            value = execute(request, normal)
        print(canonical(value).decode(), flush=True)
        return 0 if value["success"] else 1
    except BaseException as error:
        value = {"success": False, "error_type": type(error).__name__, "detail": str(error)[:240]}
        if SHA.fullmatch(str(request.get("created_cid", ""))):
            value["created_cid"] = request["created_cid"]
        print(canonical(value).decode(), flush=True)
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
