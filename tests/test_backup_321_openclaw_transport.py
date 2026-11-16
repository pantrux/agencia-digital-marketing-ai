"""Verifica el protocolo de OpenClaw con un TAR real y sin conexiones SSH."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sre import backup_321_openclaw_transport as transport

backup = transport.backup
prepare = transport.prepare


class OpenClawTransportTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.run_id = "daily-test-v1"
        self.files = {
            f"{agent}/config.json": backup.canonical({"id": agent, "model": model})
            for agent, model in prepare.AGENTS.items()
        }
        self.frame = backup.ZSTD_MAGIC + b"fixture"

    def payload(self, *, change_header=None, change_receipt=None):
        manifest = backup.canonical(
            {
                "schema": 1,
                "owner": backup.OWNER,
                "scope": "agency-state",
                "files": [
                    {"path": name, "size": len(raw), "sha256": backup.digest(raw)}
                    for name, raw in sorted(self.files.items())
                ],
            }
        )
        archive = backup.pack_tar(manifest, self.files, "agency-state")
        receipt = {
            "success": True,
            "action": "capture",
            "scope": "agency-state",
            "synthetic": False,
            "source_attestation": "external-staging-manifest",
            "agents": len(prepare.AGENTS),
            "files": len(self.files),
            "manifest_sha256": backup.digest(manifest),
            "archive_name": backup.ARCHIVE,
            "archive_sha256": backup.digest(self.frame),
            "archive_size": len(self.frame),
        }
        receipt.update(change_receipt or {})
        header = {
            "protocol": 1,
            "hostname": transport.HOSTNAME,
            "run_id": self.run_id,
            "started_at_utc": self.now.isoformat(),
            "completed_at_utc": self.now.isoformat(),
            "capture": receipt,
        }
        header.update(change_header or {})
        return backup.canonical(header) + self.frame, archive

    def verify(self, payload, archive):
        with patch.object(backup, "zstd", return_value=archive) as codec:
            row, frame, _, _ = transport.verify_response(
                payload, self.run_id, self.now, self.now + timedelta(seconds=2)
            )
            codec.assert_called_once_with(self.frame, decompress=True)
            return row, frame

    def test_proyeccion_exacta_y_archivo(self):
        row, frame = self.verify(*self.payload())
        self.assertEqual(row["files"], 5)
        self.assertEqual(frame, self.frame)

    def test_acepta_desfase_positivo_acotado_y_conserva_hora_remota(self):
        end = self.now + timedelta(seconds=2) + transport.CLOCK_SKEW
        payload, archive = self.payload(change_header={"completed_at_utc": end.isoformat()})
        with patch.object(backup, "zstd", return_value=archive):
            _, _, _, received_end = transport.verify_response(
                payload, self.run_id, self.now, self.now + timedelta(seconds=2)
            )
        self.assertEqual(received_end, end.isoformat())

    def test_rechaza_desfase_positivo_fuera_de_cota_e_orden_inverso(self):
        for changes in (
            {"completed_at_utc": (self.now + timedelta(seconds=7, microseconds=1)).isoformat()},
            {"started_at_utc": (self.now + timedelta(microseconds=1)).isoformat()},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "OPENCLAW_VENTANA"),
            ):
                self.verify(*self.payload(change_header=changes))

    def test_reloj_local_alcanza_marca_remota_antes_de_publicar(self):
        end = self.now + timedelta(milliseconds=13)
        with (
            patch.object(transport, "datetime", wraps=datetime) as clock,
            patch.object(transport.time, "sleep") as sleep,
        ):
            clock.now.side_effect = (self.now, end)
            transport.wait_until_received(end)
            sleep.assert_called_once_with(0.013)

    def test_rechaza_retroceso_del_reloj_local_durante_espera(self):
        end = self.now + timedelta(milliseconds=13)
        with (
            patch.object(transport, "datetime", wraps=datetime) as clock,
            patch.object(transport.time, "sleep") as sleep,
        ):
            clock.now.side_effect = (self.now, self.now - timedelta(seconds=6))
            with self.assertRaisesRegex(ValueError, "OPENCLAW_VENTANA"):
                transport.wait_until_received(end)
            sleep.assert_called_once_with(0.013)

    def test_rechaza_modelo_distinto_incluso_si_huellas_coinciden(self):
        self.files["agency-account/config.json"] = backup.canonical(
            {"id": "agency-account", "model": "openai/otro"}
        )
        with self.assertRaisesRegex(ValueError, "OPENCLAW_PROYECCION"):
            self.verify(*self.payload())

    def test_rechaza_fecha_pasada_corrida_ajena_y_alteraciones(self):
        cases = (
            (
                {"started_at_utc": (self.now - timedelta(minutes=1)).isoformat()},
                {},
                "OPENCLAW_VENTANA",
            ),
            ({"run_id": "otra-corrida"}, {}, "OPENCLAW_PROTOCOLO"),
            ({}, {"archive_sha256": "0" * 64}, "OPENCLAW_RECIBO_REMOTO"),
            ({}, {"manifest_sha256": "0" * 64}, "OPENCLAW_MANIFIESTO"),
        )
        for header, receipt, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.verify(*self.payload(change_header=header, change_receipt=receipt))

    def test_conserva_archivo_de_memoria_incluido(self):
        self.files["agency-editor/memory/revision.md"] = b"# Ejemplo sintetico\n"
        row, _ = self.verify(*self.payload())
        self.assertEqual(row["files"], 6)

    def test_ssh_fija_host_usuario_llave_y_desactiva_forwarding(self):
        argv = transport.ssh_argv("/perfil/identity", "/perfil/known_hosts", "ssh-ed25519")
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertEqual(argv[-2:], ["jandrade@192.168.0.7", "agency-backup-321-daily"])
        for option in (
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "IdentityAgent=none",
            "IdentitiesOnly=yes",
            "GlobalKnownHostsFile=/dev/null",
            "UserKnownHostsFile=/perfil/known_hosts",
            "ForwardAgent=no",
            "ClearAllForwardings=yes",
        ):
            self.assertIn(option, argv)


if __name__ == "__main__":
    unittest.main()
