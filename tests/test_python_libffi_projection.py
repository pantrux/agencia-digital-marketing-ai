"""Proyección como datos; no construye ni ejecuta una imagen o ELF."""

import copy
import hashlib
import io
import tarfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    path = ROOT / "docker/python-runtime/project_libffi_context.py"
    module = types.ModuleType("projection_tests")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), vars(module))
    return module


def rows():
    # Cuerpos sintéticos: prueban conservación, no identidad ni ABI de libffi.
    output = []
    for name, kind, body, target in (
        ("rootfs/usr/lib", tarfile.DIRTYPE, None, ""),
        ("rootfs/usr/lib64", tarfile.DIRTYPE, None, ""),
        ("rootfs/usr/lib64/libffi.so.8.5.0", tarfile.REGTYPE, b"fixture-library", ""),
        ("rootfs/usr/lib64/libffi.so", tarfile.SYMTYPE, None, "libffi.so.8.5.0"),
        ("rootfs/usr/lib64/libffi.so.8", tarfile.SYMTYPE, None, "libffi.so.8.5.0"),
        ("rootfs/usr/unchanged", tarfile.REGTYPE, b"sentinel", ""),
    ):
        node = tarfile.TarInfo(name)
        node.type, node.mode, node.mtime = kind, 0o777 if target else 0o755, 1791622591
        node.linkname, node.size = target, len(body) if body is not None else 0
        output.append((name, node, body))
    for name in ("clear", "reset", "lzcat", "lzma", "unlzma", "unxz", "xzcat", "tset", "xz"):
        target = "tset" if name == "reset" else "xz" if name not in ("clear", "tset", "xz") else ""
        node = tarfile.TarInfo("rootfs/usr/bin/" + name)
        node.type, node.mode = (tarfile.SYMTYPE, 0o777) if target else (tarfile.REGTYPE, 0o755)
        body = None if target else b"C" * 17208 if name == "clear" else b"retained-tool"
        node.linkname, node.size = target, len(body) if body is not None else 0
        output.append((node.name, node, body))
    return output


def inventory(module):
    files = {
        "usr/lib64": {"kind": "dir", "mode": 0o755, "uid": 0, "gid": 0},
        "usr/lib64/libffi.so.8.5.0": {
            "kind": "file",
            "mode": 0o755,
            "uid": 0,
            "gid": 0,
            "bytes": 76584,
            "sha256": module.LIBFFI_SHA256,
            "packages": ["libffi"],
        },
        "unrelated": {"kind": "file", "sha256": "a" * 64},
    }
    for name in ("libffi.so", "libffi.so.8"):
        files["usr/lib64/" + name] = {
            "kind": "symlink",
            "mode": 0o777,
            "uid": 0,
            "gid": 0,
            "target": "libffi.so.8.5.0",
            "packages": ["libffi"],
        }
    files["usr/bin/clear"] = {
        "kind": "file",
        "mode": 0o755,
        "uid": 0,
        "gid": 0,
        "bytes": 17208,
        "sha256": module.CLEAR_SHA256,
        "packages": ["ncurses"],
    }
    for name in ("reset", "lzcat", "lzma", "unlzma", "unxz", "xzcat"):
        files["usr/bin/" + name] = {
            "kind": "symlink",
            "mode": 0o777,
            "uid": 0,
            "gid": 0,
            "target": "tset" if name == "reset" else "xz",
            "packages": ["ncurses"] if name == "reset" else ["xz"],
        }
    return {
        "files": files,
        "elf": {
            "usr/bin/clear": {"sha256": module.CLEAR_SHA256, "bytes": 17208},
            "usr/lib64/libffi.so.8.5.0": {
                "sha256": module.LIBFFI_SHA256,
                "bytes": 76584,
                "soname": "libffi.so.8",
                "needed": ["libc.so.6"],
                "pt_interp": None,
            },
        },
        "manual_materials": [{"name": "libffi", "payload_complete": True}],
        "license_evidence": {"declaration": "MIT", "raw_path": "usr/lib64/libffi.so"},
        "load_paths": ["/usr/lib64", "/usr/lib"],
        "runtime_approved": False,
    }


class Projection(unittest.TestCase):
    def test_solo_tres_rutas_y_un_directorio_cambian_sin_mutar_original(self):
        module, original = load(), rows()
        before = [(name, copy.deepcopy(node.get_info()), body) for name, node, body in original]
        changed = module._project_rows(original)
        self.assertEqual(len(changed), len(original) - 8)
        expected = {"rootfs/" + old: "rootfs/" + new for old, new in module.MAPPING.items()}
        for name, node, body in original:
            if name == "rootfs/usr/lib64" or name.removeprefix("rootfs/") in module.OMITTED:
                continue
            target = expected.get(name, name)
            projected = next(row for row in changed if row[0] == target)
            self.assertEqual(projected[2], body)
            for attribute in ("type", "mode", "uid", "gid", "mtime", "linkname", "size"):
                self.assertEqual(getattr(projected[1], attribute), getattr(node, attribute))
        self.assertEqual(before, [(n, node.get_info(), body) for n, node, body in original])

    def test_extra_faltante_colision_y_directorio_no_adoptados(self):
        module = load()
        for change in ("extra", "missing", "collision", "directory"):
            value = rows()
            if change == "extra":
                node = tarfile.TarInfo("rootfs/usr/lib64/foreign")
                value.append((node.name, node, b""))
            elif change == "missing":
                value.pop(3)
            elif change == "collision":
                node = tarfile.TarInfo("rootfs/usr/lib/libffi.so")
                value.append((node.name, node, b""))
            else:
                value[1][1].type = tarfile.SYMTYPE
            with self.subTest(change=change), self.assertRaises(ValueError):
                module._project_rows(value)

    def test_pax_proyectado_se_relee_con_nombres_fisicos_sin_alias_antiguo(self):
        module, value = load(), rows()
        value[4][1].pax_headers = {"path": value[4][0], "mtime": "1791622591.125"}
        changed = module._project_rows(value)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:", format=tarfile.PAX_FORMAT) as archive:
            for _name, node, body in changed:
                archive.addfile(node, io.BytesIO(body) if body is not None else None)
        with tarfile.open(fileobj=io.BytesIO(stream.getvalue()), mode="r:") as archive:
            members = {node.name: node for node in archive}
        self.assertNotIn("rootfs/usr/lib64/libffi.so.8", members)
        node = members["rootfs/usr/lib/libffi.so.8"]
        self.assertEqual(node.linkname, "libffi.so.8.5.0")
        self.assertEqual(node.pax_headers["path"], node.name)
        self.assertEqual(node.pax_headers["mtime"], "1791622591.125")
        self.assertEqual(value[4][1].pax_headers["path"], value[4][0])

    def test_inventario_mueve_solo_claves_fisicas_y_conserva_procedencia(self):
        module = load()
        value = inventory(module)
        before = copy.deepcopy(value)
        changed = module._project_inventory(value)
        self.assertEqual(value, before)
        self.assertNotIn("usr/lib64", changed["files"])
        for old, new in module.MAPPING.items():
            self.assertNotIn(old, changed["files"])
            self.assertEqual(changed["files"][new], before["files"][old])
        self.assertEqual(
            changed["elf"]["usr/lib/libffi.so.8.5.0"], before["elf"]["usr/lib64/libffi.so.8.5.0"]
        )
        for name in set(before) - {"files", "elf"}:
            self.assertEqual(changed[name], before[name])
        self.assertEqual(changed["physical_projection"]["mapping"], module.MAPPING)
        self.assertFalse(changed["physical_projection"]["runtime_approved"])
        self.assertEqual(
            set(changed["physical_projection"]["not_projected_manual_contributions"]),
            set(module.OMITTED),
        )
        self.assertNotIn("usr/bin/clear", changed["elf"])
        self.assertFalse(changed["physical_projection"]["complete_manual_payload_copied"])

    def test_inventario_rechaza_elf_distinto_y_destinos_ocupados(self):
        module = load()
        for change in ("elf", "collision", "target", "extra"):
            value = inventory(module)
            if change == "elf":
                value["elf"]["usr/lib64/libffi.so.8.5.0"]["sha256"] = "a" * 64
            elif change == "collision":
                value["files"]["usr/lib/libffi.so"] = {"kind": "file"}
            elif change == "target":
                value["files"]["usr/lib64/libffi.so"]["target"] = "../../foreign"
            else:
                value["files"]["usr/lib64/extra"] = {"kind": "file"}
            with self.subTest(change=change), self.assertRaises(ValueError):
                module._project_inventory(value)

    def test_pines_publicos_fallan_antes_de_cargar_codigo_ajeno(self):
        module = load()
        for context, source in (
            (b"arbitrary", b"raise AssertionError('no ejecutar')"),
            (bytearray(), b""),
        ):
            with self.subTest(type=type(context).__name__), self.assertRaises(ValueError):
                module.derive_context(context, source)
        with self.assertRaises(ValueError):
            module.derive_physical_inventory(b"{}", b"raise AssertionError('no ejecutar')")

    def test_core_real_resuelve_alias_a_la_biblioteca_fisica_proyectada(self):
        module = load()
        source = (ROOT / "docker/python-runtime/runtime_image_inventory_v3.py").read_bytes()
        core = module.load_core(source)
        files = {
            "usr/lib64": {"kind": "symlink", "target": "lib"},
            "usr/lib/libffi.so.8": {"kind": "symlink", "target": "libffi.so.8.5.0"},
            "usr/lib/libffi.so.8.5.0": {"kind": "file", "sha256": module.LIBFFI_SHA256},
        }
        self.assertEqual(core.follow(files, "usr/lib64/libffi.so.8"), "usr/lib/libffi.so.8.5.0")
        self.assertEqual(hashlib.sha256(source).hexdigest(), module.CORE_SHA256)

    def test_base_de_otra_identidad_se_rechaza_sin_parsear_payload(self):
        module = load()
        with self.assertRaises(ValueError):
            module.check_base(b"not-oci", b"raise AssertionError('no ejecutar')", b"")

    def test_omite_solo_siete_cli_y_conserva_tset_xz_y_otro_payload(self):
        module = load()
        self.assertEqual(
            set(module.OMITTED),
            {
                "usr/bin/" + n
                for n in ("clear", "reset", "lzcat", "lzma", "unlzma", "unxz", "xzcat")
            },
        )
        result = {name: body for name, _, body in module._project_rows(rows())}
        self.assertIn("rootfs/usr/bin/tset", result)
        self.assertIn("rootfs/usr/bin/xz", result)
        self.assertIn("rootfs/usr/unchanged", result)
        self.assertTrue(all("rootfs/" + n not in result for n in module.OMITTED))

    def test_cli_faltante_target_cambiado_hash_o_omision_extra_rechazados(self):
        module = load()
        for change in ("missing", "target"):
            value = rows()
            i = next(i for i, (name, _, _) in enumerate(value) if name == "rootfs/usr/bin/reset")
            if change == "missing":
                value.pop(i)
            else:
                value[i][1].linkname = "../../foreign"
            with self.subTest(change=change), self.assertRaises(ValueError):
                module._project_rows(value)
        value = inventory(module)
        value["files"]["usr/bin/clear"]["sha256"] = "a" * 64
        with self.assertRaises(ValueError):
            module._project_inventory(value)


if __name__ == "__main__":
    unittest.main()
