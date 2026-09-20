-- Runs once, on a fresh postgres data volume only (docker-entrypoint-initdb.d
-- scripts don't re-run against an already-initialized volume). Airflow's own
-- metadata lives in a separate database on this same Postgres instance,
-- rather than a second stateful container.
CREATE DATABASE airflow;
