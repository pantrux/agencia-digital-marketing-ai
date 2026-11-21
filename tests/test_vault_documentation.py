"""Comprueba ubicación y riesgos de los nuevos campos operativos sin leer secretos."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/ci"))
from document_vault import documentation


class PhaseFourVaultDocumentation(unittest.TestCase):
    def test_imagenes_y_rutas_de_plataforma_tienen_destino_y_tres_comentarios(self):
        for key in (
            "MEDIA_PLATFORM_INSTALL_ROOT",
            "MEDIA_PLATFORM_DATA_ROOT",
            "MEDIA_COMPOSE_PROJECT",
            "MEDIA_BUILD_PLATFORM",
            "MEDIA_ML_AUTH_BASE_IMAGE",
            "MEDIA_ML_AUTH_SOURCE_BUNDLE_SHA256",
        ):
            with self.subTest(key=key):
                block = documentation(key)
                self.assertEqual(len(block), 3)
                self.assertIn(key, block[0])
                self.assertIn(".85", block[1])
                self.assertTrue(block[2].startswith("# Riesgo al rotar:"))

    def test_herramientas_de_copia_identifican_los_dos_hosts(self):
        for key in (
            "BACKUP_TOOLS_MANIFEST_SHA256",
            "BACKUP_GO_SDK_VERSION",
            "RCLONE_BINARY_SHA256",
        ):
            with self.subTest(key=key):
                block = documentation(key)
                self.assertIn("Windows", block[1])
                self.assertIn(".6", block[1])
                self.assertIn(".85", block[1])

    def test_nuevo_grupo_no_oculta_identidad_vm_ni_admite_campos_desconocidos(self):
        self.assertIn("Identidad y red de la VM", documentation("MEDIA_VM_IP")[0])
        self.assertIn("clave anterior", documentation("RESTIC_PASSWORD")[2])
        with self.assertRaises(ValueError):
            documentation("CAMPO_SIN_UBICACION")

    def test_token_de_davinci_documenta_ambos_extremos_del_mcp(self):
        purpose, location, risk = documentation("DAVINCI_MCP_TOKEN")
        self.assertIn("Lucas", purpose)
        self.assertIn(".6", location)
        self.assertIn(".7", location)
        self.assertIn("ambos extremos", risk)
        with self.assertRaises(ValueError):
            documentation("DAVINCI_MCP_TOKEN_INCORRECTO")


if __name__ == "__main__":
    unittest.main()
