"""Archivos comprimidos del SDK/fuente: pruebas locales sin ejecutar herramientas."""

import importlib.util
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_backup_sdk", ROOT / "sre/prepare_backup_sdk.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def zip_bytes(names):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        for name, data in names:
            package.writestr(name, data)
    return stream.getvalue()


def tar_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as package:
        for name, kind, data in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.LNKTYPE, tarfile.SYMTYPE):
                member.linkname = "other"
            package.addfile(member, io.BytesIO(data) if member.isfile() else None)
    return stream.getvalue()


class BackupSdk(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sdk-archive-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_zip_rechaza_traversal_ads_reservados_y_normalizacion_windows_antes_de_escribir(self):
        for name in (
            "go/../x",
            "go/NUL.txt",
            "go/COM1.exe",
            "go/COM¹.txt",
            "go/x:ads",
            "go/x. ",
            "go//x",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    MODULE.extract_archive(
                        zip_bytes([("go/bin/go.exe", b"good"), (name, b"bad")]),
                        "zip",
                        "go",
                        self.root / "target",
                    )
                self.assertFalse((self.root / "target").exists())

    def test_zip_rechaza_nul_y_backslash_en_nombre_crudo_del_archivo(self):
        for raw in (b"go/x\x00other", b"go/x\\other"):
            data = zip_bytes([("go/x-other", b"x")]).replace(b"go/x-other", raw)
            with self.assertRaises(ValueError):
                MODULE.extract_archive(data, "zip", "go", self.root / "target")
            self.assertFalse((self.root / "target").exists())

    def test_zip_rechaza_colision_casefold_y_archivo_directorio(self):
        for names in (
            [("go/a", b"x"), ("go/A", b"y")],
            [("go/a", b"x"), ("go/a/b", b"y")],
            [("go/A/x", b"x"), ("go/a/y", b"y")],
        ):
            with self.assertRaises(ValueError):
                MODULE.extract_archive(zip_bytes(names), "zip", "go", self.root / "target")
            self.assertFalse((self.root / "target").exists())

    def test_zip_rechaza_entrada_symlink_sin_publicar(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as package:
            entry = zipfile.ZipInfo("go/bin/tool")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            package.writestr(entry, "../elsewhere")
        with self.assertRaises(ValueError):
            MODULE.extract_archive(stream.getvalue(), "zip", "go", self.root / "target")
        self.assertFalse((self.root / "target").exists())

    def test_tar_rechaza_enlaces_dispositivos_y_rutas_fuera_de_raiz(self):
        for entries in (
            [("restic/a", tarfile.SYMTYPE, b"")],
            [("restic/a", tarfile.LNKTYPE, b"")],
            [("restic/a", tarfile.CHRTYPE, b"")],
            [("other/a", tarfile.REGTYPE, b"x")],
        ):
            with self.assertRaises(ValueError):
                MODULE.extract_archive(tar_bytes(entries), "tar.gz", "restic", self.root / "target")
            self.assertFalse((self.root / "target").exists())

    def test_limites_de_expansion_y_miembros_se_aplican_antes_de_escribir(self):
        data = zip_bytes([("go/a", b"12345"), ("go/b", b"x")])
        for limits in ({"max_bytes": 5}, {"max_file": 4}, {"max_members": 1}):
            with self.assertRaises(ValueError):
                MODULE.extract_archive(data, "zip", "go", self.root / "target", **limits)
            self.assertFalse((self.root / "target").exists())

    def test_destino_ajeno_se_conserva_incluso_vacio(self):
        target = self.root / "target"
        target.mkdir()
        with self.assertRaises(ValueError):
            MODULE.extract_archive(zip_bytes([("go/a", b"x")]), "zip", "go", target)
        self.assertEqual(list(target.iterdir()), [])

    def test_extraccion_real_fija_hashes_y_no_ejecuta_archivos(self):
        result = MODULE.extract_archive(
            zip_bytes([("go/bin/go.exe", b"MZstatic"), ("go/src/a.go", b"package a")]),
            "zip",
            "go",
            self.root / "target",
        )
        self.assertEqual((self.root / "target/bin/go.exe").read_bytes(), b"MZstatic")
        self.assertEqual(len(result), 2)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in result))

    def test_tar_regular_extrae_solo_archivos_y_directorios(self):
        content = tar_bytes(
            [
                ("restic", tarfile.DIRTYPE, b""),
                ("restic/cmd", tarfile.DIRTYPE, b""),
                ("restic/cmd/main.go", tarfile.REGTYPE, b"package main"),
            ]
        )
        result = MODULE.extract_archive(content, "tar.gz", "restic", self.root / "target")
        self.assertEqual((self.root / "target/cmd/main.go").read_bytes(), b"package main")
        self.assertEqual(result[0]["size"], 12)


if __name__ == "__main__":
    unittest.main()
