import base64
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import derive_moviepy_wheel as wheel


def sample_wheel(extra=None, corrupt=False):
    members = {
        "moviepy/__init__.py": b"# programa original\n",
        "moviepy-2.2.1.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: moviepy\nVersion: 2.2.1\n"
            b"Requires-Dist: pillow<12.0,>=9.2.0\nRequires-Dist: numpy>=1.25.0\n"
            b"\nDescripcion original\n"
        ),
        "moviepy-2.2.1.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "moviepy-2.2.1.dist-info/licenses/LICENCE.txt": b"licencia original\n",
    }
    members.update(extra or {})
    record = "moviepy-2.2.1.dist-info/RECORD"
    rows = []
    for name, data in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if corrupt and name == "moviepy/__init__.py":
            digest = "incorrecto"
        rows.append(f"{name},sha256={digest},{len(data)}\n")
    members[record] = ("".join(rows) + f"{record},,\n").encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return stream.getvalue(), members


class MoviePyWheelTests(unittest.TestCase):
    def test_metadata_changes_only_the_two_fixed_headers(self):
        _, source = sample_wheel()
        raw = source["moviepy-2.2.1.dist-info/METADATA"]
        self.assertEqual(
            wheel.rewrite_metadata(raw),
            raw.replace(b"Version: 2.2.1\n", b"Version: 2.2.1+agency1\n").replace(
                b"pillow<12.0,>=9.2.0", b"pillow<14,>=13"
            ),
        )

    def test_unexpected_or_duplicate_pillow_header_is_rejected(self):
        _, source = sample_wheel()
        raw = source["moviepy-2.2.1.dist-info/METADATA"]
        for replacement in (
            b"Requires-Dist: pillow>=9.2.0",
            b"Requires-Dist: pillow<12.0,>=9.2.0\nRequires-Dist: Pillow",
        ):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                wheel.rewrite_metadata(
                    raw.replace(b"Requires-Dist: pillow<12.0,>=9.2.0", replacement)
                )

    def test_archive_rejects_traversal_and_casefold_collision(self):
        for name in ("moviepy/../outside.py", "moviepy/__INIT__.py", "moviepy/NUL.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                wheel.inspect_wheel(sample_wheel({name: b"x"})[0], "2.2.1")

    def test_bad_record_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "RECORD"):
            wheel.inspect_wheel(sample_wheel(corrupt=True)[0], "2.2.1")

    def test_record_covers_every_member(self):
        raw, members = sample_wheel()
        self.assertEqual(wheel.inspect_wheel(raw, "2.2.1"), members)

    def test_derivation_rejects_a_well_formed_but_different_source(self):
        with self.assertRaisesRegex(ValueError, "SOURCE_PIN"):
            wheel.derive_bytes(sample_wheel()[0])

    def test_derivation_is_deterministic_and_keeps_all_other_members(self):
        raw, source = sample_wheel()
        with patch.multiple(
            wheel, SOURCE_SIZE=len(raw), SOURCE_SHA256=hashlib.sha256(raw).hexdigest()
        ):
            result, receipt = wheel.derive_bytes(raw)
            self.assertEqual(wheel.derive_bytes(raw), (result, receipt))
        derived = wheel.inspect_wheel(result, "2.2.1+agency1")
        for name, data in source.items():
            if name.endswith(("/RECORD", "/METADATA")):
                continue
            target = name.replace("moviepy-2.2.1.dist-info/", "moviepy-2.2.1+agency1.dist-info/")
            self.assertEqual(derived[target], data)
        self.assertEqual(receipt["member_contents_preserved"], len(source) - 2)

    def test_publish_does_not_replace_an_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / wheel.OUTPUT_NAME
            target.write_bytes(b"centinela")
            with self.assertRaises(FileExistsError):
                wheel.publish_new(target, b"nuevo")
            self.assertEqual(target.read_bytes(), b"centinela")


if __name__ == "__main__":
    unittest.main()
