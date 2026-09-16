#!/usr/bin/env bash
# Local runner for the same preflight and pytest marker used in GitHub Actions.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
REQUIRED=(
  DEX_TEST_CH_CLOUD_ORG_ID DEX_TEST_CH_CLOUD_SERVICE_ID
  DEX_TEST_CH_CLOUD_HOST DEX_TEST_CH_CLOUD_PORT
  DEX_TEST_CH_CLOUD_DATABASE DEX_TEST_CH_CLOUD_DEV_DATABASE
  DEX_TEST_CH_CLOUD_COMPUTE_UNIT_PRICE_USD DEX_TEST_CH_CLOUD_MAX_SECONDS
  DEX_TEST_CH_CLOUD_MAX_DAILY_CHC DEX_TEST_CH_CLOUD_DSN
  DEX_TEST_CH_CLOUD_DEV_PASSWORD DEX_TEST_CH_CLOUD_API_KEY
  DEX_TEST_CH_CLOUD_API_SECRET
)
for name in "${REQUIRED[@]}"; do
  [[ -n "${!name:-}" ]] || { echo "$name is required" >&2; exit 2; }
done

"$ROOT/scripts/clickhouse_cloud/preflight.sh"
cd "$ROOT/packages/dex-core"
uv run pytest tests/integration/test_clickhouse_cloud.py -q --tb=line \
  -m clickhouse_cloud "$@"
