"""Pruebas puras del inventario del SDK; no ejecutan SDK ni escáner."""

import base64
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "docker/postgres-immich/prepare_sdk_inventory.py"
SPEC = importlib.util.spec_from_file_location("sdk_inventory", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def record(name="libdemo", version="1.2-r0", origin="demo", license_name="MIT"):
    control = "Q1" + base64.b64encode(b"x" * 20).decode("ascii")
    return (
        f"P:{name}\nV:{version}\nA:x86_64\nC:{control}\n"
        f"L:{license_name}\no:{origin}\nU:https://example.org/\n"
    ).encode()


class InstalledTests(unittest.TestCase):
    def test_preserva_metadatos_y_no_confunde_archivos_con_cabecera(self):
        raw = record() + b"F:usr/lib\nR:libdemo.so\na:0:0:755\nF:usr/bin\nR:demo\n"
        row = MODULE.parse_installed(raw)["libdemo"]
        self.assertEqual((row["V"], row["o"], row["L"]), ("1.2-r0", "demo", "MIT"))

    def test_rechaza_utf8_invalido_y_nul(self):
        for suffix in (b"\xff", b"\x00"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                MODULE.parse_installed(record() + suffix)

    def test_rechaza_cabecera_y_paquete_duplicados(self):
        for raw in (record() + b"V:2-r0\n", record() + b"\n" + record()):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                MODULE.parse_installed(raw)

    def test_rechaza_identidad_o_arquitectura_incompletas(self):
        for raw in (
            record().replace(b"V:1.2-r0\n", b""),
            record().replace(b"x86_64", b"arm64"),
            record().replace(b"Q1", b"XX"),
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                MODULE.parse_installed(raw)

    def test_respeta_limite_de_entrada(self):
        with self.assertRaises(ValueError):
            MODULE.parse_installed(record(), maximum=10)

    def test_virtual_conserva_ausencia_de_licencia_y_origen(self):
        row = MODULE.parse_installed(record(".virtual", origin="", license_name=""))[".virtual"]
        component = MODULE.apk_component(row, "3.24.4", "base", None)
        self.assertNotIn("licenses", component)
        props = {p["name"]: p["value"] for p in component["properties"]}
        self.assertEqual(props["agencia:apk:license_raw"], "")
        self.assertEqual(props["agencia:apk:origin_raw"], "")
        self.assertNotIn("aquasecurity:trivy:SrcName", props)
        self.assertNotIn("hashes", component)  # Q1 no es el hash del archivo APK.


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.base = MODULE.parse_installed(record("base"))
        row = MODULE.parse_installed(record("extra"))["extra"]
        self.lock = {
            "packages": [
                {
                    "name": "extra",
                    "version": row["V"],
                    "arch": row["A"],
                    "license": row["L"],
                    "origin": row["o"],
                    "control_identity": row["C"],
                    "archive_sha256": "a" * 64,
                    "url": "https://dl-cdn.alpinelinux.org/example.apk",
                }
            ]
        }

    def test_union_y_comparacion_biyectiva(self):
        expected, _ = MODULE.expected_union(self.base, self.lock)
        result = MODULE.compare_packages(expected, copy.deepcopy(expected))
        self.assertTrue(result["matches"])
        self.assertEqual(result["compared_fields"], ["V", "A", "L", "o", "C"])

    def test_no_oculta_extra_ausente_ni_licencia_distinta(self):
        expected, _ = MODULE.expected_union(self.base, self.lock)
        actual = MODULE.parse_installed(
            record("base", license_name="GPL-2.0-only") + b"\n" + record("foreign")
        )
        result = MODULE.compare_packages(expected, actual)
        self.assertEqual(result["missing"], ["extra"])
        self.assertEqual(result["unexpected"], ["foreign"])
        self.assertEqual(result["different"][0]["field"], "L")
        self.assertFalse(result["matches"])

    def test_rechaza_colision_base_y_agregado(self):
        self.lock["packages"][0]["name"] = "base"
        with self.assertRaises(ValueError):
            MODULE.expected_union(self.base, self.lock)

    def test_licencia_literal_y_origen_para_alpine(self):
        row = MODULE.parse_installed(record(license_name="MIT AND BSD-2-Clause"))["libdemo"]
        result = MODULE.apk_component(row, "3.24.4", "added", self.lock["packages"][0])
        self.assertEqual(result["licenses"], [{"license": {"name": "MIT AND BSD-2-Clause"}}])
        props = {p["name"]: p["value"] for p in result["properties"]}
        self.assertEqual(props["aquasecurity:trivy:SrcName"], "demo")
        self.assertIn("arch=x86_64&distro=3.24.4", result["purl"])


class CaptureTests(unittest.TestCase):
    def setUp(self):
        raw = record()
        self.capture = {
            "schema_version": 1,
            "read_only": True,
            "sdk_started": False,
            "sdk_executed": False,
            "container_mutated": False,
            "container": {"pid": 0, "running": False, "no_foreign": True},
            "process": {"returncode": 0},
            "database_b64": base64.b64encode(raw).decode(),
            "database_sha256": hashlib.sha256(raw).hexdigest(),
            "database_bytes": len(raw),
        }

    def test_captura_ligada_a_sus_bytes(self):
        self.assertEqual(MODULE.capture_database(self.capture), record())

    def test_rechaza_hash_o_estado_activo(self):
        for change in (
            {"database_sha256": "0" * 64},
            {"sdk_executed": True},
            {"container": {"pid": 1, "running": True, "no_foreign": True}},
        ):
            data = copy.deepcopy(self.capture)
            data.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                MODULE.capture_database(data)

    def test_rechaza_booleano_como_retorno_cero(self):
        self.capture["process"]["returncode"] = False
        with self.assertRaises(ValueError):
            MODULE.capture_database(self.capture)


class CoverageTests(unittest.TestCase):
    def test_cobertura_exige_nombre_version_arch_y_origen(self):
        rows = MODULE.parse_installed(record())
        report = {
            "Metadata": {"OS": {"Family": "alpine", "Name": "3.24.4"}},
            "Results": [
                {
                    "Type": "alpine",
                    "Class": "os-pkgs",
                    "Packages": [
                        {
                            "Name": "libdemo",
                            "Version": "1.2-r0",
                            "Arch": "x86_64",
                            "SrcName": "demo",
                            "SrcVersion": "1.2-r0",
                        }
                    ],
                }
            ],
        }
        self.assertEqual(MODULE.validate_scan_coverage(report, rows)["packages"], 1)
        for mutation in (
            lambda p: p.clear(),
            lambda p: p.append(p[0]),
            lambda p: p[0].update(Arch="noarch"),
            lambda p: p[0].update(SrcName="foreign"),
        ):
            broken = copy.deepcopy(report)
            mutation(broken["Results"][0]["Packages"])
            with self.subTest(report=broken), self.assertRaises(ValueError):
                MODULE.validate_scan_coverage(broken, rows)

    def test_hallazgos_no_se_transforman_en_aprobacion(self):
        rows = MODULE.parse_installed(record())
        report = {
            "Metadata": {"OS": {"Family": "alpine", "Name": "3.24.4"}},
            "Results": [
                {
                    "Type": "alpine",
                    "Class": "os-pkgs",
                    "Packages": [
                        {
                            "Name": "libdemo",
                            "Version": "1.2-r0",
                            "Arch": "x86_64",
                            "SrcName": "demo",
                            "SrcVersion": "1.2-r0",
                        }
                    ],
                    "Vulnerabilities": [{"Severity": "HIGH"}],
                }
            ],
        }
        result = MODULE.validate_scan_coverage(report, rows)
        self.assertEqual(result["high_critical"], 1)
        self.assertFalse(result["approved"])

    def test_rechaza_os_omitido_o_distinto_aunque_haya_110_nombres(self):
        rows = MODULE.parse_installed(record())
        for os_info in (
            {},
            {"Family": "alpine", "Name": "3.20"},
            {"Family": "debian", "Name": "3.24.4"},
        ):
            report = {
                "Metadata": {"OS": os_info},
                "Results": [
                    {
                        "Type": "alpine",
                        "Class": "os-pkgs",
                        "Packages": [
                            {
                                "Name": "libdemo",
                                "Version": "1.2-r0",
                                "Arch": "x86_64",
                                "SrcName": "demo",
                                "SrcVersion": "1.2-r0",
                            }
                        ],
                    }
                ],
            }
            with self.subTest(os=os_info), self.assertRaises(ValueError):
                MODULE.validate_scan_coverage(report, rows)

    def test_sbom_solo_apk_y_os_no_inventa_rust(self):
        rows = MODULE.parse_installed(record())
        sbom = MODULE.make_sbom(rows, set(rows), {}, "3.24.4", "b" * 64)
        libraries = [c for c in sbom["components"] if c["type"] == "library"]
        self.assertEqual(len(libraries), 1)
        self.assertEqual(libraries[0]["name"], "libdemo")
        self.assertEqual(
            [c["name"] for c in sbom["components"] if c["type"] == "operating-system"], ["alpine"]
        )
        self.assertNotIn("trivy", json.dumps(sbom.get("metadata", {}).get("tools", [])))


class OciTests(unittest.TestCase):
    def fixture(self):
        layer_io = io.BytesIO()
        with tarfile.open(fileobj=layer_io, mode="w:") as layer:
            for name, content in (
                ("etc/alpine-release", b"3.24.4\n"),
                ("usr/lib/os-release", b"ID=alpine\nVERSION_ID=3.24.4\n"),
            ):
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                layer.addfile(entry, io.BytesIO(content))
            link = tarfile.TarInfo("etc/os-release")
            link.type = tarfile.SYMTYPE
            link.linkname = "../usr/lib/os-release"
            layer.addfile(link)
        plain = layer_io.getvalue()
        packed = gzip.compress(plain, mtime=0)

        def digest(value):
            return hashlib.sha256(value).hexdigest()

        config = json.dumps({"rootfs": {"diff_ids": ["sha256:" + digest(plain)]}}).encode()
        manifest = json.dumps(
            {
                "config": {"digest": "sha256:" + digest(config)},
                "layers": [{"digest": "sha256:" + digest(packed)}],
            }
        ).encode()
        index = json.dumps({"manifests": [{"digest": "sha256:" + digest(manifest)}]}).encode()
        outer_io = io.BytesIO()
        with tarfile.open(fileobj=outer_io, mode="w:") as outer:
            for name, content in [("index.json", index)] + [
                ("blobs/sha256/" + digest(b), b) for b in (config, manifest, packed)
            ]:
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                outer.addfile(entry, io.BytesIO(content))
        return outer_io.getvalue(), digest(manifest), digest(config), packed

    def test_diffid_de_capa_gzip_corresponde_a_bytes_descomprimidos(self):
        raw, manifest, config, _ = self.fixture()
        result = MODULE.base_os(raw, manifest, config)
        self.assertEqual((result["family"], result["version"]), ("alpine", "3.24.4"))
        self.assertEqual(result["layers_hashed"], 1)

    def test_rechaza_blob_o_subject_oci_alterado(self):
        raw, manifest, config, packed = self.fixture()
        broken = raw.replace(packed, packed[:-1] + bytes([packed[-1] ^ 1]))
        for content, subject in ((broken, manifest), (raw, "0" * 64)):
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                MODULE.base_os(content, subject, config)


if __name__ == "__main__":
    unittest.main()
