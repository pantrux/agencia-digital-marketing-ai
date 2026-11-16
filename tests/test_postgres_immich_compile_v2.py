"""Guardas de compilación y ELF con proveedores controlados, sin SDK o red."""

import importlib.util
import io
import json
import os
import re
import struct
import subprocess
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docker/postgres-immich"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def elf(*, needed=(), soname=None, runpath=None, defined=(), undefined=(), symtab=True):
    strings = bytearray(b"\0")

    def text(value):
        offset = len(strings)
        strings.extend(value.encode() + b"\0")
        return offset

    dynamic = [(1, text(name)) for name in needed]
    if soname:
        dynamic.append((14, text(soname)))
    if runpath:
        dynamic.append((29, text(runpath)))
    dynamic.append((0, 0))
    symbols = bytearray(24)
    for name, section in [(n, 1) for n in defined] + [(n, 0) for n in undefined]:
        symbols.extend(struct.pack("<IBBHQQ", text(name), 0x12, 0, section, 0, 0))
    payloads = [
        (3, bytes(strings), 0, 0),
        (6, b"".join(struct.pack("<qQ", *v) for v in dynamic), 1, 16),
        (11, bytes(symbols), 1, 24),
    ]
    if symtab:
        payloads.append((2, bytes(symbols), 1, 24))
    body = bytearray(64)
    sections = [b"\0" * 64]
    for kind, data, link, width in payloads:
        offset = len(body)
        body.extend(data)
        sections.append(
            struct.pack("<IIQQQQIIQQ", 0, kind, 0, 0, offset, len(data), link, 0, 1, width)
        )
    section_offset = len(body)
    body.extend(b"".join(sections))
    body[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH",
        body,
        16,
        3,
        62,
        1,
        0,
        0,
        section_offset,
        0,
        64,
        0,
        0,
        64,
        len(sections),
        0,
    )
    return bytes(body)


class CompileGuards(unittest.TestCase):
    def setUp(self):
        self.compile = load("compile_remote_v2")

    def test_elf_exige_sqlite_dinamico_y_no_c_embebido(self):
        sqlite = elf(soname="libsqlite3.so.0", defined=("sqlite3_libversion", "sqlite3_sourceid"))
        good = elf(
            needed=("libsqlite3.so.0", "libc.musl-x86_64.so.1"),
            runpath="$ORIGIN",
            undefined=("sqlite3_open_v2", "sqlite3_step"),
        )
        result = self.compile.sqlite_link_proof(good, sqlite)
        self.assertIn("sqlite3_step", result["vchord"]["sqlite_undefined"])
        for bad in (
            elf(
                needed=("libsqlite3.so.0",),
                runpath="$ORIGIN",
                defined=("sqlite3_step",),
                undefined=("sqlite3_open_v2",),
            ),
            elf(runpath="$ORIGIN", undefined=("sqlite3_step",)),
            elf(needed=("libsqlite3.so.0",), runpath="/tmp/ajeno", undefined=("sqlite3_step",)),
            elf(
                needed=("libsqlite3.so.0",),
                runpath="$ORIGIN",
                undefined=("sqlite3_step",),
                symtab=False,
            ),
        ):
            with self.assertRaises(ValueError):
                self.compile.sqlite_link_proof(bad, sqlite)

    def test_elf_rechaza_truncamiento_glibc_y_arquitectura_distinta(self):
        raw = elf(needed=("libsqlite3.so.0",), runpath="$ORIGIN", undefined=("sqlite3_step",))
        for bad in (raw[:60], raw[:40] + b"\xff" * 8 + raw[48:]):
            with self.assertRaises(ValueError):
                self.compile.elf_report(bad)
        changed = bytearray(raw)
        struct.pack_into("<H", changed, 18, 183)
        with self.assertRaises(ValueError):
            self.compile.elf_report(bytes(changed))
        sqlite = elf(soname="libsqlite3.so.0", defined=("sqlite3_sourceid",))
        with self.assertRaises(ValueError):
            self.compile.sqlite_link_proof(
                elf(
                    needed=("libsqlite3.so.0", "libc.so.6"),
                    runpath="$ORIGIN",
                    undefined=("sqlite3_step",),
                ),
                sqlite,
            )

    def test_probe_no_confunde_version_con_sourceid(self):
        self.compile.validate_sqlite_probe(
            {
                "version": "3.54.1",
                "number": 3054001,
                "source_id": self.compile.SQLITE_SOURCE_ID,
                "fts5": True,
                "threadsafe": True,
            }
        )
        for field, value in (("version", "3.51.1"), ("source_id", "otro"), ("fts5", False)):
            probe = {
                "version": "3.54.1",
                "number": 3054001,
                "source_id": self.compile.SQLITE_SOURCE_ID,
                "fts5": True,
                "threadsafe": True,
            }
            probe[field] = value
            with self.assertRaises(ValueError):
                self.compile.validate_sqlite_probe(probe)

    def test_controles_no_sustituyen_prueba_sql_y_exigen_version(self):
        self.assertEqual(
            self.compile.control_version(b"default_version = '0.8.5'\n", "0.8.5"), "0.8.5"
        )
        for invalid in (
            b"default_version = '0.8.4'\n",
            b"default_version = '0.8.5'\ndefault_version = '0.8.5'\n",
            b"sin version",
        ):
            with self.assertRaises(ValueError):
                self.compile.control_version(invalid, "0.8.5")

    def test_build_output_no_admite_bundle_o_busqueda_del_sistema(self):
        good = "cargo:rustc-link-search=native=/opt/agency-sqlite-v2/lib\ncargo:rustc-link-lib=sqlite3\n"
        self.compile.validate_sqlite_build_output(good)
        for bad in (
            "cargo:rustc-link-search=native=/usr/lib\ncargo:rustc-link-lib=sqlite3\n",
            "cargo:rustc-link-lib=static=sqlite3\n" + good,
            good + "cargo:rustc-link-search=native=/work/target/out\n",
        ):
            with self.assertRaises(ValueError):
                self.compile.validate_sqlite_build_output(bad)

    def test_features_wrapper_y_raw_no_se_intercambian(self):
        with self.assertRaises(ValueError):
            self.compile.validate_feature_wrapper(b'{"sha256":"otro","features":"bundled"}')
        features = "vchord v0.0.0 (/work/source)|default,pg16\npgrx v0.17.0|cshim,pg16\nsimd v0.0.0 (/work/source/crates/simd)|init\n"
        raw_sha = self.compile.digest(features.encode())
        proof = self.compile.canonical({"sha256": raw_sha, "features": features})
        with (
            mock.patch.object(self.compile, "FEATURE_WRAPPER_SHA", self.compile.digest(proof)),
            mock.patch.object(self.compile, "FEATURE_RAW_SHA", raw_sha),
        ):
            self.compile.validate_feature_wrapper(proof)
            with self.assertRaises(ValueError):
                self.compile.validate_feature_wrapper(proof + b"\n")

    def test_frame_fragmentado_y_eof_no_admiten_peticion_parcial(self):
        class Fragmented:
            data = b"abcd"

            def read(self, _size):
                result, self.data = self.data[:1], self.data[1:]
                return result

        self.assertEqual(self.compile.exact_read(Fragmented(), 4), b"abcd")
        with self.assertRaises(ValueError):
            self.compile.exact_read(Fragmented(), 5)

    def test_timeout_termina_solo_cid_y_reserva_limpieza(self):
        normal = load("offline_sdk_remote_v4")
        spec = importlib.util.spec_from_file_location(
            "compile_fixture", ROOT / "tests/test_postgres_immich_sdk_controller.py"
        )
        helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helpers)
        outer = self

        class Docker:
            running = True
            calls = []

            def inspect(self, cid, timeout=2):
                value = helpers.container(normal, self.running)
                value["Id"] = outer.compile.CID
                return value

            def limits(self, observed):
                return {"verified": True, "pid": observed["pid"]}

            def command(self, arguments, timeout, sink=None):
                self.calls.append((arguments, timeout))
                if arguments[0] == "exec":
                    raise TimeoutError("Plazo simulado")
                self.running = False
                return {"returncode": 0, "output": b"", "process": {"returncode": 0}}

        docker = Docker()
        with self.assertRaises(normal.StageError):
            self.compile.execute_compile(
                docker,
                normal,
                {"profile_sha256": "a" * 64, "sources": {"remote": "b" * 64}},
                io.BytesIO(),
                25,
            )
        self.assertFalse(docker.running)
        self.assertLessEqual(docker.calls[0][1], 5)
        self.assertEqual(docker.calls[-1][0][-1], self.compile.CID)

    def test_perfil_exige_jobs2_y_rechaza_desviacion(self):
        good = self.compile.settings()
        self.compile.validate_settings(good)
        for field, value in (
            ("jobs", 4),
            ("timeout_seconds", 1801),
            ("sqlite_dynamic", False),
            ("runpath", "/tmp"),
        ):
            bad = {**good, field: value}
            with self.assertRaises(ValueError):
                self.compile.validate_settings(bad)

    def test_reinicio_respaldado_es_obligatorio_y_no_adopta_start_anterior(self):
        original = {"created": "2026-11-06T10:56:21Z", "started_at": "2026-11-06T10:57:01Z"}
        proof = {
            "verified": True,
            "sdk_executed": False,
            "compiler_executed": False,
            "apk_count": 110,
        }
        receipt = {
            "stage": "restart",
            "success": True,
            "cid": self.compile.CID,
            "profile_sha256": self.compile.RESUME_PROFILE_SHA,
            "source_sha256": self.compile.RESUME_SOURCE_SHA,
            "failed_receipt_sha256": self.compile.FAILED_V1_SHA,
            "sdk_executed": False,
            "proof": proof,
            "proof_sha256": self.compile.digest(self.compile.canonical(proof)),
            "container": {
                "cid": self.compile.CID,
                "created": original["created"],
                "started_at": "2026-11-06T12:01:00Z",
                "pid": 40000,
                "running": True,
            },
        }

        def envelope(value):
            return {
                "profile_sha256": self.compile.RESUME_PROFILE_SHA,
                "restart": {
                    "receipt": value,
                    "receipt_sha256": self.compile.digest(self.compile.canonical(value)),
                },
            }

        self.compile.validate_resume(envelope(receipt), original)
        for changed in (
            {**receipt, "success": False},
            {**receipt, "source_sha256": "b" * 64},
            {
                **receipt,
                "container": {**receipt["container"], "started_at": original["started_at"]},
            },
        ):
            with self.assertRaises(ValueError):
                self.compile.validate_resume(envelope(changed), original)
        with self.assertRaises(ValueError):
            self.compile.validate_resume({}, original)

    def test_profile_heredado_se_elimina_antes_de_make_sin_ejecutar_sdk(self):
        bash = r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else "/bin/bash"

        def check(filename):
            # Ejecuta sólo las declaraciones de entorno que afectan PROFILE,
            # nunca el cuerpo de la receta ni make/Cargo/compiladores.
            lines = [
                line
                for line in (SOURCE / filename).read_text().splitlines()
                if line.startswith(("export ", "unset "))
                and re.search(r"\bPROFILE(?:=|\s|$)", line)
            ]
            return subprocess.run(
                [
                    bash,
                    "-c",
                    "export PROFILE=release\n" + "\n".join(lines) + "\n[[ ! -v PROFILE ]]",
                ],
                check=False,
                capture_output=True,
                timeout=5,
            ).returncode

        self.assertNotEqual(check("compile-recipe.sh"), 0)
        self.assertEqual(check("compile-recipe-v2.sh"), 0)

    def test_plan_local_no_abre_ssh_y_apply_exige_respaldo(self):
        local = load("compile_local_v2")
        with (
            mock.patch.object(local, "build_profile", return_value={"sources": {}, "settings": {}}),
            mock.patch.object(local, "connect") as connect,
            mock.patch("sys.stdout", new=io.StringIO()) as output,
        ):
            self.assertEqual(local.main([]), 0)
        connect.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        with (
            mock.patch.object(local, "connect") as connect,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(local.main(["apply"]), 1)
        connect.assert_not_called()

    def test_recibo_invalido_despues_de_exec_detiene_cid_sin_escribir_boveda(self):
        local = load("compile_local_v2")
        profile = {"sources": {"remote": "a" * 64}, "sdk_plan": {}}
        writer = mock.Mock()
        sdk = types.SimpleNamespace(
            read_source=lambda _path: self.compile.canonical(profile),
            contract=lambda: types.SimpleNamespace(json_object=json.loads),
            read_values=lambda: {},
            root_writer=writer,
        )
        with (
            mock.patch.object(local, "sdk_local", return_value=sdk),
            mock.patch.object(local, "verify_pins"),
            mock.patch.object(local, "connect", return_value=mock.Mock()),
            mock.patch.object(
                local,
                "request",
                side_effect=[{"success": True, "result": {}}, {"success": True}],
            ) as request,
            mock.patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(
                local.main(
                    [
                        "apply",
                        "--profile",
                        "perfil-controlado.json",
                        "--expected-profile-sha256",
                        "b" * 64,
                        "--root-update-vault",
                    ]
                ),
                1,
            )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs, {"action": "stop"})
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
