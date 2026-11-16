"""Lectura de bibliotecas como datos; sin red, Docker ni ejecución de ELF."""

import io
import tarfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "docker/kitsu-zou/capture_opencv_runtime_materials_remote.py"
)
M = types.ModuleType("opencv_runtime_materials_pure_test")
M.__file__ = str(SOURCE)
exec(compile(SOURCE.read_bytes(), str(SOURCE), "exec"), M.__dict__)
LOCAL_SOURCE = SOURCE.with_name("capture_opencv_runtime_materials.py")
L = types.ModuleType("opencv_runtime_materials_local_test")
L.__file__ = str(LOCAL_SOURCE)
exec(compile(LOCAL_SOURCE.read_bytes(), str(LOCAL_SOURCE), "exec"), L.__dict__)


def archive(rows):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w", format=tarfile.USTAR_FORMAT) as output:
        for name, kind, payload in rows:
            item = tarfile.TarInfo(name)
            item.mode = 0o644
            if kind == "file":
                item.size = len(payload)
                output.addfile(item, io.BytesIO(payload))
            else:
                item.type = {
                    "sym": tarfile.SYMTYPE,
                    "hard": tarfile.LNKTYPE,
                    "dir": tarfile.DIRTYPE,
                }[kind]
                item.linkname = payload
                output.addfile(item)
    return result.getvalue()


def selected(raw, targets):
    return M.select_files(io.BytesIO(raw), len(raw), targets)


class ArchiveTests(unittest.TestCase):
    def test_prefijo_punto_relativo_se_normaliza_y_no_oculta_duplicados(self):
        raw = archive([("./usr/lib/a", "file", b"a")])
        self.assertEqual(selected(raw, {"a": "usr/lib/a"})["a"]["data"], b"a")
        with self.assertRaises(ValueError):
            selected(archive([("./a", "file", b"1"), ("a", "file", b"2")]), {"a": "a"})

    def test_resuelve_alias_virtual_sin_extraer_y_preserva_bytes(self):
        raw = archive(
            [
                ("lib", "sym", "usr/lib"),
                ("usr/lib/libz.so.1", "sym", "libz.so.1.3"),
                ("usr/lib/libz.so.1.3", "file", b"biblioteca"),
            ]
        )
        result = selected(raw, {"z": "lib/libz.so.1"})
        self.assertEqual(result["z"]["data"], b"biblioteca")
        self.assertEqual(result["z"]["resolved_member"], "usr/lib/libz.so.1.3")

    def test_enlace_absoluto_se_resuelve_dentro_del_tar_y_hardlink_desde_raiz(self):
        raw = archive(
            [
                ("usr/lib/a", "file", b"a"),
                ("lib64", "sym", "/usr/lib"),
                ("usr/lib/b", "hard", "usr/lib/a"),
            ]
        )
        self.assertEqual(selected(raw, {"a": "lib64/b"})["a"]["data"], b"a")

    def test_rechaza_duplicados_y_traversal_aunque_no_sean_objetivos(self):
        for rows in [
            [("a", "file", b"1"), ("a", "file", b"2")],
            [("../otro", "file", b"1"), ("a", "file", b"2")],
        ]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                selected(archive(rows), {"a": "a"})

    def test_rechaza_ciclos_escapes_y_archivo_usado_como_directorio(self):
        for rows, target in [
            ([("a", "sym", "b"), ("b", "sym", "a")], "a"),
            ([("a", "sym", "../fuera")], "a"),
            ([("a", "file", b"x")], "a/b"),
        ]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                selected(archive(rows), {"x": target})

    def test_rechaza_objetivos_ausentes_directorios_y_presupuesto(self):
        raw = archive([("a", "dir", ""), ("b", "file", b"1234")])
        for target in ("ausente", "a"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                selected(raw, {"x": target})
        with self.assertRaises(ValueError):
            M.select_files(io.BytesIO(raw), len(raw), {"x": "b"}, maximum_file=3)

    def test_rechaza_trailer_no_nulo_y_tar_truncado(self):
        raw = archive([("a", "file", b"contenido")])
        for changed in (raw + b"otro" + bytes(508), raw[:1024]):
            with self.subTest(size=len(changed)), self.assertRaises(ValueError):
                selected(changed, {"a": "a"})

    def test_firma_posix_detecta_cambios_de_identidad_y_contenido(self):
        first = types.SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_ctime_ns=5)
        other = types.SimpleNamespace(**{**vars(first), "st_ino": 8})
        self.assertNotEqual(M.fingerprint(first), M.fingerprint(other))

    def test_importar_no_inicia_operaciones(self):
        self.assertNotIn("paramiko", M.__dict__)
        self.assertEqual(
            M.TARGETS,
            {
                name: "usr/lib/" + name
                for name in (
                    "ld-linux-x86-64.so.2",
                    "libc.so.6",
                    "libm.so.6",
                    "libgcc_s.so.1",
                    "libz.so.1",
                )
            },
        )


class EntryTests(unittest.TestCase):
    def test_plan_no_prepara_ni_abre_red(self):
        with (
            patch.object(
                L, "context", return_value=({"source_sha256": "fuente"}, {}, b"", None, None)
            ),
            patch.object(L, "prepare", side_effect=AssertionError("No preparar")),
            patch.object(L, "fetch", side_effect=AssertionError("Sin red")),
        ):
            self.assertEqual(L.main([]), 0)

    def test_pins_ausentes_rechazan_preparacion_y_captura_antes_de_efectos(self):
        for mode in ("prepare", "capture"):
            with (
                self.subTest(mode=mode),
                patch.object(
                    L, "context", return_value=({"source_sha256": "fuente"}, {}, b"", None, None)
                ),
                patch.object(L, "prepare", side_effect=AssertionError("No preparar")),
                patch.object(L, "fetch", side_effect=AssertionError("Sin red")),
                self.assertRaises(ValueError),
            ):
                L.main([mode])

    def test_modulo_remoto_no_admite_usuario_windows_ni_ejecuta_auditor(self):
        with patch.object(M.os, "name", "nt"), self.assertRaises(ValueError):
            M.capture({})


if __name__ == "__main__":
    unittest.main()
