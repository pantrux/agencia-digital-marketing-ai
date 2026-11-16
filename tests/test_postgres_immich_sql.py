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
        "sql_accept_contract", ROOT / "docker/postgres-immich/sql_acceptance_remote.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def local():
    spec = importlib.util.spec_from_file_location(
        "sql_accept_local", ROOT / "docker/postgres-immich/sql_acceptance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile():
    module = remote()
    image = approval(datetime.now(UTC))["image"]
    parent = "/var/lib/media-platform/build/postgres-immich-runtime/fixture"
    root = parent + "/sql-acceptance-v1"
    return {
        "schema_version": 1,
        "owner": module.OWNER,
        "target": {"hostname": "vm-media-01", "ip": "192.168.0.85", "user": "jandrade", "port": 22},
        "parent": {
            "path": parent,
            "directory": {"device": 1, "inode": 2},
            "marker_sha256": "d" * 64,
        },
        "root": root,
        "data": root + "/data",
        "secrets": root + "/secrets",
        "name": "agencia-pg-immich-sql-fixture",
        "image": image,
        "password_sha256": "e" * 64,
        "approval_sha256": "f" * 64,
        "sources": {k: "1" * 64 for k in ("local", "remote", "sql")},
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


def approval(now):
    return {
        "schema_version": 1,
        "decision": "approved_for_isolated_sql",
        "coverage_complete": True,
        "image": {
            "id": "sha256:" + "a" * 64,
            "config_digest": "sha256:" + "b" * 64,
            "user": "70:70",
            "entrypoint": ["docker-entrypoint.sh"],
            "cmd": ["postgres"],
            "env": ["PGDATA=/var/lib/postgresql/data"],
            "labels": {
                "cl.e-scale.agencia.owner": "agencia-digital-marketing-ai:postgres-immich-runtime:v4.1"
            },
        },
        "finalization": {
            "success": True,
            "receipt_sha256": "c" * 64,
            "image_id": "sha256:" + "a" * 64,
            "config_digest": "sha256:" + "b" * 64,
        },
        "unresolved_high_critical": 0,
        "unresolved_advisories": [],
        "db_updated_at_utc": (now - timedelta(hours=1)).isoformat(),
        "expires_at_utc": (now + timedelta(hours=1)).isoformat(),
        "reports": [
            {"kind": k, "path": "/private/" + k + ".json", "sha256": str(i) * 64, "components": n}
            for i, (k, n) in enumerate(
                (("os", 45), ("cargo", 165), ("sqlite", 1), ("mimalloc", 1)), 1
            )
        ],
        "sqlite_version": "3.54.1",
    }


class ScaGate(unittest.TestCase):
    def setUp(self):
        self.module = remote()
        self.now = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)

    def test_subject_dinamico_y_cobertura_real_no_candidata_literal(self):
        value = approval(self.now)
        self.module.validate_approval(value, value["image"]["id"], self.now)
        self.assertNotIn("96c6", value["image"]["id"])

    def test_rechaza_cero_paquetes_y_avisos_pendientes(self):
        for edit in ("zero", "advisory", "high", "coverage"):
            value = approval(self.now)
            if edit == "zero":
                value["reports"][1]["components"] = 0
            elif edit == "advisory":
                value["unresolved_advisories"] = ["sin resolver"]
            elif edit == "high":
                value["unresolved_high_critical"] = 1
            else:
                value["coverage_complete"] = False
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.module.validate_approval(value, value["image"]["id"], self.now)

    def test_vencimiento_y_subject_divergente_rechazan(self):
        value = approval(self.now)
        with self.assertRaises(ValueError):
            self.module.validate_approval(value, "sha256:" + "d" * 64, self.now)
        value["expires_at_utc"] = (self.now - timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            self.module.validate_approval(value, value["image"]["id"], self.now)

    def test_reporte_incorrecto_y_json_no_objeto_rechazan_cerrado(self):
        for malformed in ([], None, {"reports": [None]}, {"reports": {}}):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                self.module.validate_approval(malformed, "sha256:" + "a" * 64, self.now)

    def test_mimalloc_c_embebido_exige_reporte_separado(self):
        value = approval(self.now)
        self.module.validate_approval(value, value["image"]["id"], self.now)
        value["reports"] = [r for r in value["reports"] if r["kind"] != "mimalloc"]
        with self.assertRaises(ValueError):
            self.module.validate_approval(value, value["image"]["id"], self.now)


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

    def test_reportes_se_leen_y_se_comparan_no_solo_clean(self):
        module, contract = local(), remote()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = approval(datetime.now(UTC))
            for report in value["reports"]:
                path = root / (report["kind"] + ".json")
                path.write_bytes(b'{"components":1}')
                report.update(path=str(path), sha256=contract.digest(path.read_bytes()))
            module.verify_reports(value, root=root)
            (root / "cargo.json").write_bytes(b'{"clean":true}')
            with self.assertRaises(ValueError):
                module.verify_reports(value, root=root)

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

    def test_hash_aprobacion_sin_reportes_reales_no_habilita_la_etapa(self):
        module = local()
        value = approval(datetime.now(UTC))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError, FileNotFoundError)):
                module.verify_reports(value, root=Path(directory))

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
