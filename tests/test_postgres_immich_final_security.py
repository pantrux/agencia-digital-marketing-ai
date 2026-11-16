"""Datos OCI/Cargo reales y fixtures; sin Trivy, SDK, HTTP o infraestructura."""

import importlib.util
import io
import struct
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "docker/postgres-immich/final_security.py"
SPEC = importlib.util.spec_from_file_location("pg_final_security_test", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
CLI_SPEC = importlib.util.spec_from_file_location(
    "pg_final_security_cli_test", PATH.with_name("audit_final_security.py")
)
C = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(C)


class FinalSecurityTests(unittest.TestCase):
    def reader(self):
        return M.load_oci_reader((PATH.parent / "final_image_remote.py").read_bytes())

    def test_complete_oci_reader_reads_fixture_tar_and_elf_without_execution(self):
        reader = self.reader()

        def archive(members):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as target:
                for name, raw in members.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(raw)
                    target.addfile(entry, io.BytesIO(raw))
            return stream.getvalue()

        layer = archive({"fixture": b"datos"})
        config = M.canonical(
            {
                "architecture": "amd64",
                "os": "linux",
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + M.digest(layer)]},
            }
        )

        def descriptor(raw):
            return {"digest": "sha256:" + M.digest(raw), "size": len(raw)}

        manifest = M.canonical(
            {
                "config": descriptor(config),
                "layers": [
                    {**descriptor(layer), "mediaType": "application/vnd.oci.image.layer.v1.tar"}
                ],
            }
        )
        raw = archive(
            {
                "index.json": M.canonical(
                    {"schemaVersion": 2, "manifests": [descriptor(manifest)]}
                ),
                "oci-layout": M.canonical({"imageLayoutVersion": "1.0.0"}),
                **{"blobs/sha256/" + M.digest(value): value for value in (layer, config, manifest)},
            }
        )
        image = reader.read_oci(raw, M.digest(manifest))
        self.assertEqual(image["config_digest"], "sha256:" + M.digest(config))
        self.assertEqual(image["files"]["fixture"]["sha256"], M.digest(b"datos"))
        self.assertEqual(reader.MAX_FILE, 512 * 1024**2)
        self.assertFalse(hasattr(reader, "main"))
        self.assertFalse(hasattr(reader, "execute"))
        with self.assertRaises(ValueError):
            reader.read_oci(raw, "0" * 64)

    def test_tar_duplicate_and_bad_elf_fail_in_loaded_parsers(self):
        reader = self.reader()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for _ in range(2):
                row = tarfile.TarInfo("same")
                row.size = 1
                archive.addfile(row, io.BytesIO(b"x"))
        with self.assertRaises(ValueError):
            reader.apply_layer({}, stream.getvalue())
        raw = bytearray(128)
        raw[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<HH", raw, 16, 3, 62)
        struct.pack_into("<Q", raw, 40, 64)
        struct.pack_into("<HH", raw, 58, 64, 1)
        self.assertEqual(reader.elf_info(raw)["needed"], [])
        with self.assertRaises(ValueError):
            reader.elf_info(raw[:70])
        with self.assertRaises(ValueError):
            M.load_oci_reader((PATH.parent / "final_image_remote.py").read_bytes() + b"\n")

    def test_cargo_bom_preserves_all_packages_and_separates_selection(self):
        inventory = {
            "registry": [
                {"name": name, "version": "1.0", "archive_sha256": "a" * 64, "selected": selected}
                for name, selected in [("used", True), ("not-used", False)]
            ]
        }
        bom = M.cargo_sbom(inventory)
        self.assertEqual(
            {row["purl"] for row in bom["components"]},
            {"pkg:cargo/used@1.0", "pkg:cargo/not-used@1.0"},
        )

    def test_report_bijection_rejects_empty_duplicate_or_wrong_package(self):
        expected = {("example", "1.0")}
        report = {
            "Results": [
                {
                    "Type": "cargo",
                    "Packages": [{"Name": "example", "Version": "1.0"}],
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-fixture",
                            "PkgName": "example",
                            "InstalledVersion": "1.0",
                            "Severity": "HIGH",
                        }
                    ],
                }
            ]
        }
        result = M.report_coverage(report, "cargo", expected)
        self.assertEqual(result["high_critical"], 1)
        self.assertEqual(len(result["findings"]), 1)
        for packages in (
            [],
            [{"Name": "other", "Version": "1.0"}],
            report["Results"][0]["Packages"] * 2,
        ):
            with self.subTest(packages=packages), self.assertRaises(ValueError):
                M.report_coverage(
                    {"Results": [{"Type": "cargo", "Packages": packages}]}, "cargo", expected
                )

    def test_osv_artifact_requires_exact_queries_complete_pages_and_preserves_unknown(self):
        bundle = {
            "ecosystem": "crates.io",
            "queries": 1,
            "omitted": 0,
            "pagination_exhausted": True,
            "results": [
                {
                    "name": "example",
                    "version": "1",
                    "pages": [
                        {
                            "vulns": [
                                {
                                    "id": "RUSTSEC-fixture",
                                    "aliases": ["CVE-fixture"],
                                    "affected": [
                                        {"package": {"ecosystem": "crates.io", "name": "example"}}
                                    ],
                                }
                            ]
                        }
                    ],
                    "pagination_exhausted": True,
                }
            ],
        }
        result = M.osv_coverage(bundle, {("example", "1")})
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["findings"][0]["raw"]["id"], "RUSTSEC-fixture")
        self.assertEqual(result["findings"][0]["raw"]["aliases"], ["CVE-fixture"])
        for changed in (
            {**bundle, "omitted": 1},
            {**bundle, "results": []},
            {**bundle, "queries": 0},
        ):
            with self.assertRaises(ValueError):
                M.osv_coverage(changed, {("example", "1")})

    def test_clean_os_cargo_never_approves_missing_native_annexes(self):
        result = M.assessment(
            {"high_critical": 0, "unknown": 0}, {"high_critical": 0, "unknown": 0}, None, {}
        )
        self.assertFalse(result["coverage_complete"])
        self.assertFalse(result["approved"])
        self.assertEqual(
            set(result["pending_annexes"]),
            {"postgresql", "pgvector", "vectorchord", "sqlite", "mimalloc"},
        )

    def test_osv_rejects_malformed_affected_and_non_list_vulns(self):
        for affected in ("not-an-array", [], ["not-an-object"]):
            bundle = {
                "ecosystem": "crates.io",
                "queries": 1,
                "omitted": 0,
                "pagination_exhausted": True,
                "results": [
                    {
                        "name": "example",
                        "version": "1",
                        "pages": [{"vulns": [{"id": "fixture", "affected": affected}]}],
                        "pagination_exhausted": True,
                    }
                ],
            }
            with self.subTest(affected=affected), self.assertRaises(ValueError):
                M.osv_coverage(bundle, {("example", "1")})

    def test_known_warning_is_preserved_and_unknown_or_error_rejected(self):
        warning = "Using severities from other vendors for some vulnerabilities. Read https://trivy.dev/docs/v0.76/guide/scanner/vulnerability#severity-selection for details."
        self.assertEqual(M.scan_warnings(("date\tWARN\t" + warning).encode()), [warning])
        for raw in (b"date\tWARN\tnew warning", b"date\tERROR\tfailure", b"date WARN unstructured"):
            with self.assertRaises(ValueError):
                M.scan_warnings(raw)

    def test_file_reader_bounds_pin_and_exclusive_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data"
            C.write_new(path, b"bounded")
            self.assertEqual(C.read_file(path, 7, M.digest(b"bounded")), b"bounded")
            with self.assertRaises(ValueError):
                C.read_file(path, 6)
            with self.assertRaises(ValueError):
                C.read_file(path, 7, "0" * 64)
            with self.assertRaises(FileExistsError):
                C.write_new(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"bounded")

    def test_supplement_refs_reject_env_and_outside_evidence(self):
        for name in (
            ".env",
            "../data",
            "docker/stack-artifacts.json",
            ".tools/evidence/../data.json",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                C.evidence_path(name)

    def test_default_plan_never_starts_a_scanner_or_opens_run(self):
        manifest, plan, refs = {}, {"ready_for_inventory": True}, {}
        with (
            mock.patch.object(C, "load", return_value=M),
            mock.patch.object(C, "prepare_plan", return_value=(manifest, plan, refs)),
            mock.patch.object(C, "scan") as scan,
            mock.patch.object(C, "open_run") as opened,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(C.main([]), 0)
            scan.assert_not_called()
            opened.assert_not_called()

    def test_scan_rejects_missing_external_inventory_pin_before_dispatch(self):
        plan = {"ready_for_inventory": True}
        with (
            mock.patch.object(C, "load", return_value=M),
            mock.patch.object(C, "prepare_plan", return_value=({}, plan, {})),
            mock.patch.object(C, "scan") as scan,
        ):
            with self.assertRaisesRegex(ValueError, "INVENTORY_BACKUP_PIN"):
                C.main(["scan", "--expected-plan-sha256", M.digest(M.canonical(plan))])
            scan.assert_not_called()

    def test_sdk_gates_reject_other_birth_or_epoch_even_with_positive_hash(self):
        owner = "agencia-digital-marketing-ai:postgres-immich-security-rebuild:v7"
        container = {
            "cid": "c" * 64,
            "created": "birth",
            "started_at": "epoch",
            "image_id": "sha256:base",
            "no_foreign": True,
            "pid": 12,
            "running": True,
        }

        def envelope(stage, state):
            row = {
                "owner": owner,
                "stage": stage,
                "success": True,
                "profile_sha256": M.PROFILE_SHA,
                "container": state,
            }
            if stage == "stop":
                row["cleanup"] = {"stopped": True}
            return {"receipt": row, "receipt_sha256": M.digest(M.canonical(row))}

        gates = {
            "v7_restart": envelope("restart", container),
            "v7_catalog": envelope("catalog", container),
            "v7_stop": envelope("stop", {**container, "running": False, "pid": 0}),
        }
        M.sdk_gates(gates)
        for field in ("created", "started_at", "cid"):
            changed = dict(gates)
            changed["v7_stop"] = envelope(
                "stop", {**container, "running": False, "pid": 0, field: "other"}
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                M.sdk_gates(changed)

    def test_fetch_binding_requires_physical_owner_marker_and_same_inode(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "runtime-image.tar"
            archive.write_bytes(b"tar-fixture")
            info = root.stat()
            marker = {
                "owner": "fixture-owner",
                "path": str(root),
                "directory": {"device": info.st_dev, "inode": info.st_ino},
            }
            (root / ".owner.json").write_bytes(M.canonical(marker))
            subject = {
                "image_id": "image",
                "config_digest": "config",
                "archive": {
                    "sha256": M.digest(archive.read_bytes()),
                    "size": archive.stat().st_size,
                },
            }
            fetch = {
                "directory": marker,
                "local": str(archive),
                "owner": "fixture-owner",
                "success": True,
                "archive": subject["archive"],
                "header": {
                    "image_id": "image",
                    "config_digest": "config",
                    "archive": subject["archive"],
                },
                "footer": {
                    "success": True,
                    "record_unchanged": True,
                    "image_executed": False,
                    "sdk_executed": False,
                    "archive": subject["archive"],
                },
            }
            C.validate_fetch(M, archive, subject, fetch)
            (root / ".owner.json").write_bytes(M.canonical({**marker, "owner": "foreign"}))
            with self.assertRaises(ValueError):
                C.validate_fetch(M, archive, subject, fetch)

    def test_scanner_job_result_preserved_before_incomplete_process_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = types.SimpleNamespace(
                status="timeout",
                exit_code=None,
                stdout=b"partial",
                stderr=b"failure",
                cleanup_complete=True,
                elapsed_seconds=1.0,
                error_operation=None,
                error_code=None,
            )
            C.save_job(M, root, "os", result)
            event = M.json_object((root / "os.job.json").read_bytes())
            self.assertEqual(event["status"], "timeout")
            self.assertEqual((root / "os.trivy.json").read_bytes(), b"partial")
            self.assertFalse(event["success"])

    def test_report_rejects_finding_for_a_package_outside_inventory(self):
        report = {
            "Results": [
                {
                    "Type": "cargo",
                    "Packages": [{"Name": "example", "Version": "1"}],
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-fixture",
                            "PkgName": "foreign",
                            "InstalledVersion": "1",
                            "Severity": "HIGH",
                        }
                    ],
                }
            ]
        }
        with self.assertRaises(ValueError):
            M.report_coverage(report, "cargo", {("example", "1")})

    def test_native_raw_high_is_preserved_even_when_root_classifies_it(self):
        annexes = {
            name: {
                "assessment": "evaluated_by_root",
                "raw_findings": [],
                "unresolved_advisories": [],
                "unresolved_high_critical": 0,
            }
            for name in M.ANNEXES
        }
        annexes["sqlite"]["raw_findings"] = [
            {
                "id": "CVE-cli-fixture",
                "severity": "HIGH",
                "classification": "CLI ausente; evidencia requerida",
            }
        ]
        result = M.assessment({}, {}, {"high_critical": 0}, annexes)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["raw_high_critical"], 1)
        self.assertFalse(result["approved"])

    def test_hasher_keeps_ctime_signatures_separate_and_rejects_change(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data"
            path.write_bytes(b"fixture")
            native = C.os.fstat

            def clock(fd, changed=False):
                row = native(fd)
                return types.SimpleNamespace(
                    **{
                        name: getattr(row, name)
                        for name in (
                            "st_dev",
                            "st_ino",
                            "st_size",
                            "st_mtime_ns",
                            "st_nlink",
                            "st_mode",
                        )
                    },
                    st_ctime_ns=42 if not changed else 43,
                )

            with mock.patch.object(C.os, "fstat", side_effect=clock):
                self.assertEqual(C.file_hash(path, 10)["sha256"], M.digest(b"fixture"))
            calls = 0

            def mutation(fd):
                nonlocal calls
                calls += 1
                return clock(fd, calls > 1)

            with (
                mock.patch.object(C.os, "fstat", side_effect=mutation),
                self.assertRaisesRegex(ValueError, "FILE_CHANGED"),
            ):
                C.file_hash(path, 10)

    def test_changed_inventory_is_rejected_before_scanner_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "inventory.json").write_bytes(M.canonical({"subject": "fixture"}))
            with (
                mock.patch.object(C, "open_run", return_value=root),
                mock.patch.object(C, "load") as loader,
            ):
                with self.assertRaisesRegex(ValueError, "FILE_PIN"):
                    C.scan(M, {}, {"subject": "fixture"}, "0" * 64)
                loader.assert_not_called()

    def test_receipt_uses_producer_ascii_codec_and_rejects_declared_success(self):
        row = {
            "stage": "catalog",
            "owner": "fixture",
            "success": True,
            "scope": "Sólo datos, sin compilación",
        }
        envelope = {"receipt": row, "receipt_sha256": M.digest(M.canonical(row))}
        self.assertEqual(M.positive(envelope, "catalog", "fixture"), row)
        self.assertIn(b"\\u00f3", M.canonical(row))
        row["success"] = 1
        envelope["receipt_sha256"] = M.digest(M.canonical(row))
        with self.assertRaises(ValueError):
            M.positive(envelope, "catalog", "fixture")

    def test_cargo_rejects_git_source_before_forming_an_inventory(self):
        raw = b'[[package]]\nname="fixture"\nversion="1.0"\nsource="git+https://example.invalid/source"\n'
        with self.assertRaisesRegex(ValueError, "NON_REGISTRY_SOURCE"):
            M.cargo_inventory({}, raw, {}, {})

    def test_osv_rejects_unfinished_pagination_and_another_package(self):
        row = {
            "name": "example",
            "version": "1",
            "pagination_exhausted": True,
            "pages": [{"next_page_token": "not-finished"}],
        }
        bundle = {
            "ecosystem": "crates.io",
            "queries": 1,
            "omitted": 0,
            "pagination_exhausted": True,
            "results": [row],
        }
        with self.assertRaisesRegex(ValueError, "OSV_PAGE_EXHAUSTION"):
            M.osv_coverage(bundle, {("example", "1")})
        row["pages"] = [
            {
                "vulns": [
                    {
                        "id": "fixture",
                        "affected": [{"package": {"ecosystem": "crates.io", "name": "foreign"}}],
                    }
                ]
            }
        ]
        with self.assertRaisesRegex(ValueError, "OSV_ADVISORY_PACKAGE"):
            M.osv_coverage(bundle, {("example", "1")})


if __name__ == "__main__":
    unittest.main()
