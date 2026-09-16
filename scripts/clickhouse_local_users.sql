-- Local-only identities layered over the shared parameterized data fixture.
DROP USER IF EXISTS dex_ro;
DROP USER IF EXISTS dbt_dev;

CREATE USER dex_ro IDENTIFIED WITH plaintext_password BY 'dex_ro';
GRANT SELECT ON app.* TO dex_ro;
GRANT SELECT ON system.tables TO dex_ro;
GRANT SELECT ON system.columns TO dex_ro;
GRANT SELECT ON system.databases TO dex_ro;
GRANT SELECT ON system.settings TO dex_ro;
GRANT SELECT ON system.grants TO dex_ro;
GRANT SELECT ON system.role_grants TO dex_ro;
GRANT SELECT ON system.asynchronous_metrics TO dex_ro;
GRANT SELECT ON system.clusters TO dex_ro;

CREATE USER dbt_dev IDENTIFIED WITH plaintext_password BY 'dbt_dev';
GRANT SELECT ON app.* TO dbt_dev;
GRANT SELECT ON system.tables TO dbt_dev;
GRANT SELECT ON system.columns TO dbt_dev;
GRANT SELECT ON system.databases TO dbt_dev;
GRANT SELECT, INSERT, ALTER, CREATE TABLE, CREATE VIEW, DROP TABLE, DROP VIEW,
TRUNCATE, OPTIMIZE ON dbt_dev.* TO dbt_dev;
GRANT CREATE DATABASE ON dbt_dev.* TO dbt_dev;
