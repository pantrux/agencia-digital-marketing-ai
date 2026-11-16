-- Duración del máster aprobado, incluida en los manifiestos firmados v2.
ALTER TABLE delivery_kits ADD COLUMN duration_ms INTEGER
    CHECK (duration_ms IS NULL OR duration_ms BETWEEN 1 AND 600100);
