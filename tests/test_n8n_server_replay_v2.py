import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker" / "n8n"))

import n8n_server_replay_vm_v2 as cli  # noqa: E402
import replay_server_linux_baseline_v2 as replay  # noqa: E402


class BaselineReplayV2Tests(unittest.TestCase):
    def test_native_store_without_marker_accepts_exact_catalog_and_rejects_extra_file(self):
        keys = {f"sha512-demo\tdemo-{index}@1.0.0" for index in range(3124)}
        keys.add(replay.base.VENDOR_KEY)
        op = SimpleNamespace(sha=lambda value: hashlib.sha256(value).hexdigest())
        catalogue = op.sha(("\n".join(sorted(keys)) + "\n").encode())
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp).resolve()
            (store / "v11/files").mkdir(parents=True)
            (store / "v11/index.db").touch()
            with (
                patch.object(
                    replay.os, "statvfs", create=True, return_value=SimpleNamespace(f_flag=1)
                ),
                patch.object(replay.os, "ST_RDONLY", create=True, new=1),
                patch.object(replay, "KEYS", catalogue),
                patch.object(replay.base, "store_keys", return_value=(keys, replay.INDEX)),
            ):
                self.assertEqual(replay.verify_store(op, store), replay.INDEX)
                (store / "v11/tarball-integrity").touch()
                with self.assertRaisesRegex(ValueError, "STORE_ARBOL_FISICO"):
                    replay.verify_store(op, store)

    def test_plan_binds_complete_original_read_only_and_reserves_new_container(self):
        source = ROOT / "docker/n8n/server_baseline_vm_remote.py"
        raw = source.read_bytes()
        original = cli.module(raw, cli.REMOTE_V1, "n8n_replay_v2_test_original", source)
        args, _ = cli.container_contract(original, raw)
        self.assertTrue(cli.paths(original)[0].as_posix().endswith("/baseline-replay-v2"))
        self.assertNotIn("start", args())
        self.assertTrue(
            any(arg.endswith("/baseline-seed-v1/store,dst=/store,readonly") for arg in args())
        )
        self.assertIn("/ops/replay_server_linux_baseline_v2.py", args())


if __name__ == "__main__":
    unittest.main()
