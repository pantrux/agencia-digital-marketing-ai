"""Comprueba la migración de negocio y su comportamiento con SQLite local."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "cloudflare-saas" / "migrations"
PREVIOUS = (MIGRATIONS / "0000_resource_owner.sql").read_text(encoding="utf-8")
SCHEMA = MIGRATIONS / "0001_schema.sql"
NOVEMBER = 1794614400
NOVEMBER_START = "2026-11-01"


def connect(path=":memory:"):
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def migrate(db):
    db.executescript(PREVIOUS)
    db.executescript(SCHEMA.read_text(encoding="utf-8"))


@pytest.fixture
def database():
    db = connect()
    try:
        migrate(db)
        with db:
            db.execute(
                "INSERT INTO users (id, google_sub, email) VALUES ('u', 'google-u', 'u@fixture.invalid')"
            )
        yield db
    finally:
        db.close()


def reserve(db, order, *, user="u", plan="free", key=None, queue_priority=0):
    db.execute(
        """INSERT INTO orders (id, user_id, plan_id, kind, status, idempotency_key, project_slug,
                               request_sha256, queue_priority, created_at)
        VALUES (?, ?, ?, 'production', 'queued', ?, ?, ?, ?, ?)""",
        (order, user, plan, key or order, order, "a" * 64, queue_priority, NOVEMBER),
    )
    db.execute(
        """INSERT INTO usage_ledger (id, user_id, order_id, period_start, units, status, created_at)
        VALUES (?, ?, ?, ?, 1, 'reserved', ?)""",
        (f"quota-{order}", user, order, NOVEMBER_START, NOVEMBER),
    )


def publish_media(db, order):
    db.execute(
        """INSERT INTO upload_sessions (id, user_id, order_id, object_key, sha256, bytes,
                                         mime_type, extension, upload_id, status, object_etag,
                                         created_at, expires_at)
        VALUES (?, 'u', ?, ?, ?, 20, 'video/mp4', 'mp4', ?, 'completed', 'etag', ?, ?)""",
        (
            f"asset-{order}",
            order,
            f"ingest/{order}/asset.mp4",
            "b" * 64,
            f"upload-{order}",
            NOVEMBER,
            NOVEMBER + 100,
        ),
    )
    db.execute("INSERT INTO job_outbox (order_id, payload) VALUES (?, '{}')", (order,))


def test_migration_preserves_owner_and_unrelated_data():
    db = connect()
    try:
        db.executescript(PREVIOUS)
        owner = db.execute("SELECT * FROM _agency_resource_owner").fetchone()
        db.executescript(
            "CREATE TABLE unrelated(value TEXT); INSERT INTO unrelated VALUES ('intacto');"
        )
        db.executescript(SCHEMA.read_text(encoding="utf-8"))
        assert db.execute("SELECT * FROM _agency_resource_owner").fetchone() == owner
        assert db.execute("SELECT * FROM unrelated").fetchone() == ("intacto",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_upload_abort_reason_migration_preserves_historical_sessions(database):
    reserve(database, "historico")
    database.execute(
        """INSERT INTO upload_sessions (id, user_id, order_id, object_key, sha256, bytes,
            mime_type, extension, status, created_at, expires_at)
        VALUES ('asset-historico', 'u', 'historico', 'ingest/historico', ?, 20,
            'video/mp4', 'mp4', 'aborted', ?, ?)""",
        ("a" * 64, NOVEMBER, NOVEMBER + 100),
    )
    migration = MIGRATIONS / "0014_upload_abort_reason.sql"
    database.executescript(migration.read_text(encoding="utf-8"))

    assert database.execute("SELECT status, abort_reason FROM upload_sessions").fetchall() == [
        ("aborted", None)
    ]
    with pytest.raises(sqlite3.IntegrityError):
        database.execute("UPDATE upload_sessions SET abort_reason='asserted_without_evidence'")
    with pytest.raises(sqlite3.IntegrityError):
        database.execute("UPDATE upload_sessions SET status='creating', abort_reason='expired'")
    database.execute("UPDATE upload_sessions SET abort_reason='sha256_mismatch'")
    assert database.execute("SELECT abort_reason FROM upload_sessions").fetchone() == (
        "sha256_mismatch",
    )


def test_catalog_has_documented_values_without_unconfirmed_duration_or_prices(database):
    assert database.execute(
        """SELECT plan_id, monthly_reels, retention_days, storage_bytes,
                  storage_limit_bytes, max_output_seconds, queue_priority, max_resolution,
                  watermark, allow_prores4444
        FROM plan_quotas ORDER BY queue_priority"""
    ).fetchall() == [
        ("free", 1, 7, 2_000_000_000, 2_000_000_000, None, 0, "1080p", 1, 0),
        ("pro", 12, 30, 25_000_000_000, 25_000_000_000, None, 1, "1080p", 0, 0),
        ("ultra", 40, 90, 100_000_000_000, 100_000_000_000, None, 2, "4k", 0, 0),
        ("ilimitado", None, None, 500_000_000_000, None, None, 3, "4k", 0, 1),
    ]


def test_migracion_de_resolucion_preserva_pedidos_y_reservas(database):
    reserve(database, "pedido-historico")
    database.executescript(
        (MIGRATIONS / "0009_production_resolution.sql").read_text(encoding="utf-8")
    )
    assert database.execute(
        "SELECT o.output_resolution, u.status FROM orders o JOIN usage_ledger u ON u.order_id=o.id"
    ).fetchone() == ("1080p", "reserved")
    with pytest.raises(sqlite3.IntegrityError, match="OUTPUT_RESOLUTION_IMMUTABLE"):
        database.execute("UPDATE orders SET output_resolution='4k' WHERE id='pedido-historico'")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        database.execute(
            """INSERT INTO orders (id,user_id,plan_id,kind,idempotency_key,request_sha256,output_resolution)
               VALUES ('otro','u','free','production','otra',?,'4k')""",
            ("b" * 64,),
        )


def test_migracion_de_formato_preserva_mp4_y_restringe_prores(database):
    reserve(database, "pedido-historico")
    database.executescript(
        (MIGRATIONS / "0011_production_output_format.sql").read_text(encoding="utf-8")
    )
    assert database.execute(
        "SELECT o.output_format, u.status FROM orders o JOIN usage_ledger u ON u.order_id=o.id"
    ).fetchone() == ("mp4", "reserved")
    with pytest.raises(sqlite3.IntegrityError, match="OUTPUT_FORMAT_IMMUTABLE"):
        database.execute("UPDATE orders SET output_format='prores4444' WHERE id='pedido-historico'")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        database.execute(
            """INSERT INTO orders (id,user_id,plan_id,kind,idempotency_key,request_sha256,output_format)
               VALUES ('otro','u','ultra','production','otra',?,'prores4444')""",
            ("b" * 64,),
        )


def test_one_concurrent_reservation_wins_and_losing_batch_rolls_back(tmp_path):
    path = tmp_path / "test.sqlite"
    with connect(path) as db:
        migrate(db)
        db.execute(
            "INSERT INTO users (id, google_sub, email) VALUES ('u', 'google-u', 'u@fixture.invalid')"
        )
    db.close()
    ready = Barrier(2)

    def submission(order):
        db = connect(path)
        try:
            ready.wait(timeout=5)
            with db:
                reserve(db, order)
                publish_media(db, order)
        except sqlite3.IntegrityError as error:
            assert "QUOTA_EXCEEDED" in str(error)
            return False
        finally:
            db.close()
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submission, ("first", "second"))) == [False, True]
    db = connect(path)
    try:
        for table in ("orders", "usage_ledger", "job_outbox"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
    finally:
        db.close()


def test_release_can_be_reused_but_cannot_be_reopened(database):
    with database:
        reserve(database, "one")
        database.execute("UPDATE usage_ledger SET status = 'released' WHERE order_id = 'one'")
        reserve(database, "two")
    with pytest.raises(sqlite3.IntegrityError, match="QUOTA_IMMUTABLE"):
        with database:
            database.execute("UPDATE usage_ledger SET status = 'reserved' WHERE order_id = 'one'")


def test_authorized_mandate_or_expired_paid_period_grants_no_plan(database):
    with database:
        database.execute(
            """INSERT INTO subscriptions (id, user_id, plan_id, status, billing_status, provider,
                                          provider_mode, external_reference, provider_subscription_id,
                                          current_period_start, current_period_end)
            VALUES ('sub', 'u', 'pro', 'authorized', 'pending', 'mercadopago',
                    'production', 'external', 'mp-sub', ?, ?)""",
            (NOVEMBER - 100, NOVEMBER + 100),
        )
    with pytest.raises(sqlite3.IntegrityError, match="QUOTA_PLAN_MISMATCH"):
        with database:
            reserve(database, "blocked", plan="pro", queue_priority=1)
    with database:
        database.execute("UPDATE subscriptions SET status = 'active', billing_status = 'paid'")
        reserve(database, "actual", plan="pro", queue_priority=1)
        database.execute(
            "UPDATE subscriptions SET current_period_end = ?",
            (NOVEMBER - 1,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="QUOTA_PLAN_MISMATCH"):
        with database:
            reserve(database, "expired", plan="pro", queue_priority=1)


def test_session_revocation_and_idempotent_orders_and_webhook_deliveries(database):
    with database:
        database.execute(
            """INSERT INTO sessions (id, user_id, nonce, created_at, expires_at)
               VALUES (?, 'u', 'jti-fixture', ?, ?)""",
            ("b" * 64, NOVEMBER, NOVEMBER + 100),
        )
        database.execute("UPDATE sessions SET revoked_at = ?", (NOVEMBER + 1,))
        reserve(database, "first", key="attempt")
        event = """INSERT INTO webhook_events (provider, provider_mode, event_id, resource_id, topic)
           VALUES ('mercadopago', 'sandbox', ?, 'pay-11', 'payment')
           ON CONFLICT(provider, provider_mode, event_id) DO NOTHING"""
        assert database.execute(event, ("delivery-1",)).rowcount == 1
        assert database.execute(event, ("delivery-1",)).rowcount == 0
        assert database.execute(event, ("delivery-2",)).rowcount == 1
    assert database.execute(
        "SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL AND expires_at > ?",
        (NOVEMBER + 2,),
    ).fetchone() == (0,)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with database:
            reserve(database, "second", key="attempt")
    assert database.execute("SELECT COUNT(*) FROM orders").fetchone() == (1,)


def test_failed_dependency_rolls_back_order_ledger_and_outbox(database):
    with pytest.raises(sqlite3.IntegrityError):
        with database:
            reserve(database, "one")
            publish_media(database, "one")
            database.execute("UPDATE job_outbox SET payload = 'not json'")
    for table in ("orders", "usage_ledger", "job_outbox"):
        assert database.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_production_message_requires_completed_upload_and_reserved_quota(database):
    with database:
        reserve(database, "one")
    with pytest.raises(sqlite3.IntegrityError, match="UPLOAD_NOT_VERIFIED"):
        with database:
            database.execute("INSERT INTO job_outbox (order_id, payload) VALUES ('one', '{}')")
    with database:
        publish_media(database, "one")
    assert database.execute("SELECT order_id FROM job_outbox").fetchall() == [("one",)]


def test_usage_survives_plan_changes_and_cannot_cross_provider_environment(database):
    with database:
        reserve(database, "initial")
        database.execute("UPDATE usage_ledger SET status = 'consumed' WHERE order_id = 'initial'")
        database.execute(
            """INSERT INTO subscriptions (id, user_id, plan_id, status, billing_status,
                                          provider_mode, external_reference, provider_subscription_id,
                                          current_period_start, current_period_end)
            VALUES ('paid', 'u', 'pro', 'active', 'paid', 'production', 'ext-paid', 'mp-paid', ?, ?),
                   ('sandbox', 'u', 'ultra', 'active', 'paid', 'sandbox', 'ext-test', 'mp-test', ?, ?)""",
            (NOVEMBER - 100, NOVEMBER + 100, NOVEMBER - 100, NOVEMBER + 100),
        )
        for number in range(11):
            reserve(database, f"paid-{number}", plan="pro", queue_priority=1)
    with pytest.raises(sqlite3.IntegrityError, match="QUOTA_EXCEEDED"):
        with database:
            reserve(database, "thirteenth", plan="pro", queue_priority=1)
    with pytest.raises(sqlite3.IntegrityError, match="QUOTA_PLAN_MISMATCH"):
        with database:
            reserve(database, "sandbox-in-production", plan="ultra", queue_priority=2)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with database:
            database.execute(
                """INSERT INTO subscriptions (id, user_id, plan_id, status, billing_status,
                                              provider_mode, external_reference, provider_subscription_id,
                                              current_period_start, current_period_end)
                VALUES ('duplicate', 'u', 'pro', 'active', 'paid', 'production', 'duplicate',
                        'mp-duplicate', ?, ?)""",
                (NOVEMBER - 100, NOVEMBER + 100),
            )


def test_failed_schema_batch_does_not_leave_half_applied_migration():
    db = connect()
    try:
        db.executescript(PREVIOUS)
        db.executescript(
            "CREATE TABLE orders(fixture TEXT); INSERT INTO orders VALUES ('original');"
        )
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            db.executescript("BEGIN;\n" + SCHEMA.read_text(encoding="utf-8") + "\nCOMMIT;")
        db.rollback()
        assert db.execute("SELECT * FROM orders").fetchall() == [("original",)]
        assert db.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name"
        ).fetchall() == [
            ("_agency_resource_owner",),
            ("orders",),
        ]
    finally:
        db.close()
