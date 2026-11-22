"""Observación y carga SRE de la bóveda; sin efectos al importar o pedir el plan."""

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
import xml.etree.ElementTree as etree
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from pathlib import Path

JOB_OUTPUT_LIMIT = 1024**2
OUTER_TIMEOUT = 390
MAX_PARALLEL_UPLOADS = 3
MAX_CAPTURE_NS = (1 << 63) - 1
MAX_GENERATION = (1 << 128) - 1
RECOVERY_NAMESPACE = "recovery-receipts-v1/"
RECOVERY_DOMAIN = b"e-scale:vault-recovery-receipts:v1:"
OWNER_HTTP_ERROR = re.compile(r"R2 rechazó la operación HTTP ([1-5][0-9]{2})\.")


def proposal():
    workspace = r"D:\Proyectos\Agencia Digital de Marketing AI"
    code_root = workspace + r"\.tools\backup-source-build\vault-changes-code-v1"
    here = Path(__file__).absolute().parent
    sources = {
        name: hashlib.sha256((here / name).read_bytes()).hexdigest()
        for name in ("vault_updates_v2.py", "vault_change_queue.py", "vault_change_worker.py")
    }
    sources["document_vault.py"] = (
        "1a52555de27a51403da07bdad35091b1a7fd88ee6b9888a79b3d9215fb046c18"
    )
    return {
        "ready": False,
        "queue_root_proposed": r"C:\GPU-Bridge\vault-changes-v1",
        "owner": "agencia-digital-marketing-ai:vault-changes:v1",
        "requires_root_profile_and_identity": True,
        "task_registered": False,
        "network_used": False,
        "vault_written": False,
        "config_template": {
            "kind": "vault-changes-config-v1",
            "owner": "agencia-digital-marketing-ai:vault-changes:v1",
            "queue_root": r"C:\GPU-Bridge\vault-changes-v1",
            "queue_identity": None,
            "code_root": code_root,
            "code_identity": None,
            "sources": sources,
            "vault_path": workspace + r"\.env",
            "template_path": workspace + r"\.env.example",
            "publish_parent": workspace + r"\.tools\backup-source-build",
            "legacy_profile": {
                "path": r"C:\GPU-Bridge\vault-backup-r2-v1\profile.json",
                "sha256": "96d171fc9e822ebad90406dba6c1aee5b3738e5fdb359fd1582027ecacdd89a4",
            },
            "repository_id": "83a5af3624f05aa209e30ed81f75d225d265330fc3d13086cf21f297b5e12241",
            "python": {"path": None, "sha256": None},
            "poll_seconds": 1,
            "maximum_vault_bytes": 8 * 1024**2,
            "maximum_queue_bytes": 2 * 1024**3,
            "maximum_generations": 1024,
            "outer_timeout_seconds": OUTER_TIMEOUT,
            "snapshot_ids_in_vault": False,
        },
    }


def backup_arguments(capture, hostname, generation, status):
    if not re.fullmatch(r"[a-f0-9]{32}", generation) or status not in {
        "PREPARED",
        "COMMITTED",
        "OBSERVED",
    }:
        raise ValueError("UPLOAD_ARGUMENTS")
    return [
        "backup",
        capture,
        "--host",
        hostname,
        "--tag",
        "vault-changes-v1",
        "--tag",
        generation,
        "--tag",
        status,
    ]


def require(value, code):
    if not value:
        raise ValueError(code)


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def decoded(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _value: require(False, "JSON_CONSTANT")
    )


def recovery_prefix(client):
    prefix = client.location["prefix"]
    require(
        isinstance(prefix, str) and re.fullmatch(r"windows-vault-v1/[a-f0-9-]{36}/", prefix),
        "RECOVERY_PREFIX",
    )
    return prefix + RECOVERY_NAMESPACE


def recovery_key(prefix, record):
    require(
        type(record) is dict
        and set(record)
        == {
            "repository_id",
            "generation_id",
            "prepared_ns",
            "snapshot_id",
            "sha256",
            "bytes",
            "publication_status",
        }
        and all(
            type(record[name]) is str and re.fullmatch(r"[a-f0-9]{64}", record[name])
            for name in ("repository_id", "snapshot_id", "sha256")
        )
        and type(record["generation_id"]) is str
        and re.fullmatch(r"[a-f0-9]{32}", record["generation_id"])
        and type(record["prepared_ns"]) is int
        and 0 < record["prepared_ns"] <= MAX_CAPTURE_NS
        and type(record["bytes"]) is int
        and 0 < record["bytes"] <= 8 * 1024**2
        and record["publication_status"] in ("OBSERVED", "COMMITTED"),
        "RECOVERY_RECORD_FIELDS",
    )
    # R2 lista en orden lexicográfico. Se invierte la clave de orden de la cola.
    rank_time = MAX_CAPTURE_NS - record["prepared_ns"]
    rank_generation = MAX_GENERATION - int(record["generation_id"], 16)
    return f"{prefix}{rank_time:019d}-{rank_generation:032x}.json"


def signed_recovery_record(key, password, record):
    require(type(password) is str and bool(password), "RECOVERY_PASSWORD")
    integrity_key = hashlib.scrypt(
        password.encode(),
        salt=RECOVERY_DOMAIN + bytes.fromhex(record["repository_id"]),
        n=2**15,
        r=8,
        p=1,
        maxmem=64 * 1024**2,
        dklen=32,
    )
    signature = hmac.digest(integrity_key, key.encode() + b"\n" + encoded(record), "sha256")
    return encoded({"record": record, "hmac": signature.hex()})


def publish_recovery_record(client, expected_owner, password, record):
    """Crea un acuse inmutable para un snapshot previamente verificado."""
    key = recovery_key(recovery_prefix(client), record)
    body = signed_recovery_record(key, password, record)
    client.verify(expected_owner)
    try:
        client._request("PUT", key, body=body, conditional=True)
    except (OSError, ValueError):
        observed = client._request("GET", key)
        require(hmac.compare_digest(observed, body), "RECOVERY_RECORD_CONFLICT")
    return key


def latest_recovery_record(client, expected_owner, password, repository_id):
    """Localiza la última captura publicada con un listado y una lectura."""
    prefix = recovery_prefix(client)
    client.verify(expected_owner)
    raw = client._request("GET", query={"list-type": "2", "max-keys": "1", "prefix": prefix})
    require(
        len(raw) <= 65536 and b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
        "RECOVERY_LIST_XML",
    )
    document = etree.fromstring(raw)
    items = document.findall("./{*}Contents")
    require(
        document.tag.split("}")[-1] == "ListBucketResult"
        and document.findtext("./{*}Prefix") == prefix
        and len(items) <= 1
        and document.findtext("./{*}KeyCount") == str(len(items)),
        "RECOVERY_LIST_SCOPE",
    )
    if not items:
        raise FileNotFoundError("RECOVERY_RECORDS_EMPTY")
    key = items[0].findtext("./{*}Key", "")
    require(
        re.fullmatch(re.escape(prefix) + r"[0-9]{19}-[a-f0-9]{32}[.]json", key),
        "RECOVERY_LIST_KEY",
    )
    body = client._request("GET", key)
    require(len(body) <= 2048, "RECOVERY_RECORD_SIZE")
    envelope = decoded(body)
    require(type(envelope) is dict and set(envelope) == {"record", "hmac"}, "RECOVERY_RECORD_BODY")
    record = envelope["record"]
    require(recovery_key(prefix, record) == key, "RECOVERY_RECORD_KEY")
    require(record["repository_id"] == repository_id, "RECOVERY_REPOSITORY_ID")
    require(
        hmac.compare_digest(signed_recovery_record(key, password, record), body),
        "RECOVERY_RECORD_HMAC",
    )
    return record


def validate_child_limits(information):
    require(
        information.BasicLimitInformation.LimitFlags == 0x2208
        and information.BasicLimitInformation.ActiveProcessLimit == 8
        and information.JobMemoryLimit == 2 * 1024**3,
        "CHILD_JOB_LIMITS",
    )


def verify_child_job(job):
    """NULL consulta el Job inmediato; sólo lee límites, no abre procesos ajenos."""
    import ctypes
    from ctypes import wintypes

    require(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8, "CHILD_WINDOWS_AMD64")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel.QueryInformationJobObject
    query.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    information = job.EXTENDED_LIMITS()
    require(ctypes.sizeof(information) == 144, "CHILD_JOB_ABI")
    returned = wintypes.DWORD()
    require(
        query(
            None, 9, ctypes.byref(information), ctypes.sizeof(information), ctypes.byref(returned)
        ),
        "CHILD_JOB_QUERY",
    )
    require(returned.value == ctypes.sizeof(information), "CHILD_JOB_LENGTH")
    validate_child_limits(information)


def accept_upload_result(queue, generation, result):
    """Confirma por creación exclusiva mientras run_one mantiene uploader_lock."""
    require(
        result.status == "completed"
        and result.exit_code == 0
        and result.cleanup_complete is True
        and len(result.stdout) <= JOB_OUTPUT_LIMIT
        and result.stderr == b"",
        "OUTER_JOB_REJECTED",
    )
    value = decoded(result.stdout)
    require(isinstance(value, dict), "UPLOAD_RESULT_OBJECT")
    _raw, metadata = queue.candidate(generation)
    attempt = queue.store.json(queue.path(generation, "attempt.json"))
    require(
        value.get("success") is True
        and value.get("generation_id") == generation
        and value.get("sha256") == metadata["sha256"]
        and re.fullmatch(r"[a-f0-9]{64}", str(value.get("snapshot_id", "")))
        and value.get("publication_status") == attempt["subject"]["publication_status"],
        "UPLOAD_SUBJECT",
    )
    if value["publication_status"] == "COMMITTED":
        committed = queue.store.json(queue.path(generation, "committed.json"))
        require(
            committed.get("kind") == "COMMITTED"
            and committed.get("generation_id") == generation
            and committed.get("sha256") == metadata["sha256"],
            "PUBLICATION_NOT_PROVED",
        )
    # La captura no escribe este recibo y el lock del uploader serializa su lectura.
    queue.record(
        generation,
        "uploaded.json",
        {
            **value,
            "uploaded_ns": time.time_ns(),
            "publication_proved": value["publication_status"] == "COMMITTED",
        },
    )
    return {"success": True, "generation_id": generation, "uploaded": True, "values_hidden": True}


def restic_call(runtime, provider, environment, generation, stage, arguments, timeout):
    """Cada comando usa el Job congelado; su salida bruta permanece privada."""
    runtime.verify_binary()
    job = runtime.job()
    common = [
        "--json",
        "--cache-dir",
        str(runtime.store.root / "cache"),
        "-o",
        "s3.region=auto",
        "-o",
        "s3.bucket-lookup=path",
        "-o",
        "s3.connections=2",
        "-o",
        "s3.retries=2",
    ]
    result = job.run_job(
        Path(runtime.profile["binary_path"]),
        common + arguments,
        cwd=runtime.store.root / "profile",
        env=environment,
        timeout=timeout,
        limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
        output_limit=JOB_OUTPUT_LIMIT,
    )
    prefix = "operations/" + generation + "-" + stage
    runtime.store.write(prefix + ".stdout", result.stdout)
    runtime.store.write(prefix + ".stderr", result.stderr)
    runtime.store.write(
        prefix + ".job.json",
        encoded(
            {key: value for key, value in vars(result).items() if key not in {"stdout", "stderr"}}
        ),
    )
    provider.validate_result(result)
    runtime.verify_binary()
    return result.stdout


def verify_upload_owner(client, owner):
    """Reintenta solo la lectura inicial del titular ante errores transitorios de R2."""
    next_attempt_before = time.monotonic() + 12
    for delay in (1, 3, 6, None):
        try:
            client.verify(owner)
            return
        except ValueError as error:
            status = OWNER_HTTP_ERROR.fullmatch(str(error))
            if (
                status is None
                or status[1] not in {"500", "502", "503", "504"}
                or delay is None
                or time.monotonic() + delay >= next_attempt_before
            ):
                raise
            time.sleep(delay)


def upload_child(runtime, generation, nonce):
    """No llama al controlador monouso: compone sólo sus validadores y entorno."""
    queue = runtime.queue
    raw, metadata = queue.candidate(generation)
    attempt = runtime.store.json(queue.path(generation, "attempt.json"))
    subject = attempt["subject"]
    require(
        subject.get("repository_id") == runtime.config["repository_id"]
        and subject.get("sha256") == metadata["sha256"]
        and attempt.get("child_nonce") == nonce
        and re.fullmatch(r"[a-f0-9]{32}", nonce),
        "ATTEMPT_SUBJECT",
    )
    queue.record(
        generation,
        "claim.json",
        {"generation_id": generation, "child_nonce": nonce, "claimed_ns": time.time_ns()},
    )
    provider = runtime.backup_helpers()
    values = runtime.current_values()
    environment = provider.environment({**runtime.profile, "root": str(runtime.store.root)}, values)

    def owner_client():
        return provider.S3.OwnerClient(
            runtime.profile["repository_url"],
            values["R2_PLATFORM_ACCESS_KEY_ID"],
            values["R2_PLATFORM_SECRET_ACCESS_KEY"],
        )

    expected_owner = provider.STATE.encoded(
        {
            "schema_version": 1,
            "owner": runtime.profile["owner"],
            "operation_id": runtime.profile["operation_id"],
        }
    )
    record = {
        "success": False,
        "generation_id": generation,
        "sha256": metadata["sha256"],
        "publication_status": subject["publication_status"],
    }
    stage = "propietario_r2"
    try:
        verify_upload_owner(owner_client(), expected_owner)
        stage = "configuracion_restic"
        config = restic_call(
            runtime, provider, environment, generation, "cat", ["cat", "config"], 60
        )
        provider.parse_config(config, runtime.config["repository_id"])
        capture = str(runtime.store.root / queue.path(generation, "candidate.env"))
        stage = "respaldo_restic"
        output = restic_call(
            runtime,
            provider,
            environment,
            generation,
            "backup",
            backup_arguments(
                capture, runtime.profile["hostname"], generation, subject["publication_status"]
            ),
            180,
        )
        snapshot = provider.parse_backup(output)
        record["snapshot_id"] = snapshot
        queue.record(
            generation,
            "snapshot-recorded.json",
            {
                "generation_id": generation,
                "snapshot_id": snapshot,
                "sha256": metadata["sha256"],
                "publication_status": subject["publication_status"],
            },
        )
        stage = "listado_restic"
        listing = restic_call(
            runtime, provider, environment, generation, "ls", ["ls", snapshot], 60
        )
        provider.parse_listing(listing, snapshot, capture, len(raw))
        queue.candidate(generation)
        if subject["publication_status"] in ("OBSERVED", "COMMITTED"):
            stage = "acuse_r2"
            recovery = {
                "repository_id": runtime.config["repository_id"],
                "generation_id": generation,
                "prepared_ns": metadata["prepared_ns"],
                "snapshot_id": snapshot,
                "sha256": metadata["sha256"],
                "bytes": len(raw),
                "publication_status": subject["publication_status"],
            }
            try:
                publish_recovery_record(
                    owner_client(), expected_owner, values["RESTIC_PASSWORD"], recovery
                )
            except (OSError, ValueError):
                # El snapshot ya existe. Se repite sólo la escritura idempotente del acuse.
                publish_recovery_record(
                    owner_client(), expected_owner, values["RESTIC_PASSWORD"], recovery
                )
        record["success"] = True
    except Exception as error:
        record.update(
            error_type=type(error).__name__, stage=stage, requires_manual_reconciliation=True
        )
        if stage == "propietario_r2" and (status := OWNER_HTTP_ERROR.fullmatch(str(error))):
            record["http_status"] = int(status[1])
    queue.record(generation, "result.json", record)
    return record


def bootstrap_runtime(config_path, config_sha256, *, preparing=False):
    """Bootstrap sólo de bytes fijados; Runtime vuelve a comprobar perfil y ACL."""
    import stat

    def pinned(path, pin):
        path = Path(path)
        info = path.lstat()
        require(
            path.is_absolute()
            and stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and not getattr(info, "st_file_attributes", 0) & 0x400
            and info.st_size <= 256 * 1024,
            "BOOTSTRAP_FILE",
        )
        with path.open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        require(
            len(raw) == info.st_size and hashlib.sha256(raw).hexdigest() == pin, "BOOTSTRAP_SHA"
        )
        return raw

    raw = pinned(config_path, config_sha256)
    config = decoded(raw)
    source = Path(config["code_root"]) / "vault_change_queue.py"
    raw_source = pinned(source, config["sources"][source.name])
    module = types.ModuleType("vault_changes_runtime_queue")
    module.__file__ = str(source)
    sys.modules[module.__name__] = module
    exec(compile(raw_source, str(source), "exec"), module.__dict__)
    return module.Runtime(config, config_path, config_sha256, __file__, preparing=preparing)


def reserve_upload(runtime, row):
    return runtime.queue.begin_attempt(
        row["generation_id"],
        {
            "repository_id": runtime.config["repository_id"],
            "sha256": row["sha256"],
            "publication_status": row["status"],
        },
    )


def run_one(runtime, *, reservation_lock=None, idle_cache=False, prefer_latest=False):
    reservation = nullcontext() if reservation_lock is None else reservation_lock
    with runtime.queue.uploader_lock():
        with reservation:
            row = runtime.queue.next_pending(idle_cache=idle_cache, prefer_latest=prefer_latest)
            if row is None:
                return {"success": True, "uploaded": False, "pending": 0}
            attempt = reserve_upload(runtime, row)
        return run_reserved(runtime, row["generation_id"], attempt, runtime.job())


def run_batch(runtime, *, reservation_lock, prefer_latest):
    # El bloqueo cubre también los hijos que terminan después de un error.
    with (
        runtime.queue.uploader_lock(),
        ThreadPoolExecutor(max_workers=MAX_PARALLEL_UPLOADS) as workers,
    ):
        active = {}
        completed = 0
        job = None

        def collect_finished():
            nonlocal completed
            for generation, (future, _attempt) in tuple(active.items()):
                if future.done():
                    future.result()
                    del active[generation]
                    completed += 1

        while True:
            collect_finished()
            if len(active) < MAX_PARALLEL_UPLOADS:
                with reservation_lock:
                    try:
                        rows = runtime.queue.pending_uploads(
                            limit=MAX_PARALLEL_UPLOADS - len(active),
                            idle_cache=True,
                            prefer_latest=prefer_latest,
                            active_attempts={key: row[1] for key, row in active.items()},
                        )
                    except FileExistsError:
                        rows = []
                    for row in rows:
                        collect_finished()
                        if job is None:
                            job = runtime.job()
                        attempt = reserve_upload(runtime, row)
                        generation = row["generation_id"]
                        future = workers.submit(run_reserved, runtime, generation, attempt, job)
                        active[generation] = future, attempt
                        prefer_latest = not prefer_latest
            if not active:
                return completed
            wait(
                [future for future, _attempt in active.values()],
                timeout=1 if len(active) < MAX_PARALLEL_UPLOADS else None,
                return_when=FIRST_COMPLETED,
            )


def run_reserved(runtime, generation, attempt, job):
    runtime.access.integrity(Path(runtime.config["python"]["path"]))
    queue_module = sys.modules[type(runtime.queue).__module__]
    queue_module.read_pinned(
        Path(runtime.config["python"]["path"]),
        runtime.config["python"]["sha256"],
        128 * 1024**2,
    )
    args = [
        "-I",
        "-S",
        "-B",
        str(Path(__file__).absolute()),
        "--_upload",
        "--generation",
        generation,
        "--attempt-nonce",
        attempt["child_nonce"],
        "--config",
        runtime.config_path,
        "--config-sha256",
        runtime.config_sha256,
    ]
    env = {
        "SystemRoot": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "PATH": r"C:\Windows\System32;C:\Windows",
        "HOME": str(runtime.store.root / "profile"),
        "USERPROFILE": str(runtime.store.root / "profile"),
        "TEMP": str(runtime.store.root / "tmp"),
        "TMP": str(runtime.store.root / "tmp"),
    }
    result = job.run_job(
        Path(runtime.config["python"]["path"]),
        args,
        cwd=runtime.store.root / "profile",
        env=env,
        timeout=OUTER_TIMEOUT,
        limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
        output_limit=JOB_OUTPUT_LIMIT,
    )
    runtime.store.write("operations/" + generation + "-outer.stdout", result.stdout)
    runtime.store.write("operations/" + generation + "-outer.stderr", result.stderr)
    runtime.store.write(
        "operations/" + generation + "-outer.job.json",
        encoded(
            {key: value for key, value in vars(result).items() if key not in {"stdout", "stderr"}}
        ),
    )
    return accept_upload_result(runtime.queue, generation, result)


def report_state(runtime, component, state, error_type=None):
    """Sólo el ledger privado propio; nunca la bóveda ni valores de credenciales."""
    try:
        with runtime.store.lock("watch-status"):
            record = runtime.store.json("ledger.json")
            record[component] = {
                "state": state,
                "error_type": error_type,
                "updated_ns": time.time_ns(),
            }
            runtime.store.ledger(record)
    except (OSError, ValueError):
        return False
    return True


def windows_boot_ns():
    result = subprocess.run(
        [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; "
            "(Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 7)"
            ".LastBootUpTime.ToFileTimeUtc()",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=12,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=True,
    )
    stamp = result.stdout.strip()
    require(re.fullmatch(rb"[0-9]{17,18}", stamp), "BOOT_TIMESTAMP")
    boot_ns = (int(stamp) - 116_444_736_000_000_000) * 100
    require(0 < boot_ns <= time.time_ns(), "BOOT_TIMESTAMP")
    return boot_ns


@contextmanager
def windows_boot_recovery_mutex():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = create(None, False, r"Global\AgenciaDigitalMarketingAI.VaultBootRecovery.v1")
    require(handle, "BOOT_RECOVERY_MUTEX")
    try:
        require(wait(handle, 5000) in (0, 0x80), "BOOT_RECOVERY_MUTEX_WAIT")
        try:
            yield
        finally:
            kernel.ReleaseMutex(handle)
    finally:
        kernel.CloseHandle(handle)


def watch(runtime):
    stop = threading.Event()
    reservation_lock = threading.Lock()

    def capture_loop():
        previous = None
        last_report = 0
        while not stop.is_set():
            try:
                with reservation_lock:
                    runtime.queue.observe(Path(runtime.config["vault_path"]), idle_cache=True)
                current = ("observando", None)
            except (OSError, ValueError) as error:
                current = ("error-captura", type(error).__name__)
            if current != previous or time.monotonic() - last_report >= 30:
                with reservation_lock:
                    report_state(runtime, "capture", *current)
                previous, last_report = current, time.monotonic()
            stop.wait(runtime.config["poll_seconds"])

    capture = threading.Thread(target=capture_loop, name="captura-boveda", daemon=True)
    paused = False
    prefer_latest = True
    report_state(runtime, "uploader", "activo")
    capture.start()
    try:
        while True:
            if not paused:
                try:
                    uploaded = run_batch(
                        runtime,
                        reservation_lock=reservation_lock,
                        prefer_latest=prefer_latest,
                    )
                    # Alterna el respaldo vigente con el histórico durante una ráfaga.
                    if not uploaded:
                        prefer_latest = True
                    elif uploaded % 2:
                        prefer_latest = not prefer_latest
                except FileExistsError:
                    pass
                except (OSError, ValueError) as error:
                    paused = True  # Root reconcilia; capturar continúa sin volver a subir.
                    report_state(
                        runtime, "uploader", "reconciliacion-requerida", type(error).__name__
                    )
            stop.wait(1)
    finally:
        stop.set()
        capture.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("plan", "prepare", "once", "watch", "status"), default="plan"
    )
    parser.add_argument("--config")
    parser.add_argument("--config-sha256")
    parser.add_argument("--_upload", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--generation", help=argparse.SUPPRESS)
    parser.add_argument("--attempt-nonce", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.mode == "plan" and not args._upload:
        print(encoded(proposal()).decode())
        return 0
    try:
        require(
            args.config and re.fullmatch(r"[a-f0-9]{64}", args.config_sha256 or ""),
            "CONFIG_PIN_REQUIRED",
        )
        require(not args._upload or args.mode == "plan", "CHILD_MODE_COMBINATION")
        require(sys.flags.isolated == sys.flags.no_site == 1, "ISOLATED_PYTHON_REQUIRED")
        runtime = bootstrap_runtime(
            args.config, args.config_sha256, preparing=args.mode == "prepare"
        )
        if args._upload:
            verify_child_job(runtime.job())
            result = upload_child(runtime, args.generation, args.attempt_nonce)
        elif args.mode == "prepare":
            result = {
                "success": True,
                "queue_identity": runtime.store.prepare(),
                "network_used": False,
            }
        elif args.mode == "status":
            with runtime.store.lock("status"):
                rows = runtime.queue.generations()
                ledger = runtime.store.json("ledger.json")
            result = {
                "success": True,
                "generations": len(rows),
                "capture": ledger.get("capture"),
                "uploader": ledger.get("uploader"),
                "uploader_lock_present": (
                    runtime.store.root / "operations/uploader.lock.json"
                ).exists(),
                "network_used": False,
                "vault_written": False,
            }
        elif args.mode == "once":
            runtime.queue.observe(Path(runtime.config["vault_path"]))
            result = run_one(runtime)
        else:
            if any(
                os.path.lexists(runtime.store.root / name)
                for name in (".active.json", "operations/uploader.lock.json")
            ):
                boot_ns = windows_boot_ns()
                with windows_boot_recovery_mutex():
                    runtime.queue.archive_previous_boot_locks(boot_ns)
            watch(runtime)
            return 0
        print(encoded(result).decode())
        return 0 if result.get("success") is True else 2
    except Exception as error:
        print(
            encoded(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "error": "VAULT_CHANGES_REJECTED",
                    "requires_manual_reconciliation": True,
                    "values_hidden": True,
                }
            ).decode()
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
