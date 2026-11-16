"""Pruebas de reanudación: estados, respaldo previo y bytes, sin SDK ni red."""

import copy
import importlib.util
import io
import json
import os
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
        self.remote = load("augment_sdk_remote")
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
        local = load("augment_sdk")
        with (
            mock.patch.object(local, "build_profile", return_value={}),
            mock.patch.object(local, "connect") as connect,
            mock.patch("sys.stdout", new=io.StringIO()) as output,
        ):
            self.assertEqual(local.main(["restart"]), 1)
        connect.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["success"])

    def test_cli_autorizado_aun_exige_stopped_antes_de_conectar(self):
        local = load("augment_sdk")
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

    def test_payload_copia_solo_rustfmt_y_avisos_del_manifest(self):
        spec = json.loads((HERE / "rustfmt-augmentation.json").read_bytes())
        self.remote.validate_spec(spec)
        memory = io.BytesIO()
        payload = {}
        with tarfile.open(fileobj=memory, mode="w:xz") as archive:
            row = tarfile.TarInfo("rustfmt-beta-x86_64-unknown-linux-musl")
            row.type = tarfile.DIRTYPE
            archive.addfile(row)
            for name in [*spec["files_to_copy"], "bin/cargo-fmt"]:
                data = ("datos de " + name).encode()
                row = tarfile.TarInfo(spec["archive_prefix"] + name)
                row.size = len(data)
                archive.addfile(row, io.BytesIO(data))
                if name != "bin/cargo-fmt":
                    payload[name] = data
                    spec["files_to_copy"][name] = {
                        "sha256": self.remote.digest(data),
                        "size": len(data),
                    }
        raw = memory.getvalue()
        spec["archive_sha256"] = self.remote.digest(raw)
        spec["archive_size"] = len(raw)
        self.assertEqual(self.remote.selected_payload(raw, spec), payload)
        with self.assertRaises(ValueError):
            self.remote.selected_payload(raw + b"otra cosa", spec)

    def test_raiz_rustfmt_solo_directorio_y_sin_duplicados(self):
        spec = json.loads((HERE / "rustfmt-augmentation.json").read_bytes())
        for kind, repeat in (
            (tarfile.REGTYPE, False),
            (tarfile.SYMTYPE, False),
            (tarfile.DIRTYPE, True),
        ):
            memory = io.BytesIO()
            with tarfile.open(fileobj=memory, mode="w:xz") as archive:
                for _ in range(2 if repeat else 1):
                    row = tarfile.TarInfo("rustfmt-beta-x86_64-unknown-linux-musl")
                    row.type = kind
                    row.linkname = "/fuera"
                    archive.addfile(row)
            raw = memory.getvalue()
            spec["archive_size"] = len(raw)
            spec["archive_sha256"] = self.remote.digest(raw)
            with self.assertRaises(ValueError):
                self.remote.selected_payload(raw, spec)

    def test_manifiesto_no_acepta_helper_cargo_fmt_o_prefijo_ajeno(self):
        spec = json.loads((HERE / "rustfmt-augmentation.json").read_bytes())
        for changed in ("bin/cargo-fmt", "../fuera"):
            bad = copy.deepcopy(spec)
            bad["files_to_copy"][changed] = {"sha256": "a" * 64, "size": 1}
            with self.assertRaises(ValueError):
                self.remote.validate_spec(bad)
        with self.assertRaises(ValueError):
            self.remote.validate_spec({**spec, "install_prefix": "/usr/local"})

    def test_driver_exacto_antes_de_ejecutar_el_formateador(self):
        self.remote.require_driver(b"driver fijado", self.remote.digest(b"driver fijado"))
        with self.assertRaises(ValueError):
            self.remote.require_driver(b"driver cambiado", self.remote.digest(b"driver fijado"))

    def test_catalogo_parcial_lee_hardlinks_sin_adoptarlos_como_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "objeto").write_bytes(b"objeto de Cargo")
            os.link(root / "objeto", root / "alias")
            with self.assertRaises(ValueError):
                self.remote.read_regular(root / "objeto")
            self.assertEqual(
                self.remote.read_regular(root / "alias", allow_hardlinks=True), b"objeto de Cargo"
            )

    def test_augment_exige_restart_respaldado_antes_de_conectar(self):
        local = load("augment_sdk")
        sdk = types.SimpleNamespace(
            read_source=lambda _path: b"{}", read_values=lambda: {}, decode=lambda _value: {}
        )
        with (
            mock.patch.object(local, "sdk", return_value=sdk),
            mock.patch.object(local, "verify_pins"),
            mock.patch.object(local, "connect") as connect,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(
                local.main(
                    [
                        "augment",
                        "--root-update-vault",
                        "--profile",
                        "controlado.json",
                        "--expected-profile-sha256",
                        "a" * 64,
                    ]
                ),
                1,
            )
        connect.assert_not_called()

    def test_version_formatter_distingue_etiqueta_distribucion_y_version_reportada(self):
        self.remote.validate_formatter_version("rustfmt 1.11.0-beta (01ddda61fa 2026-11-04)")
        for invalid in (
            "rustfmt 1.10.0-beta (01ddda61fa 2026-11-04)",
            "rustfmt 1.11.0-beta (otro 2026-11-04)",
            "simulador 1.11.0-beta (01ddda61fa)",
        ):
            with self.assertRaises(ValueError):
                self.remote.validate_formatter_version(invalid)


if __name__ == "__main__":
    unittest.main()
