"""Contratos de cierre CPU y fuentes como datos: no instala ni ejecuta SDK/apps."""

import base64
import copy
import hashlib
import importlib.util
import io
import json
import tarfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "supoclip_cpu_inputs", ROOT / "docker/supoclip/prepare_cpu_inputs.py"
)
CPU = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CPU)


def package(name, dependencies=None, **extra):
    return {
        "name": name,
        "version": "1.0",
        "source": {"registry": "https://pypi.org/simple"},
        "dependencies": dependencies or [],
        **extra,
    }


def lock():
    return {
        "requires-python": ">=3.11",
        "package": [
            {
                "name": "supoclip-backend",
                "version": "0.1.0",
                "source": {"editable": "."},
                "dependencies": [
                    {"name": "base"},
                    {"name": "openai-whisper"},
                    {"name": "whisper"},
                    {"name": "setuptools-rust"},
                    {"name": "provider", "extra": ["voice"]},
                ],
            },
            package(
                "base",
                [
                    {"name": "unix", "marker": "sys_platform == 'linux'"},
                    {"name": "windows", "marker": "sys_platform == 'win32'"},
                ],
            ),
            package("provider", **{"optional-dependencies": {"voice": [{"name": "voice"}]}}),
            package("unix"),
            package("windows"),
            package("voice"),
        ],
    }


class GraphTests(unittest.TestCase):
    def test_perfil_slim_es_explicito_y_no_muta_lock_original(self):
        value = lock()
        value["package"][0]["dependencies"].append({"name": "pydantic-ai"})
        original = copy.deepcopy(value)
        profiled = CPU.cpu_profile(value)
        self.assertEqual(value, original)
        roots = profiled["package"][0]["dependencies"]
        self.assertNotIn({"name": "pydantic-ai"}, roots)
        self.assertIn(
            {"name": "pydantic-ai-slim", "extra": ["anthropic", "google", "openai"]}, roots
        )
        self.assertIn({"name": "python-multipart"}, roots)

    def test_perfil_slim_rechaza_upstream_cambiado_o_multipart_ya_directo(self):
        for duplicate in (False, True):
            value = lock()
            if duplicate:
                value["package"][0]["dependencies"] += [
                    {"name": "pydantic-ai"},
                    {"name": "python-multipart"},
                ]
            with self.subTest(duplicate=duplicate), self.assertRaises(CPU.InputError):
                CPU.cpu_profile(value)

    def test_cierre_target_con_extras_sin_whisper_torch_ni_dev(self):
        value = CPU.select_graph(lock())
        self.assertEqual(set(value["packages"]), {"base", "unix", "provider", "voice"})
        self.assertEqual(value["extras"]["provider"], ["", "voice"])
        self.assertNotIn("windows", value["packages"])

    def test_dependencia_gpu_indirecta_falla_no_se_omite(self):
        value = lock()
        value["package"].append(package("nvidia-cublas"))
        value["package"][1]["dependencies"].append({"name": "nvidia-cublas"})
        with self.assertRaisesRegex(CPU.InputError, "GPU_DEPENDENCY"):
            CPU.select_graph(value)

    def test_referencia_ausente_ambigua_vcs_o_extra_desconocida_falla(self):
        for kind in ("missing", "ambiguous", "vcs", "extra"):
            value = lock()
            if kind == "missing":
                value["package"] = [x for x in value["package"] if x["name"] != "voice"]
            elif kind == "ambiguous":
                value["package"].append(package("base", version_override="irrelevant"))
            elif kind == "vcs":
                value["package"][1]["source"] = {"git": "https://invalid.example/project"}
            else:
                value["package"][0]["dependencies"][-1]["extra"] = ["unknown"]
            with self.subTest(kind=kind), self.assertRaises(CPU.InputError):
                CPU.select_graph(value)

    def test_metadata_real_valida_cierre_y_rechaza_specifier_nuevo_faltante_url(self):
        graph = CPU.select_graph(lock())
        rows = [
            {
                "name": name,
                "version": "1.0",
                "metadata_name": name,
                "metadata_version": "1.0",
                "requires_python": ">=3.11",
                "requires_dist": [],
            }
            for name in graph["packages"]
        ]
        rows[0]["requires_dist"] = ['unix>=1.0; sys_platform == "linux"']
        self.assertTrue(CPU.validate_metadata(graph, rows)["complete"])
        for requirement in ("unix>=2", "absent>=1", "unix @ https://invalid.example/package.whl"):
            changed = copy.deepcopy(rows)
            changed[0]["requires_dist"] = [requirement]
            with self.subTest(requirement=requirement), self.assertRaises(CPU.InputError):
                CPU.validate_metadata(graph, changed)


class WheelTests(unittest.TestCase):
    def artifact(self, filename):
        return {
            "url": "https://files.pythonhosted.org/packages/aa/bb/" + filename,
            "hash": "sha256:" + "a" * 64,
            "size": 123,
        }

    def test_tags_linux_cp312_abi3_y_puro_sin_aceptar_windows_cuda(self):
        value = package(
            "sample",
            wheels=[
                self.artifact("sample-1.0-cp312-cp312-win_amd64.whl"),
                self.artifact("sample-1.0-cp37-abi3-manylinux_2_28_x86_64.whl"),
            ],
        )
        self.assertTrue(
            CPU.choose_artifact(value)["artifact"]["url"].endswith("manylinux_2_28_x86_64.whl")
        )
        value["wheels"] = [self.artifact("sample-1.0-py3-none-any.whl")]
        self.assertEqual(CPU.choose_artifact(value)["kind"], "wheel")
        value["wheels"] = [self.artifact("sample-1.0-cp313-cp313-manylinux_2_28_x86_64.whl")]
        with self.assertRaises(CPU.InputError):
            CPU.choose_artifact(value)

    def test_host_url_hash_y_sdist_no_autorizado_fallan(self):
        for field, wrong in [
            ("url", "https://files.pythonhosted.org.evil.example/sample.whl"),
            ("hash", "sha1:" + "a" * 40),
            ("size", 0),
        ]:
            artifact = self.artifact("sample-1.0-py3-none-any.whl")
            artifact[field] = wrong
            with self.subTest(field=field), self.assertRaises(CPU.InputError):
                CPU.choose_artifact(package("sample", wheels=[artifact]))
        with self.assertRaises(CPU.InputError):
            CPU.choose_artifact(package("arbitrary", sdist=self.artifact("arbitrary-1.0.tar.gz")))


class SourceTests(unittest.TestCase):
    def archive(self, names):
        output = io.BytesIO()
        tree = []
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, kind, raw in names:
                member = tarfile.TarInfo("supoclip-" + CPU.COMMIT + "/" + name)
                member.type = kind
                member.size = len(raw) if kind == tarfile.REGTYPE else 0
                archive.addfile(member, io.BytesIO(raw) if member.isfile() else None)
                if member.isfile():
                    tree.append(
                        {
                            "path": name,
                            "mode": "100644",
                            "type": "blob",
                            "size": len(raw),
                            "sha": hashlib.sha1(
                                b"blob " + str(len(raw)).encode() + b"\0" + raw
                            ).hexdigest(),
                        }
                    )
        return output.getvalue(), {"sha": CPU.COMMIT, "tree": tree, "truncated": False}

    def test_blob_tar_completo_sin_extraer_y_rechazo_tamper(self):
        raw, tree = self.archive([("backend/src/public.py", tarfile.REGTYPE, b"PUBLIC = True\n")])
        receipt = CPU.audit_source(raw, hashlib.sha256(raw).hexdigest(), tree)
        self.assertEqual(receipt["files_verified"], 1)
        tree["tree"][0]["sha"] = "0" * 40
        with self.assertRaises(CPU.InputError):
            CPU.audit_source(raw, hashlib.sha256(raw).hexdigest(), tree)

    def test_traversal_link_duplicado_y_casefold_fallan(self):
        for names in [
            [("../escape.py", tarfile.REGTYPE, b"x")],
            [("link", tarfile.SYMTYPE, b"")],
            [("same", tarfile.REGTYPE, b"a"), ("same", tarfile.REGTYPE, b"a")],
            [("Same", tarfile.REGTYPE, b"a"), ("same", tarfile.REGTYPE, b"a")],
        ]:
            raw, tree = self.archive(names)
            with self.subTest(names=names), self.assertRaises(CPU.InputError):
                CPU.audit_source(raw, hashlib.sha256(raw).hexdigest(), tree)


class NodeTests(unittest.TestCase):
    def value(self):
        return {
            "lockfileVersion": "9.0",
            "packages": {
                "@scope/sample@1.2.3": {
                    "resolution": {"integrity": "sha512-" + base64.b64encode(b"a" * 64).decode()}
                }
            },
        }

    def test_sri_y_tarball_del_registro_son_cerrados(self):
        rows = CPU.node_inventory(self.value())
        self.assertEqual(
            rows[0]["url"], "https://registry.npmjs.org/@scope/sample/-/sample-1.2.3.tgz"
        )
        self.assertEqual(rows[0]["version"], "1.2.3")

    def test_git_tarball_alterno_o_sri_insuficiente_no_se_omite(self):
        for resolution in (
            {"git": "https://invalid.example/repo"},
            {"tarball": "https://invalid.example/file.tgz"},
            {"integrity": "sha1-" + "A" * 28},
        ):
            value = self.value()
            value["packages"]["@scope/sample@1.2.3"]["resolution"] = resolution
            with self.subTest(resolution=resolution), self.assertRaises(CPU.InputError):
                CPU.node_inventory(value)


class PlanTests(unittest.TestCase):
    def test_lock_exige_biyeccion_version_y_hash_de_las_ruedas(self):
        rows = [
            {
                "name": "example",
                "version": "1.0",
                "role": "registry-wheel",
                "artifact": {"sha256": "a" * 64},
            }
        ]
        valid = ("example==1.0 --hash=sha256:" + "a" * 64 + "\n").encode()
        self.assertEqual(CPU.validate_requirements(valid, rows), 1)
        for raw in [
            b"",
            valid + valid,
            valid.replace(b"1.0", b"2.0"),
            valid.replace(b"a" * 64, b"b" * 64),
            valid + b"--extra-index-url https://invalid.example\n",
        ]:
            with self.subTest(raw=raw), self.assertRaises(CPU.InputError):
                CPU.validate_requirements(raw, rows)

    def test_manifiesto_real_rechaza_gpu_metadata_incompleta_y_afirmacion_de_build(self):
        value = json.loads(
            (ROOT / "docker/supoclip/cpu-build-inputs.json").read_text(encoding="utf-8")
        )
        requirements = (ROOT / "docker/supoclip/requirements-cpu.lock").read_bytes()
        self.assertEqual(CPU.validate_inputs(value, requirements)["python_nodes"], 84)
        for kind in (
            "gpu",
            "missing_metadata",
            "metadata_edge",
            "build",
            "native_pip_claim",
            "native_catalogue",
            "npm",
        ):
            changed = copy.deepcopy(value)
            if kind == "gpu":
                changed["python"]["packages"][0]["name"] = "torch"
            elif kind == "missing_metadata":
                changed["python"]["packages"].pop()
            elif kind == "metadata_edge":
                changed["python"]["packages"][0]["requires_dist"] = ["not-in-closure>=1"]
            elif kind == "build":
                changed["build_enabled"] = True
            elif kind == "native_pip_claim":
                changed["native"]["payload_materialization_and_native_solver_complete"] = True
            elif kind == "native_catalogue":
                changed["native"]["packages"][0]["record"]["D"].append("foreign-library")
            else:
                changed["frontend"]["packages"][0]["url"] = "https://invalid.example/alternate.tgz"
            with self.subTest(kind=kind), self.assertRaises(CPU.InputError):
                CPU.validate_inputs(changed, requirements)

    def test_json_duplicado_nan_y_archivo_lock_alterado_fallan(self):
        for raw in ['{"schema":1,"schema":1}', '{"schema":NaN}']:
            with self.subTest(raw=raw), self.assertRaises(CPU.InputError):
                CPU.strict_json(raw)
        value = json.loads(
            (ROOT / "docker/supoclip/cpu-build-inputs.json").read_text(encoding="utf-8")
        )
        with self.assertRaisesRegex(CPU.InputError, "REQUIREMENTS_PIN"):
            CPU.validate_inputs(value, b"# altered\n")


if __name__ == "__main__":
    unittest.main()
