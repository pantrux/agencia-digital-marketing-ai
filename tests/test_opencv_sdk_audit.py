"""Auditoría de datos SDK: TAR, identidades y cobertura; sin SSH, Trivy ni SDK."""

import contextlib
import copy
import importlib.util
import io
import json
import os
import py_compile
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "docker/kitsu-zou" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_inputs():
    profile = json.loads((ROOT / "docker/kitsu-zou/opencv-minimal-sdk-profile.json").read_bytes())
    manifest = json.loads((ROOT / "docker/kitsu-zou/opencv-minimal-artifacts.json").read_bytes())
    return profile, manifest


def database(profile):
    records = {name: row for name, row in profile["base_packages"].items()}
    records.update({row["name"]: row["record"] for row in profile["managed_packages"]})
    return (
        "\n\n".join(
            "\n".join(
                f"{key}:{value}" for key, value in row.items() if key in {"P", "V", "A", "L", "o"}
            )
            for row in records.values()
        ).encode()
        + b"\n"
    )


def header_records(manifest):
    # Licencia de fixture: la ejecución real exige los registros del índice autenticado.
    return [
        {"P": row["name"], "V": row["version"], "A": "x86_64", "L": "fixture-license"}
        for row in manifest["direct_headers"]
    ]


def tar_bytes(
    *,
    linked=False,
    duplicate=False,
    bad_link=False,
    unrelated_links=True,
    merged=False,
    lib_target="usr/lib",
    also_alias=False,
    duplicate_lib=False,
):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as package:
        database_name = "usr/lib/apk/db/installed" if merged else "lib/apk/db/installed"
        entries = [(database_name, b"P:a\nV:1\nA:x86_64\nL:MIT\n")]
        if merged and lib_target is not None:
            link = tarfile.TarInfo("lib")
            link.type, link.linkname = tarfile.SYMTYPE, lib_target
            package.addfile(link)
            if duplicate_lib:
                package.addfile(link)
        if also_alias:
            entries.append(("lib/apk/db/installed", b"P:a\nV:1\nA:x86_64\nL:MIT\n"))
        if linked:
            link = tarfile.TarInfo("etc/os-release")
            link.type, link.linkname = (
                tarfile.SYMTYPE,
                "../../fuera" if bad_link else "../usr/lib/os-release",
            )
            package.addfile(link)
            entries.append(("usr/lib/os-release", b'ID=wolfi\nVERSION_ID="20230201"\n'))
        else:
            entries.append(("etc/os-release", b'ID=wolfi\nVERSION_ID="20230201"\n'))
        if duplicate:
            entries.append(("./lib/apk/db/installed", b"P:duplicado\n"))
        if unrelated_links:
            link = tarfile.TarInfo("usr/bin/legitimo")
            link.type, link.linkname = tarfile.SYMTYPE, "/bin/herramienta"
            package.addfile(link)
        for name, data in entries:
            row = tarfile.TarInfo(name)
            row.size = len(data)
            package.addfile(row, io.BytesIO(data))
    return target.getvalue()


class TarMetadata(unittest.TestCase):
    def test_usrmerge_exige_lib_usr_lib_y_no_admite_las_dos_db(self):
        remote = load("opencv_sdk_audit_remote")
        raw = tar_bytes(merged=True)
        value = remote.tar_metadata(io.BytesIO(raw), len(raw))
        self.assertEqual(value["database_path"], "usr/lib/apk/db/installed")
        for raw in (
            tar_bytes(merged=True, lib_target="../usr/lib"),
            tar_bytes(merged=True, also_alias=True),
            tar_bytes(merged=True, lib_target=None),
            tar_bytes(merged=True, duplicate_lib=True),
        ):
            with self.assertRaises(ValueError):
                remote.tar_metadata(io.BytesIO(raw), len(raw))

    def test_regular_y_enlace_relativo_acotado_sin_rechazar_otros_enlaces(self):
        remote = load("opencv_sdk_audit_remote")
        for linked in (False, True):
            raw = tar_bytes(linked=linked)
            value = remote.tar_metadata(io.BytesIO(raw), len(raw))
            self.assertIn(b"P:a", value["database"])
            self.assertIn(b"ID=wolfi", value["os_release"])
            self.assertEqual(
                value["os_release_path"], "usr/lib/os-release" if linked else "etc/os-release"
            )

    def test_duplicados_link_fuera_y_basura_final_rechazados(self):
        remote = load("opencv_sdk_audit_remote")
        for raw in (
            tar_bytes(duplicate=True),
            tar_bytes(linked=True, bad_link=True),
            tar_bytes() + b"alterado",
        ):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                remote.tar_metadata(io.BytesIO(raw), len(raw))

    def test_tar_truncado_y_fichero_objetivo_excesivo_rechazan(self):
        remote = load("opencv_sdk_audit_remote")
        raw = tar_bytes()
        with self.assertRaises(ValueError):
            remote.tar_metadata(io.BytesIO(raw[:2048]), 2048)
        with self.assertRaises(ValueError):
            remote.tar_metadata(io.BytesIO(raw), len(raw), database_limit=8)


class InstalledCoverage(unittest.TestCase):
    def test_origen_omitido_se_conserva_desde_registro_autenticado_no_desde_subpaquete(self):
        sbom = load("opencv_sdk_sbom")
        profile, _ = public_inputs()
        expected = {
            **profile["base_packages"],
            **{r["name"]: r["record"] for r in profile["managed_packages"]},
        }
        name, record = next(
            (name, row) for name, row in expected.items() if row.get("o", name) != name
        )
        blocks = database(profile).decode().strip().split("\n\n")
        stripped = [
            "\n".join(line for line in block.splitlines() if not line.startswith("o:"))
            if "P:" + name in block.splitlines()
            else block
            for block in blocks
        ]
        found = {
            row["P"]: row
            for row in sbom.installed_packages("\n\n".join(stripped).encode(), profile)
        }
        self.assertEqual(found[name]["o"], record["o"])
        self.assertEqual(found[name]["_origin_provenance"], "indice-autenticado")

    def test_89_instalados_distintos_de_2_headers_y_numpy_parciales(self):
        sbom = load("opencv_sdk_sbom")
        profile, manifest = public_inputs()
        installed = sbom.installed_packages(database(profile), profile)
        self.assertEqual(len(installed), 89)
        value = sbom.make_sbom(
            installed, b'ID=wolfi\nVERSION_ID="20230201"\n', manifest, header_records(manifest)
        )
        apk = [c for c in value["components"] if c["purl"].startswith("pkg:apk/")]
        self.assertEqual(len(apk), 91)
        scopes = [
            dict((p["name"], p["value"]) for p in c["properties"])["agencia:scope"] for c in apk
        ]
        self.assertEqual(scopes.count("instalado-verificado"), 89)
        self.assertEqual(scopes.count("headers-parciales-verificados"), 2)
        self.assertTrue(all(c.get("licenses") for c in value["components"]))

    def test_omitido_extra_version_o_duplicado_no_son_inventario_instalado_valido(self):
        sbom = load("opencv_sdk_sbom")
        profile, _ = public_inputs()
        raw = database(profile)
        for changed in (
            raw.split(b"\n\n", 1)[1],
            raw + b"\n\nP:extra\nV:1\nA:x86_64\nL:MIT\n",
            raw.replace(b"V:", b"V:alterado-", 1),
            raw + b"\n\n" + raw.split(b"\n\n", 1)[0],
        ):
            with self.subTest(size=len(changed)), self.assertRaises(ValueError):
                sbom.installed_packages(changed, profile)

    def test_os_release_no_se_inventa_y_campos_duplicados_rechazan(self):
        sbom = load("opencv_sdk_sbom")
        for raw in (
            b"ID=debian\nVERSION_ID=1\n",
            b"ID=wolfi\nID=wolfi\nVERSION_ID=1\n",
            b"ID=wolfi\n",
        ):
            with self.assertRaises(ValueError):
                sbom.os_identity(raw)

    def test_bijeccion_trivy_y_findings_completos_sin_ocultar_high(self):
        sbom = load("opencv_sdk_sbom")
        profile, manifest = public_inputs()
        bom = sbom.make_sbom(
            sbom.installed_packages(database(profile), profile),
            b"ID=wolfi\nVERSION_ID=20230201\n",
            manifest,
            header_records(manifest),
        )
        packages = [{"Name": c["name"], "Version": c["version"]} for c in bom["components"]]
        report = {
            "Results": [
                {
                    "Class": "os-pkgs",
                    "Type": "wolfi",
                    "Packages": packages[:-1],
                    "Vulnerabilities": [{"Severity": "HIGH", "VulnerabilityID": "CVE-fixture"}],
                },
                {
                    "Class": "lang-pkgs",
                    "Type": "python-pkg",
                    "Packages": packages[-1:],
                    "Vulnerabilities": [{"Severity": "MEDIUM", "VulnerabilityID": "CVE-medium"}],
                },
            ]
        }
        assessment = sbom.assess_report(report, bom)
        self.assertEqual(len(assessment["high_critical"]), 1)
        self.assertEqual(assessment["all_findings_count"], 2)
        broken = copy.deepcopy(report)
        broken["Results"][0]["Packages"].pop()
        with self.assertRaises(ValueError):
            sbom.assess_report(broken, bom)
        broken = copy.deepcopy(report)
        broken["Results"][0]["Packages"].append(packages[0])
        with self.assertRaises(ValueError):
            sbom.assess_report(broken, bom)


class AuditBoundary(unittest.TestCase):
    def test_load_ejecuta_bytes_verificados_y_no_pyc_obsoleto(self):
        local = load("audit_opencv_sdk")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.py"
            old, new = b"VALUE = 'old'\n", b"VALUE = 'new'\n"
            path.write_bytes(old)
            info = path.stat()
            py_compile.compile(str(path), doraise=True)
            path.write_bytes(new)
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            result = local.load("audit_only_fixture", path, local.digest(new))
            self.assertEqual(result.VALUE, "new")

    def test_scan_exige_sha_de_captura_respaldado_antes_de_reutilizarla(self):
        local = load("audit_opencv_sdk")
        data = b'{"metadatos":"verificados"}'
        local.require_capture(data, local.digest(data))
        for wanted in (None, "CHANGE_ME", "0" * 64):
            with self.assertRaises(ValueError):
                local.require_capture(data, wanted)

    def test_plan_no_lee_boveda_ni_abre_ssh_o_job(self):
        local = load("audit_opencv_sdk")
        with (
            patch.object(local, "bind_audit", side_effect=AssertionError("no bóveda")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(local.main([]), 0)
        self.assertFalse(json.loads(output.getvalue())["infrastructure_executed"])

    def test_nativejob_timeout_salida_o_cleanup_incompleto_no_aprueban(self):
        local = load("audit_opencv_sdk")
        good = SimpleNamespace(
            status="completed",
            exit_code=0,
            stdout=b"",
            stderr=b"INFO scanner",
            cleanup_complete=True,
        )
        local.validate_job(good)
        for changes in (
            {"status": "timeout"},
            {"cleanup_complete": False},
            {"exit_code": 1},
            {"stderr": b"WARN formato descartado"},
        ):
            candidate = SimpleNamespace(**{**vars(good), **changes})
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                local.validate_job(candidate)

    def test_db_vencida_o_future_no_habilita_scan(self):
        local = load("audit_opencv_sdk")
        now = datetime.now(UTC)
        good = {
            "Version": 2,
            "UpdatedAt": (now - timedelta(hours=1)).isoformat(),
            "NextUpdate": (now + timedelta(hours=1)).isoformat(),
        }
        local.validate_db(good, now)
        for changed in (
            {**good, "NextUpdate": (now - timedelta(seconds=1)).isoformat()},
            {**good, "UpdatedAt": (now + timedelta(seconds=1)).isoformat()},
        ):
            with self.assertRaises(ValueError):
                local.validate_db(changed, now)


if __name__ == "__main__":
    unittest.main()
