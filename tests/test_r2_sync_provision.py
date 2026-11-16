"""Aprovisionamiento de órdenes con fixtures locales, sin NAS o servicios."""

import base64
import errno
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker/r2-sync-consumer"))
import sync_provision as PROVISION  # noqa: E402
from sync_nas_entry import provision  # noqa: E402
from sync_protocol import verify_request  # noqa: E402
from sync_storage import RootProof, StorageError, SyncStorage  # noqa: E402

JOB = "22222222-2222-4222-8222-222222222222"
ASSET = "11111111-1111-4111-8111-111111111111"
SLUG = "job-" + JOB.replace("-", "")
STAGE = ".agency-sync-project-" + JOB + ".staging"
KEY = bytes(range(32))


def authorized(slug=SLUG):
    data = b"x"
    value = {
        "job_id": JOB,
        "asset_id": ASSET,
        "project_slug": slug,
        "extension": "mp4",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "object_key": f"ingest/{JOB}/{ASSET}.mp4",
    }
    raw = json.dumps(value, separators=(",", ":")).encode("ascii")
    fingerprint = hashlib.sha256(raw).hexdigest()
    sig = hmac.new(KEY, f"v1\n42\nPOST\n/v1/sync\n{fingerprint}".encode(), "sha256").hexdigest()
    return value, verify_request(raw, "42", f"v1={sig}", KEY, 1024, lambda: 42)


class PortableContractTests(unittest.TestCase):
    def test_unsigned_value_and_noncanonical_signed_slug_cannot_open_storage(self):
        value, _ = authorized()
        invalid = [value, None, authorized("existing")[1], authorized("job-" + "a" * 32)[1]]
        for supplied in invalid:
            with self.subTest(supplied=type(supplied)), self.assertRaises(StorageError) as err:
                PROVISION.ensure_project(None, None, supplied)
            self.assertEqual(err.exception.code, "ORDEN_APROVISIONAMIENTO_INVALIDA")

    def test_native_rename_requires_exact_noreplace_and_reports_unsupported(self):
        native = unittest.mock.Mock(return_value=-1)
        with (
            patch.object(
                PROVISION.ctypes, "CDLL", return_value=unittest.mock.Mock(renameat2=native)
            ),
            patch.object(PROVISION.ctypes, "get_errno", return_value=errno.EINVAL),
            self.assertRaises(StorageError) as result,
        ):
            PROVISION._rename_noreplace(51, STAGE, SLUG)
        native.assert_called_once_with(51, STAGE.encode(), 51, SLUG.encode(), 1)
        self.assertEqual(result.exception.code, "RENOMBRE_EXCLUSIVO_NO_SOPORTADO")


@unittest.skipUnless(sys.platform.startswith("linux"), "Requiere Linux dir_fd y renameat2.")
class NativeProvisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.media = self.base / "media"
        self.projects = self.media / "proyectos"
        self.projects.mkdir(parents=True, mode=0o700)
        self.root_marker = self.media / ".root"
        self.root_marker.write_bytes(b"authenticated root")
        info = self.media.stat()
        self.storage = SyncStorage(
            self.media,
            RootProof(
                info.st_dev, info.st_ino, ".root", hashlib.sha256(b"authenticated root").hexdigest()
            ),
            max_object_bytes=1024,
        )
        info = self.projects.stat()
        self.proof = PROVISION.ProjectsProof(info.st_dev, info.st_ino, info.st_uid, info.st_gid)
        self.value, self.verified = authorized()
        self.stage = self.projects / STAGE
        self.project = self.projects / SLUG

    def ensure(self):
        return PROVISION.ensure_project(self.storage, self.proof, self.verified)

    def test_native_create_idempotent_and_existing_frozen_publisher_works(self):
        result = self.ensure()
        self.assertTrue(result.created)
        self.assertEqual(result.inode, self.project.stat().st_ino)
        self.assertEqual(list((self.project / "01_raw").iterdir()), [])
        self.assertFalse(self.stage.exists())
        publication = self.storage.publish(self.value, iter((b"x",)))
        self.assertTrue(publication.created)
        repeat = self.ensure()
        self.assertFalse(repeat.created)
        self.assertEqual(repeat.inode, result.inode)

    def test_el_mismo_filesystem_se_recupera_con_otro_numero_de_dispositivo(self):
        previous_device = self.proof.device + 1
        profile = self.nas_profile()
        profile["media_proof"]["device"] = previous_device
        profile["projects_proof"]["device"] = previous_device
        with self.assertRaises(StorageError):
            self.nas_request(profile)

        profile["filesystem_id"] = os.statvfs(self.media).f_fsid
        result = self.nas_request(profile)
        replay = self.nas_request(profile)
        self.assertTrue(result["created"])
        self.assertEqual(result["inode"], self.project.stat().st_ino)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["inode"], result["inode"])
        self.assertEqual(profile["media_proof"]["device"], previous_device)
        self.assertEqual(profile["projects_proof"]["device"], previous_device)

    def test_rechaza_otra_identidad_de_filesystem_o_marcador_aun_con_el_mismo_dispositivo(self):
        filesystem_id = os.statvfs(self.media).f_fsid
        profile = self.nas_profile()
        profile["filesystem_id"] = filesystem_id ^ 1
        with self.assertRaises(StorageError):
            self.nas_request(profile)
        profile["filesystem_id"] = filesystem_id
        profile["projects_proof"]["inode"] += 1
        with self.assertRaises(StorageError):
            self.nas_request(profile)
        profile["projects_proof"]["inode"] = self.proof.inode
        self.root_marker.write_bytes(b"replacement marker")
        with self.assertRaises(StorageError):
            self.nas_request(profile)
        self.assertEqual(list(self.projects.iterdir()), [])

    def nas_profile(self):
        return {
            "media_root": str(self.media),
            "media_proof": asdict(self.storage.proof),
            "projects_proof": asdict(self.proof),
            "hmac_file": str(self.base / "hmac"),
            "max_object_bytes": 1024,
        }

    def nas_request(self, profile):
        raw = json.dumps(self.value, separators=(",", ":")).encode("ascii")
        message = f"v1\n42\nPOST\n/v1/sync\n{self.verified.fingerprint}".encode()
        signature = "v1=" + hmac.digest(KEY, message, "sha256").hex()
        body = {
            "body": base64.b64encode(raw).decode("ascii"),
            "timestamp": "42",
            "signature": signature,
        }
        with (
            patch("sync_nas_entry.read_file", return_value=KEY),
            patch("sync_nas_entry.time.time", return_value=42),
        ):
            response, _ = provision(profile, json.dumps(body).encode())
        return json.loads(response)

    def test_strict_proof_and_symlink_or_unmarked_project_are_never_adopted(self):
        self.project.mkdir()
        empty = self.project.stat().st_ino
        with self.assertRaises(StorageError):
            self.ensure()
        self.assertEqual(self.project.stat().st_ino, empty)
        self.project.rmdir()
        target = self.base / "unrelated"
        target.mkdir()
        self.project.symlink_to(target, target_is_directory=True)
        with self.assertRaises(StorageError):
            self.ensure()
        self.assertEqual(list(target.iterdir()), [])
        self.project.unlink()
        with self.assertRaises(StorageError):
            other = PROVISION.ProjectsProof(self.proof.device, 1, self.proof.uid, self.proof.gid)
            PROVISION.ensure_project(self.storage, other, self.verified)
        self.assertEqual(list(self.projects.iterdir()), [])

    def test_unsupported_nfs_leaves_only_own_stage_and_native_retry_recovers(self):
        with patch.object(
            PROVISION,
            "_rename_noreplace",
            side_effect=StorageError("RENOMBRE_EXCLUSIVO_NO_SOPORTADO"),
        ):
            with self.assertRaises(StorageError) as result:
                self.ensure()
        self.assertEqual(result.exception.code, "RENOMBRE_EXCLUSIVO_NO_SOPORTADO")
        staged_inode = self.stage.stat().st_ino
        self.assertFalse(self.project.exists())
        result = self.ensure()
        self.assertEqual(result.inode, staged_inode)
        self.assertFalse(self.stage.exists())

    def test_empty_stage_collision_is_memorialized_and_raw_symlink_never_followed(self):
        self.stage.mkdir(mode=0o700)
        with self.assertRaises(StorageError):
            self.ensure()
        self.assertEqual(list(self.stage.iterdir()), [])
        self.stage.rmdir()
        self.ensure()
        (self.project / "01_raw").rmdir()
        target = self.base / "outside"
        target.mkdir()
        (self.project / "01_raw").symlink_to(target, target_is_directory=True)
        with self.assertRaises(StorageError):
            self.ensure()
        self.assertFalse(any(target.iterdir()))

    def test_concurrent_foreign_destination_is_preserved_even_if_empty(self):
        native = PROVISION._rename_noreplace

        def collide(parent, source, destination):
            self.project.mkdir(mode=0o700)
            self.foreign = self.project.stat().st_ino
            return native(parent, source, destination)

        with patch.object(PROVISION, "_rename_noreplace", side_effect=collide):
            with self.assertRaises(StorageError):
                self.ensure()
        self.assertEqual(self.project.stat().st_ino, self.foreign)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertTrue(self.stage.is_dir())

    def test_ambiguous_rename_reconciles_only_by_the_durable_marker_and_inode(self):
        rename = PROVISION._rename_noreplace

        def lost_reply(*args):
            rename(*args)
            raise OSError(errno.EIO, "reply was lost")

        with patch.object(PROVISION, "_rename_noreplace", side_effect=lost_reply):
            result = self.ensure()
        self.assertEqual(result.inode, self.project.stat().st_ino)
        self.assertFalse(self.stage.exists())
