"""Preparación Cargo con archivos sintéticos; sin red, SDK ni compiladores."""

import base64
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_immich_vendor", ROOT / "docker/postgres-immich/prepare_vendor.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def package(entries, prefix="example-1.0.0"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data, kind in entries:
            member = tarfile.TarInfo(prefix + "/" + name)
            member.type = kind
            member.mode = 0o755 if name.endswith(".sh") else 0o644
            if kind == tarfile.REGTYPE:
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            else:
                member.linkname = "../other"
                archive.addfile(member)
    return output.getvalue()


def crate(data, name="example", version="1.0.0"):
    return {
        "name": name,
        "version": version,
        "checksum": hashlib.sha256(data).hexdigest(),
        "download_url": f"https://static.crates.io/crates/{name}/{name}-{version}.crate",
    }


class Vendor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vendor-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_importar_no_descarga_ni_ejecuta_herramientas(self):
        spec = importlib.util.spec_from_file_location("vendor_import", SPEC.origin)
        module = importlib.util.module_from_spec(spec)
        with (
            mock.patch("urllib.request.OpenerDirector.open") as network,
            mock.patch("subprocess.run") as process,
        ):
            spec.loader.exec_module(module)
        network.assert_not_called()
        process.assert_not_called()

    def test_manifiesto_exacto_y_rechazo_de_cambio_antes_de_descargar(self):
        source = ROOT / "docker/postgres-immich/musl-build-inputs.json"
        manifest = MODULE.load_manifest(source)
        self.assertEqual(len(manifest["cargo"]["registry_crates"]), 244)
        changed = self.directory / "changed.json"
        changed.write_bytes(source.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            MODULE.load_manifest(changed)

    def test_hash_incorrecto_impide_analizar_el_archivo(self):
        content = package([("Cargo.toml", b"[package]\nname='example'", tarfile.REGTYPE)])
        entry = crate(content)
        entry["checksum"] = "0" * 64
        with self.assertRaises(ValueError):
            MODULE.inspect_crate(content, entry)

    def test_rechaza_escape_enlaces_duplicados_y_colision_de_tipo(self):
        cases = [
            [("../outside", b"x", tarfile.REGTYPE)],
            [("a", b"", tarfile.SYMTYPE)],
            [("a", b"", tarfile.LNKTYPE)],
            [("a", b"", tarfile.CHRTYPE)],
            [("a", b"x", tarfile.REGTYPE), ("a", b"y", tarfile.REGTYPE)],
            [("a", b"x", tarfile.REGTYPE), ("a/b", b"y", tarfile.REGTYPE)],
            [(".cargo-checksum.json", b"{}", tarfile.REGTYPE)],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                content = package(entries)
                with self.assertRaises(ValueError):
                    MODULE.inspect_crate(content, crate(content))

    def test_limites_se_aplican_a_miembros_y_bytes_descomprimidos(self):
        content = package([("a", b"12345", tarfile.REGTYPE), ("b", b"x", tarfile.REGTYPE)])
        for limits in ({"max_members": 1}, {"max_bytes": 5}, {"max_file": 4}):
            with self.assertRaises(ValueError):
                MODULE.inspect_crate(content, crate(content), **limits)

    def test_vendor_conserva_bytes_modo_y_checksum_cargo_sin_extraer_codigo(self):
        content = package(
            [
                ("src/lib.rs", b"pub fn hello() {}\n", tarfile.REGTYPE),
                ("build.sh", b"#!/bin/sh\ntrue\n", tarfile.REGTYPE),
            ]
        )
        item = MODULE.inspect_crate(content, crate(content))
        target = self.directory / "vendor.tar.gz"
        result = MODULE.write_vendor([item], target)
        self.assertEqual(result["crate_count"], 1)
        self.assertFalse((self.directory / "example-1.0.0").exists())
        with tarfile.open(target, "r:gz") as archive:
            self.assertEqual(
                archive.extractfile("example-1.0.0/src/lib.rs").read(), b"pub fn hello() {}\n"
            )
            self.assertEqual(archive.getmember("example-1.0.0/build.sh").mode, 0o755)
            checks = json.loads(archive.extractfile("example-1.0.0/.cargo-checksum.json").read())
        self.assertEqual(checks["package"], crate(content)["checksum"])
        self.assertEqual(
            checks["files"]["src/lib.rs"], hashlib.sha256(b"pub fn hello() {}\n").hexdigest()
        )

    def test_vendor_es_determinista_y_no_sobrescribe_un_destino(self):
        content = package([("lib.rs", b"x", tarfile.REGTYPE)])
        item = MODULE.inspect_crate(content, crate(content))
        first, second = self.directory / "one.tar.gz", self.directory / "two.tar.gz"
        MODULE.write_vendor([item], first)
        MODULE.write_vendor([item], second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with self.assertRaises(ValueError):
            MODULE.write_vendor([item], first)

    def test_reporte_git_y_c_incluye_fuentes_sin_afirmar_seleccion_o_seguridad(self):
        content = package(
            [
                ("build.rs", b'fn main(){ std::process::Command::new("git"); }', tarfile.REGTYPE),
                ("sqlite3.c", b'#define SQLITE_VERSION "3.51.1"\n', tarfile.REGTYPE),
            ]
        )
        item = MODULE.inspect_crate(content, crate(content))
        report = MODULE.audit_sources([item])
        self.assertFalse(report["manual_git_review_complete"])
        self.assertFalse(report["runtime_link_verified"])
        self.assertEqual(len(report["git_candidates"]), 1)
        self.assertEqual(report["c_sources"][0]["path"], "example-1.0.0/sqlite3.c")

    def test_acl_rechaza_herencia_owner_y_concesiones_ajenas(self):
        sid = "S-1-5-21-1-2-3-1001"
        good = {
            "owner": sid,
            "protected": True,
            "rules": [
                {"sid": sid, "allow": True, "inherits": True},
                {"sid": "S-1-5-18", "allow": True, "inherits": True},
                {"sid": "S-1-5-32-544", "allow": True, "inherits": True},
            ],
        }
        MODULE.validate_acl(good, sid)
        for change in (
            {"protected": False},
            {"owner": "S-1-1-0"},
            {"rules": good["rules"] + [{"sid": "S-1-1-0", "allow": True, "inherits": True}]},
        ):
            with self.assertRaises(ValueError):
                MODULE.validate_acl({**good, **change}, sid)

    def test_consulta_acl_usa_script_completo_codificado_y_no_stdin_interactivo(self):
        command = MODULE.acl_command("D:/raiz privada", "C:/Windows")
        self.assertIn("-EncodedCommand", command)
        self.assertNotIn("-Command", command)
        script = base64.b64decode(command[-1]).decode("utf-16le")
        self.assertIn("Get-Acl -LiteralPath", script)
        self.assertIn("$ProgressPreference = 'SilentlyContinue'", script)
        self.assertNotIn("D:/raiz privada", script)

    def test_url_rechaza_credenciales_redirect_http_y_otro_host(self):
        for url in (
            "http://static.crates.io/x",
            "https://user:secret@static.crates.io/x",
            "https://example.org/x",
            "file:///C:/x",
        ):
            with self.assertRaises(ValueError):
                MODULE.validate_url(url)

    def test_error_de_transporte_conserva_codigo_sin_reflejar_texto_ajeno(self):
        error = URLError(OSError(10054, "valor que no debe publicarse"))
        result = MODULE.error_fields(error)
        self.assertEqual(result["reason_type"], type(error.reason).__name__)
        self.assertEqual(result["errno"], 10054)
        self.assertNotIn("valor que no debe publicarse", json.dumps(result))

    def test_transporte_schannel_no_lee_config_ni_sigue_redirects_y_verifica_bytes(self):
        target = self.directory / "artifact.crate"
        content = b"archivo fijado"

        def downloader(command, **options):
            self.assertTrue(command[0].replace("\\", "/").endswith("System32/curl.exe"))
            self.assertEqual(command[1], "-q")
            self.assertNotIn("--location", command)
            self.assertNotIn("--insecure", command)
            self.assertNotIn("-k", command)
            self.assertEqual(command[command.index("--proto") + 1], "=https")
            target.write_bytes(content)
            return mock.Mock(stdout=None, stderr=b"200", returncode=0)

        with (
            mock.patch.object(MODULE.subprocess, "run", side_effect=downloader),
            mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No red")),
        ):
            MODULE.download(
                "https://static.crates.io/x",
                target,
                hashlib.sha256(content).hexdigest(),
                MODULE.time.monotonic() + 20,
            )
        self.assertEqual(target.read_bytes(), content)

    def test_sbom_muestra_c_embebido_y_no_lo_presenta_como_runtime_verificado(self):
        data = package([("sqlite3.c", b'#define SQLITE_VERSION "3.51.1"\n', tarfile.REGTYPE)])
        entry = crate(data)
        audit = MODULE.audit_sources([MODULE.inspect_crate(data, entry)])
        bom = MODULE.source_sbom([entry], audit)
        sqlite = next(c for c in bom["components"] if c["name"] == "SQLite")
        self.assertEqual(sqlite["version"], "3.51.1")
        self.assertEqual(sqlite["scope"], "optional")
        self.assertIn(
            {"name": "agencia:enlace_runtime_verificado", "value": "false"}, sqlite["properties"]
        )
        source = next(c for c in bom["components"] if c["type"] == "file")
        self.assertEqual(source["name"], "example-1.0.0/sqlite3.c")
        self.assertEqual(
            source["hashes"][0]["content"],
            hashlib.sha256(b'#define SQLITE_VERSION "3.51.1"\n').hexdigest(),
        )

    def test_vendor_conserva_directorios_vacios_y_acota_el_total(self):
        data = package([("empty", b"", tarfile.DIRTYPE), ("x", b"123", tarfile.REGTYPE)])
        item = MODULE.inspect_crate(data, crate(data))
        target = self.directory / "vendor.tar.gz"
        MODULE.write_vendor([item], target)
        with tarfile.open(target, "r:gz") as archive:
            self.assertTrue(archive.getmember("example-1.0.0/empty").isdir())
        with self.assertRaises(ValueError):
            MODULE.write_vendor([item], self.directory / "limited.tar.gz", max_bytes=2)

    def test_preparacion_publica_solo_recibo_completo_y_no_adopta_otro_destino(self):
        lock = b"lock exacto"
        source = package([("Cargo.lock", lock, tarfile.REGTYPE)], prefix="VectorChord-abc")
        archive = package([("src/lib.rs", b"pub fn f(){}", tarfile.REGTYPE)])
        manifest = {
            "vectorchord_source": {
                "revision": "abc",
                "url": "https://codeload.github.com/source",
                "sha256": hashlib.sha256(source).hexdigest(),
            },
            "cargo": {
                "lock_sha256": hashlib.sha256(lock).hexdigest(),
                "registry_crates": [crate(archive)],
            },
        }

        def fake_download(url, destination, expected, deadline):
            data = source if "codeload" in url else archive
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
            destination.write_bytes(data)
            return destination

        with (
            mock.patch.object(MODULE, "ROOT", self.directory),
            mock.patch.object(MODULE, "verify_root"),
            mock.patch.object(MODULE, "download", side_effect=fake_download),
        ):
            receipt = MODULE.prepare(manifest)
            target = Path(receipt["path"])
            self.assertTrue((target / "receipt.json").is_file())
            self.assertTrue((target / "source-sbom.cdx.json").is_file())
            self.assertFalse(receipt["sdk_executed"])
            self.assertFalse(receipt["build_enabled"])
            with self.assertRaises(ValueError):
                MODULE.prepare(manifest)


if __name__ == "__main__":
    unittest.main()
