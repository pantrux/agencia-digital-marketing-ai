"""Recibos y archivos de prueba locales para el ensamblador 3-2-1."""

import hashlib
import sys
import tempfile
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sre import backup_321_staging as join


def sha(data):
    return hashlib.sha256(data).hexdigest()


class EnsamblarStagingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.out = self.root / "ensamblados"
        self.out.mkdir()
        self.start = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        self.end = (datetime.now(UTC) - timedelta(minutes=4)).isoformat()
        self.enterContext(patch.object(join.core, "ROOT", self.root))
        self.enterContext(patch.object(join.core, "private_directory"))
        self.enterContext(patch.object(join.core, "private", side_effect=lambda p, *a: p.lstat()))
        self.enterContext(patch.object(join.core, "sync_directory"))
        self.enterContext(patch.object(join.core, "exclusive", side_effect=nullcontext))
        self.enterContext(patch.object(join.core, "read_verified", side_effect=self.leer_fixture))
        if sys.platform == "win32":
            original_identity = join.core.identity

            def identidad_windows(node):
                raw = original_identity(node)
                # Windows puede devolver ctime distinto para lstat y fstat.
                return (*raw[:-1], 0)

            self.enterContext(patch.object(join.core, "identity", side_effect=identidad_windows))
        self.enterContext(
            patch.object(
                join.os,
                "statvfs",
                create=True,
                return_value=SimpleNamespace(f_bavail=100000, f_frsize=4096),
            )
        )
        self.enterContext(patch.object(join.os, "O_NOFOLLOW", 0, create=True))
        self.enterContext(patch.object(join.os, "O_NONBLOCK", 0, create=True))
        self.original_unpack = join.openclaw.unpack_tar
        self.openclaw_unpack = self.enterContext(
            patch.object(
                join.openclaw,
                "unpack_tar",
                return_value=(b"manifiesto", dict.fromkeys(range(5), b"x")),
            )
        )
        self.openclaw_zstd = self.enterContext(
            patch.object(join.openclaw, "zstd", return_value=b"tar")
        )
        self.request = {"root": str(self.out), "run_id": "corrida-1", "sources": {}}
        pg = self.root / "pg" / "corrida-1"
        pg.mkdir(parents=True)
        self.sources = {}
        self.pg_receipt = {
            "success": True,
            "step": "captured",
            "scope": "production",
            "run_id": "corrida-1",
            "run_directory": str(pg),
            "backup_set_complete": False,
            "captures": {},
        }
        for i, service in enumerate(("postgres-apps", "postgres-immich"), 1):
            name = service + ".sql.zst"
            subdir = pg / service
            subdir.mkdir()
            payload = subdir / ("postgres.sql.zst" if i == 1 else name)
            provenance = {
                "cid": str(i) * 64,
                "image": "sha256:" + str(i + 3) * 64,
                "pgdata": str(self.root / (service + "-data")),
            }
            self.request["sources"][service] = provenance
            self.pg_receipt["captures"][service] = {
                **self.preparar_payload(name, payload),
                "service": service,
                "cid": provenance["cid"],
                "source": {**provenance, "pid": 20 + i, "started": self.start},
                "staging_name": name,
                "receipt_sha256": "f" * 64,
                "started_at_utc": self.start,
                "completed_at_utc": self.end,
            }
        self.recibo("postgres", pg / "result.json", self.pg_receipt)
        d1 = self.root / "d1" / "corrida-1"
        d1.mkdir(parents=True)
        self.recibo(
            "d1",
            d1 / "result.json",
            {
                **self.preparar_payload("d1.sql.zst", d1 / "d1.sql.zst"),
                "success": True,
                "step": "captured",
                "backup_set_complete": False,
                "run_id": "corrida-1",
                "run_directory": str(d1),
                "resource": "studio-saas-db",
                "account_id": "a" * 32,
                "database_id": join.d1.DATABASE_ID,
                "owner_verified": True,
                "sqlite_integrity_verified": True,
                "cloud_restore_verified": False,
                "sql_bytes": 20,
                "sql_sha256": "1" * 64,
                "export_receipt_sha256": "2" * 64,
                "staging_name": "d1.sql.zst",
                "started_at_utc": self.start,
                "completed_at_utc": self.end,
            },
        )
        self.request["sources"]["d1_account_id"] = "a" * 32
        oc = self.root / "openclaw" / "corrida-1"
        oc.mkdir(parents=True)
        payload = self.preparar_payload("openclaw.tar.zst", oc / "openclaw.tar.zst")
        self.oc_receipt = {
            "success": True,
            "action": "capture",
            "scope": "agency-state",
            "synthetic": False,
            "source_attestation": "external-staging-manifest",
            "agents": 5,
            "files": 5,
            "manifest_sha256": sha(b"manifiesto"),
            "archive_name": "openclaw.tar.zst",
            "archive_size": payload["bytes"],
            "archive_sha256": payload["sha256"],
        }
        self.recibo("openclaw", oc / "capture.json", self.oc_receipt)
        self.request["openclaw"].update(
            hostname="openclaw-ia",
            manifest_sha256=sha(b"manifiesto"),
            started_at_utc=self.start,
            completed_at_utc=self.end,
        )

    @staticmethod
    def leer_fixture(path, maximum=None, modes=None):
        raw = path.read_bytes()
        return raw if maximum is not None else sha(raw), join.core.identity(path.lstat())

    def preparar_payload(self, name, path):
        data = join.ZSTD_MAGIC + name.encode()
        path.write_bytes(data)
        self.sources[name] = path
        return {"path": str(path), "sha256": sha(data), "bytes": len(data)}

    def recibo(self, key, path, value):
        raw = join.core.encode(value) + b"\n"
        path.write_bytes(raw)
        self.request.setdefault(key, {}).update(receipt=str(path), sha256=sha(raw))

    def ensamblar(self):
        join.validate_request(self.request)
        result = {"success": False, "backup_set_complete": False}
        join.assemble(self.request, result)
        return result

    def test_manifiesto_exacto_y_recibo_externo_con_fuentes_intactas(self):
        before = {name: path.read_bytes() for name, path in self.sources.items()}
        result = self.ensamblar()
        self.assertTrue(result["success"], result)
        self.assertFalse(result["backup_set_complete"])
        stage = Path(result["staging"])
        manifest = "".join(f"{sha(before[name])}  {name}\n" for name in join.core.FILES).encode()
        self.assertEqual((stage / "SHA256SUMS").read_bytes(), manifest)
        self.assertEqual(result["manifest_sha256"], sha(manifest))
        self.assertTrue((stage.parent / "result.json").is_file())
        self.assertEqual(before, {name: path.read_bytes() for name, path in self.sources.items()})
        with self.assertRaises(FileExistsError):
            self.ensamblar()

    def test_rechaza_postgres_de_ensayo_y_openclaw_sintetico(self):
        pg_path = Path(self.request["postgres"]["receipt"])
        self.recibo("postgres", pg_path, {**self.pg_receipt, "scope": "acceptance"})
        with self.assertRaisesRegex(ValueError, "POSTGRES_ALCANCE"):
            self.ensamblar()
        self.recibo("postgres", pg_path, self.pg_receipt)
        oc_path = Path(self.request["openclaw"]["receipt"])
        self.recibo(
            "openclaw", oc_path, {**self.oc_receipt, "scope": "synthetic", "synthetic": True}
        )
        with self.assertRaisesRegex(ValueError, "OPENCLAW_ALCANCE"):
            self.ensamblar()
        self.assertFalse(any(self.out.iterdir()))

    def test_rechaza_recibo_sin_pin_fuente_distinta_y_captura_antigua(self):
        original = self.request["d1"]["sha256"]
        self.request["d1"]["sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "SHA_RECIBO"):
            self.ensamblar()
        self.request["d1"]["sha256"] = original
        original = self.request["sources"]["postgres-immich"]["image"]
        self.request["sources"]["postgres-immich"]["image"] = "sha256:" + "a" * 64
        with self.assertRaisesRegex(ValueError, "POSTGRES_FUENTE"):
            self.ensamblar()
        self.request["sources"]["postgres-immich"]["image"] = original
        self.request["openclaw"]["started_at_utc"] = (
            datetime.now(UTC) - timedelta(days=2)
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "VENTANA_CAPTURA"):
            self.ensamblar()

    def test_archivo_alterado_o_tar_de_otro_ambito_no_publica_staging(self):
        path = self.sources["d1.sql.zst"]
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] + b"x")
        self.assertEqual(self.ensamblar()["error"], "SHA_PAYLOAD")
        self.assertFalse((self.out / "corrida-1/staging").exists())
        path.write_bytes(raw)
        with self.assertRaises(FileExistsError):
            self.ensamblar()

    def test_tar_acredita_el_ambito_y_el_manifiesto_interno(self):
        self.openclaw_unpack.return_value = b"manifiesto ajeno", {"archivo": b"x"}
        self.assertEqual(self.ensamblar()["error"], "OPENCLAW_TAR")
        self.openclaw_unpack.assert_called_once_with(b"tar", "agency-state")
        self.assertFalse((self.out / "corrida-1/staging").exists())

    def test_rechaza_una_copia_postgres_ausente_y_el_destino_ya_ocupado(self):
        self.pg_receipt["captures"].pop("postgres-immich")
        self.recibo("postgres", Path(self.request["postgres"]["receipt"]), self.pg_receipt)
        with self.assertRaisesRegex(ValueError, "POSTGRES_RECIBO"):
            self.ensamblar()
        self.assertFalse(any(self.out.iterdir()))
        owned = self.out / "corrida-1"
        owned.mkdir()
        existing = owned / "conservar"
        existing.write_bytes(b"otro operador")
        with self.assertRaises(FileExistsError):
            self.ensamblar()
        self.assertEqual(existing.read_bytes(), b"otro operador")

    def test_verifica_el_manifiesto_openclaw_con_el_lector_real_de_tar(self):
        configs = {
            f"{agent}/config.json": join.openclaw.canonical(
                {"id": agent, "model": "proveedor/modelo"}
            )
            for agent in join.openclaw.AGENTS
        }
        for scope in ("agency-state", "synthetic"):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "openclaw.tar.zst"
                archive.write_bytes(join.ZSTD_MAGIC + b"frame-simulado")
                manifest = join.openclaw.canonical(
                    {
                        "schema": 1,
                        "owner": join.openclaw.OWNER,
                        "scope": scope,
                        "files": [
                            {"path": name, "size": len(data), "sha256": sha(data)}
                            for name, data in sorted(configs.items())
                        ],
                    }
                )
                self.openclaw_zstd.return_value = join.openclaw.pack_tar(manifest, configs, scope)
                source = {"manifest_sha256": sha(manifest), "files": len(configs)}
                with patch.object(join.openclaw, "unpack_tar", self.original_unpack):
                    if scope == "agency-state":
                        join.verify_openclaw(archive, source)
                    else:
                        with self.assertRaisesRegex(ValueError, "OPENCLAW_TAR"):
                            join.verify_openclaw(archive, source)
