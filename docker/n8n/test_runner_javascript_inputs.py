"""Pruebas de la proyección cerrada del lock del runner JavaScript."""

import unittest

from stage_runner_javascript import (
    package_resolution,
    project_lock,
    render_lock,
    replace_references,
    yaml_documents,
)


class RunnerJavascriptInputsTests(unittest.TestCase):
    def test_keeps_explicit_bootstrap_bytes_and_writes_two_documents(self):
        raw = b"---\nlockfileVersion: '9.0'\n\nimporters: {}\n\n---\nsettings: {}\n"
        changed = render_lock(raw, {"settings": {"autoInstallPeers": True}})
        self.assertTrue(changed.startswith(raw[: raw.index(b"settings:")]))
        self.assertEqual(len(yaml_documents(changed)), 2)
        with self.assertRaisesRegex(ValueError, "Framing"):
            render_lock(raw[4:], {"settings": {}})

    def test_rewrites_selected_peer_snapshot_and_not_scoped_or_longer_version(self):
        source = {
            "axios@1.20.0(follow-redirects@1.16.1)": {"follow-redirects": "1.16.1(debug@4.4.3)"},
            "@other/follow-redirects@1.16.1": {"unrelated": "1.16.1"},
            "follow-redirects@1.16.10": {},
        }
        changed = replace_references(source, "follow-redirects", "1.16.1", "1.16.2")
        self.assertEqual(
            changed,
            {
                "axios@1.20.0(follow-redirects@1.16.2)": {
                    "follow-redirects": "1.16.2(debug@4.4.3)"
                },
                "@other/follow-redirects@1.16.1": {"unrelated": "1.16.1"},
                "follow-redirects@1.16.10": {},
            },
        )
        with self.assertRaisesRegex(ValueError, "Colisión"):
            replace_references(
                {"undici@7.29.1": {}, "undici@7.30.1": {}}, "undici", "7.29.1", "7.30.1"
            )

    def test_projection_drops_unreachable_remote_resolution_and_keeps_patch(self):
        key = "lodash@4.18.1(patch_hash=fixture)"
        original = {
            "packages": {"lodash@4.18.1": {}, "foreign@https://example.com/source": {}},
            "snapshots": {key: {}, "foreign@https://example.com/source": {}},
            "importers": {".": {}, "packages/runner": {}, "packages/foreign": {}},
            "patchedDependencies": {"lodash@4.18.1": "fixture", "foreign": "unused"},
        }
        workspace = {
            "patchedDependencies": {
                "lodash@4.18.1": "patches/lodash.patch",
                "foreign": "patches/foreign.patch",
            }
        }
        lock, config = project_lock(original, workspace, {"packages/runner"}, {key})
        self.assertEqual(set(lock["packages"]), {"lodash@4.18.1"})
        self.assertEqual(set(lock["importers"]), {".", "packages/runner"})
        self.assertEqual(config["patchedDependencies"], {"lodash@4.18.1": "patches/lodash.patch"})

    def test_rejects_noncanonical_tarball_even_with_integrity(self):
        lock = {
            "packages": {
                "demo@1.0.0": {
                    "resolution": {
                        "integrity": "sha512-" + "A" * 86 + "==",
                        "tarball": "https://example.com/demo.tgz",
                    }
                }
            }
        }
        with self.assertRaisesRegex(ValueError, "registro npm"):
            package_resolution(lock, "demo@1.0.0")
