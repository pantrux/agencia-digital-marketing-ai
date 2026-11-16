"""Pruebas de integridad y extracción de los validadores locales."""

import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "scripts/ci/install_tools.py"
SPEC = importlib.util.spec_from_file_location("install_tools", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class InstallToolsTests(unittest.TestCase):
    def test_rechaza_descarga_alterada(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "tool.zip"
            archive.write_bytes(b"alterado")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                MODULE.verify_checksum(archive, "0" * 64)

    def test_acepta_hash_correcto(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "tool.zip"
            archive.write_bytes(b"correcto")
            MODULE.verify_checksum(archive, hashlib.sha256(b"correcto").hexdigest())

    def test_zip_extrae_solo_binario_sin_rutas_arbitrarias(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "tool.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../../fuera.txt", "no escribir")
                bundle.writestr("carpeta/tool.exe", b"binario")
            target = directory / "bin/tool.exe"
            MODULE.extract_binary(archive, "tool.exe", target)
            self.assertEqual(target.read_bytes(), b"binario")
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ["bin", "tool.zip"])

    def test_tar_rechaza_enlace_simbolico(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "tool.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                link = tarfile.TarInfo("tool")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                bundle.addfile(link)
            with self.assertRaisesRegex(ValueError, "regular"):
                MODULE.extract_binary(archive, "tool", directory / "bin/tool")

    def test_tar_instala_binario_regular(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = directory / "tool.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("version/tool")
                member.size = 7
                bundle.addfile(member, io.BytesIO(b"binario"))
            target = directory / "bin/tool"
            MODULE.extract_binary(archive, "tool", target)
            self.assertEqual(target.read_bytes(), b"binario")

    def test_rechaza_binarios_ambiguos(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "tool.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("uno/tool.exe", b"uno")
                bundle.writestr("dos/tool.exe", b"dos")
            with self.assertRaisesRegex(ValueError, "único"):
                MODULE.extract_binary(archive, "tool.exe", Path(temporary) / "bin/tool.exe")


if __name__ == "__main__":
    unittest.main()
