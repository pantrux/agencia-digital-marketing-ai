"""Valida propiedad y carga declarativa sin conectar a vCenter ni generar llaves reales."""

import json
import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "vcenter/Test-MediaCloudInit.ps1"
FIXTURE = """
$configuration=[pscustomobject]@{
 Name='vm-media-01'; IpAddress='192.168.0.85'; PrefixLength=24;
 Gateway='192.168.0.1'; DnsServers=@('192.168.0.1'); SshUser='jandrade';
 SshPublicKey='ssh-ed25519 AAAA prueba-sin-validez';
 RegisteredId='VirtualMachine-vm-123'; RegisteredUuid='11111111-1111-4111-8111-111111111111';
 OwnerMarker='agencia-digital-marketing-ai:vm-media-01:v4.1'
}
$machine=[pscustomobject]@{
 Name='vm-media-01'; Id='VirtualMachine-vm-123'; Notes=$configuration.OwnerMarker;
 PowerState='PoweredOff'; VMHost=[pscustomobject]@{Id='HostSystem-host-123'};
 ExtensionData=[pscustomobject]@{
  Config=[pscustomobject]@{InstanceUuid=$configuration.RegisteredUuid;Template=$false;ExtraConfig=@()};
  Guest=[pscustomobject]@{HostName='vm-media-01';ToolsRunningStatus='guestToolsRunning'};
  Runtime=[pscustomobject]@{BootTime=[datetime]::UtcNow}
 }
}
"""


class PruebaCloudInitAislada(unittest.TestCase):
    def ejecutar(self, code):
        return subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; . $env:MEDIA_TEST_CLOUDINIT; " + FIXTURE + code,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ | {"MEDIA_TEST_CLOUDINIT": str(SCRIPT)},
            timeout=20,
        )

    def test_importar_helper_no_contacta_infraestructura(self):
        result = self.ejecutar("Write-Output 'solo-funciones'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "solo-funciones")

    def test_importar_helper_conserva_boveda_del_aprovisionador(self):
        result = subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; $VaultPath='boveda-alternativa'; . $env:MEDIA_TEST_CLOUDINIT; if ($VaultPath -cne 'boveda-alternativa') {throw 'La importación sustituyó la bóveda del llamador'}",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ | {"MEDIA_TEST_CLOUDINIT": str(SCRIPT)},
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rechaza_vm_sin_propiedad(self):
        result = self.ejecutar(
            "$machine.Notes='otro-equipo'; Test-MediaCloudInitTarget -Machine $machine -Configuration $configuration -TargetHostId 'HostSystem-host-123' -RequirePoweredOff"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_rechaza_uuid_no_respaldado(self):
        result = self.ejecutar(
            "$machine.ExtensionData.Config.InstanceUuid='22222222-2222-4222-8222-222222222222'; Test-MediaCloudInitTarget -Machine $machine -Configuration $configuration -TargetHostId 'HostSystem-host-123' -RequirePoweredOff"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_rechaza_vm_ya_encendida(self):
        result = self.ejecutar(
            "$machine.PowerState='PoweredOn'; Test-MediaCloudInitTarget -Machine $machine -Configuration $configuration -TargetHostId 'HostSystem-host-123' -RequirePoweredOff"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_admite_solo_clone_propio_y_apagado(self):
        result = self.ejecutar(
            "Test-MediaCloudInitTarget -Machine $machine -Configuration $configuration -TargetHostId 'HostSystem-host-123' -RequirePoweredOff"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_payload_offline_conserva_llaves_y_no_autogenera(self):
        result = self.ejecutar("""
$identity=[pscustomobject]@{PrivateKey="-----BEGIN OPENSSH PRIVATE KEY-----`nPRUEBA-SIN-VALIDEZ`n-----END OPENSSH PRIVATE KEY-----`n";PublicKey='ssh-ed25519 AAAA host-sin-validez';Fingerprint='SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'}
$payload=New-MediaCloudInitPayload -Configuration $configuration -HostIdentity $identity -MacAddress '00:50:56:11:22:33'
[pscustomobject]@{Metadata=($payload.MetadataJson|ConvertFrom-Json);UserData=$payload.UserData;GuestInfoCount=$payload.GuestInfo.Count}|ConvertTo-Json -Depth 12 -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["Metadata"]["instance-id"], "11111111-1111-4111-8111-111111111111")
        self.assertFalse(payload["Metadata"]["wait-on-network"]["ipv4"])
        self.assertTrue(payload["Metadata"]["network"]["ethernets"]["media0"]["optional"])
        self.assertIn("ssh_genkeytypes: [ed25519]", payload["UserData"])
        self.assertIn("ed25519_private:", payload["UserData"])
        self.assertIn("guestinfo.media-cloudinit.instance-id", payload["UserData"])
        self.assertIn("write_files:", payload["UserData"])
        self.assertIn(
            "/var/lib/cloud/scripts/per-boot/90-media-platform-proof", payload["UserData"]
        )
        self.assertIn("owner: root:root", payload["UserData"])
        self.assertIn("permissions: '0700'", payload["UserData"])
        self.assertNotIn("runcmd:", payload["UserData"])
        boot_script = textwrap.dedent(payload["UserData"].split("    content: |\n", 1)[1])
        bash = shutil.which("bash") if os.name != "nt" else r"C:\Program Files\Git\bin\bash.exe"
        syntax = subprocess.run(
            [bash, "-n"], input=boot_script, capture_output=True, text=True, check=False
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertEqual(payload["GuestInfoCount"], 4)

    def test_hostname_heredado_sin_pruebas_no_acredita_cloudinit(self):
        result = self.ejecutar(
            "Test-MediaCloudInitProof -Machine $machine -Configuration $configuration -ExpectedFingerprint 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' -StartedAtUtc ([datetime]::UtcNow.AddSeconds(-2)) | ConvertTo-Json -Compress"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout))

    def test_payload_lan_exige_interfaz_disponible(self):
        result = self.ejecutar("""
$identity=[pscustomobject]@{PrivateKey='identidad-sintetica-sin-validez';PublicKey='ssh-ed25519 AAAA host-sin-validez';Fingerprint='SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'}
$payload=New-MediaCloudInitPayload -Configuration $configuration -HostIdentity $identity -MacAddress '00:50:56:11:22:33' -NetworkOptional:$false
$payload.MetadataJson
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["network"]["ethernets"]["media0"]["optional"])

    def comprobar_interfaz(self, power_state):
        return self.ejecutar(
            """
function Set-NetworkAdapter {
 [CmdletBinding(SupportsShouldProcess)]
 param($Server,$NetworkAdapter,[bool]$StartConnected,[bool]$Connected)
 $script:LastNicCall=@{Keys=@($PSBoundParameters.Keys);StartConnected=$StartConnected;Connected=$Connected}
}
"""
            + f"$machine.PowerState='{power_state}'; "
            + """
Set-MediaCloudInitNicDisconnected -Connection 'sin-infraestructura' -Machine $machine -NetworkAdapter ([pscustomobject]@{Name='interfaz-de-prueba'}) -Confirm:$false
$script:LastNicCall | ConvertTo-Json -Compress
"""
        )

    def test_interfaz_apagada_no_envia_connected_a_powercli(self):
        result = self.comprobar_interfaz("PoweredOff")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)
        self.assertNotIn("Connected", call["Keys"])
        self.assertFalse(call["StartConnected"])

    def test_interfaz_encendida_desconecta_estado_actual_y_arranque(self):
        result = self.comprobar_interfaz("PoweredOn")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)
        self.assertIn("Connected", call["Keys"])
        self.assertFalse(call["Connected"])
        self.assertFalse(call["StartConnected"])

    def test_prueba_exige_kubelet_inactivo_o_ausente(self):
        result = self.ejecutar("""
$machine.ExtensionData.Config.ExtraConfig=@(
 [pscustomobject]@{Key='guestinfo.media-cloudinit.instance-id';Value=$configuration.RegisteredUuid},
 [pscustomobject]@{Key='guestinfo.media-cloudinit.host-fingerprint';Value='SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'}
)
$before=Test-MediaCloudInitProof -Machine $machine -Configuration $configuration -ExpectedFingerprint 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' -StartedAtUtc ([datetime]::UtcNow.AddSeconds(-2))
$machine.ExtensionData.Config.ExtraConfig += [pscustomobject]@{Key='guestinfo.media-cloudinit.kubelet-disabled';Value='true'}
$after=Test-MediaCloudInitProof -Machine $machine -Configuration $configuration -ExpectedFingerprint 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' -StartedAtUtc ([datetime]::UtcNow.AddSeconds(-2))
[pscustomobject]@{SinConstancia=$before;ConConstancia=$after} | ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        proof = json.loads(result.stdout)
        self.assertFalse(proof["SinConstancia"])
        self.assertTrue(proof["ConConstancia"])


if __name__ == "__main__":
    unittest.main()
