"""Pruebas de reanudación: estados, respaldo previo y bytes, sin SDK ni red."""

import importlib.util
import io
import json
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "docker/postgres-immich"


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResumeGuards(unittest.TestCase):
    def setUp(self):
        self.remote = load("resume_sdk_remote")
        self.previous = {
            "cid": self.remote.CID,
            "created": "2026-11-06T10:56:21Z",
            "pid": 29557,
            "started_at": "2026-11-06T10:57:01Z",
        }
        self.value = {
            "Id": self.remote.CID,
            "Created": self.previous["created"],
            "State": {
                "Status": "exited",
                "Running": False,
                "Pid": 0,
                "StartedAt": self.previous["started_at"],
                "FinishedAt": "2026-11-06T12:00:00Z",
                "OOMKilled": False,
                "Dead": False,
                "Paused": False,
                "Restarting": False,
                "ExitCode": 0,
            },
        }

    def test_solo_instancia_propia_detenida_tras_fallo(self):
        self.remote.require_stopped(self.value, self.previous)
        for key, value in (
            ("Pid", 123),
            ("Running", True),
            ("OOMKilled", True),
            ("Status", "created"),
            ("StartedAt", "otra"),
        ):
            bad = {**self.value, "State": {**self.value["State"], key: value}}
            with self.assertRaises(ValueError):
                self.remote.require_stopped(bad, self.previous)
        with self.assertRaises(ValueError):
            self.remote.require_stopped({**self.value, "Id": "a" * 64}, self.previous)

    def test_restart_exige_nueva_instancia_sin_prohibir_pid_reciclado(self):
        current = {**self.previous, "running": True, "started_at": "2026-11-06T12:01:00Z"}
        self.remote.require_restarted(current, self.previous)
        with self.assertRaises(ValueError):
            self.remote.require_restarted(
                {**current, "started_at": self.previous["started_at"]}, self.previous
            )
        with self.assertRaises(ValueError):
            self.remote.require_restarted({**current, "pid": 0}, self.previous)

    def test_tamper_y_archivos_adicionales_no_se_adoptan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "lib").mkdir()
            (root / "lib/a").write_bytes(b"correcto")
            expected = {"lib/a": self.remote.digest(b"correcto")}
            self.remote.verify_tree(root, expected)
            (root / "lib/a").write_bytes(b"cambiado")
            with self.assertRaises(ValueError):
                self.remote.verify_tree(root, expected)
            (root / "lib/a").write_bytes(b"correcto")
            (root / "ajeno").write_bytes(b"nuevo")
            with self.assertRaises(ValueError):
                self.remote.verify_tree(root, expected)

    def test_tar_solo_datos_rechaza_enlaces_y_traversal(self):
        for name, kind in (
            ("wrapper/../salida", tarfile.REGTYPE),
            ("wrapper/lib/a", tarfile.SYMTYPE),
        ):
            memory = io.BytesIO()
            with tarfile.open(fileobj=memory, mode="w") as archive:
                row = tarfile.TarInfo(name)
                row.type = kind
                row.linkname = "/ajeno"
                archive.addfile(row)
            memory.seek(0)
            with tarfile.open(fileobj=memory) as archive, self.assertRaises(ValueError):
                self.remote.tar_files(archive, "wrapper/")

    def test_perfil_restart_sin_stopped_respaldado_no_abre_ssh(self):
        local = load("resume_sdk")
        with (
            mock.patch.object(local, "build_profile", return_value={}),
            mock.patch.object(local, "connect") as connect,
            mock.patch("sys.stdout", new=io.StringIO()) as output,
        ):
            self.assertEqual(local.main(["restart"]), 1)
        connect.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["success"])

    def test_cli_autorizado_aun_exige_stopped_antes_de_conectar(self):
        local = load("resume_sdk")
        sdk = types.SimpleNamespace(
            read_source=lambda _path: b"{}", read_values=lambda: {}, decode=lambda _value: {}
        )
        with (
            mock.patch.object(local, "sdk", return_value=sdk),
            mock.patch.object(local, "verify_pins"),
            mock.patch.object(local, "connect") as connect,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            result = local.main(
                [
                    "restart",
                    "--root-update-vault",
                    "--profile",
                    "controlado.json",
                    "--expected-profile-sha256",
                    "a" * 64,
                ]
            )
        self.assertEqual(result, 1)
        connect.assert_not_called()

    def test_frame_fragmentado_no_trunca_la_peticion(self):
        class Fragmented:
            data = b"abcd"

            def read(self, _size):
                result, self.data = self.data[:1], self.data[1:]
                return result

        self.assertEqual(self.remote.exact_read(Fragmented(), 4), b"abcd")
        with self.assertRaises(ValueError):
            self.remote.exact_read(Fragmented(), 5)


if __name__ == "__main__":
    unittest.main()
