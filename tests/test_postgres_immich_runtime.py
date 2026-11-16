"""Aceptación de datos OCI, cierre ABI y gates; no ejecuta imágenes ni SDK."""

import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeGuards(unittest.TestCase):
    def setUp(self):
        self.remote = load("docker/postgres-immich/final_image_remote.py", "runtime_contract")
        self.elf = load("tests/test_postgres_immich_compile.py", "elf_fixture").elf

    def files(self):
        return {
            "usr/local/bin/postgres": self.remote.file_entry(
                self.elf(defined=("pg_call",), needed=("libc.musl-x86_64.so.1",))
            ),
            "lib/libc.musl-x86_64.so.1": self.remote.file_entry(
                self.elf(defined=("malloc", "memcpy"))
            ),
        }, {
            "usr/local/lib/postgresql/vchord.so": self.remote.file_entry(
                self.elf(
                    needed=("libsqlite3.so.0", "libc.musl-x86_64.so.1"),
                    runpath="$ORIGIN",
                    undefined=("sqlite3_step", "pg_call"),
                )
            ),
            "usr/local/lib/postgresql/libsqlite3.so.0": self.remote.file_entry(
                self.elf(
                    soname="libsqlite3.so.0",
                    needed=("libc.musl-x86_64.so.1",),
                    defined=("sqlite3_step", "sqlite3_sourceid"),
                )
            ),
            "usr/local/lib/postgresql/vector.so": self.remote.file_entry(
                self.elf(needed=("libc.musl-x86_64.so.1",), undefined=("malloc",))
            ),
        }

    def test_cierre_resuelve_sqlite_adyacente_y_simbolos_del_servidor(self):
        base, additions = self.files()
        result = self.remote.abi_closure(base, additions)
        self.assertTrue(result["verified"])
        self.assertIn("usr/local/lib/postgresql/libsqlite3.so.0", result["resolved"])

    def test_cierre_no_copia_biblioteca_ausente_del_sdk(self):
        base, additions = self.files()
        del additions["usr/local/lib/postgresql/libsqlite3.so.0"]
        with self.assertRaisesRegex(ValueError, "libsqlite3"):
            self.remote.abi_closure(base, additions)

    def test_cierre_rechaza_simbolo_postgres_inexistente(self):
        base, additions = self.files()
        base["usr/local/bin/postgres"] = self.remote.file_entry(self.elf())
        with self.assertRaisesRegex(ValueError, "pg_call"):
            self.remote.abi_closure(base, additions)

    def test_enlace_virtual_no_sale_al_host_ni_admite_ciclos(self):
        base, additions = self.files()
        libc = base.pop("lib/libc.musl-x86_64.so.1")
        base["lib/loader"] = libc
        base["lib/libc.musl-x86_64.so.1"] = {"kind": "symlink", "target": "/lib/loader"}
        self.assertTrue(self.remote.abi_closure(base, additions)["verified"])
        base["lib/loader"] = {"kind": "symlink", "target": "libc.musl-x86_64.so.1"}
        with self.assertRaises(ValueError):
            self.remote.abi_closure(base, additions)

    def test_sin_evidencia_separada_y_stop_respaldado_no_promueve(self):
        for compile_value, stop_value in (({}, {}), ({"receipt": {"success": False}}, {})):
            with self.assertRaises(ValueError):
                self.remote.require_gates(compile_value, stop_value, {})

    def test_versionado_elf_no_se_infiere_por_soname(self):
        base, additions = self.files()
        entry = copy.deepcopy(additions["usr/local/lib/postgresql/vchord.so"])
        entry["elf"]["version_requirements"] = {"libc.musl-x86_64.so.1": ["VERSION_NO_DISPONIBLE"]}
        additions["usr/local/lib/postgresql/vchord.so"] = entry
        with self.assertRaisesRegex(ValueError, "VERSION_NO_DISPONIBLE"):
            self.remote.abi_closure(base, additions)


class OciAndPromotion(unittest.TestCase):
    def setUp(self):
        self.remote = load("docker/postgres-immich/final_image_remote.py", "runtime_oci")

    def test_capas_aplican_whiteout_sin_tocar_archivos_del_host(self):
        import io
        import tarfile

        def layer(entries):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as tar:
                for name, data in entries:
                    item = tarfile.TarInfo(name)
                    item.size = len(data)
                    item.mode = 0o644
                    tar.addfile(item, io.BytesIO(data))
            return stream.getvalue()

        files = {}
        self.remote.apply_layer(files, layer([("usr/bin/gosu", b"old"), ("usr/bin/keep", b"ok")]))
        self.remote.apply_layer(files, layer([("usr/bin/.wh.gosu", b"")]))
        self.assertNotIn("usr/bin/gosu", files)
        self.assertEqual(files["usr/bin/keep"]["sha256"], self.remote.digest(b"ok"))
        with self.assertRaises(ValueError):
            self.remote.apply_layer(files, layer([("../escape", b"bad")]))

    def test_blob_y_diffid_se_verifican_independientemente(self):
        import gzip

        raw = b"contenido del tar"
        blob = gzip.compress(raw)
        row = {
            "digest": "sha256:" + self.remote.digest(blob),
            "size": len(blob),
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
        }
        self.assertEqual(
            self.remote.layer_bytes(blob, row, "sha256:" + self.remote.digest(raw)), raw
        )
        with self.assertRaises(ValueError):
            self.remote.layer_bytes(blob, row, "sha256:" + "0" * 64)
        with self.assertRaises(ValueError):
            self.remote.layer_bytes(blob + b"changed", row, "sha256:" + self.remote.digest(raw))

    def test_apk_base_completa_no_se_deduce_de_una_etiqueta(self):
        packages = "".join(f"P:pkg{i}\nV:1.0-r0\nA:x86_64\n\n" for i in range(45)).encode()
        self.assertEqual(len(self.remote.apk_inventory(packages)), 45)
        with self.assertRaises(ValueError):
            self.remote.apk_inventory(packages + b"P:pkg0\nV:2-r0\nA:x86_64\n\n")
        with self.assertRaises(ValueError):
            self.remote.apk_inventory(packages.replace(b"V:1.0-r0\n", b"", 1))

    def test_proyeccion_excluye_headers_y_no_acepta_una_cuarta_biblioteca(self):
        candidates = {
            "usr/local/lib/postgresql/" + name: {"size": 1, "sha256": "a" * 64}
            for name in ("vector.so", "vchord.so", "libsqlite3.so.0")
        }
        for name in ("vector.control", "vector--0.8.5.sql", "vchord.control", "vchord--1.1.1.sql"):
            candidates["usr/local/share/postgresql/extension/" + name] = {
                "size": 1,
                "sha256": "a" * 64,
            }
        for name in self.remote.LICENSES:
            candidates["usr/local/share/licenses/agency-postgres-immich/" + name] = {
                "size": 1,
                "sha256": "a" * 64,
            }
        header = "usr/local/include/postgresql/server/extension/vector/vector.h"
        candidates[header] = {"size": 1, "sha256": "a" * 64}
        selected, excluded = self.remote.project(candidates)
        self.assertNotIn(header, selected)
        self.assertEqual(excluded, [header])
        candidates["usr/local/lib/postgresql/libgcc_s.so.1"] = {"size": 1, "sha256": "a" * 64}
        with self.assertRaises(ValueError):
            self.remote.project(candidates)

    def test_gate_acepta_fallo_mas_postverify_y_stop_sin_fingir_compile_positivo(self):
        from unittest.mock import patch

        failed = self.remote.envelope(
            {
                "success": False,
                "stage": "compile",
                "cid": self.remote.CID,
                "source_sha256": self.remote.COMPILE_SOURCE,
                "profile_sha256": self.remote.COMPILE_PROFILE,
            }
        )
        stopped = self.remote.envelope(
            {
                "success": True,
                "stage": "stop",
                "cid": self.remote.CID,
                "source_sha256": self.remote.SDK_SOURCE,
                "plan_sha256": self.remote.SDK_PLAN,
                "stopped": True,
                "after_pid": 0,
            }
        )
        artifact = {"verified": True}
        verified = self.remote.envelope(
            {
                "success": True,
                "stage": self.remote.POSTVERIFY_STAGE,
                "cid": self.remote.CID,
                "source_sha256": self.remote.POSTVERIFY_SOURCE,
                "profile_sha256": self.remote.POSTVERIFY_PROFILE,
                "failed_compile_receipt_sha256": failed["receipt_sha256"],
                "stop_receipt_sha256": stopped["receipt_sha256"],
                "prior_postverify_failed_sha256": self.remote.PRIOR_POSTVERIFY_FAILED,
                "prior_acceptance_failed_sha256": self.remote.PRIOR_ACCEPTANCE_FAILED,
                "files_manifest_sha256": self.remote.FILES_MANIFEST_SHA,
                "sdk_executed": False,
                "compiler_executed": False,
                "artifact": artifact,
                "artifact_sha256": self.remote.digest(self.remote.canonical(artifact)),
            }
        )
        with (
            patch.object(self.remote, "FAILED_COMPILE_SHA", failed["receipt_sha256"]),
            patch.object(self.remote, "STOP_SHA", stopped["receipt_sha256"]),
            patch.object(self.remote, "POSTVERIFY_RECEIPT_SHA", verified["receipt_sha256"]),
            patch.object(
                self.remote,
                "POSTVERIFY_ARTIFACT_SHA",
                self.remote.digest(self.remote.canonical(artifact)),
            ),
        ):
            self.remote.require_gates(failed, stopped, verified)
            altered = self.remote.envelope(dict(failed["receipt"], success=True))
            with self.assertRaises(ValueError):
                self.remote.require_gates(altered, stopped, verified)
            altered = self.remote.envelope(dict(verified["receipt"], sdk_executed=True))
            with self.assertRaises(ValueError):
                self.remote.require_gates(failed, stopped, altered)

    def test_build_no_admite_prepare_recalculado_para_otra_fuente(self):
        prepared = self.remote.envelope(
            {
                "stage": "prepare",
                "success": True,
                "profile_sha256": "a" * 64,
                "source_sha256": "b" * 64,
            }
        )
        self.remote.validate_prepared(prepared, "a" * 64, "b" * 64)
        with self.assertRaises(ValueError):
            self.remote.validate_prepared(prepared, "a" * 64, "c" * 64)


class RuntimeLifecycle(unittest.TestCase):
    def setUp(self):
        self.remote = load("docker/postgres-immich/final_image_remote.py", "runtime_lifecycle")
        self.local = load("docker/postgres-immich/final_image.py", "runtime_local")

    def test_plan_predeterminado_no_abre_boveda_ni_ssh(self):
        import contextlib
        import io
        from unittest.mock import patch

        output = io.StringIO()
        with patch.object(self.local, "sdk_local") as sdk, contextlib.redirect_stdout(output):
            sdk.return_value.read_source.side_effect = lambda p: p.read_bytes()
            self.assertEqual(self.local.main([]), 0)
            sdk.return_value.read_values.assert_not_called()
            sdk.return_value.connect.assert_not_called()
        import json

        result = json.loads(output.getvalue())
        self.assertTrue(result["plan_only"])
        self.assertFalse(result["infrastructure_executed"])

    def test_backing_fallido_impide_conexion_y_build(self):
        import contextlib
        import io
        from unittest.mock import patch

        with (
            patch.object(self.local, "sdk_local") as sdk,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with patch.object(self.local, "read_values", return_value={}):
                self.assertEqual(self.local.main(["build"]), 1)
            sdk.return_value.connect.assert_not_called()

    def test_oci_rechaza_blob_alterado_y_miembros_enlace(self):
        import io
        import tarfile

        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            item = tarfile.TarInfo("blobs/sha256/" + "0" * 64)
            item.size = 4
            tar.addfile(item, io.BytesIO(b"bad!"))
        with self.assertRaisesRegex(ValueError, "alterado"):
            self.remote.read_oci(stream.getvalue())
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            item = tarfile.TarInfo("index.json")
            item.type, item.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(item)
        with self.assertRaises(ValueError):
            self.remote.read_oci(stream.getvalue())

    def test_rutas_fijas_de_build_no_aceptan_contextos_de_red_o_sdk(self):
        arguments = self.remote.build_arguments()
        self.assertIn(
            "pgbase=oci-layout://"
            + self.remote.RECORD
            + "/base-oci@sha256:"
            + self.remote.BASE_MANIFEST,
            arguments,
        )
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertNotIn("--secret", arguments)
        self.assertNotIn("--ssh", arguments)
        self.assertEqual(arguments[-1], self.remote.CONTEXT)

    def test_purge_de_archivo_ajeno_en_capa_final_es_rechazado(self):
        from unittest.mock import patch

        base = {
            "files": {"base-data": {"kind": "file", "sha256": "a", "size": 1}},
            "manifest": {"layers": []},
            "config": {"config": {}, "rootfs": {"diff_ids": []}},
        }
        final = copy.deepcopy(base)
        final["config"]["config"]["Labels"] = self.remote.LABELS
        del final["files"]["base-data"]
        with (
            patch.object(self.remote, "validate_base"),
            self.assertRaisesRegex(ValueError, "heredado"),
        ):
            self.remote.verify_final(base, final, {})


class OciRoundTrip(unittest.TestCase):
    def test_esquema_oci_y_capas_validan_toda_la_cadena(self):
        import io
        import json
        import tarfile

        remote = load("docker/postgres-immich/final_image_remote.py", "runtime_round_trip")

        def tar(rows):
            target = io.BytesIO()
            with tarfile.open(fileobj=target, mode="w") as archive:
                for name, value in rows.items():
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(value), 0o644
                    archive.addfile(info, io.BytesIO(value))
            return target.getvalue()

        layer = tar({"etc/os-release": b"ID=alpine\n", "own/data": b"public"})

        def descriptor(data, kind):
            return {"digest": "sha256:" + remote.digest(data), "size": len(data), "mediaType": kind}

        config = remote.canonical(
            {
                "architecture": "amd64",
                "os": "linux",
                "config": {},
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + remote.digest(layer)]},
            }
        )
        manifest = remote.canonical(
            {
                "schemaVersion": 2,
                "config": descriptor(config, "application/vnd.oci.image.config.v1+json"),
                "layers": [descriptor(layer, "application/vnd.oci.image.layer.v1.tar")],
            }
        )
        index = remote.canonical(
            {
                "schemaVersion": 2,
                "manifests": [descriptor(manifest, "application/vnd.oci.image.manifest.v1+json")],
            }
        )
        rows = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}', "index.json": index}
        for blob in (config, manifest, layer):
            rows["blobs/sha256/" + remote.digest(blob)] = blob
        value = remote.read_oci(tar(rows), remote.digest(manifest))
        self.assertEqual(value["files"]["own/data"]["sha256"], remote.digest(b"public"))
        self.assertEqual(value["config_digest"], "sha256:" + remote.digest(config))
        # Una referencia de índice distinta se rechaza aunque todos los blobs físicos sean válidos.
        changed = json.loads(index)
        changed["manifests"][0]["size"] += 1
        rows["index.json"] = remote.canonical(changed)
        with self.assertRaisesRegex(ValueError, "tamaño"):
            remote.read_oci(tar(rows), remote.digest(manifest))


class RuntimeFraming(unittest.TestCase):
    def test_main_llega_al_collector_v4_con_booleano_real(self):
        import contextlib
        import io
        import signal
        import struct
        import sys
        import time
        from unittest.mock import patch

        remote = load("docker/postgres-immich/final_image_remote.py", "runtime_framing")
        cli = load("docker/postgres-immich/run_offline_sdk_v4.py", "runtime_frozen_collector")

        class Input:
            buffer = io.BytesIO()

        class Channel:
            def __init__(self, raw):
                self.raw = raw

            def recv_ready(self):
                return bool(self.raw)

            def recv(self, _size):
                data, self.raw = self.raw, b""
                return data

            def recv_stderr_ready(self):
                return False

            def recv_stderr(self, _size):
                return b""

            def exit_status_ready(self):
                return True

            def recv_exit_status(self):
                return 0

        Input.buffer = io.BytesIO(struct.pack(">I", 2) + b"{}")
        result = remote.envelope({"success": True})
        output = io.StringIO()
        with (
            patch.object(sys, "stdin", Input()),
            patch.object(remote, "apply", return_value=result),
            patch.object(signal, "SIGALRM", 14, create=True),
            patch.object(signal, "signal"),
            patch.object(signal, "alarm", create=True),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(remote.main(), 0)
        framed = cli.collect(Channel(output.getvalue().encode()), time.monotonic() + 1, 65536)
        self.assertIs(framed["success"], True)
        self.assertEqual(framed["result"], result)


class BuildIdentity(unittest.TestCase):
    def test_iidfile_prioriza_config_y_conserva_manifiesto_distinto(self):
        remote = load("docker/postgres-immich/final_image_remote.py", "runtime_build_identity")
        metadata = {
            "containerimage.config.digest": "sha256:" + "a" * 64,
            "containerimage.digest": "sha256:" + "b" * 64,
        }
        ids = remote.build_identity(metadata, "sha256:" + "a" * 64)
        self.assertEqual(
            ids,
            {
                "manifest": metadata["containerimage.digest"],
                "config": metadata["containerimage.config.digest"],
                "iidfile": metadata["containerimage.config.digest"],
            },
        )
        with self.assertRaises(ValueError):
            remote.build_identity(metadata, metadata["containerimage.digest"])


class RecoveryRuntimeDelta(unittest.TestCase):
    def setUp(self):
        self.remote = load("docker/postgres-immich/final_image_remote.py", "runtime_recover_delta")

    def test_perfil_envuelto_conserva_sdk_y_compile_historicos(self):
        from unittest.mock import patch

        base = {"sdk_plan": {"layout": "fijado"}, "compile_profile": {"history": "literal"}}
        wrapper = {"base_profile": base, "failed_acceptance": {"preserved": True}}
        profile = {
            "sdk_plan": base["sdk_plan"],
            "compile_profile": base["compile_profile"],
            "postverify_profile": wrapper,
        }
        with patch.object(
            self.remote, "POSTVERIFY_PROFILE", self.remote.digest(self.remote.canonical(wrapper))
        ):
            self.assertEqual(self.remote.postverify_history(profile), base)
            changed = copy.deepcopy(profile)
            changed["sdk_plan"] = {"layout": "otro"}
            with self.assertRaises(ValueError):
                self.remote.postverify_history(changed)

    def test_promocion_pasa_path_completo_al_lector_exacto_de_ambas_bibliotecas(self):
        remote = self.remote

        class Verifier:
            @staticmethod
            def read_owned(_normal, _parent, leaf, *, maximum, logical_path):
                if (
                    logical_path
                    not in {
                        "usr/local/lib/postgresql/vchord.so",
                        "usr/local/lib/postgresql/libsqlite3.so.0",
                    }
                    or leaf != logical_path.rsplit("/", 1)[-1]
                ):
                    raise ValueError("La excepción no recibió el path completo.")
                return b"contenido de prueba", {"mode": 0o700}

        expected = {
            "size": len(b"contenido de prueba"),
            "sha256": remote.digest(b"contenido de prueba"),
        }
        for leaf in ("vchord.so", "libsqlite3.so.0"):
            name = "usr/local/lib/postgresql/" + leaf
            self.assertEqual(
                remote.read_overlay_source(None, Verifier(), 123, leaf, name, expected),
                b"contenido de prueba",
            )
        with self.assertRaises(ValueError):
            remote.read_overlay_source(None, Verifier(), 123, "vchord.so", "vchord.so", expected)


class RuntimeVaultLimit(unittest.TestCase):
    def test_lector_8mib_acepta_mas_2mib_y_rechaza_exceso_e_irregular(self):
        import tempfile
        import types
        from unittest.mock import patch

        local = load("docker/postgres-immich/final_image.py", "runtime_vault_reader")
        sdk = local.sdk_local()
        parser = sdk.load(
            "runtime_real_env_parser",
            ROOT / "scripts/ci/check_env.py",
            "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda",
        )
        pins = []

        def pinned_loader(_name, _path, expected):
            pins.append(expected)
            return parser

        provider = types.SimpleNamespace(load=pinned_loader, read_source=sdk.read_source)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(local, "ROOT", Path(directory)),
        ):
            target = Path(directory) / ".env"
            target.write_text("PRUEBA_TAMANIO=" + "a" * (3 * 1024**2) + "\n", encoding="utf-8")
            values = local.read_values(provider)
            self.assertEqual(len(values["PRUEBA_TAMANIO"]), 3 * 1024**2)
            target.write_bytes(b"x" * (8 * 1024**2 + 1))
            with self.assertRaises(ValueError):
                local.read_values(provider)
            target.unlink()
            target.mkdir()
            with self.assertRaises((OSError, ValueError)):
                local.read_values(provider)
        self.assertTrue(pins)
        self.assertTrue(all(pin == local.ENV_PARSER_SHA for pin in pins))


if __name__ == "__main__":
    unittest.main()
