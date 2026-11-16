-- El brief queda fijado al confirmar la orden recibida por la VM de medios.
CREATE TABLE production_briefs (
    order_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    objective TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 800),
    audience TEXT NOT NULL CHECK (length(audience) BETWEEN 1 AND 480),
    tone TEXT NOT NULL CHECK (length(tone) <= 240),
    confirmed_at INTEGER NOT NULL DEFAULT (unixepoch()) CHECK (confirmed_at >= 0),
    FOREIGN KEY (order_id, user_id) REFERENCES orders(id, user_id)
) STRICT;
