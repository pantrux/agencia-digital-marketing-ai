-- Señales reportadas por el navegador. Cero indica ausencia de reporte.
ALTER TABLE upload_sessions ADD COLUMN direct_put_403_observed INTEGER NOT NULL DEFAULT 0
    CHECK (direct_put_403_observed IN (0, 1));
ALTER TABLE upload_sessions ADD COLUMN direct_put_no_response_observed INTEGER NOT NULL DEFAULT 0
    CHECK (direct_put_no_response_observed IN (0, 1));
