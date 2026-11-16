"""Contrato real NativeJob y reportes por FD; no ejecuta Trivy ni procesos."""

import hashlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "docker/postgres-immich/audit_final_security_v2.py"


def source_module(name, path):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(path.read_bytes(), str(path), "exec"), vars(module))
    return module


class BeforeNativeCreation(RuntimeError):
    pass


class Sentinel:
    def __init__(self):
        self.reached = False

    def create_job(self, limits):
        self.reached = True
        raise BeforeNativeCreation("Validador real atravesado; no se crea Job/proceso.")


class FinalSecurityV2Tests(unittest.TestCase):
    def setUp(self):
        self.m = source_module("pg_final_security_v2_tests", PATH)

    def test_real_native_job_validator_rejects_old_32mib_before_api(self):
        job = source_module("pg_final_v2_real_native_validator", ROOT / "sre/windows_build_job.py")
        sentinel = Sentinel()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / "not-executed.exe"
            executable.write_bytes(b"fixture")
            with self.assertRaisesRegex(ValueError, "Límites del Job"):
                job.run_job(
                    executable,
                    [],
                    cwd=root,
                    env={},
                    timeout=180,
                    limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                    output_limit=32 * 1024**2,
                    _api=sentinel,
                )
        self.assertFalse(sentinel.reached)

    def test_v2_adapter_passes_actual_validator_before_native_creation(self):
        path = ROOT / "sre/windows_build_job.py"
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44",
        )
        job = source_module("pg_final_v2_real_adapter_validator", path)
        sentinel = Sentinel()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable = root / "not-executed.exe"
            executable.write_bytes(b"fixture")
            with self.assertRaises(BeforeNativeCreation):
                self.m.run_scanner(job, executable, [], cwd=root, env={}, api=sentinel)
        self.assertTrue(sentinel.reached)

    def test_declaration_remains_v1_and_operator_is_explicit_v2_copy(self):
        declaration = {
            "owner": self.m.DECLARATION_OWNER,
            "run_root": self.m.DECLARATION_ROOT,
            "bounds": {"capture_bytes": 32 * 1024**2, "report_bytes": 32 * 1024**2},
        }
        operator = self.m.operator_manifest(declaration)
        self.assertEqual(declaration["bounds"]["capture_bytes"], 32 * 1024**2)
        self.assertEqual(declaration["owner"], self.m.DECLARATION_OWNER)
        self.assertEqual(operator["owner"], self.m.OPERATOR_OWNER)
        self.assertNotEqual(operator["run_root"], declaration["run_root"])
        self.assertEqual(operator["bounds"]["capture_bytes"], 1024**2)
        self.assertEqual(operator["bounds"]["report_bytes"], 16 * 1024**2)

    def test_report_reservation_is_exclusive_and_read_through_owned_fd(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            fd, identity = self.m.reserve_report(path)
            try:
                with self.assertRaises(FileExistsError):
                    self.m.reserve_report(path)
                path.write_bytes(b'{"Results":[]}')
                self.assertEqual(self.m.read_report(path, fd, identity), b'{"Results":[]}')
            finally:
                os.close(fd)

    def test_report_rejects_replacement_and_oversize_without_adoption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.json"
            fd, identity = self.m.reserve_report(path)
            try:
                path.write_bytes(b"x" * 10)
                with self.assertRaises(ValueError):
                    self.m.read_report(path, fd, identity, maximum=9)
                with self.assertRaises(ValueError):
                    self.m.read_report(path, fd, (identity[0], identity[1] + 1))
            finally:
                os.close(fd)

    def test_v1_core_and_manifest_are_still_exact_and_no_import_effects(self):
        for name, pin in [
            (
                "final_security.py",
                "43ac1bbeefc602e78291c7963018003b7e5cdf83bde2d3d6829b9815ff86a923",
            ),
            (
                "final-security-inputs-v1.json",
                "2bfd9ad19fd988d0b1c9accfdd4a0b2812cc018b366ce81975c76eadde8813d0",
            ),
        ]:
            self.assertEqual(hashlib.sha256(PATH.with_name(name).read_bytes()).hexdigest(), pin)
        self.assertEqual(
            self.m.CORE_SHA, "43ac1bbeefc602e78291c7963018003b7e5cdf83bde2d3d6829b9815ff86a923"
        )
        self.assertEqual(
            self.m.MANIFEST_SHA, "2bfd9ad19fd988d0b1c9accfdd4a0b2812cc018b366ce81975c76eadde8813d0"
        )

    def test_diagnostics_never_replace_the_separate_report(self):
        core = source_module("pg_final_v2_core_data", PATH.with_name("final_security.py"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = root / "os.trivy.json"
            report.write_bytes(b'{"Results":[]}')
            result = types.SimpleNamespace(
                status="completed",
                exit_code=0,
                stdout=b"diagnostico",
                stderr=b"",
                elapsed_seconds=0.1,
                cleanup_complete=True,
                error_operation=None,
                error_code=None,
            )
            self.m.save_job(core, root, "os", result)
            self.assertEqual(report.read_bytes(), b'{"Results":[]}')
            self.assertEqual((root / "os.stdout.log").read_bytes(), b"diagnostico")


if __name__ == "__main__":
    unittest.main()
