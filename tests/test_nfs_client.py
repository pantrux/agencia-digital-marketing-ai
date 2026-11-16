"""Verifica reconciliación y guardas NFS con datos locales, sin montar recursos."""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "storage/nfs_client_config.py"
SPEC = importlib.util.spec_from_file_location("nfs_client_config", MODULE_PATH)
NFS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NFS)
FOREIGN = "# Raíz existente\nUUID=raiz / ext4 defaults 0 1\n192.168.0.9:/equipo /mnt/equipo nfs defaults 0 0\n"


class NfsClientTests(unittest.TestCase):
    def test_verificador_admite_destino_ausente_despues_de_prepararlo(self):
        bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
        script = MODULE_PATH.with_name("configure-nfs-client.sh")
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "fstab"
            candidate.write_text(NFS.ENTRY, encoding="utf-8")
            program = r"""
source "$1"
nfs_test_target_exists=false
nfs_mount_present() { return 1; }
nfs_check_empty_directory() { return 0; }
mkdir() { nfs_test_target_exists=true; }
findmnt() { [[ "$nfs_test_target_exists" == true ]]; }
nfs_verify_candidate "$2"
[[ "$nfs_test_target_exists" == true ]]
"""
            result = subprocess.run(
                [bash, "-c", program, "prueba", script.as_posix(), candidate.as_posix()],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verificador_no_crea_directorio_si_la_guarda_rechaza_destino(self):
        bash = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")
        script = MODULE_PATH.with_name("configure-nfs-client.sh")
        program = r"""
source "$1"
nfs_mount_present() { return 1; }
nfs_check_empty_directory() { nfs_fail 'Destino en conflicto'; }
mkdir() { printf 'CREACION_INDEBIDA'; }
findmnt() { printf 'VALIDACION_INDEBIDA'; }
nfs_verify_candidate /candidato-de-prueba
"""
        result = subprocess.run(
            [bash, "-c", program, "prueba", script.as_posix()],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Destino en conflicto", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_preserva_entradas_ajenas_y_es_idempotente(self):
        rendered = NFS.render_fstab(FOREIGN)
        self.assertTrue(rendered.startswith(FOREIGN))
        self.assertEqual(NFS.render_fstab(rendered), rendered)
        self.assertEqual(rendered.count(NFS.BEGIN_MARKER), 1)
        self.assertIn("192.168.0.8:/volume2/producciones /mnt/producciones nfs4 ", rendered)
        self.assertIn("vers=4.1", rendered)

    def test_reconcilia_solo_bloque_propio(self):
        original = (
            FOREIGN + NFS.BEGIN_MARKER + "\n"
            "192.168.0.8:/volume2/producciones /mnt/producciones nfs vers=4.0,rw 0 0\n"
            + NFS.END_MARKER
            + "\n# Fin ajeno\n"
        )
        rendered = NFS.render_fstab(original)
        self.assertTrue(rendered.startswith(FOREIGN))
        self.assertTrue(rendered.endswith("# Fin ajeno\n"))
        self.assertNotIn("vers=4.0", rendered)

    def test_rechaza_destino_ajeno(self):
        with self.assertRaisesRegex(ValueError, "ajena"):
            NFS.render_fstab(FOREIGN + "192.168.0.9:/equipo /mnt/producciones nfs rw 0 0\n")

    def test_no_adopta_entrada_sin_marcador(self):
        with self.assertRaisesRegex(ValueError, "ajena"):
            NFS.render_fstab(
                FOREIGN + "192.168.0.8:/volume2/producciones /mnt/producciones nfs4 vers=4.1 0 0\n"
            )

    def test_rechaza_volume1_incluso_con_marcador_o_montaje_previo(self):
        previous = "192.168.0.8:/volume1/producciones /mnt/producciones nfs4 vers=4.1,rw 0 0\n"
        for original in (
            FOREIGN + previous,
            FOREIGN + NFS.BEGIN_MARKER + "\n" + previous + NFS.END_MARKER + "\n",
        ):
            with self.subTest(original=original), self.assertRaisesRegex(ValueError, "ajena"):
                NFS.render_fstab(original)
        with self.assertRaises(ValueError):
            NFS.validate_mount(
                {
                    "filesystems": [
                        {
                            "source": "192.168.0.8:/volume1/producciones",
                            "target": "/mnt/producciones",
                            "fstype": "nfs4",
                            "options": "rw,vers=4.1,proto=tcp,hard",
                        }
                    ]
                }
            )

    def test_detecta_destino_codificado_o_con_barra_final(self):
        for target in (r"/mnt/\160roducciones", "/mnt/producciones/", "/mnt/otro/../producciones"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                NFS.render_fstab(FOREIGN + f"192.168.0.9:/equipo {target} nfs rw 0 0\n")

    def test_rechaza_bloque_incompleto_o_duplicado(self):
        for text in (
            FOREIGN + NFS.BEGIN_MARKER + "\n",
            NFS.render_fstab(FOREIGN) + NFS.render_fstab(""),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                NFS.render_fstab(text)

    def test_rechaza_entrada_ajena_dentro_de_bloque(self):
        original = (
            NFS.BEGIN_MARKER + "\n"
            "192.168.0.8:/volume2/producciones /mnt/producciones nfs4 vers=4.1 0 0\n"
            "192.168.0.9:/equipo /mnt/equipo nfs rw 0 0\n" + NFS.END_MARKER + "\n"
        )
        with self.assertRaises(ValueError):
            NFS.render_fstab(original)

    def test_acepta_solo_montaje_real_nfs41_rw_de_fuente_exacta(self):
        mount = {
            "filesystems": [
                {
                    "source": "192.168.0.8:/volume2/producciones",
                    "target": "/mnt/producciones",
                    "fstype": "nfs4",
                    "options": "rw,vers=4.1,proto=tcp,hard",
                }
            ]
        }
        NFS.validate_mount(mount)
        for key, value in (
            ("source", "192.168.0.9:/equipo"),
            ("fstype", "autofs"),
            ("options", "rw,vers=4.2,proto=tcp"),
            ("options", "ro,vers=4.1,proto=tcp"),
        ):
            altered = {"filesystems": [mount["filesystems"][0] | {key: value}]}
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                NFS.validate_mount(altered)

    def test_identidad_requiere_ip_y_hostname_reales(self):
        addresses = [
            {
                "ifname": "media0",
                "addr_info": [{"family": "inet", "local": "192.168.0.85", "prefixlen": 24}],
            }
        ]
        NFS.validate_identity("192.168.0.85", "vm-media-01", "vm-media-01", addresses)
        for target, expected, actual in (
            ("192.168.0.6", "windows", "windows"),
            ("192.168.0.7", "ubuntu-openclaw", "ubuntu-openclaw"),
            ("192.168.0.85", "vm-media-01", "otro"),
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                NFS.validate_identity(target, expected, actual, addresses)

    def test_admite_openclaw_sin_asumir_su_hostname(self):
        addresses = [
            {
                "ifname": "ens192",
                "addr_info": [{"family": "inet", "local": "192.168.0.7", "prefixlen": 24}],
            }
        ]
        NFS.validate_identity("192.168.0.7", "ubuntu-openclaw", "ubuntu-openclaw", addresses)


if __name__ == "__main__":
    unittest.main()
