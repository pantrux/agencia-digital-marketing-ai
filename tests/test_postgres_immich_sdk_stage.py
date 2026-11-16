"""Staging del SDK con archivos sintéticos; sin red, SDK ni compiladores."""

import hashlib
import importlib.util
import io
import json
import struct
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_sdk_stage", ROOT / "docker/postgres-immich/stage_native_sdk.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:xz") as package:
        for name, content, kind, target in entries:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, target
            if kind == tarfile.REGTYPE:
                member.size = len(content)
                package.addfile(member, io.BytesIO(content))
            else:
                package.addfile(member)
    return stream.getvalue()


class SdkStage(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdk-stage-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_importar_no_ejecuta_ni_descarga(self):
        spec = importlib.util.spec_from_file_location("stage_import", SPEC.origin)
        module = importlib.util.module_from_spec(spec)
        with mock.patch("subprocess.run") as process:
            spec.loader.exec_module(module)
        process.assert_not_called()

    def test_catalogo_conserva_hashes_sin_extraer_ni_ejecutar(self):
        prefix = "cargo-beta-x86_64-unknown-linux-musl"
        raw = archive([(prefix + "/cargo/bin/cargo", b"archivo de prueba", tarfile.REGTYPE, "")])
        path = self.root / "cargo.tar.xz"
        path.write_bytes(raw)
        with mock.patch.object(
            MODULE, "elf_info", side_effect=AssertionError("No releer un archivo no ELF")
        ):
            result = MODULE.inspect_sdk(path, "cargo", hashlib.sha256(raw).hexdigest())
        self.assertFalse(result["sdk_executed"])
        self.assertEqual(
            result["files"][0]["sha256"], hashlib.sha256(b"archivo de prueba").hexdigest()
        )
        self.assertFalse((self.root / prefix).exists())

    def test_elf_catalogado_identifica_musl_needed_y_glibc_sin_ejecutarlo(self):
        data = bytearray(1024)
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", data, 18, 62)
        struct.pack_into("<Q", data, 32, 64)
        struct.pack_into("<HH", data, 54, 56, 3)
        struct.pack_into("<IIQQQQQQ", data, 64, 1, 4, 0, 0, 0, 1024, 1024, 4096)
        interpreter = b"/lib/ld-musl-x86_64.so.1\0"
        struct.pack_into(
            "<IIQQQQQQ", data, 120, 3, 4, 256, 256, 0, len(interpreter), len(interpreter), 1
        )
        data[256 : 256 + len(interpreter)] = interpreter
        strings = b"\0libc.so\0GLIBC_2.38\0"
        struct.pack_into("<IIQQQQQQ", data, 176, 2, 4, 320, 320, 0, 64, 64, 8)
        for offset, pair in enumerate(((5, 512), (10, len(strings)), (1, 1), (0, 0))):
            struct.pack_into("<qQ", data, 320 + 16 * offset, *pair)
        data[512 : 512 + len(strings)] = strings
        result = MODULE.elf_info(io.BytesIO(data))
        self.assertEqual(result["interpreter"], "/lib/ld-musl-x86_64.so.1")
        self.assertEqual(result["needed"], ["libc.so"])
        self.assertEqual(result["glibc_versions"], ["GLIBC_2.38"])

    def test_catalogo_rechaza_hash_path_y_enlace_fuera_de_raiz(self):
        prefix = "cargo-beta-x86_64-unknown-linux-musl"
        cases = [
            [(prefix + "/../other", b"x", tarfile.REGTYPE, "")],
            [("other/bin/cargo", b"x", tarfile.REGTYPE, "")],
            [(prefix + "/link", b"", tarfile.SYMTYPE, "../../other")],
            [
                (prefix + "/a", b"x", tarfile.REGTYPE, ""),
                (prefix + "/a", b"y", tarfile.REGTYPE, ""),
            ],
        ]
        for index, entries in enumerate(cases):
            raw = archive(entries)
            path = self.root / f"case-{index}.tar.xz"
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                MODULE.inspect_sdk(path, "cargo", hashlib.sha256(raw).hexdigest())

    def test_catalogo_permite_enlace_interno_y_rechaza_tamano_excesivo(self):
        prefix = "cargo-beta-x86_64-unknown-linux-musl"
        raw = archive(
            [
                (prefix + "/bin/tool", b"123", tarfile.REGTYPE, ""),
                (prefix + "/bin/alias", b"", tarfile.SYMTYPE, "tool"),
            ]
        )
        path = self.root / "links.tar.xz"
        path.write_bytes(raw)
        result = MODULE.inspect_sdk(path, "cargo", hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(result["links"]), 1)
        with self.assertRaises(ValueError):
            MODULE.inspect_sdk(path, "cargo", hashlib.sha256(raw).hexdigest(), max_bytes=2)

    def test_descarga_tiene_tls_normal_sin_config_ni_redireccion_y_no_sobrescribe(self):
        target = self.root / "sdk.tar.xz"
        data = b"entrada fijada"

        def run(command, **options):
            self.assertTrue(command[0].replace("\\", "/").endswith("System32/curl.exe"))
            self.assertEqual(command[1], "-q")
            self.assertNotIn("--location", command)
            self.assertNotIn("--insecure", command)
            self.assertNotIn("-k", command)
            self.assertEqual(command[command.index("--proto") + 1], "=https")
            options["stdout"].write(data)
            return mock.Mock(stderr=b"200")

        with mock.patch.object(subprocess, "run", side_effect=run):
            MODULE.download_file(
                "https://static.rust-lang.org/dist/fixed.tar.xz",
                target,
                hashlib.sha256(data).hexdigest(),
                expected_size=len(data),
            )
        with self.assertRaises(ValueError):
            MODULE.download_file(
                "https://static.rust-lang.org/dist/fixed.tar.xz",
                target,
                hashlib.sha256(data).hexdigest(),
            )

    def test_descarga_rechaza_host_no_autorizado_y_checksum_incorrecto(self):
        with self.assertRaises(ValueError):
            MODULE.download_file("https://example.org/a", self.root / "a", "0" * 64)
        target = self.root / "b"

        def run(_command, **options):
            options["stdout"].write(b"alterado")
            return mock.Mock(stderr=b"200")

        with mock.patch.object(subprocess, "run", side_effect=run):
            with self.assertRaises(ValueError):
                MODULE.download_file("https://static.rust-lang.org/dist/a", target, "0" * 64)

    def test_helper_alterado_no_se_importa(self):
        path = self.root / "helper.py"
        path.write_text("raise RuntimeError('no ejecutar')\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            MODULE.load_pinned_module(path, "0" * 64)

    def test_publicacion_no_reemplaza_directorio_ajeno(self):
        stage, final = self.root / "stage", self.root / "final"
        stage.mkdir()
        final.mkdir()
        (stage / "data").write_bytes(b"fijado")
        with self.assertRaises(ValueError):
            MODULE.publish(stage, final, {"sdk_executed": False})
        self.assertTrue((stage / "data").is_file())
        self.assertEqual(list(final.iterdir()), [])

    def test_plan_por_defecto_no_prepara_ni_ejecuta_sdk(self):
        with (
            mock.patch.object(MODULE, "prepare_inputs") as prepare,
            mock.patch("sys.stdout", new=io.StringIO()) as output,
        ):
            result = MODULE.main([])
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(output.getvalue())["sdk_executed"])
        prepare.assert_not_called()

    def test_reutilizar_solo_staging_propio_y_manifiesto_exactos(self):
        name = ".native-staging-" + "a" * 32
        stage = self.root / name
        stage.mkdir()
        (stage / "downloads").mkdir()
        (stage / ".owner.json").write_text(
            json.dumps({"owner": "propio", "manifest_sha256": "b" * 64})
        )
        vendor = mock.Mock(ROOT=self.root, OWNER="propio", MANIFEST_SHA256="b" * 64)
        self.assertEqual(MODULE.reuse_directory(vendor, name), stage / "downloads")
        for candidate in ("../ajeno", str(stage), ".native-staging-" + "x" * 32):
            with self.assertRaises(ValueError):
                MODULE.reuse_directory(vendor, candidate)
        (stage / ".owner.json").write_text(
            json.dumps({"owner": "otro", "manifest_sha256": "b" * 64})
        )
        with self.assertRaises(ValueError):
            MODULE.reuse_directory(vendor, name)


if __name__ == "__main__":
    unittest.main()
