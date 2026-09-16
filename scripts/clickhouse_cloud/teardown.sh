#!/usr/bin/env bash
# Remove only resources carrying dex's fixed prefix from the explicitly named
# service. The service itself is never stopped or deleted.

set -euo pipefail

ORGANIZATION="b199ae61-7c10-4a80-a40e-9d714bdbae2e"
SERVICE="f293ae48-6335-4a7b-8d58-b9dd46fad0d3"
REPO="exmergo/dex"
ENVIRONMENT="clickhouse-cloud-integration"
OWNERSHIP_TAG_VALUE="exmergo-dex"
CONFIRM=false
SKIP_GITHUB=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --organization) ORGANIZATION="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --confirm) CONFIRM=true; shift ;;
    --skip-github) SKIP_GITHUB=true; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[[ "$CONFIRM" == true ]] || {
  echo "teardown requires --confirm after reviewing the exact organization and service" >&2
  exit 2
}
for name in CLICKHOUSE_CLOUD_API_KEY CLICKHOUSE_CLOUD_API_SECRET CLICKHOUSE_USER CLICKHOUSE_PASSWORD; do
  [[ -n "${!name:-}" ]] || { echo "$name is required in the environment" >&2; exit 2; }
done
for tool in clickhousectl clickhouse jq curl; do
  command -v "$tool" >/dev/null || { echo "$tool is required on PATH" >&2; exit 2; }
done

SERVICE_JSON=$(clickhousectl cloud service get "$SERVICE" --org-id "$ORGANIZATION" --json)
[[ "$(jq -er '.id' <<<"$SERVICE_JSON")" == "$SERVICE" ]] || {
  echo "service ID mismatch" >&2; exit 1; }
jq -e --arg value "$OWNERSHIP_TAG_VALUE" '
  any(.tags[]?; ((.key? == "dex-ci-owned" and .value? == $value) or
    . == ("dex-ci-owned=" + $value)))
' >/dev/null <<<"$SERVICE_JSON" || {
  echo "refusing teardown: dex ownership tag is absent" >&2; exit 1; }

HOST=$(jq -er '.endpoints[] | select(.protocol == "nativesecure") | .host' <<<"$SERVICE_JSON")
PORT=$(jq -er '.endpoints[] | select(.protocol == "nativesecure") | .port' <<<"$SERVICE_JSON")

echo "==> removing dex_ci database resources only"
printf '%s;\n' \
  "DROP SETTINGS PROFILE IF EXISTS dex_ci_ro_limits" \
  "DROP SETTINGS PROFILE IF EXISTS dex_ci_dbt_limits" \
  "DROP USER IF EXISTS dex_ci_ro" \
  "DROP USER IF EXISTS dex_ci_dbt" \
  "DROP DATABASE IF EXISTS dex_ci_app" \
  "DROP DATABASE IF EXISTS dex_ci_dbt" |
  clickhouse client --secure --host "$HOST" --port "$PORT" \
    --user "$CLICKHOUSE_USER" --multiquery

echo "==> removing dex CI API keys"
KEYS=$(clickhousectl cloud key list --org-id "$ORGANIZATION" --json)
while IFS= read -r key_id; do
  [[ -n "$key_id" ]] || continue
  clickhousectl cloud key delete "$key_id" --org-id "$ORGANIZATION" --json >/dev/null
done < <(jq -r '.[]? | select(.name | startswith("dex-ci-usage-")) | .id' <<<"$KEYS")

API_ROOT="https://api.clickhouse.cloud/v1/organizations/$ORGANIZATION"
cloud_api_curl_config() {
  local credentials="$CLICKHOUSE_CLOUD_API_KEY:$CLICKHOUSE_CLOUD_API_SECRET"
  credentials=${credentials//\\/\\\\}
  credentials=${credentials//\"/\\\"}
  printf 'user = "%s"\n' "$credentials"
}
ROLES=$(curl --fail-with-body --silent --show-error \
  --config <(cloud_api_curl_config) "$API_ROOT/roles")
while IFS= read -r role_id; do
  [[ -n "$role_id" ]] || continue
  curl --fail-with-body --silent --show-error --request DELETE \
    --config <(cloud_api_curl_config) \
    "$API_ROOT/roles/$role_id" >/dev/null
done < <(jq -r '.result[]? | select(.name == "dex-ci-usage-reader" and .type == "custom") | .id' <<<"$ROLES")

if [[ "$SKIP_GITHUB" != true ]]; then
  command -v gh >/dev/null || { echo "gh is required (or pass --skip-github)" >&2; exit 2; }
  echo "==> removing the dex GitHub environment"
  gh api --method DELETE "repos/$REPO/environments/$ENVIRONMENT" >/dev/null 2>&1 || true
fi

echo "==> removing dex attribution tags; service remains intact"
clickhousectl cloud service update "$SERVICE" --org-id "$ORGANIZATION" \
  --remove-tag "dex-ci-owned=$OWNERSHIP_TAG_VALUE" \
  --remove-tag "dex-ci-environment=$ENVIRONMENT" --json >/dev/null

echo "teardown complete; service $SERVICE was not stopped or deleted"
