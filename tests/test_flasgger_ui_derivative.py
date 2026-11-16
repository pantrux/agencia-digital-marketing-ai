"""Contratos puros; sin Flask instalado, JavaScript, red ni SDK."""

import ast
import base64
import csv
import hashlib
import importlib.util
import io
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "docker/kitsu-zou/derive_flasgger_wheel.py"
SPEC = importlib.util.spec_from_file_location("flasgger_derivative_test", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def fixture():
    files = {f"flasgger/ui2/static/fixture{i}.js": b"legado" for i in range(65)}
    files.update(
        {
            "flasgger/ui3/static/lib/jquery.min.js": b"jQuery 2.2.4",
            "flasgger/ui3/static/lib/jquery.min.map": b"mapa",
            "flasgger/__init__.py": b"__version__ = '0.9.7.1'\n",
            "flasgger/base.py": (
                b"def register(config):\n"
                b"    class Self: pass\n"
                b"    self = Self()\n"
                b"    self.config = config\n"
                b"    if self.config.get('swagger_ui', True):\n"
                b"        uiversion = self.config.get('uiversion', 3)\n"
                b"        return uiversion\n"
            ),
            "flasgger/ui3/templates/flasgger/body_scripts.html": (
                b'<script src="{{ swagger_ui_bundle_js }}"></script>\n'
                b'<script src="{{ swagger_ui_standalone_preset_js }}"></script>\n'
                b"<script src=\"{{ jquery_js }}\" type='text/javascript'></script>\n"
            ),
            "flasgger/ui3/templates/flasgger/head.html": (
                b"<title>{{ title }}</title>\n"
                b'<link href="https://fonts.googleapis.com/css?family=Open+Sans:400,700|Source+Code+Pro:300,600|Titillium+Web:400,600,700" rel="stylesheet">\n'
                b'<link rel="stylesheet" href="{{ swagger_ui_css }}">\n'
                b"<style>body {margin:0;background:#fafafa;}</style>\n"
            ),
            "flasgger/ui3/templates/flasgger/swagger.html": (
                b"    // Build a system\n"
                b"    const ui = SwaggerUIBundle(\n"
                b"    Object.assign(\n"
                b"    {requestInterceptor: preserved, responseInterceptor: preserved},\n"
                b"    {{ json.dumps(flasgger_config.get('ui_params', {})) | safe }}\n"
                b"    {% if flasgger_config.ui_params_text -%}\n"
                b"    , {{ flasgger_config.ui_params_text | safe }}\n"
                b"    {%- endif %}\n"
                b"    )\n    \n    )\n\n    let auth_config = null;\n"
                b"    ui.initOAuth(auth_config);\n    window.ui = ui\n"
                b"    {% if not flasgger_config.hide_top_bar -%}\n"
                b'    $(".topbar-wrapper .link span").replaceWith("<span>{{title}}</span>");\n'
                b"    {%- endif %}\n"
            ),
            "flasgger-0.9.7.1.dist-info/METADATA": (
                b"Name: flasgger\r\nVersion: 0.9.7.1\r\nRequires-Dist: Flask>=0.10\r\n\r\n"
            ),
            "flasgger-0.9.7.1.dist-info/licenses/LICENSE": b"MIT conservada\n",
            "flasgger/ui3/static/swagger-ui-bundle.js": b"bundle original\n",
        }
    )
    removed = [n for n in files if n.startswith("flasgger/ui2/") or "/lib/jquery.min." in n]
    return files, removed


def wheel_bytes(files, *, extra=None):
    files = dict(files)
    record = M.OLD_DIST + "/RECORD"
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\n")
    for name, raw in sorted(files.items()):
        checksum = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        writer.writerow((name, "sha256=" + checksum, str(len(raw))))
    writer.writerow((record, "", ""))
    files[record] = text.getvalue().encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
        if extra is not None:
            name, raw, mode = extra
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, raw)
    return stream.getvalue()


class MemberTests(unittest.TestCase):
    def test_removes_only_exact_67_and_preserves_bundle_and_license(self):
        files, removed = fixture()
        changed = M.derive_members(files, removed)
        normalized = {n.replace(M.NEW_DIST + "/", M.OLD_DIST + "/", 1) for n in changed}
        self.assertEqual(set(files) - normalized, set(removed))
        self.assertFalse(any(n.startswith("flasgger/ui2/") for n in changed))
        self.assertEqual(changed["flasgger/ui3/static/swagger-ui-bundle.js"], b"bundle original\n")
        self.assertEqual(
            changed["flasgger-0.9.7.1+agency1.dist-info/licenses/LICENSE"], b"MIT conservada\n"
        )

    def test_ui3_guard_executes_before_registration_and_rejects_other_versions(self):
        files, removed = fixture()
        base = M.derive_members(files, removed)["flasgger/base.py"]
        tree = ast.parse(base)
        namespace = {}
        exec(compile(tree, "<guard-fixture>", "exec"), namespace)
        self.assertEqual(namespace["register"]({}), 3)
        self.assertEqual(namespace["register"]({"uiversion": 3}), 3)
        for value in [2, 4, True, "3", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                namespace["register"]({"uiversion": value})

    def test_callback_preserves_this_args_return_and_exception_in_finally(self):
        files, removed = fixture()
        changed = M.derive_members(files, removed)
        js = changed["flasgger/ui3/templates/flasgger/swagger.html"]
        self.assertIn(b"const priorOnComplete = uiOptions.onComplete;", js)
        self.assertIn(b"return priorOnComplete.apply(this, arguments);", js)
        self.assertIn(b"finally {", js)
        self.assertIn(b"element.textContent = {{ title | tojson }};", js)
        self.assertNotIn(b"replaceWith", js)
        self.assertIn(b"ui.initOAuth(auth_config);", js)
        self.assertIn(b"requestInterceptor: preserved, responseInterceptor: preserved", js)
        self.assertIn(b"ui_params_text", js)
        self.assertNotIn(b"jquery", changed["flasgger/ui3/templates/flasgger/body_scripts.html"])

    def test_removal_manifest_cannot_expand_omit_or_duplicate(self):
        files, removed = fixture()
        for changed in [removed[:-1], removed + removed[:1], removed[:-1] + ["flasgger/base.py"]]:
            with self.subTest(removed=changed[-2:]), self.assertRaises(ValueError):
                M.derive_members(files, changed)

    def test_unexpected_or_repeated_template_patch_is_rejected(self):
        files, removed = fixture()
        path = M.TEMPLATE + "swagger.html"
        for changed in [b"otro recurso", files[path] + files[path]]:
            with self.subTest(raw=changed[:20]), self.assertRaises(ValueError):
                M.derive_members({**files, path: changed}, removed)

    def test_removes_only_google_fonts_link_keeps_local_css_and_style_fallback(self):
        files, removed = fixture()
        before = files[M.TEMPLATE + "head.html"]
        after = M.derive_members(files, removed)[M.TEMPLATE + "head.html"]
        self.assertNotIn(b"fonts.googleapis.com", after)
        expected = b"\n".join(
            line for line in before.split(b"\n") if b"fonts.googleapis.com" not in line
        )
        self.assertEqual(after, expected)
        self.assertIn(b"{{ swagger_ui_css }}", after)
        self.assertIn(b"background:#fafafa", after)


class WheelTests(unittest.TestCase):
    def test_output_is_reproducible_and_record_covers_every_retained_byte(self):
        files, removed = fixture()
        original = wheel_bytes(files)
        before = bytes(original)
        derived, proof = M.derive_bytes(original, removed)
        self.assertEqual(derived, M.derive_bytes(original, removed)[0])
        self.assertEqual(original, before)
        actual = M.inspect_wheel(derived, M.NEW_DIST)
        self.assertEqual(len(actual), len(files) + 1 - 67)
        self.assertEqual(proof["removed_count"], 67)
        self.assertFalse(proof["runtime_approved"])
        self.assertIn(b"Version: 0.9.7.1+agency1\r\n", actual[M.NEW_DIST + "/METADATA"])
        self.assertEqual(
            actual[M.NEW_DIST + "/METADATA"].replace(
                b"Version: 0.9.7.1+agency1\r\n", b"Version: 0.9.7.1\r\n"
            ),
            files[M.OLD_DIST + "/METADATA"],
        )

    def test_record_is_not_trusted_after_bytes_change(self):
        files, _ = fixture()
        raw = wheel_bytes(files)
        stream = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(stream, "w") as target:
            for info in source.infolist():
                body = source.read(info)
                if info.filename.endswith("/LICENSE"):
                    body += b"alterado"
                target.writestr(info, body)
        with self.assertRaises(ValueError):
            M.inspect_wheel(stream.getvalue(), M.OLD_DIST)

    def test_zip_rejects_escapes_links_case_aliases_and_trailing_bytes(self):
        files, _ = fixture()
        for name, mode in [
            ("../escape", stat.S_IFREG | 0o600),
            ("flasgger/BASE.py", stat.S_IFREG | 0o600),
            ("flasgger/link", stat.S_IFLNK | 0o777),
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                M.inspect_wheel(wheel_bytes(files, extra=(name, b"datos", mode)), M.OLD_DIST)
        with self.assertRaises(ValueError):
            M.inspect_wheel(wheel_bytes(files) + b"contenido extra", M.OLD_DIST)

    def test_private_publication_is_exclusive_and_does_not_delete_foreign_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "candidate.whl"
            foreign = root / "foreign"
            foreign.write_bytes(b"ajeno")
            M.write_new(target, b"primero", lambda *_args, **_kwargs: None)
            with self.assertRaises(FileExistsError):
                M.write_new(target, b"segundo", lambda *_args, **_kwargs: None)
            self.assertEqual(target.read_bytes(), b"primero")
            self.assertEqual(foreign.read_bytes(), b"ajeno")

    def test_bounded_reader_rejects_oversized_and_irregular_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            source.write_bytes(b"1234")
            self.assertEqual(M.read_file(source, 4), b"1234")
            with self.assertRaises(ValueError):
                M.read_file(source, 3)
            with self.assertRaises(ValueError):
                M.read_file(root, 32)

    def test_root_identity_is_checked_before_writes_and_completed_namespace_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / M.ROOT_RELATIVE
            root.mkdir(parents=True)
            info = root.stat()
            identity = {
                "schema_version": 1,
                "owner": M.OWNER,
                "root": str(root),
                "device": info.st_dev,
                "inode": info.st_ino,
            }
            marker = M.canonical(identity)
            (root / M.MARKER).write_bytes(marker)
            proof = {**identity, "marker_sha256": M.digest(marker)}
            with (
                patch.object(M, "WORKSPACE", workspace),
                patch.object(M, "load_access", return_value=lambda *_a, **_kw: None),
            ):
                with self.assertRaisesRegex(ValueError, "ROOT_IDENTITY"):
                    M.publish({}, b"datos", {}, M.canonical({**proof, "inode": info.st_ino + 1}))
                self.assertEqual({p.name for p in root.iterdir()}, {M.MARKER})
                self.assertTrue(M.publish({}, b"datos", {}, M.canonical(proof))["success"])
                with self.assertRaisesRegex(ValueError, "ROOT_NOT_EMPTY"):
                    M.publish({}, b"otro", {}, M.canonical(proof))
            self.assertEqual((root / M.OUTPUT).read_bytes(), b"datos")

    def test_apply_requires_external_manifest_plan_and_root_proof_before_publication(self):
        with (
            patch.object(M, "inspect_context", return_value=(b"manifest", {}, b"wheel", {})),
            patch.object(M, "publish", side_effect=AssertionError("NO_PUBLICAR")),
            self.assertRaisesRegex(ValueError, "APPLY_PINS"),
        ):
            M.main(["--operation", "apply"])

    def test_import_does_not_read_manifest_publish_or_run_main(self):
        source = PATH.read_bytes()
        with patch.object(Path, "open", side_effect=AssertionError("NO_LEER")):
            namespace = {"__file__": str(PATH), "__name__": "pure_import_fixture"}
            exec(compile(source, str(PATH), "exec"), namespace)
        self.assertIn("derive_bytes", namespace)


if __name__ == "__main__":
    unittest.main()
