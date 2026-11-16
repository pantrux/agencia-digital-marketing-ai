"""Reubicación ELF como datos: casos sintéticos y plan real sin publicar wheel."""

import hashlib
import io
import json
import os
import struct
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OLD = b"$ORIGIN/opencv_python_headless.libs:$ORIGIN:/overlay/usr/lib"


def load():
    path = ROOT / "docker/kitsu-zou/prepare_opencv_relocated_wheel.py"
    module = types.ModuleType("opencv_relocation_test")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), vars(module))
    return module


def elf(
    *,
    rpath=OLD,
    shared_symbol=False,
    shared_dynamic=False,
    version_alias=False,
    unknown_link=False,
    runpath=False,
):
    """ELF64 amd64 con DT_STRTAB, símbolos y versiones controlados independientemente."""
    names = b"\0.dynstr\0.dynamic\0.dynsym\0.shstrtab\0.gnu.version_r\0.unknown\0"
    strings = b"\0libc.so.6\0symbol\0" + rpath + b"\0"
    at = strings.index(rpath)
    data = bytearray(256)
    data.extend(strings)

    def append(raw):
        while len(data) % 8:
            data.append(0)
        offset = len(data)
        data.extend(raw)
        return offset, len(raw)

    dynsym = bytes(24) + struct.pack(
        "<IBBHQQ", at + 1 if shared_symbol else strings.index(b"symbol"), 0x12, 0, 0, 0, 0
    )
    symoff, symsize = append(dynsym)
    extra = []
    if version_alias:
        off, size = append(
            struct.pack("<HHIII", 1, 1, 1, 16, 0) + struct.pack("<IHHII", 0, 0, 0, at + 2, 0)
        )
        extra.append((names.index(b".gnu.version_r"), 0x6FFFFFFE, off, size, 1, 0))
    if unknown_link:
        off, size = append(bytes(16))
        extra.append((names.index(b".unknown"), 0x6FFFFFF7, off, size, 1, 0))
    stroff, strsize = append(names)
    while len(data) % 8:
        data.append(0)
    shoff = len(data)
    sections = [
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (names.index(b".dynstr"), 3, 2, 0x1000 + 256, 256, len(strings), 0, 0, 1, 0),
        (names.index(b".dynamic"), 6, 3, 0x1000 + 128, 128, 96, 1, 0, 8, 16),
        (names.index(b".dynsym"), 11, 2, 0x1000 + symoff, symoff, symsize, 1, 0, 8, 24),
        (names.index(b".shstrtab"), 3, 0, 0, stroff, strsize, 0, 0, 1, 0),
    ]
    sections.extend(
        (name, kind, 0, 0, offset, size, link, 0, 8, entsize)
        for name, kind, offset, size, link, entsize in extra
    )
    for row in sections:
        data.extend(struct.pack("<IIQQQQIIQQ", *row))
    data[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into(
        "<HHIQQQIHHHHHH", data, 16, 3, 62, 1, 0, 64, shoff, 0, 64, 56, 1, 64, len(sections), 4
    )
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 4, 0, 0x1000, 0x1000, len(data), len(data), 4096)
    dynamic = [(5, 0x1000 + 256), (10, len(strings)), (29 if runpath else 15, at), (1, 1)]
    if shared_dynamic:
        dynamic.append((14, at))
    dynamic.append((0, 0))
    for i, row in enumerate(dynamic):
        struct.pack_into("<qQ", data, 128 + i * 16, *row)
    return bytes(data)


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.r = load()

    def test_retira_solo_sufijo_mantiene_tamano_tablas_y_bytes_externos(self):
        original = elf()
        result, proof = self.r.relocate(original)
        start, end = proof["interval"]
        self.assertEqual(len(original), len(result))
        self.assertEqual(original[:start], result[:start])
        self.assertEqual(original[end:], result[end:])
        self.assertEqual(original[start:end], b":/overlay/usr/lib")
        self.assertEqual(result[start:end], bytes(end - start))
        self.assertEqual(proof["sha256_before"], hashlib.sha256(original).hexdigest())
        self.assertEqual(proof["sha256_after"], hashlib.sha256(result).hexdigest())

    def test_rechaza_compartir_cadena_por_simbolo_dynamic_o_version(self):
        for kwargs in ({"shared_symbol": True}, {"shared_dynamic": True}, {"version_alias": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.r.relocate(elf(**kwargs))

    def test_rechaza_semantica_desconocida_que_enlaza_dynstr(self):
        with self.assertRaises(ValueError):
            self.r.relocate(elf(unknown_link=True))

    def test_rechaza_runpath_doble_rpath_y_sufijo_distinto(self):
        for original in (elf(runpath=True), elf(rpath=OLD + b"/other"), elf(rpath=b"$ORIGIN")):
            with self.assertRaises(ValueError):
                self.r.relocate(original)
        original = bytearray(elf())
        struct.pack_into("<qQ", original, 128 + 4 * 16, 15, 17)
        with self.assertRaises(ValueError):
            self.r.relocate(bytes(original))

    def test_rechaza_offset_cadena_secciones_o_segmento_ambiguos(self):
        for key in ("section", "pointer", "segment"):
            original = bytearray(elf())
            if key == "section":
                struct.pack_into("<Q", original, 40, len(original) + 64)
            elif key == "pointer":
                struct.pack_into("<Q", original, 128 + 2 * 16 + 8, 999999)
            else:
                struct.pack_into("<Q", original, 64 + 8, 1)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.r.relocate(bytes(original))

    def test_evidencia_no_admite_modificar_fuera_intervalo(self):
        original = elf()
        result, proof = self.r.relocate(original)
        changed = bytearray(result)
        changed[-1] ^= 1
        with self.assertRaises(ValueError):
            self.r.verify_delta(original, bytes(changed), proof["interval"])

    def test_parsers_congelados_aceptan_sintetico_corregido_sin_hooks(self):
        packager, parser = self.r.frozen_sources()
        original = elf()
        result, _ = self.r.relocate(original)
        self.assertEqual(parser.elf_info(original)["rpath"], OLD.decode())
        self.assertEqual(
            parser.elf_info(result)["rpath"], "$ORIGIN/opencv_python_headless.libs:$ORIGIN"
        )
        self.assertEqual(packager.elf_dynamic(result)["needed"], ["libc.so.6"])

    def test_rutas_relativas_rechazan_traversal_ads_y_dispositivos_windows(self):
        for name in (
            "../a",
            "/root",
            "C:/a",
            "a\\b",
            "a//b",
            "a/./b",
            "a/NUL.txt",
            "COM1.exe",
            "a./b",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.r.safe_relative(name)
        self.assertEqual(
            self.r.safe_relative("stage/lib/libopencv_core.so.4.13.0"),
            "stage/lib/libopencv_core.so.4.13.0",
        )

    def test_io_exclusivo_no_sustituye_ajeno_y_rechaza_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "artifact.bin"
            target.write_bytes(b"foreign")
            with self.assertRaises(FileExistsError):
                self.r.write_exclusive(target, b"replacement")
            self.assertEqual(target.read_bytes(), b"foreign")
            link = Path(directory) / "hardlink"
            os.link(target, link)
            with self.assertRaises(ValueError):
                self.r.read_regular(target)

    def test_arbol_captura_admite_archivo_regular_y_rechaza_extra(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build").mkdir()
            (root / "build/a").write_bytes(b"safe")
            self.r.exact_input_tree(root, {"build/a"})
            (root / "extra").mkdir()
            with self.assertRaises(ValueError):
                self.r.exact_input_tree(root, {"build/a"})

    def test_spec_cierra_entradas_fuentes_y_salida(self):
        spec = json.loads((ROOT / "docker/kitsu-zou/opencv-relocation-spec.json").read_bytes())
        self.r.validate_spec(spec)
        for key, value in (
            ("owner", "foreign"),
            ("output_relative", "../../foreign"),
            ("input_marker_sha256", "0" * 64),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.r.validate_spec({**spec, key: value})

    def test_plan_default_y_apply_sin_hash_no_publican(self):
        with (
            patch.object(self.r, "make_plan", return_value={"fixture": True}),
            patch.object(self.r, "apply", side_effect=AssertionError("Publicación")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(self.r.main([]), 0)
            self.assertEqual(self.r.main(["--apply"]), 1)
        with (
            patch.object(self.r, "make_plan", return_value={"fixture": True}),
            patch.object(self.r, "load_spec", side_effect=AssertionError("IO")),
            self.assertRaises(ValueError),
        ):
            self.r.apply("0" * 64)


if __name__ == "__main__":
    unittest.main()
