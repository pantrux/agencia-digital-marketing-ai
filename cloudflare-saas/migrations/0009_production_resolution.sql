ALTER TABLE orders ADD COLUMN output_resolution TEXT NOT NULL DEFAULT '1080p'
    CHECK (output_resolution = '1080p' OR (
        output_resolution = '4k' AND kind = 'production' AND plan_id IN ('ultra', 'ilimitado')
    ));

CREATE TRIGGER order_output_resolution_immutable
BEFORE UPDATE OF output_resolution ON orders
WHEN NEW.output_resolution <> OLD.output_resolution
BEGIN
    SELECT RAISE(ABORT, 'OUTPUT_RESOLUTION_IMMUTABLE');
END;
