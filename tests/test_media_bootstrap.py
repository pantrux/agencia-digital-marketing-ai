"""Pruebas del arranque de .85 sin instalar paquetes ni contactar infraestructura."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "vcenter/bootstrap-media-vm.sh").as_posix()
BASH = shutil.which("bash") if os.name != "nt" else r"C:\Program Files\Git\bin\bash.exe"
DOCKER_SOURCE = """Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
"""


class ArranqueMedios(unittest.TestCase):
    def ejecutar_guardia(self, hostname="vm-media-01", ip="192.168.0.85/24", user="jandrade"):
        code = """
source "$1"
uname() { printf 'Linux\\n'; }
hostname() { printf '%s\\n' "$BOOTSTRAP_TEST_HOSTNAME"; }
ip() { printf '2: ens192 inet %s brd 192.168.0.255 scope global ens192\\n' "$BOOTSTRAP_TEST_IP"; }
getent() { printf '%s:x:1000:1000:Usuario:/home/jandrade:/bin/bash\\n' "$BOOTSTRAP_TEST_USER"; }
apt-get() { printf 'NO_DEBE_INSTALAR\\n'; return 99; }
validar_destino
"""
        environ = os.environ.copy()
        environ.update(
            BOOTSTRAP_TEST_HOSTNAME=hostname, BOOTSTRAP_TEST_IP=ip, BOOTSTRAP_TEST_USER=user
        )
        return subprocess.run(
            [BASH, "-c", code, "prueba", str(SCRIPT)],
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_rechaza_host_distinto_antes_de_instalar(self):
        result = self.ejecutar_guardia(hostname="Ubuntu-Openclaw")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("NO_DEBE_INSTALAR", result.stdout)

    def test_rechaza_puente_gpu_antes_de_instalar(self):
        result = self.ejecutar_guardia(ip="192.168.0.6/24")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("NO_DEBE_INSTALAR", result.stdout)

    def test_rechaza_usuario_1000_que_no_sea_jandrade(self):
        result = self.ejecutar_guardia(user="usuario-existente")
        self.assertNotEqual(result.returncode, 0)

    def test_admite_destino_exacto_sin_ejecutar_apt(self):
        result = self.ejecutar_guardia()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("NO_DEBE_INSTALAR", result.stdout)

    def test_modo_desconocido_no_ejecuta_instaladores(self):
        result = subprocess.run(
            [BASH, str(SCRIPT), "modo-no-valido"], capture_output=True, text=True, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Modo", result.stderr)

    def test_inicio_exige_recibo_de_respaldo(self):
        code = 'source "$1"; verificar_recibo_respaldo ""'
        result = subprocess.run(
            [BASH, "-c", code, "prueba", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recibo", result.stderr)

    def test_recibo_incompleto_o_autodeclarado_se_rechaza(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "recibo.json"
            receipt.write_text(json.dumps({"vault_persisted": True}), encoding="utf-8")
            code = 'source "$1"; verificar_recibo_respaldo "$2"'
            result = subprocess.run(
                [BASH, "-c", code, "prueba", str(SCRIPT), str(receipt)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

    def ejecutar_seleccion_fuente(self, inherited=None, managed=None):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            expected = directory / "expected"
            expected.write_text(DOCKER_SOURCE, encoding="utf-8", newline="\n")
            for name, value in (
                ("docker.sources", inherited),
                ("media-platform-docker.sources", managed),
            ):
                if value is not None:
                    (directory / name).write_text(value, encoding="utf-8", newline="\n")
            code = 'source "$1"; if seleccion=$(seleccionar_fuente_docker "$2" "$3"); then printf "%s\\n" "$seleccion"; else exit 1; fi'
            result = subprocess.run(
                [BASH, "-c", code, "prueba", SCRIPT, expected.as_posix(), directory.as_posix()],
                capture_output=True,
                text=True,
                check=False,
            )
            return result, directory.as_posix()

    def test_fuente_equivalente_heredada_se_reutiliza(self):
        result, directory = self.ejecutar_seleccion_fuente(DOCKER_SOURCE, DOCKER_SOURCE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), directory + "/docker.sources")

    def test_fuente_nueva_se_administra_si_no_hay_heredada(self):
        result, directory = self.ejecutar_seleccion_fuente()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), directory + "/media-platform-docker.sources")

    def test_fuente_heredada_distinta_se_rechaza(self):
        result, _directory = self.ejecutar_seleccion_fuente(
            DOCKER_SOURCE.replace(
                "Signed-By: /etc/apt/keyrings/docker.asc", "Signed-By: /otra/llave.asc"
            )
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("equivalente", result.stderr)

    def test_fuente_propia_alterada_no_se_archiva(self):
        result, _directory = self.ejecutar_seleccion_fuente(
            DOCKER_SOURCE, DOCKER_SOURCE.replace("Suites: noble", "Suites: jammy")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("equivalente", result.stderr)

    def programa_python_incrustado(self, function_name):
        source = Path(SCRIPT).read_text(encoding="utf-8")
        segment = source.split(function_name + "() {", 1)[1]
        return segment.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]

    def test_recibo_acepta_runner_con_bom_y_preserva_todos_los_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            repository = "https://github.com/pantrux/agencia-digital-marketing-ai"
            raw_files = {
                ".runner": b"\xef\xbb\xbf"
                + json.dumps({"gitHubUrl": repository, "agentName": "vm-media-01"}).encode(),
                ".credentials": b'\xef\xbb\xbf{"scheme":"prueba-sin-validez"}',
                ".credentials_rsaparams": b'\xef\xbb\xbf{"prueba":"identidad-inerte"}',
            }
            for name, raw in raw_files.items():
                (directory / name).write_bytes(raw)
            receipt = directory / "recibo.json"
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_url": repository,
                        "runner_name": "vm-media-01",
                        "vault_path": r"D:\Proyectos\Agencia Digital de Marketing AI\.env",
                        "env_example_synchronized": True,
                        "vault_persisted": True,
                        "created_at_utc": "2020-01-01T00:00:00Z",
                        "files": {
                            name: hashlib.sha256(raw).hexdigest() for name, raw in raw_files.items()
                        },
                    }
                ),
                encoding="utf-8",
            )
            original_stat = Path.stat

            def linux_metadata(path, *args, **kwargs):
                metadata = original_stat(path, *args, **kwargs)
                if path.parent == directory and path.name in {*raw_files, receipt.name}:
                    values = list(metadata)
                    values[0] = stat.S_IFMT(metadata.st_mode) | 0o600
                    values[4] = 0 if path == receipt else 1000
                    return os.stat_result(values)
                return metadata

            code = self.programa_python_incrustado("verificar_recibo_respaldo")
            with (
                patch.object(Path, "stat", linux_metadata),
                patch.object(
                    sys,
                    "argv",
                    ["verificador", str(receipt), str(directory), repository, "vm-media-01"],
                ),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                exec(compile(code, "verificador-del-bootstrap", "exec"), {})
            for name, raw in raw_files.items():
                self.assertEqual((directory / name).read_bytes(), raw)

    def test_registro_existente_acepta_bom_sin_reescribir_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Path(tmp) / ".runner"
            repository = "https://github.com/pantrux/agencia-digital-marketing-ai"
            raw = (
                b"\xef\xbb\xbf"
                + json.dumps({"gitHubUrl": repository, "agentName": "vm-media-01"}).encode()
            )
            runner.write_bytes(raw)
            code = self.programa_python_incrustado("configurar_runner")
            with (
                patch.object(sys, "argv", ["verificador", str(runner), repository, "vm-media-01"]),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                exec(compile(code, "registro-existente-del-bootstrap", "exec"), {})
            self.assertEqual(runner.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
