"""Decisiones de recuperación acotada, sin detener procesos de la estación."""

import asyncio
import ctypes
import gc
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"))
import bridge_supervisor as supervisor  # noqa: E402
from bridge_supervisor import RestartPolicy, child_environment  # noqa: E402


class SupervisorTests(unittest.TestCase):
    def test_el_supervisor_escribe_logs_sin_salidas_heredadas_de_windows(self):
        import launch_service

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            mapping = {"GPU_INSTALL_ROOT": str(root), "GPU_LOG_ROOT": str(logs)}
            with patch.object(launch_service.sys, "stdout", None):
                with patch.object(launch_service.sys, "stderr", None):
                    launch_service.redirect_supervisor_logs(mapping)
                    with launch_service.sys.stdout as output:
                        self.assertIs(launch_service.sys.stderr, output)
                        print("arranque oculto")
                        print("diagnóstico", file=launch_service.sys.stderr)
            self.assertEqual(
                (logs / "supervisor.log").read_text(encoding="utf-8"),
                "arranque oculto\ndiagnóstico\n",
            )

    def test_lanzador_usa_bind_explicito_sin_proxy_ni_multiples_workers(self):
        from types import SimpleNamespace

        import launch_service

        calls = []
        mapping = {"GPU_BRIDGE_BIND_ADDRESS": "0.0.0.0"}
        fake = object()
        with patch.dict(
            sys.modules,
            {
                "immich_service": SimpleNamespace(create_app=lambda config: fake),
                "uvicorn": SimpleNamespace(run=lambda app, **kwargs: calls.append((app, kwargs))),
            },
        ):
            launch_service.serve("immich", mapping)
        self.assertIs(calls[0][0], fake)
        self.assertEqual(calls[0][1]["host"], "0.0.0.0")
        self.assertEqual(calls[0][1]["port"], 3003)
        self.assertEqual(calls[0][1]["workers"], 1)
        self.assertFalse(calls[0][1]["proxy_headers"])
        self.assertEqual(calls[0][1]["lifespan"], "on")

    def test_json_plano_rechaza_claves_duplicadas_y_secretos_ajenos(self):
        self.assertEqual(
            supervisor.parse_runtime_config('\ufeff{"GPU_BRIDGE_HOST":"192.168.0.6"}'),
            {"GPU_BRIDGE_HOST": "192.168.0.6"},
        )
        for body in (
            '{"GPU_A":"1","GPU_A":"2"}',
            '{"SYNOLOGY_PASSWORD":"ajeno"}',
            '{"GPU_A":["1"]}',
            '{"GPU_A":"a\\nb"}',
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                supervisor.parse_runtime_config(body)

    def test_specs_solo_comandos_fijos_y_node_con_huella(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            services = root / "services"
            services.mkdir()
            (services / "launch_service.py").write_text("# fixture")
            (root / "logs").mkdir()
            renderer = root / "render-worker"
            (renderer / "src").mkdir(parents=True)
            (renderer / "src/start.ts").write_text("// fixture")
            node = root / "node.exe"
            node.write_bytes(b"node fixture")
            import hashlib

            mapping = {
                "GPU_BRIDGE_HOST": "192.168.0.6",
                "GPU_BRIDGE_BEARER_TOKEN": "t" * 32,
                "GPU_INSTALL_ROOT": str(root),
                "GPU_MODEL_CACHE_ROOT": str(root / "models"),
                "GPU_SERVICES_ROOT": str(services),
                "GPU_LOG_ROOT": str(root / "logs"),
                "GPU_RENDER_WORKER_ROOT": str(renderer),
                "GPU_NODE_PATH": str(node),
                "GPU_NODE_SHA256": hashlib.sha256(node.read_bytes()).hexdigest(),
                "GPU_RUNTIME_CONFIG_PATH": str(root / "runtime.json"),
                "GPU_BRIDGE_BIND_ADDRESS": "0.0.0.0",
            }
            for name in ("METRICS", "WHISPER", "IMMICH"):
                executable = root / name.lower() / "venv/Scripts/python.exe"
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"python fixture")
                mapping[f"GPU_{name}_PYTHON_PATH"] = str(executable)
            specs = supervisor.service_specs(mapping)
            self.assertEqual([spec.name for spec in specs], ["immich", "whisper", "render"])
            self.assertEqual(specs[0].command[1:3], ("-I", str(services / "launch_service.py")))
            self.assertEqual(
                specs[2].command,
                (str(node), "--experimental-strip-types", str(renderer / "src/start.ts")),
            )
            with self.assertRaises(ValueError):
                supervisor.service_specs(dict(mapping, GPU_NODE_SHA256="0" * 64))
            with self.assertRaises(ValueError):
                supervisor.service_specs(dict(mapping, GPU_WHISPER_PYTHON_PATH=str(node)))

    def test_startup_grace_and_three_failures_before_restart(self):
        policy = RestartPolicy(started_at=0)
        self.assertFalse(policy.observe(False, alive=True, now=100))
        self.assertFalse(policy.observe(False, alive=True, now=181))
        self.assertFalse(policy.observe(False, alive=True, now=191))
        self.assertTrue(policy.observe(False, alive=True, now=201))

    def test_dead_process_restarts_but_backoff_prevents_loop(self):
        policy = RestartPolicy(started_at=0)
        self.assertTrue(policy.observe(False, alive=False, now=1))
        policy.restarted(now=1)
        self.assertFalse(policy.observe(False, alive=False, now=2))
        self.assertTrue(policy.observe(False, alive=False, now=12))

    def test_success_resets_failed_observations(self):
        policy = RestartPolicy(started_at=0)
        self.assertFalse(policy.observe(False, alive=True, now=200))
        self.assertFalse(policy.observe(True, alive=True, now=210))
        self.assertFalse(policy.observe(False, alive=True, now=220))
        self.assertFalse(policy.observe(False, alive=True, now=230))

    def test_children_never_inherit_other_credentials_or_python_path(self):
        env = child_environment(
            {
                "PATH": "Windows",
                "SYSTEMROOT": r"C:\Windows",
                "GITHUB_TOKEN": "ajeno",
                "SYNOLOGY_PASSWORD": "ajeno",
                "PYTHONPATH": "ajeno",
            },
            {"GPU_BRIDGE_BEARER_TOKEN": "propio"},
        )
        self.assertEqual(env["PATH"], "Windows")
        self.assertEqual(env["GPU_BRIDGE_BEARER_TOKEN"], "propio")
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SYNOLOGY_PASSWORD", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_hilo_http_no_acepta_login_html_ni_gpu_sin_verificar(self):
        class Response:
            status = 200
            body = b"<html>login</html>"

            def getheader(self, name, default=None):
                return "application/json"

            def read(self, size):
                return self.body[:size]

            def close(self):
                return None

        class Connection:
            sock = type("Socket", (), {"shutdown": lambda *args: None})()

            def connect(self):
                return None

            def request(self, *args, **kwargs):
                self.requested = (args, kwargs)

            def getresponse(self):
                return Response()

            def close(self):
                return None

        spec = supervisor.ServiceSpec("whisper", (), Path("."), 8000, "whisper", "cuda_available")
        connection = Connection()
        with patch("http.client.HTTPConnection", return_value=connection):
            self.assertFalse(supervisor.health_probe(spec, "propio", timeout=0.1))
            Response.body = json.dumps(
                {
                    "service": "whisper",
                    "status": "ok",
                    "ready": True,
                    "version": "1",
                    "cuda_available": False,
                }
            ).encode()
            self.assertFalse(supervisor.health_probe(spec, "propio", timeout=0.1))
            Response.body = json.dumps(
                {
                    "service": "whisper",
                    "status": "ok",
                    "ready": True,
                    "version": "1",
                    "cuda_available": True,
                }
            ).encode()
            self.assertTrue(supervisor.health_probe(spec, "propio", timeout=0.1))
            self.assertEqual(connection.requested[1]["headers"]["Authorization"], "Bearer propio")
            self.assertEqual(connection.requested[0], ("GET", "/health"))


@dataclass
class FakeChild:
    alive: bool = True
    stopped: bool = False

    def is_alive(self):
        return self.alive

    def stop(self):
        self.alive = False
        self.stopped = True


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_acepta_una_respuesta_cuda_valida_que_tarda_mas_de_dos_segundos(self):
        child = FakeChild()
        observed = []
        spec = supervisor.ServiceSpec("whisper", (), Path("."), 8000, "whisper", "cuda_available")

        def probe(target, token, *, timeout):
            observed.append(timeout)
            return timeout >= 2.5

        manager = supervisor.BridgeSupervisor(
            [spec],
            {"GPU_BRIDGE_BEARER_TOKEN": "propio"},
            Path("."),
            process_factory=lambda *args: child,
            probe=probe,
        )
        await manager.start()
        try:
            await manager.check_once()
            self.assertEqual(len(observed), 1)
            self.assertTrue(manager.snapshot()["children"]["whisper"]["healthy"])
            self.assertEqual(manager.snapshot()["children"]["whisper"]["restarts"], 0)
        finally:
            await manager.stop()

    async def test_tres_fallos_reinician_solo_el_hijo_propio_y_stop_cierra_todos(self):
        now = [0]
        created = []
        healthy = {"immich": True, "whisper": False, "render": True}
        specs = [
            supervisor.ServiceSpec(name, (), Path("."), port, name, "cuda_available")
            for name, port in (("immich", 3003), ("whisper", 8000), ("render", 8090))
        ]

        def factory(spec, env, logs):
            child = FakeChild()
            created.append((spec.name, child, env))
            return child

        manager = supervisor.BridgeSupervisor(
            specs,
            {"GPU_BRIDGE_BEARER_TOKEN": "propio"},
            Path("."),
            process_factory=factory,
            probe=lambda spec, *args, **kwargs: healthy[spec.name],
            clock=lambda: now[0],
        )
        await manager.start()
        for stamp in (181, 191, 201):
            now[0] = stamp
            await manager.check_once()
        self.assertEqual(
            [name for name, child, env in created], ["immich", "whisper", "render", "whisper"]
        )
        self.assertTrue(created[1][1].stopped)
        self.assertFalse(created[0][1].stopped)
        self.assertFalse(manager.snapshot()["healthy"])
        healthy["whisper"] = True
        now[0] = 220
        await manager.check_once()
        self.assertTrue(manager.snapshot()["healthy"])
        self.assertEqual(manager.snapshot()["children"]["whisper"]["restarts"], 1)
        await manager.stop()
        self.assertTrue(all(child.stopped for _, child, _ in created))
        self.assertFalse(manager.snapshot()["healthy"])

    async def test_cancelar_creacion_espera_y_cierra_el_handle_recien_creado(self):
        entered, release = threading.Event(), threading.Event()
        child = FakeChild()

        def factory(*args):
            entered.set()
            release.wait(5)
            return child

        spec = supervisor.ServiceSpec("whisper", (), Path("."), 8000, "whisper", "cuda_available")
        manager = supervisor.BridgeSupervisor(
            [spec], {"GPU_BRIDGE_BEARER_TOKEN": "propio"}, Path("."), process_factory=factory
        )
        task = asyncio.create_task(manager.start())
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(child.stopped)
        self.assertFalse(manager.snapshot()["healthy"])


class ProcessContainmentTests(unittest.TestCase):
    def test_fallo_antes_de_resume_no_ejecuta_codigo_y_cierra_proceso_suspendido(self):
        if os.name != "nt":
            with self.assertRaisesRegex(OSError, "Windows"):
                supervisor.OwnedProcess.start(None, {}, Path("."))
            return

        class RejectedJob:
            def assign_and_resume(self, process):
                self.process = process
                raise OSError("rechazo controlado")

            def close(self):
                self.closed = True

        job = RejectedJob()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "never.txt"
            spec = supervisor.ServiceSpec(
                "fixture",
                (
                    sys.executable,
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ejecutado')",
                    str(marker),
                ),
                root,
                0,
                "fixture",
                "cuda_available",
            )
            with self.assertRaisesRegex(OSError, "controlado"):
                supervisor.OwnedProcess.start(
                    spec, child_environment(dict(os.environ), {}), root, job_factory=lambda: job
                )
            self.assertTrue(job.closed)
            self.assertIsNotNone(job.process.poll())
            self.assertFalse(marker.exists())

    def test_job_cierra_descendiente_real_sin_buscar_por_nombre(self):
        if os.name != "nt":
            with self.assertRaisesRegex(OSError, "Windows"):
                supervisor.OwnedProcess.start(None, {}, Path("."))
            return
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receipt = root / "child.pid"
            code = (
                "import subprocess,sys,time,pathlib; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
            )
            spec = supervisor.ServiceSpec(
                "fixture",
                (sys.executable, "-c", code, str(receipt)),
                root,
                0,
                "fixture",
                "cuda_available",
            )
            process = supervisor.OwnedProcess.start(
                spec, child_environment(dict(os.environ), {}), root
            )
            self.addCleanup(process.stop)
            deadline = time.monotonic() + 5
            while not receipt.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(receipt.exists(), "El descendiente propio debe iniciar realmente")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int
            handle = kernel.OpenProcess(0x100000, False, int(receipt.read_text()))
            self.assertTrue(handle)
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                process.stop()
                self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
                self.assertFalse(process.is_alive())
            finally:
                kernel.CloseHandle(handle)
            gc.collect()


class FailedStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_reinicio_real_controlado_solo_reemplaza_su_job(self):
        if os.name != "nt":
            with self.assertRaisesRegex(OSError, "Windows"):
                supervisor.OwnedProcess.start(None, {}, Path("."))
            return
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = supervisor.ServiceSpec(
                "fixture",
                (sys.executable, "-c", "import time; time.sleep(60)"),
                root,
                0,
                "fixture",
                "cuda_available",
            )
            owned = []
            now = [0]

            def factory(*args):
                child = supervisor.OwnedProcess.start(*args)
                owned.append(child)
                return child

            manager = supervisor.BridgeSupervisor(
                [spec],
                {"GPU_BRIDGE_BEARER_TOKEN": "propio"},
                root,
                process_factory=factory,
                probe=lambda *args, **kwargs: False,
                clock=lambda: now[0],
                interval_seconds=3600,
            )
            try:
                await manager.start()
                for stamp in (181, 191, 201):
                    now[0] = stamp
                    await manager.check_once()
                self.assertEqual(len(owned), 2)
                self.assertFalse(owned[0].is_alive())
                self.assertTrue(owned[1].is_alive())
                self.assertEqual(manager.snapshot()["children"]["fixture"]["restarts"], 1)
            finally:
                await manager.stop()
            self.assertTrue(all(not child.is_alive() for child in owned))

    async def test_fallo_de_arranque_cierra_hijos_ya_creados(self):
        created = []

        def factory(spec, env, logs):
            if spec.name == "whisper":
                raise OSError("no exponer detalle")
            child = FakeChild()
            created.append(child)
            return child

        specs = [
            supervisor.ServiceSpec(name, (), Path("."), 8000, name, "cuda_available")
            for name in ("immich", "whisper")
        ]
        manager = supervisor.BridgeSupervisor(
            specs, {"GPU_BRIDGE_BEARER_TOKEN": "propio"}, Path("."), process_factory=factory
        )
        with self.assertRaises(RuntimeError):
            await manager.start()
        self.assertTrue(all(child.stopped for child in created))
        await manager.stop()


if __name__ == "__main__":
    unittest.main()
