"""Fronteras del build SRT: fixtures, sin SDK, instalación ni fuentes ejecutadas."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "docker/supoclip" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load("srt_build_tests", "build_srt_wheel.py")
BACKEND = load("srt_backend_tests", "srt_build_backend.py")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def source():
    return {
        "setup.py": b"from setuptools import setup\nsetup(name='srt',version='3.5.3')\n",
        "srt.py": b"VERSION='fixture'\n",
        "srt_tools/utils.py": b"# fixture\n",
        "srt_tools/__init__.py": b"",
        "LICENSE": b"MIT fixture\n",
        **{
            "srt_tools/" + name: b"#!/usr/bin/env python\n# fixture\n"
            for name in [
                "srt",
                "srt-deduplicate",
                "srt-normalise",
                "srt-fixed-timeshift",
                "srt-linear-timeshift",
                "srt-lines-matching",
                "srt-mux",
                "srt-play",
                "srt-process",
            ]
        },
    }


def catalogue(files):
    return [
        {"path": name, "size": len(raw), "sha256": digest(raw)}
        for name, raw in sorted(files.items())
    ]


def archive(entries):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as package:
        for name, body, kind in entries:
            entry = tarfile.TarInfo("srt-3.5.3/" + name)
            entry.type = kind
            entry.size = len(body) if kind == tarfile.REGTYPE else 0
            package.addfile(entry, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
    return raw.getvalue()


def wheel(files, *, change=None):
    prefix = "srt-3.5.3.dist-info/"
    data = {
        "srt.py": files["srt.py"],
        "srt_tools/utils.py": files["srt_tools/utils.py"],
        "srt_tools/__init__.py": files["srt_tools/__init__.py"],
        prefix + "licenses/LICENSE": files["LICENSE"],
        prefix
        + "METADATA": b"Metadata-Version: 2.4\nName: srt\nVersion: 3.5.3\nRequires-Python: >=2.7\nLicense-Expression: MIT\n\n",
        prefix + "WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        prefix + "top_level.txt": b"srt\nsrt_tools\n",
    }
    for name, raw in files.items():
        if name.startswith("srt_tools/") and not name.endswith(".py"):
            data["srt-3.5.3.data/scripts/" + name.rsplit("/", 1)[-1]] = raw.replace(
                b"#!/usr/bin/env python\n", b"#!python\n", 1
            )
    if change:
        change(data)
    records = io.StringIO(newline="")
    writer = csv.writer(records, lineterminator="\n")
    for name, raw in sorted(data.items()):
        writer.writerow(
            [
                name,
                "sha256="
                + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="),
                len(raw),
            ]
        )
    writer.writerow([prefix + "RECORD", "", ""])
    data[prefix + "RECORD"] = records.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, raw in data.items():
            package.writestr(name, raw)
    return output.getvalue()


class SrtSourceTests(unittest.TestCase):
    def test_sdist_completo_se_verifica_sin_ejecutar_setup(self):
        files = source()
        raw = archive([(name, body, tarfile.REGTYPE) for name, body in files.items()])
        result = BUILD.inspect_source(raw, catalogue(files), digest(raw))
        self.assertEqual(result, files)

    def test_sdist_rechaza_hash_extra_enlace_escape_casefold_y_dos(self):
        files = source()
        entries = [(name, body, tarfile.REGTYPE) for name, body in files.items()]
        for name, body, kind in [
            ("../escape", b"x", tarfile.REGTYPE),
            ("CON.txt", b"x", tarfile.REGTYPE),
            ("SRT.py", b"x", tarfile.REGTYPE),
            ("link", b"", tarfile.SYMTYPE),
            ("extra.py", b"x", tarfile.REGTYPE),
        ]:
            raw = archive(entries + [(name, body, kind)])
            with self.subTest(name=name), self.assertRaises(BUILD.BuildError):
                BUILD.inspect_source(raw, catalogue(files), digest(raw))
        raw = archive(entries)
        with self.assertRaises(BUILD.BuildError):
            BUILD.inspect_source(raw, catalogue(files), "0" * 64)

    def test_sdist_faltante_o_contenido_alterado_no_adopta(self):
        files = source()
        for changed in [
            {k: v for k, v in files.items() if k != "LICENSE"},
            {**files, "srt.py": b"changed"},
        ]:
            raw = archive([(n, b, tarfile.REGTYPE) for n, b in changed.items()])
            with self.assertRaises(BUILD.BuildError):
                BUILD.inspect_source(raw, catalogue(files), digest(raw))


class SrtWheelTests(unittest.TestCase):
    def test_shebang_y_crlf_exactos_del_backend_windows_sin_relajar_librerias(self):
        def windows_scripts(data):
            for name in data:
                if name.startswith("srt-3.5.3.data/scripts/"):
                    data[name] = data[name].replace(b"\n", b"\r\n")

        result = BUILD.verify_wheel_bytes(wheel(source(), change=windows_scripts), source())
        self.assertEqual(result["script_line_endings"], "CRLF")
        for kind in ("mixed_body", "changed_script", "mixed_files"):

            def changed(data, kind=kind):
                windows_scripts(data)
                name = "srt-3.5.3.data/scripts/srt"
                if kind == "mixed_body":
                    data[name] = data[name].replace(b"# fixture\r\n", b"# fixture\n")
                elif kind == "changed_script":
                    data[name] += b"# unexpected\r\n"
                else:
                    data[name] = data[name].replace(b"\r\n", b"\n")

            with self.subTest(kind=kind), self.assertRaises(BUILD.BuildError):
                BUILD.verify_wheel_bytes(wheel(source(), change=changed), source())

    def test_rueda_pura_verifica_todos_los_record_codigo_licencia_y_nueve_cli(self):
        files = source()
        result = BUILD.verify_wheel_bytes(wheel(files), files)
        self.assertEqual(result["scripts"], 9)
        self.assertEqual(result["tags"], ["py3-none-any"])
        self.assertTrue(result["record_complete"])

    def test_no_acepta_codigo_alterado_aunque_record_sea_coherente(self):
        for name in ["srt.py", "srt_tools/utils.py", "srt-3.5.3.dist-info/licenses/LICENSE"]:
            with self.subTest(name=name), self.assertRaises(BUILD.BuildError):
                BUILD.verify_wheel_bytes(
                    wheel(source(), change=lambda d, name=name: d.__setitem__(name, b"altered")),
                    source(),
                )

    def test_no_acepta_dependencias_tags_nativos_ni_runtime_extra(self):
        for kind in ["dependency", "native", "pth", "missing_cli"]:

            def change(data, kind=kind):
                if kind == "dependency":
                    data["srt-3.5.3.dist-info/METADATA"] = data[
                        "srt-3.5.3.dist-info/METADATA"
                    ].replace(b"\n\n", b"\nRequires-Dist: malicious\n\n")
                elif kind == "native":
                    data["srt-3.5.3.dist-info/WHEEL"] = (
                        b"Root-Is-Purelib: false\nTag: cp312-cp312-win_amd64\n"
                    )
                elif kind == "pth":
                    data["execute.pth"] = b"import malicious\n"
                else:
                    data.pop("srt-3.5.3.data/scripts/srt-play")

            with self.subTest(kind=kind), self.assertRaises(BUILD.BuildError):
                BUILD.verify_wheel_bytes(wheel(source(), change=change), source())

    def test_record_sin_hash_tamano_incorrecto_o_entrada_repetida_falla(self):
        raw = wheel(source())
        with zipfile.ZipFile(io.BytesIO(raw)) as p:
            contents = {n: p.read(n) for n in p.namelist()}
        record = "srt-3.5.3.dist-info/RECORD"
        for kind in ["empty", "size", "duplicate", "missing"]:
            changed = dict(contents)
            rows = list(csv.reader(io.StringIO(contents[record].decode())))
            if kind == "empty":
                rows[0][1] = ""
            elif kind == "size":
                rows[0][2] = "999999"
            elif kind == "duplicate":
                rows.append(rows[0])
            else:
                rows.pop(0)
            out = io.StringIO(newline="")
            csv.writer(out).writerows(rows)
            changed[record] = out.getvalue().encode()
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as p:
                for n, body in changed.items():
                    p.writestr(n, body)
            with self.subTest(kind=kind), self.assertRaises(BUILD.BuildError):
                BUILD.verify_wheel_bytes(output.getvalue(), source())


class SrtExecutionPolicyTests(unittest.TestCase):
    def test_manifiesto_no_permite_otro_python_job_version_o_limites(self):
        import copy

        value = json.loads((ROOT / "docker/supoclip/srt-build-inputs.json").read_bytes())
        BUILD.validate_manifest_contract(value)
        for kind in ("python", "job", "version", "timeout"):
            changed = copy.deepcopy(value)
            if kind == "python":
                changed["sdk"]["python"] = "C:/other/python.exe"
            elif kind == "job":
                changed["helpers"]["job"]["sha256"] = "0" * 64
            elif kind == "version":
                changed["source"]["version"] = "99.0"
            else:
                changed["limits"]["seconds"] = 181
            with self.subTest(kind=kind), self.assertRaises(BUILD.BuildError):
                BUILD.validate_manifest_contract(changed)

    def test_pins_de_plan_obsoletos_fallan_antes_de_preparar(self):
        dto = {"manifest_sha256": "a" * 64, "plan_sha256": "b" * 64}
        BUILD.require_plan_pins(dto, "a" * 64, "b" * 64)
        for manifest, plan in [(None, None), ("c" * 64, "b" * 64), ("a" * 64, "c" * 64)]:
            with self.subTest(manifest=manifest, plan=plan), self.assertRaises(BUILD.BuildError):
                BUILD.require_plan_pins(dto, manifest, plan)

    def test_identidad_o_marcador_ajeno_se_rechaza(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = root.stat()
            marker = {
                "schema": 1,
                "root": str(root),
                "owner": BUILD.OWNER,
                "device": info.st_dev,
                "inode": info.st_ino,
            }
            raw = json.dumps(marker).encode()
            BUILD.validate_marker(root, raw, marker, digest(raw))
            for changed in [{**marker, "owner": "foreign"}, {**marker, "inode": info.st_ino + 1}]:
                encoded = json.dumps(changed).encode()
                with self.assertRaises(BUILD.BuildError):
                    BUILD.validate_marker(root, encoded, changed, digest(encoded))

    def test_no_sobrescribe_archivo_y_rechaza_hardlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "sentinel"
            sentinel.write_bytes(b"foreign fixture")
            with self.assertRaises(FileExistsError):
                BUILD.write_new(sentinel, b"changed")
            self.assertEqual(sentinel.read_bytes(), b"foreign fixture")
            os.link(sentinel, root / "linked")
            with self.assertRaises(BUILD.BuildError):
                BUILD.selected_sdk_files(root, "source")

    def prepared_fixture(self, root):
        files = source()
        for name, raw in files.items():
            target = root / "source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        backend = b"# fixture, never executed\n"
        (root / "backend.py").write_bytes(backend)
        (root / "prepare.json").write_text(
            json.dumps({"owner": BUILD.OWNER, "source_sha256": "a" * 64})
        )
        (root / "wheels").mkdir()
        return {
            "root": str(root),
            "source": {"sha256": "a" * 64, "files": catalogue(files)},
            "backend_sha256": digest(backend),
            "sdk": {"python": "C:/fixed/python.exe"},
            "helpers": {"job": {"path": "not-executed"}},
        }

    def test_preparado_alterado_no_llega_al_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.prepared_fixture(root)
            BUILD.verify_prepared(manifest, root)
            (root / "source/srt.py").write_bytes(b"changed")
            with patch.object(BUILD, "load_helper") as helper:
                with self.assertRaises(BUILD.BuildError):
                    BUILD.build(manifest, {"plan_sha256": "b" * 64})
                helper.assert_not_called()

    def test_timeout_conserva_intento_logs_y_centininela_sin_reintento(self):
        @dataclass
        class Result:
            status: str = "timeout"
            exit_code: object = None
            cleanup_complete: bool = True
            stdout: bytes = b"controlled stdout"
            stderr: bytes = b"controlled stderr"
            elapsed_seconds: float = 0.1

        calls = []

        def run(executable, arguments, **kwargs):
            calls.append((executable, arguments, kwargs))
            return Result()

        fake_job = SimpleNamespace(run_job=run, Limits=lambda **kwargs: kwargs)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.prepared_fixture(root)
            sentinel = root / "foreign-sentinel"
            sentinel.write_bytes(b"preserved")
            with patch.object(BUILD, "load_helper", return_value=fake_job):
                with self.assertRaises(BUILD.BuildError):
                    BUILD.build(manifest, {"plan_sha256": "b" * 64})
                self.assertEqual((root / "build.stderr.log").read_bytes(), b"controlled stderr")
                self.assertEqual(
                    json.loads((root / "build-result.json").read_bytes())["status"], "timeout"
                )
                self.assertTrue((root / "attempt.json").is_file())
                self.assertFalse((root / "wheel-receipt.json").exists())
                with self.assertRaises(BUILD.BuildError):
                    BUILD.build(manifest, {"plan_sha256": "b" * 64})
            self.assertEqual(sentinel.read_bytes(), b"preserved")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1][:4], ["-I", "-B", "-X", "utf8"])
            self.assertEqual(calls[0][2]["timeout"], 180)
            self.assertEqual(calls[0][2]["output_limit"], 1024**2)

    def test_backend_bloquea_red_procesos_pip_y_no_bloquea_import_estandar(self):
        for event, args in [
            ("subprocess.Popen", ()),
            ("os.system", ()),
            ("os.exec", ()),
            ("socket.__new__", ()),
            ("import", ("pip._internal",)),
        ]:
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                BACKEND.deny_audit_event(event, args)
        BACKEND.deny_audit_event("import", ("codecs",))

    def test_entorno_desde_cero_y_argumentos_cerrados(self):
        root = Path("C:/owned-srt")
        env = BUILD.make_environment(root)
        self.assertEqual(env["PATH"], "C:\\Windows\\System32")
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("TOKEN", json.dumps(env))
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertNotIn("HTTP_PROXY", env)

    def test_resultado_sin_cleanup_o_timeout_no_es_salida_aprobada(self):
        for status, code, cleanup in [
            ("timeout", None, True),
            ("completed", 0, False),
            ("completed", 1, True),
        ]:
            with (
                self.subTest(status=status, code=code, cleanup=cleanup),
                self.assertRaises(BUILD.BuildError),
            ):
                BUILD.accept_job_result(
                    {"status": status, "exit_code": code, "cleanup_complete": cleanup}
                )
        BUILD.accept_job_result({"status": "completed", "exit_code": 0, "cleanup_complete": True})


if __name__ == "__main__":
    unittest.main()
