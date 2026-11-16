-- Manifiestos inmutables de objetos ya publicados en el bucket privado.
CREATE TABLE delivery_kits (
    id TEXT NOT NULL PRIMARY KEY CHECK (length(id) = 36),
    order_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    provider_mode TEXT NOT NULL CHECK (provider_mode IN ('sandbox', 'production')),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    bytes INTEGER NOT NULL CHECK (bytes BETWEEN 1 AND 5497558138880),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    r2_etag TEXT NOT NULL CHECK (length(r2_etag) BETWEEN 1 AND 128),
    r2_version TEXT NOT NULL CHECK (length(r2_version) BETWEEN 1 AND 128),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    FOREIGN KEY (order_id, user_id) REFERENCES orders(id, user_id)
) STRICT;

CREATE INDEX delivery_kits_user_latest
    ON delivery_kits(user_id, provider_mode, created_at DESC, id DESC);
