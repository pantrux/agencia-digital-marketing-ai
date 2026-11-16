"""Pruebas de gobierno de credenciales y avance de fases sin infraestructura."""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))


class GobiernoCredenciales(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "check_env", ROOT / "scripts" / "ci" / "check_env.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_parsea_literal_sin_ejecutar_sustituciones(self):
        datos, errores = self.module.parse_env('TOKEN="$(comando)"\nPUERTO=443\n')
        self.assertEqual(datos, {"TOKEN": "$(comando)", "PUERTO": "443"})
        self.assertEqual(errores, [])

    def test_detecta_duplicados_sin_imprimir_valores(self):
        _, errores = self.module.parse_env("TOKEN=valor_privado\nTOKEN=otro_privado\n")
        self.assertTrue(errores)
        self.assertNotIn("valor_privado", " ".join(errores))

    def test_reconoce_reservas_de_fases(self):
        for valor in ["CHANGE_ME", "GENERAR_EN_FASE_1", "pendiente", "", "<completar>"]:
            with self.subTest(valor=valor):
                self.assertTrue(self.module.is_pending(valor))

    def test_plantilla_rechaza_valores_incluso_no_secretos(self):
        errores = self.module.validate_template({"IP": "192.168.0.85"})
        self.assertTrue(errores)
        self.assertNotIn("192.168.0.85", " ".join(errores))

    def test_palabra_independiente_no_es_reserva_pendiente(self):
        self.assertFalse(self.module.is_pending("PRIMER_USO_SIN_COTEJO_INDEPENDIENTE"))

    def test_plantilla_coincide_con_claves_boveda(self):
        errores = self.module.compare_keys({"A": "privado"}, {"B": "CHANGE_ME"})
        self.assertEqual(len(errores), 2)


if __name__ == "__main__":
    unittest.main()
