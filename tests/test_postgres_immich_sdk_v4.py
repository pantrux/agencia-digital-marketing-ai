"""Separación del intento V4 y diferencia declarativa cerrada, sin infraestructura."""

import hashlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "docker/postgres-immich"
KIT = "03107096f926423b08a91f58269628bcd99232c5d89ba348246e65d3bd62bb4a"
ID = KIT[:32]
REMOTE_REPLACEMENTS = (
    ("d3f427770834e2d2c20337a9a3d3c816", ID),
    (
        "c77cdcbc4c327db69fd72eeaa01ef73a77fb333fa199b2f049129e6dcfe0899e",
        "db933c4289ac90eae8f1c125132c63616e5e08d1c6ee776ee8abde40e93e69b5",
    ),
    ("d3f427770834e2d2c20337a9a3d3c8161a191a16d2cb174ea2d66cdfab4b03a0", KIT),
)
LOCAL_REPLACEMENTS = (
    ("MEDIA_POSTGRES_IMMICH_SDK_", "MEDIA_POSTGRES_IMMICH_SDK_V4_"),
    (
        "dc23c9100b38b36f9973319ab045976fa627aeabbbf80c1618b976d2a643ad3e",
        "a5994aeb2c0ca62d27a553912eb3bc41157555abeba90482159f2e2b34017384",
    ),
    ("offline_sdk_remote.py", "offline_sdk_remote_v4.py"),
    (
        "phase4-postgres-immich-offline-container-plan.json",
        "phase4-postgres-immich-offline-container-plan-v2.json",
    ),
    ("phase4-postgres-immich-offline-inputs.json", "phase4-postgres-immich-offline-inputs-v2.json"),
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class SeparateAttempt(unittest.TestCase):
    def test_diff_cerrado_preserva_algoritmos_congelados(self):
        for old_name, new_name, expected, replacements in (
            (
                "run_offline_sdk.py",
                "run_offline_sdk_v4.py",
                "f4db7217da149f5283629223f4176e3d6b28e44acd0fc41441171682d9b0cf39",
                LOCAL_REPLACEMENTS,
            ),
            (
                "offline_sdk_remote.py",
                "offline_sdk_remote_v4.py",
                "3d3e0e2638da5ccc1ebb035f2dd348d9dad078faae5365545654c9bd830d7dea",
                REMOTE_REPLACEMENTS,
            ),
        ):
            original = (SOURCES / old_name).read_bytes()
            self.assertEqual(hashlib.sha256(original).hexdigest(), expected)
            derived = original
            # Los valores largos se sustituyen primero para evitar coincidencias por prefijo.
            for before, after in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
                self.assertIn(before.encode(), derived)
                derived = derived.replace(before.encode(), after.encode())
            if old_name == "run_offline_sdk.py":
                # Único ajuste de formato exigido al alargar la ruta; mismo argumento.
                before = b'                PROJECT / ".tools/evidence/phase4-postgres-immich-offline-inputs-v2.json", 256 * 1024\n'
                after = b'                PROJECT / ".tools/evidence/phase4-postgres-immich-offline-inputs-v2.json",\n                256 * 1024,\n'
                self.assertEqual(derived.count(before), 1)
                derived = derived.replace(before, after)
            self.assertEqual((SOURCES / new_name).read_bytes(), derived)

    def test_no_foreign_no_adopta_el_intento_anterior(self):
        old = load("pg_v3_guard", SOURCES / "offline_sdk_remote.py")
        new = load("pg_v4_guard", SOURCES / "offline_sdk_remote_v4.py")
        helpers = load("pg_fixture", ROOT / "tests/test_postgres_immich_sdk_controller.py")
        own = helpers.container(new)
        own["HostConfig"]["CapDrop"] = ["CAP_NET_RAW"]
        new.check_container(own, "a" * 64)
        with self.assertRaises(ValueError):
            old.check_container(own, "a" * 64)
        with self.assertRaises(ValueError):
            new.check_container(helpers.container(old), "a" * 64)
        self.assertEqual(new.BUILD_ID, ID)
        self.assertEqual(new.KIT_SHA, KIT)

    def test_prefijo_nuevo_preserva_los_cuatro_campos_viejos(self):
        old = load("pg_old_keys", SOURCES / "run_offline_sdk.py")
        new = load("pg_new_keys", SOURCES / "run_offline_sdk_v4.py")
        self.assertEqual(new.PREFIX, "MEDIA_POSTGRES_IMMICH_SDK_V4_")
        self.assertEqual(len(new.KEYS), 4)
        self.assertFalse(set(new.KEYS.values()) & set(old.KEYS.values()))
        with mock.patch.object(new, "connect") as connection:
            with self.assertRaises(ValueError):
                new.bind_plan({}, {key: "historia" for key in old.KEYS.values()})
        connection.assert_not_called()

    def test_historico_regenerado_usa_solo_los_binds_nuevos(self):
        old = load("pg_old_mounts", SOURCES / "offline_sdk_remote.py")
        new = load("pg_new_mounts", SOURCES / "offline_sdk_remote_v4.py")
        recipe = load("pg_fixed_recipe", SOURCES / "offline_recipe.py")
        arguments = new.derive_create_arguments(
            recipe.container_plan(new.INPUTS, new.OUTPUTS, KIT)["create_arguments"]
        )
        self.assertIn(f"type=bind,src={new.INPUTS},dst=/inputs,readonly", arguments)
        self.assertIn(f"type=bind,src={new.OUTPUTS},dst=/outputs", arguments)
        self.assertFalse(any(old.INPUTS in a or old.OUTPUTS in a for a in arguments))

    def test_plan_predeterminado_v4_no_lee_boveda_ni_conecta(self):
        local = load("pg_v4_default_plan", SOURCES / "run_offline_sdk_v4.py")
        with (
            mock.patch.object(local, "proposal", return_value={"kit_sha256": KIT}),
            mock.patch.object(local, "read_values") as vault,
            mock.patch.object(local, "connect") as connection,
            mock.patch("sys.stdout", new=io.StringIO()) as output,
        ):
            self.assertEqual(local.main([]), 0)
        vault.assert_not_called()
        connection.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["kit_sha256"], KIT)


if __name__ == "__main__":
    unittest.main()
