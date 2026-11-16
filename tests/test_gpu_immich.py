"""Verifica portabilidad y pruebas de inferencia sin cargar CUDA ni lanzar servicios."""

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import struct
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "immich_portability", ROOT / "gpu-bridge-win11/immich_portability.py"
)
PORTABLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PORTABLE)
with patch.dict(sys.modules, {"immich_portability": PORTABLE}):
    SERVICE_SPEC = importlib.util.spec_from_file_location(
        "immich_service", ROOT / "gpu-bridge-win11/immich_service.py"
    )
    SERVICE = importlib.util.module_from_spec(SERVICE_SPEC)
    SERVICE_SPEC.loader.exec_module(SERVICE)


async def request(app, path="/predict", headers=()):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "path": path,
            "method": "POST",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
        },
        receive,
        send,
    )
    return messages


class PortabilidadImmich(unittest.TestCase):
    def test_parche_exige_fuente_y_resultado_exactos_y_es_idempotente(self):
        before, after = b"valor = 1\n", b"valor = 2\n"
        manifest = {
            "patches": [
                {
                    "path": "immich_ml/config.py",
                    "before_sha256": hashlib.sha256(before).hexdigest(),
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                    "replacements": [{"old": "valor = 1", "new": "valor = 2"}],
                }
            ]
        }
        files = {"immich_ml/config.py": before, "immich_ml/main.py": b"original\n"}
        patched = PORTABLE.patch_sources(files, manifest)
        self.assertEqual(patched["immich_ml/config.py"], after)
        self.assertEqual(patched["immich_ml/main.py"], files["immich_ml/main.py"])
        self.assertEqual(PORTABLE.patch_sources(patched, manifest), patched)
        self.assertEqual(files["immich_ml/config.py"], before)
        with self.assertRaises(ValueError):
            PORTABLE.patch_sources(files | {"immich_ml/config.py": b"otra fuente\n"}, manifest)

    def test_un_parche_ambiguo_o_fuera_del_arbol_se_rechaza(self):
        for path, content in (("../otra.py", b"x"), ("immich_ml/config.py", b"x x")):
            manifest = {
                "patches": [
                    {
                        "path": path,
                        "before_sha256": hashlib.sha256(content).hexdigest(),
                        "after_sha256": "0" * 64,
                        "replacements": [{"old": "x", "new": "y"}],
                    }
                ]
            }
            with self.subTest(path=path), self.assertRaises(ValueError):
                PORTABLE.patch_sources({path: content}, manifest)

    def test_cuda_debe_ser_primario_y_no_puede_desaparecer(self):
        PORTABLE.validate_provider_order(["CUDAExecutionProvider", "CPUExecutionProvider"])
        for providers in (
            [],
            ["CPUExecutionProvider"],
            ["CPUExecutionProvider", "CUDAExecutionProvider"],
            ["CUDAExecutionProvider", "otro"],
        ):
            with self.subTest(providers=providers), self.assertRaises(RuntimeError):
                PORTABLE.validate_provider_order(providers)

    def test_perfil_exige_inferencia_cuda_y_solo_control_acotado_en_cpu(self):
        gpu = {
            "cat": "Node",
            "name": "MatMul_kernel_time",
            "args": {"op_name": "MatMul", "provider": "CUDAExecutionProvider"},
        }
        control = {
            "cat": "Node",
            "name": "Shape_kernel_time",
            "args": {"op_name": "Shape", "provider": "CPUExecutionProvider"},
        }
        proof = PORTABLE.validate_cuda_profile([gpu, control])
        self.assertEqual(proof["cuda_nodes"], 1)
        self.assertEqual(proof["cpu_control_nodes"], 1)
        for events in (
            [],
            [control],
            [gpu | {"args": {"op_name": "MatMul", "provider": "CPUExecutionProvider"}}],
            [
                gpu,
                {
                    "cat": "Node",
                    "name": "Conv_kernel_time",
                    "args": {"op_name": "Conv", "provider": "CPUExecutionProvider"},
                },
            ],
        ):
            with self.subTest(events=events), self.assertRaises(RuntimeError):
                PORTABLE.validate_cuda_profile(events)

    def test_copias_cuda_no_acreditan_calculo_y_control_entero_tiene_limite(self):
        copy = {
            "cat": "Node",
            "name": "copy_kernel_time",
            "args": {"op_name": "MemcpyFromHost", "provider": "CUDAExecutionProvider"},
        }
        with self.assertRaises(RuntimeError):
            PORTABLE.validate_cuda_profile([copy])
        gpu = {
            "cat": "Node",
            "name": "MatMul_kernel_time",
            "args": {"op_name": "MatMul", "provider": "CUDAExecutionProvider"},
        }
        control = {
            "cat": "Node",
            "name": "add_kernel_time",
            "args": {
                "op_name": "Add",
                "provider": "CPUExecutionProvider",
                "input_type_shape": [{"int64": [2]}],
            },
        }
        self.assertEqual(PORTABLE.validate_cuda_profile([gpu, control])["cpu_control_nodes"], 1)
        control["args"]["input_type_shape"] = [{"float": [512]}]
        with self.assertRaises(RuntimeError):
            PORTABLE.validate_cuda_profile([gpu, control])

    def test_argmax_de_tokens_clip_es_control_entero_y_no_inferencia_cpu(self):
        gpu = {
            "cat": "Node",
            "name": "MatMul_kernel_time",
            "args": {"op_name": "MatMul", "provider": "CUDAExecutionProvider"},
        }
        tokens = {
            "cat": "Node",
            "name": "/ArgMax_kernel_time",
            "args": {
                "op_name": "ArgMax",
                "provider": "CPUExecutionProvider",
                "input_type_shape": [{"int32": [1, 77]}],
            },
        }
        proof = PORTABLE.validate_cuda_profile([gpu, tokens])
        self.assertEqual(proof["cuda_nodes"], 1)
        self.assertEqual(proof["cpu_control_nodes"], 1)
        for shape in ({"int32": [1, 78]}, {"float": [1, 77]}):
            tokens["args"]["input_type_shape"] = [shape]
            with self.assertRaises(RuntimeError):
                PORTABLE.validate_cuda_profile([gpu, tokens])

    def test_prueba_cuda_caduca_y_un_fallo_no_se_oculta_con_otro_modelo(self):
        clock = [0.0]
        evidence = PORTABLE.CudaEvidence(clock=lambda: clock[0])
        self.assertFalse(evidence.snapshot()["ready"])
        evidence.record("probe", {"cuda_nodes": 1, "cpu_control_nodes": 0})
        self.assertTrue(evidence.snapshot()["ready"])
        clock[0] = 301.0
        self.assertFalse(evidence.snapshot()["ready"])
        evidence.fail()
        evidence.record("clip/visual", {"cuda_nodes": 2, "cpu_control_nodes": 0})
        self.assertFalse(evidence.snapshot()["ready"])

    def test_sesion_deshabilita_reintento_cpu_y_valida_perfil_real_antes_de_contar(self):
        class Options:
            pass

        class Session:
            def __init__(self, options):
                self.options = options
                self.providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
                self.fallback_disabled = False

            def disable_fallback(self):
                self.fallback_disabled = True

            def get_providers(self):
                return self.providers

            def run(self, *args, **kwargs):
                return ["resultado original"]

            def end_profiling(self):
                path = Path(self.options.profile_file_prefix + ".json")
                path.write_text(
                    json.dumps(
                        [
                            {
                                "cat": "Node",
                                "name": "Conv_kernel_time",
                                "args": {"op_name": "Conv", "provider": "CUDAExecutionProvider"},
                            }
                        ]
                    )
                )
                return str(path)

        with tempfile.TemporaryDirectory() as temporary:
            evidence = PORTABLE.CudaEvidence()

            def factory(*args, **kwargs):
                return Session(kwargs["sess_options"])

            session = PORTABLE.strict_cuda_session(
                factory,
                "model.onnx",
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                sess_options=Options(),
                profile_directory=Path(temporary),
                evidence=evidence,
                model_kind="probe",
            )
            self.assertTrue(session.fallback_disabled)
            self.assertEqual(session.run(None, {}), ["resultado original"])
            self.assertTrue(evidence.snapshot()["ready"])
            session.providers[:] = ["CPUExecutionProvider"]
            with self.assertRaises(RuntimeError):
                session.run(None, {})
            self.assertFalse(evidence.snapshot()["ready"])

    def test_cache_solo_adopta_directorio_vacio_o_marcador_propio(self):
        manifest = {"source": {"commit": "a" * 40}, "models": []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "immich"
            root.mkdir()
            (root / "ajeno.txt").write_text("conservar")
            with self.assertRaises(ValueError):
                PORTABLE.configure_runtime(root, manifest)
            self.assertEqual((root / "ajeno.txt").read_text(), "conservar")
            own = Path(temporary) / "propio"
            PORTABLE.configure_runtime(own, manifest)
            PORTABLE.configure_runtime(own, manifest)
            self.assertTrue((own / ".immich-cache-owner.json").is_file())

    def test_pesos_fijados_y_cuarentena_preservan_datos_y_rechazan_escape(self):
        data = b"peso de prueba"
        manifest = {
            "source": {"commit": "a" * 40},
            "models": [
                {
                    "task": "clip",
                    "name": "modelo",
                    "commit": "b" * 40,
                    "files": [
                        {
                            "path": "visual/model.onnx",
                            "cache_relative": "clip/modelo/visual/model.onnx",
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "size": len(data),
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "immich"
            PORTABLE.configure_runtime(root, manifest)
            model = root / "clip/modelo"
            (model / "visual").mkdir(parents=True)
            (model / "visual/model.onnx").write_bytes(data)
            PORTABLE.verify_model_cache(model, "modelo", "clip")
            (model / "visual/model.onnx").write_bytes(b"cambiado")
            with self.assertRaises(ValueError):
                PORTABLE.verify_model_cache(model, "modelo", "clip")
            moved = PORTABLE.clear_private_model_cache(model)
            self.assertEqual((moved / "visual/model.onnx").read_bytes(), b"cambiado")
            self.assertEqual(list(model.iterdir()), [])
            outside = Path(temporary) / "ajeno"
            outside.mkdir()
            with self.assertRaises(ValueError):
                PORTABLE.clear_private_model_cache(outside)
            self.assertTrue(outside.exists())

    def test_fuente_cacheada_exige_archivo_recibo_y_hashes_originales(self):
        payload = b"app = object()\n"
        source_commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                item = tarfile.TarInfo(
                    "immich-" + source_commit + "/machine-learning/immich_ml/main.py"
                )
                item.size = len(payload)
                bundle.addfile(item, io.BytesIO(payload))
            manifest = {
                "version": "3.3.6",
                "source": {
                    "commit": source_commit,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                },
                "source_files": {"immich_ml/main.py": hashlib.sha256(payload).hexdigest()},
                "patches": [],
            }
            target = root / "vendor/machine-learning"
            PORTABLE.prepare_source(archive, target, manifest)
            self.assertEqual(PORTABLE.verify_source(target, manifest), target.resolve())
            PORTABLE.prepare_source(archive, target, manifest)
            adopted = root / "existing/machine-learning"
            (adopted / "immich_ml").mkdir(parents=True)
            (adopted / "immich_ml/main.py").write_bytes(payload)
            with self.assertRaises(ValueError):
                PORTABLE.prepare_source(archive, adopted, manifest)
            PORTABLE.prepare_source(archive, adopted, manifest, allow_existing_original=True)
            self.assertEqual(PORTABLE.verify_source(adopted, manifest), adopted.resolve())
            (target / "immich_ml/main.py").write_text("fuente ajena\n")
            with self.assertRaises(ValueError):
                PORTABLE.verify_source(target, manifest)
            with self.assertRaises(ValueError):
                PORTABLE.prepare_source(archive, target, manifest)

    def test_adopcion_de_pesos_exige_propietario_recibo_exacto_y_cero_extras(self):
        content = b"peso verificado"
        digest = hashlib.sha256(content).hexdigest()
        manifest = {
            "source": {"commit": "a" * 40},
            "models": [
                {
                    "task": "clip",
                    "name": "modelo",
                    "commit": "b" * 40,
                    "files": [
                        {
                            "cache_relative": "clip/modelo/visual/model.onnx",
                            "size": len(content),
                            "sha256": digest,
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            install = Path(temporary)
            cache = install / "models/immich"
            model = cache / "clip/modelo/visual/model.onnx"
            model.parent.mkdir(parents=True)
            model.write_bytes(content)
            (install / ".agencia-gpu-owner").write_text(PORTABLE.INSTALL_OWNER + "\n")
            receipt = install / "model-install-receipt.json"
            record = {
                "relative": "immich/clip/modelo/visual/model.onnx",
                "size": len(content),
                "sha256": digest,
            }
            receipt.write_text(json.dumps({"model_files": [record]}))
            extra = cache / "ajeno.txt"
            extra.write_text("preservar")
            with self.assertRaises(ValueError):
                PORTABLE.adopt_verified_cache(cache, install, manifest)
            self.assertFalse((cache / PORTABLE.CACHE_MARKER).exists())
            self.assertEqual(extra.read_text(), "preservar")
            extra.unlink()
            receipt.write_text(json.dumps({"model_files": [record | {"sha256": "0" * 64}]}))
            with self.assertRaises(ValueError):
                PORTABLE.adopt_verified_cache(cache, install, manifest)
            receipt.write_text(json.dumps({"model_files": [record]}))
            (install / ".agencia-gpu-owner").write_text("ajeno")
            with self.assertRaises(ValueError):
                PORTABLE.adopt_verified_cache(cache, install, manifest)
            (install / ".agencia-gpu-owner").write_text(PORTABLE.INSTALL_OWNER)
            PORTABLE.adopt_verified_cache(cache, install, manifest)
            PORTABLE.adopt_verified_cache(cache, install, manifest)
            self.assertTrue((cache / PORTABLE.CACHE_MARKER).is_file())
            self.assertEqual(model.read_bytes(), content)

    def test_embedding_json_original_rechaza_formato_tamano_no_finito_y_cero(self):
        valid = json.dumps([0.1] * 512)
        expected = hashlib.sha256(struct.pack("<512f", *([0.1] * 512))).hexdigest()
        self.assertEqual(SERVICE.validate_embedding(valid), expected)
        self.assertEqual(SERVICE.validate_embedding(json.dumps([0.1] * 512, indent=2)), expected)
        for value in (
            None,
            "!!!",
            base64.b64encode(struct.pack("<512f", *([0.1] * 512))).decode(),
            json.dumps([0.1] * 511),
            json.dumps([0.1] * 513),
            json.dumps([0] * 512),
            json.dumps([float("nan")] * 512),
            json.dumps([float("inf")] * 512),
            json.dumps([1e100] * 512),
            json.dumps([True] * 512),
            json.dumps(["0.1"] * 512),
            json.dumps({"embedding": [0.1] * 512}),
            json.dumps([[0.1]] * 512),
        ):
            with self.subTest(value=str(value)[:12]), self.assertRaises(ValueError):
                SERVICE.validate_embedding(value)

    def test_predict_conserva_respuesta_y_libera_modelos_antes_de_entregarla(self):
        backend = SERVICE.NativeImmich(None, {}, None, {})
        backend.started = True
        backend.memory_free_mib = lambda: 2048
        sequence = []
        backend.release_models = lambda: sequence.append("liberado")

        async def upstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"clip":"original"}'})

        backend.app = upstream

        async def run():
            async def send(message):
                self.assertEqual(sequence, ["liberado"])
                messages.append(message)

            await backend({"type": "http", "path": "/predict"}, None, send)

        messages = []
        with patch.dict(sys.modules, {"bridge_common": SimpleNamespace()}):
            sys.modules["bridge_common"].json_response = lambda *args: None
            asyncio.run(run())
        self.assertEqual(messages[-1]["body"], b'{"clip":"original"}')

    def test_predict_fallido_libera_modelos_y_no_envia_exito_parcial(self):
        backend = SERVICE.NativeImmich(None, {}, None, {})
        backend.started = True
        backend.memory_free_mib = lambda: 2048
        released = []
        backend.release_models = lambda: released.append(True)

        async def upstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise RuntimeError("fallo simulado")

        backend.app = upstream
        with patch.dict(sys.modules, {"bridge_common": SimpleNamespace(json_response=None)}):
            with self.assertRaises(RuntimeError):
                asyncio.run(request(backend))
        self.assertEqual(released, [True])

    def test_factory_no_carga_cuda_y_modelo_no_fijado_se_rechaza(self):
        with patch.dict(sys.path_importer_cache):
            sys.path.insert(0, str(ROOT / "gpu-bridge-win11"))
            try:
                mapping = {
                    "GPU_BRIDGE_HOST": "192.168.0.6",
                    "GPU_BRIDGE_BEARER_TOKEN": "prueba" * 12,
                    "GPU_INSTALL_ROOT": r"C:\GPU-Bridge",
                    "GPU_MODEL_CACHE_ROOT": r"C:\GPU-Bridge\models",
                    "GPU_IMMICH_CLIP_MODEL": "ViT-B-32__openai",
                    "GPU_IMMICH_FACE_MODEL": "buffalo_l",
                }
                with patch.object(SERVICE, "preload_cuda", side_effect=AssertionError("no CUDA")):
                    app = SERVICE.create_app(mapping)
                    self.assertEqual(app.service, "immich-ml")
                    self.assertFalse(app.app.started)
                    with self.assertRaises(ValueError):
                        SERVICE.create_app(mapping | {"GPU_IMMICH_FACE_MODEL": "otro"})
            finally:
                sys.path.pop(0)


if __name__ == "__main__":
    unittest.main()
