"""Entradas y wheel mínimo: firmas sintéticas reales y archivos, sin ejecutar SDK."""

import base64
import gzip
import hashlib
import importlib.util
import io
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "package_opencv_minimal", ROOT / "docker/kitsu-zou/package_opencv_minimal.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def tar_bytes(files):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    return result.getvalue()


def elf_fixture(needed, soname=None, runpath="$ORIGIN/opencv_python_headless.libs:$ORIGIN"):
    # Fixture binaria sintética sólo para validar empaquetado; nunca se importa.
    strings = bytearray(b"\x00")
    entries = []
    for tag, values in ((1, needed), (14, [soname] if soname else []), (29, [runpath])):
        for value in values:
            entries.append((tag, len(strings)))
            strings.extend(value.encode() + b"\x00")
    dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in entries + [(0, 0)])
    string_offset = 64
    dynamic_offset = string_offset + len(strings)
    section_offset = dynamic_offset + len(dynamic)
    header = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    header += struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, section_offset, 0, 64, 0, 0, 64, 3, 0)
    sections = b"\x00" * 64
    sections += struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, string_offset, len(strings), 0, 0, 1, 0)
    sections += struct.pack("<IIQQQQIIQQ", 0, 6, 0, 0, dynamic_offset, len(dynamic), 1, 0, 8, 16)
    return header + strings + dynamic + sections


class InputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Clave efímera de fixture, sólo memoria; no es una identidad de despliegue.
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = cls.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        cls.key_hash = hashlib.sha256(cls.public).hexdigest()

    def fixture(self, *, name="imath-dev", version="3.2.3-r2", arch="x86_64", files=None):
        payload = gzip.compress(
            tar_bytes(files or {"usr/include/Imath/config.h": b"header fixture\n"}), mtime=0
        )
        pkginfo = f"pkgname = {name}\npkgver = {version}\narch = {arch}\ndatahash = {hashlib.sha256(payload).hexdigest()}\n".encode()
        control = gzip.compress(
            tar_bytes({".PKGINFO": pkginfo, ".melange.yaml": b"fixture: true\n"}), mtime=0
        )
        package = control + payload
        record = {
            "P": "imath-dev",
            "V": "3.2.3-r2",
            "A": "x86_64",
            "S": str(len(package)),
            "C": "Q1" + base64.b64encode(hashlib.sha1(control).digest()).decode(),
        }
        text = "\n".join(f"{key}:{value}" for key, value in record.items()) + "\n\n"
        index_control = gzip.compress(
            tar_bytes({"APKINDEX": text.encode(), "DESCRIPTION": b"fixture\n"}), mtime=0
        )
        signature = self.key.sign(index_control, padding.PKCS1v15(), hashes.SHA256())
        index = (
            gzip.compress(tar_bytes({".SIGN.RSA256.wolfi-signing.rsa.pub": signature}), mtime=0)
            + index_control
        )
        return package, index

    def authenticated(self, index):
        return MODULE.authenticate_index(
            index,
            self.public,
            index_sha256=hashlib.sha256(index).hexdigest(),
            key_sha256=self.key_hash,
        )

    def test_cadena_rsa256_indice_q1_control_datahash_sin_firma_individual(self):
        package, index = self.fixture()
        verified = MODULE.verify_wolfi_apk(
            package,
            self.authenticated(index),
            "imath-dev",
            "3.2.3-r2",
            hashlib.sha256(package).hexdigest(),
        )
        self.assertEqual(verified.signature_mode, "index-rsa256-control-q1-payload-sha256")
        self.assertFalse(verified.individual_signature)
        self.assertEqual(verified.name, "imath-dev")

    def test_indice_firma_o_clave_alterados_se_rechazan(self):
        package, index = self.fixture()
        del package
        altered = index[:-20] + bytes([index[-20] ^ 1]) + index[-19:]
        for raw, public in ((altered, self.public), (index, self.public.replace(b"A", b"B", 1))):
            with self.assertRaises(ValueError):
                MODULE.authenticate_index(
                    raw,
                    public,
                    index_sha256=hashlib.sha256(raw).hexdigest(),
                    key_sha256=hashlib.sha256(public).hexdigest(),
                )

    def test_control_payload_identidad_arquitectura_y_bytes_extra_se_rechazan(self):
        package, index = self.fixture()
        authenticated = self.authenticated(index)
        for changed in (
            package + b"extra",
            package[:-1],
            bytes([package[0] ^ 1]) + package[1:],
            package[:-15] + bytes([package[-15] ^ 1]) + package[-14:],
        ):
            with self.assertRaises(ValueError):
                MODULE.verify_wolfi_apk(
                    changed,
                    authenticated,
                    "imath-dev",
                    "3.2.3-r2",
                    hashlib.sha256(changed).hexdigest(),
                )
        for name, version, arch in (
            ("otro", "3.2.3-r2", "x86_64"),
            ("imath-dev", "9.0-r0", "x86_64"),
            ("imath-dev", "3.2.3-r2", "aarch64"),
        ):
            changed, changed_index = self.fixture(name=name, version=version, arch=arch)
            with self.assertRaises(ValueError):
                MODULE.verify_wolfi_apk(
                    changed,
                    self.authenticated(changed_index),
                    "imath-dev",
                    "3.2.3-r2",
                    hashlib.sha256(changed).hexdigest(),
                )

    def test_pin_externo_y_payload_sin_traversal_o_pyimath(self):
        package, index = self.fixture()
        with self.assertRaises(ValueError):
            MODULE.verify_wolfi_apk(
                package, self.authenticated(index), "imath-dev", "3.2.3-r2", "0" * 64
            )
        package, index = self.fixture(files={"../escape": b"no"})
        checked = MODULE.verify_wolfi_apk(
            package,
            self.authenticated(index),
            "imath-dev",
            "3.2.3-r2",
            hashlib.sha256(package).hexdigest(),
        )
        with self.assertRaises(ValueError):
            MODULE.payload_inventory(checked)

    def test_version_local_pep440_satisface_pin_publico_sin_falsear_version_cv(self):
        self.assertTrue(SpecifierSet("==4.13.0.92").contains(Version(MODULE.WHEEL_VERSION)))
        self.assertEqual(MODULE.CV_VERSION, "4.13.0")
        self.assertEqual(MODULE.WHEEL_VERSION, "4.13.0.92+agency1")


class PackagingTests(unittest.TestCase):
    def test_elf_rechaza_tls_videoio_y_rpath_de_builder(self):
        for needed, runpath in (
            (["libssl.so.1.1"], "$ORIGIN"),
            (["libavcodec.so.62"], "$ORIGIN"),
            (["libc.so.6"], "/tmp/builder/lib"),
            (["libc.so.6"], "$ORIGIN:"),
        ):
            with self.assertRaises(ValueError):
                MODULE.elf_dynamic(elf_fixture(needed, runpath=runpath))

    def test_deteccion_real_y_openexr_externo_obligatorios(self):
        cache = dict(self.cache(), OpenEXR_DIR="/kit/usr/lib/cmake/OpenEXR")
        config = "#define HAVE_OPENEXR\n#define HAVE_PNG\n"
        modules = (
            "#define HAVE_OPENCV_CORE\n#define HAVE_OPENCV_IMGPROC\n#define HAVE_OPENCV_IMGCODECS\n"
        )
        MODULE.validate_configuration(cache, config, modules, "/kit/usr/lib/cmake/OpenEXR")
        for bad in (
            config.replace("HAVE_OPENEXR", "SIN_OPENEXR"),
            config + "#define HAVE_FFMPEG\n",
        ):
            with self.assertRaises(ValueError):
                MODULE.validate_configuration(cache, bad, modules, "/kit/usr/lib/cmake/OpenEXR")
        with self.assertRaises(ValueError):
            MODULE.validate_configuration(
                dict(cache, OpenEXR_DIR="OpenEXR_DIR-NOTFOUND"),
                config,
                modules,
                "/kit/usr/lib/cmake/OpenEXR",
            )
        with self.assertRaises(ValueError):
            MODULE.validate_configuration(
                cache,
                config,
                modules + "#define HAVE_OPENCV_VIDEOIO\n",
                "/kit/usr/lib/cmake/OpenEXR",
            )

    def cache(self):
        return MODULE.REQUIRED_CACHE.copy()

    def test_cache_no_acepta_videoio_gui_descargas_git_o_fallback_openexr(self):
        cache = self.cache()
        MODULE.validate_cmake_cache(cache)
        for key in (
            "WITH_FFMPEG",
            "WITH_IPP",
            "WITH_ITT",
            "WITH_TBB",
            "BUILD_OPENEXR",
            "BUILD_opencv_videoio",
            "BUILD_opencv_highgui",
        ):
            bad = dict(cache, **{key: "ON"})
            with self.subTest(key=key), self.assertRaises(ValueError):
                MODULE.validate_cmake_cache(bad)
        for key in ("WITH_OPENEXR", "WITH_PNG", "CMAKE_DISABLE_FIND_PACKAGE_Git"):
            with self.assertRaises(ValueError):
                MODULE.validate_cmake_cache(dict(cache, **{key: "OFF"}))

    def test_wheel_reproducible_record_metadata_licencias_y_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "stage"
            (staging / "lib").mkdir(parents=True)
            needed = ["libopencv_" + name + ".so.413" for name in ("core", "imgproc", "imgcodecs")]
            (staging / "cv2.cpython-312-x86_64-linux-gnu.so").write_bytes(elf_fixture(needed))
            for component in ("core", "imgproc", "imgcodecs"):
                deps = ["libc.so.6"] + (
                    ["libpng16.so.16", "libOpenEXR-3_5.so.34"] if component == "imgcodecs" else []
                )
                (staging / "lib" / f"libopencv_{component}.so.4.13.0").write_bytes(
                    elf_fixture(deps, f"libopencv_{component}.so.413")
                )
            license_path = root / "LICENSE"
            license_path.write_text("Apache License\nVersion 2.0\n")
            one, two = root / "one", root / "two"
            one.mkdir()
            two.mkdir()
            numpy_license = b"BSD-3-Clause fixture; no licencia de un build real.\n"
            manifest = {
                "source": {"sha256": "a" * 64},
                "numpy_requires_dist": MODULE.NUMPY_REQUIRES,
                "numpy_license": {
                    "base64": base64.b64encode(numpy_license).decode(),
                    "sha256": hashlib.sha256(numpy_license).hexdigest(),
                },
            }
            first = MODULE.write_wheel(staging, one, license_path, manifest)
            second = MODULE.write_wheel(staging, two, license_path, manifest)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                metadata = archive.read(MODULE.DIST_INFO + "/METADATA").decode()
                self.assertIn("Version: 4.13.0.92+agency1", metadata)
                self.assertIn('Requires-Dist: numpy>=2; python_version >= "3.9"', metadata)
                self.assertIn(
                    "Tag: cp312-cp312-linux_x86_64",
                    archive.read(MODULE.DIST_INFO + "/WHEEL").decode(),
                )
                for row in archive.read(MODULE.DIST_INFO + "/RECORD").decode().splitlines():
                    name, checksum, size = row.split(",")
                    if name.endswith("/RECORD"):
                        self.assertEqual((checksum, size), ("", ""))
                        continue
                    data = archive.read(name)
                    self.assertEqual(int(size), len(data))
                    self.assertEqual(
                        checksum,
                        "sha256="
                        + base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                        .decode()
                        .rstrip("="),
                    )
            with self.assertRaises(ValueError):
                MODULE.write_wheel(staging, one, license_path, manifest)


if __name__ == "__main__":
    unittest.main()
