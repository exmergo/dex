#!/usr/bin/env bash
# Stand up (or re-seed) the local dogfood PostgreSQL for the postgres connector.
#
# Unlike the cloud connectors' setup scripts, nothing here provisions cloud
# infrastructure: the operational-database connector is exercised against a
# throwaway local container. The same scripts/postgres_seed.sql drives the CI
# service container in .github/workflows/integration.yml.
#
# Usage:
#   scripts/setup_postgres_dev.sh          # start container if needed, (re)seed
#   scripts/setup_postgres_dev.sh --down   # remove the container
#
# Idempotent: re-running drops and recreates the dex_dogfood database.

set -euo pipefail

CONTAINER=dex-pg
PORT="${DEX_PG_PORT:-5433}"
IMAGE=postgres:16
SUPERPASS=postgres
SEED="$(cd "$(dirname "$0")" && pwd)/postgres_seed.sql"

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
    docker run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD="$SUPERPASS" \
        -p "$PORT":5432 \
        "$IMAGE" >/dev/null
    echo "started $IMAGE as $CONTAINER on port $PORT"
elif [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]]; then
    docker start "$CONTAINER" >/dev/null
    echo "restarted existing container $CONTAINER"
fi

until docker exec "$CONTAINER" pg_isready -U postgres -q; do
    sleep 0.5
done

docker exec "$CONTAINER" psql -U postgres -q \
    -c "DROP DATABASE IF EXISTS dex_dogfood WITH (FORCE)" \
    -c "CREATE DATABASE dex_dogfood"
docker exec -i "$CONTAINER" psql -U postgres -q -d dex_dogfood -v ON_ERROR_STOP=1 \
    <"$SEED"

echo "seeded dex_dogfood from scripts/postgres_seed.sql"
echo
echo "connect dex as the read-only role:"
echo "  export DATABASE_URL=postgresql://dex_ro:dex_ro@localhost:$PORT/dex_dogfood"
echo "dbt dev builds (transform build) authenticate as dbt_dev:"
echo "  export PGPASSWORD=dbt_dev   # profiles.yml reads it via env_var"
