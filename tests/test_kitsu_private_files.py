"""Planes y guardas de archivos Kitsu, sin credenciales reales ni infraestructura."""

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "kitsu_files", ROOT / "docker/kitsu-zou/prepare_private_files.py"
)
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)
REMOTE_SPEC = importlib.util.spec_from_file_location(
    "kitsu_files_remote", ROOT / "docker/kitsu-zou/private_files_remote.py"
)
remote = importlib.util.module_from_spec(REMOTE_SPEC)
REMOTE_SPEC.loader.exec_module(remote)


def fixture():
    identities = {
        "postgres_admin_user": "fixture_admin",
        "postgres_admin_database": "postgres",
        "zou_database_user": "fixture_zou",
        "zou_database_name": "fixture_zoudb",
    }
    # Valores públicos de fixture; ninguna generación de credenciales operativas.
    files = {
        "postgres_password": b'fixture-admin-:"\\password\n',
        "valkey_password": b'fixture-valkey-"\\password\n',
        "zou_db_password": b"fixture-zou-password\n",
        "zou_kv_password": b'fixture-valkey-"\\password\n',
        "zou_secret_key": b"fixture-application-key-public\n",
        "zou_admin_password": b"fixture-admin-application-password\n",
    }
    files["postgres_pgpass"] = core.pgpass(
        identities, core.secret_value(files["postgres_password"])
    )
    files["valkey.conf"] = core.valkey_config(core.secret_value(files["valkey_password"]))
    metadata = {
        name: {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            **core.FILE_POLICY[name],
        }
        for name, raw in files.items()
    }
    return core.make_plan(identities, metadata), files


class PrivateFilesTests(unittest.TestCase):
    def test_plan_contains_only_hashes_and_fixed_paths(self):
        plan, files = fixture()
        core.validate_plan(plan)
        encoded = core.canonical(plan)
        self.assertNotIn(files["zou_secret_key"].strip(), encoded)
        self.assertEqual(set(plan["targets"]), {"config", "data"})
        self.assertEqual(len(plan["files"]), 8)

    def test_password_and_pgpass_escaping_are_literal(self):
        plan, files = fixture()
        self.assertEqual(
            files["postgres_pgpass"],
            b'127.0.0.1:5432:postgres:fixture_admin:fixture-admin-\\:"\\\\password\n',
        )
        self.assertIn(b'requirepass "fixture-valkey-\\"\\\\password"\n', files["valkey.conf"])
        self.assertEqual(core.validate_bundle(plan, files), files)

    def test_wrong_hash_bytes_permissions_or_root_rejected(self):
        plan, _ = fixture()
        for key, value in (("sha256", "x" * 64), ("bytes", True), ("uid", 0), ("mode", "0644")):
            bad = copy.deepcopy(plan)
            bad["files"]["zou_db_password"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                core.validate_plan(bad)
        bad = copy.deepcopy(plan)
        bad["targets"]["config"] = "/opt/media-platform/other-project"
        with self.assertRaises(ValueError):
            core.validate_plan(bad)

    def test_missing_extra_or_changed_payload_rejected_before_io(self):
        plan, files = fixture()
        for change in ("missing", "extra", "changed"):
            bad = dict(files)
            if change == "missing":
                bad.pop("zou_admin_password")
            elif change == "extra":
                bad["../outside"] = b"fixture"
            else:
                bad["postgres_password"] = b"different-fixture-password\n"
            with self.subTest(change=change), self.assertRaises(ValueError):
                core.validate_bundle(plan, bad)

    def test_semantic_mismatch_is_rejected_even_with_matching_hashes(self):
        plan, files = fixture()
        for name, payload in (
            ("zou_kv_password", b"different-cache-password\n"),
            ("postgres_pgpass", b"*:5432:*:*:fixture\n"),
            ("valkey.conf", files["valkey.conf"] + b"protected-mode no\n"),
        ):
            bad = dict(files)
            bad[name] = payload
            modified = copy.deepcopy(plan)
            modified["files"][name].update(
                sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload)
            )
            with self.subTest(name=name), self.assertRaises(ValueError):
                core.validate_bundle(modified, bad)

    def test_secret_newlines_controls_and_pending_values_rejected(self):
        for raw in (
            b"CHANGE_ME\n",
            b"PENDIENTE_REEMPLAZAR",
            b"fixture\nsecond-line\n",
            b"fixture-password\r\n",
            b"fixture-\x00-password",
            b"short",
            b"\xff" * 20,
        ):
            with self.subTest(raw=raw[:4]), self.assertRaises(ValueError):
                core.secret_value(raw)

    def test_application_cannot_use_postgres_admin_role(self):
        plan, _ = fixture()
        plan["identifiers"]["zou_database_user"] = plan["identifiers"]["postgres_admin_user"]
        with self.assertRaises(ValueError):
            core.validate_plan(plan)

    def test_duplicate_json_keys_and_oversized_input_rejected(self):
        for raw in (b'{"x":1,"x":2}', b" " * (core.MAX_REQUEST + 1)):
            with self.assertRaises(ValueError):
                core.decode(raw)

    def test_base64_request_requires_exact_closed_payload_set(self):
        plan, files = fixture()
        encoded = {k: base64.b64encode(v).decode() for k, v in files.items()}
        self.assertEqual(core.decode_files(plan, encoded), files)
        encoded["zou_db_password"] = "!bad!"
        with self.assertRaises(ValueError):
            core.decode_files(plan, encoded)

    def test_default_remote_plan_never_loads_layout_or_writes(self):
        plan, _ = fixture()
        request = {"core_b64": "fixture", "plan": plan}
        output = io.StringIO()
        with (
            patch.object(remote.sys, "argv", ["private_files_remote.py"]),
            patch.object(
                remote.sys,
                "stdin",
                types.SimpleNamespace(buffer=io.BytesIO(core.canonical(request))),
            ),
            patch.object(remote.sys, "stdout", output),
            patch.object(remote, "load_raw", return_value=core) as loader,
            patch.object(remote, "prepare") as prepare,
        ):
            self.assertEqual(remote.main(), 0)
        self.assertEqual(loader.call_count, 1)
        prepare.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["mode"], "plan")
        self.assertFalse(result["runtime_verified"])
        self.assertFalse(result["production_approved"])

    def test_raw_loader_rejects_changed_source_before_execution(self):
        raw = b'raise RuntimeError("must not execute")'
        with self.assertRaisesRegex(ValueError, "SOURCE_SHA"):
            remote.load_raw("kitsu_rejected_fixture", base64.b64encode(raw).decode(), "0" * 64)

    def test_remote_pins_exact_core_bytes(self):
        raw = (ROOT / "docker/kitsu-zou/prepare_private_files.py").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), remote.CORE_SHA)

    def test_bad_payload_rejected_before_host_inspection_or_writes(self):
        plan, files = fixture()
        files["zou_db_password"] = b"fixture-tampered-password\n"
        layout = unittest.mock.Mock()
        with (
            patch.object(remote.sys, "platform", "linux"),
            patch.object(remote.os, "geteuid", return_value=0, create=True),
            patch.object(remote.os, "getegid", return_value=0, create=True),
        ):
            with self.assertRaisesRegex(ValueError, "PAYLOAD_HASH"):
                remote.prepare(core, layout, {}, plan, files, {})
        layout.LocalFilesystem.assert_not_called()

    def test_existing_target_rejected_before_any_directory_or_file_write(self):
        plan, files = fixture()
        fs = unittest.mock.MagicMock()
        fs.__enter__.return_value = fs
        fs.open_target.side_effect = [100, 101]
        layout = types.SimpleNamespace(LocalFilesystem=lambda: fs)
        info = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0)
        with (
            patch.object(remote.sys, "platform", "linux"),
            patch.object(remote.os, "geteuid", return_value=0, create=True),
            patch.object(remote.os, "getegid", return_value=0, create=True),
            patch.object(remote, "verify_layout"),
            patch.object(remote.os, "fstat", return_value=info),
            patch.object(remote.os, "stat", return_value=info),
            patch.object(remote.os, "close"),
            patch.object(remote, "create_directory") as mkdir,
            patch.object(remote, "write_file") as write,
        ):
            with self.assertRaisesRegex(FileExistsError, "TARGET_COLLISION"):
                remote.prepare(core, layout, {}, plan, files, {})
        mkdir.assert_not_called()
        write.assert_not_called()

    def test_first_parent_fd_closed_if_second_parent_cannot_open(self):
        plan, files = fixture()
        fs = unittest.mock.MagicMock()
        fs.__enter__.return_value = fs
        fs.open_target.side_effect = [100, PermissionError("fixture")]
        layout = types.SimpleNamespace(LocalFilesystem=lambda: fs)
        info = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0)
        with (
            patch.object(remote.sys, "platform", "linux"),
            patch.object(remote.os, "geteuid", return_value=0, create=True),
            patch.object(remote.os, "getegid", return_value=0, create=True),
            patch.object(remote, "verify_layout"),
            patch.object(remote.os, "fstat", return_value=info),
            patch.object(remote.os, "close") as closed,
        ):
            with self.assertRaises(PermissionError):
                remote.prepare(core, layout, {}, plan, files, {})
        closed.assert_called_once_with(100)

    def test_file_writer_uses_exclusion_no_follow_and_handles_short_writes(self):
        plan, files = fixture()
        raw = files["zou_db_password"]
        chunks = []

        def short_write(fd, view):
            chunk = bytes(view[:3])
            chunks.append(chunk)
            return len(chunk)

        with (
            patch.object(remote.os, "open", return_value=42) as opened,
            patch.object(remote.os, "fchown", create=True),
            patch.object(remote.os, "fchmod", create=True),
            patch.object(remote.os, "write", side_effect=short_write),
            patch.object(remote.os, "fsync"),
            patch.object(remote.os, "close"),
            patch.object(remote.os, "O_NOFOLLOW", 0x20000, create=True),
            patch.object(remote, "verify_file", return_value={"sha256": core.digest(raw)}),
        ):
            result = remote.write_file(9, "zou_db_password", raw, plan["files"]["zou_db_password"])
        self.assertEqual(b"".join(chunks), raw)
        flags = opened.call_args.args[1]
        self.assertTrue(flags & os.O_EXCL)
        self.assertTrue(flags & 0x20000)
        self.assertNotIn(raw.strip(), core.canonical(result))

    @unittest.skipUnless(
        sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0,
        "FD/UID reales requieren Linux Root; no se simulan como aprobación",
    )
    def test_native_tree_keeps_pg_and_valkey_empty_and_never_adopts(self):
        plan, files = fixture()
        with tempfile.TemporaryDirectory() as folder:
            parents = {
                "/opt/media-platform": Path(folder) / "config-parent",
                "/var/lib/media-platform": Path(folder) / "data-parent",
            }
            for path in parents.values():
                path.mkdir(mode=0o700)

            class Filesystem:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def open_target(self, path):
                    return os.open(parents[path], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

            layout = types.SimpleNamespace(LocalFilesystem=Filesystem)
            result = {"targets": {}, "files": {}, "data_directories": {}}
            with patch.object(remote, "verify_layout"):
                remote.prepare(core, layout, {}, plan, files, result)
            data = parents["/var/lib/media-platform"] / "kitsu-base-v1"
            self.assertEqual(list((data / "postgres-apps").iterdir()), [])
            self.assertEqual(list((data / "valkey").iterdir()), [])
            self.assertEqual({p.name for p in (data / "zou").iterdir()}, {"previews", "tmp"})
            self.assertEqual((data / "postgres-apps").stat().st_uid, 70)
            self.assertTrue(result["success"])
            with patch.object(remote, "verify_layout"), self.assertRaises(FileExistsError):
                remote.prepare(
                    core,
                    layout,
                    {},
                    plan,
                    files,
                    {"targets": {}, "files": {}, "data_directories": {}},
                )


if __name__ == "__main__":
    unittest.main()
