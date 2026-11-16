ALTER TABLE delivery_kits ADD COLUMN prores_bytes INTEGER
    CHECK (prores_bytes BETWEEN 1 AND 5497558138880);
ALTER TABLE delivery_kits ADD COLUMN prores_sha256 TEXT
    CHECK (length(prores_sha256) = 64 AND prores_sha256 NOT GLOB '*[^0-9a-f]*');
ALTER TABLE delivery_kits ADD COLUMN prores_r2_etag TEXT
    CHECK (length(prores_r2_etag) BETWEEN 1 AND 128);
ALTER TABLE delivery_kits ADD COLUMN prores_r2_version TEXT
    CHECK (
        (prores_bytes IS NULL AND prores_sha256 IS NULL
            AND prores_r2_etag IS NULL AND prores_r2_version IS NULL)
        OR (prores_bytes IS NOT NULL AND prores_sha256 IS NOT NULL
            AND prores_r2_etag IS NOT NULL AND prores_r2_version IS NOT NULL
            AND length(prores_r2_version) BETWEEN 1 AND 128)
    );

CREATE TRIGGER delivery_kit_prores_matches_order
BEFORE INSERT ON delivery_kits
WHEN EXISTS (
    SELECT 1 FROM orders o WHERE o.id = NEW.order_id AND o.user_id = NEW.user_id
        AND (o.output_format = 'prores4444') <> (NEW.prores_bytes IS NOT NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'DELIVERY_FORMAT_MISMATCH');
END;

CREATE TRIGGER delivery_kit_prores_immutable
BEFORE UPDATE OF prores_bytes, prores_sha256, prores_r2_etag, prores_r2_version ON delivery_kits
WHEN NEW.prores_bytes IS NOT OLD.prores_bytes OR NEW.prores_sha256 IS NOT OLD.prores_sha256
    OR NEW.prores_r2_etag IS NOT OLD.prores_r2_etag OR NEW.prores_r2_version IS NOT OLD.prores_r2_version
BEGIN
    SELECT RAISE(ABORT, 'DELIVERY_PRORES_IMMUTABLE');
END;
