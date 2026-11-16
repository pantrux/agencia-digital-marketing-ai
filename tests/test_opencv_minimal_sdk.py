"""Contrato del SDK OpenCV Zou: sin SSH, Docker, compiladores ni código descargado."""

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REMOTE = load("opencv_sdk_contract_tests", "docker/kitsu-zou/opencv_minimal_sdk_remote.py")
LOCAL = load("opencv_sdk_local_tests", "docker/kitsu-zou/run_opencv_minimal_sdk.py")


def profile():
    value = {
        "schema_version": 1,
        "owner": "agencia-digital-marketing-ai:opencv-zou-minimal-sdk:v1",
        "build_id": "a" * 32,
        "kit_sha256": "a" * 64,
        "base": {
            "reference": "cgr.dev/chainguard/wolfi-base@sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72",
            "manifest": "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72",
            "config": "sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7",
        },
        "limits": {
            "cpu": 2,
            "memory_bytes": 6 * 1024**3,
            "pids": 128,
            "stage_seconds": {"sdk": 600, "build": 1800},
            "capture_bytes": 4 * 1024**2,
        },
        "target": {"hostname": "vm-media-01", "ip": "192.168.0.85", "port": 22, "user": "jandrade"},
    }
    value["paths"] = REMOTE.derive_paths(value["build_id"])
    return value


def observed(p, *, cid="b" * 64, running=True):
    mounts = REMOTE.mount_contract(p)
    return {
        "Id": cid,
        "Image": p["base"]["manifest"],
        "Name": "/agency-opencv-zou-" + p["build_id"],
        "Config": {
            "Image": p["base"]["reference"],
            "User": "0:0",
            "Labels": REMOTE.labels(p),
            "Env": REMOTE.container_environment(p),
            "Entrypoint": ["/bin/sh"],
            "Cmd": REMOTE.hold_arguments(),
            "Volumes": None,
        },
        "HostConfig": {
            "NanoCpus": 2_000_000_000,
            "Memory": 6 * 1024**3,
            "MemorySwap": 6 * 1024**3,
            "PidsLimit": 128,
            "NetworkMode": "none",
            "Privileged": False,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "ReadonlyRootfs": False,
            "PortBindings": {},
            "Devices": [],
            "DeviceRequests": [],
            "Binds": None,
            "Mounts": copy.deepcopy(mounts),
            "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=67108864,mode=1777"},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": row["Source"],
                "Destination": row["Target"],
                "RW": not row.get("ReadOnly", False),
                "Propagation": "rprivate",
            }
            for row in mounts
        ],
        "State": {
            "Running": running,
            "Status": "running" if running else "exited",
            "Pid": 123 if running else 0,
            "StartedAt": "2026-11-06T12:00:00Z",
        },
    }


def stage_receipt(stage, *, success=True):
    return REMOTE.envelope(
        {
            "stage": stage,
            "success": success,
            "plan_sha256": "d" * 64,
            "source_sha256": "e" * 64,
            "cid": "b" * 64,
        }
    )


class ContainerContractTests(unittest.TestCase):
    def test_mounts_omit_readonly_false_pero_no_admiten_destinos_repetidos(self):
        p = profile()
        current = observed(p)
        for row in current["HostConfig"]["Mounts"]:
            if not row["ReadOnly"]:
                row.pop("ReadOnly")
        self.assertEqual(REMOTE.check_container(current, "b" * 64, p)["cid"], "b" * 64)
        current["Mounts"].append(copy.deepcopy(current["Mounts"][0]))
        with self.assertRaises(ValueError):
            REMOTE.check_container(current, "b" * 64, p)

    def test_noforeign_imagen_cid_usuario_montajes_puertos_cap_y_limites(self):
        p = profile()
        for kind in [
            "image",
            "cid",
            "user",
            "mount",
            "port",
            "cap",
            "memory",
            "label",
            "environment",
        ]:
            value = observed(p)
            if kind == "image":
                value["Image"] = "sha256:" + "f" * 64
            elif kind == "cid":
                value["Id"] = "c" * 64
            elif kind == "user":
                value["Config"]["User"] = "1000:1000"
            elif kind == "mount":
                value["Mounts"][0]["Source"] = "/foreign"
            elif kind == "port":
                value["HostConfig"]["PortBindings"] = {"8000/tcp": [{"HostPort": "8000"}]}
            elif kind == "cap":
                value["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
            elif kind == "memory":
                value["HostConfig"]["Memory"] += 1
            elif kind == "label":
                value["Config"]["Labels"]["agencia.owner"] = "foreign"
            else:
                value["Config"]["Env"].append("UNEXPECTED=1")
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                REMOTE.check_container(value, "b" * 64, p)

    def test_creacion_sin_pull_red_git_puertos_ni_volume_anonimo(self):
        p = profile()
        arguments = REMOTE.create_arguments(p)
        self.assertIn("--pull=never", arguments)
        self.assertIn("--network=none", arguments)
        self.assertIn("--cap-drop=ALL", arguments)
        self.assertIn("--memory-swap=" + str(6 * 1024**3), arguments)
        self.assertNotIn("--publish", arguments)
        self.assertNotIn("--volume", arguments)
        self.assertNotIn("git", arguments)

    def test_stop_solo_cid_acreditado_sin_abrir_inputs_sdk_o_nfs(self):
        p = profile()

        class Docker:
            def __init__(self):
                self.commands = []
                self.running = True

            def inspect(self, cid, timeout=2):
                return observed(p, cid=cid, running=self.running)

            def command(self, arguments, timeout, sink=None):
                self.commands.append(arguments)
                self.running = False
                return {"returncode": 0, "output": b""}

        docker = Docker()
        result = REMOTE.stop_owned(docker, "b" * 64, p)
        self.assertTrue(result["stopped"])
        self.assertEqual(docker.commands, [["stop", "--time", "5", "b" * 64]])

    def test_plan_no_permite_host_root_o_id_elegible(self):
        p = profile()
        REMOTE.validate_profile(p)
        for kind in ["host", "path", "id", "limit"]:
            changed = copy.deepcopy(p)
            if kind == "host":
                changed["target"]["ip"] = "192.168.0.7"
            elif kind == "path":
                changed["paths"]["control"] = "/foreign"
            elif kind == "id":
                changed["build_id"] = "../escape"
            else:
                changed["limits"]["stage_seconds"]["build"] = 1801
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                REMOTE.validate_profile(changed)


class PersistenceTests(unittest.TestCase):
    def test_create_devuelve_cid_aunque_falle_inspeccion_y_lo_respalda_antes_de_error(self):
        p = profile()
        p["plan_sha256"] = "d" * 64
        p["remote_sha256"] = hashlib.sha256(
            (ROOT / "docker/kitsu-zou/opencv_minimal_sdk_remote.py").read_bytes()
        ).hexdigest()
        channel = Mock()
        client = Mock()
        client.get_transport.return_value.open_session.return_value = channel
        writes = []
        operator = LOCAL.Controller(
            {}, p, writer=lambda values: writes.append(values), client=client
        )
        with (
            patch.object(LOCAL, "verify_local", return_value=ROOT),
            patch.object(
                LOCAL,
                "collect",
                return_value={
                    "success": False,
                    "created_cid": "b" * 64,
                    "error_type": "ValueError",
                },
            ),
        ):
            with self.assertRaises(ValueError):
                operator.perform("create")
        self.assertEqual(len(writes), 1)
        persisted = REMOTE.json_object(base64.b64decode(writes[0][LOCAL.KEYS["container"]]))
        self.assertEqual(persisted["create_unverified"]["cid"], "b" * 64)
        self.assertIs(persisted["create_unverified"]["verified"], False)
        channel.close.assert_called_once()

    def test_bootstrap_no_lleva_fuente_gigante_en_argv_y_rechaza_modo_inyectado(self):
        command = LOCAL.remote_command("a" * 64, "sdk")
        self.assertLess(len(command), 8192)
        self.assertIn("/usr/bin/python3 -I -B", command)
        self.assertIn("sudo -n /usr/bin/env -i", command)
        with self.assertRaises(ValueError):
            LOCAL.remote_command("a" * 64, "sdk; injected")

    def test_cli_sin_flag_raiz_no_lee_boveda_ni_abre_ssh(self):
        with (
            patch.object(LOCAL, "proposal", return_value=profile()),
            patch.object(LOCAL, "read_values") as vault,
            patch.object(LOCAL, "connect") as ssh,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(LOCAL.main(["sdk"]), 1)
            vault.assert_not_called()
            ssh.assert_not_called()

    def test_stop_cli_no_revalida_archivos_sdk_locales_ausentes(self):
        p = profile()
        p["bound_to_vault"] = True
        p["vault_helper_sha256"] = "c" * 64
        writer = Mock()
        client = Mock()
        operator = Mock()
        operator.perform.return_value = {"success": True}
        with (
            patch.object(LOCAL, "proposal", return_value=p),
            patch.object(LOCAL, "verify_local", side_effect=OSError("SDK unavailable")) as sdk,
            patch.object(LOCAL, "read_values", return_value={LOCAL.KEYS["plan"]: "saved"}),
            patch.object(LOCAL, "bind_plan", return_value=p),
            patch.object(LOCAL, "load", return_value=SimpleNamespace(update=writer)),
            patch.object(LOCAL, "decode", return_value=p),
            patch.object(LOCAL, "connect", return_value=client),
            patch.object(LOCAL, "Controller", return_value=operator),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(LOCAL.main(["stop", "--root-update-vault"]), 0)
            sdk.assert_not_called()
            operator.perform.assert_called_once_with("stop")

    def test_no_avanza_sin_recibo_anterior_respaldado(self):
        actual = {stage: stage_receipt(stage) for stage in ("prepare", "create")}
        REMOTE.require_backup("start", actual, copy.deepcopy(actual))
        for changed in [
            {},
            {"prepare": actual["prepare"]},
            {**actual, "create": {"sha256": "c" * 64}},
        ]:
            with self.assertRaises(ValueError):
                REMOTE.require_backup("start", actual, changed)

    def test_prerequisito_fallido_o_envelope_incorrecto_no_habilita_build(self):
        baseline = {
            stage: stage_receipt(stage)
            for stage in ("prepare", "create", "start", "sdk", "approve")
        }
        REMOTE.require_backup("build", baseline, baseline)
        for kind in ("failed", "truthy", "wrong-stage", "wrong-hash", "extra-key"):
            actual = copy.deepcopy(baseline)
            if kind in ("failed", "truthy"):
                actual["sdk"] = stage_receipt("sdk", success=False if kind == "failed" else 1)
            elif kind == "wrong-stage":
                actual["sdk"] = stage_receipt("start")
            elif kind == "wrong-hash":
                actual["sdk"]["receipt_sha256"] = "0" * 64
            else:
                actual["sdk"]["extra"] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                REMOTE.require_backup("build", actual, actual)

    def test_cleanup_cid_no_verificado_exige_intent_y_prepare_positivos(self):
        plan = profile()
        plan["plan_sha256"] = "d" * 64
        actual = {"prepare": stage_receipt("prepare"), "sdk": stage_receipt("sdk", success=False)}
        intent = {"cid": "b" * 64, "plan_sha256": "d" * 64}
        backed = {**actual, "create_unverified": {**intent, "verified": False}}
        self.assertEqual(REMOTE.cleanup_cid(actual, backed, plan, intent), "b" * 64)
        for change in ("cid", "prepared", "verified", "missing-verified-create"):
            changed = copy.deepcopy(backed)
            if change == "cid":
                changed["create_unverified"]["cid"] = "c" * 64
            elif change == "prepared":
                changed["prepare"] = stage_receipt("prepare", success=False)
            elif change == "verified":
                changed["create_unverified"]["verified"] = True
            else:
                changed["create"] = stage_receipt("create")
            with self.subTest(change=change), self.assertRaises(ValueError):
                REMOTE.cleanup_cid(actual, changed, plan, intent)

    def test_stop_con_create_positivo_ignora_fallo_sdk(self):
        actual = {stage: stage_receipt(stage) for stage in ("prepare", "create")}
        actual["sdk"] = stage_receipt("sdk", success=False)
        self.assertEqual(REMOTE.cleanup_cid(actual, actual, {"plan_sha256": "d" * 64}), "b" * 64)

    def test_materializacion_segura_no_adopta_archivos_o_enlaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "file"
            path.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                LOCAL.write_new(path, b"changed")
            self.assertEqual(path.read_bytes(), b"existing")

    def test_lector_boveda_8mib_admite_mas_de_2_y_rechaza_exceso(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.txt"
            raw = b"#" + b"x" * (2 * 1024**2 + 1) + b"\nNAME=VALUE\n"
            path.write_bytes(raw)
            self.assertEqual(LOCAL.read_vault_bytes(path), raw)
            path.write_bytes(b"x" * (8 * 1024**2 + 1))
            with self.assertRaises(ValueError):
                LOCAL.read_vault_bytes(path)

    def test_json_duplicado_nan_y_inventario_cambiado_fallan(self):
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}']:
            with self.assertRaises(ValueError):
                REMOTE.json_object(raw)
        files = {"data/example": {"sha256": "a" * 64, "size": 3}}
        checksum = REMOTE.inventory_digest(files)
        REMOTE.validate_inventory(files, checksum)
        with self.assertRaises(ValueError):
            REMOTE.validate_inventory(files, "b" * 64)


class RecipeTests(unittest.TestCase):
    def test_wrapper_build_fija_umask_antes_de_exec_con_argv_constante(self):
        args = REMOTE.stage_arguments("build", profile(), "b" * 64, "c" * 64)
        self.assertEqual(
            args[:6],
            [
                "exec",
                "b" * 64,
                "/usr/bin/bash",
                "-c",
                'umask 077; exec /usr/bin/bash "$@"',
                "opencv-private-build",
            ],
        )
        self.assertEqual(
            args[6:9],
            [
                "/inputs/code/docker/kitsu-zou/build-opencv-minimal.sh",
                "--execute",
                "/inputs/data/manifest-approved.json",
            ],
        )

    def test_limite_de_log_es_combinado_entre_comandos_sdk(self):
        target = io.BytesIO()
        sink = REMOTE.LimitedSink(target)
        sink.write(b"x" * (3 * 1024**2))
        with self.assertRaises(ValueError):
            sink.write(b"y" * (2 * 1024**2))
        self.assertEqual(len(target.getvalue()), 3 * 1024**2)

    def test_sca_sdk_incompleta_high_o_db_vencida_no_habilita_ejecucion(self):
        p = profile()
        p["input_sca"] = {
            "apk_coverage": {"expected": 91, "reported": 91, "complete": True},
            "numpy_header_coverage": [["numpy", "2.4.6"]],
            "high_critical": [],
            "report_sha256": "a" * 64,
            "db_metadata": {"NextUpdate": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }
        REMOTE.validate_input_sca(p)
        for kind in ["coverage", "high", "expired"]:
            changed = copy.deepcopy(p)
            if kind == "coverage":
                changed["input_sca"]["apk_coverage"]["reported"] = 90
            elif kind == "high":
                changed["input_sca"]["high_critical"] = [{"Severity": "HIGH"}]
            else:
                changed["input_sca"]["db_metadata"]["NextUpdate"] = "2000-01-01T00:00:00Z"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                REMOTE.validate_input_sca(changed)

    def test_siempre_receta_bash_congelada_no_traduccion_de_arrays(self):
        p = profile()
        arguments = REMOTE.stage_arguments("build", p, "b" * 64, "c" * 64)
        self.assertIn("/usr/bin/bash", arguments)
        self.assertIn("/inputs/code/docker/kitsu-zou/build-opencv-minimal.sh", arguments)
        self.assertIn("--execute", arguments)
        self.assertNotIn("pip", arguments)
        self.assertNotIn("curl", arguments)

    def test_apk_sdk_argv_archivos_exactos_no_scripts_no_repo_ni_deps_omitidas(self):
        rows = [{"path": "apks/a.apk"}, {"path": "apks/bash.apk"}]
        arguments = REMOTE.apk_arguments(rows)
        self.assertIn("--no-network", arguments)
        self.assertIn("--no-scripts", arguments)
        self.assertIn("--allow-untrusted", arguments)
        self.assertIn("/dev/null", arguments)
        self.assertNotIn("--no-deps", arguments)
        self.assertEqual(arguments[-2:], ["/inputs/data/apks/a.apk", "/inputs/data/apks/bash.apk"])


if __name__ == "__main__":
    unittest.main()
