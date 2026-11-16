"""ZIP adversarios como datos; nunca extrae ni ejecuta fuentes o SDK."""

import base64
import hashlib
import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sre"))
import restic_archive_inputs as M  # noqa: E402
import stage_restic_archives as S  # noqa: E402

MODULE, VERSION = "example.com/demo", "v1.0.0"
PREFIX = MODULE + "@" + VERSION + "/"


def archive(entries, *, compressed=zipfile.ZIP_STORED):
    out = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(out, "w", compression=compressed) as z:
        warnings.simplefilter("ignore", UserWarning)
        for name, data, mode in entries:
            info = zipfile.ZipInfo(PREFIX + name)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = compressed
            z.writestr(info, data)
    return out.getvalue()


def ordinary(name, data=b"package demo\n"):
    return name, data, stat.S_IFREG | 0o644


class ArchiveInputs(unittest.TestCase):
    def inspect(self, raw, **kwargs):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "module.zip"
            path.write_bytes(raw)
            return M.inspect_module_zip(path, MODULE, VERSION, **kwargs)

    def reject(self, raw, code=None, **kwargs):
        with self.assertRaises(M.ArchiveError) as failure:
            self.inspect(raw, **kwargs)
        if code is not None:
            self.assertEqual(failure.exception.code, code)

    def test_h1_independiente_del_orden_compresion_y_marca_tiempo(self):
        files = {PREFIX + "a.go": b"package demo\n", PREFIX + "LICENSE": b"MIT\n"}
        summary = b"".join(
            hashlib.sha256(files[name]).hexdigest().encode() + b"  " + name.encode() + b"\n"
            for name in sorted(files)
        )
        expected = "h1:" + base64.b64encode(hashlib.sha256(summary).digest()).decode()
        for method, entries in [
            (zipfile.ZIP_STORED, list(files.items())),
            (zipfile.ZIP_DEFLATED, list(reversed(list(files.items())))),
        ]:
            raw = archive(
                [ordinary(name[len(PREFIX) :], data) for name, data in entries], compressed=method
            )
            result = self.inspect(raw, expected_h1=expected)
            self.assertEqual(result["h1"], expected)
            self.assertEqual(result["files"], 2)
            self.assertEqual(result["source_utf8_files"], 1)
            self.assertEqual(result["licenses"][0]["path"], "LICENSE")

    def test_hashzip_incluye_entradas_directorio_vacias(self):
        raw = archive([("empty/", b"", stat.S_IFDIR | 0o755), ordinary("a.go")])
        names = {PREFIX + "empty/": b"", PREFIX + "a.go": b"package demo\n"}
        summary = "".join(
            hashlib.sha256(names[n]).hexdigest() + "  " + n + "\n" for n in sorted(names)
        ).encode()
        expected = "h1:" + base64.b64encode(hashlib.sha256(summary).digest()).decode()
        self.assertEqual(self.inspect(raw)["h1"], expected)

    def test_checksum_declarado_distinto_se_rechaza(self):
        self.reject(
            archive([ordinary("a.go")]),
            "H1_MISMATCH",
            expected_h1="h1:" + base64.b64encode(b"x" * 32).decode(),
        )

    def test_nombres_raw_no_utf8_aunque_cp437_sea_decodificable(self):
        raw = archive([ordinary("a.go")]).replace(b"a.go", b"\xff.go")
        self.reject(raw, "NAME_NOT_UTF8")

    def test_header_local_y_central_deben_conservar_mismo_nombre_raw(self):
        raw = bytearray(archive([ordinary("a.go")]))
        position = raw.find(b"a.go")
        raw[position] = ord("b")
        self.reject(bytes(raw), "LOCAL_NAME_MISMATCH")

    def test_nombres_utf8_sin_flag_no_dependen_de_cp437(self):
        raw = bytearray(archive([ordinary("\u00e9.go")]))
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            p = raw.find(signature) + offset
            flags = struct.unpack_from("<H", raw, p)[0]
            struct.pack_into("<H", raw, p, flags & ~0x800)
        result = self.inspect(bytes(raw))
        self.assertEqual(result["source_utf8_files"], 1)

    def test_escapes_prefijo_sufijo_dos_y_case_collisions(self):
        for name in ("../x.go", "/x.go", "a/../../x.go", "NUL.txt", "x. ", "a/go.mod", "Go.Mod"):
            with self.subTest(name=name):
                self.reject(archive([ordinary(name)]))
        self.reject(archive([ordinary("a/x.go")]).replace(b"a/x.go", b"a\\x.go"))
        for names in (("A.go", "a.go"), ("A/x.go", "a/y.go"), ("a", "a/x.go")):
            with self.subTest(names=names):
                self.reject(archive([ordinary(name) for name in names]))
        self.reject(
            archive([ordinary("a.go")]).replace(
                PREFIX.encode(), b"other.test/demo@v1/".ljust(len(PREFIX), b"x")
            )
        )

    def test_symlink_tipos_especiales_y_duplicados(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFCHR | 0o600):
            self.reject(archive([("link", b"target", mode)]), "FILE_TYPE")
        self.reject(archive([ordinary("a.go"), ordinary("a.go")]), "PATH_COLLISION")

    def test_fuentes_go_y_assembly_utf8_estricto_sin_exigirlo_al_binary_fixture(self):
        for name in ("a.go", "a.s", "a.S"):
            self.reject(archive([ordinary(name, b"\xff")]), "SOURCE_NOT_UTF8")
        self.assertEqual(self.inspect(archive([ordinary("data.bin", b"\xff")]))["files"], 1)

    def test_crc_truncado_y_limites_reales(self):
        raw = bytearray(archive([ordinary("a.go", b"package demo\n")]))
        raw[raw.find(b"package demo")] ^= 1
        self.reject(bytes(raw), "ZIP_INVALID")
        self.reject(archive([ordinary("a.go")])[:-10])
        self.reject(
            archive([ordinary("a.go", b"x" * 50)]),
            "EXPANDED_LIMIT",
            limits=M.ZipLimits(max_expanded=40),
        )
        self.reject(
            archive([ordinary("a.go"), ordinary("b.go")]),
            "ENTRY_LIMIT",
            limits=M.ZipLimits(max_entries=1),
        )


class StagingContract(unittest.TestCase):
    def test_redirect_solo_https_dos_hosts_sin_usuario_ni_puertos_ajenos(self):
        for value in (
            "https://storage.googleapis.com/object?signature=privateFixture",
            "https://proxy.golang.org/object",
        ):
            self.assertEqual(S.allowed_url(value), value)
        for value in (
            "http://storage.googleapis.com/object",
            "https://storage.googleapis.com.evil/object",
            "https://user@proxy.golang.org/object",
            "https://proxy.golang.org:8443/x",
        ):
            with self.subTest(value=value), self.assertRaises(S.StageError):
                S.allowed_url(value)

    def test_plan_predeterminado_no_prepara_raiz_ni_usa_red(self):
        with (
            patch.object(S, "make_plan", return_value={"modules": []}),
            patch.object(S, "prepare") as prepare,
            patch.object(S, "stage") as stage,
            patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(S.main([]), 0)
        prepare.assert_not_called()
        stage.assert_not_called()

    def test_archivos_ajenos_no_se_sobrescriben(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owned"
            path.write_bytes(b"centinela")
            with self.assertRaises(FileExistsError):
                S.write_new(path, b"otro")
            self.assertEqual(path.read_bytes(), b"centinela")

    def test_download_acotado_y_sin_env_auth_cookie(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Length": "4", "Content-Encoding": "identity"}

        requests = []

        def opener(request, timeout):
            requests.append(request)
            return Response(b"data")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"HTTPS_PROXY": "otro", "TOKEN": "otro"}),
        ):
            path = Path(tmp) / "new.zip"
            report = S.download_zip(MODULE, VERSION, path, open_url=opener)
            self.assertEqual(path.read_bytes(), b"data")
            self.assertEqual(report["compressed_bytes"], 4)
            self.assertEqual(report["redirects"], 0)
        self.assertEqual(len(requests), 1)
        self.assertFalse(
            any(
                name.lower() in ("authorization", "cookie", "proxy-authorization")
                for name in requests[0].headers
            )
        )

    def test_limite_de_bytes_se_rechaza(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Length": "5"}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(S.StageError) as failure:
                S.download_zip(
                    MODULE,
                    VERSION,
                    Path(tmp) / "new.zip",
                    max_bytes=4,
                    open_url=lambda *args, **kwargs: Response(b"12345"),
                )
            self.assertEqual(str(failure.exception), "DOWNLOAD_LIMIT")

    def test_rutas_de_recibo_no_pueden_salir_de_la_raiz(self):
        with self.assertRaises(S.StageError):
            S.file_row("../centinela")

    def test_un_destino_preexistente_no_se_adopta_como_quarantine_propia(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "quarantine").mkdir()
            foreign = root / "quarantine/0001.zip"
            foreign.write_bytes(b"centinela")
            plan = {"modules": [{"index": 1, "module": MODULE, "version": VERSION}]}
            with (
                patch.object(S, "ROOT", root),
                patch.object(S, "guard_root"),
                patch.object(S, "records", return_value={}),
                patch.object(S, "download_zip") as download,
            ):
                with self.assertRaises(S.StageError):
                    S.stage(plan, "a" * 64, 1)
                download.assert_not_called()
            self.assertEqual(foreign.read_bytes(), b"centinela")

    def test_receipt_no_lee_rutas_entrantes_fuera_de_su_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "receipts").mkdir()
            row = {
                "index": 1,
                "module": MODULE,
                "version": VERSION,
                "status": "staged",
                "plan_sha256": "a" * 64,
                "files": [{"path": "../centinela"}],
            }
            (root / "receipts/0001.json").write_text(json.dumps(row), encoding="utf-8")
            plan = {
                "modules": [
                    {
                        "index": 1,
                        "module": MODULE,
                        "version": VERSION,
                        "target": "proxy/x.zip",
                        "metadata": {
                            "mod": {"target": "proxy/x.mod"},
                            "info": {"target": "proxy/x.info"},
                        },
                    }
                ]
            }
            with patch.object(S, "ROOT", root), patch.object(S, "file_row") as read:
                with self.assertRaises(S.StageError):
                    S.records(plan, "a" * 64)
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
