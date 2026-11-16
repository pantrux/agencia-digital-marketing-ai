"""Reconcilia sólo el primer intento pendiente acreditado de la cola de bóveda V2."""

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
from pathlib import Path

CODE = Path(
    r"D:\Proyectos\Agencia Digital de Marketing AI\.tools\backup-source-build\vault-changes-code-v2"
)
CONFIG = CODE / "config-runtime-v2.json"
CONFIG_SHA = "bb22175a7d9fe4911882d6c3e55f8eaadc9c029acc3aaf4d6740db9f728fc2a5"
WORKER_SHA = "b5d3d03462948f0117bdb28d0c280ebfb8a0feb001b852a32c6b43d4a7d84b55"
PYTHON_SHA = "cfd05f0ad320310c4891a5fd297ec96e25f4a23fe64f3fb06df67004198c4ab5"
RECORDS = {
    "prepared.json": "35045f41c58804f1ecbc2e2925c9b40e195b4f30982c83541605c697f0f32262",
    "attempt.json": "c1c4d57b696bc2760f6114bd10b79ffbe3333a5b9ba679f26b43519d86029f27",
    "claim.json": "0af6f833e393e9eab6f13d59de318c0b461b2baf78431e49900d10e75b4fac59",
    "snapshot-recorded.json": "034660c2a1046e142a55b48c62b670235acd2c3e95b5dcd658d636619c86ac8b",
    "result.json": "5a1ba72440d64072adf5aabd667fedecaf6993b58f6dae7003b59a5bde253604",
}
LOCKS = (
    (
        "operations/uploader.lock.json",
        "archived-uploader.json",
        "cc37433710d2eed4a5de28839f61e097500357ffa7cd5627629b7a4644828204",
        70368744177685447,
    ),
    (
        ".active.json",
        "archived-observe.json",
        "bcc0561a221cfac7a853a14bf5638220106560efba5907c61e262bc6247fe69d",
        6473924464371811,
    ),
)
SYSTEM = r"C:\Windows"
LOCK_DEVICE = 5947135651059614097
PROCESS_CHECK = r"""
$ErrorActionPreference = 'Stop'
$rows = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name LIKE 'restic%.exe'")
$active = @($rows | Where-Object { $_.ProcessId -ne [int]$env:VAULT_RECONCILE_PID -and
  ($_.Name -like 'restic*.exe' -or -not $_.CommandLine -or
  $_.CommandLine -match '(vault_change_worker|reconcile_vault_changes_v2)\.py') })
if ($active.Count) { exit 4 }
exit 0
"""


class Rejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Rejected(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def no_workers():
    environment = {
        "SystemRoot": SYSTEM,
        "WINDIR": SYSTEM,
        "PATH": SYSTEM + r"\System32",
        "VAULT_RECONCILE_PID": str(os.getpid()),
    }
    result = subprocess.run(
        [
            SYSTEM + r"\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            PROCESS_CHECK,
        ],
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    require(
        result.returncode == 0 and not result.stderr,
        "WORKER_OR_RESTIC_ACTIVE_OR_PROCESS_CHECK_FAILED",
    )


def bootstrap():
    require(
        os.name == "nt" and sys.flags.isolated and sys.flags.no_site, "ISOLATED_WINDOWS_REQUIRED"
    )
    require(sha(Path(sys.executable).read_bytes()) == PYTHON_SHA, "PYTHON_PIN")
    path = CODE / "vault_change_worker.py"
    raw = path.read_bytes()
    require(sha(raw) == WORKER_SHA, "WORKER_PIN")
    worker = types.ModuleType("vault_reconciliation_worker")
    worker.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), worker.__dict__)
    runtime = worker.bootstrap_runtime(str(CONFIG), CONFIG_SHA)
    runtime.verify_binary()
    return worker, runtime


def read_locked(runtime, name, pin, inode):
    path = runtime.store.root / name
    raw, info = runtime.state.read_regular(path, runtime.access, 4096)
    item = runtime.state.decode(raw)
    require(
        info["sha256"] == pin
        and info["inode"] == inode
        and info["device"] == LOCK_DEVICE
        and item.get("owner") == runtime.config["owner"]
        and re.fullmatch(r"[a-f0-9]{32}", item.get("nonce", "")),
        "LOCK_IDENTITY",
    )


def inspect_locks(runtime, prefix, *, allow_archived):
    for original, archived, pin, inode in LOCKS:
        destination = prefix + "/" + archived
        if os.path.lexists(runtime.store.root / destination):
            require(
                allow_archived and not os.path.lexists(runtime.store.root / original),
                "LOCK_ARCHIVE_CONFLICT",
            )
            read_locked(runtime, destination, pin, inode)
        else:
            read_locked(runtime, original, pin, inode)


def subject(runtime, *, allow_ack=False):
    queue = runtime.queue
    generation = None
    for row in queue.generations():
        candidate_id = row["generation_id"]
        prepared = runtime.store.read(queue.path(candidate_id, "prepared.json"))
        if sha(prepared) == RECORDS["prepared.json"]:
            generation = candidate_id
            break
        require(
            (runtime.store.root / queue.path(candidate_id, "uploaded.json")).is_file(),
            "EARLIER_PENDING",
        )
    require(generation is not None and row["status"] == "OBSERVED", "PENDING_SUBJECT")
    require(
        allow_ack
        or not os.path.lexists(runtime.store.root / queue.path(generation, "uploaded.json")),
        "ALREADY_ACKNOWLEDGED",
    )
    contents = {}
    for name, pin in RECORDS.items():
        raw = runtime.store.read(queue.path(generation, name))
        require(sha(raw) == pin, "PENDING_RECEIPT_PIN")
        contents[name] = runtime.state.decode(raw)
    attempt = contents["attempt.json"]
    claim = contents["claim.json"]
    snapshot = contents["snapshot-recorded.json"]
    failure = contents["result.json"]
    snapshot_id = snapshot["snapshot_id"]
    require(
        all(item["generation_id"] == generation for item in contents.values())
        and re.fullmatch(r"[a-f0-9]{64}", snapshot_id)
        and attempt["child_nonce"] == claim["child_nonce"]
        and attempt["subject"]["repository_id"] == runtime.config["repository_id"]
        and attempt["sha256"]
        == attempt["subject"]["sha256"]
        == snapshot["sha256"]
        == failure["sha256"]
        == row["sha256"]
        and attempt["subject"]["publication_status"]
        == snapshot["publication_status"]
        == failure["publication_status"]
        == "OBSERVED"
        and failure["snapshot_id"] == snapshot_id
        and failure["success"] is False
        and failure["requires_manual_reconciliation"] is True,
        "PENDING_RECEIPT_LINKAGE",
    )
    provider = runtime.backup_helpers()
    provider.parse_config(
        runtime.store.read("operations/" + generation + "-cat.stdout"),
        runtime.config["repository_id"],
    )
    require(
        provider.parse_backup(runtime.store.read("operations/" + generation + "-backup.stdout"))
        == snapshot_id,
        "ORIGINAL_BACKUP_PROOF",
    )
    for stage, code in (("cat", 0), ("backup", 0), ("outer", 2)):
        check_job(runtime.store.json("operations/" + generation + "-" + stage + ".job.json"), code)
    previous_ls = runtime.store.json("operations/" + generation + "-ls.job.json")
    require(
        previous_ls["status"] == "cleanup_error" and previous_ls["cleanup_complete"] is False,
        "ORIGINAL_LISTING_STATE",
    )
    queue.candidate(generation)
    return provider, row, snapshot_id


def check_job(metadata, expected_code=0):
    require(
        metadata["status"] == "completed"
        and metadata["exit_code"] == expected_code
        and metadata["cleanup_complete"] is True,
        "INCOMPLETE_JOB",
    )


def restic(runtime, job, environment, prefix, name, arguments, timeout):
    runtime.verify_binary()
    result = job.run_job(
        Path(runtime.profile["binary_path"]),
        [
            "--json",
            "--no-lock",
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
            *arguments,
        ],
        cwd=runtime.store.root / "profile",
        env=environment,
        timeout=timeout,
        limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
        output_limit=1024**2,
    )
    job_metadata = {
        key: value for key, value in vars(result).items() if key not in {"stdout", "stderr"}
    }
    runtime.store.write(prefix + "/" + name + ".stdout", result.stdout)
    runtime.store.write(prefix + "/" + name + ".stderr", result.stderr)
    runtime.store.write(prefix + "/" + name + ".job.json", runtime.state.encoded(job_metadata))
    check_job(job_metadata)
    require(result.stderr == b"", "RESTIC_STDERR")
    runtime.verify_binary()
    return result.stdout


def check_restore(runtime, restore_relative, row):
    target = runtime.store.root / restore_relative
    runtime.state.ancestors(target)
    runtime.access.check(target)
    require({file.name for file in target.iterdir()} == {"candidate.env"}, "RESTORE_CONTENTS")
    _raw, info = runtime.state.read_regular(
        target / "candidate.env", runtime.access, runtime.config["maximum_vault_bytes"]
    )
    require(
        info["sha256"] == row["sha256"] and info["bytes"] == row["bytes"], "RESTORE_SHA_OR_SIZE"
    )
    return info


def locations(operation):
    return "operations/reconciliation-" + operation, "restores/reconciliation-" + operation


def verify(runtime, operation):
    no_workers()
    prefix, restore_relative = locations(operation)
    inspect_locks(runtime, prefix, allow_archived=False)
    provider, row, snapshot = subject(runtime)
    generation = row["generation_id"]
    runtime.store.mkdir(prefix)
    runtime.store.mkdir(restore_relative)
    target = runtime.store.root / restore_relative
    target_id = runtime.state.identity(runtime.state.node(target, True))
    require(not any(target.iterdir()), "RESTORE_TARGET_NOT_EMPTY")
    runtime.store.write(
        prefix + "/intent.json",
        runtime.state.encoded({"generation_id": generation, "record_pins": RECORDS}),
    )
    values = runtime.current_values()
    environment = provider.environment({**runtime.profile, "root": str(runtime.store.root)}, values)
    client = provider.S3.OwnerClient(
        runtime.profile["repository_url"],
        values["R2_PLATFORM_ACCESS_KEY_ID"],
        values["R2_PLATFORM_SECRET_ACCESS_KEY"],
    )
    del values
    client.verify(
        provider.STATE.encoded(
            {
                "schema_version": 1,
                "owner": runtime.profile["owner"],
                "operation_id": runtime.profile["operation_id"],
            }
        )
    )
    job = runtime.job()
    config = restic(runtime, job, environment, prefix, "cat", ["cat", "config"], 60)
    provider.parse_config(config, runtime.config["repository_id"])
    listing = restic(runtime, job, environment, prefix, "ls", ["ls", snapshot], 120)
    header = provider.events(listing)[0]
    require(
        header["hostname"] == runtime.profile["hostname"]
        and sorted(header["tags"]) == sorted(("vault-changes-v1", generation, "OBSERVED")),
        "SNAPSHOT_HOST_OR_TAGS",
    )
    capture = runtime.store.root / runtime.queue.path(generation, "candidate.env")
    parent = provider.parse_listing(listing, snapshot, str(capture), row["bytes"])
    restored = restic(
        runtime,
        job,
        environment,
        prefix,
        "restore",
        [
            "restore",
            snapshot + ":" + parent,
            "--target",
            str(target),
            "--verify",
            "--overwrite",
            "never",
            "--include",
            "/candidate.env",
        ],
        180,
    )
    require(
        runtime.state.identity(runtime.state.node(target, True)) == target_id,
        "RESTORE_ROOT_CHANGED",
    )
    info = check_restore(runtime, restore_relative, row)
    runtime.queue.candidate(generation)
    inspect_locks(runtime, prefix, allow_archived=False)
    receipt = runtime.state.encoded(
        {
            "kind": "vault-v2-reconciliation-verified",
            "operation": operation,
            "config_sha256": CONFIG_SHA,
            "record_pins": RECORDS,
            "generation_id": generation,
            "snapshot_id": snapshot,
            "restored_file": info,
            "logs": {"cat": sha(config), "ls": sha(listing), "restore": sha(restored)},
        }
    )
    runtime.store.write(prefix + "/verified.json", receipt)
    return {
        "success": True,
        "operation": operation,
        "receipt_sha256": sha(receipt),
        "uploaded": False,
    }


def acknowledge(runtime, operation, receipt_pin):
    no_workers()
    prefix, restore_relative = locations(operation)
    _provider, row, snapshot = subject(runtime, allow_ack=True)
    generation = row["generation_id"]
    raw = runtime.store.read(prefix + "/verified.json")
    require(sha(raw) == receipt_pin, "VERIFY_RECEIPT_PIN")
    receipt = runtime.state.decode(raw)
    require(
        receipt["kind"] == "vault-v2-reconciliation-verified"
        and receipt["operation"] == operation
        and receipt["config_sha256"] == CONFIG_SHA
        and receipt["record_pins"] == RECORDS
        and receipt["generation_id"] == generation
        and receipt["snapshot_id"] == snapshot
        and receipt["restored_file"] == check_restore(runtime, restore_relative, row),
        "VERIFY_RECEIPT_LINKAGE",
    )
    for stage in ("cat", "ls", "restore"):
        check_job(runtime.store.json(prefix + "/" + stage + ".job.json"))
        require(
            runtime.store.read(prefix + "/" + stage + ".stderr") == b""
            and sha(runtime.store.read(prefix + "/" + stage + ".stdout")) == receipt["logs"][stage],
            "VERIFY_JOB_LOG",
        )
    relative = runtime.queue.path(generation, "uploaded.json")
    acknowledged = runtime.store.root / relative
    exists = os.path.lexists(acknowledged)
    inspect_locks(runtime, prefix, allow_archived=exists)
    # Se publica primero el ACK. Si la operación se interrumpe, el mismo recibo
    # permite terminar de archivar los locks que aún falten sin repetir el backup.
    ack = {
        "success": True,
        "generation_id": generation,
        "sha256": row["sha256"],
        "snapshot_id": snapshot,
        "publication_status": "OBSERVED",
        "publication_proved": False,
    }
    staged = prefix + "/ack.pending.json"
    if not exists and not os.path.lexists(runtime.store.root / staged):
        runtime.store.write(staged, runtime.state.encoded({**ack, "uploaded_ns": time.time_ns()}))
    payload_raw = runtime.store.read(relative if exists else staged)
    payload = runtime.state.decode(payload_raw)
    require(
        type(payload.get("uploaded_ns")) is int
        and {**ack, "uploaded_ns": payload["uploaded_ns"]} == payload,
        "ACK_CONFLICT",
    )
    intent = prefix + "/ack.intent.json"
    expected_intent = {"ack_sha256": sha(payload_raw), "verify_sha256": receipt_pin}
    if not exists and not os.path.lexists(runtime.store.root / intent):
        runtime.store.write(intent, runtime.state.encoded(expected_intent))
    require(runtime.store.json(intent) == expected_intent, "ACK_INTENT_CONFLICT")
    if not exists:
        no_workers()
        os.rename(runtime.store.root / staged, acknowledged)
    require(runtime.store.json(relative) == payload, "ACK_WRITE")
    for original, archived, pin, inode in LOCKS:
        archive = prefix + "/" + archived
        if not os.path.lexists(runtime.store.root / archive):
            no_workers()
            read_locked(runtime, original, pin, inode)
            os.rename(runtime.store.root / original, runtime.store.root / archive)
        read_locked(runtime, archive, pin, inode)
    return {
        "success": True,
        "operation": operation,
        "uploaded": True,
        "locks_archived": 2,
        "network_used": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "verify", "ack"))
    parser.add_argument("--operation")
    parser.add_argument("--receipt-sha256")
    args = parser.parse_args()
    operation = None
    try:
        if args.mode == "plan":
            result = {
                "success": True,
                "config_sha256": CONFIG_SHA,
                "worker_sha256": WORKER_SHA,
                "python_sha256": PYTHON_SHA,
                "network_used": False,
                "operational_state_read": False,
            }
        elif args.mode == "verify":
            require(not args.operation and not args.receipt_sha256, "UNEXPECTED_VERIFY_ARGUMENT")
            operation = uuid.uuid4().hex
            _worker, runtime = bootstrap()
            result = verify(runtime, operation)
        else:
            require(
                re.fullmatch(r"[a-f0-9]{32}", args.operation or "")
                and re.fullmatch(r"[a-f0-9]{64}", args.receipt_sha256 or ""),
                "ACK_OPERATION_AND_RECEIPT_REQUIRED",
            )
            _worker, runtime = bootstrap()
            result = acknowledge(runtime, args.operation, args.receipt_sha256)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # Los mensajes de bibliotecas pueden incluir rutas privadas o IDs.
        safe = str(error) if isinstance(error, Rejected) else type(error).__name__
        print(
            json.dumps(
                {"success": False, "error": safe, "operation": operation, "values_hidden": True}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
