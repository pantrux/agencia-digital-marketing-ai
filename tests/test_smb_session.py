"""Valida evidencia SMB suministrada, sin conexiones ni credenciales reales."""

import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "storage/verify_smb_session.py"
SPEC = importlib.util.spec_from_file_location("verify_smb_session", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def snapshot():
    server = {"pid": "123", "task_id": "0", "vnn": "4294967295", "unique_id": "456"}
    return {
        "timestamp": "2026-11-05T19:07:59.370590-0300",
        "version": "4.15.13",
        "sessions": {
            "100": {
                "session_id": "100",
                "server_id": server,
                "uid": 1026,
                "username": "jandrade",
                "remote_machine": "192.168.0.6",
                "hostname": "ipv4:192.168.0.6:51900",
                "session_dialect": "SMB3_11",
                "encryption": {"cipher": "AES-128-GCM", "degree": "partial"},
            }
        },
        "tcons": {
            "200": {
                "tcon_id": "200",
                "session_id": "100",
                "server_id": copy.deepcopy(server),
                "service": "producciones",
                "machine": "192.168.0.6",
                "encryption": {"cipher": "AES-128-GCM", "degree": "full"},
            }
        },
        "open_files": {"no-publicar": {"filename": "dato-privado-de-prueba"}},
    }


class PruebasSesionSmb(unittest.TestCase):
    def test_acredita_recurso_cifrado_aunque_sesion_general_sea_parcial(self):
        evidence = MODULE.verify(snapshot())
        self.assertTrue(evidence["verificado"])
        self.assertEqual(evidence["dialectos"], ["SMB3_11"])
        self.assertEqual(evidence["cifrados"], ["AES-128-GCM"])
        self.assertNotIn("dato-privado", json.dumps(evidence))

    def test_sin_conexion_a_producciones_no_acredita(self):
        data = snapshot()
        data["tcons"] = {}
        with self.assertRaises(ValueError):
            MODULE.verify(data)

    def test_firma_no_sustituye_cifrado_del_recurso(self):
        data = snapshot()
        data["tcons"]["200"]["encryption"] = {"cipher": "", "degree": "none"}
        data["tcons"]["200"]["signing"] = {"cipher": "AES-128-GMAC", "degree": "full"}
        with self.assertRaises(ValueError):
            MODULE.verify(data)

    def test_pid_coincidente_sin_identidad_completa_no_acredita(self):
        data = snapshot()
        data["tcons"]["200"]["server_id"]["unique_id"] = "999"
        with self.assertRaises(ValueError):
            MODULE.verify(data)

    def test_smb2_no_acredita(self):
        data = snapshot()
        data["sessions"]["100"]["session_dialect"] = "SMB2_10"
        with self.assertRaises(ValueError):
            MODULE.verify(data)

    def test_otro_usuario_ip_o_recurso_no_acredita(self):
        for section, key, field, value in (
            ("sessions", "100", "username", "otra-persona"),
            ("sessions", "100", "remote_machine", "192.168.0.60"),
            ("tcons", "200", "machine", "192.168.0.60"),
            ("tcons", "200", "service", "otro-proyecto"),
        ):
            with self.subTest(field=field):
                data = snapshot()
                data[section][key][field] = value
                with self.assertRaises(ValueError):
                    MODULE.verify(data)

    def test_cifrado_parcial_anonimo_o_algoritmo_de_firma_no_acredita(self):
        for encryption in (
            {"cipher": "AES-128-GCM", "degree": "partial"},
            {"cipher": "AES-128-GCM", "degree": "anonymous"},
            {"cipher": "AES-128-GMAC", "degree": "full"},
            {"cipher": "", "degree": "full"},
        ):
            with self.subTest(encryption=encryption):
                data = snapshot()
                data["tcons"]["200"]["encryption"] = encryption
                with self.assertRaises(ValueError):
                    MODULE.verify(data)

    def test_vinculos_ausentes_o_ambiguos_no_acreditan(self):
        for section, key, field, value in (
            ("tcons", "200", "session_id", "999"),
            ("tcons", "200", "tcon_id", "999"),
            ("sessions", "100", "session_id", "999"),
            ("sessions", "100", "server_id", {"pid": "123"}),
            ("sessions", "100", "uid", -1),
        ):
            with self.subTest(field=field):
                data = snapshot()
                data[section][key][field] = value
                with self.assertRaises(ValueError):
                    MODULE.verify(data)

    def test_no_oculta_conexion_sin_cifrar_junto_a_otra_valida(self):
        data = snapshot()
        data["tcons"]["201"] = copy.deepcopy(data["tcons"]["200"])
        data["tcons"]["201"]["tcon_id"] = "201"
        data["tcons"]["201"]["encryption"]["degree"] = "none"
        with self.assertRaises(ValueError):
            MODULE.verify(data)

    def test_sesiones_y_archivos_ajenos_no_aparecen_en_evidencia(self):
        data = snapshot()
        data["sessions"]["999"] = {"username": "otra-persona-privada"}
        data["tcons"]["999"] = {"service": "share-privado-ajeno"}
        evidence = json.dumps(MODULE.verify(data))
        self.assertNotIn("otra-persona", evidence)
        self.assertNotIn("share-privado", evidence)
        self.assertNotIn("open_files", evidence)
        self.assertNotIn("server_id", evidence)

    def test_tipo_de_dialecto_invalido_rechaza_sin_error_de_tipo(self):
        data = snapshot()
        data["sessions"]["100"]["session_dialect"] = []
        with self.assertRaises(ValueError):
            MODULE.verify(data)

    def test_cli_valido_solo_emite_evidencia_saneada(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(snapshot()),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["verificado"])
        self.assertNotIn("dato-privado", result.stdout + result.stderr)

    def test_cli_rechaza_claves_duplicadas_y_entrada_excesiva(self):
        for content in ('{"sessions":{},"sessions":{},"tcons":{}}', " " * (MODULE.MAX_INPUT + 1)):
            with self.subTest(tamano=len(content)):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT)],
                    input=content,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["codigo"], "ENTRADA_INVALIDA")

    def test_cli_no_reproduce_entrada_invalida(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="dato-privado-no-json",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("dato-privado", result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)["verificado"])


if __name__ == "__main__":
    unittest.main()
