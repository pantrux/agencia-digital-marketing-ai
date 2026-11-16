"""Planes de build y control remoto simulado; nunca abre SSH ni ejecuta Docker."""

import base64
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load("media_component_build", ROOT / "docker/build_component.py")
REMOTE = load("component_build_remote", ROOT / "docker/component_build_remote.py")
LAYOUT = load("component_layout", ROOT / "docker/host_layout.py")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.source = self.workspace / "docker/immich-ml-auth"
        self.source.mkdir(parents=True)
        self.base = "python:3.12.16-alpine3.24@sha256:" + "1" * 64
        self.files = {
            "Dockerfile": ("FROM " + self.base + "\nCOPY proxy.py /app/proxy.py\n").encode(),
            "proxy.py": b"print('control publico')\n",
        }
        for name, data in self.files.items():
            (self.source / name).write_bytes(data)
        self.identities = []
        for number, (path, role) in enumerate(LAYOUT.TARGETS.items(), 1):
            self.identities.append(
                {
                    "path": path,
                    "role": role,
                    "changed": True,
                    "directory": {"device": 1, "inode": number},
                    "marker_sha256": "2" * 64,
                    "receipt_sha256": "3" * 64,
                }
            )
        self.spec = {
            "schema_version": 1,
            "helper_sha256": sha((ROOT / "docker/component_build_remote.py").read_bytes()),
            "layout_helper_sha256": sha((ROOT / "docker/host_layout.py").read_bytes()),
            "components": {
                "immich-ml-auth": {
                    "build_id": "fase4-control-01",
                    "tag": "media-platform/immich-ml-auth:fase4-control-01",
                    "base": self.base,
                    "files": {name: sha(data) for name, data in self.files.items()},
                    "context_root": "/opt/media-platform/build/contexts",
                    "logs_root": "/var/lib/media-platform/build",
                }
            },
        }
        self.values = {
            "MEDIA_VM_IP": "192.168.0.85",
            "MEDIA_VM_SSH_USER": "jandrade",
            "MEDIA_VM_SSH_HOST_FINGERPRINT": "SHA256:"
            + base64.b64encode(hashlib.sha256(b"pin-publico-fixture").digest())
            .decode()
            .rstrip("="),
            "MEDIA_PLATFORM_INSTALL_ROOT": "/opt/media-platform",
            "MEDIA_PLATFORM_DATA_ROOT": "/var/lib/media-platform",
            "MEDIA_PLATFORM_OWNER_MARKER": LAYOUT.OWNER,
            "MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64": encoded(
                {
                    "success": True,
                    "owner": LAYOUT.OWNER,
                    "plan_sha256": "4" * 64,
                    "targets": self.identities,
                }
            ),
            "MEDIA_PLATFORM_COMPONENT_BUILDS_B64": encoded(self.spec),
        }

    def plan(self):
        self.values["MEDIA_PLATFORM_COMPONENT_BUILDS_B64"] = encoded(self.spec)
        return BUILD.prepare_plan(self.values, "immich-ml-auth", self.workspace)

    def test_plan_sin_red_contiene_solo_whitelist_y_hash_estable(self):
        plan = self.plan()
        self.assertEqual(plan, self.plan())
        self.assertEqual(set(plan["files"]), {"Dockerfile", "proxy.py"})
        self.assertEqual(plan["plan_sha256"], REMOTE.plan_hash(plan))
        self.assertNotIn("PRIVATE_KEY", json.dumps(plan))

    def test_componente_no_autorizado_y_rutas_ajenas_se_rechazan(self):
        with self.assertRaises(ValueError):
            BUILD.prepare_plan(self.values, "immich-server", self.workspace)
        self.spec["components"]["immich-ml-auth"]["context_root"] = "/opt/ajeno"
        with self.assertRaises(ValueError):
            self.plan()

    def test_hash_de_fuente_o_helper_alterado_bloquea_plan(self):
        (self.source / "proxy.py").write_bytes(b"alterado")
        with self.assertRaises(ValueError):
            self.plan()
        (self.source / "proxy.py").write_bytes(self.files["proxy.py"])
        self.spec["helper_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.plan()

    def test_base_tag_y_destinos_invalidos_se_rechazan(self):
        entry = self.spec["components"]["immich-ml-auth"]
        for field, value in (
            ("base", "python:latest"),
            ("tag", "ajeno/app:latest"),
            ("build_id", "../escape"),
            ("logs_root", "/var/log/ajeno"),
        ):
            before = entry[field]
            with self.subTest(field=field):
                entry[field] = value
                with self.assertRaises(ValueError):
                    self.plan()
            entry[field] = before

    def test_archivo_con_hardlink_se_rechaza(self):
        import os

        os.link(self.source / "proxy.py", self.source / "alias.py")
        with self.assertRaises(ValueError):
            self.plan()

    def test_plan_no_incluye_readme_caches_ni_secretos_del_workspace(self):
        (self.source / "README.md").write_text("documentacion", encoding="utf-8")
        (self.source / "__pycache__").mkdir()
        (self.workspace / ".env").write_text("SECRETO=valor-no-transferible", encoding="utf-8")
        plan = self.plan()
        self.values["MEDIA_PLATFORM_BUILD_APPROVED_PLAN_SHA256"] = plan["plan_sha256"]
        payload = BUILD.make_payload(plan, self.values, self.workspace)
        self.assertEqual(set(payload["files"]), set(self.files))
        self.assertNotIn("valor-no-transferible", json.dumps(payload))

    def test_apply_necesita_hash_igual_en_boveda_y_plan_antes_de_ssh(self):
        plan = self.plan()
        with patch.object(BUILD, "connect_ssh") as connection:
            with self.assertRaises(ValueError):
                BUILD.apply_remote(plan, self.values, "0" * 64, self.workspace)
        connection.assert_not_called()

    def test_payload_alterado_se_rechaza_antes_de_docker(self):
        plan = self.plan()
        self.values["MEDIA_PLATFORM_BUILD_APPROVED_PLAN_SHA256"] = plan["plan_sha256"]
        payload = BUILD.make_payload(plan, self.values, self.workspace)
        REMOTE.validate_payload(payload)
        payload["files"]["proxy.py"] = base64.b64encode(b"ajeno").decode()
        with self.assertRaises(ValueError):
            REMOTE.validate_payload(payload)

    def test_comando_exacto_no_git_y_sin_contenedores(self):
        plan = self.plan()
        command = REMOTE.build_command(plan)
        self.assertEqual(command[:3], ["/usr/bin/docker", "buildx", "build"])
        for flag in ("--network=none", "--provenance=false", "--load", "--platform=linux/amd64"):
            self.assertIn(flag, command)
        self.assertNotIn("run", command)
        self.assertNotIn("bake", command)
        environment = REMOTE.build_environment()
        for key in ("BUILDX_GIT_INFO", "BUILDX_GIT_LABELS", "BUILDX_GIT_CHECK_DIRTY"):
            self.assertEqual(environment[key], "false")

    def test_sudo_env_coloca_flags_despues_de_sudo(self):
        command = BUILD.remote_command(b"print('codigo publico')")
        self.assertTrue(command.startswith("sudo -n env BUILDX_GIT_INFO=false "))
        self.assertIn("BUILDX_GIT_LABELS=false", command)
        self.assertIn("--apply", command)

    def test_layout_esperado_no_admite_otro_inode_o_ruta(self):
        plan = self.plan()
        invalid = copy.deepcopy(plan)
        invalid["layout"]["targets"][0]["path"] = "/opt/ajeno"
        invalid["plan_sha256"] = REMOTE.plan_hash(invalid)
        with self.assertRaises(ValueError):
            REMOTE.validate_plan(invalid)

    def runtime(self):
        plan = self.plan()
        self.values["MEDIA_PLATFORM_BUILD_APPROVED_PLAN_SHA256"] = plan["plan_sha256"]
        payload = BUILD.make_payload(plan, self.values, self.workspace)
        image_id = "sha256:" + "9" * 64

        class Store:
            def __init__(self):
                self.prepared = False
                self.receipt = None

            def existing(self):
                return self.receipt

            def prepare(self, files):
                self.prepared = True

            def log_build(self, runner, command):
                return runner.build(command)

            def image_id(self):
                return image_id

            def complete(self, result):
                self.receipt = result
                return result

        class Runner:
            def __init__(self):
                self.ids = []
                self.build_exit = 0
                self.calls = []

            def verify_versions(self):
                self.calls.append("versions")

            def tag_ids(self, tag):
                return self.ids

            def build(self, command):
                self.calls.append(command)
                self.ids = [image_id]
                return self.build_exit

        return payload, Store(), Runner(), image_id

    def test_apply_simulado_entrega_id_sin_sca_ni_contenedores(self):
        payload, store, runner, image_id = self.runtime()
        result = REMOTE.run_apply(payload, store, runner)
        self.assertEqual(result["image_id"], image_id)
        self.assertFalse(result["containers_created"])
        self.assertFalse(result["final_sca_verified"])
        self.assertTrue(store.prepared)

    def test_tag_existente_no_se_sobrescribe(self):
        payload, store, runner, _ = self.runtime()
        runner.ids = ["sha256:" + "7" * 64]
        with self.assertRaises(ValueError):
            REMOTE.run_apply(payload, store, runner)
        self.assertFalse(any(isinstance(call, list) for call in runner.calls))

    def test_build_fallido_no_emite_recibo_ni_imagen_aprobada(self):
        payload, store, runner, _ = self.runtime()
        runner.build_exit = 1
        with self.assertRaises(ValueError):
            REMOTE.run_apply(payload, store, runner)
        self.assertIsNone(store.receipt)

    def test_recibo_existente_solo_se_reusa_si_tag_sigue_en_mismo_id(self):
        payload, store, runner, _ = self.runtime()
        first = REMOTE.run_apply(payload, store, runner)
        runner.calls.clear()
        self.assertEqual(REMOTE.run_apply(payload, store, runner)["image_id"], first["image_id"])
        self.assertFalse(any(isinstance(call, list) for call in runner.calls))
        runner.ids = ["sha256:" + "8" * 64]
        with self.assertRaises(ValueError):
            REMOTE.run_apply(payload, store, runner)

    def test_contrato_de_inspect_del_sdk_no_inventa_opcion_format(self):
        runner = REMOTE.DockerRunner(self.plan())
        with patch.object(
            runner,
            "capture",
            side_effect=[
                "29.9.3",
                "github.com/docker/buildx v0.38.1 0dbe87f",
                "Name: default\nDriver: docker\nNodes:\n",
            ],
        ) as capture:
            runner.verify_versions()
        self.assertEqual(capture.call_args_list[-1].args[0], ["buildx", "inspect", "default"])

    def test_ssh_fija_pin_y_clave_sin_agente_ni_alternativas(self):
        values = dict(self.values)
        client_raw, host_raw = b"publica-cliente-simulada", b"publica-host-simulada"
        values.update(
            {
                "MEDIA_VM_SSH_PUBLIC_KEY": "ssh-ed25519 " + base64.b64encode(client_raw).decode(),
                "MEDIA_VM_SSH_HOST_PUBLIC_KEY": "ssh-ed25519 "
                + base64.b64encode(host_raw).decode(),
                "MEDIA_VM_SSH_HOST_FINGERPRINT": "SHA256:"
                + base64.b64encode(hashlib.sha256(host_raw).digest()).decode().rstrip("="),
                "MEDIA_VM_SSH_PRIVATE_KEY_B64": base64.b64encode(
                    b"fixture-no-es-clave-real"
                ).decode(),
            }
        )
        client, key = MagicMock(), MagicMock()
        key.asbytes.return_value = client_raw
        module = SimpleNamespace(
            SSHClient=MagicMock(return_value=client),
            Ed25519Key=SimpleNamespace(from_private_key=MagicMock(return_value=key)),
            PKey=SimpleNamespace(from_type_string=MagicMock(return_value="publica-fijada")),
            RejectPolicy=MagicMock(return_value="rechazo-estricto"),
        )
        with (
            patch.dict("sys.modules", {"paramiko": module}),
            patch.object(BUILD.importlib.metadata, "version", return_value="5.1.0"),
        ):
            self.assertIs(BUILD.connect_ssh(values), client)
        self.assertFalse(client.connect.call_args.kwargs["allow_agent"])
        self.assertFalse(client.connect.call_args.kwargs["look_for_keys"])
        client.set_missing_host_key_policy.assert_called_once_with("rechazo-estricto")
        client.get_host_keys().add.assert_called_once_with(
            "192.168.0.85", "ssh-ed25519", "publica-fijada"
        )

    def test_cli_plan_no_abre_ssh_y_parser_canonico_rechaza_duplicados(self):
        self.values["MEDIA_PLATFORM_COMPONENT_BUILDS_B64"] = encoded(self.spec)
        with (
            patch.object(BUILD, "read_values", return_value=self.values),
            patch.object(BUILD, "prepare_plan", return_value=self.plan()),
            patch.object(BUILD, "connect_ssh") as connect,
            patch("sys.argv", ["build_component.py", "immich-ml-auth"]),
            patch("builtins.print"),
        ):
            self.assertEqual(BUILD.main(), 0)
        connect.assert_not_called()
        path = self.workspace / "vault-fixture"
        path.write_text("CLAVE=literal\nCLAVE=duplicada\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            BUILD.read_values(path)

    def test_timeout_cierra_grupo_aunque_lider_salio_y_pipe_sigue_abierto(self):
        process = MagicMock()
        process.pid = 44551
        process.poll.return_value = 0
        process.returncode = None
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.get_map.return_value = {"pipe": True}
        selector.select.side_effect = TimeoutError("pipe retenido por hijo propio")
        runner = REMOTE.DockerRunner(self.plan())
        with (
            patch.object(REMOTE.subprocess, "Popen", return_value=process),
            patch.object(REMOTE.selectors, "DefaultSelector", return_value=selector),
            patch.object(REMOTE.os, "killpg", create=True) as kill_group,
            patch.object(REMOTE.signal, "SIGKILL", 9, create=True),
            patch.object(REMOTE.time, "sleep"),
        ):
            with self.assertRaises(TimeoutError):
                runner.build(["comando-propio-simulado"], MagicMock())
        self.assertEqual(kill_group.call_args_list[0].args, (44551, REMOTE.signal.SIGTERM))
        self.assertEqual(kill_group.call_args_list[-1].args, (44551, 9))
        process.poll.assert_not_called()
        process.wait.assert_called()

    def layout_fixture(self):
        plan = self.plan()
        inventory = {
            "ancestors": {
                path: {
                    "kind": "directory",
                    "uid": 0,
                    "gid": 0,
                    "mode": 0o755,
                    "device": 1,
                    "inode": number + 10,
                }
                for number, path in enumerate(LAYOUT.ANCESTORS)
            },
            "targets": {},
        }
        identities = []
        for number, (path, role) in enumerate(LAYOUT.TARGETS.items(), 1):
            node = {
                "kind": "directory",
                "uid": 0,
                "gid": 0,
                "mode": 0o700,
                "device": 1,
                "inode": number,
            }
            marker = LAYOUT.new_marker(path, node, "4" * 64, "2026-11-06T00:00:00+00:00")
            receipt = LAYOUT.new_receipt(marker, "4" * 64)
            inventory["targets"][path] = {
                "node": node,
                "marker": marker,
                "receipt": receipt,
                "extra_entries": 0,
            }
            identities.append(
                {
                    "path": path,
                    "role": role,
                    "directory": {"device": 1, "inode": number},
                    "marker_sha256": LAYOUT.digest(marker),
                    "receipt_sha256": LAYOUT.digest(receipt),
                }
            )
        context = MagicMock()
        context.__enter__.return_value.inspect.return_value = inventory
        module = SimpleNamespace(
            inspect_host=lambda: {
                "system": "Linux",
                "uid": 0,
                "hostname": "vm-media-01",
                "ipv4": ["192.168.0.85"],
            },
            host_contract=LAYOUT.host_contract,
            LocalFilesystem=lambda: context,
            make_plan=LAYOUT.make_plan,
            directory_identity=LAYOUT.directory_identity,
            digest=LAYOUT.digest,
        )
        plan["layout"]["targets"] = identities
        plan["plan_sha256"] = REMOTE.plan_hash(plan)
        return plan, module

    def test_raices_reales_deben_coincidir_con_inodos_y_ambos_hashes(self):
        plan, module = self.layout_fixture()
        REMOTE.verify_layout(plan, module)
        for field in ("marker_sha256", "receipt_sha256", "directory"):
            invalid = copy.deepcopy(plan)
            invalid["layout"]["targets"][1][field] = (
                {"device": 1, "inode": 99} if field == "directory" else "0" * 64
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                REMOTE.verify_layout(invalid, module)

    def test_controller_transfiere_solo_payload_publico_y_cierra_canal(self):
        plan = self.plan()
        self.values["MEDIA_PLATFORM_BUILD_APPROVED_PLAN_SHA256"] = plan["plan_sha256"]
        self.values["OTRA_CREDENCIAL_NO_TRANSFERIBLE"] = "no-debe-viajar"
        result = {
            **REMOTE.result_fields(plan),
            "image_id": "sha256:" + "9" * 64,
            "log_sha256": "6" * 64,
            "changed": True,
        }
        channel, client = MagicMock(), MagicMock()
        client.get_transport.return_value.open_session.return_value = channel
        channel.recv_ready.side_effect = [True, False, False]
        channel.recv.return_value = json.dumps(result).encode()
        channel.recv_stderr_ready.return_value = False
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 0
        with patch.object(BUILD, "connect_ssh", return_value=client):
            self.assertEqual(
                BUILD.apply_remote(plan, self.values, plan["plan_sha256"], self.workspace), result
            )
        self.assertNotIn(b"no-debe-viajar", channel.sendall.call_args.args[0])
        self.assertTrue(channel.exec_command.call_args.args[0].startswith("sudo -n env "))
        channel.close.assert_called_once()
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
