"""Inventario nativo como datos; ninguna consulta, scanner o importación ELF."""

import base64
import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load():
    path = ROOT / "docker/kitsu-zou/inventory_opencv_wheel.py"
    module = types.ModuleType("opencv_inventory_fixture")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), vars(module))
    return module


def tar_bytes(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def apk_fixture():
    payload = gzip.compress(tar_bytes([("usr/lib/provider.so", b"provider")]), mtime=0)
    control = gzip.compress(
        tar_bytes(
            [
                (
                    ".PKGINFO",
                    (
                        "pkgname = package\npkgver = 1.0-r0\narch = x86_64\nlicense = MIT\ndatahash = "
                        + hashlib.sha256(payload).hexdigest()
                        + "\n"
                    ).encode(),
                )
            ]
        ),
        mtime=0,
    )
    return control + payload, {
        "P": "package",
        "V": "1.0-r0",
        "A": "x86_64",
        "L": "MIT",
        "C": "Q1" + base64.b64encode(hashlib.sha1(control).digest()).decode(),
    }


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_zip_cerrado_verifica_bytes_y_rechaza_duplicados_o_paths(self):
        def make(names):
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as z:
                for name in names:
                    z.writestr(name, b"data")
            return out.getvalue()

        expected = {"folder/a": {"sha256": hashlib.sha256(b"data").hexdigest(), "bytes": 4}}
        self.assertEqual(self.m.read_members(make(["folder/a"]), expected), {"folder/a": b"data"})
        for names in (["folder/a", "extra"], ["../a"]):
            with self.assertRaises(ValueError):
                self.m.read_members(make(names), expected)
        bad = {"folder/a": {"sha256": "0" * 64, "bytes": 4}}
        with self.assertRaises(ValueError):
            self.m.read_members(make(["folder/a"]), bad)

    def test_record_rechaza_omision_hash_falso_y_fila_duplicada(self):
        files = {"a": b"data", "dist/RECORD": b""}
        digest = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(b"data").digest()
        ).decode().rstrip("=")
        good = f"a,{digest},4\ndist/RECORD,,\n".encode()
        self.m.validate_record({**files, "dist/RECORD": good}, "dist")
        for raw in (
            good.replace(b",4", b",3"),
            b"dist/RECORD,,\n",
            good + good.splitlines()[0] + b"\n",
        ):
            with self.assertRaises(ValueError):
                self.m.validate_record({**files, "dist/RECORD": raw}, "dist")

    def test_metadata_identidad_propia_no_se_confunde_con_pypi(self):
        raw = b'Metadata-Version: 2.4\nName: opencv-python-headless\nVersion: 4.13.0.92+agency1\nRequires-Python: >=3.12,<3.13\nRequires-Dist: numpy<2.0; python_version < "3.9"\nRequires-Dist: numpy>=2; python_version >= "3.9"\n\n'
        result = self.m.wheel_metadata(raw)
        self.assertEqual(result["version"], "4.13.0.92+agency1")
        self.assertFalse(result["official_pypi_artifact"])
        with self.assertRaises(ValueError):
            self.m.wheel_metadata(raw.replace(b"+agency1", b""))
        with self.assertRaises(ValueError):
            self.m.wheel_metadata(raw.replace(b"\nVersion:", b"\nName: duplicate\nVersion:"))

    def test_apk_por_bytes_control_q1_datahash_sin_fingir_firma_individual(self):
        raw, record = apk_fixture()
        files, proof = self.m.apk_payload(raw, record)
        self.assertEqual(files["usr/lib/provider.so"], b"provider")
        self.assertTrue(proof["control_q1_matches"])
        self.assertFalse(proof["fresh_signature_verification"])
        for changed in ({**record, "P": "other"}, {**record, "C": "Q1wrong"}):
            with self.assertRaises(ValueError):
                self.m.apk_payload(raw, changed)
        with self.assertRaises(ValueError):
            self.m.apk_payload(raw + b"extra", record)
        with self.assertRaises(ValueError):
            self.m.apk_payload(raw[:-4], record)

    def test_tar_rechaza_traversal_duplicados_y_no_extrae(self):
        for entries in ([("../a", b"x")], [("same", b"x"), ("same", b"x")]):
            with (
                patch.object(
                    tarfile.TarFile, "extractall", side_effect=AssertionError("extracción")
                ),
                self.assertRaises(ValueError),
            ):
                self.m.tar_files(tar_bytes(entries))

    def test_clausura_distingue_bundled_externo_y_bytes_faltantes(self):
        bundled = {"core": {"soname": "core.so", "needed": ["external.so", "libc.so.6"]}}
        provider = {"soname": "external.so", "needed": [], "sha256": "a" * 64, "package": "package"}
        result = self.m.resolve_dependencies(
            bundled, [provider], {"libc.so.6": [{"name": "glibc", "version": "exact"}]}
        )
        self.assertEqual(result["external_confirmed"], ["external.so"])
        self.assertEqual(result["unconfirmed"][0]["soname"], "libc.so.6")
        self.assertFalse(result["runtime_environment_verified"])
        with self.assertRaises(ValueError):
            self.m.resolve_dependencies(bundled, [provider, {**provider, "sha256": "b" * 64}], {})

    def test_osv_request_exacta_commit_sin_ejecutar_y_paginacion_obligatoria(self):
        query = self.m.osv_request("f" * 40)
        self.assertEqual(query["body"], {"commit": "f" * 40})
        self.assertEqual(query["endpoint"], "https://api.osv.dev/v1/query")
        self.assertFalse(query["executed"])
        self.assertTrue(query["pagination"]["require_exhausted"])
        with self.assertRaises(ValueError):
            self.m.osv_request("bad")

    def test_plan_default_y_publicacion_sin_hash_no_tienen_efectos(self):
        with (
            patch.object(self.m, "build_plan", return_value={"fixture": True}),
            patch.object(self.m, "publish", side_effect=AssertionError("publicación")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(self.m.main([]), 0)
            self.assertEqual(self.m.main(["--publish"]), 1)

    def test_pines_de_spec_y_aprobacion_no_se_convierten_en_aprobacion_wheel(self):
        spec = json.loads((ROOT / self.m.SPEC_PATH).read_bytes())
        self.m.validate_spec(spec)
        for change in ({"security_approved": True}, {"output_relative": "../foreign"}):
            with self.assertRaises(ValueError):
                self.m.validate_spec({**spec, **change})
        bad = json.loads(json.dumps(spec))
        bad["references"]["wheel"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.m.validate_spec(bad)
        bad = json.loads(json.dumps(spec))
        bad["references"]["source_archive"]["path"] = ".env"
        with self.assertRaises(ValueError):
            self.m.validate_spec(bad)

    def test_python_smoke_exige_bytes_y_atestacion_de_misma_version(self):
        manifest = {
            "python": {"version": "3.12.16", "package_version": "3.12.16-r0"},
            "sdk_entrypoints": [
                {
                    "package": "python-3.12-base",
                    "version": "3.12.16-r0",
                    "path": "/usr/bin/python3.12",
                    "resolved_member": "usr/bin/python3.12",
                    "sha256": hashlib.sha256(b"python").hexdigest(),
                }
            ],
        }
        archive = {"name": "python-3.12-base", "version": "3.12.16-r0"}
        approval = {
            "tools": [
                {"path": "/usr/bin/python3.12", "sha256": hashlib.sha256(b"python").hexdigest()}
            ]
        }
        result = self.m.python_smoke_identity(
            manifest, approval, archive, {"usr/bin/python3.12": b"python"}
        )
        self.assertEqual(result["version"], "3.12.16")
        self.assertFalse(result["executed"])
        with self.assertRaises(ValueError):
            self.m.python_smoke_identity(
                manifest, approval, archive, {"usr/bin/python3.12": b"different"}
            )
        with self.assertRaises(ValueError):
            self.m.python_smoke_identity(
                manifest,
                approval,
                {**archive, "version": "3.12.15-r0"},
                {"usr/bin/python3.12": b"python"},
            )

    def test_publicacion_excl_solo_datos_y_rechaza_destino_existente(self):
        access = types.SimpleNamespace(check_access=lambda *_args, **_kwargs: None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new"
            plan = {"owner": self.m.OWNER, "output_root": str(root)}
            documents = {
                "inventory.json": b"{}",
                "wheel-source.cdx.json": b"{}",
                "osv-requests.json": b"{}",
            }
            result = self.m.publish_documents(root, plan, documents, access)
            self.assertFalse(result["security_approved"])
            self.assertEqual((root / "inventory.json").read_bytes(), b"{}")
            self.assertEqual(
                set(p.name for p in root.iterdir()),
                set(documents) | {".owner.json", "plan.json", "receipt.json"},
            )
            with self.assertRaises(FileExistsError):
                self.m.publish_documents(root, plan, documents, access)
            self.assertEqual((root / "inventory.json").read_bytes(), b"{}")

    def test_publicacion_rechaza_nombres_inyectados_antes_de_crear(self):
        access = types.SimpleNamespace(check_access=lambda *_args, **_kwargs: None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new"
            with self.assertRaises(ValueError):
                self.m.publish_documents(
                    root, {"output_root": str(root)}, {"../foreign": b"x"}, access
                )
            self.assertFalse(os.path.lexists(root))

    def test_hash_del_plan_distinto_no_crea_ni_adopta_salida(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new"
            plan = {"owner": self.m.OWNER, "output_root": str(root)}
            with (
                patch.object(self.m, "prepare_documents", return_value=(plan, {}, None, None)),
                patch.object(self.m, "publish_documents", side_effect=AssertionError("escritura")),
            ):
                with self.assertRaises(ValueError):
                    self.m.publish("0" * 64)
            self.assertFalse(os.path.lexists(root))

    def test_recibo_positivo_no_se_escribe_si_la_relectura_de_datos_falla(self):
        access = types.SimpleNamespace(check_access=lambda *_args, **_kwargs: None)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "new"
            documents = {name: b"{}" for name in self.m.DOCUMENTS}
            with self.assertRaises(ValueError):
                self.m.publish_documents(
                    root,
                    {"owner": self.m.OWNER, "output_root": str(root)},
                    documents,
                    access,
                    reader=lambda _path, _max: b"alterado",
                )
            self.assertTrue((root / "inventory.json").exists())
            self.assertFalse((root / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
