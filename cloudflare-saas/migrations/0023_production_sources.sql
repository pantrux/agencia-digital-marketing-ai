CREATE TABLE production_source_sets (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    manifest TEXT NOT NULL CHECK (json_valid(manifest) AND json_type(manifest) = 'array'),
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    FOREIGN KEY (order_id, user_id) REFERENCES orders(id, user_id) ON DELETE CASCADE
) STRICT;

-- Una identidad estable por archivo permite reintentar cada transferencia sin
-- confundir archivos con el mismo nombre o contenido. Se crea en la misma
-- transacción que el manifiesto, antes de aceptar cualquier carga.
CREATE TABLE production_sources (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES production_source_sets(order_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0 AND position < 32),
    name TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK (bytes > 0 AND bytes <= 1073741824),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    extension TEXT NOT NULL CHECK (extension IN ('mp4', 'mov', 'webm')),
    UNIQUE (order_id, position)
) STRICT;

-- Cada transferencia tiene su propia concesión y se puede reanudar sin
-- asociar el estado de una fuente con el de otra fuente de la misma orden.
CREATE TABLE production_source_uploads (
    source_id TEXT PRIMARY KEY REFERENCES production_sources(id) ON DELETE CASCADE,
    object_key TEXT NOT NULL UNIQUE,
    upload_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('creating', 'open', 'completing', 'completed', 'aborting', 'aborted')),
    operation_id TEXT,
    lease_until INTEGER CHECK (lease_until >= 0),
    object_etag TEXT,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    expires_at INTEGER NOT NULL CHECK (expires_at > created_at),
    CHECK ((operation_id IS NULL) = (lease_until IS NULL)),
    CHECK (status NOT IN ('open', 'completing', 'completed') OR upload_id IS NOT NULL),
    CHECK (status <> 'completed' OR object_etag IS NOT NULL)
) STRICT;

-- Esta cola se entrega al consumidor compatible con conjuntos. No se mezcla
-- con job_outbox, cuyo contrato admite únicamente un archivo por orden.
CREATE TABLE production_source_outbox (
    source_id TEXT PRIMARY KEY REFERENCES production_sources(id) ON DELETE CASCADE,
    payload TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) = 'object'),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (available_at >= 0),
    dispatched_at INTEGER CHECK (dispatched_at >= 0),
    synced_at INTEGER CHECK (synced_at >= 0)
) STRICT;

CREATE INDEX production_source_outbox_due ON production_source_outbox(available_at)
    WHERE dispatched_at IS NULL;

-- Las verificaciones de calidad y entrega usan la fuente principal como
-- ancla, siempre que el conjunto completo haya llegado al NAS.
CREATE VIEW production_primary_sources AS
SELECT s.order_id,s.user_id,s.id,s.sha256,s.status FROM upload_sessions s
UNION ALL
SELECT source.order_id,source_set.user_id,source.id,source.sha256,upload.status
FROM production_sources source JOIN production_source_sets source_set ON source_set.order_id=source.order_id
JOIN production_source_uploads upload ON upload.source_id=source.id
WHERE source.position=0 AND NOT EXISTS (
    SELECT 1 FROM production_sources other
    LEFT JOIN production_source_uploads up ON up.source_id=other.id
    LEFT JOIN production_source_outbox pending ON pending.source_id=other.id
    WHERE other.order_id=source.order_id AND (up.status IS NULL OR up.status<>'completed' OR pending.synced_at IS NULL)
);

CREATE TRIGGER production_source_set_materialize
AFTER INSERT ON production_source_sets
BEGIN
    INSERT INTO production_sources (id, order_id, position, name, bytes, sha256, extension)
    SELECT lower(hex(randomblob(4)) || '-' || hex(randomblob(2)) || '-4' ||
                 substr(hex(randomblob(2)), 2, 3) || '-' ||
                 substr('89ab', (random() & 3) + 1, 1) || substr(hex(randomblob(2)), 2, 3) || '-' ||
                 hex(randomblob(6))),
           NEW.order_id, CAST(j.key AS INTEGER), json_extract(j.value, '$.name'),
           json_extract(j.value, '$.bytes'), json_extract(j.value, '$.sha256'),
           json_extract(j.value, '$.extension')
    FROM json_each(NEW.manifest) AS j;
END;

-- Cada archivo verificado puede copiarse al NAS mientras los demás se suben.
-- El despacho de producción permanece condicionado al conjunto completo.
CREATE TRIGGER production_source_set_ready
AFTER UPDATE OF status ON production_source_uploads
WHEN NEW.status = 'completed' AND OLD.status <> 'completed'
BEGIN
    INSERT INTO production_source_outbox(source_id,payload)
    SELECT source.id, json_object('job_id',o.id,'asset_id',source.id,'project_slug',o.project_slug,
        'extension',source.extension,'bytes',source.bytes,'sha256',source.sha256,'object_key',upload.object_key)
    FROM production_sources source
    JOIN production_source_uploads upload ON upload.source_id=source.id
    JOIN production_source_sets source_set ON source_set.order_id=source.order_id
    JOIN orders o ON o.id=source_set.order_id AND o.user_id=source_set.user_id
    JOIN usage_ledger ledger ON ledger.order_id=o.id AND ledger.user_id=o.user_id
    WHERE source.id=NEW.source_id
      AND o.status='queued' AND ledger.status='reserved'
    ON CONFLICT(source_id) DO NOTHING;
END;
