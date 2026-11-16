"""Contratos del SDK nuevo; sin SSH, Docker, compiladores ni bóveda real."""

import base64
import copy
import importlib.util
import io
import sys
import tarfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    path = ROOT / "docker/kitsu-zou" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class OpenExrSdkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remote = load("openexr3_sdk_test", "openexr3_sdk_remote.py")

    def profile(self):
        return self.remote.minimum_profile()

    def observed(self, plan):
        r = self.remote
        return {
            "Id": "a" * 64,
            "Image": r.BASE_MANIFEST,
            "Name": "/" + r.NAME,
            "Created": "2026-11-06T18:00:00Z",
            "Config": {
                "Image": r.BASE_REFERENCE,
                "User": "0:0",
                "Labels": r.labels(plan),
                "Env": r.container_environment(plan),
                "Entrypoint": ["/bin/sh"],
                "Cmd": r.hold_arguments(),
                "Volumes": None,
            },
            "HostConfig": {
                "NanoCpus": 2_000_000_000,
                "Memory": 6 * 1024**3,
                "MemorySwap": 6 * 1024**3,
                "PidsLimit": 128,
                "NetworkMode": "none",
                "Privileged": False,
                "ReadonlyRootfs": False,
                "CapDrop": ["CAP_ALL"],
                "SecurityOpt": ["no-new-privileges=true"],
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=67108864,mode=1777"},
                "Mounts": [{"Type": "bind", **row} for row in r.mount_contract(plan)],
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": row["Source"],
                    "Destination": row["Target"],
                    "RW": not row["ReadOnly"],
                    "Propagation": "rprivate",
                }
                for row in r.mount_contract(plan)
            ],
            "State": {
                "Status": "running",
                "Running": True,
                "Pid": 123,
                "StartedAt": "2026-11-06T18:01:00Z",
            },
        }

    def test_five_real_mounts_reject_old_prefix_writable_or_foreign(self):
        r, p = self.remote, self.profile()
        good = self.observed(p)
        self.assertEqual(r.check_container(good, "a" * 64, p)["pid"], 123)
        for where, key, value in [
            ("Mounts", "RW", True),
            ("HostConfig", "NetworkMode", "host"),
            ("HostConfig", "CapDrop", ["ALL", "CAP_ALL"]),
        ]:
            bad = copy.deepcopy(good)
            if where == "Mounts":
                bad[where][2][key] = value
            else:
                bad[where][key] = value
            with self.subTest(where=where, key=key), self.assertRaises(ValueError):
                r.check_container(bad, "a" * 64, p)
        bad = copy.deepcopy(good)
        bad["Mounts"].append({"Type": "volume", "Destination": "/foreign"})
        with self.assertRaises(ValueError):
            r.check_container(bad, "a" * 64, p)

    def test_receipts_require_canonical_positive_correct_stage_and_backup(self):
        r = self.remote
        value = r.envelope({"stage": "sdk", "success": True, "plan_sha256": "b" * 64})
        self.assertTrue(r.require_receipt("sdk", {"sdk": value}, {"sdk": value})["success"])
        for changed in (
            {**value["receipt"], "success": False},
            {**value["receipt"], "success": 1},
            {**value["receipt"], "stage": "start"},
        ):
            bad = r.envelope(changed)
            with self.assertRaises(ValueError):
                r.require_receipt("sdk", {"sdk": bad}, {"sdk": bad})
        with self.assertRaises(ValueError):
            r.require_receipt("sdk", {"sdk": value}, {})

    def test_stop_needs_only_control_prepare_create_even_without_inputs(self):
        r, p = self.remote, self.profile()
        actual = {
            name: r.envelope(
                {"stage": name, "success": True, "plan_sha256": "b" * 64, "cid": "a" * 64}
            )
            for name in ("prepare", "create")
        }
        self.assertEqual(r.cleanup_cid(actual, actual, p), "a" * 64)
        broken = copy.deepcopy(actual)
        broken["create"]["receipt"]["cid"] = "c" * 64
        with self.assertRaises(ValueError):
            r.cleanup_cid(broken, actual, p)

    def approval(self):
        r, p = self.remote, self.profile()
        observation = r.check_container(self.observed(p), "a" * 64, p)
        sdk = r.envelope(
            {
                "stage": "sdk",
                "success": True,
                "sdk_artifact_sha256": "d" * 64,
                "observation": observation,
            }
        )
        now = datetime.now(UTC)
        proof = {
            "kind": "openexr3-sdk-sca-approval-v1",
            "verified": True,
            "runtime_approved": False,
            "cid": observation["cid"],
            "created": observation["created"],
            "pid": observation["pid"],
            "started_at": observation["started_at"],
            "plan_sha256": "b" * 64,
            "prefix_archive_sha256": r.PREFIX_SHA,
            "sdk_receipt_sha256": sdk["receipt_sha256"],
            "sdk_artifact_sha256": "d" * 64,
            "database_sha256": r.DB_SHA,
            "tools": [{"path": "/tool", "sha256": "e" * 64}],
            "input_sca_receipt_sha256": r.INPUT_SCA_SHA,
            "input_sca_report_sha256": r.INPUT_REPORT_SHA,
            "sbom_sha256": r.SBOM_SHA,
            "coverage": {
                "installed_apk": 89,
                "partial_apk": 5,
                "numpy": 1,
                "total": 95,
                "high_critical": 0,
                "unknown_or_unscored": 0,
            },
            "db": {
                "sha256": r.SCA_DB_SHA,
                "updated_at": (now - timedelta(hours=1)).isoformat(),
                "next_update": (now + timedelta(hours=1)).isoformat(),
            },
        }
        p.update(plan_sha256="b" * 64, sdk_entrypoints=proof["tools"])
        return p, sdk, observation, proof

    def test_sca_approval_binds_new_epoch_and_95_not_historical_2ce(self):
        r = self.remote
        p, sdk, observation, proof = self.approval()
        r.validate_approval(proof, p, sdk, observation)
        changes = [
            ("pid", 124),
            ("created", "other"),
            ("verified", 1),
            ("runtime_approved", True),
            ("input_sca_receipt_sha256", "2ce"),
            ("database_sha256", "0" * 64),
        ]
        for key, value in changes:
            bad = {**proof, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                r.validate_approval(bad, p, sdk, observation)
        for key in ("total", "unknown_or_unscored", "high_critical"):
            bad = copy.deepcopy(proof)
            bad["coverage"][key] += 1
            with self.assertRaises(ValueError):
                r.validate_approval(bad, p, sdk, observation)
        bad = copy.deepcopy(proof)
        bad["db"]["next_update"] = "2000-01-01T00:00:00Z"
        with self.assertRaises(ValueError):
            r.validate_approval(bad, p, sdk, observation)

    def test_tar_whitelist_rejects_extra_duplicate_and_hash_mutation(self):
        r = self.remote

        def make(rows):
            out = io.BytesIO()
            with tarfile.open(fileobj=out, mode="w") as archive:
                for name, raw in rows:
                    item = tarfile.TarInfo(name)
                    item.mode = 0o444
                    item.size = len(raw)
                    archive.addfile(item, io.BytesIO(raw))
            return out.getvalue()

        wanted = {"agency/a": {"kind": "file", "sha256": r.digest(b"ok"), "size": 2}}
        self.assertEqual(
            r.archive_payloads(make([("agency/a", b"ok")]), wanted), {"agency/a": b"ok"}
        )
        for rows in [
            [("agency/a", b"no")],
            [("agency/a", b"ok"), ("extra", b"x")],
            [("agency/a", b"ok"), ("agency/a", b"ok")],
        ]:
            with self.assertRaises(ValueError):
                r.archive_payloads(make(rows), wanted)

    def test_active_foreign_sdk_is_rejected_without_stop(self):
        r = self.remote

        class Docker:
            def capture(self, args, timeout=2):
                return ("f" * 64 + "\n").encode()

            def inspect(self, cid):
                return {
                    "Id": cid,
                    "Config": {
                        "Labels": {
                            "cl.e-scale.agencia.owner": "agencia-digital-marketing-ai:postgres-immich-builder:v1"
                        }
                    },
                }

        with self.assertRaises(ValueError):
            r.check_window(Docker())

    def test_default_plan_never_reads_vault_or_connects(self):
        local = load("openexr3_local_test", "run_openexr3_sdk.py")
        from unittest.mock import patch

        with (
            patch.object(local, "proposal", return_value={"runtime_applied": False}),
            patch.object(local, "read_values", side_effect=AssertionError("bóveda")),
            patch("builtins.print"),
            patch.object(local, "connect", side_effect=AssertionError("red")),
        ):
            self.assertEqual(local.main([]), 0)

    def test_timed_exec_error_stops_own_cid_before_leaving(self):
        r = self.remote
        actions = []

        class Docker:
            def command(self, args, timeout, sink=None):
                actions.append((args, timeout))
                raise TimeoutError("fixture")

        def cleanup():
            actions.append(("stop-own", "a" * 64))

        with self.assertRaises(TimeoutError):
            r.bounded_stage(Docker(), ["exec", "a" * 64], 1800, None, cleanup)
        self.assertEqual(actions[0][1], 1780)
        self.assertEqual(actions[1], ("stop-own", "a" * 64))

    def test_sdk_attestation_rejects_changed_db_tool_or_external_interface(self):
        r, p = self.remote, self.profile()
        p["sdk_entrypoints"] = [{"path": "/usr/bin/cmake", "sha256": "e" * 64}]
        good = {"database_sha256": r.DB_SHA, "tools": p["sdk_entrypoints"], "interfaces": ["lo"]}
        r.validate_attestation(good, p)
        for key, value in [
            ("database_sha256", "0" * 64),
            ("tools", []),
            ("interfaces", ["lo", "eth0"]),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                r.validate_attestation({**good, key: value}, p)

    def test_sdk_artifact_bytes_must_still_match_the_backed_receipt(self):
        r = self.remote
        raw = r.canonical({"verified": True, "tools": []})
        expected = r.digest(raw)
        r.validate_sdk_bytes(raw, expected)
        with self.assertRaises(ValueError):
            r.validate_sdk_bytes(raw + b" ", expected)

    def test_layout_request_works_with_real_frozen_loader_without_host_inspection(self):
        r = self.remote
        normal_raw = (ROOT / "docker/kitsu-zou/opencv_minimal_sdk_remote.py").read_bytes()
        normal = r.module_bytes("openexr3_normal_contract_test", normal_raw, r.NORMAL_SHA)
        layout = (ROOT / "docker/host_layout.py").read_bytes()
        request = {
            "plan": {"layout": {"success": True}, "sources": {"layout": r.digest(layout)}},
            "layout_b64": base64.b64encode(layout).decode(),
        }
        adapted = r.host_request(request)
        loaded = normal.load_layout(adapted)
        self.assertTrue(callable(loaded.inspect_host))

    def test_unverified_cid_cleanup_requires_matching_physical_intent(self):
        r, p = self.remote, self.profile()
        p["plan_sha256"] = "b" * 64
        actual = {"prepare": r.envelope({"stage": "prepare", "success": True})}
        intent = {"cid": "a" * 64, "verified": False, "plan_sha256": "b" * 64}
        backed = {**actual, "create_unverified": intent}
        self.assertEqual(r.cleanup_cid(actual, backed, p, intent), "a" * 64)
        with self.assertRaises(ValueError):
            r.cleanup_cid(actual, backed, p, {**intent, "cid": "c" * 64})

    def test_build_argv_keeps_umask_and_new_work_without_old_outputs(self):
        r = self.remote
        args = r.build_arguments("a" * 64)
        self.assertIn('umask 077; exec /usr/bin/bash "$@"', args)
        self.assertIn("/overlay/agency/recipe.sh", args)
        self.assertIn("/work/openexr3-attempt-v2", args)
        self.assertNotIn("/work/build", args)

    def test_stop_timeout_reinspects_before_killing_same_owned_cid(self):
        r, p = self.remote, self.profile()
        running = self.observed(p)
        stopped = copy.deepcopy(running)
        stopped["State"].update(Running=False, Pid=0, Status="exited")
        actions = []

        class Docker:
            def __init__(self):
                self.rows = iter([running, running, stopped])

            def inspect(self, cid):
                actions.append(("inspect", cid))
                return next(self.rows)

            def command(self, args, timeout):
                actions.append(args)
                if args[0] == "stop":
                    raise TimeoutError("fixture")
                return {"returncode": 0}

        result = r.stop_owned(Docker(), "a" * 64, p)
        self.assertTrue(result["stopped"])
        self.assertEqual(actions[2], ("inspect", "a" * 64))
        self.assertEqual(actions[3], ["kill", "--signal", "KILL", "a" * 64])


if __name__ == "__main__":
    unittest.main()
