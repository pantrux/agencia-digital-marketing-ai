import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "docker/observability/alertmanager_smtp_profile.py"
SPEC = importlib.util.spec_from_file_location("alertmanager_smtp_profile", SOURCE)
PROFILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE)


class AlertmanagerSMTPProfileTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "AGENCY_ALERTMANAGER_SMTP_USER": "sender+monitor@gmail.com",
            "AGENCY_ALERTMANAGER_SMTP_TO": "operations@example.org",
            "AGENCY_ALERTMANAGER_SMTP_PASSWORD": "should-not-be-read-by-generator",
        }

    def test_only_expected_alert_job_pairs_have_fixed_email_content(self):
        rules = SOURCE.with_name("prometheus.nas.rules.yml").read_text(encoding="utf-8")
        profiles = PROFILE.render_profiles(self.values)
        self.assertEqual(
            profiles, PROFILE.render_profiles(dict(reversed(list(self.values.items()))))
        )
        config = json.loads(profiles["alertmanager.yml"])
        route = config["route"]
        receivers = {item["name"]: item for item in config["receivers"]}
        self.assertEqual(receivers.pop(route["receiver"]), {"name": "discard"})
        actual = set()
        for child in route["routes"]:
            matchers = dict(pair.split("=", 1) for pair in child["matchers"])
            job = json.loads(matchers["job"])
            alert = json.loads(matchers["alertname"])
            self.assertIn(f"- alert: {alert}\n", rules)
            self.assertIn(f'job="{job}"', rules)
            actual.add((alert, job))
            rollback = alert == "AgenciaReversionStudio"
            self.assertEqual(matchers["severity"], '"critical"' if rollback else '"warning"')
            self.assertFalse(child["continue"])
            (email,) = receivers.pop(child["receiver"])["email_configs"]
            self.assertEqual(email["to"], self.values["AGENCY_ALERTMANAGER_SMTP_TO"])
            self.assertEqual(email["from"], email["auth_username"])
            self.assertIn(job, email["headers"]["Subject"])
            self.assertNotIn(".Labels", email["headers"]["Subject"] + email["text"])
            self.assertNotIn(".Alerts", email["text"])
            self.assertEqual(email["html"], "")
            self.assertEqual(email["send_resolved"], not rollback)
            if rollback:
                self.assertEqual(child["group_by"], ["alertname", "entrega", "modo"])
                self.assertIn(".CommonLabels.entrega", email["headers"]["Subject"])
                self.assertIn(".CommonAnnotations.description", email["text"])
            self.assertEqual(email["smarthost"], "smtp.gmail.com:587")
            self.assertTrue(email["require_tls"])
            self.assertFalse(email["force_implicit_tls"])
            self.assertEqual(email["auth_password_file"], "/run/alertmanager/smtp-password")
            self.assertNotIn("auth_password", email)
            self.assertEqual(
                email["tls_config"],
                {
                    "server_name": "smtp.gmail.com",
                    "ca_file": "/run/alertmanager/smtp-ca-bundle.pem",
                    "min_version": "TLS12",
                    "insecure_skip_verify": False,
                },
            )
        self.assertEqual(receivers, {})
        self.assertEqual(
            actual,
            {
                ("AgenciaNodeExporterSinMuestras", "agency-node"),
                ("AgenciaCAdvisorSinMuestras", "agency-cgroups"),
                ("AgenciaPuenteGPUSinMuestras", "agency-gpu-bridge"),
                ("AgenciaServicioGPUDegradado", "agency-gpu-bridge"),
                ("AgenciaReversionStudio", "agency-studio-sre"),
                ("AgenciaStudioMedicionEntregasDesactualizada", "agency-studio-delivery"),
                ("AgenciaStudioEntregasConDemora", "agency-studio-delivery"),
                ("AgenciaStudioLatenciaFueraObjetivo", "agency-studio-delivery"),
                ("AgenciaStudioSofiaIngresoDegradado", "agency-studio-edge"),
                ("AgenciaStudioSofiaSondaDesactualizada", "agency-studio-edge"),
                ("AgenciaWhisperLatenciaFueraObjetivo", "agency-whisper"),
                ("AgenciaStudioAudioFueraObjetivo", "agency-studio-render-audio"),
                ("AgenciaStudioMedicionAudioDesactualizada", "agency-studio-render-audio"),
                ("AgenciaStudioR2ServidorDegradado", "agency-studio-edge"),
                ("AgenciaStudioR2AnaliticaInaccesible", "agency-studio-edge"),
                ("AgenciaStudioIntegridadIngestaFueraObjetivo", "agency-studio-delivery"),
                ("AgenciaD1CopiaDiariaSinConfirmar", "agency-studio-d1-backup"),
                ("AgenciaD1MonitorCopiaSinMuestras", "agency-studio-d1-backup"),
            },
        )
        self.assertNotIn(
            self.values["AGENCY_ALERTMANAGER_SMTP_PASSWORD"].encode(), b"".join(profiles.values())
        )

    def test_web_profile_verifies_only_the_expected_client_san(self):
        web = json.loads(PROFILE.render_profiles(self.values)["web.yml"])
        self.assertEqual(
            web["tls_server_config"],
            {
                "cert_file": "/run/alertmanager/server.crt",
                "key_file": "/run/alertmanager/server.key",
                "client_ca_file": "/run/alertmanager/ca.crt",
                "client_auth_type": "RequireAndVerifyClientCert",
                "client_allowed_sans": ["agency-prometheus-notifier.nas.invalid"],
                "min_version": "TLS13",
            },
        )
        self.assertEqual(PROFILE.SERVER_SAN, "agency-alertmanager.nas.invalid")

    def test_rejects_multiple_recipients_and_untrusted_yaml_or_header_characters(self):
        for bad in (
            "",
            "name <x@x.org>",
            "a@x.org,b@y.org",
            "a@x.org\nBcc: b@y.org",
            "{{.Foo}}@x.org",
            "a..b@x.org",
            "a@-x.org",
        ):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "AGENCY_ALERTMANAGER_SMTP_TO") as error:
                    PROFILE.render_profiles({**self.values, "AGENCY_ALERTMANAGER_SMTP_TO": bad})
                if bad:
                    self.assertNotIn(bad, str(error.exception))
        with self.assertRaisesRegex(ValueError, "AGENCY_ALERTMANAGER_SMTP_USER"):
            PROFILE.render_profiles({"AGENCY_ALERTMANAGER_SMTP_TO": "operations@example.org"})

    @unittest.skipUnless(os.name == "posix", "Requiere permisos POSIX y openat")
    def test_publish_requires_private_directory_and_preserves_prior_file(self):
        profiles = PROFILE.render_profiles(self.values)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch).resolve()
            target = root / "profile"
            target.mkdir(mode=0o700)
            target.chmod(0o755)
            with self.assertRaises(ValueError):
                PROFILE.publish_profiles(target, profiles)
            target.chmod(0o700)
            alias = root / "alias"
            alias.symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                PROFILE.publish_profiles(alias, profiles)
            old = target / "web.yml"
            old.write_bytes(b"preexisting")
            with self.assertRaises(FileExistsError):
                PROFILE.publish_profiles(target, profiles)
            self.assertEqual(old.read_bytes(), b"preexisting")
            self.assertFalse((target / "alertmanager.yml").exists())
            old.unlink()
            PROFILE.publish_profiles(target, profiles)
            for name, raw in profiles.items():
                path = target / name
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
