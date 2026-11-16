"""Auditoría nueva con reloj y transporte locales; sin consultas externas."""

import http.client
import io
import json
import ssl
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "docker/valkey/audit_binary_materials_v3.py"
    value = types.ModuleType("valkey_binary_v3_tests")
    value.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), vars(value))
    return value


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def request(method="GET", kind="nvd-dictionary"):
    if method == "POST":
        return {
            "component": "valkey",
            "kind": "osv",
            "method": "POST",
            "url": "https://api.osv.dev/v1/query",
            "body": {"commit": "a" * 40},
        }
    return {
        "component": "lua",
        "kind": kind,
        "method": method,
        "url": "https://services.nvd.nist.gov/rest/json/cpes/2.0?keywordSearch=Lua&keywordExactMatch=&resultsPerPage=100&startIndex=0",
    }


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.m = module()
        self.h = self.m.load_helpers()

    def test_closed_ast_excludes_historical_prepare_main_and_publication(self):
        for name in ("prepare", "main", "publish_audit", "RUN_ROOT", "OWNER"):
            self.assertFalse(hasattr(self.h, name))
        self.assertEqual(self.h.BOUNDS["pages_per_query"], 5)
        self.assertEqual(self.h.BOUNDS["responses"], 40)

    def test_five_pages_exhaust_473_without_cropping_products(self):
        starts = []

        def ask(row):
            start = int(self.m.parse_qs(self.m.urlsplit(row["url"]).query)["startIndex"][0])
            starts.append(start)
            count = min(100, 473 - start)
            return {
                "format": "NVD_CPE",
                "version": "2.0",
                "startIndex": start,
                "resultsPerPage": count,
                "totalResults": 473,
                "products": [{"id": i} for i in range(start, start + count)],
            }

        values = self.h.nvd_pages(request(), "NVD_CPE", "products", ask)
        self.assertEqual(starts, [0, 100, 200, 300, 400])
        self.assertEqual([r["id"] for r in values], list(range(473)))

    def test_six_pages_and_changed_totals_fail_closed(self):
        for changed in (False, True):

            def ask(row, changed=changed):
                start = int(self.m.parse_qs(self.m.urlsplit(row["url"]).query)["startIndex"][0])
                return {
                    "format": "NVD_CPE",
                    "version": "2.0",
                    "startIndex": start,
                    "resultsPerPage": 100,
                    "totalResults": 601 if changed and start else 600,
                    "products": [{}] * 100,
                }

            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(ValueError, "NVD_TOTAL_CHANGED|NVD_PAGINATION_LIMIT"),
            ):
                self.h.nvd_pages(request(), "NVD_CPE", "products", ask)

    def test_historical_v2_has_not_been_loaded_or_its_bounds_mutated(self):
        self.assertEqual(self.m.DECLARED_BOUNDS["pages_per_query"], 4)
        self.h.BOUNDS["pages_per_query"] = 99
        self.assertEqual(self.m.load_helpers().BOUNDS["pages_per_query"], 5)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.m = module()
        self.h = self.m.load_helpers()
        self.clock = Clock()
        self.rows = []

    def runner(self, transport):
        return self.m.AttemptTransport(
            self.h, transport, self.rows.append, clock=self.clock, sleep=self.clock.sleep
        )

    def failure(self, error, status=None, partial=b""):
        try:
            raise error
        except BaseException:
            try:
                raise self.h.TransportFailure("HTTPS_INCOMPLETE", status, partial) from None
            except self.h.TransportFailure as failure:
                return failure

    def test_get_timeout_retries_once_and_preserves_each_attempt(self):
        calls = []
        failure = self.failure(TimeoutError("do not disclose me"))

        def send(row, **limits):
            calls.append((self.clock(), limits))
            if len(calls) == 1:
                raise failure
            return 200, b"{}"

        run = self.runner(send)
        self.assertEqual(run(request(), timeout=15, maximum=2097152), (200, b"{}"))
        self.assertEqual(len(self.rows), 2)
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 6.2)
        self.assertEqual(self.rows[0]["diagnostic"]["error_type"], "TimeoutError")
        self.assertNotIn(
            "do not disclose",
            json.dumps([{k: v for k, v in row.items() if k != "raw"} for row in self.rows]),
        )
        self.assertEqual(run.attempts, 2)

    def test_no_retry_post_certificate_framing_partial_or_status(self):
        cases = [
            (request("POST"), self.failure(TimeoutError("secret"))),
            (request(), self.failure(ssl.SSLCertVerificationError(1, "secret"))),
            (request(), self.failure(ValueError("HTTP_TRUNCATED_CONTENT_LENGTH"), 200, b"{}")),
            (request(), self.failure(ConnectionResetError("secret"), 200, b"partial")),
        ]
        for req, failure in cases:
            rows = []

            def fail(*_a, _failure=failure, **_kw):
                raise _failure

            run = self.m.AttemptTransport(
                self.h, fail, rows.append, clock=self.clock, sleep=self.clock.sleep
            )
            with self.subTest(kind=req["kind"]), self.assertRaises(self.h.TransportFailure):
                run(req, timeout=15, maximum=2097152)
            self.assertEqual(run.attempts, 1)
        run = self.runner(lambda *_a, **_kw: (503, b"unavailable"))
        self.assertEqual(run(request(), timeout=15, maximum=2097152), (503, b"unavailable"))
        self.assertEqual(run.attempts, 1)

    def test_retry_is_not_unlimited_and_all_attempts_count_toward_40(self):
        failure = self.failure(ConnectionResetError("secret"))

        def fail(*_a, **_kw):
            raise failure

        run = self.runner(fail)
        with self.assertRaises(self.h.TransportFailure):
            run(request(), timeout=15, maximum=2097152)
        self.assertEqual(run.attempts, 2)
        run.attempts = 40
        with self.assertRaisesRegex(ValueError, "HTTP_COUNT_LIMIT"):
            run(request(), timeout=15, maximum=2097152)
        self.assertEqual(run.attempts, 40)

    def test_40_actual_attempts_prevent_the_41st_network_operation(self):
        calls = []
        run = self.runner(lambda *_a, **_kw: (calls.append(True) or 200, b"{}"))
        for _ in range(40):
            run(request(), timeout=15, maximum=2097152)
        with self.assertRaisesRegex(ValueError, "HTTP_COUNT_LIMIT"):
            run(request(), timeout=15, maximum=2097152)
        self.assertEqual(len(calls), 40)

    def test_exact_32_mib_stops_before_another_network_operation(self):
        calls = []
        body = b"x" * 2097152
        run = self.runner(lambda *_a, **_kw: (calls.append(True) or 200, body))
        for _ in range(16):
            run(request(), timeout=15, maximum=2097152)
        with self.assertRaisesRegex(ValueError, "HTTP_BYTES_LIMIT"):
            run(request(), timeout=15, maximum=2097152)
        self.assertEqual(len(calls), 16)

    def test_global_deadline_and_total_partial_bytes_are_enforced(self):
        run = self.runner(lambda *_a, **_kw: (200, b"{}"))
        self.clock.now = 300
        with self.assertRaisesRegex(ValueError, "AUDIT_DEADLINE"):
            run(request(), timeout=15, maximum=2097152)
        self.clock.now = 0
        run = self.runner(lambda *_a, **_kw: (200, b"{}"))
        run.bytes = self.h.BOUNDS["total_response_bytes"] - 1
        with self.assertRaisesRegex(ValueError, "HTTP_BYTES_LIMIT"):
            run(request(), timeout=15, maximum=2097152)
        self.assertEqual(len(self.rows), 1)

    def test_unexpected_transport_error_still_preserves_one_attempt_without_retry(self):
        def fail(*_a, **_kw):
            raise RuntimeError("sensitive diagnostic must not be copied")

        run = self.runner(fail)
        with self.assertRaises(RuntimeError):
            run(request(), timeout=15, maximum=2097152)
        self.assertEqual(run.attempts, 1)
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(
            self.rows[0]["diagnostic"], {"error_type": "RuntimeError", "phase": "unknown"}
        )


class CliTests(unittest.TestCase):
    def test_default_prints_plan_without_publishing_or_reading_historical_responses(self):
        m = module()
        with (
            patch.object(m, "prepare_data", return_value=({"fresh_collection": True}, {}, None)),
            patch.object(m, "publish", side_effect=AssertionError("NO_PUBLISH")),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(m.main([]), 0)
        self.assertFalse(json.loads(output.getvalue())["audit_executed"])

    def test_apply_external_source_and_plan_pins_precede_namespace_creation(self):
        m = module()
        with (
            patch.object(m, "prepare_data", return_value=({"source_sha256": "a" * 64}, {}, None)),
            patch.object(m, "publish", side_effect=AssertionError("NO_PUBLISH")),
        ):
            with self.assertRaisesRegex(ValueError, "EXTERNAL_AUDIT_PINS"):
                m.main(
                    [
                        "--audit",
                        "--expected-source-sha256",
                        "b" * 64,
                        "--expected-plan-sha256",
                        "c" * 64,
                    ]
                )


class FramingTests(unittest.TestCase):
    def exchange(self, wire):
        m = module()
        h = m.load_helpers()

        class Socket:
            closed = False

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

            def makefile(self, *_a, **_kw):
                return io.BytesIO(wire)

        owned = Socket()
        response = http.client.HTTPResponse(owned)
        response.begin()
        connection = types.SimpleNamespace(
            sock=None,
            request=lambda *_a, **_kw: None,
            getresponse=lambda: response,
            close=lambda: None,
        )
        try:
            with (
                patch.object(
                    h.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("fixture", 443))]
                ),
                patch.object(h.socket, "socket", return_value=owned),
                patch.object(
                    h.ssl,
                    "create_default_context",
                    return_value=types.SimpleNamespace(wrap_socket=lambda *_a, **_kw: owned),
                ),
                patch.object(h.http.client, "HTTPSConnection", return_value=connection),
            ):
                return h.https_transport(request("POST"), timeout=15, maximum=1024)
        finally:
            self.assertTrue(owned.closed)
            self.assertTrue(response.closed)

    def test_actual_stdlib_content_length_exact_and_short_with_partial(self):
        self.assertEqual(
            self.exchange(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"), (200, b"{}")
        )
        with self.assertRaises(ValueError) as caught:
            self.exchange(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}")
        self.assertEqual(caught.exception.partial, b"{}")
        self.assertEqual(caught.exception.status, 200)

    def test_eof_without_length_valid_and_chunked_truncation_not_retried(self):
        self.assertEqual(
            self.exchange(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{}"), (200, b"{}")
        )
        with self.assertRaises(ValueError) as caught:
            self.exchange(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n{}")
        self.assertEqual(caught.exception.partial, b"{}")
        self.assertFalse(module().retryable(request(), caught.exception))


class PublicationTests(unittest.TestCase):
    def fixture(self):
        m = module()
        h = m.load_helpers()
        c = {
            "id": "lua",
            "name": "Lua",
            "scope": "runtime",
            "commit_declared": None,
            "repository": "https://github.com/lua/lua",
            "nvd_keyword": "Lua",
            "nvd_version": "5.1.5",
        }
        outputs = dict.fromkeys(m.OUTPUT_NAMES, b"{}")
        plan = {
            "source_sha256": "a" * 64,
            "outputs": {
                name: {"sha256": m.digest(raw), "bytes": len(raw)} for name, raw in outputs.items()
            },
        }
        model = {"components": [c], "queries": h.C.query_plan([c]), "outputs": outputs}
        return m, h, plan, model

    def test_fresh_publication_is_exclusive_and_never_replays_previous_response(self):
        m, h, plan, model = self.fixture()
        calls = []
        clock = Clock()

        def fresh(row, **_limits):
            calls.append(row)
            return 200, m.canonical(
                {
                    "format": "NVD_CPE",
                    "version": "2.0",
                    "startIndex": 0,
                    "resultsPerPage": 0,
                    "totalResults": 0,
                    "products": [],
                }
            )

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "fresh-v3"
            proof = m.publish(
                plan,
                model,
                h,
                fresh,
                root=root,
                current_plan=lambda: plan,
                clock=clock,
                sleep=clock.sleep,
            )
            self.assertEqual(len(calls), 1)
            self.assertTrue(proof["audit_complete"])
            self.assertEqual(proof["reused_responses"], 0)
            self.assertFalse(proof["binary_accepted"])
            sentinel = (root / "receipt.json").read_bytes()
            with self.assertRaises(FileExistsError):
                m.publish(plan, model, h, fresh, root=root, current_plan=lambda: plan)
            self.assertEqual((root / "receipt.json").read_bytes(), sentinel)
            self.assertEqual(len(calls), 1)

    def test_failure_preserves_partial_attempt_and_has_no_false_positive_receipt(self):
        m, h, plan, model = self.fixture()

        def short(*_a, **_kw):
            raise h.TransportFailure("HTTPS_INCOMPLETE", 200, b"{")

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "failure-v3"
            with self.assertRaises(h.TransportFailure):
                m.publish(plan, model, h, short, root=root, current_plan=lambda: plan)
            self.assertEqual((root / "attempt-01.body").read_bytes(), b"{")
            failure = json.loads((root / "failure.json").read_bytes())
            self.assertFalse(failure["audit_complete"])
            self.assertFalse((root / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
