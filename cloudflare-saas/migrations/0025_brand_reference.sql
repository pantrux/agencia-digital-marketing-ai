CREATE TABLE studio_reference_context (
  order_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  source_url TEXT NOT NULL CHECK(length(source_url) <= 2048),
  status TEXT NOT NULL CHECK(status IN ('available', 'unavailable')),
  title TEXT NOT NULL CHECK(length(title) <= 500),
  description TEXT NOT NULL CHECK(length(description) <= 500),
  excerpt TEXT NOT NULL CHECK(length(excerpt) <= 1000),
  updated_at INTEGER NOT NULL,
  FOREIGN KEY(order_id, user_id) REFERENCES orders(id, user_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE studio_reference_rate (
  user_id TEXT PRIMARY KEY,
  last_started INTEGER NOT NULL
) STRICT;
