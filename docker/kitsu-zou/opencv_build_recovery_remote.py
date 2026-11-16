"""Recuperación propia OpenCV: preflight host acreditado, sin instalar SDK."""

import base64
import contextlib
import hashlib
import json
import os
import signal
import sys
import time
import types
from datetime import UTC, datetime

OWNER = "agencia-digital-marketing-ai:opencv-zou-build-host-verify:v1"
NAME = "build-host-verify-v1"
DATA_NAME = ".build-host-verify-v1"
CID = "1be1ec1148e2a85b638f1c8f9003726acde7f65a1421e220930aa3c494a9907b"
ORIGINAL_SHA = "77f442612a9c14df8f0685474ce5e3f47c8c0da16e828888ee8250dc1d7e002b"
RECIPE_SHA = "fc3341fa0c8c20ad7bc2ab9b858f127cd0e0ec76b233f05273ef0df2d45aed7f"
PACKAGER_SHA = "831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9"
APPROVED_SHA = "1207f5840f3d663775ee54fd377688ef00081681a1a50952df61c5d1ee01cd3a"
HOST_PROOF_SHA = "b7e79bb8f5364c4eae0d7f36cea4e8850532c158ee245b2a071be6559ea78bb5"
FAILED_SHA = "52a01b20df6e46282f35143fe4b90ab3b8f505847a0c37acd3f5901428578881"
DB_SHA = "ae74c96e67281cf18134c2b18db8b35f59b2e9136237845839054b2bb17dc1a8"
HOST_SUMMARY = {
    "managed_apks": 70,
    "direct_header_apks": 2,
    "edges": 271,
    "signature_mode": "index-rsa256-control-q1-payload-sha256",
    "sdk_executed": False,
    "native_solver_confirmation_pending": True,
}
MODES = {"verify-host", "observe-stopped", "restart", "build", "stop"}
BUDGETS = {"verify-host": 180, "observe-stopped": 90, "restart": 90, "build": 1800, "stop": 30}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def envelope(value):
    return {"receipt": value, "receipt_sha256": digest(canonical(value))}


def module_bytes(name, raw, expected):
    require(digest(raw) == expected, "La fuente de soporte no corresponde al pin.")
    module = types.ModuleType(name)
    module.__file__ = "<soporte-fijado-por-bytes>"
    sys.modules[name] = module
    exec(compile(raw, module.__file__, "exec"), module.__dict__)
    return module


def derive_recipe(raw):
    require(digest(raw) == RECIPE_SHA, "La receta original no corresponde al pin.")
    old_dir = b'recipe_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)'
    old_verify = b'"$controller_python" "$recipe_dir/package_opencv_minimal.py" --manifest "$manifest" --manifest-sha256 "$manifest_sha" --inputs-root "$inputs" --verify-inputs'
    replacement = (
        """"$controller_python" - /work/.build-host-verify-v1/host-proof.json "$manifest_sha" <<'HOST_PROOF'
import hashlib,json,sys
from pathlib import Path
with Path(sys.argv[1]).open('rb') as stream:raw=stream.read(16385)
if len(raw)>16384 or hashlib.sha256(raw).hexdigest()!='HOST_SHA':raise SystemExit('La prueba host no corresponde al respaldo externo.')
value=json.loads(raw)
if value.get('success') is not True or value.get('manifest_sha256')!=sys.argv[2] or value.get('packager_sha256')!='PACKAGER_SHA' or value.get('summary',{}).get('signature_mode')!='index-rsa256-control-q1-payload-sha256':raise SystemExit('La prueba host no acredita el preflight RSA completo.')
HOST_PROOF""".replace("HOST_SHA", HOST_PROOF_SHA)
        .replace("PACKAGER_SHA", PACKAGER_SHA)
        .encode()
    )
    require(raw.count(old_dir) == raw.count(old_verify) == 1, "El delta de receta no es único.")
    result = raw.replace(old_dir, b"recipe_dir=/inputs/code/docker/kitsu-zou").replace(
        old_verify, replacement
    )
    marker = b"# SDK ya preparado"
    require(
        result[result.index(marker) :] == raw[raw.index(marker) :],
        "El bloque CMake/empaquetado cambió.",
    )
    return result


def validate_host_proof(value, plan, history):
    require(
        value.get("kind") == "opencv-inputs-host-signature-verification-v1"
        and value.get("success") is True
        and value.get("plan_sha256") == plan["plan_sha256"]
        and value.get("kit_sha256") == plan["kit_sha256"]
        and value.get("manifest_sha256") == APPROVED_SHA
        and value.get("packager_sha256") == PACKAGER_SHA
        and value.get("source_sha256") == ORIGINAL_SHA
        and value.get("approve_receipt_sha256") == history["approve"]["receipt_sha256"]
        and value.get("prepare_receipt_sha256") == history["prepare"]["receipt_sha256"]
        and value.get("process", {}).get("returncode") == 0
        and value.get("summary") == HOST_SUMMARY
        and all(
            value.get(k) is False
            for k in ("container_mutated", "sdk_executed", "sdk_packages_changed")
        ),
        "La evidencia host no acredita manifiesto, firmas, clausura y respaldo originales.",
    )


def require_stage(stage, actual, backed, profile_sha):
    value = actual.get("receipt", {}) if isinstance(actual, dict) else {}
    require(
        actual == backed
        and actual == envelope(value)
        and value.get("stage") == stage
        and value.get("success") is True
        and value.get("owner") == OWNER
        and value.get("profile_sha256") == profile_sha,
        "Falta un recibo positivo, canónico y respaldado: " + stage,
    )
    return value


def prerequisites(mode):
    require(mode in MODES, "Etapa no admitida.")
    return {
        "verify-host": [],
        "observe-stopped": ["verify-host"],
        "restart": ["verify-host", "observe-stopped"],
        "build": ["verify-host", "observe-stopped", "restart"],
        "stop": [],
    }[mode]


def no_partials(names):
    require(
        not set(names) & {"build", "stage", "wheels", "home", "tmp"},
        "Existe una salida parcial; no se adopta ni elimina.",
    )


def stopped(observed, origin):
    require(
        observed.get("cid") == origin.get("cid") == CID
        and observed.get("created") == origin.get("created")
        and observed.get("running") is False
        and observed.get("pid") == 0
        and observed.get("status") == "exited",
        "El CID propio debe conservar nacimiento y estar detenido/PID 0.",
    )


def validate_sca(value, sdk_sha, now=None):
    now = now or datetime.now(UTC)
    metadata = value.get("db_metadata", {})
    updated = datetime.fromisoformat(metadata["UpdatedAt"].replace("Z", "+00:00"))
    expiry = datetime.fromisoformat(metadata["NextUpdate"].replace("Z", "+00:00"))
    require(
        value.get("verified") is True
        and value.get("sdk_receipt_sha256") == sdk_sha
        and value.get("high_critical") == []
        and value.get("coverage_complete") is True
        and value.get("source_identity_verified") is True
        and value.get("operating_system_verified") is True
        and value.get("source_identity_coverage", {}).get("apk") == 91
        and value.get("source_identity_coverage", {}).get("numpy") == 1
        and updated.tzinfo is not None
        and expiry.tzinfo is not None
        and updated <= now < expiry,
        "La SCA real no tiene cobertura positiva y vigencia ligadas al SDK.",
    )


def validate_attestation(actual, expected, database_sha):
    require(
        len(expected["installed_packages"]) == 89
        and len(expected["tools"]) == 6
        and actual.get("installed_packages") == expected["installed_packages"]
        and actual.get("tools") == expected["tools"]
        and actual.get("database_sha256") == database_sha
        and actual.get("network_interfaces") == ["lo"]
        and actual.get("read_only") is True
        and actual.get("compiler_executed") is False,
        "La atestación de sólo lectura no conserva los 89 APK y seis herramientas.",
    )


def build_timeout(deadline, now=None):
    available = deadline - (time.monotonic() if now is None else now) - 20
    if available <= 0:
        raise TimeoutError("No queda presupuesto con reserva de 20 s para cleanup.")
    return min(1780, available)


def bounded_build(docker, arguments, sink, cleanup, *, timeout=1780):
    require(0 < timeout <= 1780, "Plazo de compilación no admitido.")
    try:
        result = docker.command(arguments, timeout=timeout, sink=sink)
        require(result["returncode"] == 0, "La compilación terminó con error.")
        return result
    except BaseException:
        cleanup()
        raise


@contextlib.contextmanager
def operation_lock(normal, directory):
    import fcntl

    fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        normal.checked_node(os.fstat(fd), 0)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


ATTEST = r"""import hashlib,json,os,stat,sys
from pathlib import Path
def read(path,maximum):
 with open(path,'rb') as f:
  s=os.fstat(f.fileno())
  if not stat.S_ISREG(s.st_mode) or s.st_size>maximum:raise ValueError('Archivo SDK irregular/excesivo')
  raw=f.read(maximum+1)
 if len(raw)!=s.st_size:raise ValueError('Archivo SDK cambió')
 return raw
expected=json.loads(read('/work/sdk-receipt.json',262144))
db=read('/usr/lib/apk/db/installed',4194304)
packages={}
for block in db.decode().split('\n\n'):
 fields={}
 for line in block.splitlines():
  if len(line)>1 and line[1]==':' and line[0] in ('P','V'):
   if line[0] in fields:raise ValueError('DB ambigua')
   fields[line[0]]=line[2:]
 if not fields:continue
 if set(fields)!={'P','V'} or fields['P'] in packages:raise ValueError('DB ambigua')
 packages[fields['P']]=fields['V']
tools=[{'path':r['path'],'sha256':hashlib.sha256(read(r['path'],268435456)).hexdigest()} for r in expected['tools']]
if any(os.path.lexists(p) for p in ('/usr/bin/git','/bin/git','/usr/local/bin/git','/usr/sbin/git','/sbin/git')):raise ValueError('Git presente')
print(json.dumps({'installed_packages':packages,'tools':tools,'database_sha256':hashlib.sha256(db).hexdigest(),'network_interfaces':sorted(p.name for p in Path('/sys/class/net').iterdir()),'read_only':all(os.statvfs(p).f_flag&os.ST_RDONLY for p in ('/inputs','/source','/overlay','/numpy-headers')),'compiler_executed':False}))
"""


def attest(normal, docker, store):
    expected_raw = normal.read_file(
        store.descriptors["outputs"], "sdk-receipt.json", maximum=262144
    )
    expected = normal.json_object(expected_raw)
    result = docker.command(
        ["exec", CID, "/usr/bin/python3.12", "-I", "-S", "-B", "-c", ATTEST], timeout=20
    )
    require(result["returncode"] == 0, "La atestación SDK no terminó correctamente.")
    value = normal.json_object(result["output"])
    validate_attestation(value, expected, DB_SHA)
    return {
        "data": value,
        "sdk_artifact_sha256": digest(expected_raw),
        "process": result["process"],
    }


def check_root(profile, plan):
    expected = {
        "control": plan["paths"]["control"] + "/" + NAME,
        "data": plan["paths"]["outputs"] + "/" + DATA_NAME,
    }
    require(
        profile.get("paths") == expected and profile.get("owner") == OWNER,
        "Rutas o propietario ajenos.",
    )


def open_own(normal, parent, name, profile_sha, *, create=False):
    if create:
        os.mkdir(name, 0o700, dir_fd=parent)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        normal.checked_node(os.fstat(fd), 0, directory=True)
        marker = {
            "owner": OWNER,
            "directory": normal.identity(os.fstat(fd)),
            "profile_sha256": profile_sha,
        }
        if create:
            normal.write_file(fd, ".owner.json", canonical(marker))
        else:
            require(
                normal.read_file(fd, ".owner.json") == canonical(marker),
                "El namespace de recuperación no conserva su identidad.",
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def verify_recovery_data(normal, store, profile, profile_sha, proof, own_fd):
    require(
        normal.identity(os.fstat(own_fd)) == proof["recovery_directory"]
        and digest(normal.read_file(own_fd, ".owner.json")) == proof["recovery_marker_sha256"],
        "La identidad del control no coincide con el respaldo verify-host.",
    )
    fd = open_own(normal, store.descriptors["outputs"], DATA_NAME, profile_sha)
    try:
        require(
            normal.identity(os.fstat(fd)) == proof["data_directory"]
            and digest(normal.read_file(fd, ".owner.json")) == proof["data_marker_sha256"],
            "La identidad de datos no coincide con el respaldo verify-host.",
        )
        require(
            digest(normal.read_file(fd, "host-proof.json")) == HOST_PROOF_SHA
            and digest(normal.read_file(fd, "recipe.sh")) == profile["recipe_sha256"]
            and set(os.listdir(fd)) == {".owner.json", "host-proof.json", "recipe.sh"},
            "Datos de recuperación alterados.",
        )
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_stage(normal, directory, stage):
    try:
        raw = normal.read_file(directory, stage + ".json")
    except FileNotFoundError:
        return None
    value = normal.json_object(raw)
    require(raw == canonical(value), "Recibo de recuperación no canónico.")
    return envelope(value)


def original_receipts(store, *, stop=False):
    if stop:
        return {stage: store.receipt(stage) for stage in ("prepare", "create")}
    return store.receipts()


def execute(request, normal):
    mode, profile = request["mode"], request["profile"]
    deadline = time.monotonic() + BUDGETS.get(mode, 0)
    profile_sha = digest(canonical(profile))
    require(
        mode in MODES and request["profile_sha256"] == profile_sha, "Etapa o perfil no fijados."
    )
    require(
        profile["sources"]["remote"] == request["executing_source_sha256"],
        "Ejecutor ajeno al perfil.",
    )
    audit_raw = base64.b64decode(request["audit_b64"], validate=True)
    require(digest(audit_raw) == profile["refs"]["audit"]["sha256"], "Plan SDK histórico alterado.")
    audit = normal.json_object(audit_raw)
    plan = audit["sdk_plan"]
    require(
        plan["remote_sha256"] == ORIGINAL_SHA
        and plan["bound_to_vault"] is True
        and digest(canonical({k: v for k, v in plan.items() if k != "plan_sha256"}))
        == plan["plan_sha256"],
        "Plan original no canónico o ligado.",
    )
    normal.validate_profile(plan)
    normal.validate_inventory(plan["files"], plan["kit_sha256"])
    check_root(profile, plan)
    old_request = {
        "plan": plan,
        "backed": request["original_backed"],
        "layout_source_b64": request["layout_b64"],
    }
    normal.verify_host(old_request)
    store = normal.Store(old_request, local_only=mode == "stop")
    own_fd = data_fd = log_fd = None
    docker = None
    cleanup_allowed = False
    fields = {"success": False, "compiler_executed": False}
    try:
        store.verify_control()
        actual = original_receipts(store, stop=mode == "stop")
        for stage in ("prepare", "create"):
            normal.require_backed_receipt(stage, actual, request["original_backed"])
        origin = actual["create"]["receipt"]["observation"]
        require(origin["cid"] == CID, "El recibo create no acredita el CID fijado.")
        if mode != "stop":
            for stage in ("sdk", "approve", "build"):
                if stage == "build":
                    value = actual[stage]
                    require(
                        value == request["original_backed"].get(stage)
                        and value == envelope(value["receipt"])
                        and value["receipt_sha256"] == FAILED_SHA
                        and value["receipt"].get("success") is False
                        and value["receipt"].get("stage") == "build",
                        "El fallo original no está preservado y respaldado.",
                    )
                else:
                    normal.require_backed_receipt(stage, actual, request["original_backed"])
            failed = actual["build"]["receipt"]
            require(
                failed.get("cleanup", {}).get("stopped") is True
                and failed["cleanup"]["observation"]["pid"] == 0,
                "El fallo no tiene parada acreditada.",
            )
            log = normal.read_file(
                store.descriptors["control"], "build.log", maximum=normal.MAX_OUTPUT
            )
            require(
                digest(log) == failed["log_sha256"] and len(log) == failed["log_bytes"],
                "Log original alterado.",
            )
            store.verify()
            sdk_artifact = normal.sdk_artifact(store)
            require(
                all(
                    actual["sdk"]["receipt"].get(key) == value
                    for key, value in sdk_artifact.items()
                ),
                "El artefacto SDK cambió desde su recibo original.",
            )
            sca_raw = base64.b64decode(request["sca_b64"], validate=True)
            require(
                digest(sca_raw) == profile["refs"]["sca"]["sha256"], "SCA física distinta del pin."
            )
            sca = normal.json_object(sca_raw)
            validate_sca(sca, actual["sdk"]["receipt_sha256"])
            approved = actual["approve"]["receipt"]
            require(
                approved["manifest_sha256"] == APPROVED_SHA
                and request["sdk_sca"] == approved["sdk_sca"]
                and approved["sdk_sca"]["verified"] is True
                and approved["sdk_sca"]["report_sha256"] == sca["report_sha256"],
                "La aprobación raíz no coincide con SCA y manifiesto.",
            )
        docker = store.docker()
        docker.heartbeat_fd = sys.stdin.fileno()
        before = normal.check_container(docker.inspect(CID), CID, plan)
        require(before["created"] == origin["created"], "El nacimiento del CID cambió.")
        if mode == "verify-host":
            stopped(before, origin)
            no_partials(os.listdir(store.descriptors["outputs"]))
            proof_raw = base64.b64decode(request["host_proof_b64"], validate=True)
            require(digest(proof_raw) == HOST_PROOF_SHA, "Prueba host diferente del pin externo.")
            validate_host_proof(normal.json_object(proof_raw), plan, actual)
            recipe_parent, recipe_leaf = normal.open_parent(
                store.descriptors["inputs"], "code/docker/kitsu-zou/build-opencv-minimal.sh"
            )
            try:
                recipe = derive_recipe(normal.read_file(recipe_parent, recipe_leaf))
            finally:
                os.close(recipe_parent)
            require(digest(recipe) == profile["recipe_sha256"], "Receta derivada no fijada.")
            own_fd = open_own(normal, store.descriptors["control"], NAME, profile_sha, create=True)
            data_fd = open_own(
                normal, store.descriptors["outputs"], DATA_NAME, profile_sha, create=True
            )
            normal.write_file(data_fd, "host-proof.json", proof_raw)
            normal.write_file(data_fd, "recipe.sh", recipe)
            fields.update(
                host_proof_sha256=HOST_PROOF_SHA,
                adopted_evidence=True,
                rsa_repeated=False,
                recipe_sha256=digest(recipe),
                original_directories=store.snapshot(),
                material_proofs=normal.material_proofs(plan),
                data_directory=normal.identity(os.fstat(data_fd)),
                data_marker_sha256=digest(normal.read_file(data_fd, ".owner.json")),
            )
            store.verify()
            stopped(normal.check_container(docker.inspect(CID), CID, plan), origin)
        else:
            try:
                own_fd = open_own(normal, store.descriptors["control"], NAME, profile_sha)
            except FileNotFoundError:
                if mode != "stop":
                    raise
                own_fd = open_own(
                    normal, store.descriptors["control"], NAME, profile_sha, create=True
                )
        with operation_lock(normal, own_fd):
            if mode != "verify-host":
                previous = {
                    stage: read_stage(normal, own_fd, stage) for stage in prerequisites(mode)
                }
                for stage, value in previous.items():
                    require_stage(stage, value, request["backed"].get(stage), profile_sha)
                if mode != "stop":
                    data_fd = verify_recovery_data(
                        normal,
                        store,
                        profile,
                        profile_sha,
                        previous["verify-host"]["receipt"],
                        own_fd,
                    )
                existing = read_stage(normal, own_fd, mode)
                require(existing is None, "La etapa existente no se repite ni sobrescribe.")
                os.mkdir("." + mode + "-started", 0o700, dir_fd=own_fd)
            if mode in {"observe-stopped", "restart"}:
                stopped(before, origin)
                no_partials(os.listdir(store.descriptors["outputs"]))
                fields.update(observation=before, original_directories=store.snapshot())
            if mode == "restart":
                cleanup_allowed = True
                result = docker.command(["start", CID], timeout=10)
                require(result["returncode"] == 0, "El CID propio no reinició.")
                after = normal.check_container(docker.inspect(CID), CID, plan)
                require(
                    after["running"]
                    and after["pid"] > 0
                    and after["started_at"] != before["started_at"]
                    and after["created"] == origin["created"],
                    "El restart no acredita instancia nueva del CID propio.",
                )
                fields.update(
                    observation=after,
                    kernel_limits=docker.limits(after),
                    attestation=attest(normal, docker, store),
                )
                store.verify()
            elif mode == "build":
                restart = previous["restart"]["receipt"]["observation"]
                require(
                    before["running"]
                    and all(
                        before[k] == restart[k] for k in ("cid", "created", "pid", "started_at")
                    ),
                    "La instancia reiniciada no coincide con el respaldo.",
                )
                cleanup_allowed = True
                no_partials(os.listdir(store.descriptors["outputs"]))
                fields.update(
                    kernel_limits=docker.limits(before),
                    attestation_before=attest(normal, docker, store),
                )
                log_fd = os.open(
                    "build.log",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=own_fd,
                )
                arguments = [
                    "exec",
                    CID,
                    "/usr/bin/bash",
                    "-c",
                    'umask 077; exec /usr/bin/bash "$@"',
                    "opencv-private-build-recovery",
                    "/work/" + DATA_NAME + "/recipe.sh",
                    "--execute",
                    "/inputs/data/manifest-approved.json",
                    APPROVED_SHA,
                    "/inputs/data",
                    "/work",
                    "/source",
                    "/overlay",
                    "/numpy-headers",
                    "/usr/bin/python3.12",
                ]

                def cleanup():
                    fields["cleanup"] = normal.stop_owned(docker, CID, plan)

                with os.fdopen(log_fd, "wb", buffering=0, closefd=False) as stream:
                    fields["compiler_executed"] = True
                    result = bounded_build(
                        docker,
                        arguments,
                        normal.LimitedSink(stream),
                        cleanup,
                        timeout=build_timeout(deadline),
                    )
                    stream.flush()
                    os.fsync(log_fd)
                fields.update(process=result["process"], **normal.check_build_output(store))
                after = normal.check_container(docker.inspect(CID), CID, plan)
                require(
                    all(after[k] == before[k] for k in ("cid", "created", "pid", "started_at")),
                    "La instancia cambió durante build.",
                )
                fields["attestation_after"] = attest(normal, docker, store)
                store.verify()
            elif mode == "stop":
                fields.update(normal.stop_owned(docker, CID, plan))
            fields.update(
                success=True,
                recovery_directory=normal.identity(os.fstat(own_fd)),
                recovery_marker_sha256=digest(normal.read_file(own_fd, ".owner.json")),
            )
            return put(normal, own_fd, mode, profile_sha, fields)
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
                fields["cleanup"] = normal.stop_owned(docker, CID, plan)
            except BaseException as cleanup_error:
                fields["cleanup"] = {"stopped": False, "error_type": type(cleanup_error).__name__}
        if own_fd is not None:
            return put(normal, own_fd, mode, profile_sha, fields)
        raise
    finally:
        if log_fd is not None:
            os.fsync(log_fd)
            os.close(log_fd)
        for fd in (own_fd, data_fd):
            if fd is not None:
                os.close(fd)
        store.close()


def put(normal, directory, stage, profile_sha, fields):
    if stage == "build":
        try:
            raw = normal.read_file(directory, "build.log", maximum=normal.MAX_OUTPUT)
            fields.update(log_sha256=digest(raw), log_bytes=len(raw))
        except FileNotFoundError:
            pass
    receipt = {
        "owner": OWNER,
        "stage": stage,
        "profile_sha256": profile_sha,
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "cid": CID,
        "sdk_packages_changed": False,
        **fields,
    }
    normal.write_file(directory, stage + ".json", canonical(receipt))
    return envelope(receipt)


def exact_frame(stream, maximum):
    def read(size):
        out = bytearray()
        while len(out) < size:
            block = stream.read(size - len(out))
            require(block, "Frame truncado.")
            out.extend(block)
        return bytes(out)

    import struct

    size = struct.unpack(">I", read(4))[0]
    require(0 < size <= maximum, "Frame excesivo.")
    return read(size)


def main():
    normal = None
    try:
        require(os.name == "posix" and os.geteuid() == os.getegid() == 0, "Sólo host Linux root.")
        os.umask(0o077)
        stream = sys.stdin.buffer.raw
        normal = module_bytes(
            "opencv_recovery_original", exact_frame(stream, 128 * 1024), ORIGINAL_SHA
        )
        request = normal.json_object(exact_frame(stream, 2 * 1024**2))
        request["executing_source_sha256"] = globals()["_RECOVERY_SOURCE_SHA256"]
        mode = request["mode"]
        require(mode in MODES, "Etapa no admitida.")

        def deadline(_sig, _frame):
            raise TimeoutError("Plazo total de recuperación agotado.")

        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(BUDGETS[mode] + 20)
        result = execute(request, normal)
        success = result["receipt"]["success"] is True
        print(json.dumps({"success": success, "result": result}))
        return 0 if success else 1
    except BaseException as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "detail": str(error)[:240],
                    "evidence_preserved": True,
                }
            )
        )
        return 1
    finally:
        if os.name == "posix":
            signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
