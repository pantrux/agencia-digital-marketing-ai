"""Pruebas de auditoría DSM con credenciales ficticias y transporte sin red."""

import base64
import hashlib
import importlib.util
import io
import json
import struct
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "synology_storage_audit", ROOT / "storage/synology_storage_audit.py"
)
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)
WIRE = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(32)
PUBLIC = "ssh-ed25519 " + base64.b64encode(WIRE).decode()
PIN = "SHA256:" + base64.b64encode(hashlib.sha256(WIRE).digest()).decode().rstrip("=")
PASSWORD = "prueba-local-sin-validez"


def vault_text(password=PASSWORD, pin=PIN):
    return f"""SYNOLOGY_HOST=192.168.0.8
SYNOLOGY_SSH_PORT=2222
SYNOLOGY_USER=jandrade
SYNOLOGY_PASSWORD='{password}'
SYNOLOGY_SSH_HOST_PUBLIC_KEY={PUBLIC}
SYNOLOGY_SSH_HOSTKEY_SHA256={pin}
SYNOLOGY_STORAGE_VOLUME=/volume2
SYNOLOGY_PRODUCCIONES_PATH=/volume2/producciones
SYNOLOGY_BACKUPS_PATH=/volume2/producciones/_backups
SYNOLOGY_NFS_EXPORT=192.168.0.8:/volume2/producciones
"""


class FakeChannel:
    def __init__(self, output=b"", error=b"", code=0):
        self.output = output
        self.error = error
        self.code = code
        self.eof_sent = False
        self.closed = False

    def recv_ready(self):
        return bool(self.output)

    def recv(self, size):
        value, self.output = self.output[:size], self.output[size:]
        return value

    def recv_stderr_ready(self):
        return bool(self.error)

    def recv_stderr(self, size):
        value, self.error = self.error[:size], self.error[size:]
        return value

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self.code

    def shutdown_write(self):
        self.eof_sent = True

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.closed = False

    def exec_command(self, command, timeout):
        output, error, code = self.replies.pop(0)
        channel = FakeChannel(output, error, code)
        stdin = io.StringIO()
        stdin.channel = channel
        self.calls.append(
            {"command": command, "stdin": stdin, "channel": channel, "timeout": timeout}
        )
        return stdin, types.SimpleNamespace(channel=channel), types.SimpleNamespace(channel=channel)

    def close(self):
        self.closed = True


class SynologyAuditTests(unittest.TestCase):
    def settings(self, path, text=None):
        path.write_text(text or vault_text(), encoding="utf-8")
        return audit.load_settings(path, "password")

    def test_lector_no_evalua_y_conserva_numeral_y_metacaracteres(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = "$(comando-inexistente); # texto literal"
            settings = self.settings(Path(tmp) / ".env", vault_text(literal))
            self.assertEqual(settings.password, literal)
            self.assertNotIn(literal, repr(settings))

    def test_comentario_inline_sin_comillas_no_forma_parte_del_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = vault_text().replace(
                f"SYNOLOGY_PASSWORD='{PASSWORD}'",
                f"SYNOLOGY_PASSWORD={PASSWORD} # comentario de uso",
            )
            settings = self.settings(Path(tmp) / ".env", text)
            self.assertEqual(settings.password, PASSWORD)

    def test_credencial_pendiente_impide_cualquier_conexion(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / ".env"
            vault.write_text(vault_text("CHANGE_ME"), encoding="utf-8")
            connector = Mock(side_effect=AssertionError("No debe conectar"))
            result = audit.audit_vault(vault, connector=connector)
            self.assertFalse(result["autenticado"])
            self.assertEqual(result["error"]["codigo"], "CREDENCIAL_PENDIENTE")
            connector.assert_not_called()

    def test_reservas_de_fases_impiden_cualquier_conexion(self):
        for placeholder in (
            "COMPLETAR_EN_FASE_2",
            "GENERAR_EN_FASE_2",
            "CONFIGURAR_EN_FASE_6",
            "POR_DEFINIR",
            "<contraseña>",
        ):
            with self.subTest(placeholder=placeholder), tempfile.TemporaryDirectory() as tmp:
                vault = Path(tmp) / ".env"
                vault.write_text(vault_text(placeholder), encoding="utf-8")
                connector = Mock(side_effect=AssertionError("No debe conectar"))
                result = audit.audit_vault(vault, connector=connector)
                self.assertEqual(result["error"]["codigo"], "CREDENCIAL_PENDIENTE")
                connector.assert_not_called()

    def test_pin_incorrecto_se_rechaza_antes_de_conectar(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / ".env"
            vault.write_text(vault_text(pin="SHA256:" + "A" * 43), encoding="utf-8")
            connector = Mock(side_effect=AssertionError("No debe conectar"))
            result = audit.audit_vault(vault, connector=connector)
            self.assertEqual(result["error"]["codigo"], "PIN_INVALIDO")
            connector.assert_not_called()

    def test_ruta_distinta_de_la_eleccion_respaldada_impide_conexion(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / ".env"
            vault.write_text(vault_text().replace("/volume2", "/volume1"), encoding="utf-8")
            connector = Mock(side_effect=AssertionError("No debe conectar"))
            result = audit.audit_vault(vault, connector=connector)
            self.assertEqual(result["error"]["codigo"], "ALMACENAMIENTO_INVALIDO")
            connector.assert_not_called()

    def test_consultas_usan_solo_la_ruta_volume2_aprobada(self):
        directory_command = next(
            command for name, command, _ in audit.COMMANDS if name == "directorios_producciones"
        )
        export_command = next(
            command for name, command, _ in audit.COMMANDS if name == "export_producciones"
        )
        self.assertIn("/volume2/producciones", directory_command)
        self.assertIn(r"\/volume2\/producciones", export_command)
        self.assertNotIn("volume1", directory_command + export_command)

    def test_claves_duplicadas_de_boveda_se_rechazan(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(audit.AuditError):
                self.settings(Path(tmp) / ".env", vault_text() + "SYNOLOGY_USER=otro\n")

    def test_redaccion_oculta_valor_y_forma_escapada(self):
        value = 'prueba-"sin-validez'
        result = audit.redact({"salida": value, "error": json.dumps(value)[1:-1]}, [value])
        self.assertNotIn(value, json.dumps(result))
        self.assertNotIn(json.dumps(value)[1:-1], result["error"])
        self.assertEqual(result["salida"], "[OCULTO]")

    def test_sudo_prueba_n_antes_de_enviar_password_solo_por_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp) / ".env")
            client = FakeClient([(b"", b"sudo: password required", 1), (b"", b"", 0)])
            reader = audit.RemoteReader(client, settings)
            mode, _probes = reader.prepare_sudo()
            self.assertEqual(mode, "password")
            self.assertEqual(client.calls[0]["command"], "sudo -n true")
            self.assertEqual(client.calls[0]["stdin"].getvalue(), "")
            self.assertTrue(client.calls[1]["command"].startswith("sudo -S"))
            self.assertEqual(client.calls[1]["stdin"].getvalue(), PASSWORD + "\n")
            self.assertTrue(client.calls[1]["channel"].eof_sent)
            self.assertTrue(all(PASSWORD not in call["command"] for call in client.calls))

    def test_sudo_n_exitoso_no_transmite_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp) / ".env")
            client = FakeClient([(b"", b"", 0)])
            mode, _probes = audit.RemoteReader(client, settings).prepare_sudo()
            self.assertEqual(mode, "noninteractive")
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0]["stdin"].getvalue(), "")

    def test_sudo_no_envia_password_ante_orden_no_disponible(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp) / ".env")
            client = FakeClient([(b"", b"sudo: command not found", 127)])
            mode, _probes = audit.RemoteReader(client, settings).prepare_sudo()
            self.assertEqual(mode, "none")
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0]["stdin"].getvalue(), "")

    def test_resultado_remoto_se_sanitiza_antes_de_reportar(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp) / ".env")
            client = FakeClient([(PASSWORD.encode(), PASSWORD.encode(), 1)])
            result = audit.RemoteReader(client, settings).run("id")
            self.assertNotIn(PASSWORD, json.dumps(result))

    def test_error_de_conexion_no_publica_secreto_ni_reintenta(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / ".env"
            vault.write_text(vault_text(), encoding="utf-8")
            connector = Mock(side_effect=RuntimeError(PASSWORD))
            result = audit.audit_vault(vault, connector=connector)
            self.assertFalse(result["autenticado"])
            self.assertNotIn(PASSWORD, json.dumps(result))
            connector.assert_called_once()

    def test_flujo_continuo_no_impide_comprobar_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp) / ".env")

            class ContinuousChannel(FakeChannel):
                def recv_ready(self):
                    return True

                def recv(self, _size):
                    self.code += 1
                    if self.code > 4:
                        raise AssertionError("El drenaje no comprobó el plazo")
                    return b"x"

                def exit_status_ready(self):
                    return False

            channel = ContinuousChannel()
            stdin = io.StringIO()
            stdin.channel = channel
            client = Mock()
            client.exec_command.return_value = (
                stdin,
                types.SimpleNamespace(channel=channel),
                types.SimpleNamespace(channel=channel),
            )
            with patch.object(audit.time, "monotonic", side_effect=[0, settings.timeout + 1]):
                result = audit.RemoteReader(client, settings).run("hostname")
            self.assertEqual(result["codigo_salida"], 124)
            self.assertTrue(channel.closed)

    def test_lectura_parcial_no_se_presenta_como_completa(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / ".env"
            vault.write_text(vault_text(), encoding="utf-8")
            replies = [(b"", b"", 0), (b"", b"consulta no disponible", 1)] + [(b"", b"", 0)] * (
                len(audit.COMMANDS) - 1
            )
            client = FakeClient(replies)
            result = audit.audit_vault(vault, connector=lambda _settings: client)
            self.assertFalse(result.get("auditoria_completa", True))
            self.assertTrue(client.closed)

    def test_error_al_cerrar_se_reporta_sin_publicar_secreto(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / ".env"
            vault.write_text(vault_text(), encoding="utf-8")
            client = FakeClient([(b"", b"", 0)] * (len(audit.COMMANDS) + 1))
            client.close = Mock(side_effect=RuntimeError(PASSWORD))
            result = audit.audit_vault(vault, connector=lambda _settings: client)
            self.assertIn("error", result)
            self.assertNotIn(PASSWORD, json.dumps(result))

    def test_sdk_real_exige_reject_policy_y_no_busca_otras_identidades(self):
        import paramiko

        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp) / ".env")
            key = paramiko.PKey.from_type_string("ssh-ed25519", WIRE)
            transport = types.SimpleNamespace(get_remote_server_key=lambda: key)
            with (
                patch.object(paramiko.SSHClient, "connect") as connect,
                patch.object(paramiko.SSHClient, "get_transport", return_value=transport),
            ):
                client = audit.connect_client(settings)
                self.assertIsInstance(client._policy, paramiko.RejectPolicy)
                self.assertEqual(
                    client.get_host_keys()["[192.168.0.8]:2222"]["ssh-ed25519"].asbytes(), WIRE
                )
                self.assertFalse(connect.call_args.kwargs["look_for_keys"])
                self.assertFalse(connect.call_args.kwargs["allow_agent"])
                self.assertEqual(connect.call_args.kwargs["password"], PASSWORD)
                client.close()

    def test_salida_no_puede_sobrescribir_boveda(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / ".env"
            raw = vault_text().encode()
            vault.write_bytes(raw)
            with redirect_stdout(io.StringIO()):
                code = audit.main(["--vault-path", str(vault), "--output", str(vault)])
            self.assertNotEqual(code, 0)
            self.assertEqual(vault.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
