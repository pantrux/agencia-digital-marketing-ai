"""Contratos SQL aislados; las pruebas no conectan ni crean contenedores."""

import contextlib
import copy
import importlib.util
import io
import json
import struct
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def remote():
    spec = importlib.util.spec_from_file_location(
        "sql_accept_contract", ROOT / "docker/postgres-immich/sql_acceptance_remote_v2.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local():
    spec = importlib.util.spec_from_file_location(
        "sql_accept_local", ROOT / "docker/postgres-immich/sql_acceptance_v2.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile():
    module = remote()
    image = approval(datetime.now(UTC))["image"]
    parent = PARENT
    root = parent + "/sql-functional-v2"
    return {
        "schema_version": 2,
        "owner": module.OWNER,
        "target": {"hostname": "vm-media-01", "ip": "192.168.0.85", "user": "jandrade", "port": 22},
        "parent": {
            "path": parent,
            "directory": {"device": 2050, "inode": 2531296},
            "marker_sha256": "1133849a55253203097f1d330ea26964c54a294d02f58985a9f22f6da99e0a94",
        },
        "root": root,
        "data": root + "/data",
        "secrets": root + "/secrets",
        "name": "agencia-pg-immich-sql-v2-fixture",
        "image": image,
        "password_sha256": "e" * 64,
        "approval_sha256": "f" * 64,
        "sources": {"local": "1" * 64, "remote": "1" * 64, "sql": SCRIPT},
        "budgets": module.BUDGETS,
    }


def container():
    module, spec = remote(), profile()
    value = {
        "Id": "f" * 64,
        "Image": spec["image"]["id"],
        "Name": "/" + spec["name"],
        "Created": "2026-11-06T00:00:00Z",
        "Config": {
            "Image": spec["image"]["id"],
            "User": "70:70",
            "Entrypoint": ["docker-entrypoint.sh"],
            "Cmd": module.SERVER_ARGS,
            "Env": spec["image"]["env"] + module.DATABASE_ENV,
            "Labels": {
                **spec["image"]["labels"],
                "cl.e-scale.agencia.owner": module.OWNER,
                "cl.e-scale.agencia.profile": module.digest(module.canonical(spec)),
            },
        },
        "HostConfig": {
            "NetworkMode": "none",
            "NanoCpus": 2_000_000_000,
            "Memory": 2 * 1024**3,
            "MemorySwap": 2 * 1024**3,
            "PidsLimit": 128,
            "ShmSize": 128 * 1024**2,
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "RestartPolicy": {"Name": "no"},
            "IpcMode": "private",
            "Tmpfs": module.TMPFS,
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": spec["data"],
                    "Target": "/var/lib/postgresql/data",
                    "ReadOnly": False,
                },
                {
                    "Type": "bind",
                    "Source": spec["secrets"],
                    "Target": "/run/secrets",
                    "ReadOnly": True,
                },
            ],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": spec["data"],
                "Destination": "/var/lib/postgresql/data",
                "RW": True,
            },
            {"Type": "bind", "Source": spec["secrets"], "Destination": "/run/secrets", "RW": False},
        ],
        "State": {
            "Pid": 0,
            "Running": False,
            "Status": "created",
            "StartedAt": "0001-01-01T00:00:00Z",
        },
    }
    return value, spec


IMAGE = "sha256:a00b7ac5ed30069abde2702a2b2b3d8321ea0f22d2ba64cc9f836c86b4c47601"
CONFIG = "sha256:60331f9322af6e5449db67a68975e7ce57f58562af19843bc4de47ffd46f709f"
PARENT = "/var/lib/media-platform/build/postgres-immich-security-runtime-v3/security-v7-20261106-01"
SCRIPT = "3615b4132cdbc7157fc766ad88c326b1832d132ea7a05e1de0c8bce59899885b"


def approval(now):
    return {
        "schema_version": 2,
        "decision": "functional_test_only",
        "production_approved": False,
        "coverage_complete": False,
        "global_high_critical": None,
        "issued_at_utc": (now - timedelta(seconds=1)).isoformat(),
        "expires_at_utc": (now + timedelta(minutes=59)).isoformat(),
        "script_sha256": SCRIPT,
        "runtime": {
            "prepare": {
                "path": "prepare.json",
                "sha256": "a8ffd5244af037a40dfb5be2b924ab9c2ece75b7722cb7fe4cb9272e5e89f3fc",
            },
            "build": {
                "path": "build.json",
                "sha256": "f0f8fab22ffedfa804091ff42936657174c21b7aefc62d497bd30e25373b3d58",
            },
        },
        "abi_sha256": "c96f7f677e737daaf30b99bdc8e6fe9d5acd041143b1c9f2e676c5b72686627a",
        "image": {
            "id": IMAGE,
            "config_digest": CONFIG,
            "user": "70:70",
            "entrypoint": ["docker-entrypoint.sh"],
            "cmd": ["postgres"],
            "env": ["PGDATA=/var/lib/postgresql/data"],
            "labels": {
                "cl.e-scale.agencia.owner": "agencia-digital-marketing-ai:postgres-immich-security-runtime:v3"
            },
        },
    }


class FunctionalGate(unittest.TestCase):
    def setUp(self):
        self.module = remote()
        self.now = datetime(2026, 11, 6, 23, 0, tzinfo=UTC)

    def test_aprobacion_funcional_no_requiere_sca_completa_ni_ceros(self):
        value = approval(self.now)
        self.assertEqual(self.module.validate_approval(value, IMAGE, self.now), value["image"])
        self.assertFalse(value["coverage_complete"])
        self.assertIsNone(value["global_high_critical"])

    def test_produccion_cobertura_o_conteo_ficticio_se_rechazan(self):
        for key, bad in (
            ("decision", "approved_for_isolated_sql"),
            ("production_approved", True),
            ("coverage_complete", True),
            ("global_high_critical", 0),
            ("global_high_critical", False),
        ):
            value = approval(self.now)
            value[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.module.validate_approval(value, IMAGE, self.now)
        value = approval(self.now)
        value["reports"] = [{"clean": True}]
        with self.assertRaises(ValueError):
            self.module.validate_approval(value, IMAGE, self.now)

    def test_ventana_una_hora_y_zona_utc_estricta(self):
        for issued, expires in (
            (self.now, self.now + timedelta(seconds=3601)),
            (self.now, self.now),
            (self.now + timedelta(seconds=1), self.now + timedelta(minutes=5)),
        ):
            value = approval(self.now)
            value.update(issued_at_utc=issued.isoformat(), expires_at_utc=expires.isoformat())
            with self.subTest(issued=issued, expires=expires), self.assertRaises(ValueError):
                self.module.validate_approval(value, IMAGE, self.now)
        value = approval(self.now)
        value["issued_at_utc"] = "2026-11-06T22:59:59"
        with self.assertRaises(ValueError):
            self.module.validate_approval(value, IMAGE, self.now)

    def test_subject_config_script_y_runtime_pins_exactos(self):
        for target in ("image", "config", "sql", "prepare", "build", "abi"):
            value = approval(self.now)
            if target == "image":
                value["image"]["id"] = "sha256:" + "d" * 64
            elif target == "config":
                value["image"]["config_digest"] = "sha256:" + "d" * 64
            elif target == "sql":
                value["script_sha256"] = "d" * 64
            elif target == "abi":
                value["abi_sha256"] = "d" * 64
            else:
                value["runtime"][target]["sha256"] = "d" * 64
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.module.validate_approval(value, IMAGE, self.now)

    def test_perfil_rechaza_alias_de_parent_marker_y_sql(self):
        module = self.module
        value = profile()
        module.validate_profile(value)
        for target in ("path", "device", "inode", "marker", "sql"):
            candidate = copy.deepcopy(value)
            if target == "path":
                candidate["parent"]["path"] += "/../alias"
            elif target == "marker":
                candidate["parent"]["marker_sha256"] = "d" * 64
            elif target == "sql":
                candidate["sources"]["sql"] = "d" * 64
            else:
                candidate["parent"]["directory"][target] += 1
            with self.subTest(target=target), self.assertRaises(ValueError):
                module.validate_profile(candidate)

    def test_docker29_manifiesto_sin_anotacion_config_no_se_confunde_con_config_id(self):
        module = self.module
        spec = profile()
        value = {
            "Id": IMAGE,
            "Descriptor": {"digest": IMAGE},
            "Config": {
                "User": "70:70",
                "Entrypoint": ["docker-entrypoint.sh"],
                "Cmd": ["postgres"],
                "Env": spec["image"]["env"],
                "Labels": spec["image"]["labels"],
            },
        }
        self.assertEqual(
            module.check_image(value, spec)["config_evidence"], "pinned_runtime_build_and_manifest"
        )
        for target in ("id", "config", "descriptor", "env"):
            bad = copy.deepcopy(value)
            if target == "id":
                bad["Id"] = CONFIG
            elif target == "config":
                bad["Descriptor"]["annotations"] = {"config.digest": "sha256:" + "d" * 64}
            elif target == "descriptor":
                bad["Descriptor"]["digest"] = CONFIG
            else:
                bad["Config"]["Env"].append("EXTRA=true")
            with self.subTest(target=target), self.assertRaises(ValueError):
                module.check_image(bad, spec)

    def test_recibos_fisicos_rechazan_abi_y_correlacion_adulterados(self):
        module = self.module
        abi = {
            "verified": True,
            "sdk_libraries_copied": False,
            "resolved": {p: {"sha256": h} for p, h in module.NATIVE_HASHES.items()},
        }

        def row(stage):
            return {
                "success": True,
                "stage": stage,
                "owner": module.RUNTIME_OWNER,
                "source_sha256": "35dd1e95c4f4ee63885a382cff51bad8dcd1d0d9ba0336eaa897c85f06c1c8ad",
                "profile_sha256": "e79daf8ebf96151a24dddfd3bd4d727c9e1c0a67ee3dc8238ce6bbd9f6a6da0e",
                "abi": copy.deepcopy(abi),
            }

        prep = row("prepare")
        prep["directories"] = {"record": module.PARENT_PROOF}
        prepared = module.envelope(prep)
        build = row("build")
        build.update(
            image_id=IMAGE, config_digest=CONFIG, prepare_receipt_sha256=prepared["receipt_sha256"]
        )
        with patch.object(module, "ABI_SHA256", module.digest(module.canonical(abi))):
            module.validate_runtime_receipts(prepared, module.envelope(build))
            for target in ("abi", "parent", "source", "config", "prepare"):
                before = copy.deepcopy(prep)
                after = copy.deepcopy(build)
                if target == "abi":
                    after["abi"]["verified"] = False
                elif target == "parent":
                    before["directories"]["record"]["directory"]["inode"] += 1
                elif target == "source":
                    after["source_sha256"] = "d" * 64
                elif target == "config":
                    after["config_digest"] = IMAGE
                else:
                    after["prepare_receipt_sha256"] = "d" * 64
                with self.subTest(target=target), self.assertRaises(ValueError):
                    module.validate_runtime_receipts(
                        module.envelope(before), module.envelope(after)
                    )

    def test_store_open_exige_parent_fisico_antes_de_leer_el_hijo(self):
        module = self.module
        normal = unittest.mock.Mock()
        store = module.Store(normal, profile())
        with patch.object(
            module, "verify_runtime_parent", side_effect=ValueError("parent alterado")
        ):
            with self.assertRaises(ValueError):
                store.open()
        normal.open_directory.assert_not_called()


class Lifecycle(unittest.TestCase):
    def test_cid_debe_estar_respaldado_antes_de_start_y_db_no_bloquea_stop(self):
        module = remote()
        prepared = {"prepare": module.envelope({"success": True, "stage": "prepare"})}
        created = module.envelope({"success": True, "stage": "create", "cid": "f" * 64})
        actual = {**prepared, "create": created}
        with self.assertRaises(ValueError):
            module.require_backup("start1", actual, prepared)
        module.require_backup("start1", actual, actual)
        module.require_backup("stop1", actual, actual)

    def test_cleanup_solo_detiene_cid_acreditado_aunque_db_no_se_lea(self):
        module = remote()
        calls = []

        class Docker:
            alive = True

            def inspect(self, cid, timeout=2):
                return {"cid": cid, "running": self.alive, "pid": 100 if self.alive else 0}

            def command(self, args, timeout):
                calls.append(args)
                self.alive = False
                return {"returncode": 0}

        docker = Docker()
        from unittest.mock import patch

        with patch.object(module, "check_container", side_effect=lambda value, cid, profile: value):
            result = module.stop_owned(docker, "f" * 64, {})
        self.assertTrue(result["stopped"])
        self.assertEqual(calls, [["stop", "--time", "5", "f" * 64]])

    def test_parada_timeout_de_stop_hace_kill_solo_del_cid_y_con_plazo(self):
        module = remote()
        value, spec = container()
        value["State"].update(Pid=123, Running=True)
        calls = []

        class Docker:
            def inspect(self, cid, timeout=2):
                return copy.deepcopy(value)

            def command(self, args, timeout):
                calls.append((args, timeout))
                if args[0] == "stop":
                    raise TimeoutError("daemon no respondió")
                value["State"].update(Pid=0, Running=False)
                return {"returncode": 0}

        with patch.object(module.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 0, 0]):
            result = module.stop_owned(Docker(), "f" * 64, spec)
        self.assertTrue(result["kill_used"])
        self.assertEqual([args[0] for args, _ in calls], ["stop", "kill"])
        self.assertTrue(all(timeout <= 6 for _, timeout in calls))

    def test_resumen_incompleto_cluster_nuevo_o_salida_extra_no_acreditan_sql(self):
        module = remote()
        good = {
            "kind": "sql_acceptance",
            "verified": True,
            "server_version_num": 160015,
            "vectors": 4096,
            "dimension": 512,
            "queries": 5,
            "index_verified": True,
            "sentinel": "persistido",
            "cluster_id": "123456",
            "extensions": {
                "vector": "0.8.5",
                "vchord": "1.1.1",
                "cube": "1.5",
                "earthdistance": "1.2",
            },
        }
        module.validate_summary(json.dumps(good), "123456")
        for data, cluster in (
            (json.dumps(good), "123457"),
            ("{}", None),
            (json.dumps(good) + "\notra salida", None),
            ("[]", None),
        ):
            with self.subTest(data=data[:20], cluster=cluster), self.assertRaises(ValueError):
                module.validate_summary(data, cluster)

    def test_noforeign_rechaza_anonimo_red_uid_o_capacidad_adicional(self):
        module = remote()
        value, spec = container()
        self.assertTrue(module.check_container(value, "f" * 64, spec)["no_foreign"])
        for change in ("mount", "network", "uid", "cap", "capdrop", "duplicate"):
            candidate = copy.deepcopy(value)
            if change == "mount":
                candidate["Mounts"].append({"Type": "volume", "Destination": "/otra"})
            elif change == "network":
                candidate["HostConfig"]["NetworkMode"] = "host"
            elif change == "uid":
                candidate["Config"]["User"] = "0:0"
            elif change == "cap":
                candidate["HostConfig"]["CapAdd"] = ["NET_ADMIN"]
            elif change == "capdrop":
                candidate["HostConfig"]["CapDrop"] = ["ALL", "ALL"]
            else:
                candidate["Mounts"].append(candidate["Mounts"][0])
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.check_container(candidate, "f" * 64, spec)

    def test_secreto_invalido_no_crea_directorio_parcial(self):
        module = remote()
        normal = unittest.mock.Mock()
        store = unittest.mock.Mock(normal=normal, profile=profile())
        with self.assertRaises(ValueError):
            module.prepare(store, {"password": "CHANGE_ME"})
        normal.open_directory.assert_not_called()


class LocalBoundary(unittest.TestCase):
    def test_loader_ejecuta_raw_y_no_pyc_valido_con_cuerpo_distinto(self):
        import os
        import py_compile

        module = local()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.py"
            other = Path(directory) / "other.py"
            source.write_bytes(b"VALUE = 'SAFE'\n")
            other.write_bytes(b"VALUE = 'EVIL'\n")
            stamp = source.stat().st_mtime_ns
            os.utime(other, ns=(stamp, stamp))
            py_compile.compile(
                str(other), cfile=importlib.util.cache_from_source(str(source)), doraise=True
            )
            loaded = module.load("sql_raw_fixture", source)
            self.assertEqual(loaded.VALUE, "SAFE")

    def test_loader_pinea_antes_de_ejecutar_y_cierre_sdk_no_importa_controller(self):
        module = local()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "must_not_execute.py"
            source.write_bytes(b"raise AssertionError('no ejecutar')\n")
            with self.assertRaises(ValueError):
                module.load("reject_pin", source, "0" * 64)
        sdk = module.sdk_local()
        self.assertFalse(hasattr(sdk, "Controller"))
        self.assertEqual(sdk.contract().digest(b"x"), remote().digest(b"x"))

    def test_entrada_privada_no_exige_evidence_privado_y_detecta_marcador_alterado(self):
        module = local()
        sdk = unittest.mock.Mock()
        acl = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / ".tools/backup-source-build"
            inputs = parent / "postgres-sql-functional-v2-inputs"
            inputs.mkdir(parents=True)
            marker = parent / ".agency-backup-build-owner"
            marker.write_bytes(b"agencia-digital-marketing-ai:backup-source-build:v1")
            with (
                patch.object(module, "ROOT", root),
                patch.object(module, "LOCAL_ROOT", inputs),
                patch.object(module, "access", return_value=acl),
            ):
                module.private_inputs(sdk)
                self.assertEqual(
                    [c.args[0] for c in acl.check_access.call_args_list], [parent, inputs]
                )
                marker.write_bytes(b"otra-identidad\n")
                with self.assertRaises(ValueError):
                    module.private_inputs(sdk)

    def test_lector_no_adopta_hardlink_ni_archivo_que_supera_limite(self):
        import os

        module = local()
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "approval.json"
            p.write_bytes(b"{}")
            alias = Path(directory) / "alias.json"
            os.link(p, alias)
            with self.assertRaises(ValueError):
                module.read_source(p)
            alias.unlink()
            p.write_bytes(b"a" * 33)
            with self.assertRaises(ValueError):
                module.read_source(p, 32)

    def test_publicacion_exclusiva_preserva_primera_evidencia(self):
        module = local()
        sdk = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = root / "stage.json"
            with (
                patch.object(module, "LOCAL_ROOT", root),
                patch.object(module, "private_inputs"),
                patch.object(module, "access", return_value=unittest.mock.Mock()),
            ):
                module.write_private(p, b"primero", sdk)
                with self.assertRaises(FileExistsError):
                    module.write_private(p, b"segundo", sdk)
            self.assertEqual(p.read_bytes(), b"primero")

    def test_plan_sin_boveda_ni_red(self):
        module = local()
        with (
            patch.object(module, "read_values", side_effect=AssertionError("no leer")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(module.main([]), 0)
        value = json.loads(output.getvalue())
        self.assertTrue(value["plan_only"])
        self.assertFalse(value["infrastructure_executed"])

    def test_no_persistir_recibo_fuente_distinta_y_cid_no_respaldado(self):
        module, contract, spec = local(), remote(), profile()
        result = contract.envelope(
            {
                "stage": "create",
                "success": True,
                "cid": "f" * 64,
                "profile_sha256": contract.digest(contract.canonical(spec)),
                "source_sha256": "0" * 64,
            }
        )
        with self.assertRaises(ValueError):
            module.validate_result(result, "create", spec)

    def test_stop_pines_no_necesita_password_ni_aprobacion_fresca(self):
        module, contract, spec = local(), remote(), profile()
        values = {
            module.PREFIX + "PROFILE_B64": __import__("base64")
            .b64encode(contract.canonical(spec))
            .decode(),
            module.PREFIX + "PROFILE_SHA256": contract.digest(contract.canonical(spec)),
            module.PREFIX + "CONTAINER_NAME": spec["name"],
            module.PREFIX + "ROOT": spec["root"],
            module.PREFIX + "DATA_PATH": spec["data"],
            module.PREFIX + "SECRETS_PATH": spec["secrets"],
            module.PREFIX + "OWNER": contract.OWNER,
            **{
                module.PREFIX + name.upper() + "_SHA256": sha
                for name, sha in spec["sources"].items()
            },
        }
        with (
            patch.object(module, "sources", return_value=spec["sources"]),
            patch.object(module, "build_profile", side_effect=AssertionError("no SCA/secretos")),
        ):
            module.verify_pins(
                spec, contract.digest(contract.canonical(spec)), values, None, stop=True
            )

    def test_lector_boveda_8mib_no_usa_limite_sdk_2mib(self):
        module, sdk = local(), local().sdk_local()
        parser = sdk.load(
            "fixture_env_parser", ROOT / "scripts/ci/check_env.py", remote().ENV_PARSER_SHA
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            path = target / ".env"
            path.write_bytes(b"#" + b"a" * (2 * 1024**2) + b"\nSQL_TEST=ok\n")
            with (
                patch.object(module, "ROOT", target),
                patch.object(sdk, "load", return_value=parser),
                patch.object(module, "access", return_value=unittest.mock.Mock()),
            ):
                self.assertEqual(module.read_values(sdk)["SQL_TEST"], "ok")
                path.write_bytes(b"#" + b"a" * (8 * 1024**2))
                with self.assertRaises(ValueError):
                    module.read_values(sdk)

    def test_framing_real_main_aceptado_por_collector_fijo(self):
        module, sdk = remote(), local().sdk_local()
        result = module.envelope({"success": True})
        payload = module.canonical({"mode": "prepare"})

        class Fragments(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 1))

        class Input:
            buffer = type(
                "Buffer", (), {"raw": Fragments(struct.pack(">I", len(payload)) + payload)}
            )()

            def fileno(self):
                return 0

        with (
            patch("sys.stdin", Input()),
            patch("signal.SIGALRM", 14, create=True),
            patch("signal.signal"),
            patch("signal.alarm", create=True),
            patch.object(module, "apply", return_value=result),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(module.main(), 0)
        raw = output.getvalue().encode()

        class Channel:
            data = raw

            def recv_ready(self):
                return bool(self.data)

            def recv(self, maximum):
                data, self.data = self.data[:maximum], self.data[maximum:]
                return data

            def recv_stderr_ready(self):
                return False

            def recv_stderr(self, maximum):
                return b""

            def exit_status_ready(self):
                return not self.data

            def recv_exit_status(self):
                return 0

        observed = sdk.collect(Channel(), __import__("time").monotonic() + 1, 4 * 1024**2)
        self.assertEqual(observed, {"success": True, "result": result})


if __name__ == "__main__":
    unittest.main()
