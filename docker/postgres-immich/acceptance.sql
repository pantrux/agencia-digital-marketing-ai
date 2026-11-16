-- Datos sintéticos propios; el controlador fija initialize y ON_ERROR_STOP.
\set ON_ERROR_STOP on
\if :initialize
CREATE EXTENSION vector VERSION '0.8.5';
CREATE EXTENSION vchord VERSION '1.1.1';
CREATE EXTENSION cube;
CREATE EXTENSION earthdistance;
CREATE TABLE agency_runtime_probe (id integer PRIMARY KEY, embedding vector(512) NOT NULL);
INSERT INTO agency_runtime_probe
SELECT i, ARRAY(SELECT (((i * 104729 + j * 13007) % 1000003)::double precision / 500001.5 - 1)::real
                FROM generate_series(1, 512) j)::vector(512)
FROM generate_series(1, 4096) s(i);
-- Se materializa antes de crear el índice; no puede usar el índice bajo prueba.
CREATE TABLE agency_runtime_oracle AS
SELECT q.id AS probe_id, p.id, p.distance
FROM agency_runtime_probe q
CROSS JOIN LATERAL (
    SELECT t.id, t.embedding <=> q.embedding AS distance
    FROM agency_runtime_probe t ORDER BY t.embedding <=> q.embedding, t.id LIMIT 10
) p WHERE q.id IN (1, 17, 42, 2048, 4096);
CREATE UNIQUE INDEX agency_oracle_pk ON agency_runtime_oracle(probe_id, id);
CREATE INDEX agency_probe_cosine_idx ON agency_runtime_probe
USING vchordrq (embedding vector_cosine_ops)
WITH (options = $options$
residual_quantization = false
rerank_in_table = false
[build.internal]
lists = []
spherical_centroids = true
$options$);
CREATE TABLE agency_runtime_sentinel(token text PRIMARY KEY, value text NOT NULL);
INSERT INTO agency_runtime_sentinel VALUES ('agency-postgres-immich-sql-v1', 'persistido');
ANALYZE agency_runtime_probe;
\endif
SET vchordrq.probes = '';
SET enable_seqscan = off;
SET jit = off;
DO $acceptance$
DECLARE
    q record;
    hit record;
    plan json;
    ids integer[];
    previous_distance double precision;
    threshold double precision;
BEGIN
    IF current_user <> 'agency_probe' OR current_database() <> 'agency_abi'
       OR current_setting('server_version_num') <> '160015'
       OR current_setting('data_checksums') <> 'on'
       OR current_setting('password_encryption') <> 'scram-sha-256'
       OR current_setting('shared_preload_libraries') <> 'vchord.so' THEN
        RAISE EXCEPTION 'Identidad/version/checksums/auth/precarga distintos del contrato';
    END IF;
    IF (SELECT extversion FROM pg_extension WHERE extname = 'vector') IS DISTINCT FROM '0.8.5'
       OR (SELECT extversion FROM pg_extension WHERE extname = 'vchord') IS DISTINCT FROM '1.1.1' THEN
        RAISE EXCEPTION 'Versiones de extensiones no acreditadas';
    END IF;
    IF abs(cube_distance(cube(ARRAY[0.0,0.0,0.0]::double precision[]), cube(ARRAY[1.0,2.0,2.0]::double precision[])) - 3) > 1e-9
       OR earth_distance(ll_to_earth(0,0), ll_to_earth(0,1)) NOT BETWEEN 111000 AND 112000 THEN
        RAISE EXCEPTION 'cube/earthdistance no funcionan';
    END IF;
    IF (SELECT count(*) FROM agency_runtime_probe) <> 4096
       OR EXISTS (SELECT FROM agency_runtime_probe WHERE vector_dims(embedding) <> 512 OR vector_norm(embedding) <= 0)
       OR (SELECT count(*) FROM agency_runtime_oracle) <> 50
       OR (SELECT value FROM agency_runtime_sentinel WHERE token = 'agency-postgres-immich-sql-v1') IS DISTINCT FROM 'persistido' THEN
        RAISE EXCEPTION 'Dataset/dimensiones/oraculo/sentinel no acreditados';
    END IF;
    IF (SELECT embedding <-> embedding FROM agency_runtime_probe WHERE id = 42) > 1e-6 THEN
        RAISE EXCEPTION 'Operador L2 no funciona';
    END IF;
    FOR q IN SELECT id, embedding FROM agency_runtime_probe WHERE id IN (1,17,42,2048,4096) ORDER BY id LOOP
        EXECUTE 'EXPLAIN (FORMAT JSON) SELECT id FROM agency_runtime_probe ORDER BY embedding <=> $1 LIMIT 10'
            INTO plan USING q.embedding;
        IF plan::text NOT LIKE '%agency_probe_cosine_idx%' OR plan::text NOT LIKE '%Index Scan%' THEN
            RAISE EXCEPTION 'La consulta no usa el indice propio';
        END IF;
        SELECT max(distance) INTO threshold FROM agency_runtime_oracle WHERE probe_id = q.id;
        ids := ARRAY[]::integer[];
        previous_distance := -1;
        FOR hit IN SELECT id, embedding <=> q.embedding AS distance FROM agency_runtime_probe
                   ORDER BY embedding <=> q.embedding LIMIT 10 LOOP
            IF hit.distance IN ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision)
               OR hit.distance < previous_distance - 1e-6 OR hit.distance > threshold + 1e-6 THEN
                RAISE EXCEPTION 'Resultado indexado diverge del oraculo o no es finito/ordenado';
            END IF;
            ids := array_append(ids, hit.id);
            previous_distance := hit.distance;
        END LOOP;
        IF cardinality(ids) <> 10 OR (SELECT count(DISTINCT x) FROM unnest(ids) x) <> 10 OR ids[1] <> q.id THEN
            RAISE EXCEPTION 'Top10 indexado incompleto/duplicado/sin self';
        END IF;
        IF (SELECT embedding <=> q.embedding FROM agency_runtime_probe WHERE id = ids[1]) > 1e-6 THEN
            RAISE EXCEPTION 'Self no tiene distancia cero';
        END IF;
    END LOOP;
END
$acceptance$;
SELECT json_build_object(
    'kind', 'sql_acceptance', 'verified', true,
    'cluster_id', (pg_control_system()).system_identifier::text,
    'server_version_num', current_setting('server_version_num')::integer,
    'vectors', (SELECT count(*) FROM agency_runtime_probe), 'dimension', 512,
    'queries', 5, 'index_verified', true,
    'sentinel', (SELECT value FROM agency_runtime_sentinel WHERE token = 'agency-postgres-immich-sql-v1'),
    'extensions', (SELECT json_object_agg(extname, extversion) FROM pg_extension WHERE extname IN ('vector','vchord','cube','earthdistance'))
);
