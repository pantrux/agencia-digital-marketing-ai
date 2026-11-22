"""Pausa la tarea Windows de la bóveda bajo sus dos locks y verifica su regreso."""

import argparse
import hashlib
import json
import re
import subprocess
import time
import types
from contextlib import ExitStack
from pathlib import Path

ARCHIVER_SHA = "f15374a488ad9290f6d4741402a35ee7fe38ef9c8e7f1f0723f783261ff378e9"
WATCH_PLAN_SHA = "297755f46ed413c1f3fddb622d7b8b3444c20c7d355b056992bc1c82a2ae7b3d"
WATCH_PLAN_V3_SHA = "4e9dd4989c4fac671cd66a089ec25425113c87c0a9882b814d749bf021ab7d86"
WATCH_PLAN_V4_SHA = "8a186e2ff9438044a352f3a4781120b19ad791605810b991031633e1316035b6"
WATCH_PLAN_V5_SHA = "74adfe47fd1fa5cd5271f71998b2e2f2fafd72bddcb4f1bc6c05e855566068e6"
WATCH_PLAN_V6_SHA = "89b100ee78cf58ccb69318d0db68f49be5b5d1536ec496d6c314e7a34013cf77"
WATCH_PLAN_V7_SHA = "a41b6d1a7ae83281ecf8108b06c8cc2f17ed03b130873d933c0df9c5411db929"
WATCH_PLAN_V8_SHA = "bfc1f1d305a5a9c1f7ba6d38c988b5aa392a2d158a5197875149b1594715a508"
WATCH_PLAN_V9_SHA = "cbd572d63cfeaeac67eba31bad50eb0a5d29e623ba41ba79ca40073d9ddc77cc"
WATCH_PLAN_V10_SHA = "0f1f9abd53fa28c1a01d0755e9848cd9d20c9c47428d0d18efe76c3fd03b2463"
WATCH_PLAN_V11_SHA = "02b126f6c81842f3413222d244d30275066241ba8193c6aad07bbffe5e78a220"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
LOCK_WAIT_SECONDS = 450
TASK_CONTROL = r"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $env:VAQ_TASK -TaskPath $env:VAQ_PATH
$action = @($task.Actions)
if ($action.Count -ne 1 -or $action[0].Execute -cne $env:VAQ_EXE -or
    $action[0].Arguments -cne $env:VAQ_ARGS -or
    $action[0].WorkingDirectory -cne $env:VAQ_WORKDIR -or
    $task.Principal.LogonType.ToString() -ne 'Interactive' -or
    $task.Principal.RunLevel.ToString() -ne 'Limited' -or
    $task.Settings.MultipleInstances.ToString() -ne 'IgnoreNew') { exit 11 }
$principal = [Security.Principal.NTAccount]::new($task.Principal.UserId)
$sid = $principal.Translate([Security.Principal.SecurityIdentifier]).Value
if ($sid -cne $env:VAQ_SID -or
    [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -cne $sid) { exit 12 }
$state = $task.State.ToString()
if ($env:VAQ_MODE -eq 'match') {
    [pscustomobject]@{state=$state} | ConvertTo-Json -Compress
    exit 0
}
$pidExpected = [int]$env:VAQ_PID
if ($env:VAQ_MODE -ne 'stopped') {
    $watchers = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {
        $_.ExecutablePath -ieq $env:VAQ_EXE -and $_.CommandLine -and
        $_.CommandLine.EndsWith($env:VAQ_ARGS, [StringComparison]::Ordinal)
    })
}
if ($env:VAQ_MODE -in @('inspect','stop')) {
    if ($state -ne 'Running' -or $watchers.Count -ne 1 -or
        $watchers[0].ProcessId -ne $pidExpected) { exit 13 }
    $watcher = $watchers[0]
    $schedule = Get-CimInstance Win32_Service -Filter "Name='Schedule'"
    if ($watcher.ParentProcessId -ne $schedule.ProcessId -or $watcher.SessionId -lt 1) { exit 14 }
    $born = $watcher.CreationDate.ToUniversalTime().ToString('o')
    if ($env:VAQ_MODE -eq 'stop') {
        if ($env:VAQ_BORN -cne $born -or
            @(Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $pidExpected)).Count) { exit 15 }
        $native = Get-Process -Id $pidExpected -ErrorAction Stop
        try {
            # Conserva el handle de la instancia exacta durante la espera de salida.
            $null = $native.Handle
            if ([Math]::Abs([long]($native.StartTime.ToUniversalTime().Ticks -
                $watcher.CreationDate.ToUniversalTime().Ticks)) -gt 10000) { exit 15 }
            $scheduler = New-Object -ComObject Schedule.Service
            $scheduler.Connect()
            $registered = $scheduler.GetFolder($env:VAQ_PATH).GetTask($env:VAQ_TASK)
            Stop-ScheduledTask -TaskName $env:VAQ_TASK -TaskPath $env:VAQ_PATH
            if (-not $native.WaitForExit(12000)) { exit 16 }
            $clock = [Diagnostics.Stopwatch]::StartNew()
            while ([int]$registered.State -ne 3 -and $clock.ElapsedMilliseconds -lt 3000) {
                Start-Sleep -Milliseconds 100
            }
            if ([int]$registered.State -ne 3) { exit 16 }
            $state = 'Ready'
        } finally { $native.Dispose() }
    }
} elseif ($env:VAQ_MODE -eq 'stopped') {
    if ($state -ne 'Ready' -or (Get-Process -Id $pidExpected -ErrorAction SilentlyContinue)) { exit 16 }
} elseif ($env:VAQ_MODE -eq 'start') {
    if ($state -ne 'Ready' -or $watchers.Count) { exit 17 }
    Start-ScheduledTask -TaskName $env:VAQ_TASK -TaskPath $env:VAQ_PATH
} elseif ($env:VAQ_MODE -eq 'verify') {
    if ($state -eq 'Running' -and $watchers.Count -eq 1) {
        $watcher = $watchers[0]
        $schedule = Get-CimInstance Win32_Service -Filter "Name='Schedule'"
        if ($watcher.ParentProcessId -ne $schedule.ProcessId -or $watcher.SessionId -lt 1) { exit 14 }
        $born = $watcher.CreationDate.ToUniversalTime().ToString('o')
    } else {
        $watcher = $null
    }
} else { exit 18 }
[pscustomobject]@{state=$state; pid=$watcher.ProcessId; born=$born} | ConvertTo-Json -Compress
"""


class Rejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Rejected(code)


def load_archiver():
    path = Path(__file__).with_name("archive_vault_changes_v2.py")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == ARCHIVER_SHA, "ARCHIVER_PIN")
    module = types.ModuleType("vault_pause_archiver")
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def watch_plan(archiver, runtime):
    candidates = (
        (
            archiver.CODE.with_name("vault-changes-code-v11") / "watch-plan-v11.json",
            WATCH_PLAN_V11_SHA,
        ),
        (
            archiver.CODE.with_name("vault-changes-code-v10") / "watch-plan-v10.json",
            WATCH_PLAN_V10_SHA,
        ),
        (
            archiver.CODE.with_name("vault-changes-code-v9") / "watch-plan-v9.json",
            WATCH_PLAN_V9_SHA,
        ),
        (
            archiver.CODE.with_name("vault-changes-code-v8") / "watch-plan-v8.json",
            WATCH_PLAN_V8_SHA,
        ),
        (
            archiver.CODE.with_name("vault-changes-code-v7") / "watch-plan-v7.json",
            WATCH_PLAN_V7_SHA,
        ),
        (
            archiver.CODE.with_name("vault-changes-code-v6") / "watch-plan-v6.json",
            WATCH_PLAN_V6_SHA,
        ),
        (
            archiver.CODE.with_name("vault-changes-code-v5") / "watch-plan-v5.json",
            WATCH_PLAN_V5_SHA,
        ),
        (
            archiver.CODE.with_name("vault-changes-code-v4") / "watch-plan-v4.json",
            WATCH_PLAN_V4_SHA,
        ),
        (
            archiver.CODE.with_name("vault-changes-code-v3") / "watch-plan-v3.json",
            WATCH_PLAN_V3_SHA,
        ),
        (archiver.CODE / "watch-plan-v2.json", WATCH_PLAN_SHA),
    )
    for path, pin in candidates:
        if path.parent != archiver.CODE and not path.exists():
            continue
        raw, _info = archiver.file_bytes(runtime, path)
        require(archiver.sha(raw) == pin, "WATCH_PLAN_PIN")
        plan = runtime.state.decode(raw)
        executable = Path(plan["action_exe"])
        runtime.access.integrity(executable)
        require(
            archiver.sha(executable.read_bytes()) == plan["action_exe_sha256"],
            "WATCH_EXECUTABLE_PIN",
        )
        require(plan["sid"] == runtime.access.sid, "WATCH_SID")
        try:
            task_action(plan, "match")
        except Rejected as error:
            if str(error) == "TASK_CONTROL_11":
                continue
            raise
        return plan
    raise Rejected("WATCH_PLAN_ACTION")


def task_action(plan, mode, pid=0, born=""):
    env = {"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows"}
    env.update(
        {
            "VAQ_" + k: str(v)
            for k, v in {
                "TASK": plan["task_name"],
                "PATH": plan["task_path"],
                "EXE": plan["action_exe"],
                "ARGS": plan["action_arguments"],
                "WORKDIR": plan["workdir"],
                "SID": plan["sid"],
                "MODE": mode,
                "PID": pid,
                "BORN": born,
            }.items()
        }
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", TASK_CONTROL],
        env=env,
        capture_output=True,
        timeout=50 if mode == "stop" else 30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    require(result.returncode == 0 and not result.stderr, "TASK_CONTROL_" + str(result.returncode))
    return json.loads(result.stdout)


def acquire(stack, context, deadline):
    while True:
        try:
            stack.enter_context(context())
            return
        except FileExistsError:
            require(time.monotonic() < deadline, "WATCHER_LOCK_BUSY")
            time.sleep(0.1)


def pause(archiver, runtime, plan, pid):
    before = task_action(plan, "inspect", pid)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    with ExitStack() as stack:
        acquire(stack, runtime.queue.uploader_lock, deadline)
        acquire(stack, lambda: runtime.store.lock("archive-stop-guard"), deadline)
        try:
            stopped = task_action(plan, "stop", pid, before["born"])
        except (subprocess.TimeoutExpired, Rejected) as error:
            if isinstance(error, Rejected) and str(error) != "TASK_CONTROL_16":
                raise
            # La llamada pudo detener la tarea antes de vencer. Confirma sólo lectura.
            try:
                stopped = task_action(plan, "stopped", pid)
            except (subprocess.TimeoutExpired, Rejected) as check_error:
                raise Rejected("TASK_STOP_RESULT_UNVERIFIED") from check_error
        require(stopped["state"] == "Ready", "TASK_NOT_STOPPED")
    archiver.safe_stop_check(runtime)
    return {"paused": True, "pid": pid}


def verify_archive(archiver, runtime, operation, plan_pin):
    store = runtime.store
    prefix = archiver.ARCHIVE + "/" + operation
    raw = store.read(prefix + "/plan.json")
    require(archiver.sha(raw) == plan_pin, "PLAN_SHA")
    plan = runtime.state.decode(raw)
    archive_id = archiver.directory_identity(runtime, store.root / archiver.ARCHIVE)
    batch_id = archiver.directory_identity(runtime, store.root / prefix)
    require(
        plan["kind"] == archiver.KIND
        and plan["operation"] == operation
        and plan["config_sha256"] == archiver.CONFIG_SHA
        and plan["archive"] == archive_id
        and plan["batch"] == batch_id
        and 1 <= len(plan["entries"]) <= archiver.MAX_BATCH,
        "PLAN_IDENTITY",
    )
    active = {row["generation_id"]: row for row in runtime.queue.generations()}
    provider = runtime.backup_helpers()
    for entry in plan["entries"]:
        _source, _target, present, _done, done, _receipt = archiver.inspect_entry(
            runtime, provider, prefix, entry, active, plan_pin
        )
        require(not present and done, "ARCHIVE_NOT_COMPLETE")


def resume(archiver, runtime, plan, operation, plan_pin):
    archiver.safe_stop_check(runtime)
    verify_archive(archiver, runtime, operation, plan_pin)
    archiver.safe_stop_check(runtime)
    started = time.time_ns()
    try:
        task_action(plan, "start")
    except subprocess.TimeoutExpired:
        # La solicitud pudo llegar al Scheduler. Espera evidencia sin repetir Start.
        pass
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        capture = runtime.store.json("ledger.json").get("capture", {})
        if capture.get("state") == "observando" and capture.get("updated_ns", 0) >= started:
            try:
                current = task_action(plan, "verify")
            except subprocess.TimeoutExpired:
                break
            if current["state"] == "Running" and current["pid"]:
                return {"resumed": True, "pid": current["pid"], "capture_verified": True}
        time.sleep(1)
    raise Rejected("TASK_STARTED_CAPTURE_UNVERIFIED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    pause_parser = modes.add_parser("pause")
    pause_parser.add_argument("--pid", required=True, type=int)
    resume_parser = modes.add_parser("resume")
    resume_parser.add_argument("--operation", required=True)
    resume_parser.add_argument("--plan-sha256", required=True)
    args = parser.parse_args()
    try:
        archiver = load_archiver()
        runtime = archiver.bootstrap()
        plan = watch_plan(archiver, runtime)
        with archiver.archive_lock(runtime):
            if args.mode == "pause":
                require(args.pid > 0, "WATCHER_PID_REQUIRED")
                result = pause(archiver, runtime, plan, args.pid)
            else:
                require(
                    re.fullmatch(r"[a-f0-9]{32}", args.operation)
                    and re.fullmatch(r"[a-f0-9]{64}", args.plan_sha256),
                    "ARCHIVE_PLAN_PIN_REQUIRED",
                )
                result = resume(archiver, runtime, plan, args.operation, args.plan_sha256)
        print(json.dumps({"success": True, **result}, sort_keys=True))
        return 0
    except Exception as error:
        safe = str(error) if isinstance(error, Rejected) else type(error).__name__
        print(json.dumps({"success": False, "error": safe, "values_hidden": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
