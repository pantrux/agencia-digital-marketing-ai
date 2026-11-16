"""Datos XZ fijados y propagación de fallo; nunca ejecuta el SDK Rust."""

import hashlib
import lzma
import os
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = (
    b'        tar -xJf "/inputs/rust/$componente.tar.xz" -C /work/sdk-sources --no-same-owner\n'
)
AFTER = b"""        /bin/bash -o pipefail -c '/usr/bin/xz -dc "$1" | /bin/tar -xf - -C "$2" --no-same-owner' -- "/inputs/rust/$componente.tar.xz" /work/sdk-sources\n"""


class XzExtraction(unittest.TestCase):
    def test_la_fuente_derivada_cambia_solo_extraccion_rust(self):
        original = (ROOT / "docker/postgres-immich/offline-container.sh").read_bytes()
        derived = (ROOT / "docker/postgres-immich/offline-container-v2.sh").read_bytes()
        self.assertEqual(
            hashlib.sha256(original).hexdigest(),
            "5c7c60976c89710c5ee0ca3e7786304b443d143467851927e21f0bf1b2dc8cbe",
        )
        self.assertEqual(original.count(BEFORE), 1)
        self.assertEqual(derived, original.replace(BEFORE, AFTER))

    def test_los_datos_128mib_reproducen_limite_64mib(self):
        # Cabeceras XZ/LZMA2, sin código ejecutable, construidas con diccionario128MiB.
        flags = b"\0\0"
        import zlib

        stream = b"\xfd7zXZ\0" + flags + zlib.crc32(flags).to_bytes(4, "little")
        for prop, expected_failure in ((0x1E, True), (0x1B, False)):
            block = bytes([2, 0, 0x21, 1, prop, 0, 0, 0])
            fixture = stream + block + zlib.crc32(block).to_bytes(4, "little") + b"\0"
            decoder = lzma.LZMADecompressor(memlimit=64 * 1024**2)
            if expected_failure:
                with self.assertRaisesRegex(lzma.LZMAError, "Memory usage limit"):
                    decoder.decompress(fixture)
            else:
                decoder.decompress(fixture)

    def test_pipefail_del_script_propaga_ambos_fallos(self):
        derived = (ROOT / "docker/postgres-immich/offline-container-v2.sh").read_text()
        line = next(line for line in derived.splitlines() if "/usr/bin/xz -dc" in line)
        arguments = shlex.split(line)
        self.assertEqual(arguments[:4], ["/bin/bash", "-o", "pipefail", "-c"])
        bash = shutil.which("bash")
        if os.name == "nt":
            bash = "C:/Program Files/Git/bin/bash.exe"
        self.assertIsNotNone(bash)
        program = arguments[4]
        for upstream, downstream, expected in ((7, 0, 7), (0, 9, 9)):
            # Sustitutos controlados: sólo shells y cat; no se llama xz/tar/SDK.
            stubbed = program.replace(
                "/usr/bin/xz", f"/bin/sh -c 'printf x; exit {upstream}' --"
            ).replace("/bin/tar", f"/bin/sh -c 'cat >/dev/null; exit {downstream}' --")
            result = subprocess.run(
                [bash, *arguments[1:4], stubbed, "--", "entrada", "destino"],
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
