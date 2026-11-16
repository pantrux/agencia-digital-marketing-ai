"""Contrato y recorrido local de la copia aislada OpenClaw."""

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "sre/openclaw_backup.py"
SPEC = importlib.util.spec_from_file_location("openclaw_backup", SOURCE)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

IDS = ("agency-account", "agency-critic", "agency-director", "agency-editor", "agency-strategist")
NODE = shutil.which("node")


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def fixture(scope="synthetic"):
    files = {
        f"{agent}/config.json": json_bytes({"id": agent, "model": "fixture/test"}) for agent in IDS
    }
    files["agency-editor/MEMORY.md"] = b"Recuerdo sintetico.\n"
    files["agency-critic/memory/2026-11-07.md"] = b"Nota sintetica.\n"
    return manifest(files, scope), files


def manifest(files, scope="synthetic"):
    return json_bytes(
        {
            "schema": 1,
            "owner": "agencia-digital-marketing-ai:openclaw-backup:v1",
            "scope": scope,
            "files": [
                {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(files.items())
            ],
        }
    )


class ArchiveTests(unittest.TestCase):
    def test_tar_reproducible_y_legible_con_tarfile(self):
        raw, files = fixture()
        archive = backup.pack_tar(raw, files, "synthetic")
        self.assertEqual(
            archive, backup.pack_tar(raw, dict(reversed(list(files.items()))), "synthetic")
        )
        self.assertEqual(backup.unpack_tar(archive, "synthetic"), (raw, files))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            self.assertEqual(tar.getnames(), ["manifest.json", *sorted(files)])
            self.assertTrue(
                all(item.isreg() and item.mtime == 0 and item.mode == 0o600 for item in tar)
            )

    def test_sintetico_no_se_admite_como_captura_real(self):
        raw, files = fixture()
        with self.assertRaises(ValueError):
            backup.pack_tar(raw, files, "agency-state")
        with self.assertRaises(ValueError):
            backup.unpack_tar(backup.pack_tar(raw, files, "synthetic"), "agency-state")

    def test_rechaza_miembros_ajenos_y_nombres_ambiguos(self):
        _, files = fixture()
        for name in (
            "agency-foreign/config.json",
            "agency-account/../foreign.md",
            "agency-account/memory/a/../../foreign.md",
            "agency-account/memory/CON.md",
            "agency-account/memory/evil\\foo.md",
            "agency-account/.env",
            "agency-account/openclaw.json",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                altered = files | {name: b"outside"}
                backup.pack_tar(manifest(altered), altered, "synthetic")

    def test_rechaza_config_global_y_config_de_otro_id(self):
        _, files = fixture()
        for config in (
            {"id": "agency-foreign", "model": "fixture/test"},
            {"id": "agency-account", "model": "fixture/test", "agents": []},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                altered = files | {"agency-account/config.json": json_bytes(config)}
                backup.pack_tar(manifest(altered), altered, "synthetic")

    def test_rechaza_id_faltante_y_payload_no_acreditado(self):
        raw, files = fixture()
        del files["agency-account/config.json"]
        with self.assertRaises(ValueError):
            backup.pack_tar(manifest(files), files, "synthetic")
        _, files = fixture()
        with self.assertRaises(ValueError):
            backup.pack_tar(raw, files | {"agency-editor/MEMORY.md": b"distinto"}, "synthetic")

    def test_rechaza_hash_en_manifest_miembro_excesivo_y_colision_de_caja(self):
        _, files = fixture()
        for added in (
            {"agency-account/memory/large.md": b"x" * (4 * 1024**2 + 1)},
            {"agency-editor/memory/AA/a.md": b"a", "agency-editor/memory/aa/b.md": b"b"},
        ):
            with self.subTest(paths=list(added)), self.assertRaises(ValueError):
                altered = files | added
                backup.pack_tar(manifest(altered), altered, "synthetic")

    def test_rechaza_enlaces_tamper_truncado_y_datos_posteriores(self):
        raw, files = fixture()
        archive = backup.pack_tar(raw, files, "synthetic")
        member = tarfile.TarInfo("manifest.json")
        member.type = tarfile.SYMTYPE
        member.linkname = "unrelated"
        link_header = member.tobuf(format=tarfile.USTAR_FORMAT)
        for bad in (
            link_header + archive[512:],
            archive.replace(b"Recuerdo sintetico.", b"Alterado sintetico.", 1),
            archive[:-1],
            archive + b"extra",
        ):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                backup.unpack_tar(bad, "synthetic")

    @unittest.skipUnless(NODE, "Node local para contrastar un codec Zstandard real")
    def test_sonda_sintetica_comprime_y_restaura_bytes_reales_zstd(self):
        support = subprocess.run(
            [
                NODE,
                "-e",
                "process.exit(typeof require('node:zlib').zstdCompressSync === 'function' ? 0 : 1)",
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if support.returncode == 1 and not support.stderr:
            self.skipTest("Esta versión de Node no incorpora el codec Zstandard")
        support.check_returncode()
        raw, files = fixture()
        archive = backup.pack_tar(raw, files, "synthetic")
        js = (
            "const fs=require('node:fs'),z=require('node:zlib');"
            "process.stdout.write(z[process.argv[1]](fs.readFileSync(0)));"
        )

        def codec(method, data):
            return subprocess.run(
                [NODE, "-e", js, method], input=data, capture_output=True, check=True, timeout=10
            ).stdout

        compressed = codec("zstdCompressSync", archive)
        self.assertTrue(compressed.startswith(bytes.fromhex("28b52ffd")))
        self.assertEqual(compressed, codec("zstdCompressSync", archive))
        self.assertEqual(
            backup.unpack_tar(codec("zstdDecompressSync", compressed), "synthetic"), (raw, files)
        )


@unittest.skipUnless(sys.platform == "linux", "guardas nativas Linux")
class StagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agency-openclaw-fixture-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.raw, self.files = fixture()
        self.stage = self.root / "staged"
        self.stage.mkdir(mode=0o700)
        for name, data in {"manifest.json": self.raw, **self.files}.items():
            path = self.stage / name
            for parent in reversed(path.parents):
                if parent.is_relative_to(self.stage) and not parent.exists():
                    parent.mkdir(mode=0o700)
            path.write_bytes(data)
            path.chmod(0o600)
        self.pin = hashlib.sha256(self.raw).hexdigest()

    def test_rechaza_manifiesto_no_fijado_y_staging_con_entradas_extra(self):
        with self.assertRaises(ValueError):
            backup.read_staging(self.root, "staged", "0" * 64, "synthetic")
        (self.stage / "agency-foreign").mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            backup.read_staging(self.root, "staged", self.pin, "synthetic")

    def test_rechaza_enlace_duro_y_simbolico(self):
        path = self.stage / "agency-editor/MEMORY.md"
        unrelated = self.root / "unrelated.md"
        os.link(path, unrelated)
        with self.assertRaises(ValueError):
            backup.read_staging(self.root, "staged", self.pin, "synthetic")
        path.unlink()
        path.symlink_to(unrelated)
        with self.assertRaises(ValueError):
            backup.read_staging(self.root, "staged", self.pin, "synthetic")
        self.assertEqual(unrelated.read_bytes(), self.files["agency-editor/MEMORY.md"])

    @unittest.skipUnless(Path("/usr/bin/zstd").is_file(), "requiere /usr/bin/zstd de operación")
    def test_cli_capture_y_restore_nuevos_con_hash_y_equipo_ajeno_intacto(self):
        sentinel = self.root / "other-team"
        sentinel.write_bytes(b"other team")

        def cli(*args, check=True):
            return subprocess.run(
                [sys.executable, "-I", "-B", str(SOURCE), *map(str, args)],
                capture_output=True,
                check=check,
                timeout=20,
            )

        receipt = json.loads(
            cli("capture", self.root, "staged", "snapshot", self.pin, "synthetic").stdout
        )
        self.assertEqual(receipt["scope"], "synthetic")
        self.assertTrue(receipt["synthetic"])
        self.assertEqual(receipt["archive_name"], "openclaw.synthetic.tar.zst")
        self.assertEqual(
            [path.name for path in (self.root / "snapshot").iterdir()],
            ["openclaw.synthetic.tar.zst"],
        )
        copy_receipt = json.loads(
            cli("capture", self.root, "staged", "replica", self.pin, "synthetic").stdout
        )
        self.assertEqual(receipt["archive_sha256"], copy_receipt["archive_sha256"])
        failed = cli(
            "restore", self.root, "snapshot", "restored", "0" * 64, "synthetic", check=False
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((self.root / "restored").exists())
        args = (
            "restore",
            self.root,
            "snapshot",
            "restored",
            receipt["archive_sha256"],
            "synthetic",
        )
        self.assertEqual(json.loads(cli(*args).stdout)["manifest_sha256"], self.pin)
        self.assertEqual(
            (self.root / "restored/agency-editor/MEMORY.md").read_bytes(),
            self.files["agency-editor/MEMORY.md"],
        )
        self.assertNotEqual(cli(*args, check=False).returncode, 0)
        self.assertEqual(sentinel.read_bytes(), b"other team")


if __name__ == "__main__":
    unittest.main()
