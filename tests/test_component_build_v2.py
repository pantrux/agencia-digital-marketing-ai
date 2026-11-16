"""Constructor V2 cerrado: planes y fronteras, sin SSH ni Docker reales."""

import base64
import copy
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "docker" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load("builder_v2_tests", "build_component_v2.py")
REMOTE = BUILD.REMOTE


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


class BuildV2Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        self.source = self.workspace / "docker/r2-sync-consumer"
        self.source.mkdir(parents=True)
        self.files = {
            name: (ROOT / "docker/r2-sync-consumer" / name).read_bytes()
            for name in REMOTE.FILES["r2-sync-consumer"]
        }
        for name, raw in self.files.items():
            (self.source / name).write_bytes(raw)
        layout = {
            "success": True,
            "owner": REMOTE.LAYOUT_OWNER,
            "targets": [
                {
                    "path": path,
                    "role": role,
                    "directory": {"device": 1, "inode": number},
                    "marker_sha256": "2" * 64,
                    "receipt_sha256": "3" * 64,
                }
                for number, (path, role) in enumerate(REMOTE.ROOTS.items(), 1)
            ],
        }
        self.entry = {
            "build_id": "fase4-r2-control-01",
            "tag": "media-platform/r2-sync-consumer:fase4-r2-control-01",
            "base": REMOTE.BASE,
            "context_root": REMOTE.CONTEXT_ROOT,
            "logs_root": REMOTE.LOGS_ROOT,
            "files": {name: sha(raw) for name, raw in self.files.items()},
        }
        self.spec = {
            "schema_version": 2,
            "layout_helper_sha256": sha((ROOT / "docker/host_layout.py").read_bytes()),
            "components": {"r2-sync-consumer": self.entry},
        }
        self.values = {
            "MEDIA_VM_IP": "192.168.0.85",
            "MEDIA_VM_SSH_USER": "jandrade",
            "MEDIA_VM_SSH_HOST_FINGERPRINT": "SHA256:"
            + base64.b64encode(hashlib.sha256(b"host-publico-fixture").digest())
            .decode()
            .rstrip("="),
            "MEDIA_PLATFORM_INSTALL_ROOT": "/opt/media-platform",
            "MEDIA_PLATFORM_DATA_ROOT": "/var/lib/media-platform",
            "MEDIA_PLATFORM_OWNER_MARKER": REMOTE.LAYOUT_OWNER,
            "MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64": encoded(layout),
            "MEDIA_PLATFORM_BUILDER_V2_SOURCE_SHA256": sha(
                (ROOT / "docker/build_component_v2.py").read_bytes()
            ),
            "MEDIA_PLATFORM_BUILDER_V2_HELPER_SHA256": sha(
                (ROOT / "docker/component_build_remote_v2.py").read_bytes()
            ),
        }

    def plan(self):
        self.values["MEDIA_PLATFORM_COMPONENT_BUILDS_V2_B64"] = encoded(self.spec)
        return BUILD.prepare_plan(self.values, "r2-sync-consumer", self.workspace)

    def payload(self):
        plan = self.plan()
        self.values["MEDIA_PLATFORM_BUILD_V2_APPROVED_PLAN_SHA256"] = plan["plan_sha256"]
        return BUILD.make_payload(plan, self.values, self.workspace)

    def test_plan_v2_cerrado_estable_y_manifest_sin_autorreferencia(self):
        plan = self.plan()
        self.assertEqual(plan, self.plan())
        self.assertEqual(len(plan["files"]), 7)
        sources = {name: sha(raw) for name, raw in self.files.items() if name != "Dockerfile"}
        self.assertEqual(
            plan["source_manifest"],
            {"schema_version": 1, "files": sources, "root_sha256": sha(REMOTE.canonical(sources))},
        )
        self.assertEqual(plan["plan_sha256"], REMOTE.plan_hash(plan))
        self.assertNotIn("Dockerfile", plan["source_manifest"]["files"])
        self.assertNotIn("PRIVATE_KEY", json.dumps(plan))

    def test_v1_no_da_autoridad_a_v2_y_plan_no_conecta(self):
        values = dict(self.values)
        values["MEDIA_PLATFORM_COMPONENT_BUILDS_B64"] = encoded(self.spec)
        with self.assertRaises((KeyError, ValueError)):
            BUILD.prepare_plan(values, "r2-sync-consumer", self.workspace)
        with (
            patch.object(BUILD, "read_values", return_value=self.values),
            patch.object(BUILD, "prepare_plan", return_value=self.plan()),
            patch.object(BUILD, "connect_ssh") as ssh,
            patch("sys.argv", ["build_component_v2.py", "r2-sync-consumer"]),
            patch("builtins.print"),
        ):
            self.assertEqual(BUILD.main(), 0)
        ssh.assert_not_called()

    def test_otros_roles_bases_tags_rutas_y_config_extras_rechazados(self):
        for component in ("immich-ml-auth", "postgres-apps", "../r2-sync-consumer"):
            with self.assertRaises(ValueError):
                BUILD.prepare_plan(self.values, component, self.workspace)
        for field, wrong in (
            ("base", "python:latest"),
            ("base", REMOTE.BASE.replace("213242", "013242")),
            ("context_root", "/opt/media-platform/build/contexts"),
            ("logs_root", "/var/lib/media-platform/build"),
            ("tag", "ajeno/app:latest"),
            ("build_id", "../escape"),
        ):
            original = self.entry[field]
            self.entry[field] = wrong
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.plan()
            self.entry[field] = original
        self.spec["token"] = "no-transferible"
        with self.assertRaises(ValueError):
            self.plan()

    def test_fuente_helper_controller_o_contexto_alterado_bloquea(self):
        for variable in (
            "MEDIA_PLATFORM_BUILDER_V2_SOURCE_SHA256",
            "MEDIA_PLATFORM_BUILDER_V2_HELPER_SHA256",
        ):
            original = self.values[variable]
            self.values[variable] = "0" * 64
            with self.subTest(variable=variable), self.assertRaises(ValueError):
                self.plan()
            self.values[variable] = original
        (self.source / "sync_http.py").write_bytes(b"alterado")
        with self.assertRaises(ValueError):
            self.plan()

    def test_contexto_excesivo_y_links_bloquean_antes_de_transferir(self):
        name = "sync_http.py"
        raw = b"x" * REMOTE.MAX_CONTEXT
        (self.source / name).write_bytes(raw)
        self.entry["files"][name] = sha(raw)
        with self.assertRaises(ValueError):
            self.plan()
        (self.source / name).write_bytes(self.files[name])
        self.entry["files"][name] = sha(self.files[name])
        os.link(self.source / name, self.source / "alias.py")
        with self.assertRaises(ValueError):
            self.plan()

    def test_payload_no_incluye_extras_y_rechaza_tampering(self):
        (self.source / "README.md").write_text("documentación")
        (self.source / "cache").mkdir()
        self.values["SECRETO_AJENO"] = "no-transferible"
        payload = self.payload()
        REMOTE.validate_payload(payload)
        self.assertEqual(set(payload["files"]), set(self.files))
        self.assertNotIn("no-transferible", json.dumps(payload))
        for name in self.files:
            wrong = copy.deepcopy(payload)
            wrong["files"][name] = encoded({"ajeno": True})
            with self.subTest(name=name), self.assertRaises(ValueError):
                REMOTE.validate_payload(wrong)

    def test_dockerfile_aprobado_no_puede_ejecutar_run_add_remote_o_frontend(self):
        payload = self.payload()
        for extra in (
            b"RUN git status\n",
            b"ADD https://example.invalid/a /a\n",
            b"COPY --from=remoto /a /a\n",
            b"# syntax=docker/dockerfile:latest\n",
        ):
            wrong = copy.deepcopy(payload)
            raw = self.files["Dockerfile"] + extra
            wrong["files"]["Dockerfile"] = base64.b64encode(raw).decode()
            wrong["plan"]["files"]["Dockerfile"] = {"sha256": sha(raw), "size": len(raw)}
            wrong["plan"]["plan_sha256"] = REMOTE.plan_hash(wrong["plan"])
            wrong["approved_plan_sha256"] = wrong["plan"]["plan_sha256"]
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                REMOTE.validate_payload(wrong)

    def test_apply_hash_y_fuentes_revalidan_antes_de_ssh(self):
        plan = self.plan()
        with patch.object(BUILD, "connect_ssh") as ssh:
            with self.assertRaises(ValueError):
                BUILD.apply_remote(plan, self.values, plan["plan_sha256"], self.workspace)
            self.values["MEDIA_PLATFORM_BUILD_V2_APPROVED_PLAN_SHA256"] = plan["plan_sha256"]
            (self.source / "sync_runtime.py").write_bytes(b"cambio posterior")
            with self.assertRaises(ValueError):
                BUILD.apply_remote(plan, self.values, plan["plan_sha256"], self.workspace)
        ssh.assert_not_called()

    def test_nogit_despues_de_sudo_entorno_nuevo_y_build_local(self):
        command = BUILD.remote_command(b"# fuente publica")
        self.assertTrue(command.startswith("sudo -n env BUILDX_GIT_INFO=false "))
        environment = REMOTE.build_environment(self.plan()["record_path"])
        self.assertEqual(
            set(environment), {"PATH", *REMOTE.NO_GIT, "DOCKER_CONFIG", "BUILDX_CONFIG"}
        )
        argv = REMOTE.build_command(self.plan())
        for flag in (
            "--builder=default",
            "--network=none",
            "--provenance=false",
            "--load",
            "--platform=linux/amd64",
        ):
            self.assertIn(flag, argv)
        self.assertFalse(set(argv) & {"run", "bake", "exec", "push"})

    def test_manifest_modificado_o_ruta_extra_no_se_autentica_solo_por_rehash(self):
        for change in (
            {"source_manifest": {}},
            {"extra": "no"},
            {"context_path": "/opt/ajeno"},
            {"target": {"ip": "192.168.0.7"}},
        ):
            plan = self.plan()
            plan.update(change)
            plan["plan_sha256"] = REMOTE.plan_hash(plan)
            with self.subTest(change=change), self.assertRaises(ValueError):
                REMOTE.validate_plan(plan)

    def test_timeout_cierra_grupo_propio_aunque_lider_salio(self):
        process = MagicMock()
        process.pid = 44551
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.get_map.return_value = {"pipe": True}
        selector.select.side_effect = TimeoutError("fixture")
        with (
            patch.object(REMOTE.subprocess, "Popen", return_value=process),
            patch.object(REMOTE.selectors, "DefaultSelector", return_value=selector),
            patch.object(REMOTE.os, "killpg", create=True) as kill,
            patch.object(REMOTE.signal, "SIGKILL", 9, create=True),
            patch.object(REMOTE.time, "sleep"),
        ):
            with self.assertRaises(TimeoutError):
                REMOTE.DockerRunner(self.plan()).build(["fixture-no-ejecutable"], MagicMock())
        self.assertEqual(kill.call_args_list[0].args, (44551, REMOTE.signal.SIGTERM))
        self.assertEqual(kill.call_args_list[-1].args, (44551, 9))
        process.poll.assert_not_called()
        process.wait.assert_called()

    def execution(self):
        payload = self.payload()
        image = "sha256:" + "9" * 64

        class Store:
            def __init__(self):
                self.receipt = None
                self.prepared = None

            def existing(self):
                return self.receipt

            def prepare(self, files):
                self.prepared = files

            def log_build(self, runner, command):
                runner.ids = [image]
                return runner.exit_code

            def image_id(self):
                return image

            def complete(self, result):
                self.receipt = result
                return result

        class Runner:
            def __init__(self):
                self.ids = []
                self.exit_code = 0

            def verify_versions(self):
                pass

            def tag_ids(self, _tag):
                return self.ids

        return payload, Store(), Runner(), image

    def test_recibo_imagen_idempotente_no_afirma_contenedor_ni_sca(self):
        payload, store, runner, image = self.execution()
        result = REMOTE.run_apply(payload, store, runner)
        self.assertEqual(result["image_id"], image)
        self.assertFalse(result["containers_created"])
        self.assertFalse(result["final_sca_verified"])
        self.assertEqual(result["source_manifest"], payload["plan"]["source_manifest"])
        self.assertEqual(store.prepared, self.files)
        self.assertTrue(result["changed"])
        self.assertFalse(REMOTE.run_apply(payload, store, runner)["changed"])
        runner.ids = ["sha256:" + "8" * 64]
        with self.assertRaises(ValueError):
            REMOTE.run_apply(payload, store, runner)

    def test_tag_ajeno_y_build_fallido_no_generan_recibo(self):
        for existing, exit_code in ((True, 0), (False, 1)):
            payload, store, runner, _image = self.execution()
            runner.ids = ["sha256:" + "8" * 64] if existing else []
            runner.exit_code = exit_code
            with self.subTest(existing=existing), self.assertRaises(ValueError):
                REMOTE.run_apply(payload, store, runner)
            self.assertIsNone(store.receipt)

    def test_sdk_fijo_y_builder_integrado_sin_bootstrap(self):
        runner = REMOTE.DockerRunner(self.plan())
        responses = [
            "29.9.3",
            "github.com/docker/buildx v0.38.1 prueba",
            "Name: default\nDriver: docker\n",
        ]
        with patch.object(runner, "capture", side_effect=responses) as capture:
            runner.verify_versions()
        self.assertEqual(capture.call_args_list[-1].args, (["buildx", "inspect", "default"],))
        for responses in (
            ["otro"],
            ["29.9.3", "v0.38.0"],
            ["29.9.3", "v0.38.1", "Driver: docker-container\n"],
        ):
            with (
                patch.object(runner, "capture", side_effect=responses),
                self.assertRaises(ValueError),
            ):
                runner.verify_versions()

    def test_transporte_solo_payload_publico_y_recibo_correlacionado(self):
        plan = self.plan()
        self.values["MEDIA_PLATFORM_BUILD_V2_APPROVED_PLAN_SHA256"] = plan["plan_sha256"]
        self.values["CREDENCIAL_AJENA"] = "no-transferible"
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
        sent = json.loads(channel.sendall.call_args.args[0])
        self.assertEqual(set(sent["files"]), set(self.files))
        self.assertNotIn("no-transferible", json.dumps(sent))
        channel.close.assert_called_once()
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
