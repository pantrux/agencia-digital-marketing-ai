-- Reserva durable de la producción después de verificar la carga y consumir cuota.
CREATE TABLE production_dispatch (
    order_id TEXT NOT NULL PRIMARY KEY REFERENCES orders(id),
    available_at INTEGER NOT NULL CHECK (available_at >= 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease TEXT CHECK (lease IS NULL OR length(lease) = 36),
    received_at INTEGER CHECK (received_at IS NULL OR received_at >= 0),
    CHECK (received_at IS NULL OR lease IS NULL)
) STRICT;

CREATE INDEX production_dispatch_waiting
    ON production_dispatch(available_at, order_id) WHERE received_at IS NULL;

CREATE TRIGGER production_dispatch_on_sync
AFTER UPDATE OF status ON orders
WHEN OLD.status = 'queued' AND NEW.status = 'processing' AND NEW.kind = 'production'
BEGIN
    INSERT INTO production_dispatch(order_id, available_at)
    SELECT NEW.id, unixepoch()
    WHERE EXISTS (SELECT 1 FROM usage_ledger u WHERE u.order_id = NEW.id
                  AND u.user_id = NEW.user_id AND u.status = 'consumed')
      AND EXISTS (SELECT 1 FROM upload_sessions s WHERE s.order_id = NEW.id
                  AND s.user_id = NEW.user_id AND s.status = 'completed')
      AND EXISTS (SELECT 1 FROM job_outbox j WHERE j.order_id = NEW.id)
    ON CONFLICT(order_id) DO NOTHING;
END;

-- Recupera las órdenes que ya sincronizaron medios antes de esta migración.
INSERT INTO production_dispatch(order_id, available_at)
SELECT o.id, unixepoch()
FROM orders o
JOIN usage_ledger u ON u.order_id = o.id AND u.user_id = o.user_id AND u.status = 'consumed'
JOIN upload_sessions s ON s.order_id = o.id AND s.user_id = o.user_id AND s.status = 'completed'
JOIN job_outbox j ON j.order_id = o.id
WHERE o.kind = 'production' AND o.status = 'processing';
