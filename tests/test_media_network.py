"""Prueba guardas y finalización de red sobre archivos temporales, sin red real."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "vcenter/finalize-media-network.py"
SPEC = importlib.util.spec_from_file_location("media_network", MODULE_PATH)
NETWORK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = NETWORK
SPEC.loader.exec_module(NETWORK)
MAC = "00:50:56:a7:b2:d5"
UUID = "11111111-2222-3333-4444-555555555555"
OBSERVED_NETWORK = {
    "version": 2,
    "renderer": "networkd",
    "ethernets": {
        "media0": {
            "match": {"macaddress": MAC},
            "optional": True,
            "addresses": ["192.168.0.85/24"],
            "nameservers": {"addresses": ["192.168.0.1", "1.1.1.1"]},
            "dhcp4": False,
            "dhcp6": False,
            "set-name": "media0",
            "routes": [{"to": "0.0.0.0/0", "via": "192.168.0.1"}],
        }
    },
}
OBSERVED_LEGACY = (
    "network:\n  version: 2\n  ethernets:\n    ens192:\n      dhcp4: true\n      dhcp6: false\n"
)


class MediaNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = NETWORK.network_paths(self.root, UUID)
        self.paths.active.parent.mkdir(parents=True)
        self.paths.cache.parent.mkdir(parents=True)
        self.paths.active.write_text(
            NETWORK.dump_yaml({"network": OBSERVED_NETWORK}), encoding="utf-8"
        )
        self.paths.legacy.write_text(OBSERVED_LEGACY, encoding="utf-8")
        self.paths.cache.write_text(json.dumps(OBSERVED_NETWORK), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_rechaza_configuracion_adicional_heredada(self):
        legacy = {
            "network": {
                "version": 2,
                "ethernets": {"ens192": {"dhcp4": True, "dhcp6": False, "mtu": 9000}},
            }
        }
        self.paths.legacy.write_text(NETWORK.dump_yaml(legacy), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "heredado"):
            NETWORK.build_plan(self.paths, MAC)
        self.assertTrue(self.paths.legacy.exists())

    def test_rechaza_mac_o_ip_distintas_en_cache(self):
        cache = copy.deepcopy(OBSERVED_NETWORK)
        cache["ethernets"]["media0"]["addresses"] = ["192.168.0.7/24"]
        self.paths.cache.write_text(json.dumps(cache), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "caché"):
            NETWORK.build_plan(self.paths, MAC)

    def test_rechaza_perfil_adicional_sin_modificarlo(self):
        other = self.paths.active.parent / "99-otro.yaml"
        other.write_text("network: {version: 2}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "adicional"):
            NETWORK.build_plan(self.paths, MAC)
        self.assertTrue(other.exists())

    def test_rechaza_claves_yaml_duplicadas(self):
        self.paths.active.write_text("network:\n  version: 2\n  version: 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicada"):
            NETWORK.build_plan(self.paths, MAC)

    def test_archiva_solo_legacy_y_no_aplica_red_viva(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0)

        plan = NETWORK.build_plan(self.paths, MAC)
        result = NETWORK.apply_plan(self.paths, plan, runner, "prueba")
        self.assertFalse(self.paths.legacy.exists())
        self.assertTrue((Path(result["backup_directory"]) / "50-lab-dhcp.yaml").exists())
        self.assertFalse(
            NETWORK.load_yaml(self.paths.active)["network"]["ethernets"]["media0"]["optional"]
        )
        self.assertFalse(
            json.loads(self.paths.cache.read_text(encoding="utf-8"))["ethernets"]["media0"][
                "optional"
            ]
        )
        self.assertEqual(calls, [["netplan", "generate"], ["cloud-init", "schema", "--system"]])
        self.assertTrue(result["reboot_verification_required"])

    def test_idempotencia_no_crea_otro_backup(self):
        def runner(arguments):
            return subprocess.CompletedProcess(arguments, 0)

        NETWORK.apply_plan(self.paths, NETWORK.build_plan(self.paths, MAC), runner, "primera")
        plan = NETWORK.build_plan(self.paths, MAC)
        self.assertFalse(plan.changed)
        result = NETWORK.apply_plan(self.paths, plan, runner, "segunda")
        self.assertIsNone(result["backup_directory"])
        self.assertEqual(len(list(self.paths.backups.iterdir())), 1)

    def test_fallo_generate_restaura_los_archivos(self):
        original_active = self.paths.active.read_bytes()
        original_cache = self.paths.cache.read_bytes()
        count = 0

        def runner(arguments):
            nonlocal count
            count += 1
            return subprocess.CompletedProcess(arguments, 1 if count == 1 else 0)

        with self.assertRaisesRegex(RuntimeError, "restaur"):
            NETWORK.apply_plan(self.paths, NETWORK.build_plan(self.paths, MAC), runner, "fallo")
        self.assertEqual(self.paths.active.read_bytes(), original_active)
        self.assertEqual(self.paths.cache.read_bytes(), original_cache)
        self.assertTrue(self.paths.legacy.exists())

    def test_rechaza_cambio_entre_plan_y_aplicacion(self):
        plan = NETWORK.build_plan(self.paths, MAC)
        self.paths.cache.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cambi"):
            NETWORK.apply_plan(self.paths, plan, lambda arguments: None, "carrera")
        self.assertTrue(self.paths.legacy.exists())

    def test_conserva_archivo_temporal_preexistente(self):
        plan = NETWORK.build_plan(self.paths, MAC)
        temporary = self.paths.active.with_name(
            "." + self.paths.active.name + ".media-finalize.tmp"
        )
        temporary.write_bytes(b"archivo ajeno")
        with self.assertRaisesRegex(ValueError, "temporal"):
            NETWORK.apply_plan(self.paths, plan, lambda arguments: None, "colision")
        self.assertEqual(temporary.read_bytes(), b"archivo ajeno")
        self.assertTrue(self.paths.legacy.exists())

    def test_rechaza_perfil_agregado_despues_del_plan(self):
        plan = NETWORK.build_plan(self.paths, MAC)
        other = self.paths.active.parent / "99-otro.yaml"
        other.write_text("network: {version: 2}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "adicional"):
            NETWORK.apply_plan(self.paths, plan, lambda arguments: None, "perfil_nuevo")
        self.assertTrue(self.paths.legacy.exists())
        self.assertTrue(other.exists())

    def test_guardas_rechazan_uuid_host_o_mac_ajenos(self):
        context = {
            "system": "Linux",
            "euid": 0,
            "hostname": "vm-media-01",
            "os_id": "ubuntu",
            "os_version": "24.04",
            "instance_id": UUID,
            "resolved_instance": str(self.paths.expected_instance.resolve()),
            "interface": {
                "ifname": "media0",
                "address": MAC,
                "addr_info": [{"family": "inet", "local": "192.168.0.85", "prefixlen": 24}],
            },
        }
        NETWORK.validate_context(context, self.paths, MAC, UUID)
        for key, value in (
            ("hostname", "otro"),
            ("instance_id", "22222222-2222-3333-4444-555555555555"),
            ("euid", 1000),
        ):
            invalid = context | {key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                NETWORK.validate_context(invalid, self.paths, MAC, UUID)
        invalid = context | {"interface": context["interface"] | {"address": "00:50:56:00:00:00"}}
        with self.assertRaises(ValueError):
            NETWORK.validate_context(invalid, self.paths, MAC, UUID)


if __name__ == "__main__":
    unittest.main()
