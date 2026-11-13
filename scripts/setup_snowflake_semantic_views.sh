#!/usr/bin/env bash
# One-time provisioning for dogfooding Snowflake Semantic Views
# (`semantic.vendor: snowflake`). Run by a maintainer whose `snow` CLI
# connection can use ACCOUNTADMIN. dex never deploys a semantic view: its
# `transform apply` writes YAML into the repository and stops there. The views
# a dogfood run reads are deployed by a maintainer, outside dex, with
# scripts/snowflake_semantic_views.py, into the scratch schema this script
# creates.
#
# What it sets up:
#   - DEX_SV_WH: an X-Small warehouse, 60s auto-suspend, statement timeout;
#     the compute both the deploy script and a dogfood run use
#   - DEX_SV_MONITOR: a resource monitor that SUSPENDs the warehouse at its
#     monthly credit quota (default 5)
#   - DEX_SV: a TRANSIENT database with zero time-travel retention, holding
#     the schema FIXTURES that the fixture views are deployed into. It is a
#     database of its own, apart from the CI scratch database, so the CI role's
#     write grants there never reach it.
#   - DEX_SV_DEPLOYER: the maintainer role, granted to the user running this
#     script. The only role that deploys: CREATE SEMANTIC VIEW (and CREATE
#     TABLE, for the one helper table a range relationship needs) on
#     DEX_SV.FIXTURES, plus read on the shared TPC-H sample data the fixtures
#     sit on. It calls SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML, verify_only
#     included.
#   - DEX_SV_READER: dex's read access, granted to DEX_CI_ROLE (created by
#     scripts/setup_snowflake_ci.sh), so the CI and dev identities read and
#     query the deployed views and cannot create, replace or drop one.
#     SELECT (query) and REFERENCES (DESCRIBE SEMANTIC VIEW, GET_DDL, private
#     items) on every semantic view in DEX_SV.FIXTURES, current and future.
#   - DEX_SV_SELECT_ONLY: SELECT alone, with no REFERENCES, granted to the
#     DEX_DEV user, to exercise what a permission-limited reader of a deployed
#     view can and cannot see. A session uses it alone with
#     `USE ROLE DEX_SV_SELECT_ONLY; USE SECONDARY ROLES NONE;`.
#
# Everything account-specific is a parameter, so nothing private lives in this
# script. Idempotent: safe to re-run; existing resources are left in place.
#
# Usage:
#   scripts/setup_snowflake_semantic_views.sh [snow-connection-name]
#
# The optional argument names the maintainer's admin `snow` connection; the
# default connection is used otherwise. Run scripts/setup_snowflake_ci.sh
# first: DEX_SV_READER is granted to DEX_CI_ROLE and DEX_SV_SELECT_ONLY to the
# DEX_DEV user, both created there.
#
# Overrides via environment: DEX_SV_CREDIT_QUOTA (monthly credits before the
# monitor suspends, default 5; applies only when the monitor is created, so
# change an existing one with ALTER), DEX_SV_STATEMENT_TIMEOUT
# (warehouse-level seconds, default 300).

set -euo pipefail

ADMIN_CONN="${1:-}"

CREDIT_QUOTA="${DEX_SV_CREDIT_QUOTA:-5}"
STATEMENT_TIMEOUT="${DEX_SV_STATEMENT_TIMEOUT:-300}"
WAREHOUSE="DEX_SV_WH"
MONITOR="DEX_SV_MONITOR"
DATABASE="DEX_SV"
SCHEMA="FIXTURES"
DEPLOYER="DEX_SV_DEPLOYER"
READER="DEX_SV_READER"
SELECT_ONLY="DEX_SV_SELECT_ONLY"
CI_ROLE="DEX_CI_ROLE"
DEV_USER="DEX_DEV"

SNOW=(snow sql)
if [[ -n "$ADMIN_CONN" ]]; then
  SNOW=(snow sql -c "$ADMIN_CONN")
fi
sql() { "${SNOW[@]}" -q "USE ROLE ACCOUNTADMIN; $1"; }

# The deployer role goes to whoever runs this, named by the session itself
# rather than by an argument, so the script carries no user name.
# Quoted as an identifier, with any embedded double quote doubled, so a user
# name with an @, a dot or lower case resolves to exactly this user.
MAINTAINER=$("${SNOW[@]}" --format json -q "SELECT CURRENT_USER() AS U" \
  | python3 -c 'import json,sys; u=json.load(sys.stdin)[0]["U"]; print(chr(34) + u.replace(chr(34), chr(34) * 2) + chr(34))')

echo "== dex Snowflake Semantic Views setup =="
echo "   warehouse:   ${WAREHOUSE} (X-Small, auto-suspend 60s, timeout ${STATEMENT_TIMEOUT}s)"
echo "   monitor:     ${MONITOR} (${CREDIT_QUOTA} credits/month if created, suspends)"
echo "   schema:      ${DATABASE}.${SCHEMA} (transient database, retention 0)"
echo "   deployer:    ${DEPLOYER} -> ${MAINTAINER}"
echo "   reader:      ${READER} -> ${CI_ROLE}"
echo "   select-only: ${SELECT_ONLY} -> ${DEV_USER}"
echo

echo "-- Warehouse (suspended until used)"
sql "CREATE WAREHOUSE IF NOT EXISTS ${WAREHOUSE}
       WAREHOUSE_SIZE = 'XSMALL'
       AUTO_SUSPEND = 60
       AUTO_RESUME = TRUE
       INITIALLY_SUSPENDED = TRUE
       STATEMENT_TIMEOUT_IN_SECONDS = ${STATEMENT_TIMEOUT}
       COMMENT = 'dex semantic view dogfood; capped by ${MONITOR}';"

echo "-- Resource monitor (the hard backstop: suspends the warehouse at quota)"
sql "CREATE RESOURCE MONITOR IF NOT EXISTS ${MONITOR}
       WITH CREDIT_QUOTA = ${CREDIT_QUOTA}
       FREQUENCY = MONTHLY
       START_TIMESTAMP = IMMEDIATELY
       TRIGGERS ON 80 PERCENT DO NOTIFY
                ON 100 PERCENT DO SUSPEND_IMMEDIATE;
     ALTER WAREHOUSE ${WAREHOUSE} SET RESOURCE_MONITOR = ${MONITOR};"

echo "-- Scratch database and schema (transient, zero retention)"
sql "CREATE TRANSIENT DATABASE IF NOT EXISTS ${DATABASE}
       DATA_RETENTION_TIME_IN_DAYS = 0
       COMMENT = 'dex semantic view fixtures; deployed by ${DEPLOYER}, safe to drop';
     CREATE SCHEMA IF NOT EXISTS ${DATABASE}.${SCHEMA};"

echo "-- Sample data (shared TPC-H; mounted if absent)"
sql "CREATE DATABASE IF NOT EXISTS SNOWFLAKE_SAMPLE_DATA
       FROM SHARE SFC_SAMPLES.SAMPLE_DATA;"

echo "-- Deployer role (the only role that creates a semantic view here)"
sql "CREATE ROLE IF NOT EXISTS ${DEPLOYER};
     GRANT USAGE ON WAREHOUSE ${WAREHOUSE} TO ROLE ${DEPLOYER};
     GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE ${DEPLOYER};
     GRANT USAGE ON DATABASE ${DATABASE} TO ROLE ${DEPLOYER};
     GRANT USAGE ON SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${DEPLOYER};
     GRANT CREATE SEMANTIC VIEW ON SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${DEPLOYER};
     GRANT CREATE TABLE ON SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${DEPLOYER};
     GRANT ROLE ${DEPLOYER} TO USER ${MAINTAINER};"

echo "-- Reader role (query and describe; nothing that creates, alters or drops)"
sql "CREATE ROLE IF NOT EXISTS ${READER};
     GRANT USAGE ON WAREHOUSE ${WAREHOUSE} TO ROLE ${READER};
     GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE ${READER};
     GRANT USAGE ON DATABASE ${DATABASE} TO ROLE ${READER};
     GRANT USAGE ON SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${READER};
     GRANT SELECT, REFERENCES ON ALL SEMANTIC VIEWS IN SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${READER};
     GRANT SELECT, REFERENCES ON FUTURE SEMANTIC VIEWS IN SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${READER};
     GRANT SELECT ON ALL TABLES IN SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${READER};
     GRANT SELECT ON FUTURE TABLES IN SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${READER};
     GRANT ROLE ${READER} TO ROLE ${CI_ROLE};"

echo "-- Select-only role (no REFERENCES: the permission-limited reader)"
sql "CREATE ROLE IF NOT EXISTS ${SELECT_ONLY};
     GRANT USAGE ON WAREHOUSE ${WAREHOUSE} TO ROLE ${SELECT_ONLY};
     GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE ${SELECT_ONLY};
     GRANT USAGE ON DATABASE ${DATABASE} TO ROLE ${SELECT_ONLY};
     GRANT USAGE ON SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${SELECT_ONLY};
     GRANT SELECT ON ALL SEMANTIC VIEWS IN SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${SELECT_ONLY};
     GRANT SELECT ON FUTURE SEMANTIC VIEWS IN SCHEMA ${DATABASE}.${SCHEMA} TO ROLE ${SELECT_ONLY};
     GRANT ROLE ${SELECT_ONLY} TO USER ${DEV_USER};"

echo
echo "== Done. Deploy the fixtures with:"
echo "   uv run scripts/snowflake_semantic_views.py deploy${ADMIN_CONN:+ --connection ${ADMIN_CONN}}"
echo "== Verify the reader cannot deploy (as a DEX_CI_ROLE identity, this fails):"
echo "   CALL SYSTEM\$CREATE_SEMANTIC_VIEW_FROM_YAML('${DATABASE}.${SCHEMA}', '<yaml>', TRUE);"
echo "== ${MONITOR} suspends ${WAREHOUSE} for the month at its quota."
