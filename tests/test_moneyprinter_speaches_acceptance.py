"""Fixtures MIT propios; ningún test llama a la LAN, importa App o usa claves reales."""

import base64
import contextlib
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if "CORE" not in globals():
    path = ROOT / "docker/moneyprinter/accept_speaches.py"
    CORE = types.ModuleType("mpt_acceptance_test_core")
    CORE.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), CORE.__dict__)
BUNDLE = globals().get("BUNDLE")


def response():
    words = [
        {"word": " Acción", "start": 0, "end": 0.5, "probability": 0.91},
        {"word": " completa.", "start": 0.5, "end": 1.25, "probability": 0.92},
    ]
    return {
        "text": "Acción completa.",
        "language": "es",
        "duration": 1.25,
        "words": words,
        "segments": [{"start": 0, "end": 1.25, "text": "Acción completa.", "words": words}],
    }


class Wire:
    """Sólo socketpair propio; factory de conexión sustituida durante el fixture."""

    def __init__(self, client, body=None, status=200, hold=False, truncated=False):
        self.module, self.status, self.hold, self.truncated = client, status, hold, truncated
        self.body = json.dumps(response(), ensure_ascii=False).encode() if body is None else body
        self.client, self.server = socket.socketpair()
        self.server.settimeout(2)
        self.release = threading.Event()
        self.request = bytearray()
        self.thread = threading.Thread(target=self.serve, name="mpt-owned-wire")

    def serve(self):
        try:
            while b"\r\n\r\n" not in self.request:
                block = self.server.recv(65536)
                if not block:
                    return
                self.request.extend(block)
                if len(self.request) > 26214400:
                    raise ValueError("FIXTURE_REQUEST_LIMIT")
            header, body = bytes(self.request).split(b"\r\n\r\n", 1)
            length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in header.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            while len(body) < length:
                block = self.server.recv(65536)
                if not block:
                    return
                body += block
            self.request = bytearray(header + b"\r\n\r\n" + body)
            self.server.sendall(
                f"HTTP/1.1 {self.status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {len(self.body)}\r\nConnection: close\r\n\r\n".encode()
            )
            if self.hold:
                self.release.wait(2)
            else:
                self.server.sendall(self.body[:-1] if self.truncated else self.body)
        except (OSError, ValueError, StopIteration):
            pass
        finally:
            self.server.close()

    def connect(self, deadline):
        wire = self

        class Connection(self.module._FixedConnection):
            def connect(self):
                self.sock = wire.client
                deadline.attach(self.sock)
                self.sock.settimeout(deadline.check())

        return Connection(deadline)

    def __enter__(self):
        self.thread.start()
        self.patcher = patch.object(self.module, "_connect", self.connect)
        self.patcher.start()
        return self

    def __exit__(self, *_):
        self.patcher.stop()
        self.release.set()
        self.client.close()
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("Hilo de fixture vivo")


class AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = BUNDLE if BUNDLE is not None else CORE.build_bundle(ROOT)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.audio = self.work / "own.wav"
        self.audio.write_bytes(b"RIFF-fixture-MIT-propio")
        self.secret = self.work / "synthetic-token"
        self.secret.write_bytes(b"TEST_ONLY_NOT_A_CREDENTIAL_0000000000000000")
        self.secret.chmod(0o600)

    @contextlib.contextmanager
    def modules(self):
        with CORE.modules(self.bundle) as modules:
            if sys.platform.startswith("linux"):
                yield modules
            else:
                with patch.object(
                    modules.client,
                    "_read_token",
                    return_value="TEST_ONLY_NOT_A_CREDENTIAL_0000000000000000",
                ):
                    yield modules

    def test_inventory_rejects_tamper_and_extra_inputs(self):
        CORE.validate_bundle(self.bundle)
        for changed in [dict(self.bundle, foreign=True), json.loads(CORE.canonical(self.bundle))]:
            if "foreign" not in changed:
                changed["inputs"]["provider.py"]["data"] = base64.b64encode(b"changed").decode()
            with self.assertRaises(ValueError):
                CORE.validate_bundle(changed)

    def test_original_and_patched_fragments_are_hash_bound(self):
        for row in self.bundle["fragments"]:
            self.assertEqual(CORE.sha(base64.b64decode(row["data"])), row["sha256"])
            self.assertEqual(row["origin_sha256"], self.bundle["inputs"][row["origin"]]["sha256"])
        kinds = {row["origin"] for row in self.bundle["fragments"]}
        self.assertIn("original-subtitle.py", kinds)
        self.assertIn("subtitle.py", kinds)

    def test_extract_rejects_missing_duplicate_or_unexpected_function(self):
        for raw in [b"x=1\n", b"def create(): pass\ndef create(): pass\n"]:
            with self.assertRaises(ValueError):
                CORE.fragments(raw, "test.py", {"create"})

    def test_loads_only_pinned_mit_modules_and_restores_globals(self):
        before = {name: sys.modules.get(name) for name in CORE.MODULE_NAMES}
        with CORE.modules(self.bundle) as modules:
            self.assertEqual(modules.client.HOST, "192.168.0.6")
            self.assertEqual(modules.client.MODEL, "Systran/faster-whisper-small")
        self.assertEqual({name: sys.modules.get(name) for name in CORE.MODULE_NAMES}, before)

    def test_valid_provider_uses_original_multipart_protocol(self):
        with self.modules() as m, Wire(m.client) as wire:
            result = m.provider.transcribe(self.audio, self.secret, word_level=True)
        self.assertEqual([c.text for c in result.cues], ["Acción", "completa."])
        self.assertEqual([(c.start, c.end) for c in result.cues], [(0.0, 0.5), (0.5, 1.25)])
        self.assertIn(b"POST /v1/audio/transcriptions HTTP/1.1", wire.request)
        for value in [
            b"192.168.0.6:8000",
            b"Systran/faster-whisper-small",
            b"verbose_json",
            b"word",
            b"false",
        ]:
            self.assertIn(value, wire.request)

    def test_silence_is_simulated_and_does_not_replace_existing_subtitle(self):
        silence = {"text": "", "language": None, "duration": 1, "words": [], "segments": []}
        target = self.work / "subtitle.srt"
        target.write_text("foreign-sentinel", encoding="utf-8")
        with self.modules() as m, Wire(m.client, json.dumps(silence).encode()):
            scope = CORE.scopes(self.bundle, m.provider, self.work, self.secret, lambda p: [])
            self.assertEqual(scope.create(self.audio, target), "silence")
        self.assertEqual(target.read_text(), "foreign-sentinel")

    def test_unauthorized_and_busy_are_sanitized_without_fallback(self):
        for status, code in [
            (401, "AUTH_REJECTED"),
            (429, "BUSY"),
            (503, "UPSTREAM_REJECTED"),
            (302, "REDIRECT_REJECTED"),
        ]:
            with self.modules() as m, Wire(m.client, b"SYNTHETIC_PRIVATE_BODY", status=status):
                with self.assertRaises(m.provider.ProviderError) as caught:
                    m.provider.transcribe(self.audio, self.secret)
                self.assertEqual(str(caught.exception), "MPT_SPEACHES_" + code)
                self.assertNotIn("PRIVATE", repr(caught.exception))

    def test_missing_probability_and_duplicate_json_fail(self):
        malformed = response()
        malformed["words"][0].pop("probability")
        for raw in [json.dumps(malformed).encode(), b'{"text":"x","text":"y"}']:
            with self.modules() as m, Wire(m.client, raw):
                with self.assertRaises(m.provider.ProviderError):
                    m.provider.transcribe(self.audio, self.secret)

    def test_truncated_response_fails_and_closes_socket(self):
        with self.modules() as m, Wire(m.client, truncated=True) as wire:
            with self.assertRaises(m.provider.ProviderError):
                m.provider.transcribe(self.audio, self.secret)
        self.assertEqual(wire.client.fileno(), -1)

    def test_cancel_interrupts_only_own_transport(self):
        event = threading.Event()
        with self.modules() as m, Wire(m.client, hold=True) as wire:
            timer = threading.Timer(0.05, event.set)
            timer.start()
            try:
                with self.assertRaisesRegex(m.provider.ProviderError, "MPT_SPEACHES_CANCELLED"):
                    m.provider.transcribe(self.audio, self.secret, cancel_event=event)
            finally:
                timer.cancel()
                timer.join(1)
        self.assertFalse(wire.thread.is_alive())

    def test_timeout_error_is_sanitized(self):
        with (
            self.modules() as m,
            patch.object(m.client, "_connect", side_effect=TimeoutError("SYNTHETIC_PRIVATE")),
        ):
            with self.assertRaisesRegex(m.provider.ProviderError, "MPT_SPEACHES_TIMEOUT"):
                m.provider.transcribe(self.audio, self.secret)

    def test_oversize_rejects_before_connection(self):
        with self.audio.open("wb") as stream:
            stream.truncate(24 * 1024**2 + 1)
        with self.modules() as m, patch.object(m.client, "_connect") as connect:
            with self.assertRaisesRegex(m.provider.ProviderError, "MPT_SPEACHES_AUDIO_TOO_LARGE"):
                m.provider.transcribe(self.audio, self.secret)
            connect.assert_not_called()

    def test_token_permissions_and_symlinks_are_explicit(self):
        with CORE.modules(self.bundle) as m:
            if not sys.platform.startswith("linux"):
                with self.assertRaises(m.contract.SpeachesError):
                    m.client._read_token(self.secret)
                return
            self.assertTrue(m.client._read_token(self.secret).startswith("TEST_ONLY"))
            self.secret.chmod(0o644)
            with self.assertRaises(m.contract.SpeachesError):
                m.client._read_token(self.secret)
            self.secret.chmod(0o600)
            link = self.work / "link"
            link.symlink_to(self.secret)
            with self.assertRaises(m.contract.SpeachesError):
                m.client._read_token(link)

    def test_srt_wheel_probe_preserves_unicode_multiline_and_timings(self):
        result = CORE.srt_probe(self.bundle, self.work)
        self.assertTrue(result["unicode_multiline_roundtrip"])
        self.assertTrue(result["invalid_input_rejected"])
        self.assertFalse(result["installed"])

    def test_real_response_conversion_scope_does_not_claim_app_import(self):
        with self.modules() as m:
            result = CORE.publish_response(self.bundle, m.provider, response(), self.work)
        self.assertEqual(result["word_cues"], 2)
        self.assertEqual(result["sentence_cues"], 1)
        self.assertFalse(result["application_imported"])
        self.assertEqual(result["published_from_extracted_functions"], True)

    def test_generate_uses_recognition_not_script_and_keeps_foreign_sentinel(self):
        with self.modules() as m:
            srt = CORE.srt_module(self.bundle, self.work)
            try:
                outcome = m.provider.convert_response(response())
                proxy = CORE.replay_provider(m.provider, outcome)
                scope = CORE.scopes(
                    self.bundle,
                    proxy,
                    self.work,
                    self.secret,
                    lambda p: list(srt.parse(Path(p).read_text(encoding="utf-8"))),
                )
                params = types.SimpleNamespace(
                    subtitle_enabled=True, subtitle_display_mode="sentence"
                )
                target = scope.generate_subtitle(
                    "own-task", params, "NO_CAMBIAR_PALABRAS", None, self.audio
                )
                self.assertIn("Acción completa.", Path(target).read_text(encoding="utf-8"))
                self.assertNotIn("NO_CAMBIAR", Path(target).read_text(encoding="utf-8"))
            finally:
                CORE.release_srt(srt)

    def test_plan_never_reads_vault_or_secrets(self):
        original = CORE.read_regular

        def read(path, *args, **kwargs):
            self.assertNotEqual(Path(path).name, ".env")
            return original(path, *args, **kwargs)

        with (
            patch.object(CORE, "read_regular", side_effect=read),
            patch.object(CORE, "build_bundle", return_value=self.bundle),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(CORE.main([]), 0)

    def test_plan_is_valid_json_even_with_ascii_console(self):
        buffer = io.BytesIO()
        console = io.TextIOWrapper(buffer, encoding="ascii", write_through=True)
        try:
            with (
                patch.object(CORE, "build_bundle", return_value=self.bundle),
                patch.object(CORE.sys, "stdout", console),
            ):
                self.assertEqual(CORE.main([]), 0)
            self.assertEqual(
                json.loads(buffer.getvalue())["bundle_sha256"],
                CORE.sha(CORE.canonical(self.bundle)),
            )
        finally:
            console.close()

    def remote(self):
        module = types.ModuleType("mpt_acceptance_remote_test")
        module.__file__ = str(ROOT / "docker/moneyprinter/accept_speaches_remote.py")
        exec(
            compile(
                CORE.decode(self.bundle["code"]["accept_speaches_remote.py"]),
                module.__file__,
                "exec",
            ),
            module.__dict__,
        )
        return module

    def test_remote_framing_rejects_truncation_and_oversize(self):
        remote = self.remote()
        self.assertEqual(remote.frame(io.BytesIO(b"\0\0\0\3abc"), 3), b"abc")
        for raw in [b"", b"\0\0", b"\0\0\0\4abc", b"\0\0\0\0"]:
            with self.assertRaises(ValueError):
                remote.frame(io.BytesIO(raw), 3)

    def test_remote_default_has_no_stdin_host_or_http_effect(self):
        remote = self.remote()
        with (
            patch.object(remote, "host_guard", side_effect=AssertionError("NO_HOST")),
            patch.object(remote, "frame", side_effect=AssertionError("NO_STDIN")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(remote.main([]), 0)

    def test_remote_exclusive_writes_or_unsupported_platform_are_explicit(self):
        remote = self.remote()
        with self.assertRaises(ValueError):
            remote.open_root({"owner": "foreign"})
        if not sys.platform.startswith("linux"):
            with self.assertRaises(ValueError):
                remote.write_at(-1, "../foreign", b"no")
            return
        descriptor = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY)
        try:
            remote.write_at(descriptor, "owned.txt", b"sentinel")
            with self.assertRaises(FileExistsError):
                remote.write_at(descriptor, "owned.txt", b"changed")
            self.assertEqual((self.work / "owned.txt").read_bytes(), b"sentinel")
        finally:
            os.close(descriptor)

    def test_http_error_removes_only_own_token_and_closes_descriptor(self):
        remote = self.remote()
        token = b"TEST_ONLY_NOT_A_CREDENTIAL_0000000000000000"

        class Rejected(ValueError):
            code = "SPEACHES_AUTH_REJECTED"

        client = types.SimpleNamespace(
            transcribe_audio=unittest.mock.Mock(
                side_effect=[Rejected(), ValueError("SYNTHETIC_NETWORK_FAILURE")]
            )
        )
        fake = types.SimpleNamespace(
            modules=lambda _bundle: contextlib.nullcontext(
                types.SimpleNamespace(
                    client=client, contract=types.SimpleNamespace(SpeachesError=Rejected)
                )
            )
        )
        node = types.SimpleNamespace(
            st_dev=1,
            st_ino=2,
            st_nlink=1,
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.geteuid() if hasattr(os, "geteuid") else 0,
            st_gid=os.geteuid() if hasattr(os, "geteuid") else 0,
        )

        class Stream(io.BytesIO):
            def fileno(self):
                return 122

        for changed in [False, True]:
            client.transcribe_audio.side_effect = [
                Rejected(),
                ValueError("SYNTHETIC_NETWORK_FAILURE"),
            ]
            with (
                patch.object(remote.os, "O_DIRECTORY", 0, create=True),
                patch.object(remote.os, "O_NOFOLLOW", 0, create=True),
                patch.object(remote.os, "open", side_effect=[121, 122]),
                patch.object(remote.os, "fstat", return_value=node),
                patch.object(remote.os, "stat", return_value=node),
                patch.object(remote.os, "fdopen", return_value=Stream()),
                patch.object(remote.os, "fsync"),
                patch.object(remote.os, "close") as close,
                patch.object(remote.os, "unlink") as unlink,
                patch.object(remote, "checked"),
                patch.object(remote, "write_at"),
                patch.object(remote, "read_at", return_value=b"FOREIGN" if changed else token),
            ):
                with self.assertRaises(ValueError):
                    remote.http_probe(fake, self.bundle, self.work, token)
                if changed:
                    unlink.assert_not_called()
                else:
                    unlink.assert_called_once_with("bearer", dir_fd=121)
                self.assertIn(unittest.mock.call(121), close.call_args_list)
                self.assertIn(unittest.mock.call(122), close.call_args_list)


if __name__ == "__main__":
    unittest.main()
