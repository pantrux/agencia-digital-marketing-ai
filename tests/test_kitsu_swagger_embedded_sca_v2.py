"""Pruebas puras/fixtures; no Trivy, red, SDK ni credenciales."""

import base64
import copy
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "docker/kitsu-zou/embedded_js_sca_v2.py"
SPEC = importlib.util.spec_from_file_location("embedded_js_sca_v2_tests", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def checksum(raw):
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def fixture():
    integrity = "sha512-" + base64.b64encode(b"x" * 64).decode()
    candidates = []
    lock = {"packages": {}}
    for path, version in [
        ("node_modules/@vue/shared", "3.5.13"),
        ("node_modules/other/node_modules/@vue/shared", "3.5.44"),
        ("node_modules/another/node_modules/@vue/shared", "3.5.44"),
    ]:
        row = {
            "version": version,
            "resolved": "https://registry.npmjs.org/@vue/shared/-/shared-" + version + ".tgz",
            "integrity": integrity,
            "license": "MIT",
        }
        lock["packages"][path] = row
        candidates.append(
            {
                **{k: row[k] for k in ("version", "resolved", "integrity")},
                "lock_path": path,
                "bundle_bytes_verified": False,
                "license_declared_in_lock": "MIT",
            }
        )
    members, declared = {}, []
    for name, version, path, needle in [
        ("jquery", "1.8.0", "flasgger/ui2/static/lib/jquery-1.8.0.min.js", 'jquery:"1.8.0"'),
        ("jquery", "2.2.4", "flasgger/ui3/static/lib/jquery.min.js", "/*! jQuery v2.2.4 |"),
        ("backbone", "1.1.2", "flasgger/ui2/static/lib/backbone-min.js", '.VERSION="1.1.2"'),
    ]:
        raw = b"prefix" + needle.encode() + b"suffix"
        members[path] = raw
        declared.append(
            {
                "name": name,
                "version": version,
                "member": path,
                "needle": needle,
                "offset": 6,
                "sha256": checksum(raw),
                "classification": "self-declared; not registry archive equivalence",
            }
        )
    inventory = {
        "inventories": [
            {
                "artifact": "kitsu",
                "packages": [
                    {
                        "name": "@vue/shared",
                        "version": None,
                        "version_status": "pending",
                        "sources": [{"map": "app.js.map", "source_index": 0}],
                        "lock_candidates": candidates,
                    }
                ],
                "version_evidence": [],
                "coverage": {
                    "component_version_coverage_complete": False,
                    "wasm_files": ["a.wasm"],
                },
            },
            {
                "artifact": "flasgger",
                "packages": [
                    {
                        "name": "swagger-client",
                        "version": None,
                        "version_status": "pending",
                        "lock_candidates": [],
                    }
                ],
                "version_evidence": declared,
                "coverage": {
                    "component_version_coverage_complete": False,
                    "javascript_without_linked_map": ["a.js"],
                },
            },
        ],
        "limitations": ["No equivalencia npm acreditada"],
        "sca_approved": False,
    }
    return inventory, lock, members


def report_for(components, findings=None):
    return {
        "SchemaVersion": 2,
        "Results": [
            {
                "Class": "lang-pkgs",
                "Type": "node-pkg",
                "Packages": [
                    {"Name": c["name"], "Version": c["version"], "Identifier": {"PURL": c["purl"]}}
                    for c in components
                ],
                "Vulnerabilities": findings or [],
            }
        ],
    }


class AnalysisTests(unittest.TestCase):
    def model(self):
        return M.build_analysis(*fixture())

    def test_all_candidates_deduplicate_pairs_but_keep_every_witness(self):
        model = self.model()
        lane = model["lanes"]["kitsu_candidates"]
        self.assertEqual(len(lane["components"]), 2)
        self.assertEqual(len(lane["witnesses"]), 3)
        self.assertEqual(model["ambiguous_packages"], ["@vue/shared"])
        self.assertEqual(model["unresolved_versions"]["flasgger"], ["swagger-client"])
        self.assertFalse(model["component_version_coverage_complete"])
        self.assertFalse(model["runtime_sca_approved"])

    def test_self_declared_members_require_exact_hash_and_offset(self):
        inventory, lock, members = fixture()
        self.assertEqual(
            len(
                M.build_analysis(inventory, lock, members)["lanes"]["flasgger_self_declared"][
                    "components"
                ]
            ),
            3,
        )
        for change in ["hash", "offset", "needle", "missing"]:
            wrong = copy.deepcopy(inventory)
            row = wrong["inventories"][1]["version_evidence"][0]
            if change == "hash":
                row["sha256"] = "a" * 64
            elif change == "offset":
                row["offset"] = 7
            elif change == "needle":
                row["needle"] = "not-the-version"
            else:
                row["member"] = "missing.js"
            with self.subTest(change=change), self.assertRaises(ValueError):
                M.build_analysis(wrong, lock, members)

    def test_candidate_origin_cannot_change_version_integrity_or_registry(self):
        for key, value in [
            ("version", "9.9.9"),
            ("integrity", "sha512-evil"),
            ("resolved", "https://registry.npmjs.org.evil.invalid/a.tgz"),
            ("lock_path", "node_modules/other"),
            ("bundle_bytes_verified", True),
        ]:
            inventory, lock, members = fixture()
            inventory["inventories"][0]["packages"][0]["lock_candidates"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                M.build_analysis(inventory, lock, members)

    def test_purl_scopes_and_versions_are_canonical_without_injection(self):
        self.assertEqual(M.npm_purl("@vue/shared", "3.5.13"), "pkg:npm/%40vue/shared@3.5.13")
        for name, version in [
            ("../x", "1.0.0"),
            ("jquery", "1.0.0\n"),
            ("@x/a/b", "1.0.0"),
            ("jquery", "01.0.0"),
        ]:
            with self.assertRaises(ValueError):
                M.npm_purl(name, version)

    def test_frozen_vcs_reference_is_only_a_hypothesis_and_never_resolved(self):
        inventory, lock, members = fixture()
        uri = "git+ssh://git@github.com/cgwire/fabricjs-psbrush.git#17a0cf4e853ae21809176236f5db1428667887b7"
        package = {
            "name": "fabricjs-psbrush",
            "version": None,
            "version_status": "pending",
            "sources": [{"map": "app.js.map", "source_index": 1}],
            "lock_candidates": [
                {
                    "lock_path": "node_modules/fabricjs-psbrush",
                    "version": "0.0.22",
                    "resolved": uri,
                    "integrity": None,
                    "license_declared_in_lock": "MIT",
                    "bundle_bytes_verified": False,
                }
            ],
        }
        inventory["inventories"][0]["packages"].append(package)
        lock["packages"]["node_modules/fabricjs-psbrush"] = {
            "version": "0.0.22",
            "resolved": uri,
            "integrity": None,
            "license": "MIT",
        }
        result = M.build_analysis(inventory, lock, members)
        row = next(
            c
            for c in result["lanes"]["kitsu_candidates"]["components"]
            if c["name"] == "fabricjs-psbrush"
        )
        self.assertEqual(row["evidence_kind"], "lock-vcs-candidate")
        self.assertFalse(result["vcs_candidates"][0]["npm_identity_confirmed"])
        self.assertFalse(result["vcs_candidates"][0]["git_execution_allowed"])
        package["lock_candidates"][0]["resolved"] = uri.replace("17a0cf4", "27a0cf4")
        lock["packages"]["node_modules/fabricjs-psbrush"]["resolved"] = package["lock_candidates"][
            0
        ]["resolved"]
        with self.assertRaises(ValueError):
            M.build_analysis(inventory, lock, members)

    def test_bom_does_not_claim_trivy_generator_os_or_runtime_graph(self):
        model = self.model()
        bom = M.make_bom(model["lanes"]["kitsu_candidates"])
        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertEqual(len(bom["components"]), 2)
        self.assertNotIn("dependencies", bom)
        self.assertNotIn("trivy", json.dumps(bom["metadata"]).lower())
        self.assertTrue(all(c["type"] == "library" for c in bom["components"]))
        self.assertTrue(all("hashes" not in c for c in bom["components"]))

    def test_coverage_rejects_missing_duplicate_extra_purl_and_origin(self):
        components = self.model()["lanes"]["kitsu_candidates"]["components"]
        valid = report_for(components)
        self.assertEqual(M.validate_report(valid, components)["packages"], 2)
        for change in ["missing", "duplicate", "extra", "purl", "origin", "suppressed"]:
            wrong = copy.deepcopy(valid)
            result = wrong["Results"][0]
            if change == "missing":
                result["Packages"].pop()
            elif change == "duplicate":
                result["Packages"].append(result["Packages"][0])
            elif change == "extra":
                result["Packages"][0]["Name"] = "foreign"
            elif change == "purl":
                result["Packages"][0]["Identifier"]["PURL"] += "?source=other"
            elif change == "origin":
                result["Type"] = "npm"
            else:
                result["ModifiedFindings"] = [{"Status": "not_affected"}]
            with self.subTest(change=change), self.assertRaises(ValueError):
                M.validate_report(wrong, components)

    def test_all_five_severities_are_retained_without_cvss_defaulting(self):
        components = self.model()["lanes"]["kitsu_candidates"]["components"]
        findings = [
            {
                "VulnerabilityID": "ADVISORY-" + level,
                "PkgName": "@vue/shared",
                "InstalledVersion": "3.5.13",
                "Severity": level,
            }
            for level in ["LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"]
        ]
        result = M.validate_report(report_for(components, findings), components)
        self.assertEqual(result["findings"], 5)
        self.assertEqual(result["high_critical"], 2)
        self.assertEqual(result["by_severity"]["UNKNOWN"], 1)
        for key in ["Severity", "InstalledVersion", "VulnerabilityID"]:
            bad = copy.deepcopy(findings)
            del bad[0][key]
            with self.assertRaises(ValueError):
                M.validate_report(report_for(components, bad), components)

    def test_malformed_finding_container_cannot_be_mistaken_for_zero(self):
        components = self.model()["lanes"]["kitsu_candidates"]["components"]
        for value in [{}, None, "", [1]]:
            report = report_for(components)
            report["Results"][0]["Vulnerabilities"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                M.validate_report(report, components)

    def test_warnings_are_exact_and_controls_errors_or_new_warnings_fail(self):
        stamp = b"2026-11-06T20:00:00Z\tWARN\t"
        for message in M.KNOWN_WARNINGS:
            self.assertEqual(
                M.validate_stderr(stamp + message.encode() + b"\n")[0]["message"], message
            )
        for raw in [
            stamp + b"new warning\n",
            b"ERROR failure\n",
            b"FATAL failure\n",
            b"WARNING unexpected\n",
            stamp + b"prefix " + next(iter(M.KNOWN_WARNINGS)).encode(),
            b"\xff",
            b"\x1b",
        ]:
            with self.assertRaises(ValueError):
                M.validate_stderr(raw)

    def test_db_freshness_is_required_before_and_after(self):
        now = datetime(2026, 11, 6, 20, tzinfo=UTC)
        db = {
            "Version": 2,
            "UpdatedAt": "2026-11-06T12:55:00Z",
            "NextUpdate": "2026-11-07T12:55:00Z",
            "DownloadedAt": "2026-11-06T14:00:00Z",
        }
        M.validate_db(db, now)
        for key, value in [
            ("Version", True),
            ("NextUpdate", "2026-11-06T20:00:00Z"),
            ("UpdatedAt", "2026-11-08T12:55:00Z"),
            ("DownloadedAt", "2026-11-07T12:00:00Z"),
        ]:
            with self.assertRaises(ValueError):
                M.validate_db({**db, key: value}, now)

    def test_json_duplicates_nonfinite_and_invalid_utf8_fail(self):
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}', b'"\xff"']:
            with self.assertRaises(ValueError):
                M.json_object(raw)

    def test_flags_disable_config_ignorefile_network_and_preserve_all_severities(self):
        args = M.scanner_arguments("kitsu_candidates", Path("/fixture"))
        for flag in ["--config", "--ignorefile"]:
            self.assertEqual(args[args.index(flag) + 1], "")
        self.assertIn("--offline-scan", args)
        self.assertIn("--skip-db-update", args)
        self.assertNotIn("--ignore-unfixed", args)
        self.assertNotIn("--output", args)
        self.assertEqual(args[args.index("--cache-backend") + 1], "memory")
        with self.assertRaises(ValueError):
            M.scanner_arguments("foreign", Path("/fixture"))


class ExecutionFixtureTests(unittest.TestCase):
    def test_cache_snapshot_preserves_existing_logs_and_detects_directory_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, tool = Path(tmp) / "cache", Path(tmp) / "trivy.exe"
            (cache / "db").mkdir(parents=True)
            (cache / "profile").mkdir()
            (cache / ".owner").write_bytes(M.CACHE_OWNER.encode())
            (cache / "download.stderr.txt").write_bytes(b"existing log")
            (cache / "db/trivy.db").write_bytes(b"fixed db")
            tool.write_bytes(b"fixed tool")
            now = datetime.now(UTC)
            meta = M.canonical(
                {
                    "Version": 2,
                    "UpdatedAt": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                    "DownloadedAt": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    "NextUpdate": (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
                }
            )
            (cache / "db/metadata.json").write_bytes(meta)

            def read(path, maximum, expected=None):
                raw = Path(path).read_bytes()
                if len(raw) > maximum or expected and checksum(raw) != expected:
                    raise ValueError("PIN")
                return raw

            reader = types.SimpleNamespace(
                read_exact=read,
                hash_file=lambda p, m: {"sha256": checksum(read(p, m)), "bytes": len(read(p, m))},
            )
            with (
                patch.object(M, "CACHE", cache),
                patch.object(M, "TRIVY", tool),
                patch.object(M, "METADATA_SHA", checksum(meta)),
                patch.object(M, "DB_SHA", checksum(b"fixed db")),
                patch.object(M, "TRIVY_SHA", checksum(b"fixed tool")),
            ):
                before = M.capture_snapshot(reader)
                self.assertEqual((cache / "download.stderr.txt").read_bytes(), b"existing log")
                self.assertEqual(M.capture_snapshot(reader), before)
                (cache / "profile/new").write_bytes(b"unexpected")
                self.assertNotEqual(M.capture_snapshot(reader), before)

    def exercise(self, variant):
        analysis = M.build_analysis(*fixture())
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            run = parent / "own-sca"
            before = {"db_metadata": {"Version": 2}, "db": "fixed", "tool": "fixed"}
            plan = {
                "source_sha256": "a" * 64,
                "inventory_sha256": "b" * 64,
                "snapshot": before,
                "boms": {
                    lane: {
                        "sha256": M.digest(M.canonical(M.make_bom(analysis["lanes"][lane]))),
                        "components": len(analysis["lanes"][lane]["components"]),
                    }
                    for lane in M.LANES
                },
            }
            calls = []

            def fake_job(executable, arguments, **kwargs):
                calls.append((executable, arguments, kwargs))
                lane = "kitsu_candidates" if len(calls) == 1 else "flasgger_self_declared"
                components = analysis["lanes"][lane]["components"]
                status = variant if variant in {"timeout", "output_limit"} else "completed"
                findings = []
                if variant == "findings":
                    first = components[0]
                    findings = [
                        {
                            "VulnerabilityID": "FIXTURE-" + level,
                            "PkgName": first["name"],
                            "InstalledVersion": first["version"],
                            "Severity": level,
                            "Description": "ERROR and WARN are words in advisory data",
                        }
                        for level in ["HIGH", "MEDIUM"]
                    ]
                stdout = (
                    M.canonical(report_for(components, findings))
                    if status == "completed"
                    else b'{"partial":'
                )
                return types.SimpleNamespace(
                    status=status,
                    exit_code=0 if status == "completed" else None,
                    cleanup_complete=variant != "cleanup",
                    stdout=stdout,
                    stderr=b"",
                    elapsed_seconds=0.1,
                )

            reader = types.SimpleNamespace(
                validate_process=lambda result: M.require(
                    result.status == "completed"
                    and result.exit_code == 0
                    and result.cleanup_complete,
                    "PROCESS",
                )
            )
            job = types.SimpleNamespace(run_job=fake_job, Limits=lambda **kwargs: kwargs)
            snapshots = [before] * 5
            if variant == "drift":
                snapshots[2] = {**before, "db": "changed"}
            if variant == "collision":
                run.mkdir()
                (run / "foreign").write_bytes(b"sentinel")
            with (
                patch.object(M, "capture_snapshot", side_effect=snapshots),
                patch.object(M, "scanner_environment", return_value={"PATH": "fixed"}),
            ):
                if variant in {"valid", "findings"}:
                    result = M.run_scans(reader, job, plan, analysis, run)
                    self.assertTrue(result["analysis_complete"])
                    self.assertFalse(result["runtime_sca_approved"])
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(
                        result["known_hypotheses_high_critical_or_unknown"],
                        2 if variant == "findings" else 0,
                    )
                    for _, _, kwargs in calls:
                        self.assertEqual(kwargs["timeout"], 180)
                        self.assertEqual(
                            kwargs["limits"], {"processes": 8, "memory_bytes": 2 * 1024**3}
                        )
                        self.assertEqual(kwargs["output_limit"], M.MAX_OUTPUT)
                else:
                    with self.assertRaises((ValueError, FileExistsError)):
                        M.run_scans(reader, job, plan, analysis, run)
                    if variant == "collision":
                        self.assertEqual((run / "foreign").read_bytes(), b"sentinel")
                        self.assertEqual(calls, [])
                    else:
                        failure = json.loads((run / "failure.json").read_bytes())
                        self.assertFalse(failure["analysis_complete"])
                        self.assertTrue((run / "kitsu_candidates.trivy.json").exists())
                        if variant == "output_limit":
                            attempt = failure["attempts"]["kitsu_candidates"]
                            self.assertEqual(attempt["status"], "output_limit")
                            self.assertFalse(attempt["captured_output_complete"])
                            self.assertFalse((run / "receipt.json").exists())

    def test_two_bounded_scans_with_complete_reports_and_no_runtime_approval(self):
        self.exercise("valid")

    def test_timeout_preserves_partial_and_cannot_pass(self):
        self.exercise("timeout")

    def test_output_limit_preserves_incomplete_capture_without_adopting_report(self):
        self.exercise("output_limit")

    def test_db_drift_preserves_report_and_cannot_pass(self):
        self.exercise("drift")

    def test_findings_remain_in_receipt_without_runtime_approval(self):
        self.exercise("findings")

    def test_unclean_process_cannot_pass_even_with_exit_zero_and_full_json(self):
        self.exercise("cleanup")

    def test_existing_destination_is_never_adopted(self):
        self.exercise("collision")

    def test_default_plan_never_loads_job_or_starts_scan(self):
        with (
            patch.object(M, "prepare", return_value=({"source_sha256": "fixed"}, {})),
            patch.object(M, "load_job", side_effect=AssertionError("NO_JOB")),
            patch.object(sys, "stdout", new_callable=io.StringIO) as out,
        ):
            self.assertEqual(M.main([]), 0)
            self.assertFalse(json.loads(out.getvalue())["scan_executed"])


class FrozenJobContractTests(unittest.TestCase):
    def job(self):
        path = PATH.parents[2] / "sre/windows_build_job.py"
        raw = path.read_bytes()
        self.assertEqual(
            checksum(raw), "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44"
        )
        module = types.ModuleType("embedded_sca_frozen_job_contract_v2")
        module.__file__ = str(path)
        with patch.dict(sys.modules, {module.__name__: module}):
            exec(compile(raw, str(path), "exec"), module.__dict__)
        return module

    def api(self, job):
        class StopBeforeCreate:
            def __init__(self):
                self.calls, self.closed, self.created_handles = [], [], []

            def create_job(self, limits):
                self.calls.append(("create_job", limits))
                self.created_handles.append(1)
                return 1

            def pipe(self):
                first = len(self.created_handles) + 1
                self.created_handles.extend([first, first + 1])
                return first, first + 1

            def nul(self):
                self.created_handles.append(6)
                return 6

            def create(self, executable, arguments, cwd, env, stdin, stdout, stderr):
                self.calls.append(
                    ("create", executable, arguments, cwd, env, stdin, stdout, stderr)
                )
                # No Win32, Popen, CreateProcess o ejecutable real: sólo llega al límite de API.
                raise job.NativeError("TEST_STOP_BEFORE_CREATEPROCESS", 1)

            def close(self, handle):
                self.closed.append(handle)

        return StopBeforeCreate()

    def test_real_frozen_run_job_accepts_full_scanner_parameters_before_createprocess(self):
        job = self.job()
        api = self.api(job)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).absolute()
            executable = directory / "fixture.exe"
            executable.write_bytes(b"not-executable-fixture")
            arguments = M.scanner_arguments("kitsu_candidates", directory)
            result = job.run_job(
                executable,
                arguments,
                cwd=directory,
                env={"PATH": "closed"},
                timeout=180,
                limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                output_limit=M.MAX_OUTPUT,
                _api=api,
            )
            self.assertEqual(M.MAX_OUTPUT, 1024**2)
            self.assertEqual(result.status, "launch_error")
            self.assertEqual(result.error_operation, "TEST_STOP_BEFORE_CREATEPROCESS")
            self.assertTrue(result.cleanup_complete)
            self.assertEqual(api.calls[1][0], "create")
            self.assertEqual(api.calls[1][2], arguments)
            self.assertEqual(sorted(api.closed), sorted(api.created_handles))

    def test_frozen_run_job_rejects_v1_eight_mib_before_any_api_call(self):
        job = self.job()
        api = self.api(job)
        with self.assertRaises(ValueError):
            job.run_job(
                Path("unused.exe"),
                [],
                cwd=Path("unused"),
                env={},
                timeout=180,
                limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                output_limit=8 * 1024**2,
                _api=api,
            )
        self.assertEqual(api.calls, [])

    def test_v1_sources_are_preserved(self):
        root = PATH.parents[2]
        for relative, expected in [
            (
                "docker/kitsu-zou/embedded_js_sca.py",
                "cf0fcc5cd78ce56efbf97fb0c4c7b93b394b7347e87839ca5798b425eeab5ae2",
            ),
            (
                "tests/test_kitsu_swagger_embedded_sca.py",
                "b48da02a91bcdcc5ff19f83527abf47736462f796551916f7b6136b912423c79",
            ),
            (
                "docker/kitsu-zou/README-EMBEDDED-JS-SCA.md",
                "f5a0301807361af1762061e5f6da4c83daab34fc1f422fd7d9c9906a181ecf2d",
            ),
        ]:
            self.assertEqual(checksum((root / relative).read_bytes()), expected)


if __name__ == "__main__":
    unittest.main()
