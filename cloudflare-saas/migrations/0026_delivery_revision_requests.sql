-- Una solicitud de revisión apunta a una entrega existente y nunca la modifica.
CREATE TABLE delivery_revision_requests (
    id TEXT PRIMARY KEY CHECK(length(id)=36),
    order_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    base_kit_id TEXT NOT NULL REFERENCES delivery_kits(id),
    request_key TEXT NOT NULL CHECK(length(request_key) BETWEEN 1 AND 128),
    instructions TEXT NOT NULL CHECK(length(instructions) BETWEEN 1 AND 800),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','completed','failed')),
    result_kit_id TEXT REFERENCES delivery_kits(id),
    lease TEXT,
    lease_until INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
    failure_stage TEXT CHECK(failure_stage IN ('direction','quality')),
    created_at INTEGER NOT NULL CHECK(created_at>=0),
    updated_at INTEGER NOT NULL CHECK(updated_at>=created_at),
    FOREIGN KEY(order_id,user_id) REFERENCES orders(id,user_id),
    UNIQUE(order_id,request_key),
    CHECK((status='completed' AND result_kit_id IS NOT NULL AND result_kit_id<>base_kit_id)
       OR (status<>'completed' AND result_kit_id IS NULL)),
    CHECK((status='processing' AND lease IS NOT NULL AND lease_until>updated_at)
       OR (status<>'processing' AND lease IS NULL AND lease_until IS NULL)),
    CHECK((status='failed' AND failure_stage IS NOT NULL) OR (status<>'failed' AND failure_stage IS NULL))
) STRICT;

CREATE UNIQUE INDEX delivery_revision_one_active ON delivery_revision_requests(order_id)
WHERE status IN ('pending','processing');
CREATE UNIQUE INDEX delivery_revision_one_result ON delivery_revision_requests(result_kit_id)
WHERE result_kit_id IS NOT NULL;

CREATE TRIGGER delivery_revision_verify_insert
BEFORE INSERT ON delivery_revision_requests
BEGIN
    SELECT RAISE(ABORT, 'REVISION_BASE_INVALIDA') WHERE NEW.status<>'pending' OR NOT EXISTS (
        SELECT 1 FROM delivery_kits k JOIN orders o ON o.id=k.order_id AND o.user_id=k.user_id
        WHERE k.id=NEW.base_kit_id AND k.order_id=NEW.order_id AND k.user_id=NEW.user_id
          AND k.provider_mode=COALESCE(o.provider_mode,'production') AND o.status='completed'
    );
END;

CREATE TRIGGER delivery_revision_verify_update
BEFORE UPDATE ON delivery_revision_requests
BEGIN
    SELECT RAISE(ABORT, 'REVISION_CAMBIO_INVALIDO') WHERE
        NEW.id<>OLD.id OR NEW.order_id<>OLD.order_id OR NEW.user_id<>OLD.user_id
        OR NEW.base_kit_id<>OLD.base_kit_id OR NEW.request_key<>OLD.request_key
        OR NEW.instructions<>OLD.instructions OR NEW.created_at<>OLD.created_at
        OR NEW.updated_at<OLD.updated_at
        OR NOT ((OLD.status='pending' AND NEW.status='processing' AND NEW.attempts=OLD.attempts+1)
            OR (OLD.status='pending' AND NEW.status='failed' AND NEW.attempts=OLD.attempts)
            OR (OLD.status='processing' AND NEW.status IN ('completed','failed'))
            OR (OLD.status='processing' AND NEW.status='processing'
                AND OLD.lease_until>NEW.updated_at AND NEW.lease=OLD.lease
                AND NEW.lease_until>OLD.lease_until AND NEW.attempts=OLD.attempts)
            OR (OLD.status='processing' AND NEW.status='processing'
                AND OLD.lease_until<=NEW.updated_at AND NEW.lease<>OLD.lease
                AND NEW.attempts=OLD.attempts+1))
        OR (NEW.status='completed' AND NOT EXISTS (
            SELECT 1 FROM delivery_kits k JOIN orders o ON o.id=k.order_id AND o.user_id=k.user_id
            WHERE k.id=NEW.result_kit_id AND k.order_id=NEW.order_id AND k.user_id=NEW.user_id
              AND k.provider_mode=COALESCE(o.provider_mode,'production')
        ));
END;
