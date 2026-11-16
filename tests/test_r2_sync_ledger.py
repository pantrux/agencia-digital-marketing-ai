"""Ledger local: validación portable y SQLite/flock nativos exclusivamente Linux."""

import hashlib
import importlib
import json
import os
import select
import stat
import sys
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/r2-sync-consumer"))
LEDGER = importlib.import_module("sync_ledger")
STORAGE = importlib.import_module("sync_storage")


def job():
    return {
        "job_id": "22222222-2222-4222-8222-222222222222",
        "asset_id": "11111111-1111-4111-8111-111111111111",
        "project_slug": "proyecto_01",
        "extension": "mp4",
        "bytes": 3,
        "sha256": hashlib.sha256(b"abc").hexdigest(),
        "object_key": "ingest/22222222-2222-4222-8222-222222222222/11111111-1111-4111-8111-111111111111.mp4",
    }


def fingerprint(value=None):
    return hashlib.sha256(json.dumps(value or job(), separators=(",", ":")).encode()).hexdigest()


def publication(**changes):
    result = STORAGE.Publication(
        job()["job_id"],
        job()["asset_id"],
        "proyectos/proyecto_01/01_raw/11111111-1111-4111-8111-111111111111.mp4",
        3,
        job()["sha256"],
        1,
        123,
        True,
        False,
    )
    return replace(result, **changes)


class ValidationTests(unittest.TestCase):
    def test_mountinfo_correlaciona_dispositivo_y_rechaza_red_o_fuse(self):
        local = b"1 0 8:1 / / rw - ext4 /dev/sda rw\n"
        self.assertEqual(LEDGER._mount_kind(local, "/state", "8:1"), "ext4")
        for kind in ("nfs4", "cifs", "fuse.sshfs", "tmpfs", "desconocido"):
            remote = f"2 1 0:9 / /state rw - {kind} origen rw\n".encode()
            with self.subTest(kind=kind), self.assertRaises(LEDGER.LedgerError):
                LEDGER._mount_kind(local + remote, "/state", "0:9")

    def test_catalogo_no_oculta_nombres_parecidos_a_internos_sqlite(self):
        import sqlite3

        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(LEDGER.META_SQL)
            connection.execute(LEDGER.JOBS_SQL)
            connection.execute("INSERT INTO ledger_meta VALUES (1,1,?)", (LEDGER.OWNER,))
            connection.execute(f"PRAGMA application_id={LEDGER.APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            LEDGER.Ledger._schema(None, connection)
            connection.execute("CREATE TABLE sqliteXshadow (payload TEXT)")
            with self.assertRaises(LEDGER.LedgerError):
                LEDGER.Ledger._schema(None, connection)
        finally:
            connection.close()

    def test_fingerprint_canonico_y_espera_acotada(self):
        validated = LEDGER.validate_operation(job(), fingerprint(), 1024, 0)
        self.assertEqual(validated.job_id, job()["job_id"])
        for wait in (True, -1, 5.01, float("inf"), float("nan"), "1"):
            with self.subTest(wait=wait), self.assertRaises(LEDGER.LedgerError):
                LEDGER.validate_operation(job(), fingerprint(), 1024, wait)
        for digest in ("0" * 64, "A" * 64, None):
            with self.assertRaises(LEDGER.LedgerError):
                LEDGER.validate_operation(job(), digest, 1024, 0)

    def test_recibo_cerrado_correlacion_y_tipos(self):
        snapshot = STORAGE.validate_job(job(), 1024)
        self.assertEqual(LEDGER.validate_publication(publication(), snapshot).inode, 123)
        for change in (
            {"job_id": "33333333-3333-4333-8333-333333333333"},
            {"asset_id": "33333333-3333-4333-8333-333333333333"},
            {"relative_path": "../fuera"},
            {"bytes": 4},
            {"bytes": True},
            {"sha256": "0" * 64},
            {"device": True},
            {"inode": 0},
            {"created": True, "idempotent": True},
        ):
            with self.subTest(change=change), self.assertRaises(LEDGER.LedgerError):
                LEDGER.validate_publication(publication(**change), snapshot)
        with self.assertRaises(LEDGER.LedgerError):
            LEDGER.validate_publication({"token": "no persistir"}, snapshot)


@unittest.skipUnless(
    sys.platform.startswith("linux"),
    "Requiere SQLite/flock Linux reales; Windows no acredita estos contratos.",
)
class LinuxLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agency-ledger-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.marker = self.state / ".owner"
        self.marker.write_bytes(b"estado-local-fixture")
        self.marker.chmod(0o600)
        info = self.state.stat()
        self.proof = STORAGE.RootProof(
            info.st_dev,
            info.st_ino,
            self.marker.name,
            hashlib.sha256(self.marker.read_bytes()).hexdigest(),
        )
        self.ledger = LEDGER.Ledger(self.state, self.proof)
        self.ledger.initialize()
        self.db = self.state / LEDGER.DB_NAME

    def operation(self, ledger=None, value=None, **kwargs):
        value = value or job()
        return (ledger or self.ledger).operation(
            value, fingerprint(value), max_object_bytes=1024, **kwargs
        )

    def test_reserva_persistida_pending_reabierto_requiere_reconciliacion(self):
        with self.operation() as operation:
            reservation = operation.reserve()
            self.assertTrue(reservation.is_new)
            self.assertEqual(reservation.state, "pending")
        reopened = LEDGER.Ledger(self.state, self.proof)
        with self.operation(reopened) as operation:
            self.assertFalse(operation.reserve().is_new)
            with self.assertRaises(LEDGER.LedgerError):
                operation.complete(publication())
            operation.reconcile(publication())
            receipt = operation.complete(publication())
            self.assertEqual(receipt.inode, 123)

    def test_completed_inmutable_e_idempotente_y_sin_payload(self):
        with self.operation() as operation:
            operation.reserve()
            original = operation.complete(publication())
            self.assertEqual(operation.complete(publication()), original)
            with self.assertRaises(LEDGER.LedgerError):
                operation.complete(publication(inode=124))
        with self.operation() as operation:
            cached = operation.reserve()
            self.assertEqual(cached.state, "completed")
            self.assertEqual(cached.receipt, original)
        import sqlite3

        with sqlite3.connect(self.db) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(jobs)")]
            self.assertEqual(columns, ["job_id", "fingerprint", "state", "receipt"])
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)

    def test_misma_id_otro_fingerprint_es_conflicto(self):
        with self.operation() as operation:
            operation.reserve()
        changed = job()
        changed["bytes"] = 4
        with self.operation(value=changed) as operation:
            with self.assertRaises(LEDGER.LedgerError):
                operation.reserve()
        with self.operation() as operation:
            self.assertEqual(operation.reserve().state, "pending")

    def test_complete_sin_reserva_y_uso_fuera_contexto_rechazados(self):
        with self.operation() as operation:
            with self.assertRaises(LEDGER.LedgerError):
                operation.complete(publication())
        with self.assertRaises(LEDGER.LedgerError):
            operation.reserve()

    def test_reconcile_debe_ser_el_recibo_que_se_completa(self):
        with self.operation() as operation:
            operation.reserve()
        with self.operation() as operation:
            operation.reserve()
            operation.reconcile(publication(created=False, idempotent=True))
            with self.assertRaises(LEDGER.LedgerError):
                operation.complete(publication())
            operation.complete(publication(created=False, idempotent=True))

    def test_inicializacion_explicita_no_adopta_db_existente(self):
        original = self.db.read_bytes()
        with self.assertRaises(LEDGER.LedgerError):
            self.ledger.initialize()
        self.assertEqual(self.db.read_bytes(), original)

    def test_db_symlink_hardlink_y_permisos_ajenos_rechazados(self):
        original = self.db.read_bytes()
        outside = self.base / "ajena"
        outside.write_bytes(original)
        outside.chmod(0o600)
        self.db.unlink()
        self.db.symlink_to(outside)
        with self.assertRaises(LEDGER.LedgerError):
            with self.operation():
                self.fail("No debe abrir una DB enlazada")
        self.db.unlink()
        os.link(outside, self.db)
        with self.assertRaises(LEDGER.LedgerError):
            with self.operation():
                self.fail("No debe abrir una DB con otros enlaces")
        self.db.unlink()
        self.db.write_bytes(original)
        self.db.chmod(0o644)
        with self.assertRaises(LEDGER.LedgerError):
            with self.operation():
                self.fail("No debe abrir una DB pública")
        self.assertEqual(outside.read_bytes(), original)

    def test_modo_raiz_marcador_y_prueba_externa_rechazados(self):
        self.state.chmod(0o755)
        with self.assertRaises(LEDGER.LedgerError):
            LEDGER.Ledger(self.state, self.proof)
        self.state.chmod(0o700)
        with self.assertRaises(LEDGER.LedgerError):
            LEDGER.Ledger(self.state, replace(self.proof, inode=self.proof.inode + 1))
        self.marker.write_bytes(b"otro marcador")
        with self.assertRaises(LEDGER.LedgerError):
            with self.operation():
                self.fail("No debe adoptar el marcador cambiado")

    def test_owner_o_schema_sqlite_diferente_rechazado(self):
        import sqlite3

        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE ledger_meta SET owner='ajeno'")
        with self.assertRaises(LEDGER.LedgerError):
            with self.operation():
                self.fail("Owner ajeno no permitido")

    def test_tabla_o_trigger_extra_rechazado(self):
        import sqlite3

        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE extra (payload TEXT)")
        with self.assertRaises(LEDGER.LedgerError):
            with self.operation():
                self.fail("Esquema ampliado no permitido")

    def test_limite_db_y_lock_ajeno_rechazados(self):
        with self.db.open("ab") as stream:
            stream.truncate(LEDGER.MAX_DB_BYTES + 1)
        with self.assertRaises(LEDGER.LedgerError):
            with self.operation():
                self.fail("DB excesiva no permitida")
        self.db.unlink()
        lock = self.state / LEDGER.LOCK_NAME
        lock.write_bytes(b"ajeno")
        with self.assertRaises(LEDGER.LedgerError):
            self.ledger.initialize()

    def test_cambio_padre_no_reabre_otro_arbol(self):
        previous = self.base / "state-anterior"
        with self.assertRaises(LEDGER.LedgerError):
            with self.operation() as operation:
                operation.reserve()
                self.state.rename(previous)
                self.state.mkdir(mode=0o700)
                operation.complete(publication())
        self.assertFalse((self.state / LEDGER.DB_NAME).exists())

    def test_crash_real_posterior_reserva_libera_lock_y_conserva_pending(self):
        child = os.fork()
        if child == 0:
            try:
                with self.operation() as operation:
                    operation.reserve()
                    os._exit(17)
            except BaseException:
                os._exit(99)
        _pid, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 17)
        with self.operation() as operation:
            reservation = operation.reserve()
            self.assertFalse(reservation.is_new)
            self.assertEqual(reservation.state, "pending")
            operation.reconcile(publication())
            operation.complete(publication())

    def test_dos_procesos_flock_real_y_espera_acotada(self):
        go_read, go_write = os.pipe()
        result_read, result_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(go_write)
            os.close(result_read)
            try:
                os.read(go_read, 1)
                with self.operation(wait_seconds=0.05):
                    os.write(result_write, b"F")
            except LEDGER.LedgerError as error:
                os.write(result_write, b"T" if error.code == "LOCK_TIMEOUT" else b"E")
            finally:
                os._exit(0)
        os.close(go_read)
        os.close(result_write)
        try:
            with self.operation():
                os.write(go_write, b"G")
                ready, _, _ = select.select([result_read], [], [], 3)
                self.assertTrue(ready)
                self.assertEqual(os.read(result_read, 1), b"T")
        finally:
            os.close(go_write)
            os.close(result_read)
            os.waitpid(child, 0)
        with self.operation():
            pass

    def test_reloj_inyectado_no_puede_generar_espera_infinita(self):
        with self.operation():
            with self.assertRaises(LEDGER.LedgerError):
                with self.operation(
                    wait_seconds=0.05, clock=lambda: 1.0, sleep=lambda _delay: None
                ):
                    self.fail("No debe adquirir el lock retenido")

    def test_fds_cerrados_y_max_page_count(self):
        def descriptors():
            targets = Counter()
            for descriptor in Path("/proc/self/fd").iterdir():
                try:
                    targets[os.readlink(descriptor)] += 1
                except FileNotFoundError:
                    # El descriptor de la enumeración puede cerrarse antes del readlink.
                    continue
            return targets

        before = descriptors()
        for _ in range(3):
            with self.operation() as operation:
                operation.reserve()
        self.assertFalse(descriptors() - before)

    def test_check_verifica_sin_reservar_y_respeta_lease_activo(self):
        import sqlite3

        before = self.db.read_bytes()
        self.assertIsNone(self.ledger.check())
        self.assertEqual(self.db.read_bytes(), before)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone(), (0,))
        with self.operation():
            with self.assertRaises(LEDGER.LedgerError) as failure:
                self.ledger.check()
            self.assertEqual(failure.exception.code, "LOCK_TIMEOUT")
        self.assertIsNone(self.ledger.check())
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE ajena (valor INTEGER)")
        with self.assertRaises(LEDGER.LedgerError) as failure:
            self.ledger.check()
        self.assertEqual(failure.exception.code, "ESQUEMA_U_OWNER_AJENO")

    def test_max_page_count_impide_crecer_y_rollback_preserva_reservas(self):
        import uuid

        bounded = LEDGER.Ledger(self.state, self.proof, max_db_bytes=4 * LEDGER.PAGE_SIZE)
        full = False
        for _ in range(128):
            value = job()
            value["job_id"] = str(uuid.uuid4())
            value["object_key"] = f"ingest/{value['job_id']}/{value['asset_id']}.mp4"
            try:
                with self.operation(bounded, value) as operation:
                    operation.reserve()
                    operation.complete(publication(job_id=value["job_id"]))
            except LEDGER.LedgerError:
                full = True
                break
        self.assertTrue(full)
        self.assertLessEqual(self.db.stat().st_size, 4 * LEDGER.PAGE_SIZE)


if __name__ == "__main__":
    unittest.main()
