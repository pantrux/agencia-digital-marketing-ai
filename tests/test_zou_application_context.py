"""Contratos puros y publicación local; no ejecuta ruedas, APK ni servicios."""

import base64
import csv
import hashlib
import importlib.util
import io
import pathlib
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "docker/kitsu-zou" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


context = load("zou_context_test", "prepare_application_context.py")
launcher = load("zou_launcher_test", "launch_zou.py")


def wheel(name="zou", version="1.1.1+agency1", dependencies=(), extra_files=None):
    dist = name.replace("-", "_") + "-" + version + ".dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: >=3.11\n"
    metadata += "".join(f"Requires-Dist: {dep}\n" for dep in dependencies)
    files = {
        dist + "/METADATA": metadata.encode(),
        dist + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        name.replace("-", "_") + "/__init__.py": b"raise AssertionError('do not execute')\n",
    }
    files.update(extra_files or {})
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for filename, body in files.items():
        checksum = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
        writer.writerow((filename, "sha256=" + checksum, len(body)))
    writer.writerow((dist + "/RECORD", "", ""))
    files[dist + "/RECORD"] = stream.getvalue().encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, body in files.items():
            archive.writestr(filename, body)
    raw = out.getvalue()
    return raw, f"{name.replace('-', '_')}-{version}-py3-none-any.whl"


def capture():
    return {
        "kind": "python-runtime-image-sca-inputs-v1",
        "subject": {
            "manifest": "sha256:" + "1" * 64,
            "config": "sha256:" + "2" * 64,
            "archives": [
                {"name": "base.tar", "sha256": "3" * 64, "bytes": 100},
                {"name": "final.tar", "sha256": "4" * 64, "bytes": 200},
            ],
        },
        "coverage": {"installed_apk": 19, "manual_apk_payloads": 14, "total_components": 33},
        "python": context.PYTHON_IDENTITY.copy(),
        "native_files": {
            "usr/bin/python3.12": {"sha256": context.PYTHON_IDENTITY["executable_sha256"]},
            "usr/lib/libpython3.12.so.1.0": {"sha256": context.PYTHON_IDENTITY["libpython_sha256"]},
        },
        "runtime_approved": False,
        "sca_approved": False,
    }


class ContextTests(unittest.TestCase):
    def inspect(self, *args, **kwargs):
        raw, filename = wheel(*args, **kwargs)
        return context.inspect_wheel(raw, filename, context.digest(raw))

    def test_wheel_is_data_and_metadata_record_are_verified(self):
        result = self.inspect(dependencies=["psycopg==3.3.4"])
        self.assertEqual(result["name"], "zou")
        self.assertEqual(result["version"], "1.1.1+agency1")
        self.assertIn(b"Requires-Dist: psycopg==3.3.4", result["metadata"])

    def test_changed_archive_is_rejected(self):
        raw, filename = wheel()
        with self.assertRaises(ValueError):
            context.inspect_wheel(raw + b"x", filename, context.digest(raw))

    def test_setuptools_resource_spaces_are_data_but_traversal_is_rejected(self):
        result = self.inspect(
            "setuptools",
            "84.2.1",
            extra_files={
                "setuptools/launcher manifest.xml": b"data",
                "setuptools/script (dev).tmpl": b"data",
            },
        )
        self.assertEqual(result["name"], "setuptools")
        with self.assertRaises(ValueError):
            self.inspect(extra_files={"../escape.py": b"data"})

    def test_vendored_metadata_is_preserved_and_not_outer_identity(self):
        self.inspect(
            "setuptools",
            "84.2.1",
            extra_files={
                "setuptools/_vendor/jaraco.text-1.dist-info/METADATA": b"Name: jaraco.text\nVersion: 1\n"
            },
        )
        with self.assertRaises(ValueError):
            self.inspect(extra_files={"other-1.dist-info/METADATA": b"Name: other\nVersion: 1\n"})

    def test_lock_rejects_binary_driver_and_duplicate_identity(self):
        row = {"name": "psycopg", "version": "3.3.4", "sha256": "1" * 64}
        self.assertEqual(
            context.requirements_lock([row]), b"psycopg==3.3.4 --hash=sha256:" + b"1" * 64 + b"\n"
        )
        for rows in [[row, row], [row | {"name": "psycopg-binary"}]]:
            with self.assertRaises(ValueError):
                context.requirements_lock(rows)

    def test_record_does_not_cover_changed_metadata(self):
        raw, filename = wheel()
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(out, "w") as dest:
            for item in source.infolist():
                body = source.read(item)
                if item.filename.endswith("/METADATA"):
                    body += b"Requires-Dist: foreign-package\n"
                dest.writestr(item.filename, body)
        changed = out.getvalue()
        with self.assertRaises(ValueError):
            context.inspect_wheel(changed, filename, context.digest(changed))

    def test_closure_uses_pure_driver_and_local_versions(self):
        metadata = {
            "zou": self.inspect(dependencies=["psycopg==3.3.4", "flasgger==0.9.7.1"])["metadata"],
            "psycopg": self.inspect("psycopg", "3.3.4")["metadata"],
            "flasgger": self.inspect("flasgger", "0.9.7.1+agency1")["metadata"],
        }
        result = context.validate_closure(metadata)
        self.assertEqual(result["selected"], ["flasgger", "psycopg", "zou"])

    def test_closure_rejects_binary_extra(self):
        metadata = {
            "zou": self.inspect(dependencies=["psycopg[binary]==3.3.4"])["metadata"],
            "psycopg": self.inspect("psycopg", "3.3.4")["metadata"],
        }
        with self.assertRaises(ValueError):
            context.validate_closure(metadata)

    def test_direct_url_and_missing_dependency_are_rejected(self):
        for dependency in ["foo @ https://example.invalid/foo.whl", "foo==1"]:
            with self.subTest(dependency=dependency), self.assertRaises(ValueError):
                context.validate_closure(
                    {"zou": self.inspect(dependencies=[dependency])["metadata"]}
                )

    def test_unreferenced_wheel_is_not_silently_added(self):
        metadata = {
            "zou": self.inspect()["metadata"],
            "foreign": self.inspect("foreign", "1")["metadata"],
        }
        with self.assertRaises(ValueError):
            context.validate_closure(metadata)

    def test_runtime_capture_requires_exact_hash_and_current_subject(self):
        raw = context.canonical(capture())
        result = context.runtime_subject(raw, context.digest(raw))
        self.assertEqual(result["manifest"], "sha256:" + "1" * 64)
        self.assertFalse(result["production_approved"])
        with self.assertRaises(ValueError):
            context.runtime_subject(raw, "0" * 64)

    def test_wolfi_base_is_not_a_python_capture(self):
        data = capture()
        data["subject"]["manifest"] = context.WOLFI_BASE
        raw = context.canonical(data)
        with self.assertRaises(ValueError):
            context.runtime_subject(raw, context.digest(raw))

    def test_capture_missing_python_or_final_archive_is_rejected(self):
        for key in ["python", "native_files"]:
            data = capture()
            data.pop(key)
            raw = context.canonical(data)
            with self.subTest(key=key), self.assertRaises(ValueError):
                context.runtime_subject(raw, context.digest(raw))
        data = capture()
        data["subject"]["archives"].pop()
        raw = context.canonical(data)
        with self.assertRaises(ValueError):
            context.runtime_subject(raw, context.digest(raw))

    def test_publication_requires_ready_and_matching_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp) / "context"
            for plan, pin in [({"ready": False}, "0" * 64), ({"ready": True}, "0" * 64)]:
                with self.assertRaises(ValueError):
                    context.publish_context(
                        target, plan, {}, pin, check_private=lambda *_a, **_k: None
                    )
                self.assertFalse(target.exists())

    def test_context_is_exclusive_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp) / "context"
            files = {"wheels/test.whl": b"pinned data", "Dockerfile": b"FROM pythonruntime\n"}
            plan = {
                "ready": True,
                "files": {
                    name: {"sha256": context.digest(raw), "bytes": len(raw)}
                    for name, raw in files.items()
                },
            }
            pin = context.digest(context.canonical(plan))
            result = context.publish_context(
                target, plan, files, pin, check_private=lambda *_a, **_k: None
            )
            self.assertTrue(result["success"])
            self.assertEqual((target / "wheels/test.whl").read_bytes(), b"pinned data")
            with self.assertRaises((ValueError, FileExistsError)):
                context.publish_context(
                    target, plan, files, pin, check_private=lambda *_a, **_k: None
                )


class LauncherTests(unittest.TestCase):
    def environment(self):
        return {
            "DB_HOST": "postgres",
            "DB_PORT": "5432",
            "DB_USERNAME": "zou",
            "DB_DATABASE": "zou",
            "KV_HOST": "valkey",
            "KV_PORT": "6379",
        }

    def test_only_fixed_secrets_are_read_and_pure_driver_is_forced(self):
        seen = []

        def secret(path):
            seen.append(path)
            return "a long fixture credential"

        env = launcher.runtime_environment(self.environment(), secret)
        self.assertEqual(env["PSYCOPG_IMPL"], "python")
        self.assertEqual(env["TMPDIR"], "/var/lib/zou/tmp")
        self.assertEqual(
            set(seen),
            {
                "/run/secrets/zou_db_password",
                "/run/secrets/zou_kv_password",
                "/run/secrets/zou_secret_key",
            },
        )
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("CACHE_TYPE", env)

    def test_missing_host_and_pending_secret_fail_closed(self):
        for env in [{}, self.environment() | {"DB_HOST": "https://bad/path"}]:
            with self.assertRaises(ValueError):
                launcher.runtime_environment(env, lambda _p: "fixture credential")
        with self.assertRaises(ValueError):
            launcher.runtime_environment(self.environment(), lambda _p: "CHANGE_ME")

    def test_one_process_for_api_and_events_and_unknown_command_rejected(self):
        for mode, entry in [("api", "zou.app:app"), ("events", "zou.event_stream:app")]:
            argv = launcher.gunicorn_arguments(mode)
            self.assertEqual(argv[argv.index("--workers") + 1], "1")
            self.assertEqual(argv[-1], entry)
        with self.assertRaises(ValueError):
            launcher.gunicorn_arguments("shell")

    def test_module_import_did_not_load_zou_or_gunicorn(self):
        import sys

        self.assertNotIn("zou", sys.modules)
        self.assertNotIn("gunicorn", sys.modules)

    def test_installed_database_requires_unique_exact_identities(self):
        rows = launcher.installed_database(
            b"P:apk-tools\nV:2.14.13-r0\nA:x86_64\n\nP:glibc-2.44\nV:2.44-r13\nA:x86_64\n\n"
        )
        self.assertEqual(rows, {"apk-tools": "2.14.13-r0", "glibc-2.44": "2.44-r13"})
        with self.assertRaises(ValueError):
            launcher.installed_database(b"P:one\nV:1\n\nP:one\nV:2\n\n")

    def test_apk_selection_is_native_offline_and_index_authenticated(self):
        rows = [{"name": "libpq-18", "version": "18.6-r7"}, {"name": "gdbm", "version": "1.26-r9"}]
        argv = launcher.apk_arguments(rows)
        self.assertIn("--no-network", argv)
        self.assertIn("--no-scripts", argv)
        self.assertIn("--no-commit-hooks", argv)
        self.assertNotIn("--allow-untrusted", argv)
        self.assertEqual(argv[-2:], ["gdbm=1.26-r9", "libpq-18=18.6-r7"])
        with self.assertRaises(ValueError):
            launcher.apk_arguments([{"name": "pkg;echo", "version": "1"}])

    def test_pip_arguments_require_hashes_without_dependency_resolution(self):
        argv = launcher.pip_arguments()
        self.assertIn("--no-index", argv)
        self.assertIn("--no-deps", argv)
        self.assertIn("--require-hashes", argv)
        self.assertIn("--isolated", argv)
        self.assertEqual(argv[argv.index("--target") + 1], "/opt/zou/site")


if __name__ == "__main__":
    unittest.main()
