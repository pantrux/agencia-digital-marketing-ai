"""Fixtures locales; nunca leen la bóveda real, usan R2 o registran tareas."""

import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as etree
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


queue_module = load("vault_queue_tests", ROOT / "sre/vault_change_queue.py")
updater = load("vault_updates_v2_tests", ROOT / "sre/vault_updates_v2.py")
worker = load("vault_change_worker_tests", ROOT / "sre/vault_change_worker.py")
parser = load("vault_real_parser_tests", ROOT / "scripts/ci/check_env.py")


class RecoveryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.repo = "a" * 64
        self.password = "credencial-sintetica-del-repositorio"
        self.marker = b"marcador-de-ensayo"
        self.prefix = "windows-vault-v1/3b88136b-c5d6-4d4b-bcf4-736bd9a8fa79/"
        self.client = SimpleNamespace(
            location={"prefix": self.prefix},
            verify=lambda observed: self.assertEqual(observed, self.marker),
            _request=self.request,
        )

    def request(self, method, key="", query=None, body=b"", conditional=False):
        if query:
            self.assertEqual(method, "GET")
            self.assertEqual(query["max-keys"], "1")
            prefix = query["prefix"]
            keys = sorted(key for key in self.objects if key.startswith(prefix))
            document = etree.Element("ListBucketResult")
            etree.SubElement(document, "Prefix").text = prefix
            etree.SubElement(document, "KeyCount").text = str(min(1, len(keys)))
            if keys:
                item = etree.SubElement(document, "Contents")
                etree.SubElement(item, "Key").text = keys[0]
            return etree.tostring(document)
        if method == "PUT":
            self.assertTrue(conditional)
            if key in self.objects:
                raise ValueError("CONFLICTO_DE_CREACION")
            self.objects[key] = body
            return b""
        self.assertEqual(method, "GET")
        return self.objects[key]

    def record(self, generation, prepared_ns, snapshot="c"):
        return {
            "repository_id": self.repo,
            "generation_id": generation * 32,
            "prepared_ns": prepared_ns,
            "snapshot_id": snapshot * 64,
            "sha256": "f" * 64,
            "bytes": 1234,
            "publication_status": "OBSERVED",
        }

    def publish(self, record):
        return worker.publish_recovery_record(self.client, self.marker, self.password, record)

    def latest(self):
        return worker.latest_recovery_record(self.client, self.marker, self.password, self.repo)

    def test_recupera_la_captura_mas_reciente_si_la_antigua_se_sube_al_final(self):
        old = self.record("e", 101)
        newest = self.record("b", 102)
        tie = self.record("1", 102)
        self.publish(newest)
        self.publish(tie)
        self.publish(old)
        self.assertEqual(self.latest(), newest)

    def test_repite_la_misma_publicacion_y_rechaza_un_snapshot_distinto(self):
        record = self.record("b", 102)
        self.publish(record)
        self.publish(record)
        self.assertEqual(len(self.objects), 1)
        with self.assertRaisesRegex(ValueError, "RECOVERY_RECORD_CONFLICT"):
            self.publish({**record, "snapshot_id": "d" * 64})
        self.assertEqual(self.latest(), record)

    def test_rechaza_un_acuse_valido_movido_a_la_clave_de_otra_captura(self):
        self.publish(self.record("b", 102))
        original = next(iter(self.objects.values()))
        self.publish(self.record("c", 103))
        newest = min(self.objects)
        self.objects[newest] = original
        with self.assertRaisesRegex(ValueError, "RECOVERY_RECORD_KEY"):
            self.latest()

    def test_rechaza_un_snapshot_preparado_y_un_contenido_alterado(self):
        record = self.record("b", 102)
        with self.assertRaisesRegex(ValueError, "RECOVERY_RECORD_FIELDS"):
            self.publish({**record, "publication_status": "PREPARED"})
        self.publish(record)
        key = next(iter(self.objects))
        self.objects[key] = self.objects[key].replace(b'"bytes":1234', b'"bytes":1235')
        with self.assertRaisesRegex(ValueError, "RECOVERY_RECORD_HMAC"):
            self.latest()


def documentation(key):
    return [
        f"# Para qué: Valor {key}.",
        "# Dónde: Fixture local.",
        "# Riesgo al rotar: Coordinar consumidores.",
    ]


def vault(value="uno"):
    return ("\n".join([*documentation("TEST_VALUE"), "TEST_VALUE=" + value, ""]) + "\n").encode()


class FixtureAccess:
    """Comprueba tipos en fixtures, sin afirmar aceptación de ACL Windows."""

    def check(self, path):
        if pathlib.Path(path).is_symlink():
            raise ValueError("Enlace en fixture")


def read_fixture(path, maximum=8 * 1024**2):
    path = pathlib.Path(path)
    if path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_size > maximum:
        raise ValueError("Fixture irregular")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    info = path.stat()
    return raw, {
        "device": info.st_dev,
        "inode": info.st_ino,
        "bytes": len(raw),
        "mtime_ns": info.st_mtime_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class FixtureStore:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.access = FixtureAccess()
        for name in ("captures", "operations", "tmp", "profile", "cache"):
            (self.root / name).mkdir()

    def _path(self, name):
        path = self.root / name
        if ".." in pathlib.PurePosixPath(name).parts or pathlib.Path(name).is_absolute():
            raise ValueError("Fuera de fixture")
        return path

    def write(self, name, raw):
        with self._path(name).open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def read(self, name, maximum=1024**2):
        return read_fixture(self._path(name), maximum)[0]

    def json(self, name):
        return json.loads(self.read(name))

    def mkdir(self, name):
        self._path(name).mkdir()

    @contextmanager
    def lock(self, operation):
        path = self.root / ".active.json"
        with path.open("xb") as stream:
            stream.write(operation.encode())
        try:
            yield
        finally:
            path.unlink()


class VaultChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        (self.root / "queue").mkdir()
        self.store = FixtureStore(self.root / "queue")
        self.queue = queue_module.Queue(
            self.store, read_fixture, parser.parse_env, parser.validate_comments
        )
        self.source = self.root / "fixture.env"
        self.source.write_bytes(vault())
        self.template = self.root / "fixture.env.example"
        self.template.write_bytes(b"TEST_VALUE=CHANGE_ME\n")
        self.publish_parent = self.root / "private"
        self.publish_parent.mkdir()

    def publish(self, value="dos", replace=os.replace):
        return updater.publish_update(
            self.queue,
            self.source,
            self.template,
            self.publish_parent,
            {"TEST_VALUE": value},
            documentation=documentation,
            replace=replace,
        )

    def test_recupera_los_bloqueos_previos_sin_reintentar_una_subida_inconclusa(self):
        pending = self.queue.observe(self.source)
        self.queue.begin_attempt(pending["generation_id"], {"publication_status": "OBSERVED"})
        active = self.store.root / ".active.json"
        uploader = self.store.root / "operations/uploader.lock.json"
        marks = {
            active: {"owner": queue_module.OWNER, "nonce": "1" * 32, "operation": "select-upload"},
            uploader: {"owner": queue_module.OWNER, "nonce": "2" * 32},
        }
        for path, mark in marks.items():
            path.write_bytes(queue_module.encoded(mark))
        cutoff = time.time_ns() + 1_000_000_000
        self.assertEqual(self.queue.archive_previous_boot_locks(cutoff), 2)
        self.assertEqual(self.queue.archive_previous_boot_locks(cutoff), 0)
        retained = list((self.store.root / "operations").glob("recovered-boot-lock-*.json"))
        self.assertEqual(len(retained), 2)
        self.assertEqual(
            {path.read_bytes() for path in retained},
            {queue_module.encoded(mark) for mark in marks.values()},
        )
        with self.assertRaisesRegex(ValueError, "UPLOAD_RECONCILIATION_REQUIRED"):
            self.queue.next_pending()

    def test_preserva_bloqueos_del_arranque_actual_y_rechaza_marcas_ajenas(self):
        active = self.store.root / ".active.json"
        uploader = self.store.root / "operations/uploader.lock.json"
        mark = {"owner": queue_module.OWNER, "nonce": "1" * 32, "operation": "select-upload"}
        active.write_bytes(queue_module.encoded(mark))
        cutoff = time.time_ns() + 1_000_000_000
        os.utime(active, ns=(cutoff, cutoff))
        self.assertEqual(self.queue.archive_previous_boot_locks(cutoff), 0)
        self.assertEqual(active.read_bytes(), queue_module.encoded(mark))
        uploader.write_bytes(queue_module.encoded({"owner": "ajeno", "nonce": "2" * 32}))
        with self.assertRaisesRegex(ValueError, "BOOT_LOCK_IDENTITY"):
            self.queue.archive_previous_boot_locks(cutoff)
        self.assertTrue(active.is_file())
        self.assertTrue(uploader.is_file())

    def test_candidate_and_prepared_are_durable_before_replace(self):
        def replace(source, target):
            if target == self.source:
                generations = self.queue.generations()
                self.assertEqual(len(generations), 1)
                row = generations[0]
                self.assertEqual(row["status"], "PREPARED")
                self.assertEqual(
                    self.queue.candidate(row["generation_id"])[0],
                    vault("dos").rstrip(b"\n") + b"\n",
                )
            os.replace(source, target)

        result = self.publish(replace=replace)
        self.assertTrue(result["committed"])
        self.assertEqual(self.queue.generations()[0]["status"], "COMMITTED")
        self.assertEqual(
            parser.parse_env(self.source.read_text(encoding="utf8"))[0]["TEST_VALUE"], "dos"
        )

    def test_crash_after_replace_keeps_valid_prepared_candidate(self):
        class Crash(BaseException):
            pass

        def replace(source, target):
            os.replace(source, target)
            if target == self.source:
                raise Crash()

        with self.assertRaises(Crash):
            self.publish(replace=replace)
        row = self.queue.generations()[0]
        self.assertEqual(row["status"], "PREPARED")
        self.assertEqual(self.queue.candidate(row["generation_id"])[0], self.source.read_bytes())

    def test_changed_candidate_is_never_uploaded(self):
        with self.store.lock("fixture"):
            row = self.queue.enqueue(vault(), "OBSERVED")
        path = self.store._path("captures/" + row["generation_id"] + "/candidate.env")
        path.write_bytes(vault("ajeno"))
        with self.assertRaises(ValueError):
            self.queue.candidate(row["generation_id"])

    def test_invalid_partial_manual_write_is_not_queued(self):
        self.source.write_bytes(b'TEST_VALUE="sin cierre\n')
        with self.assertRaises(ValueError):
            self.queue.observe(self.source)
        self.assertEqual(self.queue.generations(), [])

    def test_startup_capture_and_consecutive_hash_dedup(self):
        first = self.queue.observe(self.source)
        self.assertIsNotNone(first)
        self.assertIsNone(self.queue.observe(self.source))
        self.source.write_bytes(vault("dos"))
        self.assertIsNotNone(self.queue.observe(self.source))
        self.source.write_bytes(vault())
        self.assertIsNotNone(self.queue.observe(self.source))
        self.assertEqual(len(self.queue.generations()), 3)

    def test_el_observador_omite_escaneos_inactivos_y_captura_cambios_sin_esperar(self):
        with mock.patch.object(self.queue, "generations", wraps=self.queue.generations) as scans:
            self.assertIsNotNone(self.queue.observe(self.source, idle_cache=True))
            self.assertIsNone(self.queue.observe(self.source, idle_cache=True))
            scans.reset_mock()
            self.assertIsNone(self.queue.observe(self.source, idle_cache=True))
            scans.assert_not_called()
            self.source.write_bytes(vault("dos"))
            self.assertIsNotNone(self.queue.observe(self.source, idle_cache=True))
            self.source.write_bytes(vault())
            self.assertIsNotNone(self.queue.observe(self.source, idle_cache=True))
        self.assertEqual(len(self.queue.generations()), 3)

    def test_el_observador_detecta_en_cola_un_escritor_externo_con_el_mismo_archivo(self):
        self.queue.observe(self.source, idle_cache=True)
        self.assertIsNone(self.queue.observe(self.source, idle_cache=True))
        with self.store.lock("fixture"):
            self.queue.enqueue(vault("futuro"), "ROOT_CANDIDATE")
        self.assertIsNotNone(self.queue.observe(self.source, idle_cache=True))
        self.assertEqual(len(self.queue.generations()), 3)

    def test_el_cargador_reutiliza_la_cola_vacia_y_advierte_una_captura_externa(self):
        with mock.patch.object(self.queue, "generations", wraps=self.queue.generations) as scans:
            self.assertIsNone(self.queue.next_pending(idle_cache=True))
            self.assertIsNone(self.queue.next_pending(idle_cache=True))
            self.assertEqual(scans.call_count, 1)
        with self.store.lock("fixture"):
            expected = self.queue.enqueue(vault("futuro"), "ROOT_CANDIDATE")
        self.assertEqual(
            self.queue.next_pending(idle_cache=True)["generation_id"], expected["generation_id"]
        )

    def test_el_cargador_revalida_acuses_en_la_siguiente_auditoria_inactiva(self):
        row = self.queue.observe(self.source)
        self.queue.record(
            row["generation_id"],
            "uploaded.json",
            {
                "generation_id": row["generation_id"],
                "sha256": row["sha256"],
                "success": True,
                "snapshot_id": "2" * 64,
            },
        )
        with mock.patch.object(queue_module.time, "monotonic", return_value=0) as clock:
            self.assertIsNone(self.queue.next_pending(idle_cache=True))
            path = self.store._path(self.queue.path(row["generation_id"], "uploaded.json"))
            path.write_text("{}", encoding="ascii")
            clock.return_value = 11
            with self.assertRaisesRegex(ValueError, "UPLOAD_RECEIPT"):
                self.queue.next_pending(idle_cache=True)

    def test_prepared_can_upload_without_claiming_publication(self):
        with self.store.lock("fixture"):
            row = self.queue.enqueue(vault("futuro"), "ROOT_CANDIDATE")
        pending = self.queue.next_pending()
        self.assertEqual(pending["generation_id"], row["generation_id"])
        self.assertEqual(pending["status"], "PREPARED")
        self.assertFalse(pending["published"])

    def test_existing_attempt_blocks_replay(self):
        row = self.queue.observe(self.source)
        self.queue.begin_attempt(row["generation_id"], {"repository_id": "1" * 64})
        with self.assertRaises(ValueError):
            self.queue.next_pending()
        with self.assertRaises((ValueError, FileExistsError)):
            self.queue.begin_attempt(row["generation_id"], {})

    def test_render_preserves_unrelated_values_and_comment_contract(self):
        before = (
            vault() + ("\n".join([*documentation("OTHER"), "OTHER=conservar", ""]) + "\n").encode()
        )
        candidate, template, values = updater.render_update(
            before,
            {"TEST_VALUE": " valor # literal "},
            parser.parse_env,
            parser.validate_comments,
            documentation,
        )
        self.assertEqual(values["OTHER"], "conservar")
        self.assertEqual(values["TEST_VALUE"], " valor # literal ")
        self.assertNotIn(b"conservar", template)
        self.assertIn(b"OTHER=CHANGE_ME", template)
        self.assertIn(b"TEST_VALUE", candidate)

    def test_quota_failure_prevents_publication(self):
        self.queue.maximum_bytes = 1
        before = self.source.read_bytes()
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.source.read_bytes(), before)

    def test_failing_commit_marker_does_not_erase_candidate(self):
        original = self.store.write

        def write(name, raw):
            if name.endswith("/committed.json"):
                raise OSError("Fallo simulado")
            return original(name, raw)

        self.store.write = write
        with self.assertRaises(OSError):
            self.publish()
        self.assertEqual(self.queue.generations()[0]["status"], "PREPARED")
        self.assertEqual(
            self.queue.candidate(self.queue.generations()[0]["generation_id"])[0],
            self.source.read_bytes(),
        )

    def test_plan_does_not_construct_native_access_or_touch_vault(self):
        plan = worker.proposal()
        self.assertFalse(plan["ready"])
        self.assertFalse(plan["task_registered"])
        self.assertFalse(plan["network_used"])
        self.assertFalse(plan["vault_written"])

    def test_upload_arguments_are_closed_and_no_secret_in_argv(self):
        args = worker.backup_arguments("C:\\private\\candidate.env", "host", "a" * 32, "PREPARED")
        self.assertEqual(args[0], "backup")
        self.assertNotIn("init", args)
        self.assertIn("PREPARED", args)
        for forbidden in ("unlock", "forget", "prune", "--password-command"):
            self.assertNotIn(forbidden, args)
        self.assertEqual(worker.JOB_OUTPUT_LIMIT, 1024**2)

    def test_outer_job_cleanup_is_required_before_acknowledging_snapshot(self):
        self.queue.observe(self.source)
        row = self.queue.next_pending()
        self.queue.begin_attempt(
            row["generation_id"], {"repository_id": "1" * 64, "publication_status": row["status"]}
        )
        payload = {
            "success": True,
            "generation_id": row["generation_id"],
            "sha256": row["sha256"],
            "snapshot_id": "2" * 64,
            "publication_status": row["status"],
        }
        result = SimpleNamespace(
            status="completed",
            exit_code=0,
            cleanup_complete=False,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        )
        with self.assertRaises(ValueError):
            worker.accept_upload_result(self.queue, row["generation_id"], result)
        self.assertFalse(
            self.store._path(self.queue.path(row["generation_id"], "uploaded.json")).exists()
        )
        result.cleanup_complete = True
        worker.accept_upload_result(self.queue, row["generation_id"], result)
        self.assertIsNone(self.queue.next_pending())

    def test_wrong_snapshot_subject_never_acknowledges_upload(self):
        self.queue.observe(self.source)
        row = self.queue.next_pending()
        self.queue.begin_attempt(row["generation_id"], {"publication_status": row["status"]})
        payload = {
            "success": True,
            "generation_id": row["generation_id"],
            "sha256": "0" * 64,
            "snapshot_id": "2" * 64,
            "publication_status": row["status"],
        }
        result = SimpleNamespace(
            status="completed",
            exit_code=0,
            cleanup_complete=True,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        )
        with self.assertRaises(ValueError):
            worker.accept_upload_result(self.queue, row["generation_id"], result)

    def test_upload_ack_does_not_contend_with_active_observation(self):
        row = self.queue.observe(self.source)
        generation = row["generation_id"]
        self.queue.begin_attempt(generation, {"publication_status": "OBSERVED"})
        payload = {
            "success": True,
            "generation_id": generation,
            "sha256": row["sha256"],
            "snapshot_id": "2" * 64,
            "publication_status": "OBSERVED",
        }
        result = SimpleNamespace(
            status="completed",
            exit_code=0,
            cleanup_complete=True,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        )
        with self.queue.uploader_lock(), self.store.lock("observe"):
            self.assertTrue(worker.accept_upload_result(self.queue, generation, result)["uploaded"])
            name = self.queue.path(generation, "uploaded.json")
            original = self.store.read(name)
            ack = json.loads(original)
            self.assertEqual(ack["snapshot_id"], payload["snapshot_id"])
            self.assertFalse(ack["publication_proved"])
            with self.assertRaises(FileExistsError):
                worker.accept_upload_result(self.queue, generation, result)
            self.assertEqual(self.store.read(name), original)
        self.assertIsNone(self.queue.next_pending())

    def test_uploader_lock_does_not_block_capture_lock(self):
        with self.queue.uploader_lock():
            self.assertIsNotNone(self.queue.observe(self.source))
            with self.assertRaises(FileExistsError):
                with self.queue.uploader_lock():
                    self.fail("No debe haber dos uploaders")

    def test_captura_nueva_valida_la_cola_una_vez(self):
        self.assertIsNotNone(self.queue.observe(self.source))
        self.source.write_bytes(vault("segunda"))
        with mock.patch.object(self.queue, "generations", wraps=self.queue.generations) as scan:
            result = self.queue.observe(self.source)
        self.assertIsNotNone(result)
        self.assertEqual(scan.call_count, 1)

    def test_prioridad_a_la_captura_publicada_sin_saltar_intentos_inconclusos(self):
        oldest = self.queue.observe(self.source)
        self.source.write_bytes(vault("observada"))
        newest = self.queue.observe(self.source)
        prepared = self.queue.enqueue(vault("preparada"), "ROOT_CANDIDATE")
        self.assertEqual(self.queue.next_pending()["generation_id"], oldest["generation_id"])
        self.assertEqual(
            self.queue.next_pending(prefer_latest=True)["generation_id"], newest["generation_id"]
        )
        self.queue.begin_attempt(prepared["generation_id"], {"publication_status": "PREPARED"})
        with self.assertRaisesRegex(ValueError, "UPLOAD_RECONCILIATION_REQUIRED"):
            self.queue.next_pending(prefer_latest=True)

    def test_observador_alterna_capturas_vigentes_y_antiguas_y_reinicia_al_vaciarse(self):
        stop = mock.Mock()
        stop.wait.side_effect = [None, None, None, KeyboardInterrupt]
        results = [1, 2, 0, 1]
        with (
            mock.patch.object(worker.threading, "Thread"),
            mock.patch.object(worker.threading, "Event", return_value=stop),
            mock.patch.object(worker, "report_state"),
            mock.patch.object(worker, "run_batch", side_effect=results) as run,
            self.assertRaises(KeyboardInterrupt),
        ):
            worker.watch(object())
        self.assertEqual(
            [call.kwargs["prefer_latest"] for call in run.call_args_list],
            [True, False, False, True],
        )

    def test_tres_subidas_conservan_los_dos_acuses_validos_si_la_tercera_falla(self):
        oldest = self.queue.observe(self.source)
        self.source.write_bytes(vault("segunda"))
        middle = self.queue.observe(self.source)
        self.source.write_bytes(vault("tercera"))
        penultimate = self.queue.observe(self.source)
        self.source.write_bytes(vault("cuarta"))
        newest = self.queue.observe(self.source)
        subjects = {row["generation_id"]: row for row in (oldest, penultimate, newest)}
        together = threading.Barrier(3, timeout=5)
        failure_observed = threading.Event()
        reservation = threading.Lock()
        wait_for_futures = worker.wait

        def wait_for_failure(futures, **options):
            result = wait_for_futures(futures, **options)
            if any(future.exception() is not None for future in result.done):
                failure_observed.set()
            return result

        def run_job(_executable, arguments, **_options):
            generation = arguments[arguments.index("--generation") + 1]
            row = subjects[generation]
            self.assertTrue(
                (self.store.root / self.queue.path(generation, "attempt.json")).is_file()
            )
            together.wait()
            if row == newest:
                return SimpleNamespace(
                    status="completed", exit_code=1, cleanup_complete=True, stdout=b"", stderr=b""
                )
            self.assertTrue(failure_observed.wait(5))
            return SimpleNamespace(
                status="completed",
                exit_code=0,
                cleanup_complete=True,
                stderr=b"",
                stdout=worker.encoded(
                    {
                        "success": True,
                        "generation_id": generation,
                        "sha256": row["sha256"],
                        "snapshot_id": "2" * 64,
                        "publication_status": "OBSERVED",
                    }
                ),
            )

        runtime = SimpleNamespace(
            queue=self.queue,
            store=self.store,
            config={
                "repository_id": "1" * 64,
                "python": {"path": "python.exe", "sha256": "4" * 64},
            },
            config_path="fixture.json",
            config_sha256="3" * 64,
            access=SimpleNamespace(integrity=lambda _path: None),
            job=lambda: SimpleNamespace(Limits=lambda **options: options, run_job=run_job),
        )
        with (
            mock.patch.object(queue_module, "read_pinned"),
            mock.patch.object(worker, "wait", side_effect=wait_for_failure),
        ):
            with self.assertRaisesRegex(ValueError, "OUTER_JOB_REJECTED"):
                worker.run_batch(runtime, reservation_lock=reservation, prefer_latest=True)
            for row in (oldest, penultimate):
                self.assertTrue(
                    self.store.json(self.queue.path(row["generation_id"], "uploaded.json"))[
                        "success"
                    ]
                )
            self.assertFalse(
                (
                    self.store.root / self.queue.path(newest["generation_id"], "uploaded.json")
                ).exists()
            )
            with self.assertRaisesRegex(ValueError, "UPLOAD_RECONCILIATION_REQUIRED"):
                worker.run_batch(runtime, reservation_lock=reservation, prefer_latest=True)
        self.assertFalse(
            (self.store.root / self.queue.path(middle["generation_id"], "attempt.json")).exists()
        )

    def test_lote_reserva_captura_nueva_mientras_la_primera_sigue_subiendo(self):
        first = self.queue.observe(self.source)
        second_started = threading.Event()
        reservation = threading.Lock()

        def run_job(_executable, arguments, **_options):
            generation = arguments[arguments.index("--generation") + 1]
            if generation == first["generation_id"]:
                with reservation:
                    self.source.write_bytes(vault("capturada-durante-la-subida"))
                    self.assertIsNotNone(self.queue.observe(self.source))
                self.assertTrue(second_started.wait(5), "El segundo espacio quedó ocioso")
            else:
                second_started.set()
            row = self.queue.metadata(generation)
            return SimpleNamespace(
                status="completed",
                exit_code=0,
                cleanup_complete=True,
                stderr=b"",
                stdout=worker.encoded(
                    {
                        "success": True,
                        "generation_id": generation,
                        "sha256": row["sha256"],
                        "snapshot_id": "2" * 64,
                        "publication_status": "OBSERVED",
                    }
                ),
            )

        runtime = SimpleNamespace(
            queue=self.queue,
            store=self.store,
            config={
                "repository_id": "1" * 64,
                "python": {"path": "python.exe", "sha256": "4" * 64},
            },
            config_path="fixture.json",
            config_sha256="3" * 64,
            access=SimpleNamespace(integrity=lambda _path: None),
            job=lambda: SimpleNamespace(Limits=lambda **options: options, run_job=run_job),
        )
        with mock.patch.object(queue_module, "read_pinned"):
            self.assertEqual(
                worker.run_batch(runtime, reservation_lock=reservation, prefer_latest=True), 2
            )
        self.assertIsNone(self.queue.next_pending())

    def test_selector_verifica_el_intento_activo_antes_de_reservar_otra_captura(self):
        first = self.queue.observe(self.source)
        self.source.write_bytes(vault("siguiente"))
        second = self.queue.observe(self.source)
        generation = first["generation_id"]
        attempt = self.queue.begin_attempt(generation, {"publication_status": "OBSERVED"})
        picked = self.queue.pending_uploads(limit=1, active_attempts={generation: attempt})
        self.assertEqual([row["generation_id"] for row in picked], [second["generation_id"]])
        different_attempt = {**attempt, "child_nonce": "1" * 32}
        with self.assertRaisesRegex(ValueError, "UPLOAD_RECONCILIATION_REQUIRED"):
            self.queue.pending_uploads(limit=1, active_attempts={generation: different_attempt})

    def test_reservation_is_serialized_with_capture_and_releases_before_upload(self):
        row = self.queue.observe(self.source)
        reservation = threading.Lock()
        original_next_pending = self.queue.next_pending
        original_begin_attempt = self.queue.begin_attempt

        def next_pending(*, idle_cache, prefer_latest=False):
            self.assertFalse(idle_cache)
            self.assertFalse(prefer_latest)
            self.assertTrue(reservation.locked())
            return original_next_pending()

        def begin_attempt(generation, subject):
            self.assertTrue(reservation.locked())
            return original_begin_attempt(generation, subject)

        def run_job(*_args, **_kwargs):
            self.assertTrue(reservation.acquire(blocking=False))
            try:
                self.source.write_bytes(vault("segunda"))
                self.assertIsNotNone(self.queue.observe(self.source))
            finally:
                reservation.release()
            return SimpleNamespace(
                status="completed",
                exit_code=0,
                cleanup_complete=True,
                stdout=worker.encoded(
                    {
                        "success": True,
                        "generation_id": row["generation_id"],
                        "sha256": row["sha256"],
                        "snapshot_id": "2" * 64,
                        "publication_status": "OBSERVED",
                    }
                ),
                stderr=b"",
            )

        runtime = SimpleNamespace(
            queue=self.queue,
            store=self.store,
            config={
                "repository_id": "1" * 64,
                "python": {"path": "python.exe", "sha256": "4" * 64},
            },
            config_path="fixture.json",
            config_sha256="3" * 64,
            access=SimpleNamespace(integrity=lambda _path: None),
            job=lambda: SimpleNamespace(Limits=lambda **options: options, run_job=run_job),
        )
        with (
            mock.patch.object(self.queue, "next_pending", side_effect=next_pending),
            mock.patch.object(self.queue, "begin_attempt", side_effect=begin_attempt),
            mock.patch.object(queue_module, "read_pinned"),
        ):
            result = worker.run_one(runtime, reservation_lock=reservation)
        self.assertTrue(result["uploaded"])
        self.assertEqual(len(self.queue.generations()), 2)

    def fake_upload_runtime(self, *, bad_listing=False):
        calls = []
        snapshot = "2" * 64

        class FakeJob:
            Limits = staticmethod(lambda **values: SimpleNamespace(**values))

            def run_job(self, executable, arguments, **options):
                calls.append((arguments, options))
                verb = next(value for value in arguments if value in {"cat", "backup", "ls"})
                body = {
                    "cat": {"id": "1" * 64},
                    "backup": {"snapshot": snapshot},
                    "ls": {"valid": not bad_listing},
                }[verb]
                return SimpleNamespace(
                    status="completed",
                    exit_code=0,
                    cleanup_complete=True,
                    stdout=json.dumps(body).encode(),
                    stderr=b"",
                )

        def need(value):
            if not value:
                raise ValueError("Fixture inválida")

        provider = SimpleNamespace(
            environment=lambda _profile, values: {"RESTIC_PASSWORD": values["RESTIC_PASSWORD"]},
            S3=SimpleNamespace(
                OwnerClient=lambda *_args: SimpleNamespace(verify=lambda _raw: None)
            ),
            STATE=SimpleNamespace(encoded=lambda value: json.dumps(value).encode()),
            validate_result=lambda result: need(
                result.status == "completed" and result.exit_code == 0 and result.cleanup_complete
            ),
            parse_config=lambda raw, expected: need(json.loads(raw)["id"] == expected),
            parse_backup=lambda raw: json.loads(raw)["snapshot"],
            parse_listing=lambda raw, *_args: need(json.loads(raw)["valid"]),
        )
        runtime = SimpleNamespace(
            queue=self.queue,
            store=self.store,
            config={"repository_id": "1" * 64},
            profile={
                "root": str(self.store.root),
                "binary_path": "fixture-restic.exe",
                "hostname": "fixture-host",
                "repository_url": "https://example.invalid/repository",
                "owner": "legacy-owner",
                "operation_id": "fixture-operation",
            },
            verify_binary=lambda: None,
            job=lambda: FakeJob(),
            backup_helpers=lambda: provider,
            current_values=lambda: {
                "RESTIC_PASSWORD": "fixture-password",
                "R2_PLATFORM_ACCESS_KEY_ID": "fixture-access",
                "R2_PLATFORM_SECRET_ACCESS_KEY": "fixture-secret",
            },
        )
        return runtime, calls

    def test_controlled_upload_has_no_init_and_retains_prepared_status(self):
        with self.store.lock("fixture"):
            row = self.queue.enqueue(vault("futuro"), "ROOT_CANDIDATE")
        attempt = self.queue.begin_attempt(
            row["generation_id"],
            {"repository_id": "1" * 64, "sha256": row["sha256"], "publication_status": "PREPARED"},
        )
        runtime, calls = self.fake_upload_runtime()
        with mock.patch.object(worker, "publish_recovery_record") as publish:
            result = worker.upload_child(runtime, row["generation_id"], attempt["child_nonce"])
        publish.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["publication_status"], "PREPARED")
        self.assertEqual(len(calls), 3)
        self.assertFalse(
            self.store._path(self.queue.path(row["generation_id"], "uploaded.json")).exists()
        )
        for arguments, options in calls:
            self.assertNotIn("fixture-secret", " ".join(arguments))
            self.assertNotIn("init", arguments)
            self.assertEqual(options["output_limit"], 1024**2)
            self.assertEqual(options["limits"].memory_bytes, 2 * 1024**3)

    def test_bad_listing_retains_snapshot_for_manual_reconciliation(self):
        row = self.queue.observe(self.source)
        attempt = self.queue.begin_attempt(
            row["generation_id"],
            {"repository_id": "1" * 64, "sha256": row["sha256"], "publication_status": "OBSERVED"},
        )
        runtime, _calls = self.fake_upload_runtime(bad_listing=True)
        result = worker.upload_child(runtime, row["generation_id"], attempt["child_nonce"])
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "listado_restic")
        self.assertEqual(result["snapshot_id"], "2" * 64)
        self.assertTrue(result["requires_manual_reconciliation"])
        self.assertEqual(
            self.store.json(self.queue.path(row["generation_id"], "snapshot-recorded.json"))[
                "snapshot_id"
            ],
            "2" * 64,
        )
        with self.assertRaises(ValueError):
            self.queue.next_pending()

    def test_preflight_de_r2_reintenta_sin_duplicar_el_respaldo(self):
        with self.store.lock("fixture"):
            row = self.queue.enqueue(vault("preflight"), "ROOT_CANDIDATE")
        attempt = self.queue.begin_attempt(
            row["generation_id"],
            {"repository_id": "1" * 64, "sha256": row["sha256"], "publication_status": "PREPARED"},
        )
        runtime, calls = self.fake_upload_runtime()
        verify = mock.Mock(
            side_effect=[ValueError("R2 rechazó la operación HTTP 500.")] * 2 + [None]
        )
        runtime.backup_helpers().S3.OwnerClient = mock.Mock(
            return_value=SimpleNamespace(verify=verify)
        )
        with mock.patch.object(worker.time, "sleep") as pause:
            report = worker.upload_child(runtime, row["generation_id"], attempt["child_nonce"])
        self.assertTrue(report["success"])
        self.assertEqual(verify.call_count, 3)
        self.assertEqual(pause.call_args_list, [mock.call(1), mock.call(3)])
        self.assertEqual(sum("backup" in arguments for arguments, _ in calls), 1)

    def test_fallo_de_propietario_o_autorizacion_no_inicia_restic(self):
        for message, status in (
            ("El marcador R2 no pertenece a esta operación.", None),
            ("R2 rechazó la operación HTTP 403.", 403),
            ("R2 rechazó la operación HTTP 503.", 503),
        ):
            with self.subTest(status=status):
                with self.store.lock("fixture"):
                    row = self.queue.enqueue(vault(str(status)), "ROOT_CANDIDATE")
                attempt = self.queue.begin_attempt(
                    row["generation_id"],
                    {
                        "repository_id": "1" * 64,
                        "sha256": row["sha256"],
                        "publication_status": "PREPARED",
                    },
                )
                runtime, calls = self.fake_upload_runtime()
                verify = mock.Mock(side_effect=ValueError(message))
                runtime.backup_helpers().S3.OwnerClient = mock.Mock(
                    return_value=SimpleNamespace(verify=verify)
                )
                with mock.patch.object(worker.time, "sleep") as pause:
                    report = worker.upload_child(
                        runtime, row["generation_id"], attempt["child_nonce"]
                    )
                self.assertFalse(report["success"])
                self.assertEqual(report["stage"], "propietario_r2")
                self.assertEqual(report.get("http_status"), status)
                self.assertEqual(verify.call_count, 4 if status == 503 else 1)
                self.assertEqual(pause.call_count, 3 if status == 503 else 0)
                self.assertEqual(calls, [])

    def test_la_lectura_previa_respeta_el_plazo_del_reintento(self):
        verify = mock.Mock(side_effect=ValueError("R2 rechazó la operación HTTP 504."))
        with (
            mock.patch.object(worker.time, "monotonic", side_effect=[0, 11]),
            mock.patch.object(worker.time, "sleep") as pause,
            self.assertRaisesRegex(ValueError, r"HTTP 504"),
        ):
            worker.verify_upload_owner(SimpleNamespace(verify=verify), b"marcador")
        verify.assert_called_once_with(b"marcador")
        pause.assert_not_called()

    def test_child_limits_require_exact_immediate_job(self):
        limits = SimpleNamespace(
            BasicLimitInformation=SimpleNamespace(LimitFlags=0x2208, ActiveProcessLimit=8),
            JobMemoryLimit=2 * 1024**3,
        )
        worker.validate_child_limits(limits)
        limits.BasicLimitInformation.LimitFlags = 0
        with self.assertRaises(ValueError):
            worker.validate_child_limits(limits)

    def test_comment_block_cannot_inject_an_extra_variable(self):
        comments = {
            "NEW_VALUE": [
                "# Para qué: Fixture.\nINJECTED=valor",
                "# Dónde: Fixture.",
                "# Riesgo al rotar: Fixture.",
            ]
        }
        with self.assertRaises(ValueError):
            updater.render_update(
                vault(),
                {"NEW_VALUE": "nuevo"},
                parser.parse_env,
                parser.validate_comments,
                documentation,
                comments,
            )

    def test_committed_ack_reads_only_its_own_marker(self):
        result = self.publish()
        generation = result["generation_id"]
        row = self.queue.metadata(generation)
        self.queue.begin_attempt(generation, {"publication_status": "COMMITTED"})
        payload = {
            "success": True,
            "generation_id": generation,
            "sha256": row["sha256"],
            "snapshot_id": "2" * 64,
            "publication_status": "COMMITTED",
        }
        outer = SimpleNamespace(
            status="completed",
            exit_code=0,
            cleanup_complete=True,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
        )
        with mock.patch.object(
            self.queue,
            "generations",
            side_effect=AssertionError("Otra captura está en preparación"),
        ):
            self.assertTrue(worker.accept_upload_result(self.queue, generation, outer)["uploaded"])

    def test_cooperative_retry_preserves_an_unrelated_concurrent_edit(self):
        original = self.queue.reader
        calls = 0

        def reader(path, maximum):
            nonlocal calls
            if path == self.source:
                calls += 1
                if calls == 2:
                    self.source.write_bytes(
                        vault()
                        + (
                            "\n".join([*documentation("OTHER"), "OTHER=edicion-concurrente", ""])
                            + "\n"
                        ).encode()
                    )
            return original(path, maximum)

        self.queue.reader = reader
        self.assertTrue(self.publish()["committed"])
        values = parser.parse_env(self.source.read_text(encoding="utf8"))[0]
        self.assertEqual(values["OTHER"], "edicion-concurrente")
        self.assertEqual(
            [row["status"] for row in self.queue.generations()], ["PREPARED", "COMMITTED"]
        )

    def test_raw_bootstrap_checks_source_hash_and_ignores_neighbor_pyc(self):
        code = self.root / "sealed"
        code.mkdir()
        body = b"class Runtime:\n    def __init__(self, *args, **kwargs):\n        self.raw_source = True\n"
        source = code / "vault_change_queue.py"
        source.write_bytes(body)
        (code / "vault_change_queue.pyc").write_bytes(b"No debe ejecutarse este vecino")
        config = code / "config.json"
        config.write_text(
            json.dumps(
                {"code_root": str(code), "sources": {source.name: hashlib.sha256(body).hexdigest()}}
            ),
            encoding="utf8",
        )
        pin = hashlib.sha256(config.read_bytes()).hexdigest()
        for module in (updater, worker):
            self.assertTrue(module.bootstrap_runtime(str(config), pin).raw_source)
        source.write_bytes(body + b"# alterado\n")
        for module in (updater, worker):
            with self.assertRaises(ValueError):
                module.bootstrap_runtime(str(config), pin)

    def test_duplicate_config_keys_are_rejected_before_loading_code(self):
        code = self.root / "duplicate.json"
        code.write_bytes(b'{"code_root":"a","code_root":"b"}')
        pin = hashlib.sha256(code.read_bytes()).hexdigest()
        for module in (updater, worker):
            with self.subTest(module=module.__name__), self.assertRaises(ValueError):
                module.bootstrap_runtime(str(code), pin)

    def test_child_claim_blocks_repeating_even_the_same_attempt(self):
        row = self.queue.observe(self.source)
        attempt = self.queue.begin_attempt(
            row["generation_id"],
            {"repository_id": "1" * 64, "sha256": row["sha256"], "publication_status": "OBSERVED"},
        )
        runtime, calls = self.fake_upload_runtime()
        with mock.patch.object(
            worker, "publish_recovery_record", side_effect=[OSError("interrupción"), None]
        ) as publish:
            self.assertTrue(
                worker.upload_child(runtime, row["generation_id"], attempt["child_nonce"])[
                    "success"
                ]
            )
            with self.assertRaises(FileExistsError):
                worker.upload_child(runtime, row["generation_id"], attempt["child_nonce"])
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(
            [call.args[-1] for call in publish.call_args_list],
            [
                {
                    "repository_id": "1" * 64,
                    "generation_id": row["generation_id"],
                    "prepared_ns": row["prepared_ns"],
                    "snapshot_id": "2" * 64,
                    "sha256": row["sha256"],
                    "bytes": row["bytes"],
                    "publication_status": "OBSERVED",
                }
            ]
            * 2,
        )
        self.assertEqual(len(calls), 3)

    def test_native_job_adapter_accepts_one_mib_before_process_creation(self):
        job = load("vault_real_job_contract_tests", ROOT / "sre/windows_build_job.py")
        executable = self.root / "fixture.exe"
        executable.write_bytes(b"No es un ejecutable, nunca se ejecuta")

        class BeforeCreation(Exception):
            pass

        with mock.patch.object(job, "Win32", side_effect=BeforeCreation):
            with self.assertRaises(BeforeCreation):
                job.run_job(
                    executable,
                    [],
                    cwd=self.root,
                    env={},
                    timeout=390,
                    limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                    output_limit=worker.JOB_OUTPUT_LIMIT,
                )
            with self.assertRaises(ValueError):
                job.run_job(
                    executable,
                    [],
                    cwd=self.root,
                    env={},
                    timeout=390,
                    limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                    output_limit=32 * 1024**2,
                )


if __name__ == "__main__":
    unittest.main()
