"""Prueba las guardas de instantáneas con objetos locales y sin cargar PowerCLI."""

import json
import os
import subprocess
import unittest
from pathlib import Path

SNAPSHOT_SCRIPT = Path(__file__).parents[1] / "vcenter/New-MediaBaselineSnapshot.ps1"


class VcenterSnapshotTests(unittest.TestCase):
    def run_plan(self, action):
        fixture = """
$ErrorActionPreference='Stop'
. $env:MEDIA_TEST_SNAPSHOT -Name '00-pre-bootstrap'
$configuration=[pscustomobject]@{
    Name='vm-media-01'; IpAddress='192.168.0.85'; HostAddress='192.168.0.3'
    RegisteredId='VirtualMachine-vm-prueba'; RegisteredUuid='11111111-2222-3333-4444-555555555555'
    OwnerMarker='agencia-digital-marketing-ai:vm-media-01:v4.1'
}
$machine=[pscustomobject]@{
    Name='vm-media-01'; Id=$configuration.RegisteredId; Notes=$configuration.OwnerMarker; PowerState='PoweredOff'
    VMHost=[pscustomobject]@{Id='HostSystem-host-prueba'}
    ExtensionData=[pscustomobject]@{Config=[pscustomobject]@{InstanceUuid=$configuration.RegisteredUuid;Template=$false}}
}
"""
        return subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", fixture + action],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ | {"MEDIA_TEST_SNAPSHOT": str(SNAPSHOT_SCRIPT)},
            timeout=20,
        )

    def run_prechange_plan(self, names, setup=""):
        names_ps = ",".join(f"'{name}'" for name in names)
        result = self.run_plan(
            f"$snapshots=@(@({names_ps}) | ForEach-Object {{[pscustomobject]@{{Name=$_;"
            "Id=('Snapshot-'+$_); Created=[datetime]'2026-11-05T19:00:00Z'; "
            "Description=($configuration.OwnerMarker+'|instantanea:'+$_)}}); "
            + setup
            + "Get-MediaPreChangeSnapshotPlan -Machine $machine -Configuration $configuration "
            "-TargetHostId 'HostSystem-host-prueba' -Snapshots $snapshots -Name 'pre-cambio-docker-30' "
            "| ConvertTo-Json -Compress"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_prechange_crea_con_espacio_para_una_instantanea(self):
        plan = self.run_prechange_plan(["01-vm-limpia"])
        self.assertEqual(plan["Action"], "Crear")
        self.assertIsNone(plan["RetireId"])

    def test_prechange_conserva_las_dos_instantaneas_base(self):
        plan = self.run_prechange_plan(["00-pre-bootstrap", "01-vm-limpia"])
        self.assertEqual(plan["Action"], "Bloqueado")
        self.assertEqual(plan["Reason"], "CAPACIDAD_OCUPADA_POR_REFERENCIAS_INICIALES")

    def test_prechange_solo_rota_la_instantanea_de_un_cambio_anterior(self):
        plan = self.run_prechange_plan(["01-vm-limpia", "pre-cambio-docker-29"])
        self.assertEqual(plan["Action"], "Crear")
        self.assertEqual(plan["RetireId"], "Snapshot-pre-cambio-docker-29")

    def test_prechange_no_planifica_mutaciones_con_vm_encendida(self):
        plan = self.run_prechange_plan(
            ["01-vm-limpia", "pre-cambio-docker-29"], "$machine.PowerState='PoweredOn'; "
        )
        self.assertEqual(plan["Reason"], "VM_DEBE_ESTAR_APAGADA")
        self.assertIsNone(plan["RetireId"])

    def test_prechange_detiene_mantenimiento_con_consolidacion_pendiente(self):
        plan = self.run_prechange_plan(
            ["01-vm-limpia"],
            "$machine.ExtensionData | Add-Member Runtime ([pscustomobject]@{ConsolidationNeeded=$true}); ",
        )
        self.assertEqual(plan["Reason"], "CONSOLIDACION_PENDIENTE")

    def test_rechaza_vm_ajena(self):
        result = self.run_plan(
            "$machine.Notes='otro-equipo'; "
            "Get-MediaBaselineSnapshotPlan -Machine $machine -Configuration $configuration "
            "-TargetHostId 'HostSystem-host-prueba' -Snapshots @() -Name '00-pre-bootstrap'"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("propiedad", result.stderr)

    def test_rechaza_tercera_instantanea(self):
        result = self.run_plan(
            "$snapshots=@(1..2 | ForEach-Object {[pscustomobject]@{Name='00-pre-bootstrap';"
            "Id=('Snapshot-prueba-'+$_);Description=($configuration.OwnerMarker+'|instantanea:00-pre-bootstrap')}}); "
            "Get-MediaBaselineSnapshotPlan -Machine $machine -Configuration $configuration "
            "-TargetHostId 'HostSystem-host-prueba' -Snapshots $snapshots -Name '01-vm-limpia'"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dos", result.stderr)

    def test_reutiliza_solo_instantanea_propia(self):
        result = self.run_plan(
            "$snapshot=[pscustomobject]@{Name='00-pre-bootstrap';Id='Snapshot-prueba';Created=[datetime]'2026-11-05T19:00:00Z';"
            "Description=($configuration.OwnerMarker+'|instantanea:00-pre-bootstrap')}; "
            "Get-MediaBaselineSnapshotPlan -Machine $machine -Configuration $configuration "
            "-TargetHostId 'HostSystem-host-prueba' -Snapshots @($snapshot) -Name '00-pre-bootstrap' | ConvertTo-Json -Compress"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["Action"], "Reutilizar")
        self.assertEqual(plan["CreatedAtUtc"], "2026-11-05T19:00:00.0000000Z")

    def test_rechaza_instantanea_sin_marcador(self):
        result = self.run_plan(
            "$snapshot=[pscustomobject]@{Name='00-pre-bootstrap';Id='Snapshot-prueba';Description=''}; "
            "Get-MediaBaselineSnapshotPlan -Machine $machine -Configuration $configuration "
            "-TargetHostId 'HostSystem-host-prueba' -Snapshots @($snapshot) -Name '00-pre-bootstrap'"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("propiedad", result.stderr)

    def test_reutilizar_no_exige_apagar_ni_crear(self):
        result = self.run_plan(
            "$machine.PowerState='PoweredOn'; "
            "$snapshot=[pscustomobject]@{Name='00-pre-bootstrap';Id='Snapshot-prueba';Created=[datetime]'2026-11-05T19:00:00Z';"
            "Description=($configuration.OwnerMarker+'|instantanea:00-pre-bootstrap')}; "
            "Get-MediaBaselineSnapshotPlan -Machine $machine -Configuration $configuration "
            "-TargetHostId 'HostSystem-host-prueba' -Snapshots @($snapshot) -Name '00-pre-bootstrap' | ConvertTo-Json -Compress"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["Action"], "Reutilizar")

    def test_creacion_exige_vm_apagada(self):
        result = self.run_plan(
            "$machine.PowerState='PoweredOn'; "
            "Get-MediaBaselineSnapshotPlan -Machine $machine -Configuration $configuration "
            "-TargetHostId 'HostSystem-host-prueba' -Snapshots @() -Name '00-pre-bootstrap'"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("apagada", result.stderr)

    def test_plan_valido_no_crea_instantaneas(self):
        result = self.run_plan(
            "Get-MediaBaselineSnapshotPlan -Machine $machine -Configuration $configuration "
            "-TargetHostId 'HostSystem-host-prueba' -Snapshots @() -Name '00-pre-bootstrap' | ConvertTo-Json -Compress"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["Action"], "Crear")
        self.assertFalse(plan["Memory"])
        self.assertFalse(plan["Quiesce"])


if __name__ == "__main__":
    unittest.main()
