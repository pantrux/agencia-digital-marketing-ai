"""Guardas y transiciones del controlador; sin SSH, Docker o ejecución del SDK."""

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    path = ROOT / "docker/postgres-immich" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def container(remote, running=True):
    return {
        "Id": "a" * 64,
        "Image": remote.IMAGE_ID,
        "Created": "2026-11-06T00:00:00Z",
        "Config": {
            "Image": remote.BASE,
            "Labels": {
                "cl.e-scale.agencia.owner": remote.OWNER,
                "cl.e-scale.agencia.build-id": remote.BUILD_ID,
            },
            "Entrypoint": ["/bin/sh"],
            "Cmd": ["/inputs/offline-container.sh", "hold"],
            "WorkingDir": "/work",
            "User": "0:0",
            "Env": remote.BASE_ENV + remote.BUILD_ENV,
        },
        "HostConfig": {
            "NetworkMode": "none",
            "NanoCpus": 4_000_000_000,
            "Memory": 12 * 1024**3,
            "MemorySwap": 12 * 1024**3,
            "PidsLimit": 256,
            "Privileged": False,
            "CapAdd": None,
            "CapDrop": ["NET_RAW"],
            "SecurityOpt": ["no-new-privileges"],
            "RestartPolicy": {"Name": "no"},
            "PortBindings": {},
            "PublishAllPorts": False,
            "Binds": None,
            "VolumesFrom": None,
            "Devices": [],
            "DeviceRequests": None,
            "PidMode": "",
            "IpcMode": "private",
            "Mounts": [
                {"Type": "bind", "Source": remote.INPUTS, "Target": "/inputs", "ReadOnly": True},
                {"Type": "bind", "Source": remote.OUTPUTS, "Target": "/outputs", "ReadOnly": False},
            ],
            "Tmpfs": {"/var/lib/postgresql/data": "rw,nosuid,nodev,noexec,size=1048576"},
        },
        "Mounts": [
            {"Type": "bind", "Source": remote.INPUTS, "Destination": "/inputs", "RW": False},
            {"Type": "bind", "Source": remote.OUTPUTS, "Destination": "/outputs", "RW": True},
            {"Type": "tmpfs", "Source": "", "Destination": "/var/lib/postgresql/data", "RW": True},
        ],
        "State": {
            "Status": "running" if running else "exited",
            "Running": running,
            "Pid": 100 if running else 0,
            "StartedAt": "2026-11-06T00:00:01Z",
        },
    }


class SdkController(unittest.TestCase):
    def setUp(self):
        self.remote = module("offline_sdk_remote")

    def test_inventario_rechaza_tamper_escape_y_limites(self):
        files = {"alpha": {"sha256": hashlib.sha256(b"data").hexdigest(), "size": 4}}
        checksum = hashlib.sha256((files["alpha"]["sha256"] + "  alpha\n").encode()).hexdigest()
        self.remote.validate_inventory(files, checksum, expected_count=1)
        for modified in (
            {"../alpha": files["alpha"]},
            {"alpha": {**files["alpha"], "sha256": "0" * 64}},
            {"alpha": {**files["alpha"], "size": 2**40}},
        ):
            with self.assertRaises(ValueError):
                self.remote.validate_inventory(modified, checksum, expected_count=1)

    def test_no_foreign_rechaza_cid_labels_mounts_y_volumen_anonimo(self):
        image = "sha256:c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a"
        self.assertEqual(self.remote.BASE, image)
        self.assertEqual(self.remote.IMAGE_ID, image)
        historical = [
            "create",
            "--pull",
            "never",
            self.remote.UPSTREAM_BASE,
            "/inputs/offline-container.sh",
            "hold",
        ]
        original = historical.copy()
        self.assertEqual(
            self.remote.derive_create_arguments(historical),
            [
                "create",
                "--pull",
                "never",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=1048576",
                "--user",
                "0:0",
                image,
                "/inputs/offline-container.sh",
                "hold",
            ],
        )
        self.assertEqual(historical, original)
        base = [{"Id": image, "Config": {"User": "70:70", "Env": self.remote.BASE_ENV}}]
        self.remote.check_base_image(base)
        for changed in (
            [
                {
                    "Id": "sha256:4278d7b1e3deb3193ddc803158f6532963f059fc25a2eefaee4b5a9e71741873",
                    "Config": base[0]["Config"],
                }
            ],
            [{"Id": image, "Config": {"User": "0:0", "Env": self.remote.BASE_ENV}}],
        ):
            with self.assertRaises(ValueError):
                self.remote.check_base_image(changed)
        for spelling in (["NET_RAW"], ["CAP_NET_RAW"]):
            actual = container(self.remote)
            actual["HostConfig"]["CapDrop"] = spelling
            self.remote.check_container(actual, "a" * 64)
        for spelling in (
            [],
            None,
            "NET_RAW",
            ["NET_RAW", "CAP_NET_RAW"],
            ["CAP_NET_RAW", "CAP_NET_RAW"],
            ["CAP_NET_RAW", "CAP_NET_ADMIN"],
            ["net_raw"],
            ["CAP_CAP_NET_RAW"],
        ):
            actual = container(self.remote)
            actual["HostConfig"]["CapDrop"] = spelling
            with self.assertRaises(ValueError):
                self.remote.check_container(actual, "a" * 64)
        changes = [
            lambda c: c.update(Id="b" * 64),
            lambda c: c["Config"]["Labels"].update({"cl.e-scale.agencia.owner": "otro"}),
            lambda c: c["HostConfig"]["Mounts"][0].update(Source="/mnt/producciones"),
            lambda c: c["Mounts"].append(
                {"Type": "volume", "Name": "anonimo", "Destination": "/otro"}
            ),
            lambda c: c["HostConfig"].update(NetworkMode="host"),
            lambda c: c["Config"]["Env"].append("SECRET_AJENO=valor"),
            lambda c: c["Mounts"][2].update(c["Mounts"][0]),
            lambda c: c["Config"].update(User=""),
            lambda c: c["Config"].update(User="70:70"),
            lambda c: c["Config"].update(Image=self.remote.UPSTREAM_BASE),
            lambda c: c.update(
                Image="sha256:4278d7b1e3deb3193ddc803158f6532963f059fc25a2eefaee4b5a9e71741873"
            ),
        ]
        for change in changes:
            actual = container(self.remote)
            change(actual)
            with self.assertRaises(ValueError):
                self.remote.check_container(actual, "a" * 64)

    def test_respaldo_de_recibo_es_obligatorio_antes_de_transicion(self):
        receipt = {"receipt": {"stage": "create", "cid": "a" * 64}, "receipt_sha256": "b" * 64}
        prepared = {"receipt": {"stage": "prepare"}, "receipt_sha256": "c" * 64}
        actual = {"prepare": prepared, "create": receipt}
        with self.assertRaises(ValueError):
            self.remote.require_backup("start", actual, {})
        self.remote.require_backup("start", actual, {"prepare": prepared, "create": receipt})
        changed = copy.deepcopy(receipt)
        changed["receipt"]["cid"] = "c" * 64
        with self.assertRaises(ValueError):
            self.remote.require_backup("start", actual, {"prepare": prepared, "create": changed})

    def test_timeout_del_cliente_detiene_cid_y_luego_kill_si_sigue_vivo(self):
        remote = self.remote

        class Docker:
            running = True
            calls = []

            def inspect(self, cid, timeout=2):
                return container(remote, self.running)

            def limits(self, observed):
                return {"verified": True, "pid": observed["pid"]}

            def command(self, args, timeout, sink=None):
                self.calls.append(args)
                if args[0] == "exec":
                    raise TimeoutError("plazo de cliente")
                if args[0] == "kill":
                    self.running = False
                return {"returncode": 0, "output": b"", "process": {"pid": 123}}

        docker = Docker()
        with self.assertRaises(self.remote.StageError) as failure:
            remote.execute_stage(docker, "sdk", "a" * 64, io.BytesIO(), 20)
        self.assertFalse(docker.running)
        self.assertTrue(failure.exception.cleanup["stopped"])
        self.assertEqual([c[0] for c in docker.calls], ["exec", "stop", "kill"])
        self.assertTrue(all(c[-1] == "a" * 64 for c in docker.calls[1:]))

    def test_stop_nunca_senaliza_un_cid_cuyo_montaje_cambio(self):
        remote = self.remote
        docker = mock.Mock()
        actual = container(remote)
        actual["HostConfig"]["Mounts"][0]["Source"] = "/ajeno"
        docker.inspect.return_value = actual
        with self.assertRaises(ValueError):
            remote.stop_owned(docker, "a" * 64)
        docker.command.assert_not_called()

    def test_fallo_de_etapa_conserva_rc_y_proceso_en_diagnostico(self):
        remote = self.remote

        class Docker:
            running = True

            def inspect(self, _cid, timeout=2):
                return container(remote, self.running)

            def limits(self, observed):
                return {"verified": True, "pid": observed["pid"]}

            def command(self, args, timeout, sink=None):
                if args[0] == "exec":
                    return {
                        "returncode": 7,
                        "output": b"",
                        "process": {"pid": 123, "returncode": 7},
                    }
                self.running = False
                return {"returncode": 0, "output": b"", "process": {"pid": 124, "returncode": 0}}

        with self.assertRaises(remote.StageError) as failure:
            remote.execute_stage(Docker(), "sdk", "a" * 64, io.BytesIO(), 20)
        self.assertEqual(failure.exception.cause.process["returncode"], 7)
        self.assertTrue(failure.exception.cleanup["stopped"])

    @unittest.skipUnless(os.name == "posix", "El recolector POSIX se verifica realmente en Linux.")
    def test_plazo_real_recoge_el_cliente_propio(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.remote.run_bounded([sys.executable, "-c", "import time;time.sleep(30)"], 0.15)
        self.assertLess(time.monotonic() - started, 3)

    def test_plan_predeterminado_no_lee_boveda_ni_conecta(self):
        local = module("run_offline_sdk")
        with (
            mock.patch.object(local, "proposal", return_value={"sdk_executed": False}),
            mock.patch.object(local, "read_values") as vault,
            mock.patch.object(local, "connect") as connect,
            mock.patch("sys.stdout", new=io.StringIO()) as output,
        ):
            self.assertEqual(local.main([]), 0)
        vault.assert_not_called()
        connect.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["sdk_executed"])

    def test_start_local_no_contacta_host_sin_cid_respaldado(self):
        local = module("run_offline_sdk")
        controller = local.Controller({}, {}, writer=mock.Mock())
        with mock.patch.object(controller, "request") as request:
            with self.assertRaises(ValueError):
                controller.perform("start")
        request.assert_not_called()

    def test_create_persiste_cid_antes_de_poder_invocar_start(self):
        local = module("run_offline_sdk")
        remote = self.remote
        prepared = {"receipt": {"stage": "prepare", "success": True}, "receipt_sha256": "c" * 64}
        values = {
            local.KEYS["prepared"]: base64.b64encode(
                remote.canonical({"prepare": prepared})
            ).decode()
        }
        events = []

        def writer(_updates):
            events.append("vault")

        plan = {"plan_sha256": "d" * 64, "remote_sha256": "e" * 64}
        controller = local.Controller(values, plan, writer=writer)

        def request(mode, **_options):
            events.append(mode)
            if mode == "start":
                self.assertEqual(controller.saved()["create"]["receipt"]["cid"], "a" * 64)
            receipt = {
                "stage": mode,
                "cid": "a" * 64,
                "success": True,
                "plan_sha256": plan["plan_sha256"],
                "source_sha256": plan["remote_sha256"],
            }
            return {"success": True, "result": remote.envelope(receipt)}

        with mock.patch.object(controller, "request", side_effect=request):
            controller.perform("create")
            self.assertEqual(events, ["create", "vault"])
            controller.perform("start")
        self.assertEqual(events, ["create", "vault", "start", "vault"])

    def test_recibo_alterado_no_se_persiste_en_boveda(self):
        local = module("run_offline_sdk")
        writer = mock.Mock()
        controller = local.Controller(
            {}, {"plan_sha256": "d" * 64, "remote_sha256": "e" * 64}, writer=writer
        )
        value = {
            "receipt": {
                "stage": "create",
                "success": True,
                "cid": "a" * 64,
                "plan_sha256": "d" * 64,
                "source_sha256": "e" * 64,
            },
            "receipt_sha256": "0" * 64,
        }
        with self.assertRaises(ValueError):
            controller.persist("container", "create", value)
        writer.assert_not_called()

    def test_fuente_local_alterada_se_rechaza_sin_autenticar(self):
        local = module("run_offline_sdk")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"otro")
            expected = {"sha256": hashlib.sha256(b"dato").hexdigest(), "size": 4}
            with mock.patch.object(local, "connect") as connect:
                with self.assertRaises(ValueError):
                    local.verify_local_input(path, expected)
            connect.assert_not_called()

    def test_salida_continua_tambien_respeta_deadline(self):
        local = module("run_offline_sdk")
        clock = [0.0]

        class Channel:
            def recv_ready(self):
                return True

            def recv(self, _size):
                clock[0] += 0.25
                return b"x"

            def recv_stderr_ready(self):
                return False

            def recv_stderr(self, _size):
                return b""

        with mock.patch.object(local.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError):
                local.collect(Channel(), 0.5, 4096)

    @unittest.skipUnless(
        os.name == "posix", "NoFollow por descriptor se verifica realmente en Linux."
    )
    def test_nofollow_no_reescribe_archivo_fuera_del_directorio(self):
        if os.geteuid() != os.getegid():
            self.skipTest("El fixture requiere UID/GID iguales.")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            foreign = root / "foreign"
            foreign.write_bytes(b"centinela")
            directory = root / "owned"
            directory.mkdir(mode=0o700)
            (directory / "file").symlink_to(foreign)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    self.remote.write_file(descriptor, "file", b"cambio", uid=os.geteuid())
                with self.assertRaises(OSError):
                    self.remote.read_file(descriptor, "file", uid=os.geteuid())
            finally:
                os.close(descriptor)
            self.assertEqual(foreign.read_bytes(), b"centinela")

    @unittest.skipUnless(os.name == "posix", "La pérdida real del latido se verifica en Linux.")
    def test_eof_de_latido_recoge_cliente_propio_sin_esperar_timeout(self):
        reader, writer = os.pipe()
        os.close(writer)
        started = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                self.remote.run_bounded(
                    [sys.executable, "-c", "import time;time.sleep(30)"], 20, heartbeat_fd=reader
                )
        finally:
            os.close(reader)
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
