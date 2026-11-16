"""Regresiones del derivado Cargo: sólo datos, sin SDK, red ni subprocesos."""

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "docker/postgres-immich/prepare_security_vendor.py"
SPEC = importlib.util.spec_from_file_location("security_vendor", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def tar_bytes(rows):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data, kind in rows:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


class Access:
    def __init__(self):
        self.refuse = False

    def check(self, path):
        if self.refuse:
            raise ValueError("ACL ajena")

    def mkdir(self, path):
        path.mkdir(mode=0o700)


class SecurityVendorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "private"
        self.root.mkdir()
        self.access = Access()
        self.changes = []
        self.crates = []
        self.original_rows = []
        self.candidate_inputs = []
        blocks = []
        for name, old, new in [
            ("anyhow", "1.0.101", "1.0.103"),
            ("crossbeam-epoch", "0.9.18", "0.9.20"),
            ("unchanged", "1.0.0", None),
        ]:
            old_files = {
                "Cargo.toml": f'[package]\nname="{name}"\nversion="{old}"\n'.encode(),
                "src/lib.rs": b"// original\n",
            }
            checksum = digest((name + old).encode())
            self.crates.append(
                {
                    "name": name,
                    "version": old,
                    "archive_sha256": checksum,
                    "files": {k: digest(v) for k, v in old_files.items()},
                }
            )
            self.original_rows.extend(
                (f"{name}-{old}/{k}", v, tarfile.REGTYPE) for k, v in old_files.items()
            )
            self.original_rows.append(
                (
                    f"{name}-{old}/.cargo-checksum.json",
                    encoded({"files": self.crates[-1]["files"], "package": checksum}),
                    tarfile.REGTYPE,
                )
            )
            blocks.append(
                f'[[package]]\nname = "{name}"\nversion = "{old}"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "{checksum}"\n# conservar comentario\n'
            )
            if new:
                files = {
                    "Cargo.toml": f'[package]\nname="{name}"\nversion="{new}"\nlicense="MIT OR Apache-2.0"\n'.encode(),
                    "src/lib.rs": b"// corregido\n",
                    "build.rs": b"fn main() {}\n",
                    "LICENSE-MIT": b"MIT\n",
                    "LICENSE-APACHE": b"Apache\n",
                }
                archive = tar_bytes(
                    [(f"{name}-{new}/{k}", v, tarfile.REGTYPE) for k, v in files.items()]
                )
                relative = f"private/{name}.crate"
                (self.base / relative).write_bytes(archive)
                change = {
                    "name": name,
                    "old_version": old,
                    "old_checksum": checksum,
                    "version": new,
                    "checksum": digest(archive),
                    "path": relative,
                    "build_script_sha256": digest(files["build.rs"]),
                }
                self.changes.append(change)
        self.lock = ("# encabezado intacto\nversion = 4\n\n" + "\n".join(blocks)).encode()
        self.vendor = tar_bytes(self.original_rows)

    def bundle(self, vendor=None):
        candidates = {row["name"]: (self.base / row["path"]).read_bytes() for row in self.changes}
        return MODULE.derive(
            self.lock,
            vendor or self.vendor,
            {"crate_count": 3, "crates": self.crates},
            self.changes,
            candidates,
        )

    def test_lock_solo_dos_bloques_y_comentarios(self):
        new = MODULE.patch_lock(self.lock, self.changes)
        expected = self.lock
        for row in self.changes:
            expected = expected.replace(
                f'version = "{row["old_version"]}"'.encode(),
                f'version = "{row["version"]}"'.encode(),
            ).replace(row["old_checksum"].encode(), row["checksum"].encode())
        self.assertEqual(new, expected)

    def test_lock_duplicado_o_checksum_distinto_se_rechaza(self):
        for value in [
            self.lock
            + self.lock[
                self.lock.index(b"[[package]]") : self.lock.index(
                    b"[[package]]", self.lock.index(b"[[package]]") + 1
                )
            ],
            self.lock.replace(self.changes[0]["old_checksum"].encode(), b"0" * 64),
        ]:
            with self.subTest(value=value[:30]), self.assertRaises(ValueError):
                MODULE.patch_lock(value, self.changes)

    def test_vendor_conserva_tercero_y_licencias(self):
        result = self.bundle()
        output = io.BytesIO()
        MODULE.emit_vendor(result, output)
        with tarfile.open(fileobj=io.BytesIO(output.getvalue())) as archive:
            names = archive.getnames()
            self.assertIn("anyhow-1.0.103/LICENSE-MIT", names)
            self.assertNotIn("anyhow-1.0.101/src/lib.rs", names)
            self.assertEqual(
                archive.extractfile("unchanged-1.0.0/src/lib.rs").read(), b"// original\n"
            )
            for row in self.changes:
                prefix = row["name"] + "-" + row["version"]
                checksum = json.loads(archive.extractfile(prefix + "/.cargo-checksum.json").read())
                self.assertEqual(checksum["package"], row["checksum"])
                self.assertEqual(checksum["files"]["build.rs"], row["build_script_sha256"])
        self.assertEqual(result["crate_count"], 3)

    def test_vendor_determinista_y_no_ejecuta_procesos(self):
        with (
            patch("subprocess.run", side_effect=AssertionError("subproceso")),
            patch("socket.socket", side_effect=AssertionError("red")),
        ):
            bundle = self.bundle()
            a, b = io.BytesIO(), io.BytesIO()
            MODULE.emit_vendor(bundle, a)
            MODULE.emit_vendor(bundle, b)
            self.assertEqual(a.getvalue(), b.getvalue())

    def test_vendor_rechaza_escapes_duplicados_enlaces_y_casefold(self):
        for extra in [
            ("../fuera", b"x", tarfile.REGTYPE),
            ("unchanged-1.0.0/src/lib.rs", b"x", tarfile.REGTYPE),
            ("unchanged-1.0.0/LINK", b"", tarfile.SYMTYPE),
            ("unchanged-1.0.0/SRC/LIB.RS", b"x", tarfile.REGTYPE),
        ]:
            with self.subTest(extra=extra[0]), self.assertRaises(ValueError):
                self.bundle(tar_bytes([*self.original_rows, extra]))

    def test_vendor_rechaza_bytes_alterados_y_checksum_cargo(self):
        for index in [0, 2]:
            rows = list(self.original_rows)
            name, data, kind = rows[index]
            rows[index] = name, data + b"alterado", kind
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.bundle(tar_bytes(rows))

    def test_candidato_alterado_no_pasa_por_checksum(self):
        path = self.base / self.changes[0]["path"]
        path.write_bytes(path.read_bytes() + b"x")
        with self.assertRaises(ValueError):
            self.bundle()

    def test_candidato_sin_licencia_no_se_adopta(self):
        row = self.changes[0]
        raw = tar_bytes(
            [
                (
                    f"{row['name']}-{row['version']}/Cargo.toml",
                    f'[package]\nname="{row["name"]}"\nversion="{row["version"]}"\nlicense="MIT OR Apache-2.0"\n'.encode(),
                    tarfile.REGTYPE,
                ),
                (f"{row['name']}-{row['version']}/build.rs", b"fn main() {}\n", tarfile.REGTYPE),
            ]
        )
        (self.base / row["path"]).write_bytes(raw)
        row["checksum"] = digest(raw)
        with self.assertRaises(ValueError):
            self.bundle()

    def test_ruta_de_input_no_admite_raiz_ni_parent(self):
        for value in ["../outside", "C:/fuera", "/tmp/fuera", "a\\b", "a//b"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.resolve_input(self.base, value)

    def test_lectura_rechaza_hardlink_real(self):
        source = self.base / "original.txt"
        alias = self.base / "alias.txt"
        source.write_bytes(b"datos")
        os.link(source, alias)
        with self.assertRaises(ValueError):
            MODULE.regular(alias, 1024)
        self.assertEqual(source.read_bytes(), b"datos")

    def test_helper_ejecuta_bytes_verificados_sin_cache_pyc(self):
        path = self.base / "access.py"
        raw = b"class NativeAccess:\n    verified = True\n"
        path.write_bytes(raw)
        config = {"helpers": [{"path": "access.py", "sha256": digest(raw)}]}
        with (
            patch.object(MODULE, "BASE", self.base),
            patch(
                "importlib.util.spec_from_file_location",
                side_effect=AssertionError("No cargar pyc"),
            ),
        ):
            self.assertTrue(MODULE.native_access(config).verified)

    def test_publicacion_requiere_plan_externo_y_no_sobrescribe(self):
        outputs = {"Cargo.lock": b"a"}
        plan = {"output_hashes": {"Cargo.lock": digest(b"a")}, "owner": "fixture"}
        destination = self.root / "derived-v1"
        with self.assertRaises(ValueError):
            MODULE.publish(destination, outputs, plan, "0" * 64, self.access)
        self.assertFalse(destination.exists())
        MODULE.publish(destination, outputs, plan, digest(encoded(plan)), self.access)
        with self.assertRaises(ValueError):
            MODULE.publish(destination, outputs, plan, digest(encoded(plan)), self.access)
        self.assertEqual((destination / "Cargo.lock").read_bytes(), b"a")

    def test_acl_y_output_alterado_fallan_antes_de_crear(self):
        outputs = {"Cargo.lock": b"a"}
        plan = {"output_hashes": {"Cargo.lock": digest(b"a")}, "owner": "fixture"}
        for corrupt in [False, True]:
            self.access.refuse = not corrupt
            with self.subTest(corrupt=corrupt), self.assertRaises(ValueError):
                MODULE.publish(
                    self.root / "derived-v1",
                    {"Cargo.lock": b"b"} if corrupt else outputs,
                    plan,
                    digest(encoded(plan)),
                    self.access,
                )
            self.assertFalse((self.root / "derived-v1").exists())

    def test_preparar_no_modifica_los_bytes_originales(self):
        before = copy.deepcopy((self.lock, self.vendor, self.crates, self.changes))
        self.bundle()
        self.assertEqual(before, (self.lock, self.vendor, self.crates, self.changes))

    def test_plan_predeterminado_no_publica_ni_crea_derivado(self):
        config = {"schema_version": 1, "changes": self.changes, "security_root": "private"}
        plan = {"owner": "fixture", "output_hashes": {"Cargo.lock": digest(b"a")}}
        original_regular = MODULE.regular

        def fake_regular(path, maximum):
            if Path(path).name == "security-vendor-inputs.json":
                return encoded(config)
            return original_regular(path, maximum)

        before = set(self.base.rglob("*"))
        with (
            patch.object(MODULE, "regular", side_effect=fake_regular),
            patch.object(MODULE, "native_access", return_value=self.access),
            patch.object(
                MODULE, "prepare", return_value=(plan, {"Cargo.lock": b"a"}, lambda: None)
            ),
            patch.object(MODULE, "publish", side_effect=AssertionError("Publicación en plan")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(MODULE.main([]), 0)
        self.assertFalse(json.loads(output.getvalue())["derived_created"])
        self.assertEqual(before, set(self.base.rglob("*")))

    def test_lock_no_acepta_otra_fuente_registro(self):
        with self.assertRaises(ValueError):
            MODULE.patch_lock(
                self.lock.replace(
                    b"registry+https://github.com/rust-lang/crates.io-index",
                    b"registry+https://example.invalid/index",
                ),
                self.changes,
            )

    def test_vendor_omision_y_crate_extra_son_rechazados(self):
        cases = [
            self.original_rows[1:],
            [*self.original_rows, ("extra-1.0.0/src/lib.rs", b"x", tarfile.REGTYPE)],
        ]
        for rows in cases:
            with self.subTest(count=len(rows)), self.assertRaises(ValueError):
                self.bundle(tar_bytes(rows))

    def test_limpieza_no_borra_salidas_si_falla_verificacion_final(self):
        plan = {"owner": "fixture", "output_hashes": {"Cargo.lock": digest(b"a")}}
        destination = self.root / "derived-v1"

        def fail():
            raise ValueError("Entrada original cambió")

        with self.assertRaises(ValueError):
            MODULE.publish(
                destination, {"Cargo.lock": b"a"}, plan, digest(encoded(plan)), self.access, fail
            )
        self.assertEqual((destination / "Cargo.lock").read_bytes(), b"a")
        self.assertTrue((destination / ".owner.json").exists())
        self.assertFalse((destination / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
