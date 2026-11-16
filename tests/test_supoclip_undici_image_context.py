import importlib.util
import io
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docker/supoclip/prepare_undici_image_context.py"
SPEC = importlib.util.spec_from_file_location("supoclip_undici_context", SOURCE)
CONTEXT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTEXT)


class UndiciContextTests(unittest.TestCase):
    def test_projection_has_only_one_merged_copy_in_final_stage(self):
        original = (SOURCE.parent / "Dockerfile.frontend").read_bytes()
        result = CONTEXT.dockerfile(original)
        projected, final = result.split(b"FROM wolfi\n", 1)
        self.assertIn(b"FROM scratch AS projected\n", projected)
        self.assertEqual(projected.count(b"COPY "), 3)
        for name, path in CONTEXT.TARGETS.items():
            self.assertIn(f"{name} /{path}\n".encode(), projected)
        self.assertEqual(result.count(b"FROM "), 2)
        self.assertIn(b"COPY --from=projected --chown=0:0 / /opt/supoclip/frontend/", final)
        self.assertNotIn(b"COPY --from=standalone", final)
        self.assertNotIn(b"aeee7514", result)

    def test_modified_original_recipe_rejected_before_projection(self):
        raw = (SOURCE.parent / "Dockerfile.frontend").read_bytes()
        with self.assertRaises(ValueError):
            CONTEXT.dockerfile(raw.replace(b"FROM wolfi", b"FROM alpine"))

    def test_two_preimages_are_required_and_other_manifest_rows_preserved(self):
        files = {path: dict(old) for path, old in CONTEXT.PREIMAGES.items()}
        files["server.js"] = {"bytes": 4, "sha256": CONTEXT.sha(b"keep")}
        baseline = {"files": files, "symlinks": {"link": "server.js"}, "lock": CONTEXT.APP_LOCK}
        projected = CONTEXT.project_manifest(baseline)
        self.assertEqual(projected["files"]["server.js"], files["server.js"])
        self.assertEqual(projected["symlinks"], baseline["symlinks"])
        self.assertEqual(projected["lock"], baseline["lock"])
        for name, path in CONTEXT.TARGETS.items():
            self.assertEqual(projected["files"][path], CONTEXT.PRODUCTS[name])
        self.assertEqual(baseline["files"], files)
        files[next(iter(CONTEXT.TARGETS.values()))]["sha256"] = CONTEXT.sha(b"wrong")
        with self.assertRaises(ValueError):
            CONTEXT.project_manifest(baseline)

    def test_context_tar_is_deterministic_and_five_regular_files(self):
        fixture = {name: name.encode() for name in CONTEXT.CONTEXT_MEMBERS}
        raw = CONTEXT.context_tar(fixture)
        self.assertEqual(raw, CONTEXT.context_tar(dict(reversed(list(fixture.items())))))
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            self.assertEqual(archive.getnames(), sorted(fixture))
            for member in archive:
                self.assertTrue(member.isreg())
                self.assertEqual(
                    (member.uid, member.gid, member.mtime, member.mode), (0, 0, 0, 0o444)
                )
                self.assertEqual(archive.extractfile(member).read(), fixture[member.name])
        with self.assertRaises(ValueError):
            CONTEXT.context_tar({**fixture, "unexpected": b"extra"})

    def test_undici_notice_matches_official_tar(self):
        payload = b"fixture copyright notice"
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as archive:
            member = tarfile.TarInfo("package/LICENSE")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        package = out.getvalue()
        with (
            patch.object(CONTEXT, "UNDICI_TAR_SPEC", CONTEXT.row(package)),
            patch.object(CONTEXT, "UNDICI_LICENSE", CONTEXT.row(payload)),
        ):
            self.assertEqual(CONTEXT.license_from(package), payload)
            with self.assertRaises(ValueError):
                CONTEXT.license_from(package + b"tampered")


if __name__ == "__main__":
    unittest.main()
