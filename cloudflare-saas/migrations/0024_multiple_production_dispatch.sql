-- La orden múltiple solo entra al buzón cuando todos los crudos están
-- confirmados en el NAS y la cuota ya fue consumida.
CREATE TRIGGER production_dispatch_multiple_on_sync
AFTER UPDATE OF status ON orders
WHEN OLD.status = 'queued' AND NEW.status = 'processing' AND NEW.kind = 'production'
BEGIN
    INSERT INTO production_dispatch(order_id, available_at)
    SELECT NEW.id, unixepoch()
    WHERE EXISTS (SELECT 1 FROM usage_ledger u WHERE u.order_id=NEW.id
                  AND u.user_id=NEW.user_id AND u.status='consumed')
      AND EXISTS (SELECT 1 FROM production_sources s WHERE s.order_id=NEW.id)
      AND NOT EXISTS (SELECT 1 FROM production_sources s
          LEFT JOIN production_source_uploads upload ON upload.source_id=s.id
          LEFT JOIN production_source_outbox outbox ON outbox.source_id=s.id
          WHERE s.order_id=NEW.id AND (upload.status IS NULL OR upload.status<>'completed'
              OR outbox.synced_at IS NULL))
    ON CONFLICT(order_id) DO NOTHING;
END;
