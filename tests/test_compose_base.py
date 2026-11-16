"""Rechazos del contrato Compose; ninguna prueba inicia Docker ni lee la bóveda."""

import copy
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/ci/check_compose_base.py"
spec = importlib.util.spec_from_file_location("compose_base_check", SOURCE)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class ComposeBaseTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / "docker/docker-compose.yml").read_text(encoding="utf-8")
        self.model = checker.load_model(self.raw)

    def test_base_policy_and_synthetic_variables_are_closed(self):
        checker.validate_model(self.model)
        values = checker.synthetic_values(ROOT / "docker/compose.synthetic.env", self.raw)
        self.assertEqual(set(values), checker.required_variables(self.raw))
        self.assertEqual(len(self.model["services"]), 4)

    def test_database_or_events_published_on_lan_rejected(self):
        for name in ("postgres-apps", "valkey", "zou-events"):
            with self.subTest(name=name):
                bad = copy.deepcopy(self.model)
                bad["services"][name]["ports"] = ["0.0.0.0:5432:5432"]
                with self.assertRaises(ValueError):
                    checker.validate_model(bad)

    def test_pull_build_profiles_and_privileged_escape_rejected(self):
        for key, value in (
            ("build", "."),
            ("profiles", ["optional"]),
            ("pull_policy", "always"),
            ("user", "0:0"),
            ("privileged", True),
            ("cap_add", ["NET_ADMIN"]),
            ("network_mode", "host"),
            ("pid", "host"),
            ("env_file", ["../.env"]),
        ):
            with self.subTest(key=key):
                bad = copy.deepcopy(self.model)
                bad["services"]["zou-api"][key] = value
                with self.assertRaises(ValueError):
                    checker.validate_model(bad)

    def test_plaintext_secret_or_whole_vault_mount_rejected(self):
        for change in ("environment", "mount"):
            bad = copy.deepcopy(self.model)
            if change == "environment":
                bad["services"]["zou-api"]["environment"]["SECRET_KEY"] = "not-a-real-secret"
            else:
                bad["services"]["zou-api"]["volumes"][0]["source"] = "../.env"
            with self.assertRaises(ValueError):
                checker.validate_model(bad)

    def test_external_network_name_required_and_data_membership_closed(self):
        for change in ("default", "create", "membership"):
            bad = copy.deepcopy(self.model)
            if change == "default":
                bad["networks"]["data"]["name"] = "${MEDIA_PLATFORM_DATA_NETWORK_NAME:-default}"
            elif change == "create":
                bad["networks"]["data"]["external"] = False
            else:
                bad["services"]["postgres-apps"]["networks"].append("app")
            with self.assertRaises(ValueError):
                checker.validate_model(bad)

    def test_health_requires_files_and_never_expiring_token_or_shell_substitution(self):
        for name, command in (
            ("valkey", ["CMD-SHELL", "valkey-cli -a $(cat /run/secrets/valkey_password) ping"]),
            ("postgres-apps", ["CMD", "pg_isready"]),
            ("zou-api", ["CMD-SHELL", "curl -H 'Authorization: Bearer example' /status"]),
        ):
            bad = copy.deepcopy(self.model)
            bad["services"][name]["healthcheck"]["test"] = command
            with self.assertRaises(ValueError):
                checker.validate_model(bad)

    def test_logging_bounds_and_resource_limits_cannot_disappear(self):
        for key, value in (
            ("logging", {"driver": "local", "options": {"max-file": "1"}}),
            ("mem_limit", "16g"),
            ("pids_limit", -1),
            ("read_only", False),
        ):
            bad = copy.deepcopy(self.model)
            bad["services"]["valkey"][key] = value
            with self.assertRaises(ValueError):
                checker.validate_model(bad)

    def test_duplicate_yaml_key_rejected(self):
        with self.assertRaises(ValueError):
            checker.load_model("services: {}\nservices: {}\n")

    def test_synthetic_file_rejects_real_id_extra_key_and_expansion(self):
        original = (ROOT / "docker/compose.synthetic.env").read_text(encoding="utf-8")
        bad_files = (
            original.replace("sha256:" + "1" * 64, "sha256:" + "a" * 64),
            original + "\nAWS_SECRET_ACCESS_KEY=fixture\n",
            original.replace("synthetic_pg_admin_db", "${REAL_DATABASE}"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.env"
            for bad in bad_files:
                path.write_text(bad, encoding="utf-8")
                with self.assertRaises(ValueError):
                    checker.synthetic_values(path, self.raw)

    def test_health_requests_are_loopback_bounded_and_accept_only_expected_json(self):
        import json
        import types

        def check(kind, port, valid):
            observed = []

            class Response:
                status = 200

                def read(self, maximum):
                    observed.append(("read", maximum))
                    return json.dumps(self.payload).encode()

                def close(self):
                    observed.append(("close",))

            response = Response()
            response.payload = valid

            class Opener:
                def open(self, url, timeout):
                    observed.append(("open", url, timeout))
                    return response

            request = types.SimpleNamespace(
                ProxyHandler=lambda proxies: observed.append(("proxy", proxies)),
                build_opener=lambda handler: Opener(),
            )
            urllib = types.SimpleNamespace(request=request)

            def importer(name, *args):
                return {"json": json, "urllib.request": urllib}[name]

            code = checker.http_health(port, kind)[-1]
            exec(code, {"__builtins__": {**vars(__import__("builtins")), "__import__": importer}})
            self.assertEqual(
                observed,
                [
                    ("proxy", {}),
                    ("open", f"http://127.0.0.1:{port}/", 2),
                    ("read", 4097),
                    ("close",),
                ],
            )
            response.payload = {"unexpected": True}
            with self.assertRaises(AssertionError):
                exec(
                    code,
                    {"__builtins__": {**vars(__import__("builtins")), "__import__": importer}},
                )

        check("api", 5000, {"api": "Zou", "version": "1.1.1"})
        check("events", 5001, {"name": "Zou Event stream"})
        # create_app registra IndexResource antes del segundo GET / de events.
        check("events", 5001, {"api": "Zou", "version": "1.1.1"})

    def test_environment_for_config_does_not_inherit_host_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "RESTIC_PASSWORD": "fixture",
                    "DOCKER_HOST": "fixture",
                    "MEDIA_ZOU_APPLICATION_IMAGE_MANIFEST": "fixture",
                },
            ):
                env = checker.config_environment(Path(directory))
            self.assertNotIn("RESTIC_PASSWORD", env)
            self.assertNotIn("DOCKER_HOST", env)
            self.assertNotIn("MEDIA_ZOU_APPLICATION_IMAGE_MANIFEST", env)
            self.assertEqual(env["COMPOSE_DISABLE_ENV_FILE"], "true")

    def test_missing_docker_is_not_a_verified_compose_result(self):
        with patch.object(checker.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Docker Compose no disponible"):
                checker.verify_compose(
                    ROOT / "docker/docker-compose.yml", ROOT / "docker/compose.synthetic.env"
                )


if __name__ == "__main__":
    unittest.main()
