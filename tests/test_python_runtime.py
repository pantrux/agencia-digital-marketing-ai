"""Preparación pura de payloads; no ejecuta SDK, contenedores ni bibliotecas."""

import io
import json
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "docker/python-runtime/prepare_python_runtime.py"


def load():
    module = types.ModuleType("python_runtime_tests")
    module.__file__ = str(PATH)
    exec(compile(PATH.read_bytes(), str(PATH), "exec"), vars(module))
    return module


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        for name, kind, data, mode in entries:
            row = tarfile.TarInfo(name)
            row.mode = mode
            if kind == "file":
                row.size = len(data)
                archive.addfile(row, io.BytesIO(data))
            else:
                row.type = {
                    "dir": tarfile.DIRTYPE,
                    "symlink": tarfile.SYMTYPE,
                    "hardlink": tarfile.LNKTYPE,
                    "device": tarfile.CHRTYPE,
                }[kind]
                row.linkname = data
                archive.addfile(row)
    return output.getvalue()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_clausura_prefiere_base_y_exige_un_proveedor_fijado(self):
        base = {"glibc": {"P": "glibc", "V": "1-r0", "p": "so:libc.so.6=6"}}
        managed = {
            "python": {"P": "python", "V": "3.12.16-r0", "D": "so:libc.so.6 dep=1-r0"},
            "dep": {"P": "dep", "V": "1-r0", "D": "so:libc.so.6"},
        }
        closure = self.m.close_dependencies(base, managed, "python")
        self.assertEqual(closure["selected"], ["dep", "python"])
        self.assertEqual(closure["base_used"], ["glibc"])
        with self.assertRaises(ValueError):
            self.m.close_dependencies(base, {"python": managed["python"]}, "python")
        with self.assertRaises(ValueError):
            self.m.close_dependencies(
                base, {**managed, "other": {"P": "other", "V": "1-r0", "p": "dep=1-r0"}}, "python"
            )

    def test_no_interpreta_constraints_o_conflictos_desconocidos(self):
        for dependency in ("dep>=1", "!dep", "dep~1"):
            with self.assertRaises(ValueError):
                self.m.close_dependencies(
                    {}, {"python": {"P": "python", "V": "1-r0", "D": dependency}}, "python"
                )

    def test_payload_preserva_bytes_links_licencia_y_metadata(self):
        raw = tar_bytes(
            [
                ("usr", "dir", "", 0o755),
                ("usr/lib", "dir", "", 0o755),
                ("usr/lib/libx.so.1", "file", b"bytes", 0o755),
                ("usr/lib/libx.so", "symlink", "libx.so.1", 0o777),
                ("usr/share/LICENSE", "file", b"license", 0o644),
            ]
        )
        rows = self.m.payload_nodes(raw, "package")
        self.assertEqual(rows["usr/lib/libx.so.1"]["data"], b"bytes")
        self.assertEqual(self.m.resolve_link(rows, "usr/lib/libx.so"), "usr/lib/libx.so.1")
        self.assertEqual(rows["usr/share/LICENSE"]["mode"], 0o644)

    def test_payload_rechaza_traversal_case_duplicates_devices_suid_xattr(self):
        for entries in (
            [("../escape", "file", b"x", 0o644)],
            [("a", "file", b"x", 0o644), ("A", "file", b"x", 0o644)],
            [("a", "device", "", 0o644)],
            [("a", "file", b"x", 0o4755)],
        ):
            with self.assertRaises(ValueError):
                self.m.payload_nodes(tar_bytes(entries), "package")

    def test_payload_rechaza_trailer_oculto_o_sin_terminador(self):
        raw = tar_bytes([("a", "file", b"x", 0o644)])
        for changed in (raw + b"bad" + bytes(509), raw[:1024]):
            with self.assertRaises(ValueError):
                self.m.payload_nodes(changed, "package")

    def test_links_virtuales_no_siguen_el_host_ni_escapan(self):
        for entries, target in (
            ([("a", "symlink", "../outside", 0o777)], "a"),
            ([("a", "symlink", "b", 0o777), ("b", "symlink", "a", 0o777)], "a"),
        ):
            with self.assertRaises(ValueError):
                rows = self.m.payload_nodes(tar_bytes(entries), "package")
                self.m.resolve_link(rows, target)
        rows = self.m.payload_nodes(
            tar_bytes([("usr/lib/a", "file", b"x", 0o644), ("b", "hardlink", "usr/lib/a", 0o644)]),
            "package",
        )
        self.assertEqual(self.m.resolve_link(rows, "b"), "usr/lib/a")

    def test_checksum_pax_apk_es_metadata_declarada_y_no_abre_xattrs(self):
        def make(pax):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w:", format=tarfile.PAX_FORMAT) as archive:
                row = tarfile.TarInfo("a")
                row.size = 1
                row.mode = 0o644
                row.pax_headers = pax
                archive.addfile(row, io.BytesIO(b"x"))
            return output.getvalue()

        good = self.m.payload_nodes(make({"APK-TOOLS.checksum.SHA1": "a" * 40}), "package")
        self.assertEqual(good["a"]["apk_tools_checksum_sha1_declared"], "a" * 40)
        for pax in (
            {"APK-TOOLS.checksum.SHA1": "bad"},
            {"SCHILY.xattr.security.capability": "bad"},
            {"unknown": "value"},
        ):
            with self.assertRaises(ValueError):
                self.m.payload_nodes(make(pax), "package")

    def test_merge_solo_admite_directorios_identicos_y_dedup_documentada(self):
        first = self.m.payload_nodes(
            tar_bytes([("usr", "dir", "", 0o755), ("usr/a", "file", b"x", 0o644)]), "first"
        )
        second = self.m.payload_nodes(
            tar_bytes([("usr", "dir", "", 0o755), ("usr/b", "file", b"y", 0o644)]), "second"
        )
        merged = self.m.merge_payloads([first, second])
        self.assertEqual(merged["usr"]["packages"], ["first", "second"])
        altered = self.m.payload_nodes(tar_bytes([("usr/a", "file", b"different", 0o644)]), "third")
        with self.assertRaises(ValueError):
            self.m.merge_payloads([first, altered])

    def test_alias_autentico_pendiente_no_se_confunde_con_resolucion(self):
        nodes = self.m.payload_nodes(
            tar_bytes([("usr/lib/terminfo", "symlink", "../share/terminfo", 0o777)]), "ncurses"
        )
        with self.assertRaises(ValueError):
            self.m.merge_payloads([nodes])
        result = self.m.merge_payloads(
            [nodes], pending_aliases={"usr/lib/terminfo": "../share/terminfo"}
        )
        self.assertTrue(result["usr/lib/terminfo"]["unresolved_base_alias"])
        with self.assertRaises(ValueError):
            self.m.merge_payloads([nodes], pending_aliases={"usr/lib/terminfo": "different"})

    def test_contexto_determinista_preserva_symlinks_sin_crearlos_en_host(self):
        rows = self.m.payload_nodes(
            tar_bytes([("usr/a", "file", b"x", 0o644), ("usr/b", "symlink", "a", 0o777)]), "package"
        )
        first = self.m.context_bytes(b"FROM fixed\nCOPY rootfs/ /\n", rows)
        self.assertEqual(first, self.m.context_bytes(b"FROM fixed\nCOPY rootfs/ /\n", rows))
        with tarfile.open(fileobj=io.BytesIO(first), mode="r:") as archive:
            self.assertEqual(archive.getmember("rootfs/usr/b").linkname, "a")
            self.assertEqual(archive.extractfile("rootfs/usr/a").read(), b"x")

    def test_contexto_emite_destino_antes_del_hardlink_aunque_nombre_ordene_antes(self):
        rows = self.m.payload_nodes(
            tar_bytes([("z-file", "file", b"x", 0o644), ("a-link", "hardlink", "z-file", 0o644)]),
            "package",
        )
        raw = self.m.context_bytes(b"FROM fixed\nCOPY rootfs/ /\n", rows)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            names = archive.getnames()
            self.assertLess(names.index("rootfs/z-file"), names.index("rootfs/a-link"))

    def test_pin_plan_incorrecto_rechaza_antes_de_crear_namespace(self):
        with (
            patch.object(
                self.m, "prepare_data", return_value=({"fixture": True}, None, None, None)
            ),
            patch.object(self.m.os, "mkdir", side_effect=AssertionError("NO_CREATE")),
        ):
            with self.assertRaises(ValueError):
                self.m.prepare("0" * 64)

    def test_overlay_no_modifica_apk_db_ni_inyecta_compilador(self):
        for path in ("lib/apk/db/installed", "lib/apk/db/triggers", "etc/apk/world", "usr/bin/gcc"):
            nodes = self.m.payload_nodes(tar_bytes([(path, "file", b"data", 0o644)]), "package")
            with self.assertRaises(ValueError):
                self.m.validate_overlay_scope(nodes)
        self.m.validate_overlay_scope(
            self.m.payload_nodes(
                tar_bytes([("usr/include/python3.12/pyconfig.h", "file", b"config", 0o644)]),
                "python",
            )
        )

    def test_dockerfile_no_run_no_sdk_y_rutas_carga_cerradas(self):
        raw = (PATH.parent / "Dockerfile.runtime-python").read_bytes()
        self.m.validate_dockerfile(raw)
        with self.assertRaises(ValueError):
            self.m.validate_dockerfile(raw + b"RUN true\n")
        with self.assertRaises(ValueError):
            self.m.validate_dockerfile(raw.replace(b"/usr/lib64:/usr/lib", b"/tmp:/usr/lib"))

    def test_sbom_separa_manual_de_instalado_sin_fabricar_dbsha(self):
        base = {"glibc": {"P": "glibc", "V": "1-r0", "A": "x86_64", "L": "LGPL-2.1-or-later"}}
        manual = [
            {
                "name": "python",
                "version": "3.12.16-r0",
                "record": {"P": "python", "V": "3.12.16-r0", "A": "x86_64", "L": "PSF-2.0"},
                "sha256": "a" * 64,
            }
        ]
        value = self.m.sbom_documents(base, manual)
        self.assertFalse(value["manual"]["metadata"]["properties"][0]["value"] == "installed")
        self.assertEqual(len(value["base"]["components"]), 2)
        self.assertEqual(len(value["manual"]["components"]), 2)

    def test_plan_default_no_prepara_ni_toca_imagen(self):
        with (
            patch.object(self.m, "make_plan", return_value={"fixture": True}),
            patch.object(self.m, "prepare", side_effect=AssertionError("NO_PREPARE")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(self.m.main([]), 0)
            self.assertEqual(self.m.main(["--prepare"]), 1)

    def test_spec_no_puede_redirigir_lector_a_boveda_u_otro_archivo(self):
        spec = json.loads((PATH.parent / "python-runtime-inputs.json").read_bytes())
        self.m.validate_spec(spec)
        spec["references"]["key"]["path"] = ".env"
        with self.assertRaises(ValueError):
            self.m.validate_spec(spec)

    def test_preparacion_no_adopta_directorio_ajeno(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            output = parent / "runtime"
            output.mkdir()
            sentinel = output / "ajeno.txt"
            sentinel.write_bytes(b"conservar")
            info = parent.lstat()
            plan = {
                "root": str(output),
                "parent": {"device": info.st_dev, "inode": info.st_ino},
            }
            with patch.object(self.m, "prepare_data", return_value=(plan, {}, None, None)):
                with self.assertRaises(FileExistsError):
                    self.m.prepare(self.m.digest(self.m.canonical(plan)))
            self.assertEqual(sentinel.read_bytes(), b"conservar")
            self.assertEqual([p.name for p in output.iterdir()], ["ajeno.txt"])

    def test_relectura_distinta_conserva_parcial_y_no_emite_recibo_positivo(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            output = parent / "runtime"
            info = parent.lstat()
            plan = {
                "root": str(output),
                "parent": {"device": info.st_dev, "inode": info.st_ino},
            }
            reader = types.SimpleNamespace(read_regular=lambda *args: b"diferente")
            access = types.SimpleNamespace(check_access=lambda *args, **kwargs: None)
            with patch.object(
                self.m,
                "prepare_data",
                return_value=(plan, {"inventory.json": b"{}"}, reader, access),
            ):
                with self.assertRaisesRegex(ValueError, "Salida cambió"):
                    self.m.prepare(self.m.digest(self.m.canonical(plan)))
            self.assertTrue((output / ".owner.json").is_file())
            self.assertFalse((output / "receipt.json").exists())
            self.assertFalse((output / "inventory.json").exists())


if __name__ == "__main__":
    unittest.main()
