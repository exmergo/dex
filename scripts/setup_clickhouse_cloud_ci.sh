#!/usr/bin/env bash
# Provision or rotate the dedicated ClickHouse Cloud integration target.
# Account coordinates and policy are committed non-secret constants; no
# arbitrary service is ever discovered or mutated. Bootstrap credentials stay
# in the process environment.

set -euo pipefail

# Stable, non-secret deployment coordinates and pricing for the dedicated dex
# service. Flags remain available for deliberate migration/testing, but an
# ordinary setup never depends on ambient non-secret shell variables.
ORGANIZATION="b199ae61-7c10-4a80-a40e-9d714bdbae2e"
SERVICE="f293ae48-6335-4a7b-8d58-b9dd46fad0d3"
REPO="exmergo/dex"
ENVIRONMENT="clickhouse-cloud-integration"
COMPUTE_UNIT_PRICE_USD="0.29846"
STORAGE_PRICE_USD_PER_TB_MONTH="25.3"
DAILY_CHC_LIMIT="2"
MAX_SECONDS="60"
# Kept in step with adapters.clickhouse._SCAN_BYTES_PER_SECOND and
# transform.build._CH_SCAN_BYTES_PER_SECOND. The durable profile ceiling must
# admit every per-command cap dex can derive from MAX_SECONDS.
SCAN_BYTES_PER_SECOND=$((200 * 1024 * 1024))
MAX_BYTES=$((MAX_SECONDS * SCAN_BYTES_PER_SECOND))
ADOPT_SERVICE=false
RUN_DOGFOOD=false

APP_DATABASE="dex_ci_app"
DEV_DATABASE="dex_ci_dbt"
READER_USER="dex_ci_ro"
DBT_USER="dex_ci_dbt"
READER_PROFILE="dex_ci_ro_limits"
DBT_PROFILE="dex_ci_dbt_limits"
ROLE_NAME="dex-ci-usage-reader"
KEY_PREFIX="dex-ci-usage-"
OWNERSHIP_TAG="dex-ci-owned"
OWNERSHIP_TAG_VALUE="exmergo-dex"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --organization) ORGANIZATION="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --compute-unit-price-usd) COMPUTE_UNIT_PRICE_USD="$2"; shift 2 ;;
    --daily-chc-limit) DAILY_CHC_LIMIT="$2"; shift 2 ;;
    --max-seconds) MAX_SECONDS="$2"; shift 2 ;;
    --adopt-service) ADOPT_SERVICE=true; shift ;;
    --dogfood) RUN_DOGFOOD=true; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[[ "$COMPUTE_UNIT_PRICE_USD" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
  echo "--compute-unit-price-usd must be numeric" >&2; exit 2; }
[[ "$DAILY_CHC_LIMIT" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
  echo "--daily-chc-limit must be numeric" >&2; exit 2; }
[[ "$MAX_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
  echo "--max-seconds must be a positive integer" >&2; exit 2; }
MAX_BYTES=$((MAX_SECONDS * SCAN_BYTES_PER_SECOND))

for tool in clickhousectl clickhouse jq openssl curl gh; do
  command -v "$tool" >/dev/null || { echo "$tool is required on PATH" >&2; exit 2; }
done
gh auth status >/dev/null
if [[ "$RUN_DOGFOOD" == true ]]; then
  command -v uv >/dev/null || { echo "uv is required for --dogfood" >&2; exit 2; }
fi

for name in CLICKHOUSE_CLOUD_API_KEY CLICKHOUSE_CLOUD_API_SECRET CLICKHOUSE_USER CLICKHOUSE_PASSWORD; do
  [[ -n "${!name:-}" ]] || { echo "$name is required in the environment" >&2; exit 2; }
done

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SEED="$ROOT/scripts/clickhouse_seed.sql"
API_ROOT="https://api.clickhouse.cloud/v1/organizations/$ORGANIZATION"

cloud_api_curl_config() {
  # Feed Basic credentials on a private pipe. `curl --user key:secret` would
  # expose them briefly in the process list even though neither script nor curl
  # prints them.
  local credentials="$CLICKHOUSE_CLOUD_API_KEY:$CLICKHOUSE_CLOUD_API_SECRET"
  credentials=${credentials//\\/\\\\}
  credentials=${credentials//\"/\\\"}
  printf 'user = "%s"\n' "$credentials"
}

cloud_api() {
  local method=$1 path=$2 data=${3:-}
  if [[ -n "$data" ]]; then
    curl --fail-with-body --silent --show-error \
      --config <(cloud_api_curl_config) --request "$method" \
      --header "Content-Type: application/json" --data "$data" "$API_ROOT$path"
  else
    curl --fail-with-body --silent --show-error \
      --config <(cloud_api_curl_config) --request "$method" \
      "$API_ROOT$path"
  fi
}

echo "==> validating the exact organization and service"
clickhousectl cloud auth status >/dev/null
ORG_JSON=$(clickhousectl cloud org get "$ORGANIZATION" --json)
[[ "$(jq -er '.id' <<<"$ORG_JSON")" == "$ORGANIZATION" ]] || {
  echo "organization ID mismatch" >&2; exit 1; }
SERVICE_JSON=$(clickhousectl cloud service get "$SERVICE" --org-id "$ORGANIZATION" --json)
[[ "$(jq -er '.id' <<<"$SERVICE_JSON")" == "$SERVICE" ]] || {
  echo "service ID mismatch" >&2; exit 1; }
[[ "$(jq -er '.provider' <<<"$SERVICE_JSON")" == "aws" ]] || {
  echo "the dedicated service must be on AWS" >&2; exit 1; }
[[ "$(jq -er '.region' <<<"$SERVICE_JSON")" == "us-east-1" ]] || {
  echo "the dedicated service must be in AWS us-east-1" >&2; exit 1; }

has_ownership_tag() {
  jq -e --arg key "$OWNERSHIP_TAG" --arg value "$OWNERSHIP_TAG_VALUE" '
    any(.tags[]?; ((.key? == $key and .value? == $value) or . == ($key + "=" + $value)))
  ' >/dev/null <<<"$1"
}

if ! has_ownership_tag "$SERVICE_JSON"; then
  if [[ "$ADOPT_SERVICE" != true ]]; then
    echo "service lacks $OWNERSHIP_TAG=$OWNERSHIP_TAG_VALUE; inspect it, then rerun with --adopt-service" >&2
    exit 1
  fi
  echo "==> adopting the explicitly named dedicated service"
  clickhousectl cloud service update "$SERVICE" --org-id "$ORGANIZATION" \
    --add-tag "$OWNERSHIP_TAG=$OWNERSHIP_TAG_VALUE" \
    --add-tag "dex-ci-environment=$ENVIRONMENT" --json >/dev/null
  SERVICE_JSON=$(clickhousectl cloud service get "$SERVICE" --org-id "$ORGANIZATION" --json)
fi
has_ownership_tag "$SERVICE_JSON" || { echo "ownership tag verification failed" >&2; exit 1; }

# The OpenAPI contract says a primary service (the first service in a warehouse)
# has a minimum of two replicas; a secondary may use one. This derives the legal
# floor from topology instead of preserving a possibly oversized current value.
if [[ "$(jq -er '.isPrimary' <<<"$SERVICE_JSON")" == true ]]; then
  MIN_REPLICAS=2
else
  MIN_REPLICAS=1
fi

echo "==> enforcing fixed minimum capacity and five-minute idling"
clickhousectl cloud service scale "$SERVICE" --org-id "$ORGANIZATION" \
  --autoscaling-mode vertical --min-replica-memory-gb 8 \
  --max-replica-memory-gb 8 --num-replicas "$MIN_REPLICAS" \
  --idle-scaling true --idle-timeout-minutes 5 --json >/dev/null

for _ in $(seq 1 60); do
  SERVICE_JSON=$(clickhousectl cloud service get "$SERVICE" --org-id "$ORGANIZATION" --json)
  if jq -e --argjson replicas "$MIN_REPLICAS" '
      .currentScaling.effectiveAutoscalingMode == "vertical" and
      .currentScaling.effectiveMinReplicaMemoryGb == 8 and
      .currentScaling.effectiveMaxReplicaMemoryGb == 8 and
      .currentScaling.effectiveMinReplicas == $replicas and
      .currentScaling.effectiveMaxReplicas == $replicas and
      .currentScaling.effectiveIdleScaling == true and
      .currentScaling.effectiveIdleTimeoutMinutes == 5
    ' >/dev/null <<<"$SERVICE_JSON"; then
    break
  fi
  sleep 10
done
jq -e --argjson replicas "$MIN_REPLICAS" '
  .currentScaling.effectiveMinReplicaMemoryGb == 8 and
  .currentScaling.effectiveMaxReplicaMemoryGb == 8 and
  .currentScaling.effectiveMinReplicas == $replicas and
  .currentScaling.effectiveMaxReplicas == $replicas and
  .currentScaling.effectiveIdleScaling == true and
  .currentScaling.effectiveIdleTimeoutMinutes == 5
' >/dev/null <<<"$SERVICE_JSON" || { echo "service scaling did not converge" >&2; exit 1; }

HOST=$(jq -er '.endpoints[] | select(.protocol == "nativesecure") | .host' <<<"$SERVICE_JSON")
NATIVE_PORT=$(jq -er '.endpoints[] | select(.protocol == "nativesecure") | .port' <<<"$SERVICE_JSON")
HTTP_PORT=$(jq -er '.endpoints[] | select(.protocol == "https") | .port' <<<"$SERVICE_JSON")

echo "==> waking the service and validating database administrator access"
for _ in $(seq 1 30); do
  if clickhouse client --secure --host "$HOST" --port "$NATIVE_PORT" \
      --user "$CLICKHOUSE_USER" --query "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 10
done
clickhouse client --secure --host "$HOST" --port "$NATIVE_PORT" \
  --user "$CLICKHOUSE_USER" --query "SELECT 1" >/dev/null

READER_PASSWORD="Aa1!$(openssl rand -hex 24)"
DBT_PASSWORD="Aa1!$(openssl rand -hex 24)"

run_admin_multiquery() {
  local error_file line
  error_file=$(mktemp)
  if clickhouse client --secure --host "$HOST" --port "$NATIVE_PORT" \
      --user "$CLICKHOUSE_USER" --multiquery 2>"$error_file"; then
    rm -f "$error_file"
    return 0
  fi
  while IFS= read -r line; do
    line=${line//"$READER_PASSWORD"/[REDACTED]}
    line=${line//"$DBT_PASSWORD"/[REDACTED]}
    printf '%s\n' "$line" >&2
  done <"$error_file"
  rm -f "$error_file"
  return 1
}

echo "==> reseeding only $APP_DATABASE and $DEV_DATABASE from the shared fixture"
clickhouse client --secure --host "$HOST" --port "$NATIVE_PORT" \
  --user "$CLICKHOUSE_USER" --multiquery \
  --param_app_database="$APP_DATABASE" --param_dev_database="$DEV_DATABASE" <"$SEED"

echo "==> rotating dex-owned database users and durable constraints"
# A rerun adds the new password before GitHub is updated. The old password stays
# valid until every new secret has been written, so an interrupted rotation can
# be retried without breaking a running workflow. Generated values are
# generated and sent over stdin; they never appear in command arguments,
# output, or repository files.
READER_EXISTS=$(clickhouse client --secure --host "$HOST" --port "$NATIVE_PORT" \
  --user "$CLICKHOUSE_USER" --format TSVRaw \
  --query "SELECT count() FROM system.users WHERE name = '$READER_USER'")
DBT_EXISTS=$(clickhouse client --secure --host "$HOST" --port "$NATIVE_PORT" \
  --user "$CLICKHOUSE_USER" --format TSVRaw \
  --query "SELECT count() FROM system.users WHERE name = '$DBT_USER'")
if [[ "$READER_EXISTS" == 1 ]]; then
  READER_AUTH_SQL="ALTER USER $READER_USER ADD IDENTIFIED WITH sha256_password BY '$READER_PASSWORD'"
else
  READER_AUTH_SQL="CREATE USER $READER_USER IDENTIFIED WITH sha256_password BY '$READER_PASSWORD'"
fi
if [[ "$DBT_EXISTS" == 1 ]]; then
  DBT_AUTH_SQL="ALTER USER $DBT_USER ADD IDENTIFIED WITH sha256_password BY '$DBT_PASSWORD'"
else
  DBT_AUTH_SQL="CREATE USER $DBT_USER IDENTIFIED WITH sha256_password BY '$DBT_PASSWORD'"
fi
printf '%s;\n' \
  "DROP SETTINGS PROFILE IF EXISTS $READER_PROFILE" \
  "DROP SETTINGS PROFILE IF EXISTS $DBT_PROFILE" \
  "$READER_AUTH_SQL" \
  "$DBT_AUTH_SQL" \
  "REVOKE ALL ON *.* FROM $READER_USER" \
  "REVOKE ALL ON *.* FROM $DBT_USER" \
  "GRANT SELECT ON $APP_DATABASE.* TO $READER_USER" \
  "GRANT SELECT ON system.tables TO $READER_USER" \
  "GRANT SELECT ON system.columns TO $READER_USER" \
  "GRANT SELECT ON system.databases TO $READER_USER" \
  "GRANT SELECT ON system.settings TO $READER_USER" \
  "GRANT SELECT ON system.grants TO $READER_USER" \
  "GRANT SELECT ON system.role_grants TO $READER_USER" \
  "GRANT SELECT ON system.asynchronous_metrics TO $READER_USER" \
  "GRANT SELECT ON system.clusters TO $READER_USER" \
  "GRANT REMOTE ON *.* TO $READER_USER" \
  "GRANT SELECT ON $APP_DATABASE.* TO $DBT_USER" \
  "GRANT SELECT ON system.tables TO $DBT_USER" \
  "GRANT SELECT ON system.columns TO $DBT_USER" \
  "GRANT SELECT ON system.databases TO $DBT_USER" \
  "GRANT SHOW DATABASES ON $DEV_DATABASE.* TO $DBT_USER" \
  "GRANT SELECT, INSERT, ALTER, CREATE TABLE, CREATE VIEW, DROP TABLE, DROP VIEW, TRUNCATE, OPTIMIZE ON $DEV_DATABASE.* TO $DBT_USER" \
  "GRANT CREATE DATABASE ON $DEV_DATABASE.* TO $DBT_USER" \
  "CREATE SETTINGS PROFILE $READER_PROFILE SETTINGS readonly = 2 READONLY, allow_ddl = 0 READONLY, max_execution_time = $MAX_SECONDS MIN 0 MAX $MAX_SECONDS CHANGEABLE_IN_READONLY, max_bytes_to_read = $MAX_BYTES MIN 0 MAX $MAX_BYTES CHANGEABLE_IN_READONLY TO $READER_USER" \
  "CREATE SETTINGS PROFILE $DBT_PROFILE SETTINGS max_execution_time = $MAX_SECONDS MIN 0 MAX $MAX_SECONDS, max_bytes_to_read = $MAX_BYTES MIN 0 MAX $MAX_BYTES TO $DBT_USER" |
  run_admin_multiquery

echo "==> verifying database isolation and live capacity"
CLICKHOUSE_PASSWORD="$READER_PASSWORD" clickhouse client --secure --host "$HOST" \
  --port "$NATIVE_PORT" --user "$READER_USER" \
  --query "SELECT count() FROM $APP_DATABASE.events" >/dev/null
if CLICKHOUSE_PASSWORD="$READER_PASSWORD" clickhouse client --secure --host "$HOST" \
    --port "$NATIVE_PORT" --user "$READER_USER" \
    --query "INSERT INTO $APP_DATABASE.signups VALUES (1, 'blocked', now())" >/dev/null 2>&1; then
  echo "$READER_USER unexpectedly wrote to $APP_DATABASE" >&2; exit 1
fi
CLICKHOUSE_PASSWORD="$DBT_PASSWORD" clickhouse client --secure --host "$HOST" \
  --port "$NATIVE_PORT" --user "$DBT_USER" --multiquery \
  --query "CREATE TABLE $DEV_DATABASE.setup_probe (id UInt8) ENGINE=Memory; DROP TABLE $DEV_DATABASE.setup_probe" >/dev/null
if CLICKHOUSE_PASSWORD="$DBT_PASSWORD" clickhouse client --secure --host "$HOST" \
    --port "$NATIVE_PORT" --user "$DBT_USER" \
    --query "INSERT INTO $APP_DATABASE.signups VALUES (1, 'blocked', now())" >/dev/null 2>&1; then
  echo "$DBT_USER unexpectedly wrote to $APP_DATABASE" >&2; exit 1
fi
CAPACITY_ROWS=$(CLICKHOUSE_PASSWORD="$READER_PASSWORD" clickhouse client --secure \
  --host "$HOST" --port "$NATIVE_PORT" --user "$READER_USER" --format TSVRaw \
  --query "SELECT count() FROM clusterAllReplicas('default', system.asynchronous_metrics) WHERE metric = 'CGroupMemoryTotal'")
[[ "$CAPACITY_ROWS" == "$MIN_REPLICAS" ]] || {
  echo "capacity probe returned $CAPACITY_ROWS replicas; expected $MIN_REPLICAS" >&2; exit 1; }

echo "==> creating the service-scoped usage reader role"
ROLES_JSON=$(cloud_api GET /roles)
ROLE_ID=$(jq -r --arg name "$ROLE_NAME" \
  '.result[]? | select(.name == $name and .type == "custom") | .id' \
  <<<"$ROLES_JSON" | head -1)
if [[ -z "$ROLE_ID" ]]; then
  ROLE_PAYLOAD=$(jq -n --arg name "$ROLE_NAME" --arg service "$SERVICE" \
    --arg organization "$ORGANIZATION" '
      {name:$name,actors:[],policies:
        [{allowDeny:"ALLOW",permissions:["control-plane:service:view"],
          resources:[("instance/" + $service)]},
         {allowDeny:"ALLOW",permissions:["control-plane:organization:view",
                                         "control-plane:organization:view-billing"],
          resources:[("organization/" + $organization)]}]}')
  ROLE_JSON=$(cloud_api POST /roles "$ROLE_PAYLOAD")
  ROLE_ID=$(jq -er '.result.id' <<<"$ROLE_JSON")
else
  jq -e --arg role "$ROLE_ID" --arg service "$SERVICE" '
    .result[] | select(.id == $role) |
    select(any(.policies[]?;
      (.permissions | index("control-plane:service:view")) and
      (.resources | index("instance/" + $service))))
  ' >/dev/null <<<"$ROLES_JSON" || {
    echo "existing $ROLE_NAME does not match this service; teardown it before setup" >&2; exit 1; }
  ROLE_PATCH=$(jq -n --arg service "$SERVICE" --arg organization "$ORGANIZATION" '
    {policies:
      [{allowDeny:"ALLOW",permissions:["control-plane:service:view"],
        resources:[("instance/" + $service)]},
       {allowDeny:"ALLOW",permissions:["control-plane:organization:view",
                                       "control-plane:organization:view-billing"],
        resources:[("organization/" + $organization)]}]}')
  cloud_api PATCH "/roles/$ROLE_ID" "$ROLE_PATCH" >/dev/null
fi

echo "==> creating a new scoped API key before retiring older keys"
OLD_KEYS=$(clickhousectl cloud key list --org-id "$ORGANIZATION" --json)
KEY_NAME="${KEY_PREFIX}$(date -u +%Y%m%dT%H%M%SZ)"
# GitHub-hosted runners have no stable single egress address. This network
# portability applies only to the read-only control-plane key; the service's
# SQL ipAccessList is never changed here, and the role cannot mutate service or
# billing configuration.
NEW_KEY_JSON=$(clickhousectl cloud key create --org-id "$ORGANIZATION" \
  --name "$KEY_NAME" --role-id "$ROLE_ID" --state enabled \
  --ip-allow 0.0.0.0/0 --json)
NEW_KEY_RESOURCE_ID=$(jq -er '.id // .key.id' <<<"$NEW_KEY_JSON")
NEW_KEY_ID=$(jq -er '.keyId' <<<"$NEW_KEY_JSON")
NEW_KEY_SECRET=$(jq -er '.keySecret' <<<"$NEW_KEY_JSON")

SCOPED_KEY_READY=false
for _ in $(seq 1 12); do
  if DEX_TEST_CH_CLOUD_API_KEY="$NEW_KEY_ID" \
      DEX_TEST_CH_CLOUD_API_SECRET="$NEW_KEY_SECRET" \
      DEX_TEST_CH_CLOUD_ORG_ID="$ORGANIZATION" \
      DEX_TEST_CH_CLOUD_SERVICE_ID="$SERVICE" \
      DEX_TEST_CH_CLOUD_MAX_DAILY_CHC="$DAILY_CHC_LIMIT" \
      "$ROOT/scripts/clickhouse_cloud/preflight.sh" --report-only \
        >/dev/null 2>&1; then
    SCOPED_KEY_READY=true
    break
  fi
  sleep 5
done
if [[ "$SCOPED_KEY_READY" != true ]]; then
  clickhousectl cloud key delete "$NEW_KEY_RESOURCE_ID" \
    --org-id "$ORGANIZATION" --json >/dev/null
  echo "scoped API key did not become readable within 60 seconds" >&2
  exit 1
fi

SERVICE_NAME=$(jq -er '.name' <<<"$SERVICE_JSON")
if CLICKHOUSE_CLOUD_API_KEY="$NEW_KEY_ID" CLICKHOUSE_CLOUD_API_SECRET="$NEW_KEY_SECRET" \
    clickhousectl cloud service update "$SERVICE" --org-id "$ORGANIZATION" \
      --name "$SERVICE_NAME" --json >/dev/null 2>&1; then
  echo "scoped API key unexpectedly has service-management permission" >&2; exit 1
fi

echo "==> creating the protected GitHub environment and writing configuration"
  gh api --method PUT "repos/$REPO/environments/$ENVIRONMENT" \
    --input - >/dev/null <<EOF
{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
EOF
  POLICIES_JSON=$(gh api \
    "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies")
  while IFS= read -r policy_id; do
    [[ -n "$policy_id" ]] || continue
    gh api --method DELETE \
      "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies/$policy_id" \
      >/dev/null
  done < <(jq -r \
    '.branch_policies[]? | select(.name != "main" or .type != "branch") | .id' \
    <<<"$POLICIES_JSON")
  if ! jq -e \
      'any(.branch_policies[]?; .name == "main" and .type == "branch")' \
      >/dev/null <<<"$POLICIES_JSON"; then
    gh api --method POST "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" \
      -f name=main -f type=branch >/dev/null
  fi

  gh variable set DEX_TEST_CH_CLOUD_ORG_ID --env "$ENVIRONMENT" --repo "$REPO" --body "$ORGANIZATION"
  gh variable set DEX_TEST_CH_CLOUD_SERVICE_ID --env "$ENVIRONMENT" --repo "$REPO" --body "$SERVICE"
  gh variable set DEX_TEST_CH_CLOUD_HOST --env "$ENVIRONMENT" --repo "$REPO" --body "$HOST"
  gh variable set DEX_TEST_CH_CLOUD_PORT --env "$ENVIRONMENT" --repo "$REPO" --body "$HTTP_PORT"
  gh variable set DEX_TEST_CH_CLOUD_DATABASE --env "$ENVIRONMENT" --repo "$REPO" --body "$APP_DATABASE"
  gh variable set DEX_TEST_CH_CLOUD_DEV_DATABASE --env "$ENVIRONMENT" --repo "$REPO" --body "$DEV_DATABASE"
  gh variable set DEX_TEST_CH_CLOUD_COMPUTE_UNIT_PRICE_USD --env "$ENVIRONMENT" --repo "$REPO" --body "$COMPUTE_UNIT_PRICE_USD"
  gh variable set DEX_TEST_CH_CLOUD_MAX_SECONDS --env "$ENVIRONMENT" --repo "$REPO" --body "$MAX_SECONDS"
  gh variable set DEX_TEST_CH_CLOUD_MAX_DAILY_CHC --env "$ENVIRONMENT" --repo "$REPO" --body "$DAILY_CHC_LIMIT"

  READER_DSN="https://${READER_USER}:${READER_PASSWORD}@${HOST}:${HTTP_PORT}/${APP_DATABASE}"
  printf '%s' "$READER_DSN" | gh secret set DEX_TEST_CH_CLOUD_DSN --env "$ENVIRONMENT" --repo "$REPO"
  printf '%s' "$DBT_PASSWORD" | gh secret set DEX_TEST_CH_CLOUD_DEV_PASSWORD --env "$ENVIRONMENT" --repo "$REPO"
  printf '%s' "$NEW_KEY_ID" | gh secret set DEX_TEST_CH_CLOUD_API_KEY --env "$ENVIRONMENT" --repo "$REPO"
  printf '%s' "$NEW_KEY_SECRET" | gh secret set DEX_TEST_CH_CLOUD_API_SECRET --env "$ENVIRONMENT" --repo "$REPO"

  REQUIRED_VARIABLES=$(gh variable list --env "$ENVIRONMENT" --repo "$REPO" \
    --json name --jq '.[].name')
  for name in DEX_TEST_CH_CLOUD_ORG_ID DEX_TEST_CH_CLOUD_SERVICE_ID \
      DEX_TEST_CH_CLOUD_HOST DEX_TEST_CH_CLOUD_PORT \
      DEX_TEST_CH_CLOUD_DATABASE DEX_TEST_CH_CLOUD_DEV_DATABASE \
      DEX_TEST_CH_CLOUD_COMPUTE_UNIT_PRICE_USD \
      DEX_TEST_CH_CLOUD_MAX_SECONDS DEX_TEST_CH_CLOUD_MAX_DAILY_CHC; do
    grep -qx "$name" <<<"$REQUIRED_VARIABLES" || {
      echo "GitHub variable $name is missing" >&2; exit 1; }
  done
  REQUIRED_NAMES=$(gh secret list --env "$ENVIRONMENT" --repo "$REPO" \
    --json name --jq '.[].name')
  for name in DEX_TEST_CH_CLOUD_DSN DEX_TEST_CH_CLOUD_DEV_PASSWORD DEX_TEST_CH_CLOUD_API_KEY DEX_TEST_CH_CLOUD_API_SECRET; do
    grep -qx "$name" <<<"$REQUIRED_NAMES" || { echo "GitHub secret $name is missing" >&2; exit 1; }
  done

echo "==> retiring prior database passwords after GitHub accepted the rotation"
printf '%s;\n' \
  "ALTER USER $READER_USER RESET AUTHENTICATION METHODS TO NEW" \
  "ALTER USER $DBT_USER RESET AUTHENTICATION METHODS TO NEW" |
  run_admin_multiquery

echo "==> retiring older dex CI usage keys after new credentials verified"
while IFS= read -r old_id; do
  [[ -n "$old_id" && "$old_id" != "$NEW_KEY_ID" ]] || continue
  clickhousectl cloud key delete "$old_id" --org-id "$ORGANIZATION" --json >/dev/null
done < <(jq -r --arg prefix "$KEY_PREFIX" '.[]? | select(.name | startswith($prefix)) | .id' <<<"$OLD_KEYS")

if [[ "$RUN_DOGFOOD" == true ]]; then
  echo "==> running the narrow live Cloud dogfood suite with the rotated credentials"
  DEX_TEST_CH_CLOUD_ORG_ID="$ORGANIZATION" \
  DEX_TEST_CH_CLOUD_SERVICE_ID="$SERVICE" \
  DEX_TEST_CH_CLOUD_HOST="$HOST" \
  DEX_TEST_CH_CLOUD_PORT="$HTTP_PORT" \
  DEX_TEST_CH_CLOUD_DATABASE="$APP_DATABASE" \
  DEX_TEST_CH_CLOUD_DEV_DATABASE="$DEV_DATABASE" \
  DEX_TEST_CH_CLOUD_COMPUTE_UNIT_PRICE_USD="$COMPUTE_UNIT_PRICE_USD" \
  DEX_TEST_CH_CLOUD_MAX_SECONDS="$MAX_SECONDS" \
  DEX_TEST_CH_CLOUD_MAX_DAILY_CHC="$DAILY_CHC_LIMIT" \
  DEX_TEST_CH_CLOUD_DSN="$READER_DSN" \
  DEX_TEST_CH_CLOUD_DEV_PASSWORD="$DBT_PASSWORD" \
  DEX_TEST_CH_CLOUD_API_KEY="$NEW_KEY_ID" \
  DEX_TEST_CH_CLOUD_API_SECRET="$NEW_KEY_SECRET" \
    "$ROOT/scripts/clickhouse_cloud/run_integration.sh"
fi

echo "ClickHouse Cloud integration setup verified"
echo "organization: $ORGANIZATION"
echo "service: $SERVICE"
echo "host: $HOST"
echo "replicas: $MIN_REPLICAS"
echo "memory_gib_per_replica: 8"
echo "idle_timeout_minutes: 5"
echo "compute_unit_price_usd: $COMPUTE_UNIT_PRICE_USD"
echo "storage_price_usd_per_tb_month: $STORAGE_PRICE_USD_PER_TB_MONTH"
