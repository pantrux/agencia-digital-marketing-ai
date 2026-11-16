"""Contratos puros: no importa Flask ni ejecuta Edge/Node/contenedores."""

import hashlib
import io
import json
import os
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "docker/kitsu-zou/accept_flasgger_ui.py"


def module():
    result = types.ModuleType("flasgger_acceptance_test")
    result.__file__ = str(PATH)
    exec(compile(PATH.read_bytes(), str(PATH), "exec"), vars(result))
    return result


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.m = module()

    def test_lock_cierra_hashes_y_rechaza_duplicados_urls_y_sin_pin(self):
        raw = b"flask==3.1.3 --hash=sha256:" + b"a" * 64 + b"\n"
        self.assertEqual(self.m.parse_lock(raw)["flask"]["version"], "3.1.3")
        for changed in (raw + raw, b"flask>=3", b"flask @ https://example.com"):
            with self.assertRaises(ValueError):
                self.m.parse_lock(changed)

    def test_markers_exactos_linux_python312_sin_extras(self):
        for marker in ('python_version < "3.13"', "python_version >= '3.10'"):
            self.assertTrue(self.m.active_marker(marker))
        for marker in (
            'python_version < "3.11"',
            'extra == "format"',
            'platform_system == "Windows"',
        ):
            self.assertFalse(self.m.active_marker(marker))
        with self.assertRaises(ValueError):
            self.m.active_marker("unknown == 'x'")

    def test_clausura_no_omite_transitiva_y_no_acepta_version_incompatible(self):
        candidates = {
            "flasgger": {"version": "0.9.7.1", "requires": ["Flask>=0.10"]},
            "flask": {"version": "3.1.3", "requires": ["Jinja2>=3.1.2"]},
            "jinja2": {"version": "3.1.6", "requires": []},
        }
        self.assertEqual(set(self.m.close_metadata(candidates)), set(candidates))
        with self.assertRaises(ValueError):
            self.m.close_metadata({k: v for k, v in candidates.items() if k != "jinja2"})
        candidates["jinja2"]["version"] = "2.0"
        with self.assertRaises(ValueError):
            self.m.close_metadata(candidates)

    def test_requires_python_no_omite_exclusiones_o_syntax_no_soportada(self):
        self.m.python_compatible(">=2.7, !=3.0.*, !=3.1.*, !=3.2.*")
        self.m.python_compatible(">=3.9,<3.13")
        for requirement in (">=3.13", "!=3.12.*", "~=3.12"):
            with self.assertRaises(ValueError):
                self.m.python_compatible(requirement)

    def test_wheel_no_se_extrae_por_path_y_rechaza_pth_o_duplicados(self):
        for name in ("../escape", "folder/code.pth"):
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(name, b"data")
            with self.assertRaises(ValueError):
                self.m.wheel_files(archive.getvalue())

    def test_directorios_zip_vacios_legitimos_no_se_tratan_como_payload_record(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.mkdir("package/")
            output.writestr("package/data", b"data")
        self.assertEqual(self.m.wheel_files(archive.getvalue()), {"package/data": b"data"})

    def test_fuente_callback_fijada_no_admite_fragmento_parecido(self):
        fixture = (
            b"before\n"
            + self.m.CALLBACK_FRAGMENT
            + b"    const ui = SwaggerUIBundle(uiOptions);\nafter"
        )
        self.assertEqual(self.m.callback_fragment(fixture), self.m.CALLBACK_FRAGMENT)
        with self.assertRaises(ValueError):
            self.m.callback_fragment(
                fixture.replace(b"priorOnComplete.apply(this, arguments)", b"priorOnComplete()")
            )

    def test_specs_comparados_exactos_sin_ignorar_campos(self):
        base = {"swagger": "2.0", "paths": {"/fixture/echo": {"get": {"operationId": "echo"}}}}
        self.m.same_specs(base, json.loads(json.dumps(base)))
        changed = json.loads(json.dumps(base))
        changed["paths"]["/fixture/echo"]["get"]["operationId"] = "other"
        with self.assertRaises(ValueError):
            self.m.same_specs(base, changed)

    def test_publicacion_excl_conserva_ajeno_y_falla_hash_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "existing"
            target.mkdir()
            (target / "foreign").write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                self.m.create_private(target, self.m.OWNER)
            self.assertEqual((target / "foreign").read_bytes(), b"keep")
        with (
            patch.object(self.m, "make_plan", return_value={"fixed": True}),
            patch.object(self.m, "prepare_local", side_effect=AssertionError("NO_WRITE")),
        ):
            with self.assertRaises(ValueError):
                self.m.execute_prepare("0" * 64)

    def test_plan_predeterminado_no_ejecuta_operaciones(self):
        with (
            patch.object(self.m, "make_plan", return_value={"fixed": True}),
            patch.object(self.m, "execute_prepare", side_effect=AssertionError("NO_OPERATION")),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(self.m.main([]), 0)
            self.assertIn("plan_sha256", json.loads(output.getvalue()))

    def test_snapshot_replay_cerrado_sin_destinos_arbitrarios(self):
        row = {
            "path": "/fixture/echo",
            "status": 200,
            "content_type": "application/json",
            "body_base64": "e30=",
            "sha256": hashlib.sha256(b"{}").hexdigest(),
        }
        self.assertEqual(self.m.replay_entry(row)["body_base64"], "e30=")
        for path in ("//external.example", "/../secret", "https://example.com", "/safe?query=1"):
            with self.assertRaises(ValueError):
                self.m.replay_entry({**row, "path": path})

    def test_lector_real_cierra_bytes_y_rechaza_hardlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "input"
            source.write_bytes(b"data")
            self.assertEqual(
                self.m.read_file(source, 4, hashlib.sha256(b"data").hexdigest()), b"data"
            )
            with self.assertRaises(ValueError):
                self.m.read_file(source, 3)
            other = Path(temporary) / "alias"
            os.link(source, other)
            with self.assertRaises(ValueError):
                self.m.read_file(source, 4)

    def test_json_duplicado_no_finito_o_utf8_invalido_no_se_acepta(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":"\xff"}'):
            with self.assertRaises((ValueError, UnicodeError)):
                self.m.decode(raw)


if __name__ == "__main__":
    unittest.main()
