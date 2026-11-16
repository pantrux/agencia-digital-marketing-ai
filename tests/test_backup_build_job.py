"""Job Windows y perfil puro; los procesos de prueba nunca son el SDK Go."""

import copy
import ctypes
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sre"))
JOB = importlib.import_module("windows_build_job")
PROFILE = importlib.import_module("backup_build_profile")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "sre/backup-go-build-profile.json").read_text())

    def test_perfil_plan_no_crea_rutas_ni_lanza_sdk(self):
        with (
            patch("subprocess.Popen", side_effect=AssertionError),
            patch.object(JOB, "run_job", side_effect=AssertionError),
            patch.object(JOB.Win32, "__init__", side_effect=AssertionError),
            patch.object(Path, "mkdir", side_effect=AssertionError),
        ):
            result = PROFILE.make_plan(self.profile)
        self.assertFalse(result["sdk_executed"])
        self.assertEqual(result["allowed_operations"], ["env"])

    def test_perfil_no_autoriza_raiz_de_otro_proyecto_aun_con_rutas_coherentes(self):
        value = copy.deepcopy(self.profile)
        original = value["build_root"]
        replacement = r"D:\otro-proyecto\.tools\backup-source-build"
        for key in ("build_root", "run_root", "sdk_root", "sdk_executable"):
            value[key] = value[key].replace(original, replacement)
        value["sdk_tools_map_path"] = (
            r"D:\otro-proyecto\.tools\evidence\backup-go-sdk-executable-map.json"
        )
        with self.assertRaises(ValueError):
            PROFILE.validate_profile(value)

    def test_hash_externo_y_json_ambiguo_rechazados_sin_planificar(self):
        with tempfile.TemporaryDirectory(prefix="owned-build-profile-") as folder:
            file = Path(folder) / "profile.json"
            file.write_text(json.dumps(self.profile))
            with (
                redirect_stdout(io.StringIO()),
                patch.object(PROFILE, "make_plan", side_effect=AssertionError),
            ):
                self.assertEqual(
                    PROFILE.main(["--profile", str(file), "--profile-sha256", "0" * 64]), 2
                )
            file.write_text('{"schema_version":1,"schema_version":1}')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(PROFILE.main(["--profile", str(file)]), 2)

    def test_entorno_no_hereda_secretos_flags_o_configuracion_del_usuario(self):
        with patch.dict(
            os.environ,
            {
                "CF_PLATFORM_API_TOKEN": "sentinela",
                "GOFLAGS": "-toolexec=otro",
                "GOPROXY": "direct",
                "GOTOOLCHAIN": "auto",
            },
        ):
            env = PROFILE.environment(self.profile)
        self.assertNotIn("CF_PLATFORM_API_TOKEN", env)
        self.assertEqual(env["GOFLAGS"], "")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOVCS"], "*:off")
        self.assertEqual(env["GOPROXY"], "https://proxy.golang.org")
        self.assertEqual(env["GOSUMDB"], "sum.golang.org")
        self.assertEqual(env["CGO_ENABLED"], "0")
        self.assertEqual(env["GOTELEMETRY"], "off")

    def test_no_admite_operaciones_o_rutas_o_limites_ampliados(self):
        for mutate in (
            lambda p: p["allowed_operations"].append("build"),
            lambda p: p.update(sdk_executable=r"C:\Windows\System32\cmd.exe"),
            lambda p: p.update(run_root=p["sdk_root"]),
            lambda p: p["limits"].update(processes=9),
            lambda p: p["limits"].update(memory_bytes=4 * 1024**3),
            lambda p: p.update(sdk_tools_map_sha256="0" * 64),
        ):
            value = copy.deepcopy(self.profile)
            mutate(value)
            with self.assertRaises(ValueError):
                PROFILE.validate_profile(value)

    def test_comando_env_es_cerrado_y_no_acepta_argumentos_arbitrarios(self):
        exe, args = PROFILE.command(self.profile, "env")
        self.assertEqual(exe, self.profile["sdk_executable"])
        self.assertEqual(args[:2], ["env", "-json"])
        self.assertNotIn("-w", args)
        for operation in ("build", "run", "generate", "env -w"):
            with self.assertRaises(ValueError):
                PROFILE.command(self.profile, operation)


class DeadlineTests(unittest.TestCase):
    """Reloj y API controlados: jamás se crea un proceso ni se ejecuta el .exe."""

    def check_scenario(self, mode):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        clock = Clock()

        class Api:
            pipes = 0
            exit_queries = 0

            def __init__(self):
                self.reads = {}
                self.closed = []

            def create_job(self, limits):
                return 1

            def pipe(self):
                self.pipes += 1
                return (2, 3) if self.pipes == 1 else (4, 5)

            def nul(self):
                return 6

            def create(self, *args):
                return 7, 8

            def assign(self, *args):
                pass

            def resume(self, *args):
                pass

            def terminate(self, *args):
                pass

            def signaled(self, *args):
                return True

            def exit_code(self, *args):
                self.exit_queries += 1
                return 0

            def close(self, handle):
                self.closed.append(handle)
                if handle == 1 and mode == "late_close":
                    clock.now = 1.1

            def read_available(self, handle, maximum):
                count = self.reads.get(handle, 0) + 1
                self.reads[handle] = count
                if mode == "late_main" and handle == 2:
                    clock.now = 0.95 if count == 1 else 0.96
                return (b"head" if handle == 2 else b"", False) if count == 1 else (b"", True)

        api = Api()
        with tempfile.TemporaryDirectory(prefix="owned-fake-job-") as folder:
            cwd = Path(folder)
            exe = cwd / "placeholder.exe"
            exe.write_bytes(b"not-executed")
            with (
                patch.object(JOB.time, "monotonic", clock.monotonic),
                patch.object(JOB.time, "sleep", clock.sleep),
            ):
                result = JOB.run_job(exe, [], cwd=cwd, env={}, timeout=1, _api=api)
        self.assertEqual(len(api.closed), len(set(api.closed)))
        return result, api

    def test_presupuesto_vencido_antes_de_aceptar_lider_senalado_no_es_completed(self):
        result, api = self.check_scenario("late_main")
        self.assertEqual(result.status, "timeout")
        self.assertEqual(api.exit_queries, 0)

    def test_deadline_antes_del_drenado_final_no_acredita_eof_ni_completed(self):
        result, _api = self.check_scenario("late_close")
        self.assertNotEqual(result.status, "completed")
        self.assertFalse(result.cleanup_complete)


@unittest.skipUnless(os.name == "nt", "Sólo Win32; CI conserva pruebas puras del perfil.")
class WindowsJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="owned-build-job-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.env = {
            "SystemRoot": os.environ["SystemRoot"],
            "TEMP": str(self.folder),
            "TMP": str(self.folder),
        }

    def run_python(self, code, **kwargs):
        return JOB.run_job(
            sys.executable, ["-I", "-c", code], cwd=self.folder, env=self.env, **kwargs
        )

    def test_abi_amd64_de_structs_y_handles(self):
        self.assertEqual(ctypes.sizeof(ctypes.c_void_p), 8)
        self.assertEqual(ctypes.sizeof(JOB.STARTUPINFO), 104)
        self.assertEqual(ctypes.sizeof(JOB.STARTUPINFOEX), 112)
        self.assertEqual(ctypes.sizeof(JOB.PROCESS_INFORMATION), 24)
        self.assertEqual(ctypes.sizeof(JOB.EXTENDED_LIMITS), 144)
        self.assertEqual(JOB.EXTENDED_LIMITS.JobMemoryLimit.offset, 120)

    def test_proceso_real_captura_ambos_streams_y_codigo_sin_sdk(self):
        result = self.run_python(
            "import sys;print('salida');print('error',file=sys.stderr);sys.exit(7)", timeout=4
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_code, 7)
        self.assertIn(b"salida", result.stdout)
        self.assertIn(b"error", result.stderr)
        self.assertTrue(result.cleanup_complete)

    def test_error_assign_no_reanuda_y_recoge_proceso_suspendido(self):
        api = JOB.Win32()
        marker = self.folder / "no-debe-existir"
        with (
            patch.object(api, "assign", side_effect=JOB.NativeError("assign", 5)),
            patch.object(api, "resume", wraps=api.resume) as resume,
        ):
            result = self.run_python(
                f"from pathlib import Path;Path({str(marker)!r}).write_text('ejecutado')",
                timeout=3,
                _api=api,
            )
        self.assertEqual(result.status, "launch_error")
        resume.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertTrue(result.cleanup_complete)

    def test_error_resume_cierra_job_ya_asignado_sin_ejecutar_codigo(self):
        api = JOB.Win32()
        marker = self.folder / "no-resume"
        with patch.object(api, "resume", side_effect=JOB.NativeError("resume", 5)):
            result = self.run_python(
                f"from pathlib import Path;Path({str(marker)!r}).write_text('no')",
                timeout=3,
                _api=api,
            )
        self.assertEqual(result.status, "launch_error")
        self.assertFalse(marker.exists())
        self.assertTrue(result.cleanup_complete)

    def test_handle_heredable_ajeno_no_llega_al_hijo(self):
        api = JOB.Win32()
        read, write = api.pipe()
        try:
            code = (
                "import ctypes;from ctypes import wintypes as w;k=ctypes.WinDLL('kernel32',use_last_error=True);k.WriteFile.argtypes=[w.HANDLE,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID];k.WriteFile.restype=w.BOOL;n=w.DWORD();print(int(k.WriteFile("
                + str(write)
                + ",ctypes.create_string_buffer(b'foreign'),7,ctypes.byref(n),None)))"
            )
            result = self.run_python(code, timeout=3)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.stdout.strip(), b"0")
            self.assertEqual(api.read_available(read, 128)[0], b"")
        finally:
            api.close(read)
            api.close(write)

    def test_kernel_confirma_limites_de_job_memoria_y_procesos(self):
        api = JOB.Win32()
        limits = JOB.Limits()
        handle = api.create_job(limits)
        try:
            information = JOB.EXTENDED_LIMITS()
            api.kernel.QueryInformationJobObject.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ]
            api.kernel.QueryInformationJobObject.restype = ctypes.c_int
            self.assertTrue(
                api.kernel.QueryInformationJobObject(
                    handle, 9, ctypes.byref(information), ctypes.sizeof(information), None
                )
            )
            self.assertEqual(information.BasicLimitInformation.ActiveProcessLimit, 8)
            self.assertEqual(information.JobMemoryLimit, 2 * 1024**3)
            self.assertEqual(information.BasicLimitInformation.LimitFlags & 0x2208, 0x2208)
        finally:
            api.close(handle)

    def test_salida_excesiva_se_corta_y_no_devuelve_exito(self):
        result = self.run_python(
            "import sys,time;sys.stdout.write('x'*200000);sys.stdout.flush();time.sleep(30)",
            timeout=3,
            output_limit=1024,
        )
        self.assertEqual(result.status, "output_limit")
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 1024)
        self.assertTrue(result.cleanup_complete)

    def test_timeout_mata_descendiente_que_retiene_stdout_sin_tocar_ajeno(self):
        ready = self.folder / "ready.json"
        child = "import time; print('salida del descendiente', flush=True); time.sleep(60)"
        code = (
            "import subprocess,sys,time,json;from pathlib import Path;p=subprocess.Popen([sys.executable,'-I','-c',"
            + repr(child)
            + "],stdout=sys.stdout,stderr=sys.stderr);ready=Path("
            + repr(str(ready))
            + ");tmp=ready.with_suffix('.tmp');tmp.write_text(json.dumps({'pid':p.pid}));"
            "tmp.replace(ready);time.sleep(60)"
        )
        sentinel = subprocess.Popen(
            [sys.executable, "-I", "-c", "import time;time.sleep(60)"],
            env=self.env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.addCleanup(sentinel.wait)
        self.addCleanup(sentinel.terminate)
        results = []
        worker = threading.Thread(target=lambda: results.append(self.run_python(code, timeout=2)))
        worker.start()
        deadline = time.monotonic() + 1.5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        pid = json.loads(ready.read_text())["pid"]
        api = JOB.Win32()
        api.kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        api.kernel.OpenProcess.restype = ctypes.c_void_p
        handle = api.kernel.OpenProcess(0x100000, False, pid)
        self.assertTrue(handle)
        try:
            self.assertFalse(api.signaled(handle))
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0].status, "timeout")
            self.assertTrue(results[0].cleanup_complete)
            self.assertEqual(results[0].stdout.strip(), b"salida del descendiente")
            # Espera la señal del proceso que Windows emite al completar su cierre.
            self.assertEqual(api.kernel.WaitForSingleObject(handle, 1000), 0)
            self.assertIsNone(sentinel.poll())
        finally:
            api.close(handle)


if __name__ == "__main__":
    unittest.main()
