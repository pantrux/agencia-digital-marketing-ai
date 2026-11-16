"""Contrato de directorios propios con inventario simulado; nunca toca /opt ni /var/lib."""

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("media_host_layout", ROOT / "docker/host_layout.py")
LAYOUT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAYOUT)

HOST = {"system": "Linux", "hostname": "vm-media-01", "ipv4": ["192.168.0.85"], "uid": 0}


class MemoryFilesystem:
    """Modelo de operaciones mínimas para comprobar guardas y orden antes de escribir."""

    def __init__(self):
        self.ancestors = {
            path: {
                "kind": "directory",
                "uid": 0,
                "gid": 0,
                "mode": 0o755,
                "device": 1,
                "inode": number,
            }
            for number, path in enumerate(("/", "/opt", "/var", "/var/lib"), 1)
        }
        self.targets = {path: None for path in LAYOUT.TARGETS}
        self.writes = []
        self.fail_on = None

    def inspect(self):
        return {"ancestors": copy.deepcopy(self.ancestors), "targets": copy.deepcopy(self.targets)}

    def create_root(self, path, approved_hash, identity):
        if self.fail_on == path:
            raise OSError("fallo simulado")
        if self.targets[path] is not None:
            raise FileExistsError(path)
        node = {
            "kind": "directory",
            "uid": 0,
            "gid": 0,
            "mode": 0o700,
            "device": 1,
            "inode": len(self.writes) + 100,
        }
        marker = LAYOUT.new_marker(path, node, approved_hash, "2026-11-06T00:00:00+00:00")
        self.targets[path] = {"node": node, "marker": marker, "receipt": None, "extra_entries": 0}
        self.writes.append(("create", path))

    def finish_receipt(self, path, approved_hash, identity):
        target = self.targets[path]
        target["receipt"] = LAYOUT.new_receipt(target["marker"], approved_hash)
        self.writes.append(("receipt", path))


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.fs = MemoryFilesystem()

    def plan(self, host=None):
        return LAYOUT.make_plan(self.fs.inspect(), host or HOST)

    def apply(self, plan=None, host=None):
        selected = plan or self.plan()
        return LAYOUT.apply_plan(self.fs, host or HOST, selected, selected["plan_sha256"])

    def test_plan_sin_efectos_y_solo_dos_destinos_fijos(self):
        plan = self.plan()
        self.assertEqual(self.fs.writes, [])
        self.assertEqual([row["path"] for row in plan["targets"]], list(LAYOUT.TARGETS))
        self.assertEqual([row["action"] for row in plan["targets"]], ["create", "create"])
        self.assertEqual(plan["plan_sha256"], LAYOUT.plan_hash(plan))

    def test_crea_ambos_y_replanificacion_es_idempotente(self):
        result = self.apply()
        self.assertEqual(len(result["targets"]), 2)
        initial = copy.deepcopy(self.fs.targets)
        self.fs.writes.clear()
        result = self.apply()
        self.assertEqual(self.fs.writes, [])
        self.assertEqual(self.fs.targets, initial)
        self.assertTrue(all(not row["changed"] for row in result["targets"]))

    def test_colision_en_segundo_destino_no_crea_primero(self):
        self.fs.targets["/var/lib/media-platform"] = {"node": {"kind": "directory"}}
        with self.assertRaises(LAYOUT.LayoutError):
            self.apply()
        self.assertEqual(self.fs.writes, [])

    def test_plan_obsoleto_se_rechaza_antes_de_crear(self):
        plan = self.plan()
        self.fs.ancestors["/opt"]["inode"] += 1
        with self.assertRaises(LAYOUT.LayoutError):
            self.apply(plan)
        self.assertEqual(self.fs.writes, [])

    def test_hash_incorrecto_o_plan_alterado_no_muta(self):
        plan = self.plan()
        with self.assertRaises(LAYOUT.LayoutError):
            LAYOUT.apply_plan(self.fs, HOST, plan, "0" * 64)
        plan["targets"][0]["path"] = "/opt/ajeno"
        with self.assertRaises(LAYOUT.LayoutError):
            LAYOUT.apply_plan(self.fs, HOST, plan, plan["plan_sha256"])
        self.assertEqual(self.fs.writes, [])

    def test_apply_exige_linux_root_hostname_e_ip(self):
        for key, value in (
            ("system", "Windows"),
            ("uid", 1000),
            ("hostname", "otro"),
            ("ipv4", ["192.168.0.7"]),
        ):
            host = dict(HOST, **{key: value})
            with self.subTest(field=key), self.assertRaises(LAYOUT.LayoutError):
                self.apply(self.plan(), host)
        self.assertEqual(self.fs.writes, [])

    def test_plan_puede_inspeccionarse_sin_ser_root(self):
        self.assertEqual(self.plan(dict(HOST, uid=1000)), self.plan())

    def test_ancestro_enlace_ajeno_o_escribible_impide_toda_mutacion(self):
        for field, value in (("kind", "symlink"), ("uid", 1000), ("gid", 1000), ("mode", 0o777)):
            with self.subTest(field=field):
                filesystem = MemoryFilesystem()
                filesystem.ancestors["/var/lib"][field] = value
                with self.assertRaises(LAYOUT.LayoutError):
                    LAYOUT.make_plan(filesystem.inspect(), HOST)
                self.assertEqual(filesystem.writes, [])

    def test_destino_enlace_nunca_se_adopta(self):
        self.fs.targets["/opt/media-platform"] = {
            "node": {"kind": "symlink", "uid": 0, "gid": 0, "mode": 0o700, "device": 1, "inode": 55}
        }
        with self.assertRaises(LAYOUT.LayoutError):
            self.plan()
        self.assertEqual(self.fs.writes, [])

    def test_directorio_vacio_sin_marcador_no_se_adopta(self):
        self.fs.targets["/opt/media-platform"] = {
            "node": {
                "kind": "directory",
                "uid": 0,
                "gid": 0,
                "mode": 0o700,
                "device": 1,
                "inode": 55,
            },
            "marker": None,
            "receipt": None,
            "extra_entries": 0,
        }
        with self.assertRaises(LAYOUT.LayoutError):
            self.plan()
        self.assertEqual(self.fs.writes, [])

    def test_parcial_propio_se_completa_con_un_plan_nuevo(self):
        self.apply()
        path = "/var/lib/media-platform"
        self.fs.targets[path]["receipt"] = None
        original_marker = copy.deepcopy(self.fs.targets[path]["marker"])
        self.fs.writes.clear()
        self.assertEqual(self.plan()["targets"][1]["action"], "finish_receipt")
        self.apply()
        self.assertEqual(self.fs.writes, [("receipt", path)])
        self.assertEqual(self.fs.targets[path]["marker"], original_marker)

    def test_parcial_con_entradas_adicionales_se_rechaza(self):
        self.apply()
        target = self.fs.targets["/var/lib/media-platform"]
        target["receipt"] = None
        target["extra_entries"] = 1
        self.fs.writes.clear()
        with self.assertRaises(LAYOUT.LayoutError):
            self.plan()
        self.assertEqual(self.fs.writes, [])

    def test_directorio_acreditado_preserva_entradas_ajenas_a_esta_unidad(self):
        self.apply()
        self.fs.targets["/opt/media-platform"]["extra_entries"] = 7
        self.fs.writes.clear()
        self.apply()
        self.assertEqual(self.fs.targets["/opt/media-platform"]["extra_entries"], 7)
        self.assertEqual(self.fs.writes, [])

    def test_marcador_movido_de_otro_inode_o_ruta_no_acredita_propiedad(self):
        for field, value in (
            ("owner", "ajeno"),
            ("path", "/opt/otro"),
            ("role", "state"),
            ("directory", {"device": 1, "inode": 999}),
        ):
            with self.subTest(field=field):
                filesystem = MemoryFilesystem()
                plan = LAYOUT.make_plan(filesystem.inspect(), HOST)
                LAYOUT.apply_plan(filesystem, HOST, plan, plan["plan_sha256"])
                filesystem.targets["/opt/media-platform"]["marker"][field] = value
                with self.assertRaises(LAYOUT.LayoutError):
                    LAYOUT.make_plan(filesystem.inspect(), HOST)

    def test_recibo_alterado_impide_reutilizacion(self):
        self.apply()
        self.fs.targets["/opt/media-platform"]["receipt"]["marker_sha256"] = "0" * 64
        self.fs.writes.clear()
        with self.assertRaises(LAYOUT.LayoutError):
            self.plan()
        self.assertEqual(self.fs.writes, [])

    def test_fallo_segundo_destino_deja_primero_acreditado_y_permite_replan(self):
        self.fs.fail_on = "/var/lib/media-platform"
        plan = self.plan()
        with self.assertRaises(OSError):
            self.apply(plan)
        self.assertEqual(
            LAYOUT.target_state("/opt/media-platform", self.fs.targets["/opt/media-platform"]),
            "owned",
        )
        self.assertIsNone(self.fs.targets["/var/lib/media-platform"])
        with self.assertRaises(LAYOUT.LayoutError):
            self.apply(plan)
        self.fs.fail_on = None
        self.apply()

    def test_cambio_entre_preflight_y_primera_mutacion_se_rechaza(self):
        plan = self.plan()
        initial = self.fs.inspect()
        other = MemoryFilesystem()
        other_plan = LAYOUT.make_plan(other.inspect(), HOST)
        LAYOUT.apply_plan(other, HOST, other_plan, other_plan["plan_sha256"])
        with patch.object(self.fs, "inspect", side_effect=[initial, other.inspect()]):
            with self.assertRaises(LAYOUT.LayoutError):
                self.apply(plan)
        self.assertEqual(self.fs.writes, [])

    def test_archivo_plan_temporal_valido_y_limites_sin_rutas_reales(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            plan = self.plan()
            path.write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(LAYOUT.read_plan(path), plan)
            path.write_bytes(b"x" * 65537)
            with self.assertRaises(LAYOUT.LayoutError):
                LAYOUT.read_plan(path)

    def test_archivo_plan_con_hardlink_no_se_admite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(self.plan()), encoding="utf-8")
            alias = Path(directory) / "plan-alias.json"
            os.link(path, alias)
            with self.assertRaises(LAYOUT.LayoutError):
                LAYOUT.read_plan(path)

    def test_cli_sin_apply_solo_inspecciona(self):
        context = MagicMock()
        context.__enter__.return_value = self.fs
        with (
            patch.object(LAYOUT, "inspect_host", return_value=HOST),
            patch.object(LAYOUT, "LocalFilesystem", return_value=context) as filesystem,
            patch("sys.argv", ["host_layout.py"]),
            patch("builtins.print") as output,
        ):
            self.assertEqual(LAYOUT.main(), 0)
        filesystem.assert_called_once_with(apply=False)
        self.assertEqual(self.fs.writes, [])
        self.assertEqual(
            json.loads(output.call_args.args[0])["plan_sha256"], self.plan()["plan_sha256"]
        )


if __name__ == "__main__":
    unittest.main()
