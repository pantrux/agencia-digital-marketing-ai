CREATE TABLE production_manual_adjustments (
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK (number >= 1),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 3),
    review_sha256 TEXT NOT NULL CHECK (length(review_sha256) = 64 AND review_sha256 NOT GLOB '*[^0-9a-f]*'),
    instructions TEXT NOT NULL CHECK (length(instructions) BETWEEN 1 AND 800),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready', 'needs_review')),
    result_sha256 TEXT CHECK (length(result_sha256) = 64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'),
    requested_at INTEGER NOT NULL CHECK (requested_at >= 0),
    resolved_at INTEGER CHECK (resolved_at >= requested_at),
    PRIMARY KEY (order_id, number),
    UNIQUE (order_id, request_key),
    CHECK ((status = 'pending' AND resolved_at IS NULL AND result_sha256 IS NULL)
        OR (status <> 'pending' AND resolved_at IS NOT NULL AND result_sha256 IS NOT NULL)),
    CHECK (status <> 'ready' OR result_sha256 <> review_sha256)
) STRICT;

CREATE UNIQUE INDEX production_manual_adjustments_pending
ON production_manual_adjustments(order_id) WHERE status = 'pending';

-- El resultado y la guarda de entrega cambian dentro de la misma sentencia.
CREATE TRIGGER production_manual_adjustments_resolve
AFTER UPDATE OF status ON production_manual_adjustments
BEGIN
    SELECT RAISE(ABORT, 'MANUAL_ADJUSTMENT_STALE')
    WHERE OLD.status <> 'pending' OR NEW.status = 'pending' OR NOT EXISTS (
        SELECT 1 FROM production_manual_reviews
        WHERE order_id = OLD.order_id AND revision = OLD.revision AND direction_sha256 = OLD.review_sha256
    );
    UPDATE production_manual_reviews SET direction_sha256 = NEW.result_sha256, recorded_at = NEW.resolved_at
    WHERE order_id = OLD.order_id AND NEW.status = 'needs_review';
    DELETE FROM production_manual_reviews WHERE order_id = OLD.order_id AND NEW.status = 'ready';
END;
