ALTER TABLE orders ADD COLUMN output_edit TEXT NOT NULL DEFAULT 'full'
    CHECK (output_edit = 'full' OR (output_edit = 'reel' AND kind = 'production'));

CREATE TRIGGER order_output_edit_immutable
BEFORE UPDATE OF output_edit ON orders
WHEN NEW.output_edit <> OLD.output_edit
BEGIN
    SELECT RAISE(ABORT, 'OUTPUT_EDIT_IMMUTABLE');
END;
