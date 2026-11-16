"""Fixtures OCI/ELF de datos; ningún binario, scanner, red o SDK."""

import copy
import gzip
import hashlib
import importlib.util
import io
import json
import struct
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = load("valkey_inventory_core_test", "docker/valkey/inventory_core.py")
L = load("valkey_inventory_local_test", "docker/valkey/inventory_image.py")
PARSER_PATH = ROOT / "docker/postgres-immich/final_image_remote.py"
PARSER_RAW = PARSER_PATH.read_bytes()
PARSER_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
assert hashlib.sha256(PARSER_RAW).hexdigest() == PARSER_SHA
P = types.ModuleType("valkey_frozen_oci_parser_test")
P.__file__ = str(PARSER_PATH)
exec(compile(PARSER_RAW, str(PARSER_PATH), "exec"), P.__dict__)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def tar_bytes(rows):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, raw, kind, target in rows:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, target
            member.size = len(raw) if kind == tarfile.REGTYPE else 0
            member.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            archive.addfile(member, io.BytesIO(raw) if kind == tarfile.REGTYPE else None)
    return buffer.getvalue()


def file(name, raw=b"data"):
    return name, raw, tarfile.REGTYPE, ""


def link(name, target):
    return name, b"", tarfile.SYMTYPE, target


def elf():
    raw = bytearray(128)
    raw[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", raw, 16, 3, 62)
    struct.pack_into("<Q", raw, 40, 64)
    struct.pack_into("<HH", raw, 58, 64, 1)
    return bytes(raw) + b"Lua 5.1.5\0jemalloc-5.3.0\0valkey9.1.3\0"


def image(layer_rows, config_override=None):
    blobs, layers, diffids = {}, [], []
    for rows in layer_rows:
        plain = tar_bytes(rows)
        compressed = gzip.compress(plain, mtime=0)
        name = digest(compressed)
        blobs["blobs/sha256/" + name] = compressed
        layers.append(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + name,
                "size": len(compressed),
            }
        )
        diffids.append("sha256:" + digest(plain))
    config = {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": diffids},
        "config": {
            "Env": ["VALKEY_VERSION=9.1.3"],
            "Entrypoint": ["tini", "--", "docker-entrypoint.sh"],
            "Cmd": ["valkey-server"],
        },
        "history": [{"created_by": "COPY fixture", "empty_layer": False}],
    }
    if config_override:
        config.update(config_override)
    raw = canonical(config)
    config_digest = "sha256:" + digest(raw)
    blobs["blobs/sha256/" + config_digest[7:]] = raw
    descriptor = {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": config_digest,
        "size": len(raw),
    }
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": descriptor,
        "layers": layers,
    }
    raw = canonical(manifest)
    manifest_digest = "sha256:" + digest(raw)
    blobs["blobs/sha256/" + manifest_digest[7:]] = raw
    index = {
        "schemaVersion": 2,
        "manifests": [
            {"mediaType": manifest["mediaType"], "digest": manifest_digest, "size": len(raw)}
        ],
    }
    members = [file(name, value) for name, value in blobs.items()]
    members += [
        file("oci-layout", canonical({"imageLayoutVersion": "1.0.0"})),
        file("index.json", canonical(index)),
    ]
    data = tar_bytes(members)
    expected = {
        "platform_digest": manifest_digest,
        "manifest": manifest,
        "version": "9.1.3",
        "index_digest": "sha256:" + "a" * 64,
    }
    return data, expected, members


def roles():
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "packages": [
            {
                "name": "valkey-server",
                "versionInfo": "9.1.3",
                "filesAnalyzed": False,
                "licenseDeclared": "BSD-3-Clause",
                "downloadLocation": "https://github.com/valkey-io/valkey/archive/refs/tags/9.1.3.tar.gz",
                "externalRefs": [
                    {"referenceType": "purl", "referenceLocator": "pkg:generic/valkey-server@9.1.3"}
                ],
            }
        ],
    }
    rows = [
        file(
            "lib/apk/db/installed",
            b"P:musl\nV:1.2.6-r3\nA:x86_64\no:musl\nL:MIT\nc:"
            + b"1" * 40
            + b"\nF:lib\nR:libc.musl-x86_64.so.1\n",
        ),
        link("etc/os-release", "../usr/lib/os-release"),
        file("usr/lib/os-release", b"ID=alpine\nVERSION_ID=3.24.3\n"),
        file(
            "etc/passwd",
            b"root:x:0:0:root:/root:/bin/sh\nvalkey:x:999:1000::/home/valkey:/sbin/nologin\n",
        ),
        file("etc/group", b"root:x:0:\nvalkey:x:1000:valkey\n"),
        file("usr/local/valkey.spdx.json", canonical(spdx)),
        file(
            "usr/local/bin/docker-entrypoint.sh", b"#!/bin/sh\nexec setpriv --reuid=valkey -- $@\n"
        ),
    ]
    for name in ("server", "cli", "benchmark"):
        rows.append(file("usr/local/bin/valkey-" + name, elf()))
    rows += [
        link("usr/local/bin/valkey-sentinel", "valkey-server"),
        link("usr/local/bin/redis-server", "valkey-server"),
    ]
    return rows


class CoreTests(unittest.TestCase):
    def test_valid_oci_keeps_digests_origins_history_without_extract(self):
        raw, expected, _ = image([roles()])
        with (
            patch.object(tarfile.TarFile, "extract", side_effect=AssertionError("NO_FS")),
            patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("NO_FS")),
        ):
            parsed = C.parse_oci(raw, P, expected)
        self.assertEqual(parsed["manifest_digest"], expected["platform_digest"])
        self.assertEqual(parsed["origins"]["usr/local/bin/valkey-server"]["layer"], 0)
        self.assertEqual(parsed["config"]["history"][0]["created_by"], "COPY fixture")
        self.assertEqual(
            parsed["payloads"]["etc/passwd"], dict((r[0], r[1]) for r in roles())["etc/passwd"]
        )

    def test_whiteout_and_opaque_remove_old_payloads_preserve_new_origin(self):
        raw, expected, _ = image(
            [
                [file("data/a"), file("data/b")],
                [file("data/.wh.a", b""), file("data/.wh..wh..opq", b""), file("data/c", b"new")],
            ]
        )
        parsed = C.parse_oci(raw, P, expected)
        self.assertNotIn("data/a", parsed["files"])
        self.assertNotIn("data/b", parsed["payloads"])
        self.assertEqual(parsed["payloads"]["data/c"], b"new")
        self.assertEqual(parsed["origins"]["data/c"]["layer"], 1)

    def test_nonopaque_whiteout_removes_only_the_named_node(self):
        raw, expected, _ = image([[file("data/a"), file("data/b")], [file("data/.wh.a", b"")]])
        parsed = C.parse_oci(raw, P, expected)
        self.assertNotIn("data/a", parsed["payloads"])
        self.assertEqual(parsed["payloads"]["data/b"], b"data")
        self.assertEqual(parsed["origins"]["data/b"]["layer"], 0)

    def test_duplicate_traversal_links_or_nonzero_trailer_are_rejected(self):
        raw, expected, members = image([[file("data/a")]])
        for wrong in [
            tar_bytes(members + [file("./index.json", b"{}")]),
            tar_bytes(members + [file("../outside")]),
            tar_bytes(members + [link("outer-link", "/host")]),
            raw + b"foreign" + b"\0" * (512 - len(b"foreign")),
        ]:
            with self.assertRaises(ValueError):
                C.parse_oci(wrong, P, expected)

    def test_blob_diffid_platform_and_manifest_changes_reject(self):
        raw, expected, members = image([[file("data/a")]])
        wrong_members = [
            (name, value + b"x", kind, target)
            if name.startswith("blobs/")
            else (name, value, kind, target)
            for name, value, kind, target in members
        ]
        with self.assertRaises(ValueError):
            C.parse_oci(tar_bytes(wrong_members), P, expected)
        wrong = copy.deepcopy(expected)
        wrong["platform_digest"] = "sha256:" + "f" * 64
        with self.assertRaises(ValueError):
            C.parse_oci(raw, P, wrong)
        raw, expected, _ = image([[file("data/a")]], {"architecture": "arm64"})
        with self.assertRaises(ValueError):
            C.parse_oci(raw, P, expected)
        raw, expected, _ = image(
            [[file("data/a")]], {"rootfs": {"type": "layers", "diff_ids": ["sha256:" + "0" * 64]}}
        )
        with self.assertRaises(ValueError):
            C.parse_oci(raw, P, expected)

    def test_expansion_and_node_bounds_are_enforced(self):
        raw, expected, _ = image([[file("data/a", b"a" * 20000)]])
        with self.assertRaises(ValueError):
            C.parse_oci(raw, P, expected, max_layer=1024)
        with self.assertRaises(ValueError):
            C.parse_oci(raw, P, expected, max_nodes=1)

    def test_accounts_aliases_and_declared_binary_are_distinct_from_acceptance(self):
        raw, expected, _ = image([roles()])
        result = C.inventory(C.parse_oci(raw, P, expected), P)
        self.assertEqual(result["accounts"]["valkey"]["uid"], 999)
        self.assertEqual(result["accounts"]["valkey"]["gid"], 1000)
        self.assertFalse(result["configuration"]["user_field_present"])
        self.assertFalse(result["configuration"]["effective_uid_verified"])
        self.assertEqual(len(result["binaries"]), 3)
        self.assertEqual(
            result["aliases"]["usr/local/bin/redis-server"]["resolved"],
            "usr/local/bin/valkey-server",
        )
        self.assertFalse(result["binary_sca_verified"])
        self.assertFalse(result["binary_accepted"])
        self.assertFalse(result["embedded_libraries_complete"])

    def test_embedded_signatures_are_candidates_not_stock_security_claims(self):
        raw, expected, _ = image([roles()])
        result = C.inventory(C.parse_oci(raw, P, expected), P)
        self.assertEqual({x["name"] for x in result["embedded_candidates"]}, {"lua", "jemalloc"})
        self.assertTrue(
            all(
                x["modified_upstream_candidate"] and not x["exact_component_identity_verified"]
                for x in result["embedded_candidates"]
            )
        )
        self.assertEqual(result["licenses"]["text_files"], [])
        self.assertFalse(result["licenses"]["complete"])
        self.assertFalse(result["shipped_spdx"]["packages"][0]["filesAnalyzed"])

    def test_alias_cycles_and_conflicting_version_claims_fail(self):
        cycle = [
            link(row[0], "redis-server") if row[0] == "usr/local/bin/redis-server" else row
            for row in roles()
        ]
        mismatch = [
            file(
                row[0], canonical({"packages": [{"name": "valkey-server", "versionInfo": "8.0.0"}]})
            )
            if row[0] == "usr/local/valkey.spdx.json"
            else row
            for row in roles()
        ]
        for rows in [cycle, mismatch]:
            raw, expected, _ = image([rows])
            parsed = C.parse_oci(raw, P, expected)
            with self.assertRaises(ValueError):
                C.inventory(parsed, P)

    def test_apk_duplicate_source_fields_and_unsafe_file_paths_fail(self):
        raw = b"P:x\nV:1-r0\nA:x86_64\no:x\nL:MIT\n"
        self.assertEqual(C.apk_database(raw)[0]["source_name"], "x")
        for wrong in [
            raw + b"o:other\n",
            raw + b"F:../host\nR:file\n",
            raw.replace(b"A:x86_64", b"A:aarch64"),
            raw + b"\n" + raw,
        ]:
            with self.assertRaises(ValueError):
                C.apk_database(wrong)

    def test_unresolved_dynamic_provider_cannot_disappear_from_inventory(self):
        raw, expected, _ = image([roles()])
        parsed = C.parse_oci(raw, P, expected)
        parsed["files"]["usr/local/bin/valkey-server"]["elf"]["needed"] = ["unavailable.so"]
        with self.assertRaises(ValueError):
            C.inventory(parsed, P)


class LocalTests(unittest.TestCase):
    def test_wrong_helper_bytes_are_rejected_before_compile(self):
        reader = types.SimpleNamespace(read_exact=lambda *args: b"raise AssertionError('executed')")
        with (
            patch.object(L, "module_raw", side_effect=AssertionError("NO_EXECUTION")),
            self.assertRaises(ValueError),
        ):
            L.pinned_module(reader, "closed", Path("helper.py"), "0" * 64)

    def test_default_plan_does_not_publish_or_load_a_scanner(self):
        with (
            patch.object(L, "prepare", return_value=({"kind": "valkey-inventory-plan"}, {})),
            patch.object(L, "publish", side_effect=AssertionError("NO_WRITE")),
            patch.object(sys, "stdout", new_callable=io.StringIO) as out,
        ):
            self.assertEqual(L.main([]), 0)
            self.assertFalse(json.loads(out.getvalue())["artifacts_published"])

    def test_publication_is_excl_and_preserves_foreign_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "own"
            target.mkdir()
            (target / "foreign").write_bytes(b"sentinel")
            with self.assertRaises(FileExistsError):
                L.publish(target, {}, {"one.json": b"{}"})
            self.assertEqual((target / "foreign").read_bytes(), b"sentinel")

    def test_publication_verifies_written_bytes_with_frozen_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "own"
            outputs = {"one.json": b'{"data":true}'}
            plan = {
                "outputs": {
                    name: {"sha256": digest(raw), "bytes": len(raw)}
                    for name, raw in outputs.items()
                }
            }
            receipt = L.publish(target, plan, outputs)
            self.assertTrue(receipt["artifacts_published"])
            self.assertFalse(receipt["binary_accepted"])
            self.assertEqual((target / "one.json").read_bytes(), outputs["one.json"])
            with self.assertRaises(FileExistsError):
                L.publish(target, plan, outputs)

    def test_envelope_requires_positive_stage_hash_and_capture_fetch_chain(self):
        value = {"stage": "capture", "success": True, "binary_accepted": False}
        envelope = {"value": value, "sha256": digest(canonical(value))}
        self.assertEqual(L.envelope(envelope, "capture"), value)
        for wrong in [
            {**envelope, "sha256": "0" * 64},
            {
                "value": {**value, "success": False},
                "sha256": digest(canonical({**value, "success": False})),
            },
        ]:
            with self.assertRaises(ValueError):
                L.envelope(wrong, "capture")


if __name__ == "__main__":
    unittest.main()
