import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "n8n"))

import n8n_server_replay_vm as cli  # noqa: E402
import replay_server_linux_baseline as replay  # noqa: E402
import server_baseline_vm_remote as original_contract  # noqa: E402


class BaselineReplayTests(unittest.TestCase):
    @pytest.mark.evidencia_operativa
    def test_frozen_offline_copy_and_full_store_read_only(self):
        plan, _, original, raw, _, _, _, _ = cli.build_plan()
        self.assert_offline_contract(original, plan["create_args"])
        self.assertEqual(cli.container_contract(original, raw)[0](), plan["create_args"])

    def test_shipped_replay_is_offline_and_read_only(self):
        raw = Path(original_contract.__file__).read_bytes()
        self.assertEqual(cli.sha(raw), cli.REMOTE_V1)
        self.assert_offline_contract(
            original_contract, cli.container_contract(original_contract, raw)[0]()
        )

    def assert_offline_contract(self, original, args):
        source = "/opt/media-platform/n8n-server-linux-transfer-v2/baseline-control-v1/work/baseline-seed-v1/store"
        self.assertIn(f"type=bind,src={source},dst=/store,readonly", args)
        self.assertIn("--network=none", args)
        self.assertIn("--pull=never", args)
        self.assertNotIn("start", args)
        self.assertNotEqual(cli.NAME, original.NAME)
        self.assertNotEqual(cli.paths(original)[0], original.CONTROL)
        command = replay.command(Path("/work/pnpm"), Path("/work/project"))
        self.assertIn("--config.package-import-method=copy", command)
        self.assertIn("--config.frozen-store=true", command)
        self.assertIn("--offline", command)
        self.assertIn("--frozen-lockfile", command)
        self.assertIn("--ignore-scripts", command)

    def test_accepts_only_real_policy_count_and_zero_downloads(self):
        positive = "✓ Lockfile passes supply-chain policies (3476 entries in 1s)\n".encode()
        progress = b"Progress: resolved 3164, reused 3125, downloaded 0, added 3164, done\n"
        self.assertEqual(replay.parse_log(positive + progress)["added"], 3164)
        for invalid in (
            positive + positive + progress,
            positive.replace(b"3476", b"3478") + progress,
            positive + progress.replace(b"downloaded 0", b"downloaded 1"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replay.parse_log(invalid)

    def test_store_rejects_writable_view_and_checks_local_marker(self):
        with (
            patch.object(replay.os, "statvfs", create=True, return_value=SimpleNamespace(f_flag=0)),
            patch.object(replay.os, "ST_RDONLY", create=True, new=1),
        ):
            with self.assertRaisesRegex(ValueError, "STORE_DEBE_ESTAR_RO"):
                replay.verify_store(None, Path(__file__).resolve().parent)
        self.assertEqual(
            replay.MARKER, "v11/file+vendor+wa-sqlite-1.0.9-upstream-nogit.tgz/tarball-integrity"
        )


if __name__ == "__main__":
    unittest.main()
