CREATE TABLE brand_profiles (
    id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    provider_mode TEXT NOT NULL CHECK (provider_mode IN ('sandbox', 'production')),
    name_key TEXT NOT NULL CHECK (length(name_key) BETWEEN 1 AND 240),
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    audience TEXT NOT NULL CHECK (length(audience) <= 480),
    tone TEXT NOT NULL CHECK (length(tone) <= 240),
    UNIQUE (user_id, provider_mode, name_key),
    UNIQUE (id, user_id, provider_mode)
) STRICT;

CREATE TABLE order_brand_profiles (
    order_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider_mode TEXT NOT NULL,
    brand_id TEXT NOT NULL,
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    audience TEXT NOT NULL CHECK (length(audience) <= 480),
    tone TEXT NOT NULL CHECK (length(tone) <= 240),
    FOREIGN KEY (order_id, user_id) REFERENCES orders(id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (brand_id, user_id, provider_mode) REFERENCES brand_profiles(id, user_id, provider_mode)
) STRICT;

CREATE INDEX order_brand_profiles_brand ON order_brand_profiles(brand_id, user_id, provider_mode);

CREATE TRIGGER order_brand_profiles_scope
BEFORE INSERT ON order_brand_profiles
WHEN NOT EXISTS (
    SELECT 1 FROM orders
    WHERE id = NEW.order_id AND user_id = NEW.user_id AND provider_mode = NEW.provider_mode
        AND kind = 'production' AND plan_id = 'ilimitado' AND status = 'queued'
)
BEGIN
    SELECT RAISE(ABORT, 'BRAND_ORDER_SCOPE');
END;

CREATE TRIGGER order_brand_profiles_immutable
BEFORE UPDATE ON order_brand_profiles
BEGIN
    SELECT RAISE(ABORT, 'BRAND_ORDER_IMMUTABLE');
END;
