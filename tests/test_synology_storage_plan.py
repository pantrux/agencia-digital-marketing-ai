"""Regresiones del plan cerrado de almacenamiento DSM, sin red."""

import importlib.util
import io
import json
import shlex
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "synology_storage_apply", ROOT / "storage/synology_storage_apply.py"
)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
if SPEC.loader:
    SPEC.loader.exec_module(module)


class StoragePlanTests(unittest.TestCase):
    def snapshot(self):
        return {
            "model": "DVA3221",
            "dsm": "7.3.2",
            "hostname": "NAS",
            "volume": "/volume2",
            "free_kib": 2_887_392_232,
            "share": None,
            "target_exists": False,
            "exports": "/volume2/NFS 192.168.0.3(rw,sec=sys)\n/volume2/web 192.168.0.50(rw,all_squash,anonuid=1024)\n",
            "nfs": {
                "enable_nfs": True,
                "enable_nfs_v4": False,
                "enabled_minor_ver": 0,
                "nfs_v4_domain": "",
                "read_size": 8192,
                "write_size": 8192,
                "unix_pri_enable": True,
                "support_major_ver": 4,
                "support_minor_ver": 1,
            },
            "smb": {"enable_samba": True, "smb_max_protocol": 3, "smb_encrypt_transport": 1},
            "nfs_versions": "+2 +3 -4 -4.1 -4.2",
        }

    def test_plan_no_modifica_snapshot_ni_exportaciones_ajenas(self):
        snapshot = self.snapshot()
        exports = snapshot["exports"]
        plan = module.build_plan(snapshot)
        self.assertEqual(snapshot["exports"], exports)
        self.assertEqual(plan["exports_ajenos"], exports.removesuffix("\n"))
        self.assertEqual(plan["ruta"], "/volume2/producciones")
        self.assertFalse(plan["aplicado"])

    def test_solo_dos_ips_exactas_y_squash_admin_sin_wildcard(self):
        rules = module.desired_rules()
        self.assertEqual({rule["client"] for rule in rules}, {"192.168.0.85", "192.168.0.7"})
        self.assertTrue(
            all(rule["root_squash"] == "all_admin" and rule["privilege"] == "rw" for rule in rules)
        )
        self.assertTrue(
            all(
                not rule["async"] and not rule["crossmnt"] and not rule["insecure"]
                for rule in rules
            )
        )

    def test_plan_preserva_nfs2_y3_y_ajustes_de_transferencia(self):
        plan = module.build_plan(self.snapshot())
        self.assertTrue(plan["nfs_deseado"]["enable_nfs"])
        self.assertTrue(plan["nfs_deseado"]["enable_nfs_v4"])
        self.assertEqual(plan["nfs_deseado"]["enabled_minor_ver"], 1)
        self.assertEqual(plan["nfs_deseado"]["read_size"], 8192)
        self.assertEqual(plan["nfs_deseado"]["write_size"], 8192)
        self.assertTrue(plan["requiere_interrupcion_nfs"])

    def test_modelo_version_y_volumen_distintos_se_rechazan(self):
        for key, value in [("model", "otro"), ("dsm", "7.2"), ("volume", "/volume1")]:
            with self.subTest(key=key):
                snapshot = self.snapshot()
                snapshot[key] = value
                with self.assertRaises(module.StorageError):
                    module.build_plan(snapshot)

    def test_no_adopta_directorio_previo_ni_share_ajeno(self):
        for change in [
            {"target_exists": True},
            {"share": {"name": "producciones", "path": "/volume1/producciones", "desc": "ajeno"}},
        ]:
            with self.subTest(change=change):
                snapshot = self.snapshot()
                snapshot.update(change)
                with self.assertRaises(module.StorageError):
                    module.build_plan(snapshot)

    def test_backend_sin_41_o_smb3_se_rechaza(self):
        for service, field, value in [
            ("nfs", "support_minor_ver", 0),
            ("smb", "smb_max_protocol", 2),
        ]:
            snapshot = self.snapshot()
            snapshot[service][field] = value
            with self.assertRaises(module.StorageError):
                module.build_plan(snapshot)

    def test_api_failure_es_error_aunque_codigo_shell_sea_cero(self):
        with self.assertRaises(module.StorageError):
            module.api_data(
                {
                    "codigo_salida": 0,
                    "truncado": False,
                    "salida": '{"success":false,"error":{"code":402}}',
                }
            )
        self.assertEqual(
            module.api_data(
                {
                    "codigo_salida": 0,
                    "truncado": False,
                    "salida": '{"success":true,"data":{"ok":1}}',
                }
            ),
            {"ok": 1},
        )

    def test_respuesta_api_truncada_o_no_objeto_se_rechaza(self):
        for response in [
            {"codigo_salida": 0, "truncado": True, "salida": '{"success":true}'},
            {"codigo_salida": 0, "truncado": False, "salida": "[]"},
        ]:
            with self.assertRaises(module.StorageError):
                module.api_data(response)

    def test_comando_save_tiene_payload_igual_al_frontend_dsm(self):
        command = shlex.split(module.nfs_rules_command())
        parameters = dict(argument.split("=", 1) for argument in command[2:])
        self.assertEqual(parameters["api"], "SYNO.Core.FileServ.NFS.SharePrivilege")
        self.assertEqual(parameters["method"], "save")
        self.assertEqual(json.loads(parameters["share_name"]), "producciones")
        self.assertEqual(json.loads(parameters["rule"]), module.desired_rules())

    def test_protocolo_solo_envia_tres_campos_y_omite_capabilities(self):
        command = shlex.split(module.nfs_protocol_command())
        parameters = dict(argument.split("=", 1) for argument in command[2:])
        self.assertEqual(
            set(parameters),
            {"api", "method", "version", "enable_nfs", "enable_nfs_v4", "enabled_minor_ver"},
        )
        self.assertEqual(parameters["enabled_minor_ver"], "1")

    def test_comparacion_preserva_bytes_de_exports_ajenos(self):
        before = self.snapshot()["exports"]
        own = "/volume2/producciones 192.168.0.85(rw,all_squash) 192.168.0.7(rw,all_squash)\n"
        module.verify_foreign_exports(before, before + own)
        with self.assertRaises(module.StorageError):
            module.verify_foreign_exports(before, before.replace("192.168.0.3", "0.0.0.0/0") + own)

    def test_exports_sin_salto_final_admite_solo_delimitador_estructural(self):
        before = self.snapshot()["exports"].removesuffix("\n")
        own = "/volume2/producciones 192.168.0.85(rw,all_squash)\n"
        module.verify_foreign_exports(before, before + "\n" + own)
        with self.assertRaises(module.StorageError):
            module.verify_foreign_exports(before, before + "\n\n" + own)
        with self.assertRaises(module.StorageError):
            module.verify_foreign_exports(before, before.replace("(rw,", "(ro,") + "\n" + own)

    def test_parse_share_no_confunde_error_interno_con_ausencia(self):
        self.assertIsNone(
            module.parse_share(
                {
                    "codigo_salida": 255,
                    "truncado": False,
                    "salida": "SYNOShareGet failed. synoerr=[0x1400]",
                }
            )
        )
        with self.assertRaises(module.StorageError):
            module.parse_share(
                {"codigo_salida": 255, "truncado": False, "salida": "Permission denied"}
            )

    def test_tipos_invalidos_del_snapshot_se_rechazan_de_forma_controlada(self):
        for change in ({"share": []}, {"nfs": None}, {"smb": []}, {"nfs_versions": None}):
            snapshot = self.snapshot()
            snapshot.update(change)
            with self.assertRaises(module.StorageError):
                module.build_plan(snapshot)

    def test_guardas_no_ejecutan_mutaciones_sin_backup_y_hash_aprobado(self):
        plan = module.build_plan(self.snapshot())
        calls = []
        for backup_verified, approved in ((False, module.plan_hash(plan)), (True, "0" * 64)):
            with self.assertRaises(module.StorageError):
                module.execute_plan(
                    plan, lambda command: calls.append(command), backup_verified, approved
                )
        self.assertEqual(calls, [])

    def test_json_rechazado_detiene_operaciones_posteriores(self):
        plan = module.build_plan(self.snapshot())
        plan["crear_share"] = False
        calls = []

        def runner(command):
            calls.append(command)
            if "method=save" in command:
                return {
                    "codigo_salida": 0,
                    "truncado": False,
                    "salida": '{"success":false,"error":{"code":2301}}',
                }
            return {"codigo_salida": 0, "truncado": False, "salida": ""}

        with self.assertRaises(module.StorageError):
            module.execute_plan(plan, runner, True, module.plan_hash(plan))
        self.assertFalse(any("method=set" in command for command in calls))

    def test_backup_solo_admite_ruta_canonica_respaldada(self):
        path = "/volume2/homes/jandrade/.agencia-media-config-backups/fase2-20261105"
        command = shlex.split(module.backup_command(path, self.snapshot()))
        self.assertEqual(command[-2], path)
        for invalid in ("/volume1/backup", "/etc", path + "/../otro"):
            with self.assertRaises(module.StorageError):
                module.backup_command(invalid, self.snapshot())

    def test_payloads_remotos_compilan_sin_ejecutarlos(self):
        for source in (module.ACL_SOURCE, module.DIRECTORIES_SOURCE, module.BACKUP_SOURCE):
            compile(source, "<payload de prueba>", "exec")

    def test_politica_boveda_debe_coincidir_antes_de_mutar(self):
        values = dict(module.EXECUTION_VALUES)
        module.validate_execution_values(values)
        for key in module.EXECUTION_VALUES:
            invalid = values.copy()
            invalid[key] = "CHANGE_ME"
            with self.assertRaises(module.StorageError):
                module.validate_execution_values(invalid)

    def test_postcondiciones_exigen_preservacion_de_versiones_y_ajustes(self):
        before = self.snapshot()
        after = self.snapshot()
        after["share"] = {"name": "producciones", "path": module.TARGET, "desc": module.OWNER}
        after["rules"] = module.desired_rules()
        after["nfs"].update(enable_nfs_v4=True, enabled_minor_ver=1)
        after.update(nfs_versions="+2 +3 +4 +4.1 -4.2", nfs_threads="128")
        module.verify_postconditions(before, after)
        for change in ({"nfs_versions": "+3 +4 +4.1 -4.2"}, {"nfs_threads": "0"}):
            invalid = after.copy()
            invalid.update(change)
            with self.assertRaises(module.StorageError):
                module.verify_postconditions(before, invalid)

    def test_cli_apply_sin_plan_revisado_no_conecta(self):
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(module, "live_operation") as operation:
                code = module.main(["--apply", "--output", str(Path(tmp) / "resultado.json")])
            self.assertNotEqual(code, 0)
            operation.assert_not_called()

    def test_plan_idempotente_no_pide_save_ni_restart_si_ya_coincide(self):
        snapshot = self.snapshot()
        snapshot.update(
            share={"name": "producciones", "path": module.TARGET, "desc": module.OWNER},
            target_exists=True,
            rules=module.desired_rules(),
        )
        snapshot["nfs"].update(enable_nfs_v4=True, enabled_minor_ver=1)
        plan = module.build_plan(snapshot)
        self.assertFalse(plan["crear_share"])
        self.assertFalse(plan["actualizar_reglas_nfs"])
        self.assertFalse(plan["requiere_interrupcion_nfs"])

    def test_payload_acl_real_normaliza_solo_share_propio_y_es_idempotente(self):
        entries = ["group:users:allow:rwxpdDaARWcCo:fd--"]
        calls = []

        class OwnPath:
            def __str__(self):
                return module.TARGET

            def is_dir(self):
                return True

            def is_symlink(self):
                return False

            def resolve(self):
                return self

            def stat(self):
                return types.SimpleNamespace(st_uid=1024, st_gid=100)

        def command(arguments, **_kwargs):
            calls.append(arguments)
            action = arguments[1]
            if arguments[0].endswith("synoshare"):
                output = "Comment ....[" + module.OWNER + "]"
            elif action == "-get":
                if not entries:
                    if _kwargs.get("check"):
                        import subprocess

                        raise subprocess.CalledProcessError(
                            255, arguments, output="It's Linux mode"
                        )
                    return types.SimpleNamespace(
                        returncode=255, stdout="It's Linux mode", stderr=""
                    )
                output = "ACL version: 1\n" + "\n".join(
                    f"[{i}] {entry} (level:0)" for i, entry in enumerate(entries)
                )
            elif action == "-del":
                entries.pop(int(arguments[-1]))
                output = ""
            elif action == "-add":
                entries.append(arguments[-1])
                output = ""
            elif action == "-get-perm":
                output = "Perm: [rwxpdDaARWcCo]"
            else:
                raise AssertionError("Comando inesperado")
            return types.SimpleNamespace(returncode=0, stdout=output, stderr="")

        with (
            patch("pathlib.Path", return_value=OwnPath()),
            patch("subprocess.run", side_effect=command) as remote,
            patch("os.chmod", side_effect=lambda *_args: entries.clear()) as chmod,
            patch("os.chown", create=True) as chown,
            redirect_stdout(io.StringIO()),
        ):
            exec(compile(module.ACL_SOURCE, "<ACL real>", "exec"), {})
            self.assertEqual(
                entries,
                ["user:admin:allow:rwxpdDaARWcCo:fd--", "user:jandrade:allow:rwxpdDaARWcCo:fd--"],
            )
            calls.clear()
            exec(compile(module.ACL_SOURCE, "<ACL real>", "exec"), {})
            self.assertFalse(any(call[1] in ("-del", "-add", "-set-owner") for call in calls))
            self.assertEqual(chmod.call_count, 1)
            chown.assert_not_called()
            remote.side_effect = lambda arguments, **kwargs: (
                types.SimpleNamespace(
                    returncode=255, stdout="Permission denied", stderr="lectura rechazada"
                )
                if arguments[1] == "-get"
                else command(arguments, **kwargs)
            )
            error_output = io.StringIO()
            with redirect_stderr(error_output), self.assertRaises(RuntimeError):
                exec(compile(module.ACL_SOURCE, "<otro255 rechazado>", "exec"), {})
            diagnostic = json.loads(error_output.getvalue())["fallo_herramienta"]
            self.assertEqual(diagnostic["codigo_salida"], 255)
            self.assertEqual(diagnostic["salida"], "Permission denied")
            self.assertNotIn(module.TARGET, error_output.getvalue())

    def test_permiso_privado_rompe_acl_heredada_antes_de_validar_modo(self):
        import stat

        state = {"mode": 0o711, "inherited": True}
        calls = []

        class BackupPath:
            def __str__(self):
                return module.BACKUP_PATH

            def lstat(self):
                return types.SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | state["mode"])

            def is_symlink(self):
                return False

            def is_dir(self):
                return True

            def is_file(self):
                return False

        def command(arguments, **_kwargs):
            calls.append(arguments)
            if arguments[1] == "-del":
                state["inherited"] = False
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(
                returncode=0 if state["inherited"] else 255,
                stdout="ACL version: 1" if state["inherited"] else "It's Linux mode",
                stderr="",
            )

        def chmod(_path, mode):
            if not state["inherited"]:
                state["mode"] = mode

        namespace = {}
        with patch("subprocess.run", side_effect=command), patch("os.chmod", side_effect=chmod):
            exec(compile(module.PRIVATE_MODE_SOURCE, "<permisos privados>", "exec"), namespace)
            namespace["make_private"](BackupPath(), 0o700, True)
        self.assertEqual(state, {"mode": 0o700, "inherited": False})
        self.assertTrue(any(call[1] == "-del" for call in calls))

    def test_reparacion_exige_sha_y_ruta_respaldados(self):
        for path, value in (("/etc", "a" * 64), (module.BACKUP_PATH, "CHANGE_ME")):
            with self.assertRaises(module.StorageError):
                module.repair_permissions_command(path, value)

    def test_fallo_conserva_paso_y_stderr_saneado_sin_comando(self):
        response = {
            "codigo_salida": 1,
            "truncado": False,
            "error": "Traceback:\nAssertionError\nsubprocess.CalledProcessError: Command ['/ruta/gestionada', '--opcion'] returned non-zero exit status 1.",
        }
        with self.assertRaises(module.StorageError) as raised:
            module.checked_result(response, "respaldo")
        diagnostic = raised.exception.diagnostic
        self.assertEqual(diagnostic["paso"], "respaldo")
        self.assertEqual(diagnostic["codigo_salida"], 1)
        self.assertIn("AssertionError", diagnostic["detalle"])
        self.assertNotIn("/ruta/gestionada", diagnostic["detalle"])


if __name__ == "__main__":
    unittest.main()
