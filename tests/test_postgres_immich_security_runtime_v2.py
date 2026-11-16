"""Gates y formatos como datos; sin Docker, SDK, SQL, SSH ni .env real."""

import contextlib
import importlib.util
import io
import os
import stat
import struct
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

BASE = Path(__file__).resolve().parents[1]
PATH = BASE / "docker/postgres-immich/security_runtime_remote_v2.py"
SPEC = importlib.util.spec_from_file_location("security_runtime_remote_test", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
LOCAL_SPEC = importlib.util.spec_from_file_location(
    "security_runtime_local_test", PATH.with_name("security_runtime_v2.py")
)
L = importlib.util.module_from_spec(LOCAL_SPEC)
LOCAL_SPEC.loader.exec_module(L)


class ContractTests(unittest.TestCase):
    def test_loaded_pure_parser_processes_regular_tar_with_its_own_max_file(self):
        pure = M.load_pure((BASE / "docker/postgres-immich/final_image_remote.py").read_bytes())
        stream = io.BytesIO()
        body = b"Prueba de datos de la capa"
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            row = tarfile.TarInfo("usr/local/share/parser-probe")
            row.mode, row.uid, row.gid, row.size = 0o444, 0, 0, len(body)
            archive.addfile(row, io.BytesIO(body))
        files = {}
        pure.apply_layer(files, stream.getvalue())
        self.assertEqual(pure.MAX_FILE, 512 * 1024**2)
        self.assertEqual(
            files["usr/local/share/parser-probe"],
            {
                "kind": "file",
                "sha256": pure.digest(body),
                "size": len(body),
                "elf": None,
                "mode": 0o444,
                "uid": 0,
                "gid": 0,
            },
        )

    def test_loaded_pure_elf_parser_accepts_valid_sections_and_rejects_malformed_bytes(self):
        pure = M.load_pure((BASE / "docker/postgres-immich/final_image_remote.py").read_bytes())
        strings = b"\0libprobe.so\0"
        dynamic_offset = 256 + len(strings)
        raw = bytearray(dynamic_offset + 32)
        raw[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<HHI", raw, 16, 3, 62, 1)
        struct.pack_into("<Q", raw, 40, 64)
        struct.pack_into("<HHHHH", raw, 52, 64, 56, 0, 64, 3)
        struct.pack_into("<IIQQQQIIQQ", raw, 128, 0, 3, 0, 0, 256, len(strings), 0, 0, 1, 0)
        struct.pack_into("<IIQQQQIIQQ", raw, 192, 0, 6, 0, 0, dynamic_offset, 32, 1, 0, 8, 16)
        raw[256:dynamic_offset] = strings
        struct.pack_into("<qQqQ", raw, dynamic_offset, 1, 1, 0, 0)
        self.assertEqual(pure.elf_info(bytes(raw))["needed"], ["libprobe.so"])
        self.assertEqual(pure.file_entry(bytes(raw))["elf"]["defined"], [])
        for changed in [bytes(raw[:63]), bytes(raw[:100]), b"not an ELF" + bytes(120)]:
            with self.subTest(length=len(changed)), self.assertRaises(ValueError):
                pure.elf_info(changed)

    def gate_fixture(self):
        data = {
            name: M.json_object((BASE / path).read_bytes())
            for name, (path, _pin) in M.FROZEN_REFS.items()
        }
        profile = M.json_object(
            (
                BASE / ".tools/postgres-immich-security-inputs-v1/security-rebuild-v7.profile.json"
            ).read_bytes()
        )
        epoch = {
            **profile["original_container"],
            "started_at": "2026-11-06T00:00:00Z",
            "limits": {"fixture": True},
            "no_foreign": True,
            "pid": 17,
            "running": True,
        }
        files = {
            name: {key: row[key] for key in ("sha256", "size")}
            for name, row in data["baseline_recovered"]["receipt"]["artifact"][
                "runtime_files"
            ].items()
        }
        files[M.CHANGED] = {"sha256": "a" * 64, "size": files[M.CHANGED]["size"] + 1}
        artifact = {
            "success": True,
            "stage": "catalog",
            "profile_sha256": M.SECURITY_PROFILE,
            "files": files,
            "sdk_executed": False,
            "sql_executed": False,
            "security_clean": False,
            "reused_sqlite_sha256": "78711a51904d2ce47d4c16cb53365e85d73bc15cd4aa6d10c4d2ef3754d81c53",
            "reused_pgvector_sha256": "050bd2625e0efcf8f1b5bebd32135b1958a6f9b98e2c73d13137152488451d3b",
        }
        data["v7_profile"] = profile
        for mode in ("prepare", "restart", "catalog", "stop"):
            container = {
                **epoch,
                **({"running": False, "pid": 0} if mode in {"prepare", "stop"} else {}),
            }
            receipt = {
                "success": True,
                "stage": mode,
                "owner": "agencia-digital-marketing-ai:postgres-immich-security-rebuild:v7",
                "profile_sha256": M.SECURITY_PROFILE,
                "container": container,
            }
            if mode == "prepare":
                receipt["daemon_id"] = "fixture-daemon-v7"
            if mode == "catalog":
                receipt["artifact"] = artifact
            if mode == "stop":
                receipt["cleanup"] = {"stopped": True}
            data["v7_" + mode] = M.envelope(receipt)
        return data

    @pytest.mark.evidencia_operativa
    def test_v7_catalog_stop_birth_epoch_and_daemon_are_required_without_v6_fallback(self):
        data = self.gate_fixture()
        pure = M.load_pure((BASE / "docker/postgres-immich/final_image_remote.py").read_bytes())
        result = M.validate_gates(data, pure)
        self.assertEqual(len(result["projected"]), 66)
        self.assertEqual(result["daemon_id"], "fixture-daemon-v7")
        for key, value in [
            ("owner", "agencia-digital-marketing-ai:postgres-immich-security-rebuild:v6"),
            ("success", False),
        ]:
            changed = self.gate_fixture()
            changed["v7_catalog"] = M.envelope({**changed["v7_catalog"]["receipt"], key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                M.validate_gates(changed, pure)
        for field, value in [("created", "other-birth"), ("started_at", "other-epoch")]:
            changed = self.gate_fixture()
            row = changed["v7_stop"]["receipt"]
            changed["v7_stop"] = M.envelope(
                {**row, "container": {**row["container"], field: value}}
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                M.validate_gates(changed, pure)
        changed = self.gate_fixture()
        changed["v7_prepare"] = M.envelope({**changed["v7_prepare"]["receipt"], "daemon_id": ""})
        with self.assertRaises(ValueError):
            M.validate_gates(changed, pure)
        changed = self.gate_fixture()
        del changed["v7_stop"]
        with self.assertRaises((ValueError, KeyError)):
            M.validate_gates(changed, pure)

    def test_runtime_rejects_another_daemon_before_opening_control(self):
        data = {"sdk_profile": {"backed": {"prepare": {"fixture": True}}}, "v7_profile": {}}
        gates = {"daemon_id": "expected-daemon"}
        store = types.SimpleNamespace(verify=lambda: data["sdk_profile"]["backed"]["prepare"])
        security = types.SimpleNamespace(
            validate_plan=lambda _profile: M.SECURITY_PROFILE,
            daemon_identity=lambda _docker: "other-daemon",
            control_open=lambda _normal: self.fail("No abrir control con daemon distinto"),
        )
        with self.assertRaisesRegex(ValueError, "SDK_CURRENT_DAEMON"):
            M.verify_sdk_gates(object(), security, store, object(), data, gates)

    def test_pure_loader_excludes_historical_actions_and_runtime_globals(self):
        raw = (BASE / "docker/postgres-immich/final_image_remote.py").read_bytes()
        p = M.load_pure(raw)
        for forbidden in ["apply", "main", "OWNER", "LABELS", "runtime_directory", "modules"]:
            self.assertFalse(hasattr(p, forbidden))
        self.assertEqual(p.BASE_MANIFEST, M.BASE_MANIFEST)
        with self.assertRaisesRegex(ValueError, "PURE_SOURCE_PIN"):
            M.load_pure(raw + b"\n")

    def test_only_vchord_may_change_and_other_file_or_name_is_rejected(self):
        class Pure:
            @staticmethod
            def project(rows):
                return rows, []

        before = {"file" + str(i): {"sha256": "a" * 64, "size": 10} for i in range(65)}
        before[M.CHANGED] = {"sha256": "b" * 64, "size": 20}
        after = {**before, M.CHANGED: {"sha256": "c" * 64, "size": 30}}
        self.assertEqual(M.projection_delta(before, after, Pure()), (after, []))
        for bad in [
            {**after, "file0": {"sha256": "d" * 64, "size": 10}},
            {**after, "extra": {"sha256": "a" * 64, "size": 1}},
            before,
        ]:
            with self.subTest(bad=bad.get(M.CHANGED)), self.assertRaises(ValueError):
                M.projection_delta(before, bad, Pure())

    def test_docker29_real_manifest_iid_fallback_uses_config_from_oci_and_log(self):
        image = "sha256:96c6cce5d1efe176fe5b22e17c3c4c7bca926c94c403096bce9aa663df04a953"
        config = "sha256:760d54774499a5871dd92c62de5a64cb0cb31efc0acefeeb20e48cfa4d628033"
        observed = {
            "id": image,
            "descriptor": {"digest": image, "annotations": {"config.digest": config}},
            "user": "70:70",
            "entrypoint": ["docker-entrypoint.sh"],
            "cmd": ["postgres"],
            "labels": {"cl.e-scale.agencia.owner": M.OWNER},
            "os": "linux",
            "architecture": "amd64",
        }
        log = f"exporting manifest {image} done\nexporting config {config} done\n"
        final = {"manifest_digest": image, "config_digest": config}
        result = M.build_identity({"containerimage.digest": image}, image, observed, log, final)
        self.assertEqual(result["iid_interpretation"], "manifest-fallback")
        self.assertEqual(result["config_digest"], config)
        for descriptor in [None, {}, {"digest": image, "annotations": None}]:
            with self.subTest(descriptor=descriptor):
                self.assertEqual(
                    M.build_identity(
                        {"containerimage.digest": image},
                        image,
                        {**observed, "descriptor": descriptor},
                        log,
                        final,
                    )["config_digest"],
                    config,
                )
        explicit = M.build_identity(
            {"containerimage.digest": image, "containerimage.config.digest": config},
            config,
            observed,
            log,
            final,
        )
        self.assertEqual(explicit["iid_interpretation"], "config")
        for metadata, iid, output in [
            ({"containerimage.digest": image}, config, log),
            ({"containerimage.digest": image, "containerimage.config.digest": image}, image, log),
            ({"containerimage.digest": image}, image, log + log),
        ]:
            with self.subTest(iid=iid), self.assertRaises(ValueError):
                M.build_identity(metadata, iid, observed, output, final)

    def test_pending_plan_cannot_claim_runtime_or_sca(self):
        plan = M.proposal()
        self.assertIs(plan["ready"], False)
        self.assertIn("catalog-v7-positive", plan["pending"])
        self.assertEqual(
            M.SECURITY_SHA, "7206217b7933b65038a361c1c0dbda4df16d442a28d25daca21fff931e6b1d12"
        )
        self.assertEqual(
            M.SECURITY_PROFILE, "af3aee69455287b71f6d7387c1d95456f6d046455bdb39d199d41677f05c324c"
        )

    def test_positive_envelope_requires_correct_stage_owner_profile_and_boolean(self):
        row = {"success": True, "stage": "prepare", "owner": M.OWNER, "profile_sha256": "a" * 64}
        self.assertEqual(M.checked_envelope(M.envelope(row), "prepare", profile_sha="a" * 64), row)
        for key, value in [
            ("success", False),
            ("success", 1),
            ("stage", "catalog"),
            ("owner", "legacy"),
            ("profile_sha256", "b" * 64),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                M.checked_envelope(M.envelope({**row, key: value}), "prepare", profile_sha="a" * 64)

    def test_reference_bytes_must_match_external_roles_hash_and_size(self):
        pure = M.load_pure((BASE / "docker/postgres-immich/final_image_remote.py").read_bytes())
        raw = b'{"public":true}'
        profile = {"references": {"fixed": {"sha256": M.digest(raw), "size": len(raw)}}}
        encoded = {"fixed": M.base64.b64encode(raw).decode()}
        self.assertEqual(M.reference_data(profile, encoded, pure), {"fixed": {"public": True}})
        for changed in [
            {},
            {**encoded, "extra": encoded["fixed"]},
            {"fixed": M.base64.b64encode(raw + b" ").decode()},
        ]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                M.reference_data(profile, changed, pure)

    def test_dockerfile_and_args_use_only_local_oci_copy_nonroot_and_no_network(self):
        self.assertEqual(
            (PATH.parent / "Dockerfile.security-runtime-v2").read_bytes(), M.dockerfile_bytes()
        )
        commands = {
            line.split()[0]
            for line in M.dockerfile_bytes().decode().splitlines()
            if line and not line.startswith(("#", " "))
        }
        self.assertEqual(commands, {"FROM", "COPY", "LABEL", "USER"})
        args = M.build_arguments()
        self.assertIn(
            "pgbase=oci-layout://" + M.RECORD + "/base-oci@sha256:" + M.BASE_MANIFEST, args
        )
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--pull=false", args)
        self.assertIn("--load", args)

    def test_json_rejects_ambiguous_duplicate_and_nonfinite_values(self):
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                M.json_object(raw)

    def test_observed_ids_survive_before_later_oci_verification(self):
        image, config = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        partial = M.observed_ids(
            {"containerimage.digest": image}, image, f"exporting config {config} done\n"
        )
        self.assertEqual(partial["image_id"], image)
        self.assertEqual(partial["config_digest"], config)
        self.assertIs(partial["approval"], False)


class LocalTests(unittest.TestCase):
    def test_default_plan_does_not_read_vault_or_connect(self):
        with (
            patch.object(L, "read_values", side_effect=AssertionError("NO_VAULT")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(L.main([]), 0)
        result = M.json_object(output.getvalue())
        self.assertIs(result["proposal"]["ready"], False)
        self.assertFalse(result["infrastructure_executed"])
        self.assertEqual(result["prefix"], "MEDIA_POSTGRES_IMMICH_SECURITY_RUNTIME_V2_")

    def test_missing_root_flags_reject_before_reading_any_vault(self):
        with (
            patch.object(L, "read_values", side_effect=AssertionError("NO_VAULT")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(L.main(["prepare"]), 1)

    def test_reader_supports_eight_mib_and_rejects_larger_or_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture"
            path.write_bytes(b"x" * (2 * 1024**2 + 1))
            self.assertEqual(len(L.read_source(path, 8 * 1024**2)), 2 * 1024**2 + 1)
            path.write_bytes(b"x" * (8 * 1024**2 + 1))
            with self.assertRaises(ValueError):
                L.read_source(path, 8 * 1024**2)
            with self.assertRaises(ValueError):
                L.read_source(Path(directory), 8 * 1024**2)


@unittest.skipUnless(
    os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
    "FD/UID0 requiere Linux real; no se simula en Windows.",
)
class NativeFilesTests(unittest.TestCase):
    def test_only_named_metadata_accepts_0644_iid_and_links_are_rejected(self):
        normal = types.SimpleNamespace(
            identity=lambda s: {
                "device": s.st_dev,
                "inode": s.st_ino,
                "mode": stat.S_IMODE(s.st_mode),
                "uid": s.st_uid,
                "gid": s.st_gid,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for name in ["build-metadata.json", "image-id.txt"]:
                    (root / name).write_bytes(b"data")
                    (root / name).chmod(0o644)
                self.assertEqual(
                    M.read_owned(normal, fd, "build-metadata.json", 32, metadata=True), b"data"
                )
                with self.assertRaises(ValueError):
                    M.read_owned(normal, fd, "image-id.txt", 32, metadata=True)
                os.link(root / "build-metadata.json", root / "alias")
                with self.assertRaises(ValueError):
                    M.read_owned(normal, fd, "build-metadata.json", 32, metadata=True)
            finally:
                os.close(fd)

    def test_overlay_snapshot_rejects_extra_empty_directories(self):
        normal = types.SimpleNamespace(identity=lambda s: {"device": s.st_dev, "inode": s.st_ino})
        pure = types.SimpleNamespace(
            parents=lambda p: [], file_entry=lambda raw: {"sha256": M.digest(raw)}
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime-root").mkdir(mode=0o755)
            (root / "runtime-root").chmod(0o755)
            (root / "runtime-root/extra").mkdir(mode=0o755)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaisesRegex(ValueError, "OVERLAY_EXTRA_DIRECTORY"):
                    M.snapshot_overlay(normal, fd, {}, pure)
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
