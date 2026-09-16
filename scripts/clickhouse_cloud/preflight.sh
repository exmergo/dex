#!/usr/bin/env bash
# Read-only ClickHouse Cloud admission check. This performs no SQL connection,
# so an over-budget refusal cannot wake an idled service.

set -euo pipefail

ORGANIZATION="${DEX_TEST_CH_CLOUD_ORG_ID:-}"
SERVICE="${DEX_TEST_CH_CLOUD_SERVICE_ID:-}"
LIMIT="${DEX_TEST_CH_CLOUD_MAX_DAILY_CHC:-2}"
REPORT_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --organization) ORGANIZATION="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --report-only) REPORT_ONLY=true; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$ORGANIZATION" ]] || { echo "organization is required" >&2; exit 2; }
[[ -n "$SERVICE" ]] || { echo "service is required" >&2; exit 2; }
[[ "$LIMIT" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
  echo "daily CHC limit must be a non-negative number" >&2; exit 2; }
for tool in clickhousectl jq; do
  command -v "$tool" >/dev/null || { echo "$tool is required on PATH" >&2; exit 2; }
done

# The GitHub environment uses dex-specific names. clickhousectl deliberately
# receives only the two variables it understands, and neither value is printed.
export CLICKHOUSE_CLOUD_API_KEY="${DEX_TEST_CH_CLOUD_API_KEY:-${CLICKHOUSE_CLOUD_API_KEY:-}}"
export CLICKHOUSE_CLOUD_API_SECRET="${DEX_TEST_CH_CLOUD_API_SECRET:-${CLICKHOUSE_CLOUD_API_SECRET:-}}"
[[ -n "$CLICKHOUSE_CLOUD_API_KEY" ]] || { echo "Cloud API key is required" >&2; exit 2; }
[[ -n "$CLICKHOUSE_CLOUD_API_SECRET" ]] || { echo "Cloud API secret is required" >&2; exit 2; }

SERVICE_JSON=$(clickhousectl cloud service get "$SERVICE" --org-id "$ORGANIZATION" --json)
ACTUAL_ID=$(jq -er '.id' <<<"$SERVICE_JSON")
[[ "$ACTUAL_ID" == "$SERVICE" ]] || {
  echo "control plane returned service $ACTUAL_ID, expected $SERVICE" >&2; exit 1; }

TODAY=$(date -u +%F)
USAGE_JSON=$(clickhousectl cloud org usage \
  --org-id "$ORGANIZATION" --from-date "$TODAY" --to-date "$TODAY" --json)
COMPUTE_CHC=$(jq -er --arg service "$SERVICE" '
  [.costs[]? | select((.serviceId // .entityId) == $service) |
    (.metrics.computeCHC // 0)] | add // 0
' <<<"$USAGE_JSON")
PROVISIONAL=$(jq -er --arg service "$SERVICE" '
  any(.costs[]?; ((.serviceId // .entityId) == $service) and (.locked != true))
' <<<"$USAGE_JSON")
STATE=$(jq -er '.state' <<<"$SERVICE_JSON")

jq -n \
  --arg organization_id "$ORGANIZATION" \
  --arg service_id "$SERVICE" \
  --arg date "$TODAY" \
  --arg state "$STATE" \
  --argjson compute_chc "$COMPUTE_CHC" \
  --argjson limit_chc "$LIMIT" \
  --argjson provisional "$PROVISIONAL" \
  '{organization_id:$organization_id,service_id:$service_id,date:$date,
    service_state:$state,compute_chc:$compute_chc,limit_chc:$limit_chc,
    provisional:$provisional}'

if [[ "$REPORT_ONLY" != true ]] && ! jq -en \
    --argjson used "$COMPUTE_CHC" --argjson limit "$LIMIT" '$used < $limit' >/dev/null; then
  echo "ClickHouse Cloud admission refused: service has used ${COMPUTE_CHC} CHC today (limit ${LIMIT})" >&2
  exit 1
fi
