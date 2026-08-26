#!/usr/bin/env bash
# Stand up (or re-seed) the ClickHouse target for the clickhouse connector.
#
# Unlike the cloud connectors' setup scripts, nothing here provisions cloud
# infrastructure: self-hosted ClickHouse is a container, so this script is both
# the local dogfood stand-up and what the clickhouse job in
# .github/workflows/integration.yml runs. That is deliberate. The Postgres pair
# spells its seeding twice (once here, once as a psql step in the workflow) and
# the two can drift; this connector has exactly one seeding path, so they cannot.
#
# The seed is applied through `clickhouse-client --multiquery` inside the
# container rather than over HTTP, because the HTTP interface refuses
# multi-statement bodies.
#
# Usage:
#   scripts/setup_clickhouse_dev.sh          # start container if needed, (re)seed
#   scripts/setup_clickhouse_dev.sh --down   # remove the container
#
# Idempotent: re-running drops and recreates the app and dbt_dev databases.

set -euo pipefail

CONTAINER=dex-ch
HTTP_PORT="${DEX_CH_PORT:-8124}"
NATIVE_PORT="${DEX_CH_NATIVE_PORT:-9001}"
IMAGE=clickhouse/clickhouse-server:25.3
SEED="$(cd "$(dirname "$0")" && pwd)/clickhouse_seed.sql"

if [[ "${1:-}" == "--down" ]]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    echo "removed container $CONTAINER"
    exit 0
fi

if ! docker info >/dev/null 2>&1; then
    echo "docker daemon is not running; start Docker Desktop first" >&2
    exit 1
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    # CLICKHOUSE_SKIP_USER_SETUP leaves the built-in `default` user open so the
    # seed can create the two real users below; dex never connects as `default`.
    docker run -d --name "$CONTAINER" \
        -e CLICKHOUSE_SKIP_USER_SETUP=1 \
        -p "$HTTP_PORT":8123 \
        -p "$NATIVE_PORT":9000 \
        --ulimit nofile=262144:262144 \
        "$IMAGE" >/dev/null
    echo "started $IMAGE as $CONTAINER on http port $HTTP_PORT"
elif [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]]; then
    docker start "$CONTAINER" >/dev/null
    echo "restarted existing container $CONTAINER"
fi

# The server accepts TCP before it will answer queries, so poll /ping rather
# than sleeping a fixed amount.
for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" wget -q -O- http://localhost:8123/ping 2>/dev/null | grep -q Ok; then
        break
    fi
    sleep 1
done

docker exec -i "$CONTAINER" clickhouse-client --multiquery <"$SEED"

echo "seeded app and dbt_dev from scripts/clickhouse_seed.sql"
echo
echo "connect dex as the read-only user:"
echo "  export DEX_TEST_CH_DSN=clickhouse://dex_ro:dex_ro@localhost:$HTTP_PORT/app"
echo "dbt dev builds (transform build) authenticate as dbt_dev:"
echo "  export DEX_TEST_CH_DEV_PASSWORD=dbt_dev   # profiles.yml reads it via env_var"
