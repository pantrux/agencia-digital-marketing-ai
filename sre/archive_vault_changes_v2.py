"""Archiva candidatas confirmadas V2 en el mismo volumen mediante un plan fijado."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import types
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

CODE = Path(
    r"D:\Proyectos\Agencia Digital de Marketing AI\.tools\backup-source-build\vault-changes-code-v2"
)
CONFIG = CODE / "config-runtime-v2.json"
CONFIG_SHA = "bb22175a7d9fe4911882d6c3e55f8eaadc9c029acc3aaf4d6740db9f728fc2a5"
WORKER_SHA = "b5d3d03462948f0117bdb28d0c280ebfb8a0feb001b852a32c6b43d4a7d84b55"
PYTHON_SHA = "cfd05f0ad320310c4891a5fd297ec96e25f4a23fe64f3fb06df67004198c4ab5"
ARCHIVE = "quota-archive-v2"
ARCHIVE_LOCK = "operations/quota-archive-v2.lock"
KIND = "vault-quota-archive-plan-v2"
MAX_BATCH = 32
LIVE_PLAN = "operations/quota-live-plan-v2.json"
LIVE_OPERATION = "quota-live-archive"
PROCESS_CHECK = r"""
$ErrorActionPreference = 'Stop'
$rows = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name LIKE 'restic%.exe'")
$active = @($rows | Where-Object { $_.Name -like 'restic*.exe' -or -not $_.CommandLine -or
  $_.CommandLine -match '(vault_change_worker|vault_updates_v2|reconcile_vault_changes_v2)\.py' })
if ($active.Count) { exit 4 }
exit 0
"""


class Rejected(ValueError):
    pass


class IncompleteUploadProof(Rejected):
    """La captura existe, pero algún proceso de copia no dejó una prueba completa."""


def require(condition, code):
    if not condition:
        raise Rejected(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def bootstrap():
    require(
        os.name == "nt" and sys.flags.isolated and sys.flags.no_site, "ISOLATED_WINDOWS_REQUIRED"
    )
    require(sha(Path(sys.executable).read_bytes()) == PYTHON_SHA, "PYTHON_PIN")
    path = CODE / "vault_change_worker.py"
    raw = path.read_bytes()
    require(sha(raw) == WORKER_SHA, "WORKER_PIN")
    worker = types.ModuleType("vault_archive_worker")
    worker.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), worker.__dict__)
    return worker.bootstrap_runtime(str(CONFIG), CONFIG_SHA)


def safe_stop_check(runtime):
    system = r"C:\Windows"
    result = subprocess.run(
        [
            system + r"\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            PROCESS_CHECK,
        ],
        env={"SystemRoot": system, "WINDIR": system, "PATH": system + r"\System32"},
        capture_output=True,
        timeout=15,
        check=False,
    )
    require(result.returncode == 0 and not result.stderr, "WORKER_ACTIVE_OR_PROCESS_CHECK_FAILED")
    check_watcher_locks(runtime)


def check_watcher_locks(runtime):
    root = runtime.store.root
    require(
        not any(
            os.path.lexists(root / name)
            for name in (".active.json", "operations/uploader.lock.json")
        ),
        "WATCHER_LOCK_PRESENT",
    )


def file_bytes(runtime, path, maximum=1024**2):
    return runtime.state.read_regular(path, runtime.access, maximum)


def job_complete(record):
    return (
        record.get("status") == "completed"
        and record.get("exit_code") == 0
        and record.get("cleanup_complete") is True
    )


def automatic_ack_proof(runtime, provider, row, files):
    """Acredita el ACK contra el hijo y los tres resultados cerrados de restic."""
    decode = runtime.state.decode
    generation = row["generation_id"]
    operations = runtime.store.root / "operations"
    evidence = {}
    logs = {}
    for stage in ("outer", "cat", "backup", "ls"):
        name = generation + "-" + stage
        for extension in (".job.json", ".stdout", ".stderr"):
            relative = name + extension
            logs[stage + extension], evidence[relative] = file_bytes(runtime, operations / relative)
        if not job_complete(decode(logs[stage + ".job.json"])) or logs[stage + ".stderr"]:
            raise IncompleteUploadProof("INCOMPLETE_UPLOAD_PROOF")
    ack = decode(files["uploaded.json"])
    result = decode(files["result.json"])
    attempt = decode(files["attempt.json"])
    claim = decode(files["claim.json"])
    recorded = decode(files["snapshot-recorded.json"])
    snapshot = ack.get("snapshot_id", "")
    expected = {
        "generation_id": generation,
        "sha256": row["sha256"],
        "snapshot_id": snapshot,
        "publication_status": "OBSERVED",
    }
    require(re.fullmatch(r"[a-f0-9]{64}", snapshot) and recorded == expected, "SNAPSHOT_RECEIPT")
    require(
        result == decode(logs["outer.stdout"]) == {**expected, "success": True}, "OUTER_RECEIPT"
    )
    require(
        type(ack.get("uploaded_ns")) is int
        and ack == {**result, "uploaded_ns": ack["uploaded_ns"], "publication_proved": False},
        "ACK_RECEIPT",
    )
    require(
        attempt["owner"] == runtime.config["owner"]
        and attempt["generation_id"] == claim["generation_id"] == generation
        and attempt["sha256"] == row["sha256"]
        and attempt["child_nonce"] == claim["child_nonce"]
        and re.fullmatch(r"[a-f0-9]{32}", claim["child_nonce"])
        and attempt["subject"]
        == {
            "repository_id": runtime.config["repository_id"],
            "sha256": row["sha256"],
            "publication_status": "OBSERVED",
        },
        "ATTEMPT_RECEIPT",
    )
    provider.parse_config(logs["cat.stdout"], runtime.config["repository_id"])
    require(provider.parse_backup(logs["backup.stdout"]) == snapshot, "BACKUP_RECEIPT")
    listing = logs["ls.stdout"]
    capture = str(runtime.store.root / "captures" / generation / "candidate.env")
    provider.parse_listing(listing, snapshot, capture, row["bytes"])
    header = provider.events(listing)[0]
    require(
        header.get("hostname") == runtime.profile["hostname"]
        and sorted(header.get("tags", [])) == sorted(("vault-changes-v1", generation, "OBSERVED")),
        "SNAPSHOT_SUBJECT",
    )
    return evidence


@contextmanager
def archive_lock(runtime):
    import msvcrt

    store = runtime.store
    path = store.root / ARCHIVE_LOCK
    if not os.path.lexists(path):
        try:
            store.write(ARCHIVE_LOCK, b"vault-quota-archive-v2\n")
        except FileExistsError:
            pass
    raw, info = file_bytes(runtime, path, 128)
    require(raw == b"vault-quota-archive-v2\n", "ARCHIVE_LOCK_INVALID")
    with path.open("r+b") as stream:
        handle = os.fstat(stream.fileno())
        require(
            (handle.st_dev, handle.st_ino) == (info["device"], info["inode"]),
            "ARCHIVE_LOCK_CHANGED",
        )
        # Windows suelta el bloqueo del byte al cerrar el handle, incluso tras un crash.
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        yield


def directory_identity(runtime, directory):
    runtime.state.ancestors(directory)
    runtime.access.check(directory)
    info = runtime.state.node(directory, directory=True)
    return [info.st_dev, info.st_ino]


def generation_proof(runtime, provider, directory, row):
    directory_id = directory_identity(runtime, directory)
    # Sólo se admiten capturas observadas con los seis archivos del flujo automático.
    names = {
        "candidate.env",
        "prepared.json",
        "attempt.json",
        "claim.json",
        "snapshot-recorded.json",
        "result.json",
        "uploaded.json",
    }
    require({file.name for file in directory.iterdir()} == names, "GENERATION_LAYOUT")
    files, fingerprints = {}, {}
    for name in sorted(names):
        if name == "candidate.env":
            _raw, info = file_bytes(
                runtime, directory / name, runtime.config["maximum_vault_bytes"]
            )
            require(
                info["sha256"] == row["sha256"] and info["bytes"] == row["bytes"],
                "CANDIDATE_SHA_OR_SIZE",
            )
            del _raw
        else:
            files[name], info = file_bytes(runtime, directory / name)
        fingerprints[name] = info
    prepared = runtime.state.decode(files["prepared.json"])
    require(
        all(
            prepared[key] == row[key] for key in ("generation_id", "sha256", "bytes", "prepared_ns")
        )
        and prepared["source_kind"] == "OBSERVED",
        "PREPARED_RECEIPT",
    )
    evidence = automatic_ack_proof(runtime, provider, row, files)
    require(directory_identity(runtime, directory) == directory_id, "GENERATION_DIRECTORY_CHANGED")
    return {"row": row, "directory": directory_id, "files": fingerprints, "jobs": evidence}


def confirmed_entries(runtime, rows, limit, *, retain_incomplete=False):
    store = runtime.store
    provider = runtime.backup_helpers()
    entries = []
    for row in rows[:-1]:
        generation = row["generation_id"]
        directory = store.root / "captures" / generation
        if row["status"] != "OBSERVED" or not os.path.lexists(directory / "uploaded.json"):
            continue
        # Un ACK conciliado requiere una prueba diferente. Este operador deja esa fila en la cola.
        raw, _info = file_bytes(runtime, directory / "result.json")
        if runtime.state.decode(raw).get("success") is not True:
            continue
        try:
            proof = generation_proof(runtime, provider, directory, row)
        except IncompleteUploadProof:
            if retain_incomplete:
                continue
            raise
        entries.append(proof)
        if len(entries) == limit:
            break
    return entries


def record_plan(runtime, operation, latest, entries):
    store = runtime.store
    if not os.path.lexists(store.root / ARCHIVE):
        store.mkdir(ARCHIVE)
    archive_id = directory_identity(runtime, store.root / ARCHIVE)
    prefix = ARCHIVE + "/" + operation
    store.mkdir(prefix)
    batch_id = directory_identity(runtime, store.root / prefix)
    plan = {
        "kind": KIND,
        "operation": operation,
        "config_sha256": CONFIG_SHA,
        "archive": archive_id,
        "batch": batch_id,
        "latest": latest,
        "entries": entries,
    }
    raw = runtime.state.encoded(plan)
    store.write(prefix + "/plan.json", raw)
    return {
        "operation": operation,
        "plan_sha256": sha(raw),
        "generations": len(entries),
        "candidate_bytes": sum(entry["row"]["bytes"] for entry in entries),
    }


def make_plan(runtime, operation, limit):
    safe_stop_check(runtime)
    rows = runtime.queue.generations()
    entries = confirmed_entries(runtime, rows, limit)
    require(entries, "NO_CONFIRMED_GENERATIONS_TO_ARCHIVE")
    return record_plan(runtime, operation, rows[-1]["generation_id"], entries)


def inspect_entry(runtime, provider, prefix, entry, active_rows, plan_pin):
    store = runtime.store
    row = entry["row"]
    generation = row["generation_id"]
    require(re.fullmatch(r"[a-f0-9]{32}", generation), "PLAN_GENERATION")
    source = store.root / "captures" / generation
    target = store.root / prefix / generation
    source_exists, target_exists = os.path.lexists(source), os.path.lexists(target)
    require(source_exists != target_exists, "ARCHIVE_LOCATION_CONFLICT")
    done = prefix + "/" + generation + ".done.json"
    done_exists = os.path.lexists(store.root / done)
    expected_done = {
        "plan_sha256": plan_pin,
        "generation_id": generation,
        "identity": entry["directory"],
    }
    if done_exists:
        require(target_exists and store.json(done) == expected_done, "ARCHIVE_RECEIPT_CONFLICT")
    if source_exists:
        require(active_rows.get(generation) == row, "SOURCE_QUEUE_CHANGED")
    proof = generation_proof(runtime, provider, source if source_exists else target, row)
    require(proof == entry, "ARCHIVE_ENTRY_CHANGED")
    return source, target, source_exists, done, done_exists, expected_done


def apply_plan(runtime, operation, plan_pin):
    safe_stop_check(runtime)
    return apply_checked_plan(runtime, operation, plan_pin, check_watcher_locks)


def apply_checked_plan(runtime, operation, plan_pin, check_locks):
    store = runtime.store
    prefix = ARCHIVE + "/" + operation
    raw = store.read(prefix + "/plan.json")
    require(sha(raw) == plan_pin, "PLAN_SHA")
    plan = runtime.state.decode(raw)
    archive_id = directory_identity(runtime, store.root / ARCHIVE)
    batch_id = directory_identity(runtime, store.root / prefix)
    require(
        plan["kind"] == KIND
        and plan["operation"] == operation
        and plan["config_sha256"] == CONFIG_SHA
        and plan["archive"] == archive_id
        and plan["batch"] == batch_id,
        "PLAN_IDENTITY",
    )
    entries = plan["entries"]
    generations = {entry["row"]["generation_id"] for entry in entries}
    rows = runtime.queue.generations()
    require(
        0 < len(generations) == len(entries) <= MAX_BATCH
        and rows
        and plan["latest"] not in generations
        and rows[-1]["generation_id"] not in generations
        and all(entry["directory"][0] == batch_id[0] for entry in entries),
        "PLAN_SCOPE",
    )
    active_rows = {row["generation_id"]: row for row in rows}
    provider = runtime.backup_helpers()
    # Valida el lote completo antes de mover y vuelve a comprobar cada ruta en uso.
    for entry in entries:
        inspect_entry(runtime, provider, prefix, entry, active_rows, plan_pin)
    moved = 0
    for entry in entries:
        source, target, present, done, done_exists, receipt = inspect_entry(
            runtime, provider, prefix, entry, active_rows, plan_pin
        )
        if done_exists:
            continue
        check_locks(runtime)
        if present:
            # En Windows rename dentro del mismo volumen falla si el destino existe.
            os.rename(source, target)
            moved += 1
        require(
            directory_identity(runtime, target) == entry["directory"], "ARCHIVE_DIRECTORY_CHANGED"
        )
        # Un crash durante write no deja un recibo final truncado. El plan basta
        # para reanudar si queda una copia temporal sin publicar.
        temporary = done + ".pending-" + uuid.uuid4().hex
        store.write(temporary, runtime.state.encoded(receipt))
        os.rename(store.root / temporary, store.root / done)
        require(store.json(done) == receipt, "ARCHIVE_RECEIPT_WRITE")
    return {
        "operation": operation,
        "generations": len(entries),
        "newly_moved": moved,
        "candidate_bytes": sum(entry["row"]["bytes"] for entry in entries),
    }


def maintain_once(runtime):
    """Mueve una candidata completa con ambos bloqueos y el lock de archivo del llamador."""
    store = runtime.store
    with ExitStack() as locks:
        try:
            locks.enter_context(runtime.queue.uploader_lock())
            locks.enter_context(store.lock(LIVE_OPERATION))
        except FileExistsError:
            return {"state": "ocupada", "newly_moved": 0}

        paths = (store.root / ".active.json", store.root / "operations/uploader.lock.json")
        claimed = [file_bytes(runtime, path, 4096) for path in paths]
        for raw, _ in claimed:
            require(
                runtime.state.decode(raw).get("owner") == runtime.config["owner"], "LIVE_LOCK_OWNER"
            )
        require(
            runtime.state.decode(claimed[0][0]).get("operation") == LIVE_OPERATION,
            "LIVE_LOCK_OPERATION",
        )

        def confirm_locks(current):
            require(
                [file_bytes(current, path, 4096) for path in paths] == claimed, "LIVE_LOCK_CHANGED"
            )

        confirm_locks(runtime)
        pending = store.root / LIVE_PLAN
        resumed = os.path.lexists(pending)
        if resumed:
            raw, info = file_bytes(runtime, pending, 1024)
            plan = runtime.state.decode(raw)
        else:
            rows = runtime.queue.generations()
            used = sum(row["bytes"] for row in rows)
            if (
                used * 10 < runtime.config["maximum_queue_bytes"] * 9
                and len(rows) * 10 < runtime.config["maximum_generations"] * 9
            ):
                return {"state": "dentro-de-cuota", "newly_moved": 0}
            entries = confirmed_entries(runtime, rows, 1, retain_incomplete=True)
            if not entries:
                return {"state": "sin-confirmadas", "newly_moved": 0}
            published = record_plan(runtime, uuid.uuid4().hex, rows[-1]["generation_id"], entries)
            plan = {key: published[key] for key in ("operation", "plan_sha256")}
            temporary = LIVE_PLAN + ".pending-" + uuid.uuid4().hex
            store.write(temporary, runtime.state.encoded(plan))
            os.rename(store.root / temporary, pending)
            raw, info = file_bytes(runtime, pending, 1024)
        require(
            set(plan) == {"operation", "plan_sha256"}
            and re.fullmatch(r"[a-f0-9]{32}", plan["operation"])
            and re.fullmatch(r"[a-f0-9]{64}", plan["plan_sha256"]),
            "LIVE_PLAN_FORMAT",
        )
        result = apply_checked_plan(runtime, plan["operation"], plan["plan_sha256"], confirm_locks)
        confirm_locks(runtime)
        require(file_bytes(runtime, pending, 1024) == (raw, info), "LIVE_PLAN_CHANGED")
        pending.unlink()
        return {"state": "archivada", "reanudada": resumed, **result}


def maintain_batch(runtime, limit):
    """Reintenta cuando ambos bloqueos se liberan entre consultas del observador."""
    deadline = time.monotonic() + 20
    retries = 0
    history = []
    while len(history) < limit:
        result = maintain_once(runtime)
        if result["state"] == "ocupada":
            remaining = deadline - time.monotonic()
            if remaining > 0:
                retries += 1
                time.sleep(min(0.2, remaining))
                continue
        history.append(result)
        if result["state"] != "archivada":
            break
    return {
        "state": history[-1]["state"],
        "newly_moved": sum(row["newly_moved"] for row in history),
        "busy_retries": retries,
        "steps": history,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="mode", required=True)
    plan = actions.add_parser("plan")
    plan.add_argument("--limit", type=int, default=16)
    apply = actions.add_parser("apply")
    apply.add_argument("--operation", required=True)
    apply.add_argument("--plan-sha256", required=True)
    maintenance = actions.add_parser("maintain")
    maintenance.add_argument("--limit", type=int, default=4)
    args = parser.parse_args()
    operation = getattr(args, "operation", None)
    try:
        if args.mode == "plan":
            require(1 <= args.limit <= MAX_BATCH, "PLAN_BATCH_LIMIT")
            operation = uuid.uuid4().hex
        elif args.mode == "apply":
            require(
                re.fullmatch(r"[a-f0-9]{32}", operation)
                and re.fullmatch(r"[a-f0-9]{64}", args.plan_sha256),
                "PLAN_ARGUMENTS",
            )
        else:
            require(1 <= args.limit <= 8, "LIVE_BATCH_LIMIT")
        runtime = bootstrap()
        if args.mode != "maintain":
            safe_stop_check(runtime)
        with archive_lock(runtime):
            if args.mode == "plan":
                result = make_plan(runtime, operation, args.limit)
            elif args.mode == "apply":
                result = apply_plan(runtime, operation, args.plan_sha256)
            else:
                result = maintain_batch(runtime, args.limit)
        print(json.dumps({"success": True, "network_used": False, **result}, sort_keys=True))
        return 0
    except Exception as error:
        safe = str(error) if isinstance(error, Rejected) else type(error).__name__
        print(
            json.dumps(
                {"success": False, "error": safe, "operation": operation, "values_hidden": True}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
