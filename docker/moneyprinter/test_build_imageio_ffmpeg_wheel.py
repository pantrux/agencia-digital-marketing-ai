import base64
import csv
import gzip
import hashlib
import io
import tarfile
import unittest
import zipfile
from unittest.mock import patch

import build_imageio_ffmpeg_wheel as wheel

METADATA = (
    b"Metadata-Version: 2.1\nName: imageio-ffmpeg\nVersion: 0.6.0\n"
    b"License: BSD-2-Clause\nRequires-Python: >=3.9\nLicense-File: LICENSE\n\nDescripcion\n"
)


def tar_bytes(files, link=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, body in files:
            info = tarfile.TarInfo(wheel.SOURCE_ROOT + "/" + name)
            if name == link:
                info.type = tarfile.SYMTYPE
                info.linkname = "outside"
                archive.addfile(info)
            else:
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return gzip.compress(output.getvalue(), mtime=0)


def package_files():
    rows = [(name, f"dato de {name}\n".encode()) for name in sorted(wheel.PAYLOAD)]
    return rows + [("PKG-INFO", METADATA), ("LICENSE", b"Licencia original\n")]


class ImageioFFmpegWheelTests(unittest.TestCase):
    def test_accepts_only_pinned_sdist_at_build_boundary(self):
        with self.assertRaisesRegex(ValueError, "SOURCE_PIN"):
            wheel.build_bytes(tar_bytes(package_files()))

    def test_rejects_native_payload_and_traversal(self):
        for path in ("imageio_ffmpeg/binaries/ffmpeg-linux-x86_64", "imageio_ffmpeg/../outside.py"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "TAR_MEMBER"):
                wheel.read_source(tar_bytes([*package_files(), (path, b"ELF")]))

    def test_rejects_symlink_and_duplicate(self):
        with self.assertRaisesRegex(ValueError, "TAR_MEMBER"):
            wheel.read_source(tar_bytes(package_files(), link="imageio_ffmpeg/_utils.py"))
        with self.assertRaisesRegex(ValueError, "TAR_MEMBER"):
            wheel.read_source(tar_bytes([*package_files(), ("LICENSE", b"otro")]))

    def test_rejects_wrong_distribution_metadata(self):
        rows = [
            (name, data.replace(b"Version: 0.6.0", b"Version: 0.7.0"))
            for name, data in package_files()
        ]
        with self.assertRaisesRegex(ValueError, "METADATA"):
            wheel.read_source(tar_bytes(rows))

    def test_wheel_keeps_package_license_and_metadata_with_complete_record(self):
        source = dict(package_files())
        raw = tar_bytes(source.items())
        with patch.multiple(
            wheel, SOURCE_SIZE=len(raw), SOURCE_SHA256=hashlib.sha256(raw).hexdigest()
        ):
            result, proof = wheel.build_bytes(raw)
            self.assertEqual(wheel.build_bytes(raw), (result, proof))
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        prefix = "imageio_ffmpeg-0.6.0.dist-info/"
        self.assertEqual(files[prefix + "METADATA"], METADATA)
        self.assertEqual(files[prefix + "LICENSE"], source["LICENSE"])
        self.assertIn(b"Tag: py3-none-any", files[prefix + "WHEEL"])
        self.assertEqual(
            {name: data for name, data in files.items() if name in wheel.PAYLOAD},
            {name: source[name] for name in wheel.PAYLOAD},
        )
        record_name = prefix + "RECORD"
        rows = list(csv.reader(io.StringIO(files[record_name].decode())))
        self.assertEqual(len(rows), len(files))
        self.assertEqual({row[0] for row in rows}, set(files))
        for name, checksum, size in rows:
            if name == record_name:
                self.assertEqual((checksum, size), ("", ""))
                continue
            expected = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=")
            self.assertEqual(
                (checksum, size), ("sha256=" + expected.decode(), str(len(files[name])))
            )


if __name__ == "__main__":
    unittest.main()
