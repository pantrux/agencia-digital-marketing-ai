-- Huella privada para acreditar la revisión rechazada y reconocer reintentos.
ALTER TABLE orders ADD COLUMN qa_rejected_master_sha256 TEXT
    CHECK (qa_rejected_master_sha256 IS NULL OR (
        kind = 'production' AND status = 'failed'
        AND length(qa_rejected_master_sha256) = 64
        AND qa_rejected_master_sha256 NOT GLOB '*[^0-9a-f]*'
    ));
