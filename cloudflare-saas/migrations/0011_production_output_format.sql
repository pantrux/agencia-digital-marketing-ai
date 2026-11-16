ALTER TABLE orders ADD COLUMN output_format TEXT NOT NULL DEFAULT 'mp4'
    CHECK (output_format = 'mp4' OR (
        output_format = 'prores4444' AND kind = 'production' AND plan_id = 'ilimitado'
    ));

CREATE TRIGGER order_output_format_immutable
BEFORE UPDATE OF output_format ON orders
WHEN NEW.output_format <> OLD.output_format
BEGIN
    SELECT RAISE(ABORT, 'OUTPUT_FORMAT_IMMUTABLE');
END;
