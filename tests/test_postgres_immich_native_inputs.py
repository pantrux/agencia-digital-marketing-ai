"""Firmas y clausura APK: fixture público oficial, sin claves privadas ni SDK."""

import gzip
import hashlib
import importlib.util
import io
import tarfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "postgres_native_inputs", ROOT / "docker/postgres-immich/native_inputs.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURES = ROOT / "tests/fixtures"
APK = (FIXTURES / "build-base-0.5-r4.apk").read_bytes()
KEY = (FIXTURES / "alpine-devel-6165ee59.rsa.pub").read_bytes()
KEY_ID = "alpine-devel@lists.alpinelinux.org-6165ee59.rsa.pub"
EXPECTED = {"P": "build-base", "V": "0.5-r4", "A": "noarch", "C": "Q1nzZkq3HEVZFi3l/qBevDd+H7QNc="}


def split(raw):
    parts = []
    while raw:
        decoder = zlib.decompressobj(31)
        plain = decoder.decompress(raw) + decoder.flush()
        consumed = len(raw) - len(decoder.unused_data)
        parts.append((raw[:consumed], plain))
        raw = decoder.unused_data
    return parts


def invalid_signature(raw):
    parts = split(raw)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        member = tarfile.TarInfo(".SIGN.RSA." + KEY_ID)
        member.size = 512
        archive.addfile(member, io.BytesIO(b"x" * 512))
    return gzip.compress(output.getvalue(), mtime=0) + parts[1][0] + parts[2][0]


class NativeInputs(unittest.TestCase):
    def test_importacion_sin_red_procesos_ni_sdk(self):
        spec = importlib.util.spec_from_file_location("native_import", SPEC.origin)
        module = importlib.util.module_from_spec(spec)
        with (
            mock.patch("subprocess.run") as process,
            mock.patch("urllib.request.urlopen") as network,
        ):
            spec.loader.exec_module(module)
        process.assert_not_called()
        network.assert_not_called()

    def test_fixture_oficial_verifica_firma_identidad_control_y_payload(self):
        self.assertEqual(
            hashlib.sha256(APK).hexdigest(),
            "3ac72e74e96150995e8d797b6c33fc3b46d69e3667c47bd0ff25ce89f9fc4eaf",
        )
        result = MODULE.verify_apk(APK, {KEY_ID: KEY}, EXPECTED)
        self.assertEqual(result["name"], "build-base")
        self.assertEqual(result["version"], "0.5-r4")
        self.assertEqual(result["signature_scheme"], "RSA-SHA1")
        self.assertFalse(result["scripts_executed"])
        self.assertTrue(result["payload_hash_verified"])

    def test_firma_falsa_o_clave_no_confiable_se_rechazan(self):
        for raw, keys in ((invalid_signature(APK), {KEY_ID: KEY}), (APK, {})):
            with self.assertRaises(ValueError):
                MODULE.verify_apk(raw, keys, EXPECTED)

    def test_datahash_rechaza_payload_modificado_aunque_control_este_intacto(self):
        parts = split(APK)
        altered = parts[0][0] + parts[1][0] + gzip.compress(b"otro payload", mtime=0)
        with self.assertRaises(ValueError):
            MODULE.verify_apk(altered, {KEY_ID: KEY}, EXPECTED)

    def test_rechaza_identidad_nombre_version_o_arch_distintos_del_indice(self):
        for change in ({"P": "other"}, {"V": "0.5-r3"}, {"A": "aarch64"}, {"C": "Q1bad"}):
            with self.assertRaises(ValueError):
                MODULE.verify_apk(APK, {KEY_ID: KEY}, {**EXPECTED, **change})

    def test_noarch_firmado_es_valido_en_indice_x86_64_sin_aceptar_otro_binario(self):
        indexed = {**EXPECTED, "A": "x86_64"}
        result = MODULE.verify_apk(APK, {KEY_ID: KEY}, indexed)
        self.assertEqual(result["arch"], "noarch")
        control = MODULE.verified_control(APK, {KEY_ID: KEY})
        control["files"][".PKGINFO"] = control["files"][".PKGINFO"].replace(
            b"arch = noarch", b"arch = aarch64"
        )
        with mock.patch.object(MODULE, "verified_control", return_value=control):
            with self.assertRaises(ValueError):
                MODULE.verify_apk(APK, {KEY_ID: KEY}, indexed)

    def test_parser_no_interpreta_scripts_y_rechaza_campos_criticos_repetidos(self):
        records = MODULE.parse_records("P:a\nV:1-r0\nA:x86_64\nD:b $(comando)\n\n")
        self.assertEqual(records["a"]["D"], "b $(comando)")
        with self.assertRaises(ValueError):
            MODULE.parse_records("P:a\nV:1-r0\nV:2-r0\n\n")

    def test_clausura_conserva_proveedor_existente_y_resuelve_transitivos(self):
        records = {
            "sdk": {"P": "sdk", "V": "1-r0", "D": "icu-data=78-r0 compiler>=2"},
            "icu-full": {"P": "icu-full", "V": "78-r0", "p": "icu-data=78-r0"},
            "icu-en": {"P": "icu-en", "V": "78-r0", "p": "icu-data=78-r0"},
            "compiler": {"P": "compiler", "V": "2.1-r1", "D": "libc"},
            "libc": {"P": "libc", "V": "1-r0"},
        }
        result = MODULE.resolve_closure(records, {"sdk": "1-r0"}, {"icu-full": records["icu-full"]})
        self.assertEqual(set(result["packages"]), {"sdk", "compiler", "libc"})
        self.assertIn("icu-full", result["base_reused"])
        self.assertTrue(result["native_solver_confirmation_pending"])

    def test_clausura_rechaza_ambiguedad_actualizacion_ajena_y_git(self):
        for records, seeds, base in (
            (
                {
                    "a": {"P": "a", "V": "1", "D": "cap"},
                    "b": {"P": "b", "V": "1", "p": "cap=1"},
                    "c": {"P": "c", "V": "1", "p": "cap=1"},
                },
                {"a": "1"},
                {},
            ),
            ({"a": {"P": "a", "V": "2"}}, {"a": "2"}, {"a": {"P": "a", "V": "1"}}),
            ({"git": {"P": "git", "V": "1"}}, {"git": "1"}, {}),
        ):
            with self.assertRaises(ValueError):
                MODULE.resolve_closure(records, seeds, base)

    def test_clausura_incluye_install_if_y_detecta_conflicto(self):
        records = {
            "a": {"P": "a", "V": "1"},
            "b": {"P": "b", "V": "1", "i": "a=1", "D": "c"},
            "c": {"P": "c", "V": "1"},
        }
        result = MODULE.resolve_closure(records, {"a": "1"}, {})
        self.assertEqual(set(result["packages"]), {"a", "b", "c"})
        records["a"]["D"] = "!c"
        with self.assertRaises(ValueError):
            MODULE.resolve_closure(records, {"a": "1"}, {})

    def test_fuzzy_python_solo_acepta_el_prefijo_numerico_fijado(self):
        self.assertTrue(MODULE.version_match("3.14.10-r1", "~", "3.14"))
        self.assertFalse(MODULE.version_match("3.15.0-r0", "~", "3.14"))
        with self.assertRaises(ValueError):
            MODULE.version_match("3.14.10-r1", "~", "3.14-r1")


if __name__ == "__main__":
    unittest.main()
