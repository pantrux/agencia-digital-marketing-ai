"""Cobertura estricta de grafos GPU, sin instalar paquetes ni consultar servicios."""

import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_gpu_dependencies", PROJECT / "scripts/ci/check_gpu_dependencies.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
INPUTS = (
    "requirements-metrics.lock",
    "requirements-immich.in",
    "immich-artifacts.json",
    "requirements-whisper.lock",
    "whisper-artifacts.json",
)
OSV_TIMEOUT = (
    b"requests.exceptions.HTTPError: 504 Server Error: Gateway Timeout "
    b"for url: https://api.osv.dev/v1/query\n"
)


class DependenciasGpu(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "gpu-bridge-win11"
        self.source.mkdir()
        for name in INPUTS:
            shutil.copyfile(PROJECT / "gpu-bridge-win11" / name, self.source / name)

    def manifest(self, name="immich-artifacts.json"):
        return json.loads((self.source / name).read_text(encoding="utf-8"))

    def save(self, value, name="immich-artifacts.json"):
        (self.source / name).write_text(json.dumps(value), encoding="utf-8")

    def test_inventarios_separados_cubren_cada_pin_y_ort_por_osv(self):
        inventory = MODULE.collect_inventory(self.root)
        graphs = inventory["graphs"]
        self.assertEqual(
            {key: len(value["packages"]) for key, value in graphs.items()},
            {"metrics": 4, "immich-runtime": 70, "immich-build": 8, "whisper": 94},
        )
        self.assertEqual(len(graphs["immich-runtime"]["audit"]), 69)
        self.assertEqual(graphs["immich-runtime"]["audit"]["onnxruntime-gpu"], "1.27.1")
        self.assertEqual(graphs["immich-runtime"]["audit"]["packaging"], "23.2")
        self.assertEqual(graphs["immich-build"]["audit"]["packaging"], "26.3")
        self.assertEqual(len(inventory["inputs_sha256"]), 5)

    def test_omitir_o_duplicar_artefacto_runtime_o_build_rechaza(self):
        original = self.manifest()
        for role in ("runtime", "build"):
            for operation in ("missing", "duplicate"):
                value = copy.deepcopy(original)
                entry = next(item for item in value["artifacts"] if item["role"] == role)
                if operation == "missing":
                    value["artifacts"].remove(entry)
                else:
                    value["artifacts"].append(entry)
                self.save(value)
                with self.subTest(role=role, operation=operation), self.assertRaises(ValueError):
                    MODULE.collect_inventory(self.root)

    def test_fuente_exenta_exige_version_commit_url_y_hash_exactos(self):
        original = self.manifest()
        for key, replacement in (
            ("version", "0.2.1"),
            ("commit", "a" * 40),
            ("sha256", "b" * 64),
            ("url", "https://ejemplo.invalid/fuente"),
        ):
            value = copy.deepcopy(original)
            next(item for item in value["artifacts"] if item["name"] == "immich-model")[key] = (
                replacement
            )
            self.save(value)
            with self.subTest(key=key), self.assertRaises(ValueError):
                MODULE.collect_inventory(self.root)

    def test_manifest_no_puede_declarar_otras_excepciones(self):
        value = self.manifest()
        value["audit_exceptions"].append({"name": "numpy", "version": "2.5.3", "reason": "ignorar"})
        self.save(value)
        self.assertIn(
            "numpy", MODULE.collect_inventory(self.root)["graphs"]["immich-runtime"]["audit"]
        )

    def test_url_insegura_hash_invalido_o_version_distinta_del_wheel_rechazan(self):
        original = self.manifest()
        for key, replacement in (
            ("url", "http://files.pythonhosted.org/archivo.whl"),
            ("sha256", "z" * 64),
            ("version", "999.0"),
        ):
            value = copy.deepcopy(original)
            next(item for item in value["artifacts"] if item["kind"] == "wheel")[key] = replacement
            self.save(value)
            with self.subTest(key=key), self.assertRaises(ValueError):
                MODULE.collect_inventory(self.root)

    def test_lock_immich_debe_ser_biyeccion_del_runtime(self):
        file = self.source / "requirements-immich.in"
        text = file.read_text(encoding="utf-8")
        file.write_text(text.replace("packaging==23.2", "packaging==26.3"), encoding="utf-8")
        with self.assertRaises(ValueError):
            MODULE.collect_inventory(self.root)

    def test_ort_no_puede_cambiar_de_procedencia_aunque_conserve_version(self):
        value = self.manifest()
        entry = next(item for item in value["artifacts"] if item["name"] == "onnxruntime-gpu")
        previous = entry["url"]
        entry["url"] = (
            "https://files.pythonhosted.org/packages/otro/onnxruntime_gpu-1.27.1-cp312-cp312-win_amd64.whl"
        )
        self.save(value)
        file = self.source / "requirements-immich.in"
        file.write_text(
            file.read_text(encoding="utf-8").replace(previous, entry["url"]), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            MODULE.collect_inventory(self.root)

    def test_whisper_lock_completo_y_manifest_directo_no_pueden_divergir(self):
        file = self.source / "requirements-whisper.lock"
        original = file.read_text(encoding="utf-8")
        for text in (
            "\n".join(original.splitlines()[:-1]),
            original + original.splitlines()[1] + "\n",
        ):
            file.write_text(text, encoding="utf-8")
            with self.subTest(text=len(text)), self.assertRaises(ValueError):
                MODULE.collect_inventory(self.root)
        file.write_text(original, encoding="utf-8")
        value = self.manifest("whisper-artifacts.json")
        value["wheels"][0]["sha256"] = "a" * 64
        self.save(value, "whisper-artifacts.json")
        with self.assertRaises(ValueError):
            MODULE.collect_inventory(self.root)

    def test_metrics_exige_hash_y_version_fijada_para_los_cuatro(self):
        file = self.source / "requirements-metrics.lock"
        original = file.read_text(encoding="utf-8")
        for text in (original.replace("uvicorn==", "uvicorn>="), original.split(" --hash", 1)[0]):
            file.write_text(text, encoding="utf-8")
            with self.subTest(text=len(text)), self.assertRaises(ValueError):
                MODULE.collect_inventory(self.root)

    def test_whisper_no_admite_feed_externo_arbitrario_en_una_transitiva(self):
        file = self.source / "requirements-whisper.lock"
        file.write_text(
            file.read_text(encoding="utf-8").replace(
                "files.pythonhosted.org", "aiinfra.pkgs.visualstudio.com", 1
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            MODULE.collect_inventory(self.root)

    def test_informe_incompleto_extras_version_skips_vulns_y_duplicados_rechazan(self):
        expected = {"onnxruntime-gpu": "1.27.1", "numpy": "2.4.6"}
        good = {
            "dependencies": [
                {"name": name, "version": version, "vulns": []}
                for name, version in expected.items()
            ]
        }
        self.assertEqual(MODULE.validate_report(good, expected), [])
        bad = [None, {}, {"dependencies": []}, {"dependencies": good["dependencies"][:-1]}]
        for change in (
            {"version": "0.0"},
            {"skip_reason": "versión no publicada"},
            {"vulns": [{}]},
            {"vulns": None},
        ):
            value = copy.deepcopy(good)
            value["dependencies"][0].update(change)
            bad.append(value)
        bad.append({"dependencies": good["dependencies"] + [good["dependencies"][0]]})
        bad.append(
            {
                "dependencies": good["dependencies"]
                + [{"name": "immich-model", "version": "0.2.0", "vulns": []}]
            }
        )
        for value in bad:
            with self.subTest(value=value):
                self.assertTrue(MODULE.validate_report(value, expected))

    def reports(self, output, inventory):
        for name, graph in inventory["graphs"].items():
            report = {
                "dependencies": [
                    {"name": package, "version": version, "vulns": []}
                    for package, version in graph["audit"].items()
                ]
            }
            (output / f"{name}.json").write_text(json.dumps(report), encoding="utf-8")
            (output / f"{name}.stderr.txt").write_text("", encoding="utf-8")

    def test_preparacion_y_recibo_contienen_hashes_y_cobertura_exacta(self):
        output = self.root / "reports"
        inventory = MODULE.prepare(self.root, output)
        self.reports(output, inventory)
        receipt = MODULE.validate_reports(self.root, output / "inventory.json", output)
        self.assertEqual(receipt["vulnerability_service"], "osv")
        self.assertEqual(
            receipt["audited_counts"],
            {"metrics": 4, "immich-runtime": 69, "immich-build": 8, "whisper": 94},
        )
        self.assertEqual(len(receipt["reports_sha256"]), 4)
        self.assertNotIn("immich-model", (output / "immich-runtime.requirements.txt").read_text())

    def test_cambio_de_input_o_inventario_despues_de_preparar_rechaza(self):
        output = self.root / "reports"
        inventory = MODULE.prepare(self.root, output)
        self.reports(output, inventory)
        file = self.source / "requirements-metrics.lock"
        file.write_text(
            file.read_text(encoding="utf-8") + "\n# cambio posterior\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            MODULE.validate_reports(self.root, output / "inventory.json", output)
        inventory["graphs"]["metrics"]["audit"].pop("h11")
        (output / "inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
        with self.assertRaises(ValueError):
            MODULE.validate_reports(self.root, output / "inventory.json", output)

    def test_json_con_claves_duplicadas_no_puede_ocultar_un_informe(self):
        output = self.root / "reports"
        inventory = MODULE.prepare(self.root, output)
        self.reports(output, inventory)
        (output / "metrics.json").write_text(
            '{"dependencies":[],"dependencies":[]}', encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            MODULE.validate_reports(self.root, output / "inventory.json", output)

    def test_osv_no_puede_silenciar_avisos_de_esquema_o_affected_ausente(self):
        output = self.root / "reports"
        inventory = MODULE.prepare(self.root, output)
        self.reports(output, inventory)
        for warning in ("Unsupported OSV schema version", "missing 'affected' list"):
            (output / "metrics.stderr.txt").write_text(warning, encoding="utf-8")
            with self.subTest(warning=warning), self.assertRaises(ValueError):
                MODULE.validate_reports(self.root, output / "inventory.json", output)

    def test_fallo_posterior_invalida_recibo_exitoso_anterior(self):
        output = self.root / "reports"
        inventory = MODULE.prepare(self.root, output)
        self.reports(output, inventory)
        MODULE.validate_reports(self.root, output / "inventory.json", output)
        self.assertTrue((output / "receipt.json").exists())
        (output / "metrics.json").write_text('{"dependencies":[]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            MODULE.validate_reports(self.root, output / "inventory.json", output)
        self.assertFalse((output / "receipt.json").exists())

    def test_reintenta_solo_el_grafo_con_timeout_y_acredita_los_cuatro(self):
        output = self.root / "reports"
        calls = []

        def pip_audit(command, *, stderr, check):
            self.assertFalse(check)
            requirements = Path(command[command.index("--requirement") + 1])
            name = requirements.name.removesuffix(".requirements.txt")
            calls.append(name)
            if name == "immich-runtime" and calls.count(name) == 1:
                stderr.write(OSV_TIMEOUT)
                return subprocess.CompletedProcess(command, 1)
            packages = [
                line.split("==") for line in requirements.read_text(encoding="utf-8").splitlines()
            ]
            report = {
                "dependencies": [
                    {"name": key, "version": value, "vulns": []} for key, value in packages
                ]
            }
            Path(command[-1]).write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.object(MODULE.subprocess, "run", side_effect=pip_audit),
            patch.object(MODULE.time, "sleep") as sleep,
        ):
            receipt = MODULE.audit(self.root, output)
        self.assertEqual(
            calls, ["metrics", "immich-runtime", "immich-runtime", "immich-build", "whisper"]
        )
        sleep.assert_called_once_with(2)
        self.assertEqual((output / "immich-runtime.intento-1.stderr.txt").read_bytes(), OSV_TIMEOUT)
        self.assertEqual((output / "immich-runtime.stderr.txt").read_bytes(), b"")
        self.assertEqual(receipt["audited_counts"]["immich-runtime"], 69)

    def test_no_reintenta_un_informe_con_hallazgos_ni_un_aviso_incompleto(self):
        output = self.root / "reports"
        MODULE.prepare(self.root, output)
        findings = {
            "dependencies": [{"name": "h11", "version": "0.16.0", "vulns": [{"id": "ENSAYO"}]}]
        }
        cases = (
            (OSV_TIMEOUT, findings),
            (OSV_TIMEOUT + b"Unsupported OSV schema version\n", None),
            (OSV_TIMEOUT.replace(b"api.osv.dev", b"otro.ejemplo"), None),
        )
        for diagnostic, submitted in cases:

            def pip_audit(command, *, stderr, check, diagnostic=diagnostic, submitted=submitted):
                stderr.write(diagnostic)
                if submitted is not None:
                    Path(command[-1]).write_text(json.dumps(submitted), encoding="utf-8")
                return subprocess.CompletedProcess(command, 1)

            with (
                self.subTest(diagnostic=diagnostic, submitted=submitted),
                patch.object(MODULE.subprocess, "run", side_effect=pip_audit) as process,
                patch.object(MODULE.time, "sleep") as sleep,
            ):
                self.assertFalse(MODULE.audit_graph(output, "metrics"))
                process.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual((output / "metrics.stderr.txt").read_bytes(), diagnostic)

    def test_agota_dos_reintentos_y_retira_el_recibo_previo(self):
        output = self.root / "reports"
        inventory = MODULE.prepare(self.root, output)
        self.reports(output, inventory)
        MODULE.validate_reports(self.root, output / "inventory.json", output)

        def pip_audit(command, *, stderr, check):
            stderr.write(OSV_TIMEOUT)
            return subprocess.CompletedProcess(command, 1)

        with (
            patch.object(MODULE.subprocess, "run", side_effect=pip_audit) as process,
            patch.object(MODULE.time, "sleep") as sleep,
            self.assertRaisesRegex(ValueError, "Falló la auditoría OSV"),
        ):
            MODULE.audit(self.root, output)
        self.assertEqual(process.call_count, 12)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5] * 4)
        self.assertEqual((output / "whisper.intento-2.stderr.txt").read_bytes(), OSV_TIMEOUT)
        self.assertFalse((output / "receipt.json").exists())
        self.assertFalse((output / "whisper.json").exists())


if __name__ == "__main__":
    unittest.main()
