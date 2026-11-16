-- Metadatos previos a las copias; no crea usuarios, pagos ni tablas de negocio.
-- La identidad fija impide reutilizar esta migración para otra base por parámetros.
CREATE TABLE IF NOT EXISTS _agency_resource_owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    project TEXT NOT NULL CHECK (project = 'agencia-digital-marketing-ai'),
    resource TEXT NOT NULL CHECK (resource = 'studio-saas-db'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    created_at_utc TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Conservar la fecha original y los datos existentes al repetir una aplicación propia.
INSERT INTO _agency_resource_owner (id, project, resource, schema_version)
VALUES (1, 'agencia-digital-marketing-ai', 'studio-saas-db', 1)
ON CONFLICT (id) DO NOTHING;
