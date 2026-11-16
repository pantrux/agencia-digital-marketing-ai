-- Los abortos históricos y el Worker anterior conservan el motivo desconocido.
ALTER TABLE upload_sessions ADD COLUMN abort_reason TEXT
    CHECK (abort_reason IS NULL OR (status = 'aborted' AND abort_reason IN (
        'cancelled', 'expired', 'metadata_mismatch', 'sha256_mismatch'
    )));
