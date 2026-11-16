import io
import subprocess
import sys
import time
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "n8n"))

import n8n_server_baseline_vm as cli  # noqa: E402
import server_baseline_vm_remote as remote_contract  # noqa: E402


class BaselineVMTests(unittest.TestCase):
    @pytest.mark.evidencia_operativa
    def test_plan_uses_posix_paths_and_keeps_create_separate(self):
        plan, remote, _, _, _ = cli.build_plan()
        self.assertEqual(plan["delta"], "/opt/media-platform/n8n-server-linux-transfer-v2")
        self.assert_create_contract(plan["create_args"])
        self.assertEqual(remote.sha(remote.canonical(plan)), cli.sha(remote.canonical(plan)))

    def test_shipped_create_contract(self):
        raw = cli.source(cli.HERE / "server_baseline_vm_remote.py")
        self.assertEqual(cli.sha(raw), cli.REMOTE_SHA)
        self.assertEqual(
            remote_contract.DELTA.as_posix(), "/opt/media-platform/n8n-server-linux-transfer-v2"
        )
        self.assert_create_contract(remote_contract.create_args())

    def assert_create_contract(self, args):
        self.assertEqual(args[0], "create")
        self.assertNotIn("start", args)
        self.assertNotIn("--rm", args)
        self.assertIn("--network=none", args)
        self.assertIn("--pull=never", args)
        self.assertIn("--cidfile", args)
        self.assertIn(
            "type=bind,src=/opt/media-platform/n8n-js-linux-transfer-v1/inputs,dst=/v5,readonly",
            args,
        )

    def test_bootstrap_checks_pin_and_keeps_following_bytes(self):
        source = (
            b"assert sys.argv[1:]==['receive']; sys.stdout.buffer.write(sys.stdin.buffer.read())"
        )
        payload = cli.framed(source, 32768) + b"next-frame\x00artifact\n"
        argv = [sys.executable, "-I", "-B", "-c", cli.BOOTSTRAP, cli.sha(source), "receive"]
        result = subprocess.run(argv, input=payload, capture_output=True, timeout=5, check=True)
        self.assertEqual(result.stdout, b"next-frame\x00artifact\n")
        argv[-2] = "0" * 64
        rejected = subprocess.run(argv, input=payload, capture_output=True, timeout=5, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, b"")

    def test_receive_ack_handles_split_marker_and_preflight_rejection(self):
        class Channel:
            def __init__(self, raw):
                self.data = io.BytesIO(raw)
                self.size = len(raw)

            def recv_stderr_ready(self):
                return self.data.tell() < self.size

            def recv_stderr(self, size):
                return self.data.read(min(size, 3))

            def exit_status_ready(self):
                return self.size == 0

        marker = b"RECEIVE_READY\n"
        self.assertTrue(cli.wait_ready(Channel(marker), marker, time.monotonic() + 1))
        self.assertFalse(cli.wait_ready(Channel(b""), marker, time.monotonic() + 1))
        with self.assertRaisesRegex(ValueError, "PREPARACION_RESPUESTA"):
            cli.wait_ready(Channel(b"wrong"), marker, time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
