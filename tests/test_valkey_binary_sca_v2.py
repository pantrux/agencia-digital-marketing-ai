"""Datos sintéticos y transporte falso: ninguna consulta externa ni binario."""

import gzip
import hashlib
import http.client
import io
import json
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    path = ROOT / relative
    module = types.ModuleType(name)
    module.__file__ = str(path)
    with patch.dict(sys.modules, {name: module}):
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


C = load("valkey_binary_core_test", "docker/valkey/binary_materials.py")
A = load("valkey_binary_audit_test", "docker/valkey/audit_binary_materials_v2.py")


def archive_fixture(extra=()):
    stream = io.BytesIO()
    with tarfile.open(
        fileobj=stream, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": "a" * 40}
    ) as archive:
        for name, data, kind, link in [
            ("source", b"", tarfile.DIRTYPE, ""),
            ("source/COPYING", b"Copyright supplier\nBSD notice\n", tarfile.REGTYPE, ""),
            ("source/src", b"", tarfile.DIRTYPE, ""),
            ("source/src/file.c", b"Copyright contributor\n", tarfile.REGTYPE, ""),
            *extra,
        ]:
            info = tarfile.TarInfo(name)
            info.type, info.linkname, info.mode = kind, link, 0o775
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if info.isfile() else None)
    raw = gzip.compress(stream.getvalue(), mtime=0)
    return raw, {
        "root": "source",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "commit_declared": "a" * 40,
    }


def component(**changes):
    return {
        "id": "valkey",
        "name": "Valkey",
        "scope": "runtime",
        "repository": "https://github.com/valkey-io/valkey",
        "commit_declared": "a" * 40,
        "version_declared": "9.1.3",
        "nvd_version": "9.1.3",
        "nvd_keyword": "Valkey",
        "reference_prefixes": ["https://github.com/valkey-io/valkey"],
        "supplier_cpe": "cpe:2.3:a:lfprojects:valkey:9.1.3:*:*:*:*:*:*:*",
        "supplier_link_candidates": ["valkey-server"],
        "modified_upstream": False,
        "paths": ["src/"],
        **changes,
    }


def osv_record(level="HIGH", **changes):
    return {
        "id": "OSV-fixture",
        "schema_version": "1.7.0",
        "affected": [
            {
                "ranges": [
                    {
                        "type": "GIT",
                        "repo": "https://github.com/valkey-io/valkey",
                        "events": [{"introduced": "0"}],
                    }
                ]
            }
        ],
        "database_specific": {"severity": level},
        "aliases": ["CVE-2026-1234"],
        **changes,
    }


def dictionary(component_value, version="9.1.3", **changes):
    cpe = "cpe:2.3:a:lfprojects:valkey:" + version + ":*:*:*:*:*:*:*"
    return {
        "format": "NVD_CPE",
        "version": "2.0",
        "startIndex": 0,
        "resultsPerPage": 1,
        "totalResults": 1,
        "products": [
            {
                "cpe": {
                    "cpeName": cpe,
                    "deprecated": False,
                    "refs": [{"ref": component_value["repository"]}],
                }
            }
        ],
        **changes,
    }


class CoreTests(unittest.TestCase):
    def test_source_catalogue_keeps_bytes_hashes_commit_and_modes_without_extract(self):
        raw, expected = archive_fixture()
        result = C.catalogue_source(raw, expected)
        self.assertEqual(result["commit_declared"], "a" * 40)
        self.assertEqual(result["files"]["src/file.c"]["data"], b"Copyright contributor\n")
        self.assertEqual(result["files"]["src/file.c"]["mode"], 0o775)
        self.assertEqual(len(result["tree_sha256"]), 64)

    def test_source_rejects_duplicate_links_escape_ancestor_and_nonzero_trailer(self):
        for row in (
            ("source/src/file.c", b"other", tarfile.REGTYPE, ""),
            ("source/link", b"", tarfile.SYMTYPE, "/outside"),
            ("source/../escape", b"bad", tarfile.REGTYPE, ""),
            ("source/src/file.c/child", b"bad", tarfile.REGTYPE, ""),
        ):
            raw, expected = archive_fixture([row])
            with self.subTest(row=row[0]), self.assertRaises(ValueError):
                C.catalogue_source(raw, expected)
        raw, expected = archive_fixture()
        plain = gzip.decompress(raw)
        raw = gzip.compress(plain[:-1] + b"x")
        expected.update(sha256=C.digest(raw), bytes=len(raw))
        with self.assertRaises(ValueError):
            C.catalogue_source(raw, expected)

    def test_archive_limits_and_pin_are_enforced(self):
        raw, expected = archive_fixture()
        with self.assertRaises(ValueError):
            C.catalogue_source(raw, {**expected, "sha256": "0" * 64})
        with patch.object(C, "MAX_SOURCE_EXPANDED", 1024), self.assertRaises(ValueError):
            C.catalogue_source(raw, expected)

    def test_query_plan_uses_authenticated_commit_not_a_semver_as_a_tag(self):
        c = component()
        queries = C.query_plan([c])
        self.assertEqual(queries[0]["kind"], "github-commit")
        self.assertTrue(queries[0]["url"].endswith("/commits/" + "a" * 40))
        self.assertEqual(queries[1]["body"], {"commit": "a" * 40})
        without = C.query_plan([component(commit_declared=None)])
        self.assertFalse(any(row["kind"] == "osv" for row in without))
        self.assertFalse(any("ecosystem" in row.get("body", {}) for row in queries))

    def test_matrix_distinguishes_supplier_candidate_observed_marker_and_test_only(self):
        raw, expected = archive_fixture()
        source = C.catalogue_source(raw, expected)
        inv = {
            "binaries": [{"path": "usr/local/bin/valkey-server", "sha256": "b" * 64}],
            "aliases": [],
            "embedded_candidates": [
                {
                    "name": "lua",
                    "binary": "usr/local/bin/valkey-server",
                    "binary_sha256": "b" * 64,
                    "offset": 10,
                }
            ],
            "licenses": {"text_files": []},
        }
        components = [
            component(id="lua", modified_upstream=True),
            component(id="test", scope="build-test-only", supplier_link_candidates=[]),
        ]
        result = C.materials(source, inv, components)
        self.assertEqual(result["components"][0]["binary_markers"][0]["offset"], 10)
        self.assertFalse(result["components"][0]["binary_equal_to_upstream_proven"])
        self.assertEqual(result["components"][1]["supplier_link_candidates"], [])
        self.assertEqual(len(C.query_plan(components)), 3)
        self.assertTrue(result["license_overlay_pending"])

    def test_licenses_preserve_full_source_bytes_including_test_notices(self):
        raw, expected = archive_fixture()
        source = C.catalogue_source(raw, expected)
        notices = C.license_catalogue(source)
        self.assertEqual({row["path"] for row in notices}, {"COPYING", "src/file.c"})
        self.assertEqual(
            notices[0]["sha256"], C.digest(source["files"][notices[0]["path"]]["data"])
        )

    def test_osv_missing_affected_or_unknown_schema_cannot_be_a_clean_scan(self):
        for wrong in (
            {"vulns": [{"id": "x"}]},
            {"vulns": [osv_record(schema_version="2.0.0")]},
            {"error": "unsupported"},
            {"vulns": None},
        ):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                C.osv_page(wrong)

    def test_nvd_dictionary_requires_same_version_and_primary_reference(self):
        c = component(supplier_cpe=None)
        body = dictionary(c)
        matches, gaps = C.select_cpes(body["products"], c)
        self.assertEqual(len(matches), 1)
        self.assertEqual(gaps, [])
        self.assertEqual(C.select_cpes(dictionary(c, "1.0")["products"], c)[0], [])
        body["products"][0]["cpe"]["refs"] = [{"ref": "https://github.com/valkey-io/valkey-other"}]
        self.assertEqual(C.select_cpes(body["products"], c)[0], [])
        self.assertEqual(
            C.select_cpes(dictionary(c)["products"], component(nvd_version=None))[0], []
        )

    def test_unrelated_os_cpe_is_retained_and_never_queried_as_application(self):
        c = component()
        product = dictionary(c)["products"][0]
        product["cpe"]["cpeName"] = product["cpe"]["cpeName"].replace(":a:", ":o:", 1)
        selected, pending = C.select_cpes([product], c)
        self.assertEqual(selected, [])
        self.assertEqual(len(pending), 1)

    def test_all_raw_ranges_negations_aliases_and_severities_survive(self):
        cve = {
            "id": "CVE-2026-1234",
            "vulnStatus": "Analyzed",
            "configurations": [
                {
                    "negate": True,
                    "operator": "AND",
                    "nodes": [{"cpeMatch": [{"criteria": "range", "versionEndExcluding": "10"}]}],
                }
            ],
            "metrics": {"cvssMetricV31": [{"cvssData": {"baseSeverity": "CRITICAL"}}]},
        }
        finding = C.nvd_finding(cve, component())
        self.assertEqual(finding["raw"], cve)
        self.assertEqual(finding["applicability"], "unresolved")
        records = [
            C.osv_finding(osv_record(level), component())
            for level in ("LOW", "MEDIUM", "HIGH", "CRITICAL", "unrecognized")
        ]
        result = C.assess(records + [finding])
        self.assertEqual(result["findings"], 6)
        self.assertEqual(result["by_severity"]["UNKNOWN"], 1)
        self.assertFalse(result["known_findings_policy_passed"])
        self.assertFalse(result["release_accepted"])

    def test_feed_absence_is_a_gap_not_an_invented_vulnerability(self):
        result = C.assess([], [{"component": "unindexed", "reason": "feed_absent"}])
        self.assertEqual(result["findings"], 0)
        self.assertTrue(result["known_findings_policy_passed"])
        self.assertFalse(result["all_component_feeds_available"])

    def test_alias_groups_keep_every_record_and_do_not_drop_a_higher_severity(self):
        low = C.osv_finding(osv_record("LOW"), component())
        high = C.nvd_finding(
            {"id": "CVE-2026-1234", "metrics": {"v3": [{"cvssData": {"baseSeverity": "HIGH"}}]}},
            component(),
        )
        result = C.assess([low, high])
        self.assertEqual(len(result["advisory_groups"]), 1)
        self.assertEqual(result["advisory_groups"][0]["record_indices"], [0, 1])
        self.assertFalse(result["known_findings_policy_passed"])


class TransportTests(unittest.TestCase):
    def model(self):
        c = component()
        return {"components": [c], "queries": C.query_plan([c])}

    def transport(self, request):
        if request["kind"] == "github-commit":
            body = {
                "sha": "a" * 40,
                "html_url": "https://github.com/valkey-io/valkey/commit/" + "a" * 40,
            }
        elif request["kind"] == "osv":
            body = {"vulns": [osv_record()]}
        elif request["kind"] == "nvd-dictionary":
            body = dictionary(component())
        else:
            body = {
                "format": "NVD_CVE",
                "version": "2.0",
                "startIndex": 0,
                "resultsPerPage": 0,
                "totalResults": 0,
                "vulnerabilities": [],
            }
        return 200, C.canonical(body)

    def test_complete_fake_audit_authenticates_commit_before_osv_and_keeps_findings(self):
        calls = []

        def transport(request, **_kwargs):
            calls.append(request["kind"])
            return self.transport(request)

        result = A.collect(
            self.model(),
            transport,
            lambda *_args: None,
            clock=lambda: 0,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(calls, ["github-commit", "osv", "nvd-dictionary", "nvd-cves"])
        self.assertEqual(result["assessment"]["findings"], 1)
        self.assertFalse(result["assessment"]["known_findings_policy_passed"])
        self.assertFalse(result["assessment"]["runtime_accepted"])

    def test_wrong_commit_and_http_redirect_or_failed_status_stop(self):
        for status, body in (
            (302, b"{}"),
            (403, b"{}"),
            (200, C.canonical({"sha": "c" * 40, "html_url": "wrong"})),
        ):
            calls = []

            def transport(request, _calls=calls, _status=status, _body=body, **_kwargs):
                _calls.append(request)
                return _status, _body

            with self.subTest(status=status), self.assertRaises(ValueError):
                A.collect(
                    self.model(),
                    transport,
                    lambda *_args: None,
                    clock=lambda: 0,
                    sleep=lambda _seconds: None,
                )
            self.assertEqual(len(calls), 1)

    def test_paginated_osv_preserves_pages_and_rejects_repeated_tokens(self):
        calls = []

        def transport(request, **_kwargs):
            calls.append(request)
            if request["kind"] == "osv":
                return 200, C.canonical({"vulns": [], "next_page_token": "repeated"})
            return self.transport(request)

        with self.assertRaises(ValueError):
            A.collect(
                self.model(),
                transport,
                lambda *_args: None,
                clock=lambda: 0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(sum(r["kind"] == "osv" for r in calls), 2)

    def test_bounds_timeout_oversize_and_response_count_are_not_silently_expanded(self):
        for variant in ("time", "bytes", "count"):

            def transport(request, _variant=variant, **_kwargs):
                return (
                    (200, b"x" * (A.BOUNDS["response_bytes"] + 1))
                    if _variant == "bytes"
                    else self.transport(request)
                )

            with self.subTest(variant=variant):
                if variant == "count":
                    with patch.dict(A.BOUNDS, {"responses": 1}), self.assertRaises(ValueError):
                        A.collect(
                            self.model(),
                            transport,
                            lambda *_args: None,
                            clock=lambda: 0,
                            sleep=lambda _seconds: None,
                        )
                elif variant == "time":
                    with self.assertRaises(ValueError):
                        A.collect(
                            self.model(),
                            transport,
                            lambda *_args: None,
                            clock=iter([0, 301]).__next__,
                            sleep=lambda _seconds: None,
                        )
                else:
                    with self.assertRaises(ValueError):
                        A.collect(
                            self.model(),
                            transport,
                            lambda *_args: None,
                            clock=lambda: 0,
                            sleep=lambda _seconds: None,
                        )

    def test_real_transport_rejects_unapproved_host_userinfo_plain_http_and_paths(self):
        for url in (
            "http://api.osv.dev/v1/query",
            "https://evil.invalid/v1/query",
            "https://user@api.osv.dev/v1/query",
            "https://api.osv.dev/wrong",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                A.check_request(
                    {"kind": "osv", "method": "POST", "url": url, "body": {"commit": "a" * 40}}
                )

    def test_exclusive_audit_preserves_foreign_sentinel_and_default_has_no_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "foreign"
            target.mkdir()
            (target / "sentinel").write_bytes(b"keep")
            with patch.object(A, "RUN_ROOT", target), self.assertRaises(FileExistsError):
                A.publish_audit({}, {}, lambda *_args, **_kw: self.fail("NO_NETWORK"))
            self.assertEqual((target / "sentinel").read_bytes(), b"keep")
        with (
            patch.object(A, "prepare", return_value=({"fixed": True}, {})),
            patch.object(A, "publish_audit", side_effect=AssertionError("NO_AUDIT")),
            patch.object(sys, "stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(A.main([]), 0)
            self.assertFalse(json.loads(output.getvalue())["audit_executed"])

    def test_incomplete_or_extra_query_plan_fails_before_transport(self):
        for variant in ("missing", "extra"):
            model = self.model()
            if variant == "missing":
                model["queries"].pop()
            else:
                model["queries"].append(dict(model["queries"][0]))
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                A.collect(
                    model,
                    lambda *_args, **_kw: self.fail("NO_NETWORK"),
                    lambda *_args: None,
                    clock=lambda: 0,
                    sleep=lambda _seconds: None,
                )

    def test_partial_transport_failure_is_preserved_without_positive_receipt(self):
        stored = []

        def transport(*_args, **_kwargs):
            raise A.TransportFailure("fixture_timeout", 200, b"partial")

        with self.assertRaises(ValueError):
            A.collect(
                self.model(),
                transport,
                lambda *record: stored.append(record),
                clock=lambda: 0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(stored[0][3], b"partial")
        self.assertFalse(stored[0][4])

    def test_real_transport_watchdog_closes_only_its_fake_socket_and_file(self):
        timers = []

        class Timer:
            def __init__(self, _seconds, callback):
                self.callback = callback
                self.started = self.cancelled = self.joined = False
                timers.append(self)

            def start(self):
                self.started = True

            def cancel(self):
                self.cancelled = True

            def join(self):
                self.joined = True

        class Socket:
            def __init__(self):
                self.closed = False

            def settimeout(self, _timeout):
                pass

            def connect(self, _address):
                pass

            def do_handshake(self):
                pass

            def shutdown(self, _how):
                pass

            def close(self):
                self.closed = True

        owned = Socket()

        class Response:
            status = 200
            headers = Message()
            closed = False

            def read1(self, _maximum):
                # Vence cuando la respuesta ya está abierta, sin depender de carga/30 ms.
                timers[0].callback()
                raise OSError("fixture socket closed")

            def close(self):
                self.closed = True

        response = Response()

        class Connection:
            sock = None

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return response

            def close(self):
                pass

        request = self.model()["queries"][0]
        with (
            patch.object(A.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("fixture", 443))]),
            patch.object(A.socket, "socket", return_value=owned),
            patch.object(
                A.ssl,
                "create_default_context",
                return_value=types.SimpleNamespace(wrap_socket=lambda *_args, **_kwargs: owned),
            ),
            patch.object(A.http.client, "HTTPSConnection", return_value=Connection()),
            patch.object(A.threading, "Timer", Timer),
            self.assertRaises(A.TransportFailure),
        ):
            A.https_transport(request, timeout=15, maximum=1024)
        self.assertTrue(owned.closed)
        self.assertTrue(response.closed)
        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].started and timers[0].cancelled and timers[0].joined)

    def test_nvd_all_pages_and_no_progress_are_checked(self):
        calls = []
        request = self.model()["queries"][-1]

        def ask(current):
            calls.append(current)
            body = dictionary(component())
            body.update(startIndex=len(calls) - 1, totalResults=2)
            return body

        self.assertEqual(len(A.nvd_pages(request, "NVD_CPE", "products", ask)), 2)
        self.assertIn("startIndex=1", calls[-1]["url"])
        bad = dictionary(component(), totalResults=2, resultsPerPage=0, products=[])
        with self.assertRaises(ValueError):
            A.nvd_pages(request, "NVD_CPE", "products", lambda _request: bad)

    def test_complete_fake_publication_and_failures_preserve_all_captured_responses(self):
        original_collect = A.collect
        for variant in ("complete", "http", "changed", "missing-output"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "own"
                model = self.model()
                model["outputs"] = dict.fromkeys(
                    (
                        "materials.json",
                        "source-files.json",
                        "license-catalog.json",
                        "query-plan.json",
                    ),
                    b"{}",
                )
                if variant == "missing-output":
                    model["outputs"].pop("license-catalog.json")
                plan = {
                    "source_sha256": "0" * 64,
                    "outputs": {
                        name: {"sha256": C.digest(raw), "bytes": len(raw)}
                        for name, raw in model["outputs"].items()
                    },
                }
                again = {**plan, "source_sha256": "f" * 64} if variant == "changed" else plan

                def transport(request, _variant=variant, **_kwargs):
                    if _variant == "http" and request["kind"] == "nvd-dictionary":
                        return 503, b'{"upstream":"unavailable"}'
                    return self.transport(request)

                def run(model, transport, store):
                    return original_collect(
                        model, transport, store, clock=lambda: 0, sleep=lambda _seconds: None
                    )

                with (
                    patch.object(A, "RUN_ROOT", root),
                    patch.object(A, "prepare", return_value=(again, model)),
                    patch.object(A, "collect", side_effect=run),
                ):
                    if variant == "complete":
                        result = A.publish_audit(plan, model, transport)
                        self.assertTrue(result["audit_complete"])
                        self.assertFalse(result["assessment"]["known_findings_policy_passed"])
                        self.assertFalse(result["release_accepted"])
                        self.assertEqual(result["http_responses"], 4)
                        self.assertEqual(
                            json.loads((root / "response-02.json").read_bytes())["vulns"][0]["id"],
                            "OSV-fixture",
                        )
                    else:
                        with self.assertRaises(ValueError):
                            A.publish_audit(plan, model, transport)
                        failure = json.loads((root / "failure.json").read_bytes())
                        self.assertFalse(failure["audit_complete"])
                        self.assertFalse((root / "receipt.json").exists())
                        if variant != "missing-output":
                            self.assertTrue((root / "response-02.json").exists())

    def test_dns_deadline_cannot_start_late_http_and_resolver_fixture_is_joined(self):
        gate, threads = threading.Event(), []
        original_thread = threading.Thread

        def resolve(*_args, **_kwargs):
            gate.wait(0.5)
            return [(2, 1, 6, "", ("fixture", 443))]

        def create_thread(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        started = time.monotonic()
        try:
            with (
                patch.object(A.socket, "getaddrinfo", side_effect=resolve),
                patch.object(A.socket, "socket", side_effect=AssertionError("NO_LATE_NETWORK")),
                patch.object(A.threading, "Thread", side_effect=create_thread),
                self.assertRaisesRegex(A.TransportFailure, "DNS_DEADLINE"),
            ):
                A.https_transport(self.model()["queries"][0], timeout=0.02, maximum=1024)
        finally:
            gate.set()
            for thread in threads:
                thread.join(0.5)
                self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.4)


class HttpFramingV2Tests(unittest.TestCase):
    def exchange(self, wire):
        class Socket:
            def settimeout(self, _timeout):
                pass

            def connect(self, _address):
                pass

            def do_handshake(self):
                pass

            def shutdown(self, _how):
                pass

            def close(self):
                pass

            def makefile(self, *_args, **_kwargs):
                return io.BytesIO(wire)

        sock = Socket()
        response = http.client.HTTPResponse(sock)
        response.begin()

        class Connection:
            sock = None

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return response

            def close(self):
                pass

        request = {
            "kind": "osv",
            "method": "POST",
            "url": "https://api.osv.dev/v1/query",
            "body": {"commit": "a" * 40},
        }
        with (
            patch.object(A.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("fixture", 443))]),
            patch.object(A.socket, "socket", return_value=sock),
            patch.object(
                A.ssl,
                "create_default_context",
                return_value=types.SimpleNamespace(wrap_socket=lambda *_a, **_kw: sock),
            ),
            patch.object(A.http.client, "HTTPSConnection", return_value=Connection()),
        ):
            return A.https_transport(request, timeout=15, maximum=1024)

    def test_short_content_length_is_incomplete_with_exact_partial_bytes(self):
        with self.assertRaises(A.TransportFailure) as caught:
            self.exchange(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}")
        self.assertEqual(caught.exception.status, 200)
        self.assertEqual(caught.exception.partial, b"{}")

    def test_exact_content_length_and_eof_delimited_response_are_valid(self):
        for headers in (b"Content-Length: 2\r\n", b"Connection: close\r\n"):
            with self.subTest(headers=headers):
                self.assertEqual(
                    self.exchange(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n{}"), (200, b"{}")
                )

    def test_truncated_chunked_retains_partial_and_complete_chunked_is_valid(self):
        prefix = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        with self.assertRaises(A.TransportFailure) as caught:
            self.exchange(prefix + b"2\r\n{}\r\n")
        self.assertEqual(caught.exception.partial, b"{}")
        self.assertEqual(self.exchange(prefix + b"2\r\n{}\r\n0\r\n\r\n"), (200, b"{}"))

    def test_chunk_data_truncated_mid_chunk_retains_received_body(self):
        prefix = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        with self.assertRaises(A.TransportFailure) as caught:
            self.exchange(prefix + b"5\r\n{}")
        self.assertEqual(caught.exception.partial, b"{}")

    def test_short_response_cannot_become_a_zero_advisory_audit(self):
        model = TransportTests().model()
        saved = []

        def transport(request, **_kwargs):
            if request["kind"] == "osv":
                return self.exchange(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}")
            return TransportTests().transport(request)

        with self.assertRaises(A.TransportFailure):
            A.collect(
                model,
                transport,
                lambda *args: saved.append(args),
                clock=lambda: 0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(saved[-1][3], b"{}")
        self.assertIs(saved[-1][4], False)

    def test_v1_sources_are_frozen_and_declaration_owner_is_not_renamed(self):
        for name, pin in (
            (
                "docker/valkey/binary_materials.py",
                "2ef9c1735fa908a0099815f6c0e36446e7188271a3f4b5dbde00c18ebd25c442",
            ),
            (
                "docker/valkey/audit_binary_materials.py",
                "fc5c1b225b44d847d0a53da607797ea0271269027230e71352cdf5cc96a14afd",
            ),
            (
                "docker/valkey/binary-sca-inputs.json",
                "f06af3fd6e4e12299bebeb9caceb9f17516e7fafa341647aaefc1d3123ec5632",
            ),
            (
                "tests/test_valkey_binary_sca.py",
                "cf7761f912d46b89cce8a65629df73aa212b7a8206c83da0fc6ff8a9015ac924",
            ),
            (
                "docker/valkey/README-BINARY-SCA.md",
                "4e9256c502f491f1362e771281987b873b5bd10a63c1dc75f4b2304df5cb10f0",
            ),
        ):
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), pin)
        self.assertNotEqual(A.OWNER, A.DECLARATION_OWNER)
        self.assertEqual(
            A.DECLARATION_OWNER,
            json.loads((ROOT / "docker/valkey/binary-sca-inputs.json").read_bytes())["owner"],
        )
        self.assertEqual(
            A.MANIFEST_SHA, "f06af3fd6e4e12299bebeb9caceb9f17516e7fafa341647aaefc1d3123ec5632"
        )


if __name__ == "__main__":
    unittest.main()
