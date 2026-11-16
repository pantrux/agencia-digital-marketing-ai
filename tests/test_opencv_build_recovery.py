"""Recuperación OpenCV por prueba host; fixtures sin SSH, Docker ni SDK."""

import ast
import base64
import contextlib
import copy
import importlib.util
import io
import json
import struct
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]


def module(name):
    path = BASE / "docker/kitsu-zou" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_test", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.m = module("opencv_build_recovery_remote")
        self.plan = {"plan_sha256": "a" * 64, "kit_sha256": "b" * 64}
        self.proof = {
            "kind": "opencv-inputs-host-signature-verification-v1",
            "success": True,
            "plan_sha256": "a" * 64,
            "kit_sha256": "b" * 64,
            "manifest_sha256": self.m.APPROVED_SHA,
            "packager_sha256": self.m.PACKAGER_SHA,
            "source_sha256": self.m.ORIGINAL_SHA,
            "approve_receipt_sha256": "c" * 64,
            "prepare_receipt_sha256": "d" * 64,
            "process": {"returncode": 0},
            "summary": self.m.HOST_SUMMARY,
            "container_mutated": False,
            "sdk_packages_changed": False,
            "sdk_executed": False,
        }
        self.history = {
            "approve": {"receipt_sha256": "c" * 64},
            "prepare": {"receipt_sha256": "d" * 64},
        }

    def test_receta_elimina_solo_rsa_del_sdk_y_conserva_cmake_y_packager(self):
        original = (BASE / "docker/kitsu-zou/build-opencv-minimal.sh").read_bytes()
        derived = self.m.derive_recipe(original)
        marker = b"# SDK ya preparado"
        self.assertEqual(original[original.index(marker) :], derived[derived.index(marker) :])
        self.assertIn(b"recipe_dir=/inputs/code/docker/kitsu-zou\n", derived)
        self.assertNotIn(b"--verify-inputs", derived)
        self.assertIn(self.m.HOST_PROOF_SHA.encode(), derived)
        self.assertIn(b"host-proof.json", derived)
        with self.assertRaises(ValueError):
            self.m.derive_recipe(original.replace(b"--parallel 2", b"--parallel 8"))

    def test_prueba_host_no_adopta_declaracion_sin_identidades_y_hash(self):
        self.m.validate_host_proof(self.proof, self.plan, self.history)
        for field, bad in [
            ("success", False),
            ("sdk_executed", True),
            ("manifest_sha256", "f" * 64),
            ("kit_sha256", "f" * 64),
            ("process", {"returncode": 1}),
            ("approve_receipt_sha256", "f" * 64),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.m.validate_host_proof({**self.proof, field: bad}, self.plan, self.history)
        bad = copy.deepcopy(self.proof)
        bad["summary"]["managed_apks"] = 69
        with self.assertRaises(ValueError):
            self.m.validate_host_proof(bad, self.plan, self.history)

    def test_recibo_de_etapa_equivocada_o_negativo_no_habilita_reinicio(self):
        receipt = self.m.envelope(
            {
                "stage": "verify-host",
                "success": True,
                "profile_sha256": "a" * 64,
                "owner": self.m.OWNER,
            }
        )
        self.m.require_stage("verify-host", receipt, receipt, "a" * 64)
        for edited in [
            self.m.envelope({**receipt["receipt"], "stage": "build"}),
            self.m.envelope({**receipt["receipt"], "success": False}),
            {**receipt, "receipt_sha256": "b" * 64},
        ]:
            with self.assertRaises(ValueError):
                self.m.require_stage("verify-host", edited, edited, "a" * 64)

    def test_parciales_y_enlaces_se_rechazan_sin_borrar(self):
        self.m.no_partials(["sdk-receipt.json", ".opencv-sdk-owner.json"])
        for name in ("build", "stage", "wheels", "home", "tmp"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.m.no_partials([name])

    def test_reinicio_exige_mismo_nacimiento_y_estado_detenido(self):
        origin = {
            "cid": self.m.CID,
            "created": "birth",
            "running": False,
            "pid": 0,
            "status": "exited",
        }
        self.m.stopped(origin, origin)
        for change in (
            {"cid": "f" * 64},
            {"created": "other"},
            {"running": True},
            {"pid": 123},
            {"status": "created"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.m.stopped({**origin, **change}, origin)

    def test_atestacion_no_admite_paquete_o_herramienta_distintos(self):
        packages = {f"p{i}": "1-r0" for i in range(89)}
        tools = [{"path": "/usr/bin/fixture" + str(i), "sha256": "a" * 64} for i in range(6)]
        expected = {"installed_packages": packages, "tools": tools}
        actual = {
            **expected,
            "database_sha256": "b" * 64,
            "network_interfaces": ["lo"],
            "read_only": True,
            "compiler_executed": False,
        }
        self.m.validate_attestation(actual, expected, "b" * 64)
        for changed in [
            {**actual, "installed_packages": {"other": "1"}},
            {**actual, "tools": tools[:-1]},
            {**actual, "database_sha256": "c" * 64},
            {**actual, "network_interfaces": ["lo", "eth0"]},
        ]:
            with self.assertRaises(ValueError):
                self.m.validate_attestation(changed, expected, "b" * 64)

    def test_codigo_de_atestacion_separa_registros_reales_y_no_escribe(self):
        tree = ast.parse(self.m.ATTEST)
        splitters = [
            n.args[0].value
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "split"
        ]
        self.assertIn("\n\n", splitters)
        self.assertFalse(
            any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"write_bytes", "write_text", "mkdir", "unlink"}
                for n in ast.walk(tree)
            )
        )

    def test_stop_lee_solo_prepare_create_aunque_sdk_o_build_sean_ilegibles(self):
        class Store:
            def receipt(self, stage):
                if stage not in {"prepare", "create"}:
                    raise AssertionError("No debe leer SDK/build")
                return {"fixture": stage}

        self.assertEqual(set(self.m.original_receipts(Store(), stop=True)), {"prepare", "create"})

    def test_reserva_cleanup_se_descuenta_del_deadline_total(self):
        self.assertEqual(self.m.build_timeout(1800, now=100), 1680)
        self.assertEqual(self.m.build_timeout(1800, now=0), 1780)
        with self.assertRaises(TimeoutError):
            self.m.build_timeout(1800, now=1781)

    def test_sca_caducada_o_sin_cobertura_no_habilita_recuperacion(self):
        value = {
            "verified": True,
            "sdk_receipt_sha256": "a" * 64,
            "high_critical": [],
            "source_identity_verified": True,
            "operating_system_verified": True,
            "coverage_complete": True,
            "source_identity_coverage": {"apk": 91, "numpy": 1},
            "db_metadata": {
                "UpdatedAt": "2026-11-06T12:55:00Z",
                "NextUpdate": "2026-11-07T12:55:00Z",
            },
        }
        now = datetime(2026, 11, 6, 18, tzinfo=UTC)
        self.m.validate_sca(value, "a" * 64, now)
        for changed in [
            {**value, "verified": False},
            {**value, "high_critical": [{"Severity": "HIGH"}]},
            {**value, "coverage_complete": False},
            {**value, "sdk_receipt_sha256": "b" * 64},
        ]:
            with self.assertRaises(ValueError):
                self.m.validate_sca(changed, "a" * 64, now)
        with self.assertRaises(ValueError):
            self.m.validate_sca(value, "a" * 64, datetime(2026, 11, 8, tzinfo=UTC))

    def test_timeout_de_exec_detiene_solo_cid_ya_acreditado(self):
        class Docker:
            def command(self, *_a, **_kw):
                raise TimeoutError("fixture")

        calls = []
        with self.assertRaises(TimeoutError):
            self.m.bounded_build(
                Docker(), ["exec", self.m.CID], object(), lambda: calls.append(self.m.CID)
            )
        self.assertEqual(calls, [self.m.CID])

    def test_stop_no_necesita_prueba_host_ni_sdk_ni_inputs(self):
        self.assertEqual(self.m.prerequisites("stop"), [])
        self.assertEqual(self.m.prerequisites("restart"), ["verify-host", "observe-stopped"])
        with self.assertRaises(ValueError):
            self.m.prerequisites("sdk")

    def test_default_plan_no_lee_boveda_y_fuentes_se_cargan_sin_pyc(self):
        local = module("recover_opencv_build")
        with (
            patch.object(local, "proposal", return_value={"plan_only": True}),
            patch.object(local, "read_values", side_effect=AssertionError("bóveda")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(local.main(["plan"]), 0)
        with patch("importlib.util.spec_from_file_location", side_effect=AssertionError("pyc")):
            loaded = local.module_bytes(
                "fixture", b"value=7\n", "fixture.py", local.digest(b"value=7\n")
            )
            self.assertEqual(loaded.value, 7)

    def test_respaldos_solo_permiten_un_recibo_por_etapa(self):
        local = module("recover_opencv_build")
        value = {"verify-host": {"receipt": {"stage": "verify-host"}}}
        encoded = base64.b64encode(json.dumps(value).encode()).decode()
        self.assertEqual(local.decode(encoded), value)
        with self.assertRaises(ValueError):
            local.decode(base64.b64encode(b'{"x":1,"x":2}').decode())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            local.write_new(path, b"first")
            with self.assertRaises(FileExistsError):
                local.write_new(path, b"other")
            self.assertEqual(path.read_bytes(), b"first")

    def test_framing_acotado_no_consume_el_latido_posterior(self):
        stream = io.BytesIO(struct.pack(">I", 3) + b"abc.")
        self.assertEqual(self.m.exact_frame(stream, 3), b"abc")
        self.assertEqual(stream.read(), b".")
        for raw in [
            struct.pack(">I", 4) + b"abcd",
            struct.pack(">I", 3) + b"ab",
            struct.pack(">I", 0),
            b"x",
        ]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.m.exact_frame(io.BytesIO(raw), 3)

    def test_collector_exige_booleano_coincidente_con_exit_y_json_sin_duplicados(self):
        local = module("recover_opencv_build")

        class Channel:
            def __init__(self, raw, status):
                self.raw, self.status = raw, status

            def sendall(self, raw):
                self.asserted_pulse = raw

            def recv_ready(self):
                return bool(self.raw)

            def recv(self, _size):
                value, self.raw = self.raw, b""
                return value

            def recv_stderr_ready(self):
                return False

            def recv_stderr(self, _size):
                return b""

            def exit_status_ready(self):
                return True

            def recv_exit_status(self):
                return self.status

        channel = Channel(b'{"success":true}', 0)
        self.assertTrue(local.collect(channel, float("inf"), 1024)["success"])
        self.assertEqual(channel.asserted_pulse, b".")
        for raw, status in [
            (b'{"success":false}', 0),
            (b'{"success":true}', 1),
            (b'{"success":1}', 0),
            (b'{"success":true,"success":false}', 1),
        ]:
            with self.subTest(raw=raw, status=status), self.assertRaises(ValueError):
                local.collect(Channel(raw, status), float("inf"), 1024)


if __name__ == "__main__":
    unittest.main()
