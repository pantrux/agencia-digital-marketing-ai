-- La reserva autenticada conserva su origen después de cerrar la sesión de Google.
-- Los pedidos existentes permanecen sin procedencia acreditada.
ALTER TABLE orders ADD COLUMN portal_started_at INTEGER
    CHECK (portal_started_at IS NULL OR (kind = 'production' AND portal_started_at = created_at));
