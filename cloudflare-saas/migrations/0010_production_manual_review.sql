-- Conserva la decisión de Valeria sin cerrar el pedido ni liberar su cupo.
CREATE TABLE production_manual_reviews (
    order_id TEXT NOT NULL PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 3),
    direction_sha256 TEXT NOT NULL CHECK (
        length(direction_sha256) = 64 AND direction_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    recorded_at INTEGER NOT NULL CHECK (recorded_at >= 0)
) STRICT;

CREATE TRIGGER delivery_kit_requires_review_clearance
BEFORE INSERT ON delivery_kits
WHEN EXISTS (SELECT 1 FROM production_manual_reviews r WHERE r.order_id = NEW.order_id)
BEGIN
    SELECT RAISE(ABORT, 'PRODUCTION_MANUAL_REVIEW');
END;
