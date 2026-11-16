-- El receptor anterior puede seguir insertando eventos con los valores predeterminados.
ALTER TABLE webhook_events ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 0
    CHECK (retry_attempts BETWEEN 0 AND 8);
ALTER TABLE webhook_events ADD COLUMN retry_at INTEGER CHECK (retry_at >= received_at);

CREATE INDEX webhook_events_retry_due
ON webhook_events(provider, provider_mode, COALESCE(retry_at, received_at + 120), event_id)
WHERE processed_at IS NULL AND retry_attempts < 8;
