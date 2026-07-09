#!/usr/bin/env bash
# One-time provisioning for the live Databricks integration suite
# (.github/workflows/integration.yml). Run by a maintainer whose `databricks`
# CLI is authenticated against the workspace (workspace admin) and, for the
# federation policy, against the account console:
#
#   databricks auth login --host <workspace-url>
#   databricks auth login --host https://accounts.cloud.databricks.com \
#     --account-id <account-id> --profile dex-account
#
# What it sets up, keyless wherever the platform allows it:
#   - dex-ci: a service principal, the identity the CI suite runs as
#   - an account-level federation policy on that principal (GitHub OIDC),
#     pinned to this repository and to the databricks-integration environment;
#     no secret or PAT is ever created or stored for CI
#   - DEX_CI: a dedicated 2X-Small serverless SQL warehouse with the minimum
#     auto-stop; the only compute the principal is granted
#   - dex_ci: a scratch catalog the principal can write (the grant-level
#     enforcement of "dex never writes outside the dev target"); the samples
#     catalog it reads needs no grant (sample datasets are implicitly
#     readable and refuse GRANT statements)
#   - the GitHub environment (deployments restricted to main) carrying the
#     four variables the workflow reads
#
# Databricks has no resource-monitor analogue that hard-suspends compute at a
# quota, so the cost backstops are: the smallest warehouse size, the minimum
# auto-stop, the per-statement STATEMENT_TIMEOUT the engine sets, and a budget
# alert the maintainer should create in the account console (Usage > Budgets).
#
# Everything workspace-specific is a parameter, so nothing private lives in
# this script. Idempotent: safe to re-run; existing resources are left in
# place and the federation pinning is refreshed.
#
# Usage:
#   scripts/setup_databricks_ci.sh <workspace-url> [account-profile]
#
# <workspace-url> like https://dbc-xxxxxxxx-xxxx.cloud.databricks.com.
# [account-profile] names the account-console CLI profile (default:
# dex-account). Overrides via environment: DEX_CI_REPO (owner/name).

set -euo pipefail

WORKSPACE_URL="${1:-}"
ACCOUNT_PROFILE="${2:-dex-account}"
if [[ -z "$WORKSPACE_URL" ]]; then
  echo "usage: $0 <workspace-url> [account-profile]" >&2
  exit 1
fi
WORKSPACE_URL="${WORKSPACE_URL%/}"

REPO="${DEX_CI_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
SP_NAME="dex-ci"
WAREHOUSE_NAME="DEX_CI"
CATALOG="dex_ci"
GH_ENV="databricks-integration"
POLICY_ID="dex-github-oidc"

# The CLI selects a workspace via profiles, not a --host flag: resolve the
# profile whose host matches the requested workspace URL.
WORKSPACE_PROFILE=$(databricks auth profiles -o json | python3 -c "
import json, sys
for p in json.load(sys.stdin).get('profiles', []):
    if p.get('host', '').rstrip('/') == '${WORKSPACE_URL}' and p.get('valid'):
        print(p['name']); break
")
if [[ -z "$WORKSPACE_PROFILE" ]]; then
  echo "no valid CLI profile for ${WORKSPACE_URL}; run" >&2
  echo "  databricks auth login --host ${WORKSPACE_URL}" >&2
  exit 1
fi

dbx() { databricks --profile "$WORKSPACE_PROFILE" "$@"; }
dbx_account() { databricks --profile "$ACCOUNT_PROFILE" "$@"; }

echo "== dex Databricks CI setup =="
echo "   workspace:   ${WORKSPACE_URL}"
echo "   repository:  ${REPO}"
echo "   principal:   ${SP_NAME} (federation: GitHub OIDC, env ${GH_ENV})"
echo "   warehouse:   ${WAREHOUSE_NAME} (2X-Small, serverless, min auto-stop)"
echo "   catalog:     ${CATALOG} (scratch; the only writable scope)"
echo "   github env:  ${GH_ENV}"
echo

echo "-- Service principal"
SP_JSON=$(dbx service-principals list --output json 2>/dev/null || echo '[]')
APP_ID=$(printf '%s' "$SP_JSON" | python3 -c "
import json, sys
for sp in json.load(sys.stdin) or []:
    if sp.get('displayName') == '${SP_NAME}':
        print(sp['applicationId']); break
")
if [[ -n "$APP_ID" ]]; then
  echo "   ${SP_NAME} already exists (${APP_ID})"
else
  APP_ID=$(dbx service-principals create --display-name "$SP_NAME" --output json \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['applicationId'])")
  echo "   created ${SP_NAME} (${APP_ID})"
fi
SP_ID=$(dbx_account account service-principals list --output json \
  | python3 -c "
import json, sys
for sp in json.load(sys.stdin) or []:
    if sp.get('applicationId') == '${APP_ID}':
        print(sp['id']); break
")
if [[ -z "$SP_ID" ]]; then
  echo "   could not resolve the account-level id for ${APP_ID}; is the" >&2
  echo "   '${ACCOUNT_PROFILE}' profile logged into the account console?" >&2
  exit 1
fi

echo "-- Federation policy (pinned to ${REPO} + ${GH_ENV}; keyless CI)"
# The subject condition is the load-bearing line: only tokens GitHub mints for
# this repository's databricks-integration environment are accepted, and only
# with the workspace URL as the audience (what the workflow requests).
POLICY_JSON=$(python3 - <<PYEOF
import json
print(json.dumps({
    "oidc_policy": {
        "issuer": "https://token.actions.githubusercontent.com",
        "subject": "repo:${REPO}:environment:${GH_ENV}",
        "audiences": ["${WORKSPACE_URL}"],
    }
}))
PYEOF
)
if dbx_account account service-principal-federation-policy get "$SP_ID" "$POLICY_ID" \
  >/dev/null 2>&1; then
  dbx_account account service-principal-federation-policy update "$SP_ID" "$POLICY_ID" \
    --json "$POLICY_JSON" >/dev/null
  echo "   policy ${POLICY_ID} refreshed"
else
  dbx_account account service-principal-federation-policy create "$SP_ID" \
    --policy-id "$POLICY_ID" --json "$POLICY_JSON" >/dev/null
  echo "   policy ${POLICY_ID} created"
fi

echo "-- SQL warehouse (dedicated, smallest, minimum auto-stop)"
WH_ID=$(dbx warehouses list --output json | python3 -c "
import json, sys
for wh in json.load(sys.stdin) or []:
    if wh.get('name') == '${WAREHOUSE_NAME}':
        print(wh['id']); break
")
if [[ -n "$WH_ID" ]]; then
  echo "   ${WAREHOUSE_NAME} already exists (${WH_ID})"
else
  WH_ID=$(dbx warehouses create --no-wait --output json --json '{
      "name": "'"$WAREHOUSE_NAME"'",
      "cluster_size": "2X-Small",
      "min_num_clusters": 1,
      "max_num_clusters": 1,
      "auto_stop_mins": 1,
      "enable_serverless_compute": true,
      "warehouse_type": "PRO"
    }' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
  echo "   created ${WAREHOUSE_NAME} (${WH_ID})"
fi

echo "-- Warehouse permission (CAN USE for ${SP_NAME} only)"
dbx permissions update warehouses "$WH_ID" --json '{
    "access_control_list": [
      {"service_principal_name": "'"$APP_ID"'", "permission_level": "CAN_USE"}
    ]
  }' >/dev/null

run_sql() {
  dbx api post /api/2.0/sql/statements --json '{
      "warehouse_id": "'"$WH_ID"'",
      "statement": "'"$1"'",
      "wait_timeout": "50s"
    }' | python3 -c "
import json, sys
result = json.load(sys.stdin)
state = result['status']['state']
if state != 'SUCCEEDED':
    print(json.dumps(result['status']), file=sys.stderr)
    raise SystemExit(1)
"
}

echo "-- Scratch catalog (the only writable scope) and grants"
if dbx catalogs get "$CATALOG" >/dev/null 2>&1; then
  echo "   ${CATALOG} already exists"
else
  # Created via SQL on the warehouse, not the catalogs REST API: on
  # Default-Storage workspaces (no metastore storage root) the API refuses a
  # catalog without a managed location, while the SQL path backs it with the
  # workspace's default storage.
  run_sql "CREATE CATALOG IF NOT EXISTS \`${CATALOG}\`"
  run_sql "COMMENT ON CATALOG \`${CATALOG}\` IS 'dex integration scratch; safe to drop'"
  echo "   created ${CATALOG}"
fi
# The samples catalog needs no grant (and supports none:
# SAMPLE_TABLE_PERMISSIONS refuses GRANT on sample datasets); every workspace
# principal can read it implicitly.
run_sql "GRANT USE CATALOG, USE SCHEMA, CREATE SCHEMA, CREATE TABLE, SELECT, MODIFY ON CATALOG \`${CATALOG}\` TO \`${APP_ID}\`"

echo "-- GitHub environment (deployments restricted to main)"
printf '{ "deployment_branch_policy": { "protected_branches": false, "custom_branch_policies": true } }' \
  | gh api -X PUT "repos/${REPO}/environments/${GH_ENV}" --input - >/dev/null
# Adding the same branch policy twice errors; tolerate re-runs.
gh api -X POST "repos/${REPO}/environments/${GH_ENV}/deployment-branch-policies" \
  -f name=main >/dev/null 2>&1 || echo "   branch policy for main already present"

echo "-- GitHub environment variables (identifiers, not secrets: WIF stores no credential)"
gh variable set DEX_TEST_DATABRICKS_HOST --repo "$REPO" --env "$GH_ENV" \
  --body "$WORKSPACE_URL"
gh variable set DEX_TEST_DATABRICKS_CLIENT_ID --repo "$REPO" --env "$GH_ENV" \
  --body "$APP_ID"
gh variable set DEX_TEST_DATABRICKS_WAREHOUSE --repo "$REPO" --env "$GH_ENV" \
  --body "$WH_ID"
gh variable set DEX_TEST_DATABRICKS_CATALOG --repo "$REPO" --env "$GH_ENV" \
  --body "$CATALOG"

echo
echo "== Done. Verify with:"
echo "   gh workflow run integration.yml --repo ${REPO} && gh run watch --repo ${REPO}"
echo "== Recommended backstop (account console, no CLI): create a budget alert"
echo "   (Usage > Budgets) covering this workspace, since Databricks has no"
echo "   resource monitor that hard-suspends compute at a quota."
