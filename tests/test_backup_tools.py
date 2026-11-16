"""Instalación de CLI con archivos reales aislados; nunca instala en los destinos."""

import bz2
import copy
import hashlib
import importlib.util
import json
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sre"))
SPEC = importlib.util.spec_from_file_location("backup_tools", ROOT / "sre/install_backup_tools.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def binary(platform, go="go1.27.3"):
    content = bytearray(256)
    if platform.startswith("windows"):
        content[:2] = b"MZ"
        struct.pack_into("<I", content, 60, 64)
        content[64:68] = b"PE\0\0"
        struct.pack_into("<H", content, 68, 0x8664)
        struct.pack_into("<H", content, 88, 0x20B)
    else:
        content[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", content, 18, 62)
    header = bytearray(32)
    header[:14] = b"\xff Go buildinf:"
    header[14:16] = b"\x08\x02"
    return bytes(content + header + bytes([len(go)]) + go.encode() + b"\x00")


class BackupTools(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="backup-tool-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.archives = self.folder / "archives"
        self.archives.mkdir()
        self.target = self.folder / "managed"
        self.platform = MODULE.native_platform()
        self.document = json.loads((ROOT / "sre/backup-tools-artifacts.json").read_text())
        self.document["installation_enabled"] = True
        for entry in self.document["artifacts"]:
            data = binary(entry["platform"])
            entry["binary"].update(
                sha256=hashlib.sha256(data).hexdigest(), size=len(data), go_version="go1.27.3"
            )
            entry["review"].update(status="verified", high=0, critical=0, coverage_verified=True)
            archive = self.archives / entry["archive"]["filename"]
            if entry["archive"]["format"] == "bz2":
                archive.write_bytes(bz2.compress(data))
            else:
                with zipfile.ZipFile(archive, "w") as package:
                    package.writestr(entry["archive"]["member"], data)
            self.repin(entry, archive)
        self.manifest = self.folder / "manifest.json"
        self.addCleanup(patch.stopall)
        patch.object(MODULE, "INSTALL_ROOTS", {self.platform: str(self.target)}).start()
        patch.object(MODULE, "check_access", lambda *_args, **_kwargs: None).start()
        # La identidad es parte del borde simulado: los archivos son propios de
        # este fixture y la prueba no necesita elevar al usuario de CI.
        patch.object(MODULE.os, "geteuid", return_value=0, create=True).start()

    def test_guardia_posix_sigue_rechazando_usuario_no_root(self):
        simulated = SimpleNamespace(**dict(vars(MODULE.os), name="posix", geteuid=lambda: 1001))
        with patch.object(MODULE, "os", simulated):
            with self.assertRaisesRegex(ValueError, "requiere el propietario root"):
                self.apply()
        self.assertFalse(self.target.exists())

    def repin(self, entry, archive):
        entry["archive"].update(
            sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), size=archive.stat().st_size
        )

    def save(self):
        self.manifest.write_text(json.dumps(self.document), encoding="utf-8")
        return hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def apply(self):
        return MODULE.apply_install(self.manifest, self.archives, self.save(), self.platform)

    def entry(self, tool="rclone", platform=None):
        return next(
            item
            for item in self.document["artifacts"]
            if item["tool"] == tool and item["platform"] == (platform or self.platform)
        )

    def test_plan_no_abre_red_ni_ejecuta_binarios_ni_crea_destino(self):
        with (
            patch("socket.socket", side_effect=AssertionError),
            patch("subprocess.run", side_effect=AssertionError),
        ):
            result = MODULE.make_plan(self.document, self.platform)
        self.assertEqual(result["mode"], "plan")
        self.assertFalse(self.target.exists())

    def test_oficiales_con_sca_bloqueada_no_se_instalan(self):
        self.document = json.loads((ROOT / "sre/backup-tools-artifacts.json").read_text())
        with self.assertRaises(ValueError):
            self.apply()
        self.assertFalse(self.target.exists())

    def test_hash_externo_del_manifiesto_no_se_toma_del_propio_archivo(self):
        self.save()
        with self.assertRaises(ValueError):
            MODULE.apply_install(self.manifest, self.archives, "0" * 64, self.platform)
        self.assertFalse(self.target.exists())

    def test_archivo_alterado_no_deja_instalacion_parcial(self):
        (self.archives / self.entry()["archive"]["filename"]).write_bytes(b"alterado")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertFalse(self.target.exists())

    def test_zip_no_publica_traversal_enlaces_o_binarios_duplicados(self):
        entry = self.entry("rclone", "windows-amd64")
        for case in ("traversal", "symlink", "duplicate"):
            archive = self.folder / f"{case}.zip"
            with zipfile.ZipFile(archive, "w") as package:
                member = entry["archive"]["member"]
                package.writestr(member, binary("windows-amd64"))
                if case == "traversal":
                    package.writestr("../fuera.txt", b"no")
                elif case == "symlink":
                    info = zipfile.ZipInfo("enlace")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    package.writestr(info, "../fuera")
                else:
                    package.writestr(member.upper(), b"duplicado")
            self.repin(entry, archive)
            with self.subTest(case=case), self.assertRaises(ValueError):
                MODULE.extract_binary(archive, entry)
        self.assertFalse((self.folder / "fuera.txt").exists())

    def test_bz2_no_admite_concatenacion_truncado_o_expansion_excesiva(self):
        entry = self.entry("restic", "linux-amd64")
        original = bz2.compress(binary("linux-amd64"))
        for case, data in (
            ("concatenated", original + bz2.compress(b"otra carga")),
            ("truncated", original[:-5]),
            ("expanded", bz2.compress(b"x" * 10000)),
        ):
            archive = self.folder / (case + ".bz2")
            archive.write_bytes(data)
            self.repin(entry, archive)
            with self.subTest(case=case), self.assertRaises(ValueError):
                MODULE.extract_binary(archive, entry)

    def test_binario_de_otro_formato_o_go_diferente_se_rechaza(self):
        entry = self.entry("rclone", "windows-amd64")
        for data in (binary("linux-amd64"), binary("windows-amd64", "go1.26.4")):
            archive = self.folder / "wrong.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(entry["archive"]["member"], data)
            entry["binary"].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
            self.repin(entry, archive)
            with self.assertRaises(ValueError):
                MODULE.extract_binary(archive, entry)

    def test_destino_ajeno_incluso_vacio_no_se_adopta(self):
        self.target.mkdir()
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(list(self.target.iterdir()), [])

    def test_instalacion_propia_es_idempotente_y_no_ejecuta_cli(self):
        with patch("subprocess.run", side_effect=AssertionError):
            first = self.apply()
            before = {p.name: p.stat().st_mtime_ns for p in self.target.iterdir()}
            second = self.apply()
        self.assertEqual(first["installed"], 2)
        self.assertEqual(second["installed"], 0)
        self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in self.target.iterdir()})

    def test_binario_instalado_alterado_no_se_sobrescribe(self):
        self.apply()
        target = self.target / self.entry()["binary"]["filename"]
        target.write_bytes(b"ajeno")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(target.read_bytes(), b"ajeno")

    def test_archivo_extra_o_lock_existente_se_conserva_y_bloquea(self):
        for name in ("otro-proyecto.txt", MODULE.LOCK_FILE):
            if not self.target.exists():
                self.apply()
            extra = self.target / name
            extra.write_text("no tocar")
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.apply()
            self.assertEqual(extra.read_text(), "no tocar")
            extra.unlink()

    def test_colision_durante_ampliacion_no_reescribe_recibo_previo(self):
        digest = self.save()
        MODULE.apply_install(self.manifest, self.archives, digest, self.platform, ("restic",))
        receipt = self.target / MODULE.RECEIPT_FILE
        before = receipt.read_bytes()
        publish = MODULE.publish_file

        def collision(path, data, **options):
            publish(path, data, **options)
            if path.name == self.entry()["binary"]["filename"]:
                (self.target / "ajeno.txt").write_text("no modificar")

        with patch.object(MODULE, "publish_file", collision), self.assertRaises(ValueError):
            MODULE.apply_install(self.manifest, self.archives, digest, self.platform, ("rclone",))
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual((self.target / "ajeno.txt").read_text(), "no modificar")

    def test_hardlink_real_de_archivo_de_origen_se_rechaza(self):
        archive = self.archives / self.entry()["archive"]["filename"]
        MODULE.os.link(archive, self.folder / "otro-enlace.zip")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertFalse(self.target.exists())

    def test_fifo_se_rechaza_antes_de_abrir_y_bloquear_lectura(self):
        archive = self.archives / self.entry()["archive"]["filename"]
        real = MODULE.os.lstat

        def fifo(path, **kwargs):
            if Path(path) == archive:
                return SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_nlink=1, st_size=0)
            return real(path, **kwargs)

        with (
            patch.object(MODULE.os, "lstat", fifo),
            patch.object(MODULE.os, "open", side_effect=AssertionError("No abrir FIFO")),
            self.assertRaises(ValueError),
        ):
            MODULE.read_regular(archive, 1024)

    def test_no_basta_habilitar_instalacion_si_cobertura_go_o_hallazgos_fallan(self):
        for change in (
            {"coverage_verified": False},
            {"high": 1},
            {"status": "blocked"},
        ):
            document = copy.deepcopy(self.document)
            self.entry()["review"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.apply()
            self.assertFalse(self.target.exists())
            self.document = document

    def test_acl_windows_y_propietario_posix_se_validan_sin_cambiar_permisos(self):
        import backup_tools_access as access

        good = {
            "current_sid": "S-1-5-21-1000",
            "owner_sid": "S-1-5-21-1000",
            "reparse": False,
            "access": [],
        }
        access.validate_windows_access(good, private=True)
        for change in (
            {"owner_sid": "S-1-5-21-2000"},
            {"reparse": True},
            {"access": [{"sid": "S-1-1-0", "rights": 2}]},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                access.validate_windows_access(good | change, private=False)
        with self.assertRaises(ValueError):
            access.validate_windows_access(
                good | {"access": [{"sid": "S-1-1-0", "rights": 1}]}, private=True
            )
        access.validate_posix_access(
            SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700), uid=0, private=True
        )
        for info in (
            SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o700),
            SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o777),
        ):
            with self.assertRaises(ValueError):
                access.validate_posix_access(info, uid=0, private=True)

    def test_reparse_y_hardlink_en_cache_se_rechazan(self):
        real = MODULE.os.lstat
        archive = self.archives / self.entry()["archive"]["filename"]

        def altered(path, **kwargs):
            info = real(path, **kwargs)
            if Path(path) == archive:
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=2, st_file_attributes=1024)
            return info

        with patch.object(MODULE.os, "lstat", altered), self.assertRaises(ValueError):
            self.apply()
        self.assertFalse(self.target.exists())

    def test_manifest_no_admite_roles_omitidos_urls_ajenas_o_json_ambiguo(self):
        for mutation in (
            lambda data: data["artifacts"].pop(),
            lambda data: data["artifacts"][0]["archive"].update(url="https://ajeno.example/x"),
        ):
            value = copy.deepcopy(self.document)
            mutation(value)
            with self.assertRaises(ValueError):
                MODULE.validate_manifest(value)
        self.manifest.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError):
            MODULE.load_manifest(self.manifest)


if __name__ == "__main__":
    unittest.main()
