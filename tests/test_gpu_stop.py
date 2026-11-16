"""Parada por handle: predicados y transporte simulados, sin detener procesos reales."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "gpu-bridge-win11/Stop-GpuBridge.ps1"
MOCKS = r"""
$script:opened=0;$script:closed=0;$script:terminated=0;$script:forStop=$false;$script:waitResult=0
$script:birth=[datetime]::Parse('2026-11-05T20:00:00.1234560Z').ToUniversalTime()
$script:context=@{HostName='W11-HOMELAB';CurrentUserSid='S-1-5-21-1000';UserSid='S-1-5-21-1000';InstallRoot='C:\GPU-Bridge';InstallationOwner='fixture-owner';RuntimeConfigPath='C:\GPU-Bridge\runtime.json';ReceiptPath='C:\GPU-Bridge\supervisor-start.json';PythonPath='C:\GPU-Bridge\metrics\venv\Scripts\python.exe';ImagePath='C:\Tools\Python312\python.exe';LauncherPath='C:\GPU-Bridge\services\launch_service.py'}
$script:receipt=@{SchemaVersion=1;Role='supervisor';Valid=$true;Started=$true;Readiness='pending';ProcessId=4242;ParentProcessId=4000;HostName='W11-HOMELAB';UserSid=$script:context.UserSid;InstallRoot=$script:context.InstallRoot;InstallationOwner=$script:context.InstallationOwner;RuntimeConfigPath=$script:context.RuntimeConfigPath;ProcessImagePath=$script:context.ImagePath;LauncherPath=$script:context.LauncherPath;CreationFileTimeUtc=[string]$script:birth.ToFileTimeUtc();StartTimeUtc=$script:birth.ToString('o')}
$script:native=@{ProcessId=4242;CreationFileTimeUtc=[string]$script:birth.ToFileTimeUtc();ProcessImagePath=$script:context.ImagePath;Alive=$true}
$script:cim=@{ProcessId=4242;ParentProcessId=4000;CreationTimeUtc=$script:birth;UserSid=$script:context.UserSid;ExecutablePath=$script:context.ImagePath;CommandLine='PRIVADO_NO_PUBLICAR'}
$script:argv=@($script:context.ImagePath,'-I',$script:context.LauncherPath,'supervisor')
$script:parentArgv=@($script:context.PythonPath,'-I',$script:context.LauncherPath,'supervisor')
$script:context.SourceBundleSha256='a'*64;$script:receipt.SourceBundleSha256='a'*64
$script:parentCim=@{ProcessId=4000;ParentProcessId=3000;CreationTimeUtc=$script:birth.AddSeconds(-1);UserSid=$script:context.UserSid;ExecutablePath=$script:context.PythonPath;CommandLine='PRIVADO_PADRE_NO_PUBLICAR'}
$script:parentNative=@{ProcessId=4000;CreationFileTimeUtc=[string]$script:birth.AddSeconds(-1).ToFileTimeUtc();ProcessImagePath=$script:context.PythonPath;Alive=$true}
function Get-AgencyGpuStopContext {return $script:context}
function Read-AgencyGpuStartReceipt {param($Context) return $script:receipt}
function Open-AgencyGpuProcessHandle {param([int]$ProcessId,[switch]$ForStop) $script:opened++;$script:forStop=[bool]$ForStop;return [pscustomobject]@{Owned=$true;ProcessId=$ProcessId}}
function Get-AgencyGpuNativeProcess {param($Handle) if($Handle.ProcessId -eq 4000){return $script:parentNative};return $script:native}
function Get-AgencyGpuCimProcess {param([int]$ProcessId) if($ProcessId -eq 4000){return $script:parentCim};return $script:cim}
function ConvertFrom-AgencyGpuCommandLine {param([string]$CommandLine) if($CommandLine -eq 'PRIVADO_PADRE_NO_PUBLICAR'){return ,$script:parentArgv};return ,$script:argv}
function Stop-AgencyGpuHandle {param($Handle) $script:terminated++}
function Wait-AgencyGpuHandle {param($Handle,[int]$TimeoutMs) return $script:waitResult}
function Close-AgencyGpuProcessHandle {param($Handle) $script:closed++}
"""


class ParadaGpu(unittest.TestCase):
    def test_argv_real_del_hijo_usa_python_base_y_no_redirector(self):
        result = self.ejecutar(
            "$script:argv[0]=$script:context.ImagePath;$r=Invoke-AgencyGpuStop;[pscustomobject]@{Verified=$r.IdentityVerified;Terminated=$script:terminated}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Verified": True, "Terminated": 0})

    def ejecutar(self, code, *, extra_env=None):
        result = subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; . $env:GPU_STOP_TEST_SCRIPT; " + MOCKS + code,
            ],
            env=os.environ | {"GPU_STOP_TEST_SCRIPT": str(SCRIPT)} | (extra_env or {}),
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PRIVADO_NO_PUBLICAR", result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_plan_predeterminado_no_termina_y_cierra_handle_retenido(self):
        result = self.ejecutar(
            "$r=Invoke-AgencyGpuStop; [pscustomobject]@{Mode=$r.Mode;Verified=$r.IdentityVerified;Stopped=$r.Stopped;Terminated=$script:terminated;Opened=$script:opened;Closed=$script:closed;ForStop=$script:forStop}|ConvertTo-Json -Compress"
        )
        self.assertEqual(
            result,
            {
                "Mode": "Plan",
                "Verified": True,
                "Stopped": False,
                "Terminated": 0,
                "Opened": 1,
                "Closed": 1,
                "ForStop": False,
            },
        )

    def test_stop_explicito_usa_solo_handle_validado_y_espera_acotada(self):
        result = self.ejecutar(
            "$r=Invoke-AgencyGpuStop -Stop; [pscustomobject]@{Mode=$r.Mode;Stopped=$r.Stopped;Terminated=$script:terminated;Closed=$script:closed;ForStop=$script:forStop}|ConvertTo-Json -Compress"
        )
        self.assertEqual(
            result, {"Mode": "Stop", "Stopped": True, "Terminated": 1, "Closed": 1, "ForStop": True}
        )

    def rejection(self, mutation):
        return self.ejecutar(
            mutation
            + "; $rejected=$false; try {Invoke-AgencyGpuStop -Stop|Out-Null} catch {$rejected=$true}; [pscustomobject]@{Rejected=$rejected;Terminated=$script:terminated;Closed=$script:closed}|ConvertTo-Json -Compress"
        )

    def test_host_sid_raiz_marcador_y_launcher_ajenos_rechazan_antes_de_terminar(self):
        for key, value in (
            ("HostName", "OTRO"),
            ("UserSid", "S-1-5-21-2000"),
            ("InstallRoot", "C:\\Otro"),
            ("InstallationOwner", "ajeno"),
            ("LauncherPath", "C:\\Otro\\launch_service.py"),
        ):
            with self.subTest(key=key):
                result = self.rejection(f"$script:receipt['{key}']='{value}'")
                self.assertEqual(result, {"Rejected": True, "Terminated": 0, "Closed": 1})

    def test_cambio_de_usuario_actual_no_adopta_recibo_de_otro_contexto(self):
        self.assertEqual(
            self.rejection("$script:context.CurrentUserSid='S-1-5-21-2000'"),
            {"Rejected": True, "Terminated": 0, "Closed": 1},
        )

    def test_pid_reciclado_tiempos_o_imagen_distintos_rechazan(self):
        for mutation in (
            "$script:native.ProcessId=4243",
            "$script:native.Alive=$false",
            "$script:native.CreationFileTimeUtc=[string]($script:birth.ToFileTimeUtc()+10)",
            "$script:cim.CreationTimeUtc=$script:birth.AddSeconds(1)",
            "$script:cim.UserSid='S-1-5-21-2000'",
            "$script:native.ProcessImagePath='C:\\Otro\\python.exe'",
            "$script:cim.ExecutablePath='C:\\Otro\\python.exe'",
        ):
            with self.subTest(mutation=mutation):
                self.assertEqual(
                    self.rejection(mutation), {"Rejected": True, "Terminated": 0, "Closed": 1}
                )

    def test_precision_cim_inferior_a_un_microsegundo_se_admite(self):
        result = self.ejecutar(
            "$script:cim.CreationTimeUtc=$script:birth.AddTicks(-9); $r=Invoke-AgencyGpuStop; [pscustomobject]@{Verified=$r.IdentityVerified;Terminated=$script:terminated}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Verified": True, "Terminated": 0})

    def test_recibo_json_real_con_siete_fracciones_preserva_utc_en_distintas_culturas(self):
        with tempfile.TemporaryDirectory(
            prefix="gpu-stop-fecha-", dir=SCRIPT.parent.parent
        ) as folder:
            receipt_path = Path(folder) / "recibo.json"
            result = self.ejecutar(
                r"""
$stamp='2026-11-05T20:00:00.1234567Z'
$script:birth=[datetime]::ParseExact($stamp,'o',[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::RoundtripKind)
$script:receipt.StartTimeUtc=$stamp
$script:receipt.CreationFileTimeUtc=[string]$script:birth.ToFileTimeUtc()
$script:native.CreationFileTimeUtc=$script:receipt.CreationFileTimeUtc
$script:cim.CreationTimeUtc=$script:birth.AddTicks(-5)
$script:receipt|ConvertTo-Json|Set-Content -LiteralPath $env:GPU_STOP_TEST_RECEIPT -Encoding utf8
$checks=@(foreach($culture in @('es-CL','en-US','fr-FR')) {
    [Globalization.CultureInfo]::CurrentCulture=[Globalization.CultureInfo]::GetCultureInfo($culture)
    [Globalization.CultureInfo]::CurrentUICulture=[Globalization.CultureInfo]::GetCultureInfo($culture)
    $read=Read-AgencyGpuPrivateJson -LiteralPath $env:GPU_STOP_TEST_RECEIPT
    $verified=Test-AgencyGpuProcessIdentity -Context $script:context -Receipt $read -Native $script:native -Cim $script:cim -Arguments $script:argv
    [pscustomobject]@{Culture=$culture;Verified=$verified;DateType=$read.StartTimeUtc.GetType().FullName;FileTime=$read.CreationFileTimeUtc}
})
[pscustomobject]@{Checks=$checks;Terminated=$script:terminated;Opened=$script:opened}|ConvertTo-Json -Depth 4 -Compress
""",
                extra_env={"GPU_STOP_TEST_RECEIPT": str(receipt_path)},
            )
            self.assertIn("2026-11-05T20:00:00.1234567Z", receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(result["Terminated"], 0)
        self.assertEqual(result["Opened"], 0)
        self.assertEqual(
            [check["Culture"] for check in result["Checks"]], ["es-CL", "en-US", "fr-FR"]
        )
        self.assertTrue(all(check["Verified"] for check in result["Checks"]))
        self.assertEqual(len({check["FileTime"] for check in result["Checks"]}), 1)

    def test_datetimeoffset_tipado_conserva_instante_y_diez_ticks_de_tolerancia(self):
        result = self.ejecutar(
            r"""
[Globalization.CultureInfo]::CurrentCulture=[Globalization.CultureInfo]::GetCultureInfo('es-CL')
$script:receipt.StartTimeUtc=[datetimeoffset]::new($script:birth).ToOffset([timespan]::FromHours(-3)).AddTicks(10)
$r=Invoke-AgencyGpuStop
[pscustomobject]@{Verified=$r.IdentityVerified;Terminated=$script:terminated}|ConvertTo-Json -Compress
"""
        )
        self.assertEqual(result, {"Verified": True, "Terminated": 0})

    def test_fecha_tipada_que_excede_diez_ticks_se_rechaza(self):
        for value in (
            "$script:birth.AddTicks(11)",
            "[datetimeoffset]::new($script:birth).ToOffset([timespan]::FromHours(-3)).AddTicks(-11)",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    self.rejection(f"$script:receipt.StartTimeUtc={value}"),
                    {"Rejected": True, "Terminated": 0, "Closed": 1},
                )

    def test_argv_ajeno_adicional_o_check_no_puede_terminarse(self):
        for mutation in (
            "$script:argv+=@('--check')",
            "$script:argv[2]='C:\\Otro\\launch_service.py'",
            "$script:argv[3]='immich'",
            "$script:argv[0]='C:\\Otro\\python.exe'",
        ):
            with self.subTest(mutation=mutation):
                self.assertEqual(
                    self.rejection(mutation), {"Rejected": True, "Terminated": 0, "Closed": 1}
                )

    def test_recibo_minimo_antiguo_no_basta_para_adoptar_pid(self):
        self.assertEqual(
            self.rejection(
                "$script:receipt=@{Valid=$true;Started=$true;ProcessId=4242;Readiness='pending'}"
            ),
            {"Rejected": True, "Terminated": 0, "Closed": 1},
        )

    def test_whatif_no_solicita_derecho_de_terminacion_ni_termina(self):
        result = self.ejecutar(
            "$r=Invoke-AgencyGpuStop -Stop -WhatIf; [pscustomobject]@{Stopped=$r.Stopped;Terminated=$script:terminated;ForStop=$script:forStop;Closed=$script:closed}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Stopped": False, "Terminated": 0, "ForStop": False, "Closed": 1})

    def test_timeout_no_reintenta_por_pid_y_cierra_handle(self):
        result = self.ejecutar(
            "$script:waitResult=258; $rejected=$false; try {Invoke-AgencyGpuStop -Stop -TimeoutMs 10|Out-Null} catch {$rejected=$true}; [pscustomobject]@{Rejected=$rejected;Terminated=$script:terminated;Closed=$script:closed}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Rejected": True, "Terminated": 1, "Closed": 1})

    def test_adopcion_manual_valida_hijo_real_y_cierra_ambos_handles_sin_terminar(self):
        result = self.ejecutar(
            "$r=Get-AgencyGpuVerifiedStartReceipt -ProcessId 4242 -ExpectedLauncherProcessId 4000; [pscustomobject]@{Pid=$r.ProcessId;HasArgv=$r.ContainsKey('CommandLine');Closed=$script:closed;Terminated=$script:terminated}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Pid": 4242, "HasArgv": False, "Closed": 2, "Terminated": 0})

    def test_adopcion_no_acepta_padre_ajeno(self):
        result = self.ejecutar(
            "$script:parentCim.UserSid='S-1-5-21-2000';$rejected=$false;try{Get-AgencyGpuVerifiedStartReceipt -ProcessId 4242 -ExpectedLauncherProcessId 4000|Out-Null}catch{$rejected=$true};[pscustomobject]@{Rejected=$rejected;Closed=$script:closed;Terminated=$script:terminated}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Rejected": True, "Closed": 2, "Terminated": 0})

    def test_arranque_espera_recibo_nuevo_descendiente_y_no_afirma_salud(self):
        result = self.ejecutar(
            "$r=Wait-AgencyGpuStartReceipt -Context $script:context -LauncherProcessId 4000 -LauncherCreationFileTimeUtc $script:parentNative.CreationFileTimeUtc -TimeoutMs 1000;[pscustomobject]@{Pid=$r.ProcessId;Readiness=$r.Readiness;Closed=$script:closed;Terminated=$script:terminated}|ConvertTo-Json -Compress"
        )
        self.assertEqual(
            result, {"Pid": 4242, "Readiness": "pending", "Closed": 1, "Terminated": 0}
        )

    def test_arranque_rechaza_recibo_anterior_al_lanzador_actual(self):
        result = self.ejecutar(
            "$rejected=$false;try{Wait-AgencyGpuStartReceipt -Context $script:context -LauncherProcessId 4000 -LauncherCreationFileTimeUtc ([string]$script:birth.AddSeconds(1).ToFileTimeUtc()) -TimeoutMs 1|Out-Null}catch{$rejected=$true};[pscustomobject]@{Rejected=$rejected;Opened=$script:opened;Closed=$script:closed;Terminated=$script:terminated}|ConvertTo-Json -Compress"
        )
        self.assertTrue(result["Rejected"])
        self.assertEqual(result["Terminated"], 0)
        self.assertEqual(result["Opened"], result["Closed"])

    def test_interop_compila_y_terminate_recibe_safeprocesshandle_sin_usarlo(self):
        result = self.ejecutar(
            "Initialize-AgencyGpuProcessNative;$t=[AgencyGpuStop.Native].GetMethod('TerminateProcess').GetParameters()[0].ParameterType.FullName;[pscustomobject]@{Type=$t;Terminated=$script:terminated}|ConvertTo-Json -Compress"
        )
        self.assertEqual(
            result, {"Type": "Microsoft.Win32.SafeHandles.SafeProcessHandle", "Terminated": 0}
        )


class ReciboSupervisor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(SCRIPT.parent) not in sys.path:
            sys.path.insert(0, str(SCRIPT.parent))

    def test_recibo_real_preserva_filetime_y_no_serializa_argv(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import launch_service

        mapping = {
            "GPU_INSTALL_ROOT": r"C:\GPU-Bridge",
            "GPU_RUNTIME_CONFIG_PATH": r"C:\GPU-Bridge\runtime.json",
            "GPU_START_RECEIPT_PATH": r"C:\GPU-Bridge\supervisor-start.json",
            "GPU_BASE_PYTHON_PATH": r"C:\Tools\python.exe",
            "GPU_WINDOWS_USER_SID": "S-1-5-21-1000",
            "GPU_SERVICES_BUNDLE_SHA256": "a" * 64,
            "GPU_INSTALL_OWNER_MARKER": "fixture",
        }
        native = {
            "ProcessId": 4242,
            "ParentProcessId": 4000,
            "ProcessImagePath": mapping["GPU_BASE_PYTHON_PATH"],
            "UserSid": mapping["GPU_WINDOWS_USER_SID"],
            "HostName": "W11-HOMELAB",
            "CreationFileTimeUtc": "134068464001234567",
            "CommandLine": "NO_SERIALIZAR",
        }
        receipt = launch_service.make_start_receipt(mapping, native)
        self.assertEqual(receipt["CreationFileTimeUtc"], "134068464001234567")
        self.assertEqual(receipt["ProcessId"], 4242)
        self.assertNotIn("NO_SERIALIZAR", json.dumps(receipt))
        for key in ("HostName", "UserSid", "ProcessImagePath"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                launch_service.make_start_receipt(mapping, native | {key: "ajeno"})

    def test_reserva_exclusiva_precede_recibo_y_uvicorn_usa_el_mismo_socket(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import launch_service

        events = []
        listener = object()

        @contextmanager
        def reserve():
            events.append("reservado")
            try:
                yield listener
            finally:
                events.append("cerrado")

        class Server:
            def __init__(self, config):
                self.config = config

            def run(self, *, sockets):
                self.sockets = sockets
                if sockets != [listener]:
                    raise AssertionError("El servidor intentó otro bind")
                events.append("servidor")

        fake_uvicorn = SimpleNamespace(Config=lambda app, **options: options, Server=Server)
        module = SimpleNamespace(create_app=lambda config: events.append("app") or object())
        with (
            patch.object(launch_service, "reserve_supervisor_socket", reserve),
            patch.object(
                launch_service,
                "publish_start_receipt",
                side_effect=lambda config: events.append("recibo"),
            ),
            patch.object(launch_service.importlib, "import_module", return_value=module),
            patch.dict(sys.modules, {"uvicorn": fake_uvicorn}),
        ):
            launch_service.serve("supervisor", {"GPU_BRIDGE_BIND_ADDRESS": "0.0.0.0"})
        self.assertEqual(events, ["reservado", "recibo", "app", "servidor", "cerrado"])

    def test_puerto_ocupado_no_sobrescribe_recibo_existente(self):
        import launch_service

        @contextmanager
        def occupied():
            raise OSError("Puerto ocupado controlado")
            yield

        with (
            patch.object(launch_service, "reserve_supervisor_socket", occupied),
            patch.object(launch_service, "publish_start_receipt") as publish,
            patch.object(launch_service.importlib, "import_module"),
            patch.dict(sys.modules, {"uvicorn": SimpleNamespace()}),
        ):
            with self.assertRaises(OSError):
                launch_service.serve("supervisor", {"GPU_BRIDGE_BIND_ADDRESS": "0.0.0.0"})
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
