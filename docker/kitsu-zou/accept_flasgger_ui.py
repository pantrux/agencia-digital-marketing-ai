"""Aceptación finita de Flasgger; plan local sin secretos ni efectos por defecto."""

import argparse
import base64
import csv
import email.parser
import hashlib
import io
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import types
import zipfile
from pathlib import Path

SOURCE_FILE = Path(__file__).resolve()
# La copia sellada conserva el workspace canónico; Linux sólo usa /fixture y /output.
ROOT = (
    SOURCE_FILE.parents[4]
    if (
        SOURCE_FILE.parent.name == "code"
        and SOURCE_FILE.parent.parent.name == "flasgger-ui-acceptance-v1"
    )
    else SOURCE_FILE.parents[2]
)
OWNER = "agencia-digital-marketing-ai:flasgger-ui-acceptance:v1"
LOCAL_ROOT = ROOT / ".tools/backup-source-build/flasgger-ui-acceptance-v1"
LOCK_PATH = "docker/kitsu-zou/zou-runtime-linux-cp312.lock"
LOCK_SHA = "dd6377b09d7ec1456bf5f34b7ae6e01bfa2d7e4d1c1f9680e76cd8e4d3b3d545"
BASELINE_PATH = ".tools/kitsu-zou-build-flasgger-v2/wheels/flasgger-0.9.7.1-py2.py3-none-any.whl"
BASELINE_SHA = "ee71b3e30ed15c59274d72a27277ae258378a7f28dc50ac17a4526d343c4618f"
DERIVED_PATH = ".tools/kitsu-zou-flasgger-ui-v1/flasgger-0.9.7.1+agency1-py2.py3-none-any.whl"
DERIVED_SHA = "db3bb5145d3d86d82a15c3fca4cd72c665873785c3ca143466962b9747a5355e"
DERIVED_RECEIPT = ".tools/kitsu-zou-flasgger-ui-v1/derived-receipt.json"
DERIVED_RECEIPT_SHA = "0fc3304316ed4dee2dc176136a7bcf019cadbf885e0b5e7f003663bc04ee002c"
RELOCATOR_PATH = "docker/kitsu-zou/prepare_opencv_relocated_wheel.py"
RELOCATOR_SHA = "b7b14e650a0eb900f585591077680fa91a335c0224057ad99cb24e68afd771f6"
JOB_PATH = "sre/windows_build_job.py"
JOB_SHA = "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44"
BROWSER_RELATIVE = "docker/kitsu-zou/accept_flasgger_browser.mjs"
SOURCE_PATHS = [
    "docker/kitsu-zou/accept_flasgger_ui.py",
    BROWSER_RELATIVE,
    "tests/test_flasgger_ui_acceptance.py",
    "tests/test_flasgger_ui_browser.mjs",
    "docker/kitsu-zou/README-flasgger-acceptance.md",
]
DEPENDENCIES = {
    "attrs",
    "blinker",
    "click",
    "flask",
    "itsdangerous",
    "jinja2",
    "jsonschema",
    "jsonschema-specifications",
    "markupsafe",
    "mistune",
    "packaging",
    "pyyaml",
    "referencing",
    "rpds-py",
    "six",
    "typing-extensions",
    "werkzeug",
}
FIXTURE_TITLE = 'Agencia "comillas" & </script><svg id="fixture-title-injection" onload="window.__fixtureTitleExecuted=1">'
FIXTURE_JWT = "fixture-only-no-operational-credential"
MAX_CAPTURE = 1024**2
CALLBACK_FRAGMENT = b"""    const priorOnComplete = uiOptions.onComplete;
    uiOptions.onComplete = function() {
        try {
            if (typeof priorOnComplete === "function") {
                return priorOnComplete.apply(this, arguments);
            }
        } finally {
            updateTitle();
        }
    };
"""
CALLBACK_SHA = "33fab8e4252025d7f3f9a9a1e6dea1ff46a501ae2d8d3f8aeddc3cd45b155a68"
PYTHON_EXECUTABLE_SHA = "54bb87946dd0d8231507c0cef7a5745443a97411c406bf6f010f3ec75780a75b"


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def decode(raw):
    def unique(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "JSON duplicado.")
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8", "strict"),
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


def safe_name(value):
    require(
        isinstance(value, str)
        and value
        and ":" not in value
        and "\\" not in value
        and not value.startswith("/")
        and all(p not in ("", ".", "..") for p in value.split("/")),
        "Ruta relativa insegura.",
    )
    return value


def read_file(path, maximum=8 * 1024**2, expected=None):
    path = Path(path).absolute()
    require(path.resolve(strict=True) == path, "Ruta no canónica.")
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "Enlace/reparse no admitido.",
        )
    before = path.lstat()
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        require(
            all(
                stat.S_ISREG(n.st_mode) and n.st_nlink == 1 and n.st_size <= maximum
                for n in (before, opened)
            ),
            "Archivo no regular, enlazado o excesivo.",
        )
        raw = stream.read(maximum + 1)
        final = os.fstat(stream.fileno())
        after = path.lstat()

    def common(n):
        return n.st_dev, n.st_ino, stat.S_IFMT(n.st_mode), n.st_nlink, n.st_size, n.st_mtime_ns

    require(
        common(before) == common(opened) == common(final) == common(after)
        and before.st_ctime_ns == after.st_ctime_ns
        and opened.st_ctime_ns == final.st_ctime_ns
        and len(raw) == opened.st_size <= maximum,
        "Archivo cambiado durante lectura.",
    )
    require(expected is None or sha(raw) == expected, "SHA de entrada diferente.")
    return raw


def write_new(path, raw):
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    require(read_file(path, max(len(raw), 1)) == raw, "Relectura de salida diferente.")


def create_private(path, owner):
    os.mkdir(path, 0o700)
    node = path.lstat()
    proof = {
        "owner": owner,
        "path": str(path),
        "directory": {"device": node.st_dev, "inode": node.st_ino},
    }
    write_new(path / ".owner.json", canonical(proof))
    return proof


def load_fixed(path, pin, name):
    raw = read_file(path, 256 * 1024, pin)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), vars(module))
    return module


def normalized(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def parse_lock(raw):
    result = {}
    for line in raw.decode("utf-8", "strict").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+!-]+) --hash=sha256:([a-f0-9]{64})",
            line,
        )
        require(match is not None, "Lock fuera de la gramática fijada.")
        name, version, pin = match.groups()
        name = normalized(name)
        require(name not in result, "Lock duplicado.")
        result[name] = {"version": version, "sha256": pin}
    return result


def numeric(value):
    require(
        re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value),
        "La clausura sólo admite releases numéricas observadas.",
    )
    return tuple(int(x) for x in value.split("."))


def compare(left, operator, right):
    a, b = numeric(left), numeric(right)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return {">=": a >= b, ">": a > b, "<=": a <= b, "<": a < b, "==": a == b, "!=": a != b}[
        operator
    ]


def python_compatible(value):
    for item in value.replace(" ", "").split(","):
        if not item:
            continue
        match = re.fullmatch(r"(>=|<=|==|!=|>|<)([0-9]+(?:\.[0-9]+)*)(\.\*)?", item)
        require(match is not None, "Requires-Python fuera del perfil finito.")
        operator, version, wildcard = match.groups()
        if wildcard:
            require(operator in ("==", "!="), "Wildcard Python no admitido.")
            matches = (3, 12, 16)[: len(numeric(version))] == numeric(version)
            require(
                matches if operator == "==" else not matches,
                "Python no satisface exclusión de METADATA.",
            )
        else:
            require(compare("3.12.16", operator, version), "Python no satisface METADATA.")


def active_marker(marker):
    match = re.fullmatch(
        r"""\s*(python_version|platform_system|extra)\s*(==|!=|>=|<=|<|>)\s*['"]([^'"]+)['"]\s*""",
        marker,
    )
    require(match is not None, "Marker no cubierto por este perfil finito.")
    key, operator, value = match.groups()
    if key == "python_version":
        return compare("3.12", operator, value)
    require(operator in ("==", "!="), "Comparación de marker no admitida.")
    actual = "Linux" if key == "platform_system" else ""
    return (actual == value) if operator == "==" else (actual != value)


def requirement(value):
    parts = value.split(";", 1)
    match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)\s*([<>=!0-9., ]*)\s*", parts[0])
    require(match is not None, "Requires-Dist no cubierto; no se resuelve parcialmente.")
    active = len(parts) == 1 or active_marker(parts[1])
    constraints = []
    for condition in match[2].replace(" ", "").split(","):
        if not condition:
            continue
        item = re.fullmatch(r"(>=|<=|==|!=|>|<)([0-9]+(?:\.[0-9]+)*)", condition)
        require(item is not None, "Specifier no cubierto por el perfil finito.")
        constraints.append(item.groups())
    return normalized(match[1]), constraints, active


def close_metadata(candidates):
    pending = ["flasgger"]
    selected = set()
    while pending:
        name = pending.pop()
        require(name in candidates, "Falta una dependencia transitiva fijada.")
        if name in selected:
            continue
        selected.add(name)
        for raw in candidates[name]["requires"]:
            dependency, constraints, active = requirement(raw)
            if not active:
                continue
            require(
                dependency in candidates
                and all(
                    compare(candidates[dependency]["version"], op, ver) for op, ver in constraints
                ),
                "Versión transitiva ausente/incompatible.",
            )
            pending.append(dependency)
    return sorted(selected)


def wheel_files(raw):
    require(0 < len(raw) <= 32 * 1024**2, "Wheel excesivo.")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        rows = archive.infolist()
        require(
            len(rows) <= 10000
            and len({r.filename for r in rows}) == len(rows)
            and sum(r.file_size for r in rows) <= 128 * 1024**2,
            "ZIP duplicado/excesivo.",
        )
        result = {}
        for row in rows:
            safe_name(row.filename.rstrip("/") if row.is_dir() else row.filename)
            if row.is_dir():
                require(
                    row.file_size == 0
                    and stat.S_IFMT(row.external_attr >> 16) in (0, stat.S_IFDIR),
                    "Directorio ZIP con payload/tipo no admitido.",
                )
                continue
            require(
                not row.is_dir()
                and not row.filename.endswith(".pth")
                and not row.flag_bits & 1
                and stat.S_IFMT(row.external_attr >> 16) in (0, stat.S_IFREG)
                and row.file_size <= 32 * 1024**2,
                "Miembro ZIP no admitido.",
            )
            result[row.filename] = archive.read(row)
    return result


def wheel_model(raw):
    files = wheel_files(raw)
    metadata = [n for n in files if n.endswith(".dist-info/METADATA")]
    require(len(metadata) == 1, "METADATA ambiguo.")
    dist = metadata[0].rsplit("/", 1)[0]
    record = dist + "/RECORD"
    require(record in files, "Falta RECORD.")
    rows = list(csv.reader(io.StringIO(files[record].decode("utf-8", "strict"))))
    require(
        len(rows) == len(files)
        and all(len(r) == 3 for r in rows)
        and {r[0] for r in rows} == set(files),
        "RECORD incompleto/duplicado.",
    )
    for name, checksum, size in rows:
        require(
            (name == record and checksum == size == "")
            or (
                name != record
                and size == str(len(files[name]))
                and checksum
                == "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest())
                .decode()
                .rstrip("=")
            ),
            "RECORD diferente.",
        )
    meta = email.parser.BytesParser().parsebytes(files[metadata[0]])
    require(
        len(meta.get_all("Name", [])) == len(meta.get_all("Version", [])) == 1,
        "Identidad METADATA ambigua.",
    )
    require(len(meta.get_all("Requires-Python", [])) <= 1, "Requires-Python ambiguo.")
    python_compatible(meta.get("Requires-Python", ""))
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        directory_entries = sorted(r.filename for r in archive.infolist() if r.is_dir())
    return {
        "name": normalized(meta["Name"]),
        "version": meta["Version"],
        "requires": meta.get_all("Requires-Dist", []),
        "requires_python": meta.get("Requires-Python", ""),
        "directory_entries": directory_entries,
        "metadata_sha256": sha(files[metadata[0]]),
        "members": {n: {"bytes": len(v), "sha256": sha(v)} for n, v in files.items()},
    }, files


def callback_fragment(raw):
    begin = b"    const priorOnComplete = uiOptions.onComplete;\n"
    end = b"    const ui = SwaggerUIBundle(uiOptions);"
    require(raw.count(begin) == raw.count(end) == 1, "Fragmento callback ambiguo.")
    start = raw.index(begin)
    finish = raw.index(end, start)
    fragment = raw[start:finish]
    require(
        fragment == CALLBACK_FRAGMENT and sha(fragment) == CALLBACK_SHA,
        "Callback distinto del fragmento auténtico fijado.",
    )
    return fragment


def same_specs(baseline, derived):
    require(
        canonical(baseline) == canonical(derived),
        "El JSON Swagger/OpenAPI cambió; no se ignoran campos.",
    )


def validate_fixture_result(value, subject, subject_sha):
    require(
        value.get("success") is True
        and value.get("subject") == subject
        and value.get("subject_sha256") == subject_sha
        and value.get("python") == "3.12.16"
        and value.get("fixture_executed") is True
        and all(
            value.get(key) is False
            for key in ("browser_executed", "application_accepted", "sca_approved")
        ),
        "Resultado de fixture no corresponde a su alcance.",
    )
    cases = value.get("cases")
    require(
        type(cases) is list
        and len(cases) == 2
        and {row.get("hide_top_bar") for row in cases} == {False, True}
        and all(type(row.get("hide_top_bar")) is bool for row in cases),
        "Casos hide_top_bar incompletos/ambiguos.",
    )
    for row in cases:
        expected_id = subject + ("-hidden" if row["hide_top_bar"] else "-visible")
        require(
            row.get("id") == expected_id
            and row.get("subject") == subject
            and row.get("jquery_status") == (404 if subject == "derived" else 200),
            "Caso de otro sujeto.",
        )
        require(
            type(row.get("routes")) is list
            and len(row["routes"]) == 4
            and {r.get("path") for r in row["routes"]}
            == {"/apidocs/", "/apispec_1.json", "/oauth2-redirect.html", "/fixture/echo"},
            "Mapa de rutas no cerrado.",
        )
        for route in row["routes"]:
            replay_entry(route)
        require(
            type(row.get("assets")) is list and 0 < len(row["assets"]) <= 16,
            "Catálogo de assets incompleto/excesivo.",
        )
    return value


def replay_entry(row):
    require(
        set(row) == {"path", "status", "content_type", "body_base64", "sha256"},
        "Campos de replay incorrectos.",
    )
    path = row["path"]
    require(
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and "?" not in path
        and "#" not in path
        and "\\" not in path
        and all(p not in (".", "..") for p in path.split("/")),
        "Ruta replay insegura.",
    )
    raw = base64.b64decode(row["body_base64"], validate=True)
    require(
        len(raw) <= 2 * 1024**2
        and sha(raw) == row["sha256"]
        and type(row["status"]) is int
        and row["status"] in (200, 404)
        and isinstance(row["content_type"], str)
        and "\n" not in row["content_type"]
        and "\r" not in row["content_type"],
        "Contenido replay no fijado.",
    )
    return row


def input_data():
    lock_raw = read_file(ROOT / LOCK_PATH, 1024**2, LOCK_SHA)
    lock = parse_lock(lock_raw)
    paths = {"baseline": ROOT / BASELINE_PATH, "derived": ROOT / DERIVED_PATH}
    subjects = {}
    for name, path in paths.items():
        raw = read_file(path, 32 * 1024**2, BASELINE_SHA if name == "baseline" else DERIVED_SHA)
        model, files = wheel_model(raw)
        require(
            model["name"] == "flasgger"
            and model["version"] == ("0.9.7.1" if name == "baseline" else "0.9.7.1+agency1"),
            "Sujeto Flasgger diferente.",
        )
        subjects[name] = {
            "path": str(path),
            "filename": path.name,
            "sha256": sha(raw),
            "bytes": len(raw),
            "model": model,
        }
        if name == "derived":
            callback_fragment(files["flasgger/ui3/templates/flasgger/swagger.html"])
    receipt = decode(read_file(ROOT / DERIVED_RECEIPT, 16384, DERIVED_RECEIPT_SHA))
    require(
        receipt["success"] is True
        and receipt["wheel_sha256"] == DERIVED_SHA
        and receipt["runtime_approved"] is False,
        "Recibo derivado no corresponde.",
    )
    require(
        lock["flasgger"] == {"version": "0.9.7.1", "sha256": BASELINE_SHA},
        "Baseline no corresponde al lock original.",
    )
    candidates = {"flasgger": subjects["baseline"]["model"]}
    dependencies = {}
    cache = ROOT / ".tools/cache/zou-runtime-linux-cp312-v1"
    for path in sorted(cache.glob("*.whl")):
        # Se examina sólo el nombre de los archivos candidatos; cada byte usado exige el hash del lock.
        distribution = normalized(path.name.split("-", 1)[0])
        if distribution not in DEPENDENCIES:
            continue
        require(
            distribution not in dependencies and distribution in lock,
            "Candidato duplicado/no fijado.",
        )
        raw = read_file(path, 32 * 1024**2, lock[distribution]["sha256"])
        model, _files = wheel_model(raw)
        require(
            model["name"] == distribution and model["version"] == lock[distribution]["version"],
            "Wheel no corresponde al lock.",
        )
        dependencies[distribution] = {
            "path": str(path),
            "filename": path.name,
            "bytes": len(raw),
            "sha256": sha(raw),
            "model": model,
        }
        candidates[distribution] = model
    selected = close_metadata(candidates)
    require(set(selected) == DEPENDENCIES | {"flasgger"}, "La clausura observada cambió.")
    require(
        subjects["derived"]["model"]["requires"] == subjects["baseline"]["model"]["requires"],
        "Requires-Dist derivado cambió.",
    )
    return subjects, dependencies


def make_plan():
    subjects, dependencies = input_data()
    sources = {
        name: {
            "sha256": sha(read_file(ROOT / name, 256 * 1024)),
            "bytes": (ROOT / name).stat().st_size,
        }
        for name in SOURCE_PATHS
    }
    require(not os.path.lexists(LOCAL_ROOT), "No se adopta salida existente.")
    return {
        "schema_version": 1,
        "owner": OWNER,
        "root": str(LOCAL_ROOT),
        "lock": {"path": LOCK_PATH, "sha256": LOCK_SHA},
        "subjects": subjects,
        "dependencies": dependencies,
        "sources": sources,
        "callback_fragment_sha256": CALLBACK_SHA,
        "linux": {
            "python": "3.12.16",
            "uid": 1000,
            "gid": 1000,
            "network": "none",
            "read_only_rootfs": True,
            "cpus": 1,
            "memory_bytes": 512 * 1024**2,
            "pids": 32,
            "candidate_base_manifest": "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72",
            "python_present_in_candidate_base": False,
            "runtime_gate": "Requiere runtime Python finito independiente del SDK, clausura y aprobación específica Root respaldada; ce3d sola no basta.",
            "python_executable_sha256": PYTHON_EXECUTABLE_SHA,
            "seconds": 60,
            "combined_output_bytes": MAX_CAPTURE,
        },
        "browser": {
            "edge_path": "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "node_version": "24.13.0",
            "required_root_pins": [
                "edge_sha256",
                "edge_product_version",
                "node_path",
                "node_sha256",
                "linux_capture_sha256",
                "profile_sha256",
            ],
            "seconds": 90,
            "processes": 8,
            "memory_bytes": 2 * 1024**3,
            "output_bytes": MAX_CAPTURE,
            "job_sha256": JOB_SHA,
            "replay_bind": "127.0.0.1",
        },
        "fixture_executed": False,
        "browser_executed": False,
        "runtime_approved": False,
        "sca_approved": False,
        "application_accepted": False,
    }


def prepare_local(plan):
    relocator = load_fixed(ROOT / RELOCATOR_PATH, RELOCATOR_SHA, "flasgger_acceptance_acl_loader")
    access = relocator.access_reader()
    access.check_access(LOCAL_ROOT.parent, private=True)
    require(
        read_file(LOCAL_ROOT.parent / ".agency-backup-build-owner", 4096)
        == b"agencia-digital-marketing-ai:backup-source-build:v1",
        "Padre no acreditado.",
    )
    proof = create_private(LOCAL_ROOT, OWNER)
    access.check_access(LOCAL_ROOT, private=True)
    for directory in ("inputs", "code"):
        (LOCAL_ROOT / directory).mkdir(mode=0o700)
    bundle = {
        "schema_version": 1,
        "owner": OWNER,
        "plan_sha256": sha(canonical(plan)),
        "python": "3.12.16",
        "controller_sha256": plan["sources"][SOURCE_PATHS[0]]["sha256"],
        "subjects": {},
        "dependencies": {},
        "callback_fragment_sha256": CALLBACK_SHA,
    }
    for key, row in {**plan["subjects"], **plan["dependencies"]}.items():
        raw = read_file(Path(row["path"]), 32 * 1024**2, row["sha256"])
        write_new(LOCAL_ROOT / "inputs" / row["filename"], raw)
        bundle["subjects" if key in plan["subjects"] else "dependencies"][key] = {
            k: v for k, v in row.items() if k != "path"
        }
    for name, row in plan["sources"].items():
        write_new(
            LOCAL_ROOT / "code" / Path(name).name, read_file(ROOT / name, 256 * 1024, row["sha256"])
        )
    write_new(
        LOCAL_ROOT / "code" / "windows_build_job.py", read_file(ROOT / JOB_PATH, 65536, JOB_SHA)
    )
    write_new(LOCAL_ROOT / "bundle.json", canonical(bundle))
    write_new(LOCAL_ROOT / "plan.json", canonical(plan))
    for path in LOCAL_ROOT.rglob("*"):
        access.check_access(path, private=True)
    receipt = {
        "success": True,
        "owner": OWNER,
        "root": proof,
        "plan_sha256": sha(canonical(plan)),
        "bundle_sha256": sha(canonical(bundle)),
        "fixture_executed": False,
        "browser_executed": False,
        "sca_approved": False,
        "application_accepted": False,
    }
    write_new(LOCAL_ROOT / "prepare-receipt.json", canonical(receipt))
    return receipt


def execute_prepare(expected):
    plan = make_plan()
    require(sha(canonical(plan)) == expected, "Plan no corresponde al pin externo.")
    return prepare_local(plan)


def linux_context(bundle_pin, proof_pin):
    require(
        os.name == "posix"
        and sys.version_info[:3] == (3, 12, 16)
        and os.geteuid() == os.getegid() == 1000,
        "Runtime Linux/Python/UID no corresponde al perfil.",
    )
    require(
        all(
            isinstance(x, str) and re.fullmatch(r"[a-f0-9]{64}", x) for x in (bundle_pin, proof_pin)
        ),
        "Pins externos obligatorios.",
    )
    fixture = Path("/fixture")
    output = Path("/output")
    require(
        os.statvfs(fixture).f_flag & os.ST_RDONLY, "Los inputs deben estar montados sólo lectura."
    )
    bundle = decode(read_file(fixture / "bundle.json", 2 * 1024**2, bundle_pin))
    proof = decode(read_file(fixture / "linux-proof.json", 4096, proof_pin))
    require(
        isinstance(proof.get("runtime_approval_sha256"), str)
        and re.fullmatch(r"[a-f0-9]{64}", proof["runtime_approval_sha256"]),
        "Falta aprobación específica del runtime de fixture.",
    )
    approval = decode(
        read_file(fixture / "runtime-approval.json", 65536, proof["runtime_approval_sha256"])
    )
    require(
        approval.get("kind") == "flasgger-isolated-runtime-root-release-v1"
        and approval.get("authorized_isolated_fixture") is True
        and approval.get("runtime_closure_verified") is True
        and approval.get("python")
        == {"version": "3.12.16", "path": "/usr/bin/python3.12", "sha256": PYTHON_EXECUTABLE_SHA}
        and re.fullmatch(r"sha256:[a-f0-9]{64}", approval.get("runtime_image_manifest", ""))
        and re.fullmatch(r"[a-f0-9]{64}", approval.get("runtime_security_approval_sha256", "")),
        "Runtime sin procedencia/aprobación Root requerida.",
    )
    require(
        sha(read_file(Path(sys.executable), 32 * 1024**2)) == PYTHON_EXECUTABLE_SHA,
        "Ejecutable Python distinto del SDK/lock autenticado.",
    )
    node = output.lstat()
    require(
        stat.S_ISDIR(node.st_mode)
        and not stat.S_ISLNK(node.st_mode)
        and stat.S_IMODE(node.st_mode) == 0o700
        and node.st_uid == node.st_gid == 1000
        and proof
        == {
            "owner": OWNER,
            "path": "/output",
            "directory": {"device": node.st_dev, "inode": node.st_ino},
            "marker_sha256": sha(read_file(output / ".owner.json", 4096)),
            "runtime_approval_sha256": proof["runtime_approval_sha256"],
        },
        "Prueba externa de salida no coincide.",
    )
    marker = decode(read_file(output / ".owner.json", 4096))
    require(
        marker == {"owner": OWNER, "path": "/output", "directory": proof["directory"]},
        "Marcador Linux distinto.",
    )
    require(
        bundle["owner"] == OWNER
        and bundle["python"] == "3.12.16"
        and bundle["callback_fragment_sha256"] == CALLBACK_SHA
        and sha(read_file(Path(__file__), 256 * 1024)) == bundle["controller_sha256"],
        "Bundle/fuente diferente.",
    )
    require(
        set(bundle["subjects"]) == {"baseline", "derived"}
        and set(bundle["dependencies"]) == DEPENDENCIES,
        "Clausura del bundle incompleta.",
    )
    return bundle, proof, fixture, output


def install_fixture(bundle, subject, fixture, output):
    root = output / ("runtime-" + subject)
    create_private(root, OWNER)
    site = root / "site"
    site.mkdir(mode=0o700)
    names = set()
    for row in [*bundle["dependencies"].values(), bundle["subjects"][subject]]:
        require(
            safe_name(row["filename"]) == Path(row["filename"]).name, "Nombre de wheel no cerrado."
        )
        raw = read_file(fixture / "inputs" / row["filename"], 32 * 1024**2, row["sha256"])
        model, files = wheel_model(raw)
        require(model == row["model"], "Modelo METADATA/RECORD diferente del bundle.")
        for name, data in files.items():
            require(name.casefold() not in names, "Colisión entre wheels.")
            names.add(name.casefold())
            target = site.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for parent in (*target.parent.parents, target.parent):
                require(not parent.is_symlink(), "Padre enlazado durante preparación del fixture.")
            write_new(target, data)
    return root, site


def response_data(client, path):
    with client.get(path) as response:
        body = response.get_data()
        require(len(body) <= 5 * 1024**2, "Respuesta fixture excesiva.")
        return {
            "path": path,
            "status": response.status_code,
            "content_type": response.headers.get("Content-Type", ""),
            "sha256": sha(body),
            "body_base64": base64.b64encode(body).decode(),
        }


def fixture_child(subject, bundle_pin, proof_pin):
    bundle, _proof, fixture, output = linux_context(bundle_pin, proof_pin)
    root, site = install_fixture(bundle, subject, fixture, output)
    # Se añade sólo el site privado que acabamos de autenticar; -I -S evita site/.pth externos.
    sys.path.insert(0, str(site))
    os.chdir(root)

    def deny_operations(event, _args):
        if event in {
            "socket.__new__",
            "subprocess.Popen",
            "os.system",
            "os.exec",
            "os.posix_spawn",
            "os.fork",
        }:
            raise RuntimeError("El fixture Flask no permite red ni procesos.")

    sys.addaudithook(deny_operations)
    from flasgger import Swagger
    from flask import Flask, jsonify

    cases = []
    wheel_row = bundle["subjects"][subject]
    _model, wheel = wheel_model(
        read_file(fixture / "inputs" / wheel_row["filename"], 32 * 1024**2, wheel_row["sha256"])
    )
    if subject == "derived":
        require(
            not any(n.startswith("flasgger/ui2/") for n in wheel)
            and "flasgger/ui3/static/lib/jquery.min.js" not in wheel,
            "UI2/jQuery permanecen en derivada.",
        )
    for hidden in (False, True):
        app = Flask("flasgger_acceptance_fixture")
        app.config.update(
            TESTING=True,
            JWT_AUTH_URL_RULE="/fixture/jwt",
            JWT_AUTH_HEADER_NAME="Authorization",
            JWT_AUTH_HEADER_TYPE="Bearer",
            JWT_AUTH_HEADER_TOKEN="jwt-token",
        )

        @app.get("/fixture/echo")
        def echo():
            """Respuesta sintética de aceptación.
            ---
            operationId: fixtureEcho
            responses:
              200:
                description: Respuesta local sin servicios externos
            """
            return jsonify(ok=True, fixture=True)

        @app.get("/fixture/objects/<object_id>")
        def get_object(object_id):
            """Obtiene un objeto sintético.
            ---
            operationId: fixtureGet
            parameters:
              - name: object_id
                in: path
                required: true
                type: string
              - name: note
                in: query
                type: string
            responses:
              200:
                description: Objeto sintético
                schema:
                  $ref: '#/definitions/FixtureObject'
            """
            return jsonify(id=object_id)

        @app.post("/fixture/objects")
        def create_object():
            """Crea únicamente una respuesta sintética en memoria.
            ---
            operationId: fixturePost
            parameters:
              - name: body
                in: body
                required: true
                schema:
                  $ref: '#/definitions/FixtureObject'
            responses:
              201:
                description: Respuesta sintética
            """
            return jsonify(fixture=True), 201

        callback = "{onComplete:function(){window.__fixtureCallbackCount=(window.__fixtureCallbackCount||0)+1;window.__fixtureCallbackArgs=arguments.length;window.__fixtureCallbackThisType=typeof this;return 'fixture-return';}}"
        config = {
            "uiversion": 3,
            "title": FIXTURE_TITLE,
            "hide_top_bar": hidden,
            "auth": "{}",
            "ui_params_text": callback,
            "specs": [
                {
                    "endpoint": "apispec_1",
                    "route": "/apispec_1.json",
                    "rule_filter": lambda rule: rule.rule.startswith("/fixture/"),
                }
            ],
            "headers": [],
            "static_url_path": "/flasgger_static",
            "swagger_ui": True,
            "specs_route": "/apidocs/",
        }
        template = {
            "swagger": "2.0",
            "info": {"title": "Aceptación Flasgger", "version": "fixture-v1"},
            "basePath": "/",
            "definitions": {
                "FixtureObject": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                }
            },
        }
        Swagger(app, config=config, template=template)
        with app.test_client() as client:
            html = response_data(client, "/apidocs/")
            api = response_data(client, "/apispec_1.json")
            oauth = response_data(client, "/oauth2-redirect.html")
            require(
                html["status"] == api["status"] == oauth["status"] == 200,
                "Flask no sirve documentación/spec/redirect.",
            )
            spec = decode(base64.b64decode(api["body_base64"]))
            require(
                set(spec["paths"])
                == {"/fixture/echo", "/fixture/objects/{object_id}", "/fixture/objects"},
                "Endpoints Swagger incompletos.",
            )
            routes = [html, api, oauth, response_data(client, "/fixture/echo")]
            assets = []
            for member in sorted(wheel):
                if not member.startswith("flasgger/ui3/static/") or not member.endswith(
                    (".js", ".css", ".png")
                ):
                    continue
                url = "/flasgger_static/" + member.removeprefix("flasgger/ui3/static/")
                row = response_data(client, url)
                require(
                    row["status"] == 200 and row["sha256"] == sha(wheel[member]),
                    "Asset Flask distinto de bytes del wheel.",
                )
                assets.append(
                    {k: v for k, v in row.items() if k != "body_base64"} | {"wheel_member": member}
                )
            jquery = response_data(client, "/flasgger_static/lib/jquery.min.js")
            require(
                jquery["status"] == (404 if subject == "derived" else 200),
                "Estado jQuery distinto del sujeto.",
            )
            cases.append(
                {
                    "id": subject + ("-hidden" if hidden else "-visible"),
                    "subject": subject,
                    "hide_top_bar": hidden,
                    "expected_spec": spec,
                    "routes": routes,
                    "assets": assets,
                    "jquery_status": jquery["status"],
                    "ui2_members_absent": subject == "derived",
                }
            )
    result = {
        "success": True,
        "subject": subject,
        "cases": cases,
        "subject_sha256": wheel_row["sha256"],
        "python": "3.12.16",
        "fixture_executed": True,
        "browser_executed": False,
        "application_accepted": False,
        "sca_approved": False,
    }
    require(len(canonical(result)) <= MAX_CAPTURE, "Captura Flask excesiva.")
    return result


def bounded_child(arguments, deadline):
    require(os.name == "posix" and hasattr(os, "WNOWAIT"), "Se necesita retención real Linux.")
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/output",
            "TMPDIR": "/output/tmp",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        close_fds=True,
    )
    selected = selectors.DefaultSelector()
    buffers = {"out": bytearray(), "err": bytearray()}
    owned = True
    status = None
    try:
        for name, stream in (("out", process.stdout), ("err", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selected.register(stream, selectors.EVENT_READ, name)
        while selected.get_map():
            require(time.monotonic() < deadline, "Fixture hijo agotó presupuesto.")
            for key, _mask in selected.select(min(0.05, max(0, deadline - time.monotonic()))):
                data = os.read(key.fd, 65536)
                if not data:
                    selected.unregister(key.fileobj)
                else:
                    buffers[key.data].extend(data)
                    require(
                        sum(map(len, buffers.values())) <= MAX_CAPTURE,
                        "Salida de fixture excesiva.",
                    )
        status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        while status is None and time.monotonic() < deadline:
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        require(
            status is not None and status.si_code == os.CLD_EXITED and status.si_status == 0,
            "Fixture hijo no terminó íntegro.",
        )
    finally:
        selected.close()
        try:
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            owned = False
        if owned:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            stream.close()
    require(not buffers["err"], "Diagnóstico inesperado del fixture.")
    return decode(bytes(buffers["out"]))


def linux_capture(bundle_pin, proof_pin):
    bundle, proof, _fixture, output = linux_context(bundle_pin, proof_pin)
    require(
        {x.name for x in output.iterdir()} == {".owner.json"},
        "No se repite ni adopta una captura previa.",
    )
    write_new(
        output / "capture-intent.json",
        canonical({"bundle_sha256": bundle_pin, "proof_sha256": proof_pin}),
    )
    (output / "tmp").mkdir(mode=0o700)
    deadline = time.monotonic() + 56
    results = {}
    try:
        for subject in ("baseline", "derived"):
            results[subject] = bounded_child(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-W",
                    "error::ResourceWarning",
                    str(Path(__file__).absolute()),
                    "--fixture-child",
                    subject,
                    "--bundle-sha256",
                    bundle_pin,
                    "--proof-sha256",
                    proof_pin,
                ],
                deadline,
            )
            validate_fixture_result(
                results[subject], subject, bundle["subjects"][subject]["sha256"]
            )
            write_new(output / (subject + ".json"), canonical(results[subject]))
        for hidden in (False, True):
            before = next(
                c["expected_spec"]
                for c in results["baseline"]["cases"]
                if c["hide_top_bar"] is hidden
            )
            after = next(
                c["expected_spec"]
                for c in results["derived"]["cases"]
                if c["hide_top_bar"] is hidden
            )
            same_specs(before, after)
        current = output.lstat()
        require(
            proof["directory"] == {"device": current.st_dev, "inode": current.st_ino},
            "Identidad de salida Linux cambió.",
        )
        result = {
            "success": True,
            "owner": OWNER,
            "proof_sha256": proof_pin,
            "bundle_sha256": bundle_pin,
            "directory": proof["directory"],
            "python": "3.12.16",
            "results": results,
            "specs_identical": True,
            "fixture_executed": True,
            "browser_executed": False,
            "application_accepted": False,
            "sca_approved": False,
        }
        require(len(canonical(result)) <= MAX_CAPTURE, "Captura combinada excesiva.")
        write_new(output / "capture.json", canonical(result))
        return result
    except BaseException as error:
        write_new(
            output / "failure.json",
            canonical(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "application_accepted": False,
                }
            ),
        )
        raise


def browser_operation(profile_path, profile_pin):
    require(
        os.name == "nt"
        and isinstance(profile_pin, str)
        and re.fullmatch(r"[a-f0-9]{64}", profile_pin),
        "Perfil explícito Windows obligatorio.",
    )
    profile = decode(read_file(profile_path, 65536, profile_pin))
    require(
        set(profile)
        == {
            "schema_version",
            "owner",
            "root_proof_sha256",
            "capture_path",
            "capture_sha256",
            "node_path",
            "node_sha256",
            "edge_path",
            "edge_sha256",
            "edge_product_version",
            "browser_source_sha256",
            "controller_sha256",
            "plan_sha256",
        }
        and profile["schema_version"] == 1
        and profile["owner"] == OWNER,
        "Perfil browser fuera del contrato.",
    )
    relocator = load_fixed(ROOT / RELOCATOR_PATH, RELOCATOR_SHA, "flasgger_browser_acl_loader")
    access = relocator.access_reader()
    access.check_access(LOCAL_ROOT, private=True)
    root_proof = decode(read_file(LOCAL_ROOT / ".owner.json", 4096, profile["root_proof_sha256"]))
    node = LOCAL_ROOT.lstat()
    require(
        root_proof
        == {
            "owner": OWNER,
            "path": str(LOCAL_ROOT),
            "directory": {"device": node.st_dev, "inode": node.st_ino},
        },
        "Raíz browser distinta de la preparada.",
    )
    plan = decode(read_file(LOCAL_ROOT / "plan.json", 2 * 1024**2))
    require(sha(canonical(plan)) == profile["plan_sha256"], "Plan browser cambió.")
    require(
        sha(read_file(Path(__file__), 256 * 1024))
        == profile["controller_sha256"]
        == plan["sources"][SOURCE_PATHS[0]]["sha256"],
        "Controlador no fijado.",
    )
    capture = decode(
        read_file(Path(profile["capture_path"]), MAX_CAPTURE, profile["capture_sha256"])
    )
    bundle = decode(read_file(LOCAL_ROOT / "bundle.json", 2 * 1024**2))
    require(
        capture["success"] is True
        and capture["specs_identical"] is True
        and capture["bundle_sha256"] == sha(canonical(bundle))
        and capture["python"] == "3.12.16",
        "Captura Linux no ligada al bundle.",
    )
    require(set(capture["results"]) == {"baseline", "derived"}, "Captura sin ambos sujetos.")
    for subject in ("baseline", "derived"):
        validate_fixture_result(
            capture["results"][subject], subject, bundle["subjects"][subject]["sha256"]
        )
    for hidden in (False, True):
        same_specs(
            next(
                c["expected_spec"]
                for c in capture["results"]["baseline"]["cases"]
                if c["hide_top_bar"] is hidden
            ),
            next(
                c["expected_spec"]
                for c in capture["results"]["derived"]["cases"]
                if c["hide_top_bar"] is hidden
            ),
        )
    require(
        profile["edge_path"] == plan["browser"]["edge_path"]
        and Path(profile["node_path"]).is_absolute(),
        "Ruta de ejecutable no corresponde al perfil.",
    )
    for path, pin in (
        (profile["edge_path"], profile["edge_sha256"]),
        (profile["node_path"], profile["node_sha256"]),
    ):
        relocator.read_regular(Path(path), 512 * 1024**2)
        require(
            sha(read_file(Path(path), 512 * 1024**2)) == pin, "Ejecutable browser distinto del pin."
        )
    browser_source = LOCAL_ROOT / "code" / Path(BROWSER_RELATIVE).name
    read_file(browser_source, 256 * 1024, profile["browser_source_sha256"])
    require(
        profile["browser_source_sha256"] == plan["sources"][BROWSER_RELATIVE]["sha256"],
        "Cliente CDP no fijado.",
    )
    output = LOCAL_ROOT / "browser-v1"
    proof = create_private(output, OWNER)
    access.check_access(output, private=True)
    payloads = {}
    cases = []
    for subject in ("baseline", "derived"):
        row = bundle["subjects"][subject]
        _model, wheel = wheel_model(
            read_file(LOCAL_ROOT / "inputs" / row["filename"], 32 * 1024**2, row["sha256"])
        )
        for item in capture["results"][subject]["cases"]:
            require(item["subject"] == subject, "Caso de otro sujeto.")
            routes = []
            for route in item["routes"]:
                replay_entry(route)
                data = base64.b64decode(route["body_base64"], validate=True)
                payloads[sha(data)] = base64.b64encode(data).decode()
                routes.append({k: v for k, v in route.items() if k != "body_base64"})
            for asset in item["assets"]:
                require(asset["wheel_member"] in wheel, "Asset fuera del wheel.")
                data = wheel[asset["wheel_member"]]
                require(
                    sha(data) == asset["sha256"]
                    and asset["status"] == 200
                    and asset["wheel_member"].startswith("flasgger/ui3/static/")
                    and asset["path"]
                    == "/flasgger_static/"
                    + asset["wheel_member"].removeprefix("flasgger/ui3/static/"),
                    "Asset de captura no coincide con wheel/ruta.",
                )
                payloads[sha(data)] = base64.b64encode(data).decode()
                routes.append({k: v for k, v in asset.items() if k != "wheel_member"})
            cases.append(
                {k: item[k] for k in ("id", "subject", "hide_top_bar", "expected_spec")}
                | {"routes": routes}
            )
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    require("SystemRoot" in environment, "Entorno Windows inválido.")
    environment.update(
        PATH=str(Path(environment["SystemRoot"]) / "System32"),
        HOME=str(output),
        TEMP=str(output),
        TMP=str(output),
    )
    browser_input = {
        "schema_version": 1,
        "owner": OWNER,
        "output": str(output),
        "edge_path": profile["edge_path"],
        "edge_sha256": profile["edge_sha256"],
        "edge_product_version": profile["edge_product_version"],
        "browser_environment": environment,
        "cases": cases,
        "payloads": payloads,
        "fixture_title": FIXTURE_TITLE,
        "application_accepted": False,
    }
    encoded = canonical(browser_input)
    require(len(encoded) <= 16 * 1024**2, "Replay excesivo.")
    write_new(output / "input.json", encoded)
    job = load_fixed(
        LOCAL_ROOT / "code/windows_build_job.py", JOB_SHA, "flasgger_browser_native_job"
    )
    result = job.run_job(
        Path(profile["node_path"]),
        [str(browser_source), "--run", str(output / "input.json"), sha(encoded)],
        cwd=output,
        env=environment,
        timeout=90,
        limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
        output_limit=MAX_CAPTURE,
    )
    write_new(output / "stdout.json", result.stdout)
    write_new(output / "stderr.log", result.stderr)
    require(
        result.status == "completed"
        and result.exit_code == 0
        and result.cleanup_complete
        and not result.stderr,
        "Browser no completó con limpieza íntegra.",
    )
    value = decode(result.stdout)
    require(
        value["success"] is True
        and value["application_accepted"] is False
        and len(value["results"]) == 4,
        "Respuesta browser incompleta.",
    )
    receipt = {
        "success": True,
        "owner": OWNER,
        "profile_sha256": profile_pin,
        "directory": proof,
        "capture_sha256": profile["capture_sha256"],
        "result": value,
        "job": {
            "status": result.status,
            "exit_code": result.exit_code,
            "cleanup_complete": result.cleanup_complete,
            "elapsed_seconds": result.elapsed_seconds,
        },
        "browser_executed": True,
        "fixture_executed": True,
        "application_accepted": False,
        "sca_approved": False,
    }
    write_new(output / "receipt.json", canonical(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--linux-capture", action="store_true")
    parser.add_argument("--bundle-sha256")
    parser.add_argument("--proof-sha256")
    parser.add_argument("--fixture-child", choices=("baseline", "derived"))
    parser.add_argument("--browser-profile")
    parser.add_argument("--profile-sha256")
    args = parser.parse_args(argv)
    try:
        require(
            sum(
                bool(x)
                for x in (
                    args.prepare,
                    args.linux_capture,
                    args.fixture_child,
                    args.browser_profile,
                )
            )
            <= 1,
            "Modos excluyentes.",
        )
        if args.prepare:
            result = execute_prepare(args.expected_plan_sha256)
        elif args.fixture_child:
            result = fixture_child(args.fixture_child, args.bundle_sha256, args.proof_sha256)
        elif args.linux_capture:
            result = linux_capture(args.bundle_sha256, args.proof_sha256)
        elif args.browser_profile:
            result = browser_operation(Path(args.browser_profile), args.profile_sha256)
        else:
            plan = make_plan()
            result = {"plan": plan, "plan_sha256": sha(canonical(plan))}
        print(canonical(result).decode())
        return 0
    except Exception as error:
        print(
            canonical(
                {
                    "success": False,
                    "error": "FLASGGER_ACCEPTANCE_REJECTED",
                    "error_type": type(error).__name__,
                    "detail": str(error)[:200],
                    "runtime_approved": False,
                }
            ).decode()
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
