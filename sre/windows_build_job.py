"""Proceso Win32 acotado por un Job propio; sin shell, SDK ni limpieza por PID.

API genérica: el llamador acredita antes las rutas, hashes y permisos. Sólo se
heredan tres handles de stdio mediante STARTUPINFOEX. La salida se sondea desde
el propietario único; no hay lectores bloqueados ni handles cerrados por hilos.
"""

import ctypes
import math
import os
import subprocess
import time
from ctypes import wintypes as w
from dataclasses import dataclass
from pathlib import Path

SIZE_T = ctypes.c_size_t
HANDLE = w.HANDLE
INVALID_HANDLE = ctypes.c_void_p(-1).value


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", w.DWORD),
        ("lpSecurityDescriptor", w.LPVOID),
        ("bInheritHandle", w.BOOL),
    ]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        ("dwX", w.DWORD),
        ("dwY", w.DWORD),
        ("dwXSize", w.DWORD),
        ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD),
        ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", ctypes.POINTER(w.BYTE)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", w.LPVOID)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", w.DWORD),
        ("dwThreadId", w.DWORD),
    ]


class BASIC_LIMITS(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC_LIMITS),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class NativeError(Exception):
    def __init__(self, operation, code):
        self.operation, self.code = operation, code
        super().__init__(f"Win32 {operation}: código {code}.")


@dataclass(frozen=True)
class Limits:
    processes: int = 8
    memory_bytes: int = 2 * 1024**3


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class Result:
    status: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    cleanup_complete: bool
    error_operation: str | None = None
    error_code: int | None = None


class Win32:
    def __init__(self):
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ValueError("El ejecutor requiere Windows amd64.")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([w.LPVOID, w.LPCWSTR], HANDLE),
            "SetInformationJobObject": ([HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
            "CreatePipe": (
                [
                    ctypes.POINTER(HANDLE),
                    ctypes.POINTER(HANDLE),
                    ctypes.POINTER(SECURITY_ATTRIBUTES),
                    w.DWORD,
                ],
                w.BOOL,
            ),
            "SetHandleInformation": ([HANDLE, w.DWORD, w.DWORD], w.BOOL),
            "CreateFileW": (
                [
                    w.LPCWSTR,
                    w.DWORD,
                    w.DWORD,
                    ctypes.POINTER(SECURITY_ATTRIBUTES),
                    w.DWORD,
                    w.DWORD,
                    HANDLE,
                ],
                HANDLE,
            ),
            "InitializeProcThreadAttributeList": (
                [w.LPVOID, w.DWORD, w.DWORD, ctypes.POINTER(SIZE_T)],
                w.BOOL,
            ),
            "UpdateProcThreadAttribute": (
                [w.LPVOID, w.DWORD, SIZE_T, w.LPVOID, SIZE_T, w.LPVOID, w.LPVOID],
                w.BOOL,
            ),
            "DeleteProcThreadAttributeList": ([w.LPVOID], None),
            "CreateProcessW": (
                [
                    w.LPCWSTR,
                    w.LPWSTR,
                    w.LPVOID,
                    w.LPVOID,
                    w.BOOL,
                    w.DWORD,
                    w.LPVOID,
                    w.LPCWSTR,
                    ctypes.POINTER(STARTUPINFO),
                    ctypes.POINTER(PROCESS_INFORMATION),
                ],
                w.BOOL,
            ),
            "AssignProcessToJobObject": ([HANDLE, HANDLE], w.BOOL),
            "ResumeThread": ([HANDLE], w.DWORD),
            "PeekNamedPipe": (
                [HANDLE, w.LPVOID, w.DWORD, w.LPVOID, ctypes.POINTER(w.DWORD), w.LPVOID],
                w.BOOL,
            ),
            "ReadFile": ([HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID], w.BOOL),
            "WaitForSingleObject": ([HANDLE, w.DWORD], w.DWORD),
            "GetExitCodeProcess": ([HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            "TerminateProcess": ([HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([HANDLE], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def require(value, operation):
        if not value:
            raise NativeError(operation, ctypes.get_last_error())
        return value

    def close(self, handle):
        self.require(self.kernel.CloseHandle(handle), "CloseHandle")

    def create_job(self, limits):
        job = self.require(self.kernel.CreateJobObjectW(None, None), "CreateJobObjectW")
        try:
            information = EXTENDED_LIMITS()
            information.BasicLimitInformation.LimitFlags = 0x2000 | 0x200 | 0x8
            information.BasicLimitInformation.ActiveProcessLimit = limits.processes
            information.JobMemoryLimit = limits.memory_bytes
            self.require(
                self.kernel.SetInformationJobObject(
                    job, 9, ctypes.byref(information), ctypes.sizeof(information)
                ),
                "SetInformationJobObject",
            )
            return job
        except BaseException:
            self.close(job)
            raise

    def pipe(self):
        read, write = HANDLE(), HANDLE()
        security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        self.require(
            self.kernel.CreatePipe(
                ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0
            ),
            "CreatePipe",
        )
        try:
            self.require(self.kernel.SetHandleInformation(read, 1, 0), "SetHandleInformation")
        except BaseException:
            self.close(read.value)
            self.close(write.value)
            raise
        return read.value, write.value

    def nul(self):
        security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        handle = self.kernel.CreateFileW(
            "NUL", 0x80000000, 3, ctypes.byref(security), 3, 0x80, None
        )
        if handle in (None, INVALID_HANDLE):
            raise NativeError("CreateFileW", ctypes.get_last_error())
        return handle

    def create(self, executable, arguments, cwd, env, stdin, stdout, stderr):
        size = SIZE_T()
        self.kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if ctypes.get_last_error() != 122 or not 0 < size.value <= 1024**2:
            raise NativeError("InitializeProcThreadAttributeList:size", ctypes.get_last_error())
        attributes = ctypes.create_string_buffer(size.value)
        self.require(
            self.kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)),
            "InitializeProcThreadAttributeList",
        )
        try:
            inherited = (HANDLE * 3)(stdin, stdout, stderr)
            self.require(
                self.kernel.UpdateProcThreadAttribute(
                    attributes, 0, 0x20002, inherited, ctypes.sizeof(inherited), None, None
                ),
                "UpdateProcThreadAttribute",
            )
            startup = STARTUPINFOEX()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100
            (
                startup.StartupInfo.hStdInput,
                startup.StartupInfo.hStdOutput,
                startup.StartupInfo.hStdError,
            ) = stdin, stdout, stderr
            startup.lpAttributeList = ctypes.cast(attributes, w.LPVOID)
            command = ctypes.create_unicode_buffer(
                subprocess.list2cmdline([str(executable), *arguments])
            )
            environment = ctypes.create_unicode_buffer(
                "\0".join(f"{key}={env[key]}" for key in sorted(env, key=str.casefold)) + "\0\0"
            )
            process = PROCESS_INFORMATION()
            flags = 0x4 | 0x400 | 0x80000 | 0x8000000
            self.require(
                self.kernel.CreateProcessW(
                    str(executable),
                    command,
                    None,
                    None,
                    True,
                    flags,
                    ctypes.cast(environment, w.LPVOID),
                    str(cwd),
                    ctypes.cast(ctypes.byref(startup), ctypes.POINTER(STARTUPINFO)),
                    ctypes.byref(process),
                ),
                "CreateProcessW",
            )
            return process.hProcess, process.hThread
        finally:
            self.kernel.DeleteProcThreadAttributeList(attributes)

    def assign(self, job, process):
        self.require(self.kernel.AssignProcessToJobObject(job, process), "AssignProcessToJobObject")

    def resume(self, thread):
        previous = self.kernel.ResumeThread(thread)
        if previous == 0xFFFFFFFF:
            raise NativeError("ResumeThread", ctypes.get_last_error())
        if previous != 1:
            raise NativeError("ResumeThread:suspend-count", previous)

    def signaled(self, process):
        result = self.kernel.WaitForSingleObject(process, 0)
        if result not in (0, 258):
            raise NativeError("WaitForSingleObject", ctypes.get_last_error())
        return result == 0

    def exit_code(self, process):
        result = w.DWORD()
        self.require(
            self.kernel.GetExitCodeProcess(process, ctypes.byref(result)), "GetExitCodeProcess"
        )
        return result.value

    def terminate(self, process):
        self.require(self.kernel.TerminateProcess(process, 1), "TerminateProcess")

    def read_available(self, handle, maximum):
        available = w.DWORD()
        if not self.kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            code = ctypes.get_last_error()
            if code in (109, 232):
                return b"", True
            raise NativeError("PeekNamedPipe", code)
        if not available.value:
            return b"", False
        size = min(available.value, maximum, 65536)
        buffer, received = ctypes.create_string_buffer(size), w.DWORD()
        if not self.kernel.ReadFile(handle, buffer, size, ctypes.byref(received), None):
            code = ctypes.get_last_error()
            if code in (109, 232):
                return b"", True
            raise NativeError("ReadFile", code)
        return buffer.raw[: received.value], received.value == 0


def run_job(
    executable,
    arguments,
    *,
    cwd,
    env,
    timeout,
    limits=DEFAULT_LIMITS,
    output_limit=65536,
    _api=None,
):
    """Ejecuta un árbol propio acotado y devuelve datos; nunca imprime argumentos.

    El Job se cierra también al salir el proceso principal: sus descendientes no
    pueden retener stdio indefinidamente. Si Assign falla, se termina únicamente
    el proceso aún suspendido mediante el HANDLE que devolvió CreateProcessW.
    """
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0.1 <= timeout <= 1800
        or type(output_limit) is not int
        or not 1024 <= output_limit <= 1024**2
        or not isinstance(limits, Limits)
        or type(limits.processes) is not int
        or not 1 <= limits.processes <= 8
        or type(limits.memory_bytes) is not int
        or not 64 * 1024**2 <= limits.memory_bytes <= 4 * 1024**3
    ):
        raise ValueError("Límites del Job fuera del contrato.")
    executable, cwd = Path(executable), Path(cwd)
    if (
        not executable.is_absolute()
        or executable.suffix.lower() != ".exe"
        or not executable.is_file()
        or not cwd.is_absolute()
        or not cwd.is_dir()
    ):
        raise ValueError("El llamador debe acreditar ejecutable y directorio absolutos.")
    if not isinstance(arguments, list) or any(
        not isinstance(value, str) or "\0" in value for value in arguments
    ):
        raise ValueError("Argumentos no válidos.")
    if (
        not isinstance(env, dict)
        or len(env) > 128
        or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
            for key, value in env.items()
        )
        or len({key.casefold() for key in env}) != len(env)
    ):
        raise ValueError("El entorno explícito no es válido.")
    if len(subprocess.list2cmdline([str(executable), *arguments])) >= 32767:
        raise ValueError("La línea de comando excede el límite Windows.")
    started = time.monotonic()
    deadline = started + timeout
    run_until = deadline - min(0.1, timeout / 10)
    api = _api or Win32()
    handles, buffers, eof = {}, {"out": bytearray(), "err": bytearray()}, set()
    assigned, cleanup_complete, status, exit_code, error = False, True, "launch_error", None, None

    def close(name):
        nonlocal cleanup_complete
        handle = handles.pop(name, None)
        if handle is not None:
            try:
                api.close(handle)
            except NativeError:
                cleanup_complete = False

    def drain(until):
        exceeded = False
        for name in ("out", "err"):
            if time.monotonic() >= until:
                break
            if name in eof:
                continue
            capacity = output_limit - sum(len(value) for value in buffers.values())
            # Sigue consumiendo en bloques acotados aunque la salida ya haya
            # excedido el máximo; lo sobrante se descarta sin crecer en memoria.
            data, ended = api.read_available(handles[name], 65536)
            buffers[name].extend(data[:capacity])
            exceeded |= len(data) > capacity
            if ended:
                eof.add(name)
        return exceeded

    try:
        handles["job"] = api.create_job(limits)
        handles["out"], handles["out_write"] = api.pipe()
        handles["err"], handles["err_write"] = api.pipe()
        handles["stdin"] = api.nul()
        handles["process"], handles["thread"] = api.create(
            executable,
            arguments,
            cwd,
            env,
            handles["stdin"],
            handles["out_write"],
            handles["err_write"],
        )
        for name in ("out_write", "err_write", "stdin"):
            close(name)
        api.assign(handles["job"], handles["process"])
        assigned = True
        if time.monotonic() >= run_until:
            status = "timeout"
        else:
            api.resume(handles["thread"])
            close("thread")
            while True:
                if time.monotonic() >= run_until:
                    status = "timeout"
                    break
                exceeded = drain(run_until)
                if time.monotonic() >= run_until:
                    status = "timeout"
                    break
                if exceeded:
                    status = "output_limit"
                    break
                if api.signaled(handles["process"]):
                    if time.monotonic() >= run_until:
                        status = "timeout"
                        break
                    exit_code = api.exit_code(handles["process"])
                    status = "timeout" if time.monotonic() >= run_until else "completed"
                    break
                if time.monotonic() >= run_until:
                    status = "timeout"
                    break
                time.sleep(min(0.005, max(0, run_until - time.monotonic())))
    except NativeError as failure:
        error = failure
    finally:
        if "process" in handles and not assigned:
            try:
                api.terminate(handles["process"])
            except NativeError:
                cleanup_complete = False
        close("job")
        try:
            while "process" in handles and time.monotonic() < deadline:
                if (
                    "out" in handles
                    and "err" in handles
                    and drain(deadline)
                    and status == "completed"
                ):
                    status = "output_limit"
                if time.monotonic() >= deadline:
                    break
                if api.signaled(handles["process"]) and len(eof) == 2:
                    break
                time.sleep(min(0.005, max(0, deadline - time.monotonic())))
            if "process" in handles:
                if len(eof) != 2 or not api.signaled(handles["process"]):
                    cleanup_complete = False
        except NativeError as failure:
            cleanup_complete, error = False, error or failure
        for name in list(handles):
            close(name)
    finished = time.monotonic()
    if finished >= deadline and status == "completed":
        status = "timeout"
    if not cleanup_complete:
        status = "cleanup_error"
    return Result(
        status,
        exit_code,
        bytes(buffers["out"]),
        bytes(buffers["err"]),
        round(finished - started, 6),
        cleanup_complete,
        error.operation if error else None,
        error.code if error else None,
    )
