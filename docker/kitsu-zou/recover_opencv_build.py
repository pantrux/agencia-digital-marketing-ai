"""Plan por referencias; Root respalda cada transición antes de compilar OpenCV."""

import argparse
import base64
import hashlib
import json
import os
import shlex
import stat
import struct
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PREFIX = "MEDIA_ZOU_OPENCV_BUILD_RECOVERY_V1_"
OLD_PREFIX = "MEDIA_ZOU_OPENCV_SDK_"
PROFILE_PATH = ROOT / ".tools/evidence/phase4-zou-opencv-build-recovery-v1-profile.json"
REFS = {
    "audit": (
        ".tools/evidence/phase4-zou-opencv-sdk-audit-v1-profile.json",
        "747a33d04bf5238ccc374cc3ac7cd3b1f49050bf9df5cb668d4191cc851001e1",
    ),
    "host_proof": (
        ".tools/evidence/phase4-zou-opencv-inputs-host-verification-real-v1.json",
        "b7e79bb8f5364c4eae0d7f36cea4e8850532c158ee245b2a071be6559ea78bb5",
    ),
    "failure": (
        ".tools/evidence/phase4-zou-opencv-minimal-build-failed-v1.json",
        "85809b079f7a79f038d5048cde2d3998955d20417641dd7473152a8719595449",
    ),
    "sca": (
        ".tools/evidence/phase4-zou-opencv-sdk-installed-755ea032b52a767f-v2.receipt.json",
        "2ce0c511193cc5be38d902f07c4f9282e619686229d213a8a597384280750ae0",
    ),
    "original_local": (
        "docker/kitsu-zou/run_opencv_minimal_sdk.py",
        "222e30e5d78dd494482a61c54c906846c13156eb3031466990df5ecb538958bf",
    ),
    "original_remote": (
        "docker/kitsu-zou/opencv_minimal_sdk_remote.py",
        "77f442612a9c14df8f0685474ce5e3f47c8c0da16e828888ee8250dc1d7e002b",
    ),
    "recipe": (
        "docker/kitsu-zou/build-opencv-minimal.sh",
        "fc3341fa0c8c20ad7bc2ab9b858f127cd0e0ec76b233f05273ef0df2d45aed7f",
    ),
    "layout": (
        "docker/host_layout.py",
        "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9",
    ),
    "env_parser": (
        "scripts/ci/check_env.py",
        "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda",
    ),
    "access": ("sre/backup_tools_access.py", ""),
    "updater": (
        ".tools/vault_updates.py",
        "0fc75c6d082e9e25fa6a5b5146560d7373b3dfab2d701e351749dafb2183b46c",
    ),
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def json_object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON ambiguo.")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


def read_source(path, maximum=256 * 1024):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Ruta enlazada o con punto de reanálisis.")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise ValueError("Archivo irregular, enlazado o excesivo.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        held = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
        final = os.fstat(stream.fileno())

    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink, stat.S_IFMT(s.st_mode))

    if len(raw) != info.st_size or any(
        identity(s) != identity(info) for s in (held, final, path.lstat())
    ):
        raise ValueError("Archivo cambiado durante lectura.")
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
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def module_bytes(name, raw, filename, pin):
    if digest(raw) != pin:
        raise ValueError("Fuente distinta del pin.")
    module = types.ModuleType(name)
    module.__file__ = str(filename)
    sys.modules[name] = module
    exec(compile(raw, str(filename), "exec"), module.__dict__)
    return module


def remote_contract(expected=None):
    path = HERE / "opencv_build_recovery_remote.py"
    raw = read_source(path)
    if expected and digest(raw) != expected:
        raise ValueError("El ejecutor nuevo cambió.")
    return module_bytes("opencv_build_recovery_contract", raw, path, digest(raw))


def reference(name, profile=None):
    if profile:
        row = profile["refs"][name]
        path, expected = Path(row["path"]), row["sha256"]
    else:
        relative, expected = REFS[name]
        path = ROOT / relative
    raw = read_source(path, 2 * 1024**2 if name == "audit" else 256 * 1024)
    if expected and digest(raw) != expected:
        raise ValueError("Referencia histórica alterada: " + name)
    return raw


def decode(value):
    raw = base64.b64decode(value, validate=True)
    if len(raw) > 2 * 1024**2:
        raise ValueError("Respaldo excesivo.")
    result = json_object(raw)
    if type(result) is not dict:
        raise ValueError("Respaldo no es un objeto.")
    return result


def proposal():
    remote = remote_contract()
    audit = json_object(reference("audit"))
    plan = audit["sdk_plan"]
    refs = {}
    for name, (relative, _pin) in REFS.items():
        raw = reference(name)
        if name == "access" and digest(raw) != plan["access_helper_sha256"]:
            raise ValueError("El helper ACL difiere del pin histórico.")
        refs[name] = {"path": str(ROOT / relative), "sha256": digest(raw)}
    proof = json_object(reference("host_proof"))
    failure = json_object(reference("failure"))
    if failure.get("receipt_sha256") != remote.FAILED_SHA or failure != remote.envelope(
        failure["receipt"]
    ):
        raise ValueError("El fallo original no corresponde al envelope respaldado.")
    return {
        "schema_version": 1,
        "kind": "opencv-build-host-recovery-v1",
        "owner": remote.OWNER,
        "cid": remote.CID,
        "refs": refs,
        "sources": {
            "local": digest(read_source(Path(__file__))),
            "remote": digest(read_source(HERE / "opencv_build_recovery_remote.py")),
        },
        "paths": {
            "control": plan["paths"]["control"] + "/" + remote.NAME,
            "data": plan["paths"]["outputs"] + "/" + remote.DATA_NAME,
        },
        "recipe_sha256": digest(remote.derive_recipe(reference("recipe"))),
        "host_proof_success": proof["success"] is True,
        "original_plan_sha256": plan["plan_sha256"],
        "failed_build_receipt_sha256": remote.FAILED_SHA,
        "limits": {
            "build_seconds": 1800,
            "exec_seconds": 1780,
            "cleanup_reserve_seconds": 20,
            "capture_bytes": 4 * 1024**2,
            "cpu": 2,
            "memory_bytes": 6 * 1024**3,
            "pids": 128,
        },
        "rsa_repeated": False,
        "packages_changed": False,
        "sdk_executed": False,
        "docker_executed": False,
    }


def read_values(profile):
    access = module_bytes(
        "opencv_recovery_access",
        reference("access", profile),
        profile["refs"]["access"]["path"],
        profile["refs"]["access"]["sha256"],
    )
    access.check_access(ROOT / ".env", private=True)
    parser = module_bytes(
        "opencv_recovery_env",
        reference("env_parser", profile),
        profile["refs"]["env_parser"]["path"],
        profile["refs"]["env_parser"]["sha256"],
    )
    values, errors = parser.parse_env(read_source(ROOT / ".env", 8 * 1024**2).decode("utf-8-sig"))
    if errors:
        raise ValueError("Bóveda no canónica.")
    return values


def original_backed(values, *, stop=False):
    result = {}
    for suffix in (
        ("PREPARED_B64", "CONTAINER_B64")
        if stop
        else ("PREPARED_B64", "CONTAINER_B64", "STAGE_RECEIPTS_B64")
    ):
        group = decode(values[OLD_PREFIX + suffix])
        if set(result) & set(group):
            raise ValueError("Recibos duplicados entre grupos originales.")
        result.update(group)
    return result


def require_pins(profile, profile_path, values):
    expected = {
        "OWNER": profile["owner"],
        "PROFILE_PATH": str(profile_path.absolute()),
        "PROFILE_SHA256": digest(canonical(profile)),
        "LOCAL_SHA256": profile["sources"]["local"],
        "REMOTE_SHA256": profile["sources"]["remote"],
        "CONTROL_PATH": profile["paths"]["control"],
        "DATA_PATH": profile["paths"]["data"],
        "HOST_PROOF_PATH": profile["refs"]["host_proof"]["path"],
        "HOST_PROOF_SHA256": profile["refs"]["host_proof"]["sha256"],
        "FAILED_BUILD_PATH": profile["refs"]["failure"]["path"],
        "FAILED_BUILD_SHA256": profile["refs"]["failure"]["sha256"],
    }
    if any(values.get(PREFIX + key) != value for key, value in expected.items()):
        raise ValueError("Root debe respaldar perfil, fuentes y rutas antes de cualquier etapa.")


def collect(channel, deadline, maximum):
    output, stderr = bytearray(), bytearray()
    pulse = time.monotonic()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Plazo de transporte agotado.")
        if time.monotonic() >= pulse:
            channel.sendall(b".")
            pulse = time.monotonic() + 1
        for ready, read, buffer in (
            (channel.recv_ready, channel.recv, output),
            (channel.recv_stderr_ready, channel.recv_stderr, stderr),
        ):
            while ready():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Salida continua fuera del plazo.")
                buffer.extend(read(65536))
                if len(output) + len(stderr) > maximum:
                    raise ValueError("Respuesta excesiva; se conserva el log remoto sin truncar.")
        if (
            channel.exit_status_ready()
            and not channel.recv_ready()
            and not channel.recv_stderr_ready()
        ):
            break
        time.sleep(0.01)
    status = channel.recv_exit_status()
    value = json_object(output)
    if (
        stderr
        or type(value.get("success")) is not bool
        or status not in (0, 1)
        or value["success"] != (status == 0)
    ):
        raise ValueError("Framing JSON, stderr y salida no coinciden.")
    return value


def request_remote(client, mode, profile, values, remote):
    source = read_source(HERE / "opencv_build_recovery_remote.py")
    payload = {
        "mode": mode,
        "profile": profile,
        "profile_sha256": digest(canonical(profile)),
        "audit_b64": base64.b64encode(reference("audit", profile)).decode(),
        "layout_b64": base64.b64encode(reference("layout", profile)).decode(),
        "original_backed": original_backed(values, stop=mode == "stop"),
        "backed": decode(values.get(PREFIX + "RECEIPTS_B64", "e30=")),
    }
    if mode != "stop":
        payload.update(
            host_proof_b64=base64.b64encode(reference("host_proof", profile)).decode(),
            sca_b64=base64.b64encode(reference("sca", profile)).decode(),
            sdk_sca=decode(values[OLD_PREFIX + "SDK_SCA_B64"]),
        )
        original = decode(values[OLD_PREFIX + "CONTROLLER_B64"])
        if original != json_object(reference("audit", profile))["sdk_plan"]:
            raise ValueError("El plan SDK activo difiere del histórico aprobado.")
        failed = json_object(reference("failure", profile))
        if payload["original_backed"].get("build") != failed:
            raise ValueError("El fallo activo difiere del preservado.")
    encoded = canonical(payload)
    if len(encoded) > 2 * 1024**2 or digest(source) != profile["sources"]["remote"]:
        raise ValueError("Fuente o frame fuera del contrato fijado.")
    bootstrap = (
        "import sys,struct,hashlib\ns=sys.stdin.buffer.raw\n"
        "def read(n):\n b=bytearray()\n while len(b)<n:\n  x=s.read(n-len(b))\n  if not x:raise ValueError('FRAME_TRUNCATED')\n  b.extend(x)\n return bytes(b)\n"
        "n=struct.unpack('>I',read(4))[0]\nif not 0<n<=131072:raise ValueError('FRAME_LIMIT')\n"
        "source=read(n)\nh=hashlib.sha256(source).hexdigest()\nif h!="
        + repr(profile["sources"]["remote"])
        + ":raise ValueError('SOURCE_PIN')\n"
        "scope={'__name__':'__main__','_RECOVERY_SOURCE_SHA256':h}\nexec(compile(source,'<opencv-recovery-fijado>','exec'),scope)"
    )
    channel = client.get_transport().open_session(timeout=10)
    try:
        channel.settimeout(10)
        channel.exec_command(
            "sudo -n /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root /usr/bin/python3 -I -B -c "
            + shlex.quote(bootstrap)
        )
        for raw in (source, reference("original_remote", profile), encoded):
            channel.sendall(struct.pack(">I", len(raw)) + raw)
        return collect(channel, time.monotonic() + remote.BUDGETS[mode] + 35, 1024 * 1024)
    finally:
        channel.close()


def persist(profile, values, stage, result, writer):
    remote = remote_contract(profile["sources"]["remote"])
    receipt = result.get("receipt", {})
    if (
        result != remote.envelope(receipt)
        or receipt.get("stage") != stage
        or receipt.get("profile_sha256") != digest(canonical(profile))
        or type(receipt.get("success")) is not bool
        or receipt.get("owner") != profile["owner"]
    ):
        raise ValueError("Recibo remoto ajeno o de etapa diferente.")
    current = decode(values.get(PREFIX + "RECEIPTS_B64", "e30="))
    if stage in current:
        raise ValueError("El recibo de recuperación ya existe; no se reemplaza.")
    path = (
        ROOT
        / ".tools/evidence"
        / (
            "phase4-zou-opencv-build-recovery-v1."
            + stage
            + "."
            + result["receipt_sha256"][:16]
            + ".json"
        )
    )
    write_new(path, canonical(result))
    current[stage] = result
    writer({PREFIX + "RECEIPTS_B64": base64.b64encode(canonical(current)).decode()})
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        default="plan",
        choices=("plan", "verify-host", "observe-stopped", "restart", "build", "stop"),
    )
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        if args.mode == "plan":
            profile = proposal()
            if args.output:
                if args.output.absolute().parent != ROOT / ".tools/evidence":
                    raise ValueError("El perfil debe permanecer en evidencia privada.")
                write_new(args.output, canonical(profile))
            print(
                json.dumps(
                    {
                        "plan_only": True,
                        "profile_sha256": digest(canonical(profile)),
                        "profile": profile,
                    }
                )
            )
            return 0
        if not args.root_update_vault or not args.expected_profile_sha256:
            raise ValueError("La acción exige pin externo y CLI explícita de Root.")
        raw = read_source(args.profile, 32768)
        profile = json_object(raw)
        if (
            digest(raw) != args.expected_profile_sha256
            or raw != canonical(profile)
            or profile["sources"]["local"] != digest(read_source(Path(__file__)))
        ):
            raise ValueError("Perfil o fuente local cambiados.")
        remote = remote_contract(profile["sources"]["remote"])
        values = read_values(profile)
        require_pins(profile, args.profile, values)
        local = module_bytes(
            "opencv_original_transport",
            reference("original_local", profile),
            profile["refs"]["original_local"]["path"],
            profile["refs"]["original_local"]["sha256"],
        )
        updater = module_bytes(
            "opencv_recovery_root_updater",
            reference("updater", profile),
            profile["refs"]["updater"]["path"],
            profile["refs"]["updater"]["sha256"],
        )
        client = local.connect(values)
        result = request_remote(client, args.mode, profile, values, remote)
        if "result" in result:
            persist(profile, values, args.mode, result["result"], updater.update)
        print(json.dumps(result))
        return 0 if result["success"] else 1
    except (ValueError, OSError, KeyError, TypeError, TimeoutError) as error:
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
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
