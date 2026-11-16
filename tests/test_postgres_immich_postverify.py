"""Regresión de layouts Cargo y de recibos separados, sin red/SDK/Docker."""

import importlib.util
import io
import tarfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    path = ROOT / "docker/postgres-immich/postverify_remote.py"
    spec = importlib.util.spec_from_file_location("pg_postverify_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as target:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.mode, member.uid, member.gid = 0o644, 0, 0
            if kind == "file":
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
            else:
                member.type = tarfile.DIRTYPE if kind == "dir" else tarfile.SYMTYPE
                member.linkname = "/etc/passwd" if kind == "link" else ""
                target.addfile(member)
    return stream.getvalue()


class BuildOutputLayouts(unittest.TestCase):
    def setUp(self):
        self.remote = load()
        self.output = b"cargo:rustc-link-search=native=/opt/agency-sqlite-v3/lib\ncargo:rustc-link-lib=sqlite3\n"
        self.tag = "0123456789abcdef"

    def test_layout_legacy_es_unico_y_conserva_bytes(self):
        data = archive(
            [
                ("libsqlite3-sys-" + self.tag, b"", "dir"),
                ("libsqlite3-sys-" + self.tag + "/output", self.output, "file"),
            ]
        )
        result = self.remote.extract_build_output(data)
        self.assertEqual(result["layout"], "legacy")
        self.assertEqual(result["output"], self.output)

    def test_layout_cargo_1100_admite_raiz_directorio_literal(self):
        root = "libsqlite3-sys/" + self.tag
        result = self.remote.extract_build_output(
            archive(
                [
                    ("libsqlite3-sys", b"", "dir"),
                    (root, b"", "dir"),
                    (root + "/output", self.output, "file"),
                    (root + "/out", b"", "dir"),
                    (root + "/invoked.timestamp", b"", "file"),
                ]
            )
        )
        self.assertEqual(result["layout"], "nested")
        self.assertEqual(result["output"], self.output)

    def test_ambiguedad_de_dos_hashes_y_cero_output_rechaza(self):
        for rows in (
            [
                ("libsqlite3-sys/" + tag + "/output", self.output, "file")
                for tag in (self.tag, "f" * 16)
            ],
            [("libsqlite3-sys", b"", "dir")],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.remote.extract_build_output(archive(rows))

    def test_extras_enlaces_traversal_duplicados_y_raiz_file_rechaza(self):
        valid = ("libsqlite3-sys/" + self.tag + "/output", self.output, "file")
        for extra in [
            ("other-crate/output", b"x", "file"),
            ("../output", b"x", "file"),
            (valid[0], self.output, "file"),
            ("libsqlite3-sys", b"x", "file"),
            ("libsqlite3-sys/" + self.tag + "/out/link", b"", "link"),
            ("libsqlite3-sys/" + self.tag + "/unrelated.sh", b"x", "file"),
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.remote.extract_build_output(archive([valid, extra]))

    def test_solo_lee_el_cid_fijo_y_ruta_crate_del_archive_api(self):
        calls = []

        class Response:
            status = 200

            def getheader(self, _name):
                return "0"

            def read(self, _size):
                return b""

        class Connection:
            def request(self, method, path):
                calls.append((method, path))

            def getresponse(self):
                return Response()

            def close(self):
                pass

        self.assertEqual(self.remote.fetch_archive(Connection()), b"")
        self.assertEqual(calls[0][0], "GET")
        self.assertTrue(calls[0][1].startswith("/containers/" + self.remote.CID + "/archive?path="))
        self.assertNotIn("/exec", calls[0][1])

    def test_fallo_compile_y_stop_no_se_convierten_en_success(self):
        failed = {
            "success": False,
            "stage": "compile",
            "cid": self.remote.CID,
            "source_sha256": self.remote.COMPILE_SOURCE,
            "profile_sha256": self.remote.COMPILE_PROFILE,
            "log_sha256": self.remote.LOG_SHA,
        }
        stop = {
            "success": True,
            "stage": "stop",
            "cid": self.remote.CID,
            "source_sha256": self.remote.SDK_SOURCE,
            "plan_sha256": self.remote.SDK_PLAN,
            "stopped": True,
            "after_pid": 0,
        }
        from unittest.mock import patch

        with (
            patch.object(
                self.remote, "FAILED_SHA", self.remote.digest(self.remote.canonical(failed))
            ),
            patch.object(self.remote, "STOP_SHA", self.remote.digest(self.remote.canonical(stop))),
        ):
            self.remote.require_gates(self.remote.envelope(failed), self.remote.envelope(stop))
            changed = dict(failed, success=True)
            with self.assertRaises(ValueError):
                self.remote.require_gates(self.remote.envelope(changed), self.remote.envelope(stop))
        self.assertFalse(failed["success"])


class PostverifyBoundary(unittest.TestCase):
    def test_api_rechaza_truncado_y_limite_antes_de_parsear_tar(self):
        remote = load()

        class Response:
            status = 200

            def getheader(self, _name):
                return "10"

            def read(self, _size):
                return b""

        class Connection:
            closed = False

            def request(self, _method, _path):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                self.closed = True

        value = Connection()
        with self.assertRaisesRegex(ValueError, "truncada"):
            remote.fetch_archive(value)
        self.assertTrue(value.closed)
        with self.assertRaises(ValueError):
            remote.extract_build_output(b"x" * (remote.MAX_ARCHIVE + 1))

    def test_plan_por_defecto_no_abre_boveda_ni_ssh(self):
        import contextlib
        from unittest.mock import patch

        path = ROOT / "docker/postgres-immich/postverify.py"
        spec = importlib.util.spec_from_file_location("postverify_local_test", path)
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        with patch.object(local, "sdk_local") as sdk, contextlib.redirect_stdout(io.StringIO()):
            sdk.return_value.read_source.side_effect = lambda p: p.read_bytes()
            self.assertEqual(local.main([]), 0)
            sdk.return_value.read_values.assert_not_called()
            sdk.return_value.connect.assert_not_called()

    def test_fuentes_sql_y_licencias_provienen_del_tar_no_del_recibo(self):
        import zipfile

        remote = load()
        vector = "pgvector-159b79aaad5983fb7459c1e3df2897fbb2d11788/"
        vchord = "VectorChord-f9b5e234c44cee69b266b8431f92b7deed4e99d3/"

        def gz(rows):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz") as target:
                for name, data in rows:
                    item = tarfile.TarInfo(name)
                    item.size = len(data)
                    target.addfile(item, io.BytesIO(data))
            return stream.getvalue()

        compressed = io.BytesIO()
        with zipfile.ZipFile(compressed, "w") as z:
            z.writestr("sqlite-amalgamation-3540100/sqlite3.c", b"notice\n" * 30)
        files = {
            "pgvector-source.tar.gz": gz(
                [
                    (vector + "LICENSE", b"MIT"),
                    (vector + "vector.control", b"control"),
                    (vector + "sql/vector.sql", b"install"),
                    (vector + "sql/vector--0.8.4--0.8.5.sql", b"update"),
                ]
            ),
            "vectorchord-source.tar.gz": gz(
                [
                    (vchord + "LICENSE", b"dual"),
                    (vchord + "licenses/LICENSE.AGPLv3", b"AGPL"),
                    (vchord + "licenses/LICENSE.ELv2", b"ELv2"),
                ]
            ),
            "sqlite-amalgamation-3540100.zip": compressed.getvalue(),
        }

        class Normal:
            @staticmethod
            def read_file(_fd, name, **_kwargs):
                return files[name]

        class Store:
            descriptors = {"inputs": 123}

        result = remote.source_texts(Normal(), Store())
        self.assertEqual(
            result["usr/local/share/postgresql/extension/vector--0.8.5.sql"], b"install"
        )
        self.assertEqual(
            result["usr/local/share/licenses/agency-postgres-immich/SQLite-NOTICE"],
            b"notice\n" * 25,
        )
        self.assertEqual(len(result), 8)


class FramingContract(unittest.TestCase):
    def test_main_informa_success_que_acepta_collector_v4_real(self):
        import contextlib
        import json
        import signal
        import struct
        import sys
        import time
        from unittest.mock import patch

        remote = load()
        path = ROOT / "docker/postgres-immich/run_offline_sdk_v4.py"
        spec = importlib.util.spec_from_file_location("postverify_real_collector", path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)

        class Input:
            buffer = io.BytesIO(struct.pack(">I", 2) + b"{}")

        class Channel:
            def __init__(self, data, status):
                self.data, self.status = data, status

            def recv_ready(self):
                return bool(self.data)

            def recv(self, _size):
                result, self.data = self.data, b""
                return result

            def recv_stderr_ready(self):
                return False

            def recv_stderr(self, _size):
                return b""

            def exit_status_ready(self):
                return True

            def recv_exit_status(self):
                return self.status

        for success in (True, False):
            result = remote.envelope({"success": success})
            output = io.StringIO()
            Input.buffer = io.BytesIO(struct.pack(">I", 2) + b"{}")
            with (
                patch.object(sys, "stdin", Input()),
                patch.object(remote, "apply", return_value=result),
                patch.object(signal, "SIGALRM", 14, create=True),
                patch.object(signal, "signal"),
                patch.object(signal, "alarm", create=True),
                contextlib.redirect_stdout(output),
            ):
                status = remote.main()
            self.assertEqual(status, 0 if success else 1)
            observed = cli.collect(
                Channel(output.getvalue().encode(), status), time.monotonic() + 1, 65536
            )
            self.assertIs(observed["success"], success)
            self.assertEqual(observed["result"], result)
            self.assertEqual(json.loads(output.getvalue())["result"], result)


if __name__ == "__main__":
    unittest.main()
