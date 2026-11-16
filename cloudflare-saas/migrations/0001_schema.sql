-- Esquema SaaS. Wrangler aporta la transacción de migración y D1 valida claves foráneas.
-- Las fechas *_at y las ventanas de cobro son segundos UTC. El ledger usa mes civil UTC.

CREATE TABLE users (
    id TEXT NOT NULL PRIMARY KEY,
    google_sub TEXT NOT NULL UNIQUE CHECK (length(google_sub) BETWEEN 1 AND 255),
    email TEXT NOT NULL CHECK (length(email) BETWEEN 3 AND 320),
    display_name TEXT CHECK (display_name IS NULL OR length(display_name) <= 255),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (updated_at >= 0)
) STRICT;

CREATE TABLE sessions (
    id TEXT NOT NULL PRIMARY KEY CHECK (length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    nonce TEXT NOT NULL UNIQUE CHECK (length(nonce) BETWEEN 1 AND 128),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (created_at >= 0),
    expires_at INTEGER NOT NULL CHECK (expires_at > created_at),
    revoked_at INTEGER CHECK (revoked_at IS NULL OR revoked_at >= created_at)
) STRICT;

CREATE INDEX sessions_user_id ON sessions(user_id);
CREATE INDEX sessions_expires_at ON sessions(expires_at);

CREATE TABLE plan_quotas (
    plan_id TEXT NOT NULL PRIMARY KEY CHECK (plan_id IN ('free', 'pro', 'ultra', 'ilimitado')),
    monthly_reels INTEGER CHECK (monthly_reels > 0),
    retention_days INTEGER CHECK (retention_days > 0),
    storage_bytes INTEGER NOT NULL CHECK (storage_bytes > 0),
    storage_limit_bytes INTEGER CHECK (storage_limit_bytes >= storage_bytes),
    max_output_seconds INTEGER CHECK (max_output_seconds > 0),
    queue_priority INTEGER NOT NULL UNIQUE CHECK (queue_priority BETWEEN 0 AND 3),
    max_resolution TEXT NOT NULL CHECK (max_resolution IN ('1080p', '4k')),
    watermark INTEGER NOT NULL CHECK (watermark IN (0, 1)),
    allow_prores4444 INTEGER NOT NULL CHECK (allow_prores4444 IN (0, 1)),
    allow_auto_clipping INTEGER NOT NULL CHECK (allow_auto_clipping IN (0, 1)),
    allow_broll INTEGER NOT NULL CHECK (allow_broll IN (0, 1)),
    allow_copilot INTEGER NOT NULL CHECK (allow_copilot IN (0, 1)),
    allow_multi_brand INTEGER NOT NULL CHECK (allow_multi_brand IN (0, 1)),
    autonomous_qa_rounds INTEGER NOT NULL CHECK (autonomous_qa_rounds BETWEEN 1 AND 3),
    human_adjustments INTEGER CHECK (human_adjustments >= 0)
) STRICT;

-- NULL representa una característica sin límite de esta matriz. La duración comercial
-- sigue pendiente. La capacidad incluida de Ilimitado no fija un máximo de almacenamiento.
INSERT INTO plan_quotas (
    plan_id, monthly_reels, retention_days, storage_bytes, storage_limit_bytes, max_output_seconds,
    queue_priority, max_resolution, watermark, allow_prores4444, allow_auto_clipping, allow_broll,
    allow_copilot, allow_multi_brand, autonomous_qa_rounds, human_adjustments
) VALUES
    ('free',      1,    7,    2000000000,   2000000000,   NULL, 0, '1080p', 1, 0, 0, 0, 0, 0, 1, 0),
    ('pro',       12,   30,   25000000000,  25000000000,  NULL, 1, '1080p', 0, 0, 0, 0, 1, 0, 2, 1),
    ('ultra',     40,   90,   100000000000, 100000000000, NULL, 2, '4k',    0, 0, 1, 1, 1, 0, 3, 3),
    ('ilimitado', NULL, NULL, 500000000000, NULL,         NULL, 3, '4k',    0, 1, 1, 1, 1, 1, 3, NULL);

-- El mandato autorizado y la cobertura pagada son hechos distintos.
CREATE TABLE subscriptions (
    id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    plan_id TEXT NOT NULL REFERENCES plan_quotas(plan_id) CHECK (plan_id <> 'free'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'authorized', 'active', 'paused', 'cancelled', 'expired')),
    billing_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (billing_status IN ('pending', 'paid', 'failed', 'refunded', 'charged_back')),
    provider TEXT NOT NULL DEFAULT 'mercadopago' CHECK (provider = 'mercadopago'),
    provider_mode TEXT NOT NULL CHECK (provider_mode IN ('sandbox', 'production')),
    external_reference TEXT NOT NULL CHECK (length(external_reference) BETWEEN 1 AND 128),
    provider_subscription_id TEXT CHECK (provider_subscription_id IS NULL OR length(provider_subscription_id) > 0),
    current_period_start INTEGER CHECK (current_period_start >= 0),
    current_period_end INTEGER CHECK (current_period_end > current_period_start),
    provider_updated_at INTEGER CHECK (provider_updated_at >= 0),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (updated_at >= 0),
    UNIQUE (provider, provider_mode, external_reference),
    UNIQUE (provider, provider_mode, provider_subscription_id),
    CHECK ((current_period_start IS NULL) = (current_period_end IS NULL)),
    CHECK (
        status <> 'active'
        OR (
            billing_status = 'paid' AND provider_subscription_id IS NOT NULL
            AND current_period_start IS NOT NULL AND current_period_end IS NOT NULL
        )
    )
) STRICT;

CREATE UNIQUE INDEX subscriptions_active_per_user
    ON subscriptions(user_id, provider_mode) WHERE status = 'active';

CREATE TABLE orders (
    id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    plan_id TEXT NOT NULL REFERENCES plan_quotas(plan_id),
    kind TEXT NOT NULL CHECK (kind IN ('production', 'subscription')),
    status TEXT NOT NULL DEFAULT 'pending',
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_sha256 TEXT NOT NULL
        CHECK (length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    project_slug TEXT UNIQUE,
    queue_priority INTEGER CHECK (queue_priority BETWEEN 0 AND 3),
    amount_minor INTEGER CHECK (amount_minor > 0),
    currency TEXT CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    provider TEXT CHECK (provider = 'mercadopago'),
    provider_mode TEXT CHECK (provider_mode IN ('sandbox', 'production')),
    provider_order_id TEXT CHECK (provider_order_id IS NULL OR length(provider_order_id) > 0),
    provider_updated_at INTEGER CHECK (provider_updated_at >= 0),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (updated_at >= 0),
    UNIQUE (user_id, idempotency_key),
    UNIQUE (id, user_id),
    UNIQUE (provider, provider_mode, provider_order_id),
    CHECK (
        (kind = 'production' AND status IN ('pending', 'queued', 'processing', 'completed', 'failed', 'cancelled')
         AND queue_priority IS NOT NULL AND amount_minor IS NULL AND currency IS NULL
         AND project_slug IS NOT NULL AND length(project_slug) BETWEEN 1 AND 63
         AND substr(project_slug, 1, 1) GLOB '[a-z0-9]'
         AND project_slug NOT GLOB '*[^a-z0-9_-]*'
         AND provider IS NULL AND provider_order_id IS NULL AND provider_updated_at IS NULL)
        OR
        (kind = 'subscription' AND plan_id <> 'free'
         AND status IN ('pending', 'paid', 'failed', 'cancelled', 'refunded', 'charged_back')
         AND project_slug IS NULL AND queue_priority IS NULL AND amount_minor IS NOT NULL AND currency IS NOT NULL
         AND provider IS NOT NULL AND provider_mode IS NOT NULL)
    )
) STRICT;

CREATE INDEX orders_user_created ON orders(user_id, created_at DESC);

CREATE TABLE usage_ledger (
    id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    order_id TEXT NOT NULL UNIQUE,
    period_start TEXT NOT NULL,
    units INTEGER NOT NULL DEFAULT 1 CHECK (units = 1),
    status TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'consumed', 'released')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (updated_at >= 0),
    FOREIGN KEY (order_id, user_id) REFERENCES orders(id, user_id),
    CHECK (period_start IS strftime('%Y-%m-01', created_at, 'unixepoch'))
) STRICT;

CREATE INDEX usage_ledger_counted ON usage_ledger(user_id, period_start)
    WHERE status IN ('reserved', 'consumed');

-- Worker: insertar pedido y reservar en un batch de D1.
-- El cambio de cuota entre planes no reinicia el consumo de ese usuario en el mes.
CREATE TRIGGER usage_ledger_reserve
BEFORE INSERT ON usage_ledger
BEGIN
    SELECT RAISE(ABORT, 'QUOTA_ORDER_INVALID')
    WHERE NEW.status <> 'reserved'
       OR NOT EXISTS (
           SELECT 1 FROM orders AS o
           WHERE o.id = NEW.order_id AND o.user_id = NEW.user_id
             AND o.kind = 'production' AND o.status = 'queued'
             AND NEW.period_start IS strftime('%Y-%m-01', o.created_at, 'unixepoch')
       );

    SELECT RAISE(ABORT, 'QUOTA_PLAN_MISMATCH')
    FROM orders AS o JOIN plan_quotas AS p ON p.plan_id = o.plan_id
    WHERE o.id = NEW.order_id
      AND (
          o.queue_priority <> p.queue_priority OR o.plan_id <> COALESCE((
              SELECT s.plan_id FROM subscriptions AS s
              WHERE s.user_id = NEW.user_id AND s.status = 'active'
                AND s.provider_mode = COALESCE(o.provider_mode, 'production')
                AND s.current_period_start <= o.created_at
                AND o.created_at < s.current_period_end
          ), 'free')
      );

    SELECT RAISE(ABORT, 'QUOTA_EXCEEDED')
    FROM orders AS o JOIN plan_quotas AS p ON p.plan_id = o.plan_id
    WHERE o.id = NEW.order_id
      AND p.monthly_reels IS NOT NULL
      AND NEW.units + COALESCE((
          SELECT SUM(u.units) FROM usage_ledger AS u
          JOIN orders AS prior ON prior.id = u.order_id
          WHERE u.user_id = NEW.user_id AND u.period_start = NEW.period_start
            AND u.status IN ('reserved', 'consumed')
            AND COALESCE(prior.provider_mode, 'production') = COALESCE(o.provider_mode, 'production')
      ), 0) > p.monthly_reels;
END;

-- Una sesión de ingesta por orden. Los tokens de operación son identificadores de
-- concurrencia internos; las URLs firmadas no se escriben en la base.
CREATE TABLE upload_sessions (
    id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    order_id TEXT NOT NULL UNIQUE,
    object_key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    bytes INTEGER NOT NULL CHECK (bytes > 0),
    mime_type TEXT NOT NULL,
    extension TEXT NOT NULL,
    upload_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('creating', 'open', 'completing', 'completed', 'aborting', 'aborted')),
    operation_id TEXT,
    lease_until INTEGER CHECK (lease_until >= 0),
    object_etag TEXT,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    expires_at INTEGER NOT NULL CHECK (expires_at > created_at),
    CHECK ((operation_id IS NULL) = (lease_until IS NULL)),
    CHECK (status NOT IN ('open', 'completing', 'completed') OR upload_id IS NOT NULL),
    CHECK (status <> 'completed' OR object_etag IS NOT NULL),
    FOREIGN KEY (order_id, user_id) REFERENCES orders(id, user_id)
) STRICT;

CREATE INDEX upload_sessions_by_user ON upload_sessions(user_id, expires_at);

CREATE TRIGGER usage_ledger_keep_reservation
BEFORE UPDATE ON usage_ledger
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id OR NEW.order_id IS NOT OLD.order_id
     OR NEW.period_start IS NOT OLD.period_start OR NEW.units IS NOT OLD.units
     OR NEW.created_at IS NOT OLD.created_at
     OR (NEW.status IS NOT OLD.status AND OLD.status <> 'reserved')
BEGIN
    SELECT RAISE(ABORT, 'QUOTA_IMMUTABLE');
END;

-- Un fallo entre la escritura D1 y el envío deja el mensaje disponible para reintento.
-- El consumidor trata order_id como clave idempotente, incluso tras un reenvío.
CREATE TABLE job_outbox (
    order_id TEXT NOT NULL PRIMARY KEY REFERENCES usage_ledger(order_id),
    payload TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) = 'object'),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (available_at >= 0),
    dispatched_at INTEGER CHECK (dispatched_at >= 0)
) STRICT;

CREATE INDEX job_outbox_due ON job_outbox(available_at) WHERE dispatched_at IS NULL;

CREATE TRIGGER job_outbox_verified_media
BEFORE INSERT ON job_outbox
BEGIN
    SELECT RAISE(ABORT, 'UPLOAD_NOT_VERIFIED')
    WHERE NOT EXISTS (
        SELECT 1 FROM upload_sessions AS s
        JOIN usage_ledger AS u ON u.order_id = s.order_id
        WHERE s.order_id = NEW.order_id AND s.status = 'completed' AND u.status = 'reserved'
    );
END;

-- El identificador corresponde a una entrega autenticada. Update de un mismo recurso
-- puede tener otra entrega. El Worker confirma el recurso vigente por API antes de aplicar.
CREATE TABLE webhook_events (
    provider TEXT NOT NULL DEFAULT 'mercadopago' CHECK (provider = 'mercadopago'),
    provider_mode TEXT NOT NULL CHECK (provider_mode IN ('sandbox', 'production')),
    event_id TEXT NOT NULL CHECK (length(event_id) BETWEEN 1 AND 192),
    resource_id TEXT NOT NULL CHECK (length(resource_id) BETWEEN 1 AND 192),
    topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 80),
    received_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (received_at >= 0),
    processed_at INTEGER CHECK (processed_at >= received_at),
    PRIMARY KEY (provider, provider_mode, event_id)
) STRICT;
