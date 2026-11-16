"""Plan/ownership y Docker controlado del servicio R2; sin host ni Docker reales."""

import base64
import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "r2_service_contract_tests", ROOT / "docker/r2_service_contract.py"
)
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
sys.modules["r2_service_contract"] = C
remote_spec = importlib.util.spec_from_file_location(
    "r2_service_remote_tests", ROOT / "docker/r2_service_remote.py"
)
R = importlib.util.module_from_spec(remote_spec)
remote_spec.loader.exec_module(R)
controller_spec = importlib.util.spec_from_file_location(
    "r2_service_controller_tests", ROOT / "docker/r2_service.py"
)
B = importlib.util.module_from_spec(controller_spec)
controller_spec.loader.exec_module(B)


def specification():
    return {
        "schema_version": 1,
        "deployment_id": "22222222-2222-4222-8222-222222222222",
        "image_id": C.IMAGE,
        "image_receipt_sha256": C.IMAGE_RECEIPT,
        "source_root_sha256": C.SOURCE_ROOT,
        "account_id": "a" * 32,
        "max_object_bytes": 1073741824,
        "media_before": {"device": 1, "inode": 2},
        "share_evidence_sha256": "a" * 64,
        "prepared": None,
        "init_container_id": None,
        "service_container_id": None,
        "init_result": None,
    }


def prepared():
    proof = {"device": 1, "inode": 3, "marker_name": ".owner.json", "marker_sha256": "b" * 64}
    files = {
        name: {
            "device": 1,
            "inode": i,
            "uid": 65532,
            "gid": 65532,
            "mode": 0o400,
            "size": 64,
            "mtime_ns": 1,
            "ctime_ns": 1,
        }
        for i, name in enumerate(C.SECRET_NAMES, 10)
    }
    files["runtime.json"] = {**files[C.SECRET_NAMES[0]], "inode": 20, "size": 1024}
    return {
        "schema_version": 1,
        "owner": C.OWNER,
        "deployment_id": specification()["deployment_id"],
        "core_sha256": C.core_hash(specification()),
        "root": {"device": 1, "inode": 4},
        "state_proof": proof,
        "media_proof": {**proof, "inode": 2, "marker_name": C.MEDIA_MARKER},
        "project": {"device": 1, "inode": 5},
        "raw": {"device": 1, "inode": 6},
        "files": files,
        "config_sha256": "c" * 64,
    }


def inspect_fixture(desired, cid, name):
    return {
        "Id": cid,
        "Name": "/" + name,
        "Image": C.IMAGE,
        "Config": {key: value for key, value in desired.items() if key != "HostConfig"},
        "HostConfig": copy.deepcopy(desired["HostConfig"]),
        "Mounts": [
            {
                "Type": m["Type"],
                "Source": m["Source"],
                "Destination": m["Target"],
                "RW": not m["ReadOnly"],
                "Propagation": "rprivate",
            }
            for m in desired["HostConfig"]["Mounts"]
        ],
    }


def observation():
    return {
        **{
            key: "a" * 64
            for key in ("controller_sha256", "remote_sha256", "contract_sha256", "layout_sha256")
        },
        "layout": {
            "owner": "agencia-digital-marketing-ai:media-platform:v4.1",
            "success": True,
            "targets": [
                {
                    "path": path,
                    "role": role,
                    "directory": {"device": 1, "inode": i},
                    "marker_sha256": "b" * 64,
                    "receipt_sha256": "c" * 64,
                }
                for i, (path, role) in enumerate(
                    (("/opt/media-platform", "code"), ("/var/lib/media-platform", "state")), 1
                )
            ],
        },
    }


class ServiceContractTests(unittest.TestCase):
    def test_esquema_cerrado_pins_y_politica_sin_valores_de_reserva(self):
        C.validate_spec(specification())
        for changed in (
            {"max_object_bytes": True},
            {"max_object_bytes": 0},
            {"image_id": "sha256:" + "0" * 64},
            {"extra": "no"},
            {"media_before": {"device": True, "inode": 2}},
        ):
            value = specification()
            value.update(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                C.validate_spec(value)

    def test_create_init_sin_red_y_create_serve_sin_start_implicito(self):
        value = specification()
        value["prepared"] = prepared()
        init = C.container_config(value, "init")
        serve = C.container_config(value, "serve")
        self.assertEqual(init["HostConfig"]["NetworkMode"], "none")
        self.assertEqual(init["Entrypoint"][-1], "init-db")
        self.assertEqual(serve["HostConfig"]["NetworkMode"], "bridge")
        self.assertEqual(
            serve["HostConfig"]["PortBindings"],
            {"3010/tcp": [{"HostIp": "192.168.0.85", "HostPort": "3010"}]},
        )
        for body in (init, serve):
            h = body["HostConfig"]
            self.assertEqual(body["User"], "65532:65532")
            self.assertTrue(h["ReadonlyRootfs"])
            self.assertEqual(h["CapDrop"], ["ALL"])
            self.assertEqual(h["SecurityOpt"], ["no-new-privileges:true"])
            self.assertEqual(
                (h["NanoCpus"], h["Memory"], h["PidsLimit"]), (1000000000, 805306368, 64)
            )
            self.assertFalse(h["AutoRemove"])
            self.assertNotIn("Start", body)
        self.assertNotIn("R2_PLATFORM_SECRET_ACCESS_KEY", str(serve))

    def test_no_adopta_cid_ajeno_o_campos_de_contenedor_alterados(self):
        value = specification()
        value["prepared"] = prepared()
        value["service_container_id"] = "d" * 64
        desired = C.container_config(value, "serve")
        info = inspect_fixture(desired, "d" * 64, C.container_name(value, "serve"))
        C.verify_container(value, "serve", info)
        for changed in ({"Id": "e" * 64}, {"Image": "sha256:" + "e" * 64}, {"Name": "/otro"}):
            wrong = copy.deepcopy(info)
            wrong.update(changed)
            with self.assertRaises(ValueError):
                C.verify_container(value, "serve", wrong)

    def test_mount_inspect_real_omite_false_y_opciones_default_sin_aceptar_extras(self):
        value = specification()
        value["prepared"] = prepared()
        value["service_container_id"] = "d" * 64
        info = inspect_fixture(
            C.container_config(value, "serve"), "d" * 64, C.container_name(value, "serve")
        )
        for mount in info["HostConfig"]["Mounts"]:
            if not mount["ReadOnly"]:
                mount.pop("ReadOnly")
            mount.pop("BindOptions")
        info["HostConfig"]["Mounts"].reverse()
        C.verify_container(value, "serve", info)
        wrong = copy.deepcopy(info)
        wrong["HostConfig"]["Mounts"][0]["VolumeOptions"] = {"NoCopy": True}
        with self.assertRaises(ValueError):
            C.verify_container(value, "serve", wrong)
        wrong = copy.deepcopy(info)
        wrong["Mounts"].append(dict(wrong["Mounts"][0]))
        with self.assertRaises(ValueError):
            C.verify_container(value, "serve", wrong)
        for key, bad in (
            ("ReadonlyRootfs", False),
            ("NetworkMode", "host"),
            ("Privileged", True),
            ("Mounts", []),
            ("NanoCpus", 2000000000),
        ):
            wrong = copy.deepcopy(info)
            wrong["HostConfig"][key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                C.verify_container(value, "serve", wrong)

    def test_start_requiere_cid_respaldado_y_init_acreditado(self):
        value = specification()
        value["prepared"] = prepared()
        with self.assertRaises(ValueError):
            C.validate_action(value, "start-init")
        value["init_container_id"] = "d" * 64
        C.validate_action(value, "start-init")
        value["service_container_id"] = "e" * 64
        with self.assertRaises(ValueError):
            C.validate_action(value, "start-serve")
        value["init_result"] = {
            "container_id": "d" * 64,
            "exit_code": 0,
            "core_sha256": C.core_hash(value),
        }
        C.validate_action(value, "start-serve")

    def test_plan_hash_accion_y_dto_no_pueden_cambiar_silenciosamente(self):
        value = specification()
        plan = C.make_plan(value, "prepare", observation())
        self.assertEqual(plan["plan_sha256"], C.plan_hash(plan))
        wrong = copy.deepcopy(plan)
        wrong["action"] = "start-serve"
        with self.assertRaises(ValueError):
            C.verify_plan(wrong, plan["plan_sha256"])
        value["prepared"] = prepared()
        value["prepared"]["core_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            C.validate_spec(value)


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.db = False
        self.fail_verify = False

    def verify(self, _spec):
        C.require(not self.fail_verify)

    def verify_local_control(self, _spec):
        pass

    def read_record(self, name):
        return self.records.get(name)

    def record(self, name, value):
        C.require(name not in self.records)
        self.records[name] = value

    def database(self, *, exists):
        C.require(self.db == exists)


class MemoryEngine:
    def __init__(self, store):
        self.store = store
        self.created = {}
        self.calls = []
        self.foreign = False
        self.health_value = {"ready": True, "busy": False}

    def version(self):
        self.calls.append(("version",))

    def named(self, name):
        return {"Id": "f" * 64} if self.foreign else None

    def create(self, value, role):
        cid = ("d" if role == "init" else "e") * 64
        info = inspect_fixture(C.container_config(value, role), cid, C.container_name(value, role))
        info["State"] = {"Status": "created", "Running": False, "ExitCode": 0}
        self.created[cid] = info
        self.calls.append(("create", role))
        return {"Id": cid, "Warnings": []}

    def inspect(self, cid):
        return copy.deepcopy(self.created[cid])

    def start(self, cid):
        info = self.created[cid]
        info["State"].update(Status="running", Running=True)
        info["NetworkSettings"] = {"Ports": copy.deepcopy(info["HostConfig"]["PortBindings"])}
        self.calls.append(("start", cid))

    def initialized(self, cid):
        self.created[cid]["State"].update(Status="exited", Running=False)
        self.store.db = True

    def health(self):
        return self.health_value

    def port_free(self):
        self.calls.append(("port-free",))

    def stop(self, cid):
        self.created[cid]["State"].update(Status="exited", Running=False)
        self.calls.append(("stop", cid))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.value = specification()
        self.value["prepared"] = prepared()
        self.store = MemoryStore()
        self.engine = MemoryEngine(self.store)

    def execute(self, action):
        plan = C.make_plan(self.value, action, observation())
        return R.execute(plan, plan["plan_sha256"], self.store, self.engine)

    def initialize(self):
        created = self.execute("create-init")
        self.value["init_container_id"] = created["container_id"]
        result = self.execute("start-init")
        self.value["init_result"] = result["init_result"]
        return result

    def test_create_no_arranca_y_start_exige_cid_ya_respaldado(self):
        created = self.execute("create-init")
        self.assertTrue(created["requires_cid_backup_before_start"])
        self.assertFalse(any(call[0] == "start" for call in self.engine.calls))
        with self.assertRaises(ValueError):
            self.execute("start-init")
        self.value["init_container_id"] = created["container_id"]
        result = self.execute("start-init")
        self.assertEqual(result["init_result"]["exit_code"], 0)
        self.assertFalse(result["removed"])

    def test_orden_completo_init_serve_busy_stop_no_borra_estado(self):
        self.initialize()
        created = self.execute("create-serve")
        self.value["service_container_id"] = created["container_id"]
        self.engine.health_value = {"ready": False, "busy": True}
        result = self.execute("start-serve")
        self.assertEqual(result["health"], {"ready": False, "busy": True})
        self.assertEqual(self.execute("stop")["container_id"], created["container_id"])
        self.assertTrue(self.store.db)
        self.assertEqual(
            [call[1] for call in self.engine.calls if call[0] == "start"], ["d" * 64, "e" * 64]
        )
        self.assertFalse(self.execute("stop")["changed"])

    def test_no_reinicializa_db_ni_adopta_nombre_existente(self):
        self.store.db = True
        with self.assertRaises(ValueError):
            self.execute("create-init")
        self.assertFalse(self.engine.created)
        self.store.db = False
        self.engine.foreign = True
        with self.assertRaises(ValueError):
            self.execute("create-init")
        self.assertFalse(self.engine.created)

    def test_idempotencia_creacion_y_recibo_init_sin_arranque_nuevo(self):
        self.initialize()
        count = len([c for c in self.engine.calls if c[0] == "start"])
        self.assertFalse(self.execute("create-init")["changed"])
        self.assertFalse(self.execute("start-init")["changed"])
        self.assertEqual(len([c for c in self.engine.calls if c[0] == "start"]), count)

    def test_guarda_fs_y_alteracion_contenedor_se_comprueban_antes_de_mutar(self):
        self.store.fail_verify = True
        with self.assertRaises(ValueError):
            self.execute("create-init")
        self.assertFalse(self.engine.calls)
        self.store.fail_verify = False
        self.initialize()
        created = self.execute("create-serve")
        self.value["service_container_id"] = created["container_id"]
        self.engine.created["e" * 64]["HostConfig"]["Privileged"] = True
        with self.assertRaises(ValueError):
            self.execute("start-serve")
        self.assertNotIn(("start", "e" * 64), self.engine.calls)

    def test_stop_no_toca_nas_o_db_aunque_proveedor_no_responda(self):
        self.initialize()
        created = self.execute("create-serve")
        self.value["service_container_id"] = created["container_id"]
        self.execute("start-serve")
        self.store.fail_verify = True
        self.store.db = False
        result = self.execute("stop")
        self.assertTrue(result["success"])
        self.assertIn(("stop", "e" * 64), self.engine.calls)

    def test_create_devuelve_cid_aunque_postinspect_falle_y_no_arranca(self):
        native = self.engine.inspect
        self.engine.inspect = lambda _cid: (_ for _ in ()).throw(ValueError("postinspect fixture"))
        with self.assertRaises(R.CreatedUnverified) as caught:
            self.execute("create-init")
        dto = caught.exception.result
        self.assertEqual(dto["created_cid"], "d" * 64)
        self.assertFalse(dto["verified"])
        self.assertTrue(dto["requires_cid_backup_before_any_action"])
        self.assertIn("init-intent.json", self.store.records)
        self.assertIn("init-created.json", self.store.records)
        self.assertFalse(any(call[0] == "start" for call in self.engine.calls))
        self.engine.inspect = native
        self.value["init_container_id"] = dto["created_cid"]
        self.assertFalse(self.execute("create-init")["changed"])

    def test_puerto_ajeno_impide_create_serve_sin_efectos_sobre_el(self):
        self.initialize()
        self.engine.port_free = lambda: (_ for _ in ()).throw(OSError("ocupado"))
        with self.assertRaises(OSError):
            self.execute("create-serve")
        self.assertNotIn(("create", "serve"), self.engine.calls)
        self.assertNotIn("serve-intent.json", self.store.records)

    def test_stop_admite_cid_propio_pendiente_verificacion_pero_no_adopta_otro(self):
        self.initialize()
        created = self.execute("create-serve")
        self.value["service_container_id"] = created["container_id"]
        self.store.records.pop("serve-verified.json")
        self.store.fail_verify = True
        self.assertTrue(self.execute("stop")["success"])
        self.engine.created["e" * 64]["Name"] = "/ajeno"
        count = len([call for call in self.engine.calls if call[0] == "stop"])
        with self.assertRaises(ValueError):
            self.execute("stop")
        self.assertEqual(len([call for call in self.engine.calls if call[0] == "stop"]), count)


class ControllerTests(unittest.TestCase):
    def values(self):
        values = {
            "MEDIA_VM_IP": "192.168.0.85",
            "MEDIA_VM_SSH_USER": "jandrade",
            "MEDIA_R2_SERVICE_ROOT": C.ROOT,
            "MEDIA_R2_SERVICE_MEDIA_ROOT": C.MEDIA,
            "MEDIA_R2_SERVICE_PORT": "3010",
            "CF_ACCOUNT_ID": "a" * 32,
            "MEDIA_R2_SYNC_MAX_OBJECT_BYTES": "1073741824",
            "MEDIA_R2_SERVICE_SPEC_B64": base64.b64encode(C.canonical(specification())).decode(),
            "MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64": base64.b64encode(
                C.canonical(observation()["layout"])
            ).decode(),
            "STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET": "h" * 64,
            "R2_PLATFORM_ACCESS_KEY_ID": "accessFixture",
            "R2_PLATFORM_SECRET_ACCESS_KEY": "secretFixture",
            "CREDENCIAL_AJENA": "no-transferible",
        }
        for role, raw in B.sources().items():
            if role != "layout":
                values[
                    "MEDIA_R2_SERVICE_"
                    + ("SOURCE" if role == "controller" else role.upper())
                    + "_SHA256"
                ] = hashlib.sha256(raw).hexdigest()
        return values

    def test_plan_local_sin_ssh_y_payload_no_toma_otra_clave(self):
        values = self.values()
        with patch.object(B, "connect_ssh") as ssh:
            plan = B.prepare_plan(values, "prepare")
        ssh.assert_not_called()
        raw = json.dumps(plan)
        for hidden in ("h" * 64, "secretFixture", "accessFixture", "no-transferible"):
            self.assertNotIn(hidden, raw)
        payload = B.secret_payload(values)
        self.assertEqual(set(payload), set(C.SECRET_NAMES))
        self.assertEqual(base64.b64decode(payload["hmac.key"]), b"h" * 64)
        command = B.remote_command(B.sources())
        self.assertTrue(command.startswith("sudo -n env -i "))
        self.assertNotIn("secretFixture", command)
        self.assertNotIn("no-transferible", command)

    def test_hash_obsoleto_rechaza_antes_de_ssh(self):
        values = self.values()
        plan = B.prepare_plan(values, "prepare")
        with patch.object(B, "connect_ssh") as ssh, self.assertRaises(ValueError):
            B.apply_remote(plan, values, plan["plan_sha256"])
        ssh.assert_not_called()

    def test_cid_no_verificado_sobrevive_exit_remoto_no_cero(self):
        values = self.values()
        value = specification()
        value["prepared"] = prepared()
        values["MEDIA_R2_SERVICE_SPEC_B64"] = base64.b64encode(C.canonical(value)).decode()
        plan = B.prepare_plan(values, "create-init")
        values["MEDIA_R2_SERVICE_APPROVED_PLAN_SHA256"] = plan["plan_sha256"]
        result = R.CreatedUnverified("create-init", "init", "d" * 64, value).result
        channel, client = MagicMock(), MagicMock()
        client.get_transport.return_value.open_session.return_value = channel
        channel.recv_ready.side_effect = [True, False, False]
        channel.recv.return_value = C.canonical(result)
        channel.recv_stderr_ready.return_value = False
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 2
        with patch.object(B, "connect_ssh", return_value=client):
            self.assertEqual(B.apply_remote(plan, values, plan["plan_sha256"]), result)
        channel.close.assert_called_once()
        client.close.assert_called_once()
        sent = json.loads(channel.sendall.call_args.args[0])
        self.assertIsNone(sent["secrets"])

    def test_config_json_duplicada_y_secretos_malformados_rechazados(self):
        with self.assertRaises(ValueError):
            B.parse_json(b'{"a":1,"a":2}')
        values = self.values()
        values["STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET"] = "h" * 63 + "\n"
        with self.assertRaises(ValueError):
            B.secret_payload(values)


@unittest.skipUnless(
    sys.platform.startswith("linux") and os.geteuid() == 0,
    "Requiere Linux/root para comprobar NoFollow y UID65532 reales; sin host real.",
)
class LinuxFilesystemTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.data = self.base / "data"
        self.data.mkdir(mode=0o700)
        self.media = self.base / "media"
        self.media.mkdir(mode=0o700)
        self.probe = b"p" * 170
        (self.media / ".probe").write_bytes(self.probe)
        self.value = specification()
        self.value["media_before"] = R.identity(self.media.stat())
        self.store = R.Filesystem(self.data / "service", self.media)

        def mount(path):
            return (
                (str(self.media), ["nfs4", "192.168.0.8:/volume2/producciones", "rw,vers=4.1"])
                if Path(path) == self.media
                else (str(self.data), ["ext4", "/dev/fixture", "rw"])
            )

        self.mount = patch.object(R, "mount_kind", side_effect=mount)
        self.mount.start()
        self.addCleanup(self.mount.stop)
        self.probe_pin = patch.object(C, "PROBE_SHA", hashlib.sha256(self.probe).hexdigest())
        self.probe_pin.start()
        self.addCleanup(self.probe_pin.stop)
        self.secrets = {
            name: base64.b64encode(raw).decode()
            for name, raw in zip(
                C.SECRET_NAMES, (b"h" * 64, b"accessFixture", b"secretFixture"), strict=True
            )
        }

    def prepare(self):
        result = self.store.prepare(self.value, self.secrets)
        self.value["prepared"] = result
        return result

    def test_prepara_privado_65532_archivos400_e_idempotencia_sin_db(self):
        result = self.prepare()
        self.store.verify(self.value)
        self.assertEqual(self.store.local.stat().st_uid, 65532)
        self.assertFalse((self.store.local / "state/sync-ledger.sqlite3").exists())
        self.assertEqual(self.store.prepare(self.value, None), result)
        for name in (*C.SECRET_NAMES, "runtime.json"):
            path = self.store.local / name
            self.assertEqual(path.stat().st_uid, 65532)
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def test_colision_local_bloquea_antes_de_crear_marker_nas(self):
        self.store.local.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            self.store.prepare(self.value, self.secrets)
        self.assertFalse((self.media / C.MEDIA_MARKER).exists())

    def test_proyecto_ajeno_y_symlink_se_rechazan_sin_adoptar(self):
        projects = self.media / "proyectos"
        projects.mkdir()
        (projects / C.PROJECT).mkdir()
        with self.assertRaises(ValueError):
            self.store.prepare(self.value, self.secrets)
        self.assertFalse((self.media / C.MEDIA_MARKER).exists())
        (projects / C.PROJECT).rmdir()
        projects.rmdir()
        projects.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            self.store.prepare(self.value, self.secrets)
        self.assertFalse(self.store.local.exists())

    def test_secret_alterado_o_hardlink_y_parcial_no_se_adoptan(self):
        self.prepare()
        path = self.store.local / "hmac.key"
        path.chmod(0o600)
        path.write_bytes(b"z" * 64)
        path.chmod(0o400)
        with self.assertRaises(ValueError):
            self.store.verify(self.value)
        self.assertTrue((self.store.local / "prepare-receipt.json").exists())

    def test_nas_modo_proyectado_777_preserva_sha_sin_exigir_uid65532(self):
        self.prepare()
        (self.media / C.MEDIA_MARKER).chmod(0o777)
        (self.media / "proyectos" / C.PROJECT / ".owner.json").chmod(0o777)
        self.store.verify(self.value)


if __name__ == "__main__":
    unittest.main()
