#!/usr/bin/env bash
# One-time provisioning for the Snowflake connector: the local dev/test loop
# and the live integration suite (.github/workflows/integration.yml). Run by a
# maintainer whose `snow` CLI connection can use ACCOUNTADMIN and with gh
# authenticated against the repository.
#
# What it sets up, keyless wherever the platform allows it:
#   - DEX_CI_WH: an X-Small warehouse, 60s auto-suspend, statement timeout;
#     the only warehouse dex CI and dev runs are granted
#   - DEX_CI_MONITOR: a resource monitor that SUSPENDs the warehouse at its
#     monthly credit quota (the hard cost backstop; nothing dex does can
#     outspend it)
#   - DEX_CI: a TRANSIENT scratch database with zero time-travel retention
#     (the only place dex may write; transient means no fail-safe storage)
#   - DEX_CI_ROLE: read on SNOWFLAKE_SAMPLE_DATA plus write on the scratch
#     database only (the grant-level enforcement of "dex never writes outside
#     the dev target")
#   - DEX_CI user: a SERVICE user authenticated by Workload Identity
#     Federation (GitHub OIDC), pinned to this repository and to the
#     snowflake-integration environment; no key or password is ever created
#     or stored for CI
#   - DEX_DEV user: a SERVICE user with a locally generated RSA key pair plus
#     a `dex-ci` entry in ~/.snowflake/connections.toml, for running the live
#     integration suite while developing
#   - the GitHub environment (deployments restricted to main) carrying the
#     two variables the workflow reads
#
# Everything account-specific is a parameter, so nothing private lives in this
# script. Idempotent: safe to re-run; existing resources are left in place and
# the WIF pinning is refreshed.
#
# Usage:
#   scripts/setup_snowflake_ci.sh <account-identifier> [snow-connection-name]
#
# <account-identifier> is the ORGNAME-ACCOUNTNAME form (shown by
# `snow connection list` or SELECT CURRENT_ORGANIZATION_NAME() || '-' ||
# CURRENT_ACCOUNT_NAME()). The optional second argument names the maintainer's
# admin `snow` connection; the default connection is used otherwise.
#
# Overrides via environment: DEX_CI_REPO (owner/name),
# DEX_CI_CREDIT_QUOTA (monthly credits before the monitor suspends, default 5),
# DEX_CI_STATEMENT_TIMEOUT (warehouse-level seconds, default 600).

set -euo pipefail

ACCOUNT="${1:-}"
ADMIN_CONN="${2:-}"
if [[ -z "$ACCOUNT" ]]; then
  echo "usage: $0 <account-identifier> [snow-connection-name]" >&2
  exit 1
fi

REPO="${DEX_CI_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
CREDIT_QUOTA="${DEX_CI_CREDIT_QUOTA:-5}"
STATEMENT_TIMEOUT="${DEX_CI_STATEMENT_TIMEOUT:-600}"
WAREHOUSE="DEX_CI_WH"
MONITOR="DEX_CI_MONITOR"
DATABASE="DEX_CI"
ROLE="DEX_CI_ROLE"
CI_USER="DEX_CI"
DEV_USER="DEX_DEV"
GH_ENV="snowflake-integration"
DEV_CONN_NAME="dex-ci"
KEY_DIR="${HOME}/.snowflake/keys"
KEY_FILE="${KEY_DIR}/dex_dev_rsa_key.p8"

SNOW=(snow sql)
if [[ -n "$ADMIN_CONN" ]]; then
  SNOW=(snow sql -c "$ADMIN_CONN")
fi
sql() { "${SNOW[@]}" -q "USE ROLE ACCOUNTADMIN; $1"; }

echo "== dex Snowflake setup =="
echo "   account:     ${ACCOUNT}"
echo "   repository:  ${REPO}"
echo "   warehouse:   ${WAREHOUSE} (X-Small, auto-suspend 60s, timeout ${STATEMENT_TIMEOUT}s)"
echo "   monitor:     ${MONITOR} (${CREDIT_QUOTA} credits/month, suspends)"
echo "   database:    ${DATABASE} (transient, retention 0)"
echo "   role:        ${ROLE}"
echo "   ci user:     ${CI_USER} (WIF: GitHub OIDC, env ${GH_ENV})"
echo "   dev user:    ${DEV_USER} (key pair, connection '${DEV_CONN_NAME}')"
echo

echo "-- Warehouse (the only compute dex is granted; suspended until used)"
sql "CREATE WAREHOUSE IF NOT EXISTS ${WAREHOUSE}
       WAREHOUSE_SIZE = 'XSMALL'
       AUTO_SUSPEND = 60
       AUTO_RESUME = TRUE
       INITIALLY_SUSPENDED = TRUE
       STATEMENT_TIMEOUT_IN_SECONDS = ${STATEMENT_TIMEOUT}
       COMMENT = 'dex integration and dev; capped by ${MONITOR}';"

echo "-- Resource monitor (the hard backstop: suspends the warehouse at quota)"
sql "CREATE RESOURCE MONITOR IF NOT EXISTS ${MONITOR}
       WITH CREDIT_QUOTA = ${CREDIT_QUOTA}
       FREQUENCY = MONTHLY
       START_TIMESTAMP = IMMEDIATELY
       TRIGGERS ON 80 PERCENT DO NOTIFY
                ON 100 PERCENT DO SUSPEND_IMMEDIATE;
     ALTER WAREHOUSE ${WAREHOUSE} SET RESOURCE_MONITOR = ${MONITOR};"

echo "-- Scratch database (transient + zero retention: no fail-safe or"
echo "   time-travel storage cost; crashed runs leave nothing that bills)"
sql "CREATE TRANSIENT DATABASE IF NOT EXISTS ${DATABASE}
       DATA_RETENTION_TIME_IN_DAYS = 0
       COMMENT = 'dex integration scratch; safe to drop';"

echo "-- Sample data (shared TPC-H; storage is free, mounted if absent)"
sql "CREATE DATABASE IF NOT EXISTS SNOWFLAKE_SAMPLE_DATA
       FROM SHARE SFC_SAMPLES.SAMPLE_DATA;"

echo "-- Role and grants (read samples; write scratch only)"
sql "CREATE ROLE IF NOT EXISTS ${ROLE};
     GRANT USAGE ON WAREHOUSE ${WAREHOUSE} TO ROLE ${ROLE};
     GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE ${ROLE};
     GRANT ALL PRIVILEGES ON DATABASE ${DATABASE} TO ROLE ${ROLE};
     GRANT ALL PRIVILEGES ON ALL SCHEMAS IN DATABASE ${DATABASE} TO ROLE ${ROLE};
     GRANT ALL PRIVILEGES ON FUTURE SCHEMAS IN DATABASE ${DATABASE} TO ROLE ${ROLE};"

echo "-- CI service user (Workload Identity Federation, pinned to ${REPO} + ${GH_ENV})"
# CREATE IF NOT EXISTS then ALTER keeps re-runs idempotent while refreshing
# the pinning if the repository or environment name ever changes. The subject
# condition is the load-bearing line: only tokens GitHub mints for this
# repository's snowflake-integration environment are accepted.
sql "CREATE USER IF NOT EXISTS ${CI_USER}
       TYPE = SERVICE
       DEFAULT_ROLE = ${ROLE}
       DEFAULT_WAREHOUSE = ${WAREHOUSE}
       DEFAULT_NAMESPACE = ${DATABASE}
       COMMENT = 'dex integration CI (GitHub Actions via workload identity)';
     ALTER USER ${CI_USER} SET WORKLOAD_IDENTITY = (
       TYPE = OIDC
       ISSUER = 'https://token.actions.githubusercontent.com'
       SUBJECT = 'repo:${REPO}:environment:${GH_ENV}'
       OIDC_AUDIENCE_LIST = ('snowflakecomputing.com')
     );
     GRANT ROLE ${ROLE} TO USER ${CI_USER};"

echo "-- Local dev service user (key pair; the key never leaves this machine)"
sql "CREATE USER IF NOT EXISTS ${DEV_USER}
       TYPE = SERVICE
       DEFAULT_ROLE = ${ROLE}
       DEFAULT_WAREHOUSE = ${WAREHOUSE}
       DEFAULT_NAMESPACE = ${DATABASE}
       COMMENT = 'dex local development (key-pair auth)';
     GRANT ROLE ${ROLE} TO USER ${DEV_USER};"

if [[ -f "$KEY_FILE" ]]; then
  echo "   key ${KEY_FILE} already exists; reusing it"
else
  mkdir -p "$KEY_DIR"
  (umask 077 && openssl genrsa 2048 2>/dev/null \
    | openssl pkcs8 -topk8 -inform PEM -nocrypt -out "$KEY_FILE")
  echo "   generated ${KEY_FILE}"
fi
# Snowflake wants the base64 body only, without the PEM armor lines.
PUBLIC_KEY=$(openssl rsa -in "$KEY_FILE" -pubout 2>/dev/null | grep -v '^-----' | tr -d '\n')
sql "ALTER USER ${DEV_USER} SET RSA_PUBLIC_KEY = '${PUBLIC_KEY}';"

echo "-- Local snow connection '${DEV_CONN_NAME}' (used by the integration suite)"
if snow connection list 2>/dev/null | grep -q "$DEV_CONN_NAME"; then
  echo "   connection ${DEV_CONN_NAME} already exists"
else
  snow connection add --connection-name "$DEV_CONN_NAME" \
    --account "$ACCOUNT" --user "$DEV_USER" \
    --authenticator SNOWFLAKE_JWT --private-key-file "$KEY_FILE" \
    --role "$ROLE" --warehouse "$WAREHOUSE" --database "$DATABASE" \
    --no-interactive
fi

echo "-- GitHub environment (deployments restricted to main)"
printf '{ "deployment_branch_policy": { "protected_branches": false, "custom_branch_policies": true } }' \
  | gh api -X PUT "repos/${REPO}/environments/${GH_ENV}" --input - >/dev/null
# Adding the same branch policy twice errors; tolerate re-runs.
gh api -X POST "repos/${REPO}/environments/${GH_ENV}/deployment-branch-policies" \
  -f name=main >/dev/null 2>&1 || echo "   branch policy for main already present"

echo "-- GitHub environment variables (identifiers, not secrets: WIF stores no credential)"
gh variable set DEX_TEST_SNOWFLAKE_ACCOUNT --repo "$REPO" --env "$GH_ENV" --body "$ACCOUNT"
gh variable set DEX_TEST_SNOWFLAKE_USER --repo "$REPO" --env "$GH_ENV" --body "$CI_USER"

echo
echo "== Done. Verify with:"
echo "   snow connection test -c ${DEV_CONN_NAME}"
echo "   snow sql -c ${DEV_CONN_NAME} -q \"SELECT COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS\""
echo "== The second query resumes ${WAREHOUSE}; it auto-suspends after 60s and"
echo "   ${MONITOR} suspends it for the month at ${CREDIT_QUOTA} credits."
