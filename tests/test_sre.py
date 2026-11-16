"""Pruebas locales de salud y reversión: no se conectan a la plataforma."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sre_control", ROOT / "sre/sre_control.py")
sre = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sre)


class SaludHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 200
        if self.path == "/slow":
            time.sleep(0.7)
        if self.path == "/parallel":
            try:
                self.server.probe_barrier.wait(timeout=1.5)
            except threading.BrokenBarrierError:
                status = 503
        payload = b"<html>Ingresar</html>" if self.path == "/login" else b'{"status":"ok"}'
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *_args):
        pass


class PruebasSRE(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SaludHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_rechaza_html_200_de_inicio_de_sesion(self):
        probe = {
            "name": "ejemplo",
            "url": self.url + "/login",
            "method": "GET",
            "contract": "status",
        }
        result = sre.probe_one(probe, sre.Deadline(2), {})
        self.assertFalse(result["ok"])

    def test_acepta_json_de_salud_con_200(self):
        probe = {
            "name": "ejemplo",
            "url": self.url,
            "method": "GET",
            "contract": "status",
        }
        result = sre.probe_one(probe, sre.Deadline(2), {})
        self.assertTrue(result["ok"])

    def test_sondas_reales_se_ejecutan_en_paralelo(self):
        self.server.probe_barrier = threading.Barrier(4)
        probes = [
            {
                "name": str(index),
                "url": self.url + "/parallel",
                "method": "GET",
                "contract": "status",
            }
            for index in range(4)
        ]
        start = time.monotonic()
        result = sre.run_probes(probes, sre.Deadline(2), {})
        self.assertTrue(all(item["ok"] for item in result))
        self.assertLess(time.monotonic() - start, 2)

    def test_timeout_real_respeta_presupuesto(self):
        probe = {
            "name": "lenta",
            "url": self.url + "/slow",
            "method": "GET",
            "contract": "status",
        }
        start = time.monotonic()
        result = sre.probe_one(probe, sre.Deadline(0.2), {})
        self.assertFalse(result["ok"])
        self.assertLess(time.monotonic() - start, 1)

    def test_token_faltante_falla_sin_hacer_peticion(self):
        probe = {
            "name": "privada",
            "url": self.url,
            "method": "GET",
            "contract": "status",
            "token_env": "TOKEN_FALTANTE",
        }
        result = sre.probe_one(probe, sre.Deadline(2), {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "credencial_ausente")

    def test_contrato_gateway_exige_los_cinco_agentes_y_cuatro_mcps(self):
        self.assertFalse(sre.validate_body("agents", {"status": "ok", "agents": [], "mcps": []}))
        self.assertTrue(
            sre.validate_body("agents", {"status": "ok", "agents": sre.AGENTS, "mcps": sre.MCPS})
        )

    def test_json_malformado_de_dependencias_no_causa_excepcion(self):
        for checks in (None, [], "ok", 1):
            with self.subTest(checks=checks):
                self.assertFalse(sre.validate_body("edge", {"status": "ok", "checks": checks}))
        for agents in (None, {}, ",".join(sre.AGENTS), 1):
            with self.subTest(agents=agents):
                self.assertFalse(
                    sre.validate_body(
                        "agents", {"status": "ok", "agents": agents, "mcps": sre.MCPS}
                    )
                )

    def test_contrato_gpu_no_acepta_salud_sin_cuda_o_nvenc(self):
        self.assertFalse(sre.validate_body("cuda", {"status": "ok"}))
        self.assertFalse(sre.validate_body("nvenc", {"status": "ok", "nvenc_available": False}))

    def test_contratos_de_salud_de_la_plataforma_desplegada(self):
        self.assertTrue(sre.validate_body("edge", dict(ready=True, d1=True, kv=True, r2=True)))
        self.assertFalse(sre.validate_body("edge", dict(ready=True, d1=False, kv=True, r2=True)))
        self.assertTrue(sre.validate_body("consumer", dict(ready=True, busy=False)))
        self.assertFalse(sre.validate_body("consumer", dict(ready=True, busy=True)))
        self.assertTrue(sre.validate_body("gateway", {"ready": True}))
        self.assertFalse(sre.validate_body("gateway", {"ready": True, "failing": ["error"]}))
        kitsu = {"version": "1.2.3", "database-up": True, "key-value-store-up": True}
        self.assertTrue(sre.validate_body("kitsu", kitsu))
        self.assertFalse(sre.validate_body("kitsu", {**kitsu, "database-up": False}))

    def crear_manifiesto(self, directory):
        compose = directory / "compose.yml"
        compose.write_text(
            'services:\n  app:\n    image: "registry/app@sha256:' + "a" * 64 + '"\n',
            encoding="utf-8",
        )
        openclaw = directory / "openclaw.json"
        models = ["terra", "sol", "sol", "terra", "sol"]
        openclaw.write_text(
            json.dumps(
                {
                    "agents": {
                        "list": [
                            {"id": name, "model": "openai/gpt-6.1-" + model}
                            for name, model in zip(sre.AGENTS, models, strict=True)
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        manifest = {
            "schema_version": 1,
            "release_sha": "a" * 40,
            "cloudflare": {
                "worker": "studio-api-worker",
                "version_id": "11111111-1111-4111-8111-111111111111",
            },
            "compose": {
                "path": str(compose),
                "sha256": hashlib.sha256(compose.read_bytes()).hexdigest(),
            },
            "openclaw": {
                "path": str(openclaw),
                "sha256": hashlib.sha256(openclaw.read_bytes()).hexdigest(),
                "current_sha256": "b" * 64,
                "ssh_user": "jandrade",
            },
        }
        path = directory / "release-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path, hashlib.sha256(path.read_bytes()).hexdigest(), manifest

    def test_manifiesto_y_artefactos_validos_se_aceptan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, digest, manifest = self.crear_manifiesto(Path(tmp))
            self.assertEqual(
                sre.load_manifest(path, digest)["release_sha"], manifest["release_sha"]
            )

    def test_manifiesto_modificado_se_rechaza_antes_de_mutar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, digest, _manifest = self.crear_manifiesto(Path(tmp))
            path.write_text(path.read_text() + " ", encoding="utf-8")
            with self.assertRaises(ValueError):
                sre.load_manifest(path, digest)

    def test_artefacto_modificado_se_rechaza(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, digest, manifest = self.crear_manifiesto(Path(tmp))
            Path(manifest["compose"]["path"]).write_text("services: {}", encoding="utf-8")
            with self.assertRaises(ValueError):
                sre.load_manifest(path, digest)

    def test_rechaza_imagen_mutable_aunque_coincida_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _digest, manifest = self.crear_manifiesto(Path(tmp))
            artifact = Path(manifest["compose"]["path"])
            artifact.write_text(
                "services:\n  app:\n    image: registry/app:latest\n", encoding="utf-8"
            )
            manifest["compose"]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                sre.load_manifest(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_rechaza_build_local_en_artefacto_de_reversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, _digest, manifest = self.crear_manifiesto(Path(tmp))
            artifact = Path(manifest["compose"]["path"])
            artifact.write_text(artifact.read_text() + "    build: .\n", encoding="utf-8")
            manifest["compose"]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                sre.load_manifest(path, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_simulacion_fallida_no_declara_exito(self):
        fixture = {
            "probes": {name: {"ok": name != "edge"} for name in sre.PROBE_NAMES},
            "rollback": {"cloudflare": True, "compose": True, "openclaw": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            fixture_path = Path(tmp) / "simulacion.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(sre.main(["smoke", "--simulate", str(fixture_path)]), 1)

    def test_simulacion_no_ejecuta_comandos_de_produccion(self):
        with (
            patch.object(sre.subprocess, "Popen", side_effect=AssertionError("No debe mutar")),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                sre.main(
                    [
                        "smoke",
                        "--simulate",
                        str(ROOT / "tests/fixtures/sre/simulation-ok.json"),
                    ]
                ),
                0,
            )

    def test_reversion_simulada_exige_reverificacion_completa(self):
        fixture = {"rollback": {"cloudflare": True, "compose": True, "openclaw": True}}
        with tempfile.TemporaryDirectory() as tmp:
            path, digest, _manifest = self.crear_manifiesto(Path(tmp))
            fixture_path = Path(tmp) / "simulacion.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(
                    sre.main(
                        [
                            "rollback",
                            "--manifest",
                            str(path),
                            "--manifest-sha256",
                            digest,
                            "--simulate",
                            str(fixture_path),
                        ]
                    ),
                    1,
                )
            report = json.loads(output.getvalue())
            self.assertFalse(report["ok"])
            self.assertFalse(report["production_verified"])

    def test_reversion_simulada_exitosa_no_acredita_produccion(self):
        fixture = {
            "rollback": {"cloudflare": True, "compose": True, "openclaw": True},
            "post_rollback_probes": {name: {"ok": True} for name in sre.PROBE_NAMES},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path, digest, _manifest = self.crear_manifiesto(Path(tmp))
            fixture_path = Path(tmp) / "simulacion.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            with (
                patch.object(sre.subprocess, "run", side_effect=AssertionError("No debe mutar")),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(
                    sre.main(
                        [
                            "rollback",
                            "--manifest",
                            str(path),
                            "--manifest-sha256",
                            digest,
                            "--simulate",
                            str(fixture_path),
                        ]
                    ),
                    0,
                )
            self.assertFalse(json.loads(output.getvalue())["production_verified"])

    def test_sondas_no_envian_secretos_a_origen_diferente(self):
        with tempfile.TemporaryDirectory() as tmp:
            probes = sre.default_probes()
            probes[0]["url"] = "https://otro-dominio.invalid/api/healthz"
            path = Path(tmp) / "probes.json"
            path.write_text(json.dumps(probes), encoding="utf-8")
            with self.assertRaises(ValueError):
                sre.load_probes(path)

    def test_fallo_posdespliegue_publica_alerta_con_resultado_de_reversion(self):
        manifest = {"schema_version": 2, "release_sha": "a" * 40}
        failure = [{"name": "edge", "ok": False}]
        healthy = [{"name": "edge", "ok": True}]
        restored = {"cloudflare": True, "compose": True, "openclaw": True}
        with (
            patch.object(sre, "load_manifest", return_value=manifest),
            patch.object(sre, "run_probes", side_effect=[failure, healthy]),
            patch.object(sre, "run_rollback", return_value=restored),
            patch.object(sre, "send_rollback_alert", create=True, return_value=True) as send,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(
                sre.main(["smoke", "--manifest", "ejemplo", "--manifest-sha256", "a" * 64]), 1
            )
        report = json.loads(output.getvalue())
        self.assertTrue(report["production_verified"])
        self.assertEqual(report.get("alerta_sre"), "aceptada_por_prometheus")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], manifest["release_sha"])

    def test_alerta_publica_solo_el_resumen_esperado(self):
        report = {
            "ok": False,
            "probes": [{"name": "edge", "ok": False, "reason": "TOKEN_PRIVADO"}],
            "rollback_components": {"cloudflare": True, "compose": False, "openclaw": True},
            "elapsed_seconds": 11.2,
            "fault_injected": "edge",
        }
        payload = sre.rollback_alert_payload(report, "a" * 40, 3_000_000_000)
        point = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"][
            "dataPoints"
        ][0]
        labels = {value["key"]: value["value"]["stringValue"] for value in point["attributes"]}
        self.assertEqual(point["asInt"], "3")
        self.assertEqual(labels["sondas"], "edge")
        self.assertEqual(labels["componentes"], "compose")
        self.assertEqual(labels["resultado"], "pendiente")
        self.assertEqual(labels["modo"], "ensayo")
        self.assertEqual(labels["entrega"], "a" * 12)
        self.assertNotIn("TOKEN_PRIVADO", json.dumps(payload))

    def test_presupuesto_siempre_menor_a_90_segundos(self):
        self.assertLess(sre.TOTAL_BUDGET_SECONDS, 90)
        with self.assertRaises(ValueError):
            sre.Deadline(90)

    def test_reversion_productiva_bloqueada_hasta_definir_activacion_openclaw(self):
        with (
            patch.object(sre.subprocess, "Popen", side_effect=AssertionError("No debe mutar")),
            self.assertRaises(ValueError),
        ):
            sre.run_rollback({}, sre.Deadline(1))

    def test_watchdog_termina_tambien_el_subproceso_hijo(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "no-debe-existir"
            child = (
                "import pathlib,time; time.sleep(0.8); pathlib.Path("
                + repr(str(sentinel))
                + ").write_text('hijo vivo')"
            )
            parent = (
                "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
                + repr(child)
                + "]); time.sleep(10)"
            )
            started = time.monotonic()
            result = sre.run_process([sys.executable, "-c", parent], 0.25)
            self.assertIsNone(result)
            self.assertLess(time.monotonic() - started, 2)
            time.sleep(0.9)
            self.assertFalse(sentinel.exists())

    @unittest.skipUnless(os.name == "nt", "Contrato específico del kernel de Windows")
    def test_timeout_windows_no_lanza_utilidad_externa_para_limpiar(self):
        with patch.object(
            sre.subprocess, "run", side_effect=AssertionError("No debe lanzar taskkill")
        ):
            self.assertIsNone(
                sre.run_process([sys.executable, "-c", "import time;time.sleep(10)"], 0.1)
            )

    def test_arranque_se_descuenta_del_presupuesto_de_comunicacion(self):
        process = Mock(returncode=0)
        process.communicate.return_value = ("salida", None)
        with (
            patch.object(sre.os, "name", "posix"),
            patch.object(sre.subprocess, "Popen", return_value=process),
            patch.object(sre.time, "monotonic", side_effect=[0, 0.15]),
        ):
            self.assertEqual(sre.run_process(["comando-ficticio"], 0.2), (0, "salida"))
        self.assertLessEqual(process.communicate.call_args.kwargs["timeout"], 0.051)

    def test_presupuesto_agotado_no_crea_proceso(self):
        with patch.object(sre.subprocess, "Popen") as launch:
            self.assertIsNone(sre.run_process(["comando-ficticio"], 0))
        launch.assert_not_called()

    def test_recolector_es_unico_dueno_del_cierre_de_pipes_delegados(self):
        release, closed = threading.Event(), threading.Event()
        owners = []
        process = Mock(returncode=1)
        process.poll.return_value = 1
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("propio", 0.1),
            subprocess.TimeoutExpired("propio", 0.1),
        ]
        process.wait.side_effect = release.wait

        def close_pipes(_process):
            owners.append(threading.current_thread().name)
            closed.set()

        try:
            with (
                patch.object(sre.os, "name", "nt"),
                patch.object(sre, "_WindowsJob"),
                patch.object(sre.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
                patch.object(sre.subprocess, "CREATE_NO_WINDOW", 134217728, create=True),
                patch.object(sre.subprocess, "Popen", return_value=process),
                patch.object(sre, "_close_process_pipes", side_effect=close_pipes),
            ):
                self.assertIsNone(sre.run_process(["proceso-propio"], 0.1))
                self.assertFalse(closed.is_set())
                release.set()
                self.assertTrue(closed.wait(1))
            self.assertEqual(owners, ["sre-recoger-proceso"])
        finally:
            release.set()

    @unittest.skipUnless(os.name == "nt", "Contrato específico del kernel de Windows")
    def test_snapshot_windows_respeta_abi_de_thread_entry64(self):
        job = sre._WindowsJob()
        try:
            self.assertEqual(job.ctypes.sizeof(job.thread_entry), 120)
            self.assertEqual(job.thread_entry.ProcessId.offset, 16)
            self.assertEqual(job.thread_entry.ThreadId.offset, 20)
            self.assertEqual(job.thread_entry.SuspendCount.offset, 108)
            self.assertEqual(job.thread_entry.ContextRecord.offset, 112)
        finally:
            job.close()

    @unittest.skipUnless(os.name == "nt", "Contrato específico del kernel de Windows")
    def test_fallo_al_reanudar_recoge_proceso_suspendido_y_pipes(self):
        job = sre._WindowsJob()
        launched = []
        original = sre.subprocess.Popen

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            launched.append(process)
            return process

        try:
            with (
                patch.object(sre, "_WindowsJob", return_value=job),
                patch.object(sre.subprocess, "Popen", side_effect=launch),
                patch.object(job.kernel, "ResumeThread", return_value=0xFFFFFFFF) as resume,
                self.assertRaises(OSError),
            ):
                sre.run_process(
                    [sys.executable, "-c", "import time;time.sleep(10)"], 2, capture_output=True
                )
            resume.assert_called_once()
            self.assertIsNotNone(launched[0].poll())
            self.assertTrue(launched[0].stdout.closed)
        finally:
            job.close()

    @unittest.skipUnless(os.name == "nt", "Contrato específico del kernel de Windows")
    def test_descendiente_real_con_stdout_heredado_termina_con_job(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "hijo-iniciado"
            child = "import time;time.sleep(10)"
            parent = (
                "import subprocess,sys,pathlib,os;p=subprocess.Popen([sys.executable,'-c',"
                + repr(child)
                + "]);pathlib.Path("
                + repr(str(ready))
                + ").write_text(str(p.pid));os._exit(0)"
            )
            result = {}
            worker = threading.Thread(
                target=lambda: result.update(
                    value=sre.run_process([sys.executable, "-c", parent], 6, capture_output=True)
                )
            )
            handle = None
            worker.start()
            try:
                until = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < until:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "El padre debe haber creado el proceso hijo real")
                handle = kernel.OpenProcess(0x00100000, False, int(ready.read_text()))
                self.assertTrue(handle)
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                worker.join(7)
                self.assertFalse(worker.is_alive())
                self.assertIsNone(result["value"])
                self.assertEqual(kernel.WaitForSingleObject(handle, 500), 0)
            finally:
                worker.join(8)
                if handle:
                    kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
