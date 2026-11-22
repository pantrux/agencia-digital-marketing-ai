"""Pruebas del control de tarea sin iniciar ni detener procesos reales."""

import importlib.util
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "sre/pause_vault_quota_archive_v2.py"
SPEC = importlib.util.spec_from_file_location("vault_archive_pause_test", SOURCE)
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


def test_control_carga_el_archivador_con_su_huella_real():
    archiver = CONTROL.load_archiver()
    assert Path(archiver.__file__).resolve() == SOURCE.with_name("archive_vault_changes_v2.py")


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.archiver = SimpleNamespace(
            safe_stop_check=lambda runtime: self.events.append("no-workers")
        )
        self.runtime = SimpleNamespace(
            queue=SimpleNamespace(uploader_lock=lambda: self.lock("uploader")),
            store=SimpleNamespace(lock=lambda name: self.lock(name)),
        )

    @contextmanager
    def lock(self, name):
        self.events.append("enter-" + name)
        try:
            yield
        finally:
            self.events.append("exit-" + name)

    def task(self, plan, action, pid, born=""):
        self.events.append((action, pid, born))
        return {"state": "Ready", "born": "original-creation"}

    def test_pause_guards_process_identity_in_lock_order(self):
        with patch.object(CONTROL, "task_action", side_effect=self.task):
            self.assertTrue(CONTROL.pause(self.archiver, self.runtime, {}, 42)["paused"])
        self.assertEqual(
            self.events,
            [
                ("inspect", 42, ""),
                "enter-uploader",
                "enter-archive-stop-guard",
                ("stop", 42, "original-creation"),
                "exit-archive-stop-guard",
                "exit-uploader",
                "no-workers",
            ],
        )

    def test_child_refusal_releases_both_guards(self):
        action = Mock(
            side_effect=[{"born": "original-creation"}, CONTROL.Rejected("TASK_CONTROL_15")]
        )
        with (
            patch.object(CONTROL, "task_action", action),
            self.assertRaisesRegex(CONTROL.Rejected, "TASK_CONTROL_15"),
        ):
            CONTROL.pause(self.archiver, self.runtime, {}, 42)
        self.assertEqual(
            self.events,
            [
                "enter-uploader",
                "enter-archive-stop-guard",
                "exit-archive-stop-guard",
                "exit-uploader",
            ],
        )

    def test_strict_resume_requires_archive_before_start(self):
        with (
            patch.object(CONTROL, "task_action") as action,
            patch.object(
                CONTROL, "verify_archive", side_effect=CONTROL.Rejected("ARCHIVE_NOT_COMPLETE")
            ),
            self.assertRaisesRegex(CONTROL.Rejected, "ARCHIVE_NOT_COMPLETE"),
        ):
            CONTROL.resume(self.archiver, self.runtime, {}, "op", "pin")
        action.assert_not_called()

    def test_pause_reconciles_timeout_without_reissuing_stop(self):
        action = Mock(
            side_effect=[
                {"born": "original-creation"},
                subprocess.TimeoutExpired("fixture", 50),
                {"state": "Ready"},
            ]
        )
        with patch.object(CONTROL, "task_action", action):
            self.assertTrue(CONTROL.pause(self.archiver, self.runtime, {}, 42)["paused"])
        self.assertEqual(
            [call.args[1] for call in action.call_args_list], ["inspect", "stop", "stopped"]
        )

    def test_pause_fails_closed_if_timed_out_stop_cannot_be_confirmed(self):
        action = Mock(
            side_effect=[
                {"born": "original-creation"},
                CONTROL.Rejected("TASK_CONTROL_16"),
                CONTROL.Rejected("TASK_CONTROL_16"),
            ]
        )
        with (
            patch.object(CONTROL, "task_action", action),
            self.assertRaisesRegex(CONTROL.Rejected, "TASK_STOP_RESULT_UNVERIFIED"),
        ):
            CONTROL.pause(self.archiver, self.runtime, {}, 42)
        self.assertEqual(
            [call.args[1] for call in action.call_args_list], ["inspect", "stop", "stopped"]
        )
        self.assertEqual(self.events[-2:], ["exit-archive-stop-guard", "exit-uploader"])

    def test_resume_waits_for_fresh_capture(self):
        capture = self.runtime.store.json = Mock(
            side_effect=[
                {"capture": {"state": "observando", "updated_ns": 99}},
                {"capture": {"state": "observando", "updated_ns": 101}},
            ]
        )
        with (
            patch.object(
                CONTROL,
                "task_action",
                side_effect=[
                    subprocess.TimeoutExpired("fixture", 30),
                    {"state": "Running", "pid": 84},
                ],
            ) as action,
            patch.object(CONTROL, "verify_archive") as verify_archive,
            patch.object(CONTROL.time, "time_ns", return_value=100),
            patch.object(CONTROL.time, "sleep"),
        ):
            result = CONTROL.resume(self.archiver, self.runtime, {}, "op", "pin")
        verify_archive.assert_called_once_with(self.archiver, self.runtime, "op", "pin")
        self.assertEqual([call.args[1] for call in action.call_args_list], ["start", "verify"])
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(result, {"resumed": True, "pid": 84, "capture_verified": True})
