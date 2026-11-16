import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docker import r2_service_boot as boot

PIN = {"cid": "a" * 64}
INSPECT_PIN = {
    **PIN,
    "image": "sha256:" + "b" * 64,
    "deployment": "22222222-2222-4222-8222-222222222222",
    "config_sha256": "c" * 64,
}
STOPPED = {"status": "exited", "pid": 0}
RUNNING = {"status": "running", "pid": 123}


class BootTests(unittest.TestCase):
    def test_adopta_dispositivo_nfs_nuevo_con_el_mismo_recurso(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = root.lstat()
            marker = b"marcador de producciones"
            pin = {
                "media_device": info.st_dev + 1,
                "media_inode": info.st_ino,
                "media_marker_sha256": hashlib.sha256(marker).hexdigest(),
            }
            with (
                patch.object(boot, "MEDIA", directory),
                patch.object(boot, "check_mount"),
                patch.object(boot, "read_regular", return_value=marker),
            ):
                self.assertEqual(boot.nas_mount(pin), (info.st_dev, info.st_ino))

    def test_actualiza_solo_el_dispositivo_de_la_configuracion_acreditada(self):
        config = {
            "media_root": "/media",
            "media_proof": {
                "device": 49,
                "inode": 765,
                "marker_name": boot.MARKER,
                "marker_sha256": "a" * 64,
            },
            "state_proof": {"device": 2050},
        }
        raw = json.dumps(config, separators=(",", ":")).encode()
        pin = {
            "media_device": 49,
            "media_inode": 765,
            "media_marker_sha256": "a" * 64,
            "config_sha256": hashlib.sha256(raw).hexdigest(),
        }
        adjusted = boot.rebase_runtime(raw, pin, (52, 765))
        config["media_proof"]["device"] = 52
        self.assertEqual(json.loads(adjusted), config)
        self.assertEqual(boot.rebase_runtime(adjusted, pin, (52, 765)), adjusted)
        config["state_proof"]["device"] = 3000
        altered = json.dumps(config, separators=(",", ":")).encode()
        with self.assertRaisesRegex(ValueError, "R2_CONFIG_PLANTILLA"):
            boot.rebase_runtime(altered, pin, (52, 765))

    @patch.object(boot, "docker")
    @patch.object(boot, "container", return_value=STOPPED)
    @patch.object(boot, "nas_mount", side_effect=ValueError("R2_NFS_ORIGEN"))
    def test_does_not_start_without_nas(self, _mount, _container, docker):
        with self.assertRaisesRegex(ValueError, "R2_NFS_ORIGEN"):
            boot.start(PIN)
        docker.assert_not_called()

    @patch.object(boot, "health")
    @patch.object(boot, "container_mount")
    @patch.object(boot, "docker")
    @patch.object(boot, "container", side_effect=[STOPPED, RUNNING])
    @patch.object(boot, "nas_mount", return_value=(1, 2))
    @patch.object(boot, "runtime_config")
    def test_starts_same_container_when_mount_is_ready(
        self, config, _mount, _container, docker, verify, health
    ):
        boot.start(PIN)
        config.assert_called_once_with(PIN, (1, 2), write=True)
        docker.assert_called_once_with("start", PIN["cid"])
        verify.assert_called_once_with(RUNNING, (1, 2))
        health.assert_called_once_with()

    @patch.object(boot, "health")
    @patch.object(boot, "container_mount")
    @patch.object(boot, "docker")
    @patch.object(boot, "container", side_effect=[RUNNING, RUNNING])
    @patch.object(boot, "nas_mount", return_value=(1, 2))
    @patch.object(boot, "runtime_config")
    def test_adopts_running_container_without_restarting(
        self, config, _mount, _container, docker, verify, health
    ):
        boot.start(PIN)
        config.assert_called_once_with(PIN, (1, 2), write=False)
        docker.assert_not_called()
        verify.assert_called_once_with(RUNNING, (1, 2))
        health.assert_called_once_with()

    @patch.object(boot, "stop")
    @patch.object(boot, "container_mount", side_effect=ValueError("R2_NFS_ORIGEN"))
    @patch.object(boot, "docker")
    @patch.object(boot, "container", side_effect=[STOPPED, RUNNING])
    @patch.object(boot, "nas_mount", return_value=(1, 2))
    @patch.object(boot, "runtime_config")
    def test_stops_own_container_if_runtime_bind_is_wrong(
        self, _config, _mount, _container, docker, _verify, stop
    ):
        with self.assertRaisesRegex(ValueError, "R2_NFS_ORIGEN"):
            boot.start(PIN)
        docker.assert_called_once_with("start", PIN["cid"])
        stop.assert_called_once_with(PIN)

    @patch.object(boot, "docker")
    @patch.object(boot, "container", side_effect=[RUNNING, STOPPED])
    @patch.object(boot, "nas_mount", side_effect=ValueError("R2_NFS_ORIGEN"))
    def test_stop_can_run_when_nas_is_unavailable(self, mount, _container, docker):
        boot.stop(PIN)
        mount.assert_not_called()
        docker.assert_called_once_with("stop", "--time", "20", PIN["cid"], timeout=30)

    @patch.object(boot, "docker")
    @patch.object(boot, "container", return_value={**RUNNING, "policy": {"Name": "unless-stopped"}})
    def test_handoff_preserves_running_container(self, container, docker):
        self.assertFalse(boot.stop(PIN, handoff=True))
        container.assert_called_once_with(PIN, adoption=True)
        docker.assert_not_called()

    @patch.dict(boot.os.environ, {"NOTIFY_SOCKET": "@test-notify"}, clear=True)
    @patch.object(boot, "docker")
    @patch.object(boot, "notify_ready")
    @patch.object(boot, "start", side_effect=ValueError("R2_NFS_ORIGEN"))
    def test_failure_before_readiness_does_not_notify_or_wait(self, _start, notify, docker):
        with self.assertRaisesRegex(ValueError, "R2_NFS_ORIGEN"):
            boot.supervise(PIN)
        notify.assert_not_called()
        docker.assert_not_called()

    @patch.object(boot, "docker")
    def test_docker_restart_policy_must_not_bypass_nas(self, docker):
        docker.return_value = json.dumps(
            {
                **INSPECT_PIN,
                **RUNNING,
                "name": "/media-r2-serve-" + INSPECT_PIN["deployment"],
                "owner": boot.OWNER,
                "role": "serve",
                "policy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                "mounts": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "R2_RESTART_DOCKER"):
            boot.container(INSPECT_PIN)

    @patch.object(boot, "run", return_value=b"/mnt/producciones /dev/sda1 ext4 rw,relatime\n")
    def test_refuses_fallback_to_local_storage(self, _run):
        with self.assertRaisesRegex(ValueError, "R2_NFS_ORIGEN"):
            boot.check_mount(boot.MEDIA)

    @patch.object(
        boot, "run", return_value=b"/media 192.168.0.8:/volume2/producciones nfs4 rw,vers=4.1\n"
    )
    def test_accepts_exact_namespace_mount(self, run):
        boot.check_mount("/media", "--task", "123")
        run.assert_called_once_with(
            "/usr/bin/findmnt",
            "--task",
            "123",
            "--noheadings",
            "--raw",
            "--mountpoint",
            "/media",
            "-o",
            "TARGET,SOURCE,FSTYPE,OPTIONS",
        )
