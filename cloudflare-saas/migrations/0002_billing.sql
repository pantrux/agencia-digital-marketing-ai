-- Reservas de checkout de prueba y hechos de cobro observados por API.
CREATE TABLE billing_checkout (
    subscription_id TEXT NOT NULL PRIMARY KEY REFERENCES subscriptions(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    payer_email TEXT NOT NULL CHECK (length(payer_email) BETWEEN 3 AND 320),
    amount_clp INTEGER NOT NULL CHECK (amount_clp > 0),
    frequency INTEGER NOT NULL CHECK (frequency BETWEEN 1 AND 365),
    frequency_type TEXT NOT NULL CHECK (frequency_type IN ('days', 'months')),
    checkout_url TEXT CHECK (length(checkout_url) BETWEEN 1 AND 2048),
    provider_version INTEGER CHECK (provider_version >= 0),
    provider_mandate TEXT NOT NULL DEFAULT 'pending'
        CHECK (provider_mandate IN ('pending', 'authorized', 'paused', 'canceled')),
    provider_updated_at INTEGER CHECK (provider_updated_at > 0),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    closed_at INTEGER CHECK (closed_at >= created_at),
    UNIQUE (user_id, idempotency_key)
) STRICT;

CREATE UNIQUE INDEX billing_checkout_open_user ON billing_checkout(user_id) WHERE closed_at IS NULL;

CREATE TABLE billing_payments (
    payment_id TEXT NOT NULL PRIMARY KEY,
    invoice_id TEXT NOT NULL,
    subscription_id TEXT NOT NULL REFERENCES billing_checkout(subscription_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'failed', 'refunded', 'charged_back')),
    amount_clp INTEGER NOT NULL CHECK (amount_clp > 0),
    period_start INTEGER NOT NULL CHECK (period_start > 0),
    period_end INTEGER NOT NULL CHECK (period_end > period_start),
    provider_updated_at INTEGER NOT NULL CHECK (provider_updated_at > 0)
) STRICT;

CREATE INDEX billing_payments_subscription ON billing_payments(subscription_id, period_start DESC);
