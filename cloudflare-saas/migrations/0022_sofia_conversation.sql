CREATE TABLE sofia_conversation_turns (
    order_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    message TEXT NOT NULL CHECK (length(message) BETWEEN 1 AND 2048),
    wire_message TEXT NOT NULL CHECK (length(wire_message) BETWEEN 1 AND 2048),
    reply TEXT CHECK (reply IS NULL OR length(reply) BETWEEN 1 AND 8192),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (order_id, request_id),
    FOREIGN KEY (order_id, user_id) REFERENCES orders(id, user_id) ON DELETE CASCADE
) STRICT;

CREATE INDEX sofia_conversation_history
ON sofia_conversation_turns(order_id, user_id, created_at, request_id);
