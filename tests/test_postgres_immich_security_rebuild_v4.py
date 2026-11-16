"""Contrato del rebuild propio; sin SSH, SDK, SQL ni Docker real."""

import ast
import contextlib
import importlib.util
import io
import os
import stat
import tarfile
import tempfile
import tomllib
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from postgres_immich_test_fixtures import local_owner_as_root

BASE = Path(__file__).resolve().parents[1]
REMOTE = BASE / "docker/postgres-immich/security_rebuild_remote_v4.py"
SPEC = importlib.util.spec_from_file_location("security_rebuild_remote_test", REMOTE)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
LOCAL_SPEC = importlib.util.spec_from_file_location(
    "security_rebuild_local_test", REMOTE.with_name("security_rebuild_v4.py")
)
L = importlib.util.module_from_spec(LOCAL_SPEC)
LOCAL_SPEC.loader.exec_module(L)


class ContractTests(unittest.TestCase):
    def test_vchord_graph_argv_explicitly_requires_pg16_offline_frozen(self):
        args = M.graph_arguments("vchord")
        self.assertEqual(args[args.index("-p") + 1], "vchord")
        self.assertEqual(args[args.index("--features") + 1], "pg16")
        self.assertEqual(args.count("--features"), 1)
        self.assertIn("--offline", args)
        self.assertIn("--frozen", args)
        self.assertEqual(args[args.index("--target") + 1], "x86_64-unknown-linux-musl")
        self.assertEqual(args[args.index("--manifest-path") + 1], M.WORK + "/source/Cargo.toml")
        for bad in ["vchord --features pg16", "other", "", None, True]:
            with self.assertRaises(ValueError):
                M.graph_arguments(bad)

    def test_xtask_graph_argv_matches_frozen_v3_except_new_workspace(self):
        raw = (BASE / "docker/postgres-immich/security_rebuild_remote_v3.py").read_bytes()
        self.assertEqual(
            M.digest(raw), "5fe9150ca424cb844037980ef35c500723eff60e81ccb4885e3fb84451e8d8f6"
        )
        graph = next(
            node
            for node in ast.parse(raw).body
            if isinstance(node, ast.FunctionDef) and node.name == "graph_inside"
        )
        call = next(
            node
            for node in ast.walk(graph)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "cargo_step"
        )
        self.assertIsInstance(call.args[1], ast.List)
        self.assertTrue(
            all(
                isinstance(node, (ast.List, ast.Load, ast.Constant, ast.Name, ast.BinOp, ast.Add))
                for node in ast.walk(call.args[1])
            )
        )
        expected = eval(
            compile(ast.Expression(call.args[1]), "<argv-fijado-v3>", "eval"),
            {"__builtins__": {}, "WORK": M.WORK, "package": "xtask"},
        )
        self.assertEqual(M.graph_arguments("xtask"), expected)
        self.assertNotIn("--features", M.graph_arguments("xtask"))

    def test_graph_execution_uses_constructor_and_keeps_feature_comparison_strict(self):
        features = "vchord v0.0.0 (/work/source)|default,pg16\nsimd v0.0.0 (/work/source/crates/simd)|init\npgrx v0.17.0|cshim,pg16\n"
        lock = b'[[package]]\nname="vchord"\nversion="0.0.0"\n[[package]]\nname="simd"\nversion="0.0.0"\n[[package]]\nname="pgrx"\nversion="0.17.0"\n[[package]]\nname="xtask"\nversion="0.0.0"\n'
        old = {
            "feature_wrapper_b64": M.base64.b64encode(M.canonical({"features": features})).decode(),
            "backed": {
                "graph": {"receipt": {"artifact": {"trees": {"xtask": [["xtask", "0.0.0"]]}}}}
            },
        }
        for bad in [False, True]:
            calls = []

            def cargo(_normal, args, _cwd, _deadline, _filename, calls=calls, bad=bad):
                calls.append(args)
                if args[args.index("-p") + 1] == "xtask":
                    return ("xtask v0.0.0 (" + M.WORK + "/source/crates/xtask)|\n").encode(), {
                        "fixture": True
                    }
                actual = features.replace("/work/source", M.WORK + "/source")
                if bad:
                    actual = actual.replace("default,pg16", "default", 1)
                return actual.encode(), {"fixture": True}

            with (
                patch.object(M, "inside_read", return_value=lock),
                patch.object(M, "cargo_step", side_effect=cargo),
                patch.object(M, "graph_arguments", wraps=M.graph_arguments) as constructor,
            ):
                if bad:
                    with self.assertRaisesRegex(ValueError, "grafo efectivo"):
                        M.graph_inside({}, None, None, old, 0)
                else:
                    self.assertTrue(M.graph_inside({}, None, None, old, 0)["expected_graph_match"])
                self.assertEqual(
                    [row.args[0] for row in constructor.call_args_list], ["vchord", "xtask"]
                )
            self.assertEqual(calls[0][calls[0].index("--features") + 1], "pg16")
            self.assertNotIn("--features", calls[1])

    def original_archive(self):
        return (
            BASE
            / ".tools/postgres-immich-build/vendor-8d5a521ef8b2827f/downloads/vectorchord-source.tar.gz"
        ).read_bytes()

    def frozen_source_recipe(self):
        return (BASE / "docker/postgres-immich/offline_recipe.py").read_bytes()

    def archive_fixture(self, rows):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            for name, kind, body, mode in rows:
                member = tarfile.TarInfo(name)
                member.type, member.mode = kind, mode
                member.size = len(body) if kind == tarfile.REGTYPE else 0
                member.linkname = "outside" if kind in {tarfile.LNKTYPE, tarfile.SYMTYPE} else ""
                archive.addfile(member, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
        return raw.getvalue()

    def test_source_archive_775_becomes_private_without_losing_exec_bits(self):
        payload = M.source_archive_payloads(self.original_archive())
        self.assertEqual(len(payload), 259)
        self.assertEqual(payload[".github/dependabot.yml"][1], 0o600)
        with tarfile.open(fileobj=io.BytesIO(self.original_archive()), mode="r:gz") as archive:
            executable = {
                member.name[len(M.SOURCE_PREFIX) :]
                for member in archive
                if member.isfile() and member.mode & 0o111
            }
        self.assertEqual({name for name, (_, mode) in payload.items() if mode == 0o700}, executable)
        self.assertEqual(len(executable), 2)
        self.assertTrue(all(mode in {0o600, 0o700} for _, mode in payload.values()))

    def test_source_tar_rejects_links_devices_duplicates_and_escapes(self):
        prefix = "VectorChord-fixed/"
        root = (prefix.rstrip("/"), tarfile.DIRTYPE, b"", 0o775)
        good = (prefix + "file", tarfile.REGTYPE, b"first", 0o664)
        bad_cases = [
            [root, good, good],
            [root, (prefix + "link", tarfile.SYMTYPE, b"", 0o777)],
            [root, (prefix + "hard", tarfile.LNKTYPE, b"", 0o664)],
            [root, (prefix + "device", tarfile.CHRTYPE, b"", 0o600)],
            [root, (prefix + "../escape", tarfile.REGTYPE, b"no", 0o664)],
            [root, ("/absolute", tarfile.REGTYPE, b"no", 0o664)],
            [root, ("OtherRoot/file", tarfile.REGTYPE, b"no", 0o664)],
            [
                root,
                (prefix + "dir", tarfile.REGTYPE, b"no", 0o664),
                (prefix + "dir/child", tarfile.REGTYPE, b"no", 0o664),
            ],
        ]
        for rows in bad_cases:
            with (
                self.subTest(rows=[row[0] for row in rows]),
                tarfile.open(fileobj=io.BytesIO(self.archive_fixture(rows)), mode="r:") as archive,
                self.assertRaises(ValueError),
            ):
                M.source_members(archive, prefix)

    def test_source_archive_pin_rejects_before_interpreting_changed_bytes(self):
        raw = self.original_archive()
        for changed in [raw[:-1], raw + b"x", b"not a source archive"]:
            with self.assertRaises(ValueError):
                M.source_archive_payloads(changed)

    def test_private_source_derives_exact_xtask_lock_and_config_overlays(self):
        before = self.original_archive()
        payload = M.source_archive_payloads(before)
        expected = {name: M.digest(raw) for name, (raw, _) in payload.items()}
        expected["crates/xtask/src/main.rs"] = M.XTASK_SHA
        old_config = b"old frozen configuration\n"
        expected[".cargo/config.toml"] = M.digest(old_config)
        lock = b"new fixed lock\n"
        output = M.derive_private_source(
            before, lock, self.frozen_source_recipe(), expected, old_config
        )
        self.assertEqual(len(output), 260)
        self.assertEqual(output["Cargo.lock"], (lock, 0o600))
        self.assertEqual(output[".cargo/config.toml"], (M.NEW_CONFIG, 0o600))
        self.assertEqual(M.digest(output["crates/xtask/src/main.rs"][0]), M.XTASK_SHA)
        self.assertEqual(before, self.original_archive())
        for name in payload.keys() - {"Cargo.lock", "crates/xtask/src/main.rs"}:
            self.assertEqual(output[name], payload[name])

    def test_source_derivation_never_reads_historical_source_files(self):
        raw = self.original_archive()
        payload = M.source_archive_payloads(raw)
        expected = {name: M.digest(body) for name, (body, _) in payload.items()}
        expected["crates/xtask/src/main.rs"] = M.XTASK_SHA
        expected[".cargo/config.toml"] = M.digest(b"old")
        with patch.object(M, "inside_read", side_effect=AssertionError("NO_OLD_SOURCE_IO")):
            M.derive_private_source(raw, b"new lock", self.frozen_source_recipe(), expected, b"old")

    def test_source_overlays_reject_recipe_tamper_and_historical_hash_mismatch(self):
        raw = self.original_archive()
        payload = M.source_archive_payloads(raw)
        expected = {name: M.digest(body) for name, (body, _) in payload.items()}
        expected["crates/xtask/src/main.rs"] = M.XTASK_SHA
        expected[".cargo/config.toml"] = M.digest(b"old")
        for recipe, observed in [
            (self.frozen_source_recipe() + b"#changed", expected),
            (self.frozen_source_recipe(), {**expected, "Cargo.toml": "0" * 64}),
        ]:
            with self.assertRaises(ValueError):
                M.derive_private_source(raw, b"lock", recipe, observed, b"old")

    def test_historical_775_ancestor_is_still_rejected_without_opening_file(self):
        def metadata(path):
            return types.SimpleNamespace(
                st_mode=stat.S_IFDIR | (0o775 if path.name == ".github" else 0o700), st_uid=0
            )

        with (
            patch.object(M.Path, "lstat", metadata),
            patch.object(M.os, "open") as opened,
            self.assertRaisesRegex(ValueError, "Ancestro SDK inseguro"),
        ):
            M.inside_read("/work/source/.github/dependabot.yml")
        opened.assert_not_called()

    def features(self):
        return "\n".join(
            [
                f"vchord v0.0.0 ({M.WORK}/source)|default,pg16",
                f"simd v0.0.0 ({M.WORK}/source/crates/simd)|init",
                "pgrx v0.17.0|cshim,pg16",
                "anyhow v1.0.103|default,std",
                "crossbeam-epoch v0.9.20|alloc,std",
            ]
        )

    def test_parser_acepta_solo_workspace_exacto_y_paquetes_autorizados(self):
        allowed = {
            ("vchord", "0.0.0"),
            ("simd", "0.0.0"),
            ("pgrx", "0.17.0"),
            ("anyhow", "1.0.103"),
            ("crossbeam-epoch", "0.9.20"),
        }
        value = M.parse_features(self.features(), allowed)
        self.assertEqual(value[("crossbeam-epoch", "0.9.20")], {"alloc", "std"})
        for text in [
            self.features().replace(M.WORK + "/source", "/work/source"),
            self.features().replace("/source/crates/simd", "/source-other/crates/simd"),
            self.features().replace("1.0.103", "1.0.101"),
            self.features() + "\ngit2 v0.20.0|default",
        ]:
            with self.subTest(text=text[-80:]), self.assertRaises(ValueError):
                M.parse_features(text, allowed)

    def test_parser_rechaza_duplicados_incompatibles_y_escape(self):
        allowed = {
            ("vchord", "0.0.0"),
            ("simd", "0.0.0"),
            ("pgrx", "0.17.0"),
            ("anyhow", "1.0.103"),
            ("crossbeam-epoch", "0.9.20"),
        }
        for text in [
            self.features().replace("default,std", "default,\x1bstd"),
            self.features().replace("/source/crates/simd", "/source/../simd"),
        ]:
            with self.assertRaises(ValueError):
                M.parse_features(text, allowed)

    def test_entorno_no_hereda_secretos_wrappers_flags_ni_proxy(self):
        before = M.build_environment()
        with patch.dict(
            os.environ,
            {
                "TOKEN": "private",
                "RUSTC_WRAPPER": "evil",
                "HTTPS_PROXY": "evil",
                "CARGO_ENCODED_RUSTFLAGS": "evil",
            },
        ):
            after = M.build_environment()
        self.assertEqual(before, after)
        self.assertNotIn("TOKEN", after)
        self.assertNotIn("RUSTC_WRAPPER", after)
        self.assertNotIn("HTTPS_PROXY", after)
        self.assertNotIn("CARGO_ENCODED_RUSTFLAGS", after)
        self.assertEqual(after["CARGO_TARGET_DIR"], M.WORK + "/target")
        self.assertEqual(after["RUSTC"], "/opt/agency-rust/bin/rustc")
        self.assertEqual(after["RUSTFMT"], "/opt/agency-rustfmt-v1/bin/rustfmt")

    def test_harness_cuatro_paquetes_sin_deps_nuevas_y_scope_honesto(self):
        files, scope = M.regression_inputs()
        manifest = tomllib.loads(files["Cargo.toml"].decode())
        lock = tomllib.loads(files["Cargo.lock"].decode())
        self.assertEqual(set(manifest["dependencies"]), {"anyhow", "crossbeam-epoch"})
        self.assertEqual(
            {(row["name"], row["version"]) for row in lock["package"]},
            {
                ("agency-native-security-regression", "0.0.0"),
                ("anyhow", "1.0.103"),
                ("crossbeam-epoch", "0.9.20"),
                ("crossbeam-utils", "0.8.21"),
            },
        )
        self.assertIn(b"downcast_mut", files["src/lib.rs"])
        self.assertIn(b"Atomic::<usize>::null", files["src/lib.rs"])
        self.assertFalse(scope["anyhow_ub_miri_proven"])
        self.assertFalse(scope["executed"])

    def test_stage_gates_y_stop_independiente(self):
        for mode in ["restart", "materialize", "graph", "regression", "build", "catalog"]:
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                M.require_previous(mode, {}, reviewed=False)
        M.require_previous("stop", {}, reviewed=False)
        M.require_previous("receive", {}, reviewed=False)
        previous = {
            name: {"success": True}
            for name in ["prepare", "restart", "materialize", "graph", "regression", "build"]
        }
        with self.assertRaises(ValueError):
            M.require_previous("build", previous, reviewed=False)
        M.require_previous("build", previous, reviewed=True)

    def test_new_role_rejects_failed_or_prior_role_positive_receipts(self):
        row = {
            "owner": M.OWNER,
            "stage": "materialize",
            "success": True,
            "profile_sha256": "a" * 64,
        }
        self.assertEqual(
            M.verified_saved(
                {"profile_sha256": "a" * 64, "backed": {"materialize": M.envelope(row)}}
            )["materialize"],
            row,
        )
        for change in [
            {"success": False},
            {"owner": "agencia-digital-marketing-ai:postgres-immich-security-rebuild:v1"},
            {"owner": "agencia-digital-marketing-ai:postgres-immich-security-rebuild:v2"},
            {"profile_sha256": "b" * 64},
        ]:
            changed = {**row, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                M.verified_saved(
                    {"profile_sha256": "a" * 64, "backed": {"materialize": M.envelope(changed)}}
                )

    def test_reinicio_no_adopta_running_ni_creacion_ajena(self):
        old = {"cid": M.CID, "created": "fixed", "image_id": M.IMAGE}
        M.check_birth({**old, "running": False}, old, running=False)
        for observed in [
            {**old, "running": True},
            {**old, "cid": "f" * 64, "running": False},
            {**old, "created": "other", "running": False},
        ]:
            with self.assertRaises(ValueError):
                M.check_birth(observed, old, running=False)

    def test_argv_solo_modos_cerrados_y_no_recompila_sqlite_pgvector(self):
        command = M.exec_arguments("build", "a" * 64, "b" * 64)
        self.assertEqual(command[:1], ["exec"])
        self.assertIn(M.CID, command)
        self.assertIn("/usr/bin/env", command)
        self.assertIn("-i", command)
        self.assertEqual(command[-2:], [M.WORK + "/recipe.sh", "build"])
        for bad in ["sdk", "make", "sh -c rm", "inside-build"]:
            with self.assertRaises(ValueError):
                M.exec_arguments(bad, "a" * 64, "b" * 64)

    def test_soporte_fijado_se_compila_por_bytes_sin_pyc(self):
        raw = b"value = 7\n"
        with patch("importlib.util.spec_from_file_location", side_effect=AssertionError("pyc")):
            module = M.module_bytes("fixed", raw, M.digest(raw))
        self.assertEqual(module.value, 7)
        with self.assertRaises(ValueError):
            M.module_bytes("bad", raw + b"x", M.digest(raw))

    def test_recepcion_exige_sha_y_no_acepta_bytes_adicionales(self):
        meta = {"a": {"size": 3, "sha256": M.digest(b"abc")}}
        self.assertEqual(M.read_payload(io.BytesIO(b"abc"), meta), {"a": b"abc"})
        for value in [b"abd", b"ab", b"abcx"]:
            with self.assertRaises(ValueError):
                M.read_payload(io.BytesIO(value), meta)
        with self.assertRaises(ValueError):
            M.read_payload(io.BytesIO(b"abc"), {"../a": meta["a"]})

    def test_catalogo_reutiliza_bibliotecas_solo_si_hay_sha_exactos(self):
        before = {"libsqlite3.so.0": "a" * 64, "vector.so": "b" * 64}
        M.check_reused(before, before)
        with self.assertRaises(ValueError):
            M.check_reused({**before, "vector.so": "c" * 64}, before)

    def test_fallo_post_start_detiene_solo_la_instancia_ya_acreditada(self):
        origin = {"cid": M.CID, "image_id": M.IMAGE, "created": "fixed"}
        for foreign in [False, True]:
            calls = []
            first = {**origin, "running": False}
            if foreign:
                first["created"] = "other"
            docker = types.SimpleNamespace(
                inspect=lambda cid, first=first: first, command=lambda *a, **k: {"returncode": 0}
            )
            normal = types.SimpleNamespace(
                check_container=lambda value, cid: value,
                stop_owned=lambda *args, calls=calls: calls.append("stop") or {"stopped": True},
                write_file=lambda *args: None,
            )
            request = {
                "action": "restart",
                "profile_sha256": "a" * 64,
                "profile": {"original_container": origin},
            }
            with (
                self.subTest(foreign=foreign),
                patch.object(M, "control_open", return_value=42),
                patch.object(M, "check_control", return_value=docker),
                patch.object(M, "daemon_identity", return_value="daemon"),
                patch.object(M, "operation_lock", return_value=contextlib.nullcontext()),
                patch.object(M.os, "mkdir"),
                patch.object(M.os, "close"),
                patch.object(M.signal, "alarm", return_value=0, create=True),
            ):
                with self.assertRaises(ValueError):
                    M.operating(
                        request,
                        normal,
                        None,
                        None,
                        None,
                        None,
                        {"prepare": {"daemon_id": "daemon"}},
                    )
            self.assertEqual(calls, [] if foreign else ["stop"])

    def test_regresion_no_acepta_cero_tests_o_solo_exit_cero(self):
        raw = b"test tests::anyhow_context_downcast_mut_behavior ... ok\ntest tests::epoch_official_format_null ... ok\ntest result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.0s\n"
        M.check_regression_output(raw)
        for invalid in [
            b"",
            raw.replace(b"2 passed", b"0 passed"),
            raw.replace(
                b"epoch_official_format_null ... ok", b"epoch_official_format_null ... ignored"
            ),
        ]:
            with self.assertRaises(ValueError):
                M.check_regression_output(invalid)

    def regression_log(self):
        return (
            b"running 2 tests\n"
            b"test tests::anyhow_context_downcast_mut_behavior ... ok\n"
            b"test tests::epoch_official_format_null ... ok\n\n"
            b"test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; "
            b"0 filtered out; finished in 0.00s\n"
        )

    def test_regresion_rechaza_lineas_de_prueba_con_prefijo_o_sufijo(self):
        raw = self.regression_log()
        for name in [b"anyhow_context_downcast_mut_behavior", b"epoch_official_format_null"]:
            line = b"test tests::" + name + b" ... ok"
            for changed in [b"prefix " + line, line + b" trailing", b" " + line]:
                with self.subTest(line=changed), self.assertRaises(ValueError):
                    M.check_regression_output(raw.replace(line, changed))

    def test_regresion_rechaza_resultado_failed_aunque_existan_dos_ok(self):
        raw = self.regression_log()
        for failure in [
            b"test result: FAILED. 0 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out;\n",
            b"prefix test result: FAILED. 0 passed; 2 failed;\n",
            b"test tests::another ... FAILED\n",
            b"failures:\n    tests::epoch_official_format_null\n",
        ]:
            for changed in [failure + raw, raw + failure]:
                with self.subTest(failure=failure), self.assertRaises(ValueError):
                    M.check_regression_output(changed)

    def test_regresion_rechaza_pruebas_duplicadas_o_adicionales(self):
        raw = self.regression_log()
        for line in [
            b"test tests::anyhow_context_downcast_mut_behavior ... ok\n",
            b"test tests::epoch_official_format_null ... ok\n",
            b"test tests::another ... ok\n",
            b"test other::another ... ignored\n",
        ]:
            with self.subTest(line=line), self.assertRaises(ValueError):
                M.check_regression_output(line + raw)

    def test_regresion_exige_resumen_unico_completo_y_sin_prefijo(self):
        raw = self.regression_log()
        summary = raw.splitlines()[-1]
        for changed in [
            b"prefix " + summary,
            b" " + summary,
            summary + b" trailing",
            summary + b"\n" + summary,
            summary.replace(b"finished in 0.00s", b"finished in NaNs"),
            summary.replace(b"finished in 0.00s", b"finished in -1.00s"),
            summary.removesuffix(b" finished in 0.00s"),
        ]:
            with self.subTest(summary=changed), self.assertRaises(ValueError):
                M.check_regression_output(raw.replace(summary, changed))

    def test_regresion_rechaza_controles_y_utf8_invalido(self):
        raw = self.regression_log()
        for changed in [
            raw.replace(b"\n", b"\v"),
            raw.replace(b"\n", b"\r"),
            raw.replace(b" ... ok", b" ... ok\x1b[0m", 1),
            raw + b"\xff",
        ]:
            with self.subTest(raw=changed[-80:]), self.assertRaises(ValueError):
                M.check_regression_output(changed)

    def test_regresion_acepta_solo_registros_enteros_lf_o_crlf(self):
        raw = self.regression_log()
        banner = b"   Compiling agency-native-security-regression v0.0.0\n"
        for valid in [raw, raw.rstrip(b"\n"), raw.replace(b"\n", b"\r\n"), banner + raw]:
            with self.subTest(raw=valid[:60]):
                M.check_regression_output(valid)

    def test_hashes_dentro_sdk_antes_de_ejecutar_receta(self):
        expected = {M.WORK + "/recipe.sh": "a" * 64, M.WORK + "/verifier.py": "b" * 64}
        raw = (
            "a" * 64 + "  " + M.WORK + "/recipe.sh\n" + "b" * 64 + "  " + M.WORK + "/verifier.py\n"
        ).encode()
        M.check_exec_hashes(raw, expected)
        for invalid in [
            raw + raw,
            raw.replace(b"verifier.py", b"other.py"),
            raw.replace(b"a" * 64, b"c" * 64),
        ]:
            with self.assertRaises(ValueError):
                M.check_exec_hashes(invalid, expected)

    def test_sca_sdk_aprobado_vigente_ligado_a_instancia_y_formatter(self):
        now = datetime(2026, 11, 6, 18, tzinfo=UTC)
        value = {
            "schema_version": 1,
            "root_approved": True,
            "cid": M.CID,
            "created": "fixed",
            "apk_database_sha256": M.SDK_SECURITY_POLICY["apk_database_sha256"],
            "apk_package_count": 110,
            "apk_exact_coverage": True,
            "formatter_sha256": M.SDK_SECURITY_POLICY["formatter_sha256"],
            "driver_sha256": M.SDK_SECURITY_POLICY["driver_sha256"],
            "sdk_state_receipt_sha256": M.SDK_SECURITY_POLICY["state_receipt_sha256"],
            "high_critical_count": 0,
            "coverage_scope": "110 APK y formatter separado por bytes",
            "reports": [{"sha256": "a" * 64, "path": "own-report.json"}],
            "scanned_at_utc": "2026-11-06T17:00:00Z",
            "db_metadata": {
                "Version": 2,
                "UpdatedAt": "2026-11-06T12:55:00Z",
                "NextUpdate": "2026-11-07T12:55:00Z",
            },
        }
        approval = {
            "raw_b64": M.base64.b64encode(M.canonical(value)).decode(),
            "sha256": M.digest(M.canonical(value)),
        }
        profile = {"original_container": {"created": "fixed"}}
        M.validate_sdk_security(approval, profile, now=now)
        for field, bad in [
            ("root_approved", False),
            ("cid", "f" * 64),
            ("apk_package_count", 109),
            ("formatter_sha256", "0" * 64),
            ("apk_exact_coverage", False),
        ]:
            changed = dict(value)
            changed[field] = bad
            invalid = {
                "raw_b64": M.base64.b64encode(M.canonical(changed)).decode(),
                "sha256": M.digest(M.canonical(changed)),
            }
            with self.subTest(field=field), self.assertRaises(ValueError):
                M.validate_sdk_security(invalid, profile, now=now)
        with self.assertRaises(ValueError):
            M.validate_sdk_security(approval, profile, now=datetime(2026, 11, 8, tzinfo=UTC))


@unittest.skipUnless(os.name == "posix", "Pruebas dir_fd/flock requieren Linux real.")
class NativeFilesTests(unittest.TestCase):
    def test_archive_writer_creates_private_tree_excl_and_preserves_foreign(self):
        normal = L.load_reference("normal")
        raw = (
            BASE
            / ".tools/postgres-immich-build/vendor-8d5a521ef8b2827f/downloads/vectorchord-source.tar.gz"
        ).read_bytes()
        source = M.source_archive_payloads(raw)
        with local_owner_as_root(normal), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "foreign.txt").write_bytes(b"foreign-sentinel")
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for name in [
                    ".github/dependabot.yml",
                    next(name for name, (_, mode) in source.items() if mode == 0o700),
                ]:
                    body, mode = source[name]
                    M.inside_write(normal, fd, name, body, mode)
                    self.assertEqual((root / name).read_bytes(), body)
                    self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), mode)
                    with self.assertRaises(FileExistsError):
                        M.inside_write(normal, fd, name, b"changed", mode)
                self.assertEqual(stat.S_IMODE((root / ".github").stat().st_mode), 0o700)
                self.assertEqual((root / "foreign.txt").read_bytes(), b"foreign-sentinel")
            finally:
                os.close(fd)

    def test_excl_nofollow_no_sobrescribe_y_preserva_ajeno(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                M.write_new_at(anchor, "data", b"first")
                with self.assertRaises(FileExistsError):
                    M.write_new_at(anchor, "data", b"other")
                (root / "link").symlink_to(root / "data")
                with self.assertRaises(OSError):
                    M.write_new_at(anchor, "link", b"other")
                self.assertEqual((root / "data").read_bytes(), b"first")
            finally:
                os.close(anchor)

    def test_lock_no_bloquea_infinito(self):
        with tempfile.TemporaryDirectory() as directory:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with M.operation_lock(descriptor):
                    with self.assertRaises(BlockingIOError), M.operation_lock(descriptor):
                        self.fail("Lock tomado dos veces")
            finally:
                os.close(descriptor)


class LocalTests(unittest.TestCase):
    def test_remoto_se_valida_antes_de_ejecutar_sus_bytes(self):
        with (
            patch.object(L, "regular", return_value=b"raise AssertionError('ejecutado')\n"),
            patch.object(L, "module_raw", side_effect=AssertionError("No ejecutar bytes ajenos")),
        ):
            with self.assertRaises(ValueError):
                L.remote_contract("a" * 64)

    def test_plan_no_lee_boveda_y_operacion_exige_root_flag(self):
        with (
            patch.object(L, "proposal", return_value={"fixture": True}),
            patch.object(L, "read_values", side_effect=AssertionError("Bóveda")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(L.main(["plan"]), 0)
            self.assertEqual(L.main(["build"]), 1)

    def test_boveda_mayor_un_mib_sin_duplicar_credenciales(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.env"
            path.write_bytes(b"#" + b"x" * (1024 * 1024) + b"\nMEDIA_VM_IP=192.168.0.85\n")
            plan = {"references": {"env_parser": {"sha256": L.REFS["env_parser"][1]}}}
            acl = types.SimpleNamespace(check=lambda p: None)
            values = L.read_values(plan, path, acl=acl)
            self.assertEqual(values["MEDIA_VM_IP"], "192.168.0.85")
            path.write_bytes(b"x" * (8 * 1024**2 + 1))
            with self.assertRaises(ValueError):
                L.read_values(plan, path, acl=acl)

    def test_stop_no_depende_de_recibos_build_o_datos_del_derivado(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {"fixture": True, "sources": {"remote": L.digest(L.regular(REMOTE))}}
            receipt = {
                "success": True,
                "owner": M.OWNER,
                "stage": "prepare",
                "profile_sha256": M.digest(M.canonical(plan)),
            }
            path = root / "security-rebuild-v3.prepare.fixture.json"
            raw = M.canonical(M.envelope(receipt))
            path.write_bytes(raw)
            values = {
                L.PREFIX + "PREPARE_RECEIPT_PATH": str(path),
                L.PREFIX + "PREPARE_RECEIPT_SHA256": M.digest(raw),
                L.PREFIX + "BUILD_RECEIPT_PATH": "missing",
                L.PREFIX + "BUILD_RECEIPT_SHA256": "bad",
            }
            with (
                patch.object(L, "LOCAL_ROOT", root),
                patch.object(L, "proposal", side_effect=AssertionError("Derivado")),
            ):
                self.assertEqual(set(L.saved(values, "stop", plan)), {"prepare"})


if __name__ == "__main__":
    unittest.main()
