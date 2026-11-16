"""Captura Valkey como datos; sin Docker, SSH, SDK o credenciales reales."""

import base64
import copy
import gzip
import hashlib
import io
import json
import os
import struct
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "docker/valkey" / (name + ".py")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), vars(module))
    return module


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def oci_fixture(*, duplicate=False, link=False, wrong_diff=False):
    layer = b"fixture-tar-layer"
    blob = gzip.compress(layer, mtime=0)
    row = {
        "digest": "sha256:" + sha(blob),
        "size": len(blob),
        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
    }
    config = canonical(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {
                "type": "layers",
                "diff_ids": ["sha256:" + ("0" * 64 if wrong_diff else sha(layer))],
            },
        }
    )
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "digest": "sha256:" + sha(config),
            "size": len(config),
            "mediaType": "application/vnd.oci.image.config.v1+json",
        },
        "layers": [row],
    }
    raw_manifest = canonical(manifest)
    index = canonical(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "digest": "sha256:" + sha(raw_manifest),
                    "size": len(raw_manifest),
                    "mediaType": manifest["mediaType"],
                }
            ],
        }
    )
    files = {
        "oci-layout": canonical({"imageLayoutVersion": "1.0.0"}),
        "index.json": index,
        "blobs/sha256/" + sha(raw_manifest): raw_manifest,
        "blobs/sha256/" + sha(config): config,
        "blobs/sha256/" + sha(blob): blob,
    }
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:") as tar:
        for name, raw in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
        if duplicate:
            item = tarfile.TarInfo("index.json")
            item.size = len(index)
            tar.addfile(item, io.BytesIO(index))
        if link:
            item = tarfile.TarInfo("other")
            item.type = tarfile.SYMTYPE
            item.linkname = "/etc/passwd"
            tar.addfile(item)
    expected = {"platform_digest": "sha256:" + sha(raw_manifest), "manifest": manifest}
    return stream.getvalue(), expected


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.remote = load("capture_image_remote_v2")

    def load_local_proposal(self):
        local = load("capture_image_v2")
        project = Path(self.enterContext(tempfile.TemporaryDirectory()))
        candidate = canonical(
            {
                "image": self.remote.IMAGE,
                "platform_digest": self.remote.MANIFEST,
                "manifest": {"config": {"digest": self.remote.CONFIG}},
            }
        )
        updater = b"# Actualizador de ensayo.\n"
        files = {
            ".tools/evidence/phase4-valkey-alpine-candidate.json": candidate,
            ".tools/vault_updates.py": updater,
            "docker/build_component_v2.py": (ROOT / "docker/build_component_v2.py").read_bytes(),
            "docker/host_layout.py": (ROOT / "docker/host_layout.py").read_bytes(),
            "sre/backup_tools_access.py": (ROOT / "sre/backup_tools_access.py").read_bytes(),
            "scripts/ci/check_env.py": (ROOT / "scripts/ci/check_env.py").read_bytes(),
        }
        for name, contents in files.items():
            target = project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        local.PROJECT = project
        local.CANDIDATE_SHA = sha(candidate)
        local.UPDATER_SHA = sha(updater)
        return local

    def test_envelope_oci_verifica_manifiesto_config_blobs_y_diffids_sin_extraer(self):
        raw, expected = oci_fixture()
        with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("extracción")):
            result = self.remote.validate_oci(io.BytesIO(raw), expected)
        self.assertEqual(result["manifest_digest"], expected["platform_digest"])
        self.assertEqual(result["config_digest"], expected["manifest"]["config"]["digest"])
        self.assertEqual(result["layers_verified"], 1)
        self.assertFalse(result["binary_accepted"])

    def test_oci_rechaza_duplicados_enlaces_hash_falso_y_diffid_falso(self):
        for options in ({"duplicate": True}, {"link": True}, {"wrong_diff": True}):
            raw, expected = oci_fixture(**options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.remote.validate_oci(io.BytesIO(raw), expected)
        raw, expected = oci_fixture()
        expected["platform_digest"] = "sha256:" + "f" * 64
        with self.assertRaises(ValueError):
            self.remote.validate_oci(io.BytesIO(raw), expected)

    def test_solo_inspect_pull_y_save_del_digest_literal(self):
        for mode in ("inspect", "pull", "save"):
            argv = self.remote.image_command(mode, "/private/image.tar")
            self.assertEqual(argv[-1], self.remote.IMAGE)
        for mode in ("run", "create", "build", "rm", "prune"):
            with self.assertRaises(ValueError):
                self.remote.image_command(mode, "/private/image.tar")

    def test_no_confunde_id_manifest_con_id_config(self):
        value = {
            "Id": self.remote.MANIFEST,
            "Architecture": "amd64",
            "Os": "linux",
            "RepoDigests": [self.remote.IMAGE.removeprefix("docker.io/")],
        }
        self.remote.validate_inspect([value])
        self.remote.validate_inspect([{**value, "Id": self.remote.CONFIG}])
        for changed in (
            {"Id": "sha256:" + "f" * 64},
            {"Architecture": "arm64"},
            {"RepoDigests": []},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.remote.validate_inspect([{**value, **changed}])

    def test_solo_ausencia_inequivoca_habilita_pull(self):
        message = (
            "Error response from daemon: No such image: " + self.remote.IMAGE + "\n"
        ).encode()
        self.assertTrue(self.remote.missing_image(1, message))
        for rc, text in [
            (1, b"Cannot connect to the Docker daemon"),
            (1, b"permission denied"),
            (0, message),
            (1, message + b"another error\n"),
        ]:
            self.assertFalse(self.remote.missing_image(rc, text))

    def test_prepare_negativo_o_identidad_diferente_no_habilita_capture(self):
        value = {
            "owner": self.remote.OWNER,
            "stage": "prepare",
            "success": True,
            "profile_sha256": "a" * 64,
            "directory": {"device": 1, "inode": 2},
        }
        env = self.remote.envelope(value)
        self.remote.require_prepare(env, env, "a" * 64, value["directory"])
        for changed in [
            {**value, "success": False},
            {**value, "stage": "capture"},
            {**value, "directory": {"device": 1, "inode": 3}},
        ]:
            with self.assertRaises(ValueError):
                edited = self.remote.envelope(changed)
                self.remote.require_prepare(edited, edited, "a" * 64, value["directory"])

    def test_plan_no_lee_boveda_y_exige_autorizacion_externa_para_operar(self):
        local = load("capture_image_v2")
        with (
            patch.object(local, "proposal", return_value={"fixture": True}),
            patch.object(local, "read_values", side_effect=AssertionError("bóveda")),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(local.main(["plan"]), 0)
            self.assertEqual(local.main(["capture"]), 1)

    def test_connector_v2_hash_y_seleccion_antes_de_ejecutar(self):
        local = load("capture_image_v2")
        raw = (ROOT / "docker/build_component_v2.py").read_bytes()
        function = local.connector_from_source(raw)
        self.assertEqual(function.__name__, "connect_ssh")
        with self.assertRaises(ValueError):
            local.connector_from_source(raw + b"\n")
        with (
            patch("importlib.metadata.version", return_value="4.0.0"),
            self.assertRaises(ValueError),
        ):
            function({})

    def test_connector_rechaza_hostkey_y_no_prueba_otro_metodo(self):
        local = load("capture_image_v2")
        calls = []

        class WrongHostKey(Exception):
            pass

        class Client:
            def get_host_keys(self):
                return types.SimpleNamespace(add=lambda *_args: None)

            def set_missing_host_key_policy(self, policy):
                calls.append(type(policy).__name__)

            def connect(self, *_args, **kwargs):
                calls.append(kwargs)
                raise WrongHostKey()

            def close(self):
                calls.append("closed")

        class RejectPolicy:
            pass

        fake = types.SimpleNamespace(
            SSHClient=Client,
            RejectPolicy=RejectPolicy,
            Ed25519Key=types.SimpleNamespace(
                from_private_key=lambda _stream: types.SimpleNamespace(asbytes=lambda: b"client")
            ),
            PKey=types.SimpleNamespace(from_type_string=lambda *_args: object()),
        )
        host = base64.b64encode(b"host").decode()
        values = {
            "MEDIA_VM_IP": "192.168.0.85",
            "MEDIA_VM_SSH_USER": "jandrade",
            "MEDIA_VM_SSH_PUBLIC_KEY": "ssh-ed25519 " + base64.b64encode(b"client").decode(),
            "MEDIA_VM_SSH_HOST_PUBLIC_KEY": "ssh-ed25519 " + host,
            "MEDIA_VM_SSH_HOST_FINGERPRINT": "SHA256:"
            + base64.b64encode(hashlib.sha256(b"host").digest()).decode().rstrip("="),
            "MEDIA_VM_SSH_PRIVATE_KEY_B64": base64.b64encode(b"fixture").decode(),
        }
        with (
            patch("importlib.metadata.version", return_value="5.1.0"),
            patch.dict(sys.modules, {"paramiko": fake}),
            self.assertRaises(WrongHostKey),
        ):
            local.connector_from_source((ROOT / "docker/build_component_v2.py").read_bytes())(
                values
            )
        self.assertEqual(calls[0], "RejectPolicy")
        self.assertEqual(calls[-1], "closed")
        self.assertFalse(calls[1]["allow_agent"])
        self.assertFalse(calls[1]["look_for_keys"])

    def test_capture_no_hace_pull_ante_error_de_daemon_y_no_repite_intento(self):
        calls = []

        def runner(mode, _fd=None):
            calls.append(mode)
            return {"returncode": 1, "stdout": b"", "stderr": b"permission denied"}

        with self.assertRaises(ValueError):
            self.remote.capture_commands(runner, 123)
        self.assertEqual(calls, ["inspect"])

    def test_capture_ausente_pull_literal_reinspect_y_save(self):
        calls = []
        good = canonical(
            [
                {
                    "Id": self.remote.CONFIG,
                    "Architecture": "amd64",
                    "Os": "linux",
                    "RepoDigests": [self.remote.IMAGE],
                }
            ]
        )

        def runner(mode, _fd=None):
            calls.append(mode)
            if len(calls) == 1:
                return {
                    "returncode": 1,
                    "stdout": b"",
                    "stderr": (
                        "Error response from daemon: No such image: " + self.remote.IMAGE + "\n"
                    ).encode(),
                }
            return {"returncode": 0, "stdout": good if mode == "inspect" else b"", "stderr": b""}

        self.remote.capture_commands(runner, 123)
        self.assertEqual(calls, ["inspect", "pull", "inspect", "save"])

    def test_transferencia_truncada_exceso_hash_y_destino_previo_no_se_publican(self):
        local = load("capture_image_v2")
        raw = b"contenido verificable"
        for data, size, checksum in [
            (raw[:-1], len(raw), sha(raw)),
            (raw + b"x", len(raw), sha(raw)),
            (raw, len(raw), "0" * 64),
        ]:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "image.tar"
                with self.assertRaises(ValueError):
                    local.receive_archive(io.BytesIO(data), target, size, checksum)
                self.assertFalse(target.exists())
                self.assertTrue(target.with_suffix(".tar.partial").exists())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "image.tar"
            target.write_bytes(b"ajeno")
            with self.assertRaises(FileExistsError):
                local.receive_archive(io.BytesIO(raw), target, len(raw), sha(raw))
            self.assertEqual(target.read_bytes(), b"ajeno")

    def test_transferencia_correcta_publica_sin_reemplazo_y_con_sha(self):
        local = load("capture_image_v2")
        raw = b"captura propia"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "image.tar"
            result = local.receive_archive(io.BytesIO(raw), target, len(raw), sha(raw))
            self.assertEqual(target.read_bytes(), raw)
            self.assertEqual(result["sha256"], sha(raw))
            self.assertEqual(target.stat().st_nlink, 1)
            self.assertFalse(target.with_suffix(".tar.partial").exists())

    def test_lector_literal_8mib_no_expone_valores_ni_ejecuta_parser_antes_acl(self):
        local = load("capture_image_v2")
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.env"
            path.write_bytes(b"#" + b"x" * (2 * 1024 * 1024) + b"\nKEY=literal # comentario\n")
            result = local.read_values(
                path=path,
                acl=lambda p: calls.append("acl"),
                parser=lambda text: (calls.append("parse") or {"safe": len(text)}, []),
            )
            self.assertGreater(result["safe"], 2 * 1024 * 1024)
            self.assertEqual(calls, ["acl", "parse", "acl"])

    def test_lector_ctime_separa_apis_y_rechaza_cambio_dentro_de_cada_una(self):
        local = load("capture_image_v2")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.env"
            path.write_bytes(b"KEY=literal\n")
            info = path.lstat()
            fields = {
                key: getattr(info, key)
                for key in ("st_dev", "st_ino", "st_size", "st_mode", "st_nlink", "st_mtime_ns")
            }

            def node(ctime):
                return types.SimpleNamespace(**fields, st_ctime_ns=ctime, st_file_attributes=0)

            # Un ctime distinto entre APIs es válido; cada API debe conservar su propio valor.
            for path_times, fd_times, rejected in [
                ((10, 10), (20, 20), False),
                ((10, 10), (20, 21), True),
                ((10, 11), (20, 20), True),
            ]:
                with (
                    self.subTest(path_times=path_times, fd_times=fd_times),
                    patch.object(Path, "lstat", side_effect=[node(t) for t in path_times]),
                    patch.object(local.os, "fstat", side_effect=[node(t) for t in fd_times]),
                ):
                    if rejected:
                        with self.assertRaises(ValueError):
                            local.read_values(
                                path=path,
                                acl=lambda _p: None,
                                parser=lambda _t: ({"KEY": "literal"}, []),
                            )
                    else:
                        self.assertEqual(
                            local.read_values(
                                path=path,
                                acl=lambda _p: None,
                                parser=lambda _t: ({"KEY": "literal"}, []),
                            ),
                            {"KEY": "literal"},
                        )

    def test_cleanup_antes_primer_loop_exige_hijo_retenido_y_suspende_alarma(self):
        remote = self.remote
        for child_missing in (False, True):
            events = []

            def waitid(*args, _events=events, _child_missing=child_missing):
                _events.append(("waitid", args))
                if _child_missing:
                    raise ChildProcessError("El hijo ya no está retenido.")
                return None  # El hijo propio sigue vivo y no ha sido recolectado.

            def timer(which, seconds, interval=0, _events=events):
                _events.append(("timer", which, seconds, interval))
                return (7.0, 0.25) if seconds == 0 else (0.0, 0.0)

            def mask(how, previous, _events=events):
                _events.append(("mask", how, previous))
                return {123}

            process = types.SimpleNamespace(
                pid=4321,
                stdout=types.SimpleNamespace(
                    close=lambda _events=events: _events.append(("close", "stdout"))
                ),
                stderr=types.SimpleNamespace(
                    close=lambda _events=events: _events.append(("close", "stderr"))
                ),
                wait=lambda _events=events, **_kw: _events.append(("wait",)),
                returncode=0,
            )
            fake_os = types.SimpleNamespace(
                P_PID=1,
                WEXITED=2,
                WNOWAIT=4,
                WNOHANG=8,
                waitid=waitid,
                killpg=lambda *args, _events=events: _events.append(("killpg", args)),
            )
            fake_signal = types.SimpleNamespace(
                SIGKILL=9,
                SIGALRM=14,
                ITIMER_REAL=0,
                SIG_BLOCK=0,
                SIG_SETMASK=2,
                pthread_sigmask=mask,
                setitimer=timer,
            )
            with (
                self.subTest(child_missing=child_missing),
                patch.object(remote, "os", fake_os),
                patch.object(remote, "signal", fake_signal),
                patch.object(remote.subprocess, "Popen", return_value=process),
                patch.object(
                    remote.selectors,
                    "DefaultSelector",
                    side_effect=RuntimeError("Antes del primer loop"),
                ),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                remote.run_image("inspect", None, remote.time.monotonic() + 1)
            kinds = [event[0] for event in events]
            self.assertIn("waitid", kinds)
            self.assertIn(("close", "stdout"), events)
            self.assertIn(("close", "stderr"), events)
            self.assertLess(kinds.index("timer"), kinds.index("waitid"))
            self.assertEqual([e for e in events if e[0] == "waitid"][0][1], (1, 4321, 14))
            timers = [e for e in events if e[0] == "timer"]
            self.assertEqual(timers[0], ("timer", 0, 0, 0))
            self.assertTrue(0 < timers[-1][2] <= 7)
            self.assertEqual(timers[-1][3], 0.25)
            self.assertEqual(events[-1], ("mask", 2, {123}))
            if child_missing:
                self.assertNotIn("killpg", kinds)
                self.assertNotIn("wait", kinds)
            else:
                self.assertLess(kinds.index("waitid"), kinds.index("killpg"))
                self.assertLess(kinds.index("killpg"), kinds.index("wait"))

    def test_exit_cero_con_cualquier_stderr_no_se_acepta_ni_expone_texto(self):
        local = load("capture_image_v2")
        for previously_observed in (False, True):
            remaining = [b"diagnostico-privado"] if not previously_observed else []
            channel = types.SimpleNamespace(
                recv_stderr_ready=lambda _remaining=remaining: bool(_remaining),
                recv_stderr=lambda _size, _remaining=remaining: _remaining.pop(),
                recv_ready=lambda: False,
                eof_received=True,
                closed=False,
                exit_status_ready=lambda: True,
                recv_exit_status=lambda: 0,
            )
            stream = local.ChannelStream(channel, 1)
            if previously_observed:
                stream.stderr_bytes = 1
            with self.subTest(previously_observed=previously_observed):
                with self.assertRaises(ValueError) as caught:
                    stream.finish()
                self.assertNotIn("diagnostico-privado", str(caught.exception))
                with tempfile.TemporaryDirectory() as directory:
                    destination = Path(directory) / "image.tar"
                    raw = b"captura-sin-publicar"
                    with self.assertRaises(ValueError):
                        local.receive_archive(
                            io.BytesIO(raw),
                            destination,
                            len(raw),
                            sha(raw),
                            validate=lambda _path, _stream=stream: _stream.finish(),
                        )
                    self.assertFalse(destination.exists())
                    self.assertEqual(destination.with_suffix(".tar.partial").read_bytes(), raw)

    def test_perfil_remoto_no_admite_otro_namespace_o_imagen_ni_fuente_layout(self):
        local = self.load_local_proposal()
        plan = local.proposal()
        request = {
            "stage": "prepare",
            "profile": plan,
            "profile_sha256": sha(canonical(plan)),
            "layout": {},
            "layout_source_b64": base64.b64encode(
                (ROOT / "docker/host_layout.py").read_bytes()
            ).decode(),
            "prepare": None,
            "capture": None,
        }
        self.remote.check_request(request)
        for field, value in [("namespace", "/tmp/ajeno"), ("owner", "ajeno")]:
            edited = copy.deepcopy(request)
            edited["profile"][field] = value
            edited["profile_sha256"] = sha(canonical(edited["profile"]))
            with self.assertRaises(ValueError):
                self.remote.check_request(edited)
        edited = {
            **request,
            "layout_source_b64": base64.b64encode(b"raise AssertionError('fuente')").decode(),
        }
        with self.assertRaises(ValueError):
            self.remote.check_request(edited)

    def test_inspect_ajeno_impide_save_y_todo_pull(self):
        calls = []

        def runner(mode, _fd=None):
            calls.append(mode)
            return {
                "returncode": 0,
                "stderr": b"",
                "stdout": canonical([{"Id": "sha256:" + "f" * 64}]),
            }

        with self.assertRaises(ValueError):
            self.remote.capture_commands(runner, 123)
        self.assertEqual(calls, ["inspect"])

    def test_recibo_ajeno_y_comentarios_de_boveda_rechazados_por_contrato_real(self):
        local = self.load_local_proposal()
        plan = local.proposal()
        request = {"stage": "prepare", "profile_sha256": sha(canonical(plan)), "prepare": None}
        value = {
            "success": True,
            "stage": "prepare",
            "owner": self.remote.OWNER,
            "path": self.remote.ROOT,
            "profile_sha256": request["profile_sha256"],
            "directory": {"device": 1, "inode": 2},
            "binary_accepted": False,
            "marker_sha256": "b" * 64,
        }
        local.verify_result(vars(self.remote), self.remote.envelope(value), request)
        with self.assertRaises(ValueError):
            local.verify_result(
                vars(self.remote), self.remote.envelope({**value, "owner": "ajeno"}), request
            )
        checker = local.load_pinned(ROOT / "scripts/ci/check_env.py", local.PARSER_SHA)
        content = "\n".join(local.COMMENTS) + "\nFIXTURE=CHANGE_ME\n"
        self.assertEqual(checker["validate_comments"](content), [])
        self.assertEqual(checker["parse_env"](content), ({"FIXTURE": "CHANGE_ME"}, []))

    def test_marcos_truncados_exceso_final_y_estado_remoto_negativo(self):
        local = load("capture_image_v2")

        class Channel:
            closed = False

            def __init__(self, raw, rc=0):
                self.raw, self.rc = bytearray(raw), rc

            @property
            def eof_received(self):
                return not self.raw

            def recv_stderr_ready(self):
                return False

            def recv_ready(self):
                return bool(self.raw)

            def recv(self, size):
                raw = bytes(self.raw[: min(size, 2)])
                del self.raw[: len(raw)]
                return raw

            def send_ready(self):
                return False

            def exit_status_ready(self):
                return True

            def recv_exit_status(self):
                return self.rc

        for raw in (b"\0\0", struct.pack(">I", 65537), struct.pack(">I", 2) + b"{"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                local.ChannelStream(Channel(raw), 1).frame()
        with self.assertRaises(ValueError):
            local.ChannelStream(Channel(b"extra"), 1).finish()
        with self.assertRaises(ValueError):
            local.ChannelStream(Channel(b"", 1), 1).finish()

    def test_pins_externos_distintos_impiden_operar_y_plan_incluye_hash_reproducible(self):
        local = self.load_local_proposal()
        plan = local.proposal()
        with self.assertRaises(ValueError):
            local.check_values({}, plan, sha(canonical(plan)))
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(local.main(["plan"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["profile_sha256"], sha(canonical(result["profile"])))
        (local.PROJECT / ".tools/vault_updates.py").write_bytes(b"# Actualizador distinto.\n")
        with self.assertRaisesRegex(ValueError, "Un soporte del plan fue alterado"):
            local.proposal()

    def test_no_confunde_archivo_sustituido_en_publicacion_con_captura_propia(self):
        local = load("capture_image_v2")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "image.tar"
            partial = target.with_suffix(".tar.partial")
            foreign = Path(directory) / "foreign"
            foreign.write_bytes(b"ajeno")
            link = os.link

            def swap(source, destination):
                Path(source).unlink()
                link(foreign, source)
                link(source, destination)

            with patch.object(local.os, "link", side_effect=swap), self.assertRaises(ValueError):
                local.receive_archive(io.BytesIO(b"propio"), target, 6, sha(b"propio"))
            self.assertTrue(foreign.exists())
            self.assertTrue(partial.exists())

    @unittest.skipUnless(os.name == "posix", "openat/NoFollow requiere Linux real")
    def test_namespace_exclusivo_y_symlink_no_se_adopta(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.remote.create_namespace(parent, "own", "a" * 64, uid=os.getuid())
                with self.assertRaises(FileExistsError):
                    self.remote.create_namespace(parent, "own", "a" * 64, uid=os.getuid())
                os.symlink("own", Path(directory) / "other")
                with self.assertRaises(OSError):
                    self.remote.open_owned(parent, "other", "a" * 64, uid=os.getuid())
            finally:
                os.close(parent)

    def test_v2_error_real_normalizado_y_literal_del_mismo_digest(self):
        for reference in (self.remote.IMAGE, self.remote.IMAGE.removeprefix("docker.io/")):
            message = ("Error response from daemon: No such image: " + reference + "\n").encode()
            self.assertTrue(self.remote.missing_image(1, message))
        normalized = (
            "Error response from daemon: No such image: "
            + self.remote.IMAGE.removeprefix("docker.io/")
            + "\n"
        ).encode()
        for code, message in [
            (0, normalized),
            (2, normalized),
            (-1, normalized),
            (True, normalized),
            (1, normalized[:-1]),
            (1, normalized.replace(b"\n", b"\r\n")),
            (1, b"warning\n" + normalized),
            (1, normalized + b"permission denied\n"),
            (1, normalized + normalized),
            (1, normalized.replace(b"de6fbe96", b"00000000")),
            (1, normalized.replace(b"valkey/valkey@", b"other/valkey@")),
            (1, b"permission denied"),
            (1, b"Cannot connect to the Docker daemon"),
            (1, normalized.decode()),
        ]:
            with self.subTest(code=code, message=message):
                self.assertFalse(self.remote.missing_image(code, message))

    def test_v2_ausencia_normalizada_habilita_solo_pull_literal_y_reinspect(self):
        calls = []
        good = canonical(
            [
                {
                    "Id": self.remote.MANIFEST,
                    "Architecture": "amd64",
                    "Os": "linux",
                    "RepoDigests": [self.remote.IMAGE],
                }
            ]
        )

        def runner(mode, _fd=None):
            calls.append(mode)
            if len(calls) == 1:
                return {
                    "returncode": 1,
                    "stdout": b"[]\n",
                    "stderr": (
                        "Error response from daemon: No such image: "
                        + self.remote.IMAGE.removeprefix("docker.io/")
                        + "\n"
                    ).encode(),
                }
            return {"returncode": 0, "stdout": good if mode == "inspect" else b"", "stderr": b""}

        self.remote.capture_commands(runner, 123)
        self.assertEqual(calls, ["inspect", "pull", "inspect", "save"])

    def test_v2_namespace_y_destino_no_adoptan_v1(self):
        local = load("capture_image_v2")
        self.assertTrue(local.NAMESPACE.endswith("-v2"))
        self.assertTrue(local.OWNER.endswith(":v2"))
        self.assertEqual(local.PREFIX, "MEDIA_VALKEY_OCI_CAPTURE_V2_")
        self.assertTrue(local.DESTINATION.endswith("-v2.tar"))
        self.assertNotIn("capture_image_remote.py", local.BOOTSTRAP)


if __name__ == "__main__":
    unittest.main()
