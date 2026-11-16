"""Contratos de datos y transporte simulado; nunca ejecuta Trivy."""

import copy
import hashlib
import io
import json
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "docker/valkey/scan_os_packages.py"
M = types.ModuleType("valkey_os_sca_tests")
M.__file__ = str(PATH)
exec(compile(PATH.read_bytes(), str(PATH), "exec"), M.__dict__)


def fixture():
    packages, components = (
        [],
        [
            {
                "type": "operating-system",
                "name": "alpine",
                "version": "3.24.3",
                "bom-ref": "os:alpine",
            }
        ],
    )
    for number in range(23):
        name = "lib-" + str(number)
        row = {
            "name": name,
            "version": "1.0-r0",
            "architecture": "x86_64",
            "source_name": "source-" + str(number),
            "source_version": "1.0-r0",
        }
        packages.append(row)
        purl = "pkg:apk/alpine/" + name + "@1.0-r0?arch=x86_64&distro=3.24.3"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": row["version"],
                "purl": purl,
                "bom-ref": purl,
                "properties": [
                    {"name": "aquasecurity:trivy:SrcName", "value": row["source_name"]},
                    {"name": "aquasecurity:trivy:SrcVersion", "value": row["source_version"]},
                    {"name": "aquasecurity:trivy:Arch", "value": row["architecture"]},
                ],
            }
        )
    inventory = {
        "os": {"identity": {"ID": "alpine", "VERSION_ID": "3.24.3"}, "packages": packages},
        "binary_accepted": False,
        "binary_sca_verified": False,
        "os_sca_verified": False,
    }
    return inventory, {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "components": components,
    }


def report_for(expected, levels=()):
    rows = [
        {
            "ID": row["name"] + "@" + row["version"],
            "Name": row["name"],
            "Version": row["version"],
            "Arch": row["architecture"],
            "SrcName": row["source_name"],
            "SrcVersion": row["source_version"],
            "Identifier": {"PURL": row["purl"]},
        }
        for row in expected
    ]
    first = rows[0]
    return {
        "SchemaVersion": 2,
        "Metadata": {"OS": {"Family": "alpine", "Name": "3.24.3"}},
        "Results": [
            {
                "Class": "os-pkgs",
                "Type": "alpine",
                "Packages": rows,
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "FIXTURE-" + level,
                        "PkgID": first["ID"],
                        "PkgName": first["Name"],
                        "InstalledVersion": first["Version"],
                        "PkgIdentifier": copy.deepcopy(first["Identifier"]),
                        "DataSource": {"ID": "alpine"},
                        "Severity": level,
                    }
                    for level in levels
                ],
            }
        ],
    }


class InputAndReportTests(unittest.TestCase):
    def expected(self):
        return M.validate_inputs(*fixture())

    def test_inventory_sbom_and_report_have_exactly_23_source_bound_packages(self):
        expected = self.expected()
        result = M.validate_report(report_for(expected), expected)
        self.assertEqual(result["packages"], 23)
        self.assertEqual(result["findings"], 0)
        self.assertTrue(result["os_policy_passed"])

    def test_input_os_count_duplicate_arch_purl_and_source_mismatches_reject(self):
        for variant in ("os", "count", "duplicate", "arch", "purl", "source", "property", "extra"):
            inventory, bom = fixture()
            if variant == "os":
                inventory["os"]["identity"]["VERSION_ID"] = "3.23.0"
            elif variant == "count":
                inventory["os"]["packages"].pop()
            elif variant == "duplicate":
                inventory["os"]["packages"][-1] = inventory["os"]["packages"][0]
            elif variant == "arch":
                inventory["os"]["packages"][0]["architecture"] = "aarch64"
            elif variant == "purl":
                bom["components"][1]["purl"] += "&unexpected=true"
            elif variant == "source":
                bom["components"][1]["properties"][0]["value"] = "unrelated"
            elif variant == "property":
                bom["components"][1]["properties"].append(bom["components"][1]["properties"][0])
            else:
                bom["components"].append(copy.deepcopy(bom["components"][1]))
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                M.validate_inputs(inventory, bom)

    def test_report_requires_exact_os_origin_and_bijection(self):
        expected = self.expected()
        for variant in (
            "os",
            "type",
            "class",
            "missing",
            "extra",
            "duplicate",
            "purl",
            "src",
            "version",
            "arch",
            "suppressed",
        ):
            report = report_for(expected)
            result = report["Results"][0]
            if variant == "os":
                report["Metadata"]["OS"]["Name"] = "other"
            elif variant in ("type", "class"):
                result["Type" if variant == "type" else "Class"] = "node-pkg"
            elif variant == "missing":
                result["Packages"].pop()
            elif variant == "extra":
                result["Packages"].append({"Name": "unrelated"})
            elif variant == "duplicate":
                result["Packages"][-1] = result["Packages"][0]
            elif variant == "purl":
                result["Packages"][0]["Identifier"]["PURL"] += "-wrong"
            elif variant == "src":
                result["Packages"][0]["SrcName"] = "unrelated"
            elif variant == "version":
                result["Packages"][0]["SrcVersion"] = "0-r0"
            elif variant == "arch":
                result["Packages"][0]["Arch"] = "aarch64"
            else:
                result["ModifiedFindings"] = [{"Status": "ignored"}]
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                M.validate_report(report, expected)

    def test_all_severities_survive_and_hc_unknown_block(self):
        expected = self.expected()
        report = report_for(expected, M.SEVERITIES)
        result = M.validate_report(report, expected)
        self.assertEqual(result["findings"], 5)
        self.assertEqual(result["by_severity"], dict.fromkeys(M.SEVERITIES, 1))
        self.assertFalse(result["os_policy_passed"])
        self.assertEqual(len(report["Results"][0]["Vulnerabilities"]), 5)
        low = M.validate_report(report_for(expected, ["LOW", "MEDIUM"]), expected)
        self.assertTrue(low["os_policy_passed"])
        self.assertEqual(low["findings"], 2)

    def test_finding_wrong_origin_purl_or_malformed_container_cannot_be_zero(self):
        expected = self.expected()
        for variant in ("pkg", "id", "purl", "source", "severity", "null", "dict"):
            report = report_for(expected, ["HIGH"])
            finding = report["Results"][0]["Vulnerabilities"][0]
            if variant == "pkg":
                finding["PkgName"] = "unrelated"
            elif variant == "id":
                finding["PkgID"] = "unrelated"
            elif variant == "purl":
                finding["PkgIdentifier"]["PURL"] = "pkg:npm/unrelated@1"
            elif variant == "source":
                finding["DataSource"]["ID"] = "npm"
            elif variant == "severity":
                finding["Severity"] = "unrecognized"
            else:
                report["Results"][0]["Vulnerabilities"] = None if variant == "null" else {}
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                M.validate_report(report, expected)

    def test_exact_known_warning_messages_only(self):
        known = b"".join(
            ("2026-11-06T00:00:00Z\tWARN\t" + message + "\n").encode()
            for message in M.KNOWN_WARNINGS
        )
        self.assertEqual(len(M.validate_stderr(known)), 3)
        for raw in (
            b"time\tWARN\tunknown\n",
            b"time\tERROR\tdiagnostic\n",
            known + b"WARNING unexpected\n",
            b"bad\x00line",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                M.validate_stderr(raw)

    def test_json_duplicates_nonfinite_and_invalid_utf8_reject(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":"\xff"}', b"[]"):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                M.json_object(raw)

    def test_database_must_be_current_at_scan_time(self):
        now = datetime(2026, 11, 6, tzinfo=UTC)

        def dates(delta):
            return {
                "Version": 2,
                "UpdatedAt": "2026-11-05T00:00:00Z",
                "DownloadedAt": "2026-11-05T01:00:00Z",
                "NextUpdate": (now + delta).isoformat().replace("+00:00", "Z"),
            }

        M.validate_db(dates(timedelta(seconds=1)), now)
        with self.assertRaises(ValueError):
            M.validate_db(dates(timedelta()), now)


class PublicationTests(unittest.TestCase):
    def exercise(self, variant):
        inventory, bom = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).absolute()
            identity = {"device": root.stat().st_dev, "inode": root.stat().st_ino}
            outputs = {
                "inventory.json": M.canonical(inventory),
                "os-packages.cdx.json": M.canonical(bom),
                "file-catalog.json": b"{}",
                "binary-materials.json": b"{}",
                "provenance.json": b"{}",
            }
            plan = {
                "owner": M.PUBLICATION_OWNER,
                "run_root": str(root),
                "sources": dict(M.PUBLICATION_SOURCE_PINS),
                "outputs": {
                    name: {"sha256": M.digest(raw), "bytes": len(raw)}
                    for name, raw in outputs.items()
                },
            }
            if variant == "source":
                plan["sources"]["core"] = "0" * 64
            plan_sha = M.digest(M.canonical(plan))
            marker = {
                "owner": M.PUBLICATION_OWNER,
                "path": str(root),
                "directory": identity,
                "plan_sha256": plan_sha,
            }
            receipt = {
                "owner": M.PUBLICATION_OWNER,
                "directory": identity,
                "plan_sha256": plan_sha,
                "outputs": plan["outputs"],
                "artifacts_published": variant != "negative",
                "binary_accepted": False,
                "sca_executed": False,
                "runtime_executed": False,
            }
            all_files = {
                **outputs,
                ".owner.json": M.canonical(marker),
                "plan.json": M.canonical(plan),
                "receipt.json": M.canonical(receipt),
            }
            pins = {name: M.digest(raw) for name, raw in all_files.items()}
            for name, raw in all_files.items():
                (root / name).write_bytes(raw)
            if variant == "tamper":
                (root / "inventory.json").write_bytes(b"changed")
            if variant == "identity":
                identity = {**identity, "inode": identity["inode"] + 1}
            with (
                patch.object(M, "PUBLICATION_ROOT", root),
                patch.object(M, "PUBLICATION_IDENTITY", identity),
                patch.object(M, "PUBLICATION_PLAN_SHA", plan_sha),
                patch.object(M, "PUBLICATION_FILES", pins),
            ):
                reader = M.load_reader()
                if variant == "valid":
                    result = M.capture_publication(reader)
                    self.assertEqual(len(result["packages"]), 23)
                    self.assertEqual(result["sbom_raw"], outputs["os-packages.cdx.json"])
                    self.assertEqual(set(result["publication"]["files"]), set(all_files))
                else:
                    with self.assertRaises(ValueError):
                        M.capture_publication(reader)

    def test_positive_publication_chain_with_real_safe_reader(self):
        self.exercise("valid")

    def test_changed_bytes_identity_negative_receipt_or_source_cannot_be_adopted(self):
        for variant in ("tamper", "identity", "negative", "source"):
            with self.subTest(variant=variant):
                self.exercise(variant)


class ScanTests(unittest.TestCase):
    def exercise(self, variant):
        inventory, bom = fixture()
        expected = M.validate_inputs(inventory, bom)
        model = {"packages": expected, "sbom_raw": M.canonical(bom), "publication": {"fixed": True}}
        snapshot = {"fixed": True}
        plan = {
            "snapshot": snapshot,
            "publication": model["publication"],
            "source_sha256": "0" * 64,
            "sbom_sha256": M.digest(model["sbom_raw"]),
        }
        calls = []

        def fake_job(executable, arguments, **kwargs):
            calls.append((executable, arguments, kwargs))
            status = variant if variant in {"timeout", "output_limit"} else "completed"
            report = report_for(
                expected, ["UNKNOWN", "HIGH", "MEDIUM"] if variant == "findings" else []
            )
            if variant == "coverage":
                report["Results"][0]["Packages"].pop()
            return types.SimpleNamespace(
                status=status,
                exit_code=0 if status == "completed" else None,
                cleanup_complete=variant != "cleanup",
                elapsed_seconds=0.01,
                stdout=M.canonical(report) if status == "completed" else b'{"partial":',
                stderr=b"time\tWARN\tunknown\n" if variant == "coverage" else b"",
            )

        reader = types.SimpleNamespace(
            validate_process=lambda result: M.require(
                result.status == "completed" and result.exit_code == 0 and result.cleanup_complete,
                "PROCESS",
            )
        )
        job = types.SimpleNamespace(run_job=fake_job, Limits=lambda **kwargs: kwargs)
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "own"
            if variant == "collision":
                run.mkdir()
                (run / "foreign").write_bytes(b"keep")
            snapshots = [snapshot, snapshot, {"changed": True} if variant == "drift" else snapshot]
            with (
                patch.object(M, "capture_snapshot", side_effect=snapshots),
                patch.object(M, "capture_publication", return_value=model),
                patch.object(M, "verify_sources"),
                patch.object(M, "scanner_environment", return_value={"PATH": "fixed"}),
                patch.object(M, "validate_stderr", wraps=M.validate_stderr) as diagnostics,
            ):
                if variant in {"valid", "findings"}:
                    receipt = M.run_scan(reader, job, plan, model, run)
                    self.assertTrue(receipt["os_coverage_complete"])
                    self.assertEqual(receipt["os_policy_passed"], variant == "valid")
                    self.assertFalse(receipt["binary_accepted"])
                    self.assertFalse(receipt["runtime_accepted"])
                    self.assertEqual(receipt["coverage"]["packages"], 23)
                    self.assertEqual(calls[0][2]["output_limit"], 1024**2)
                    self.assertEqual(calls[0][2]["timeout"], 180)
                    self.assertEqual(
                        calls[0][2]["limits"], {"processes": 8, "memory_bytes": 2 * 1024**3}
                    )
                    report = json.loads((run / "os-packages.trivy.json").read_bytes())
                    self.assertEqual(
                        len(report["Results"][0]["Vulnerabilities"]),
                        3 if variant == "findings" else 0,
                    )
                else:
                    with self.assertRaises((ValueError, FileExistsError)):
                        M.run_scan(reader, job, plan, model, run)
                    if variant == "collision":
                        self.assertEqual((run / "foreign").read_bytes(), b"keep")
                        self.assertEqual(calls, [])
                    else:
                        failure = json.loads((run / "failure.json").read_bytes())
                        self.assertFalse(failure["analysis_complete"])
                        self.assertFalse((run / "receipt.json").exists())
                        self.assertTrue((run / "os-packages.trivy.json").exists())
                        if variant == "output_limit":
                            self.assertFalse(failure["attempt"]["captured_output_complete"])
                        if variant == "coverage":
                            diagnostics.assert_not_called()

    def test_bounded_scan_preserves_report_without_runtime_or_binary_approval(self):
        self.exercise("valid")

    def test_findings_are_preserved_and_policy_blocks(self):
        self.exercise("findings")

    def test_timeout_overflow_cleanup_and_snapshot_drift_cannot_pass(self):
        for variant in ("timeout", "output_limit", "cleanup", "drift"):
            with self.subTest(variant=variant):
                self.exercise(variant)

    def test_coverage_is_checked_before_warning_classification(self):
        self.exercise("coverage")

    def test_existing_directory_is_never_adopted(self):
        self.exercise("collision")

    def test_default_plan_does_not_load_job_or_run_scanner(self):
        with (
            patch.object(M, "prepare", return_value=({"fixed": True}, {})),
            patch.object(M, "load_job", side_effect=AssertionError("NO_JOB")),
            patch.object(sys, "stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(M.main([]), 0)
            self.assertFalse(json.loads(output.getvalue())["scan_executed"])

    def test_scanner_arguments_close_config_ignore_network_and_severity_scope(self):
        args = M.scanner_arguments(Path("fixture"))
        self.assertEqual(args[args.index("--config") + 1], "")
        self.assertEqual(args[args.index("--ignorefile") + 1], "")
        self.assertEqual(args[args.index("--cache-backend") + 1], "memory")
        self.assertEqual(args[args.index("--pkg-types") + 1], "os")
        self.assertEqual(set(args[args.index("--severity") + 1].split(",")), set(M.SEVERITIES))
        self.assertTrue(
            {
                "--offline-scan",
                "--skip-db-update",
                "--skip-java-db-update",
                "--disable-telemetry",
                "--list-all-pkgs",
            }.issubset(args)
        )
        self.assertFalse({"--ignore-unfixed", "--ignore-policy", "--output"}.intersection(args))


class FrozenJobContractTests(unittest.TestCase):
    def job(self):
        path = PATH.parents[2] / "sre/windows_build_job.py"
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), M.JOB_SHA)
        module = types.ModuleType("valkey_os_frozen_job_test")
        module.__file__ = str(path)
        with patch.dict(sys.modules, {module.__name__: module}):
            exec(compile(raw, str(path), "exec"), module.__dict__)
        return module

    def test_actual_frozen_api_accepts_limits_and_stops_before_createprocess(self):
        job = self.job()

        class Api:
            def __init__(self):
                self.handles, self.closed, self.called = [], [], False

            def create_job(self, _limits):
                self.handles.append(1)
                return 1

            def pipe(self):
                first = len(self.handles) + 1
                self.handles.extend([first, first + 1])
                return first, first + 1

            def nul(self):
                self.handles.append(6)
                return 6

            def create(self, *_args):
                self.called = True
                raise job.NativeError("STOP_BEFORE_CREATEPROCESS", 1)

            def close(self, handle):
                self.closed.append(handle)

        api = Api()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).absolute()
            executable = root / "fixture.exe"
            executable.write_bytes(b"data-not-executable")
            result = job.run_job(
                executable,
                M.scanner_arguments(root),
                cwd=root,
                env={"PATH": "closed"},
                timeout=180,
                output_limit=M.MAX_OUTPUT,
                limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                _api=api,
            )
        self.assertTrue(api.called)
        self.assertEqual(result.error_operation, "STOP_BEFORE_CREATEPROCESS")
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(sorted(api.closed), sorted(api.handles))

    def test_actual_frozen_api_rejects_eight_mib_before_api_calls(self):
        job = self.job()
        api = types.SimpleNamespace(create_job=lambda *_args: self.fail("NO_API"))
        with self.assertRaises(ValueError):
            job.run_job(
                Path("unused.exe"),
                [],
                cwd=Path("unused"),
                env={},
                timeout=180,
                output_limit=8 * 1024**2,
                limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                _api=api,
            )


if __name__ == "__main__":
    unittest.main()
