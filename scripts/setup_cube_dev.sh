#!/usr/bin/env bash
# Stand up a local Cube Core that renders SQL for dex, and nothing else.
#
# dex's governed Cube query (#913) asks a Cube server for the SQL a query would
# run (`/v1/sql`) and executes it through its own connector. So the Cube server
# here is a renderer: it is told which kind of warehouse to render for
# (CUBEJS_DB_TYPE) and is given no warehouse credentials at all. It cannot run a
# query, and a test that passes against it proves dex never needed it to.
#
# Usage:
#   scripts/setup_cube_dev.sh [--db duckdb|postgres|bigquery|snowflake|redshift]
#                             [--model DIR]
#   scripts/setup_cube_dev.sh --down          # remove every dex-cube-* container
#
# --db defaults to postgres. --model defaults to scripts/cube/model, the model the
# unit tests replay and the integration tests run; it is mounted read-only. One
# container per database type, each on its own loopback port:
#   duckdb 4000, postgres 4001, bigquery 4002, snowflake 4003, redshift 4004
# (DEX_CUBE_PORT overrides). Cube Core outside dev mode does not reload an edited
# model, so re-run this script after editing one: it recreates the container.
#
# The API secret is random, kept in ~/.cache/dex-dogfood/cube.env (0600) beside
# the per-database URLs, and never printed. `source` that file to export
# CUBEJS_API_SECRET and DEX_TEST_CUBE_URL_<DB> for dex and the tests.

set -euo pipefail

# cubejs/cube:v1.8.14, the release dex's Cube model rules are ported from
# (CUBE_VALIDATOR_RELEASE), pinned by its multi-arch index digest. Bump both
# together, re-reading the digest with
#   docker buildx imagetools inspect cubejs/cube:v1.8.14 --format '{{json .Manifest.Digest}}'
DIGEST=sha256:a09be2f036993869e45767f527807246600d7c0a8639c25ffdd23e8cbbb1c7d9
IMAGE="cubejs/cube@$DIGEST"
# The image's own `node` user, no capabilities: Cube Core binds an unprivileged
# port and writes nothing outside its own tmp. scripts/check_container_hardening.sh
# refuses the container if any of these is missing.
HARDENING=(
    --user node
    --cap-drop ALL
    --security-opt no-new-privileges:true
    --pids-limit 256
    --memory 1g
)
SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/dex-dogfood"
EXPORTS="$STATE_DIR/cube.env"
SECRET_FILE="$STATE_DIR/cube.secret"

DB=postgres
MODEL="$SCRIPTS/cube/model"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --down)
            for name in $(docker ps -a --format '{{.Names}}' | grep '^dex-cube-' || true); do
                docker rm -f "$name" >/dev/null
            done
            echo "removed every dex-cube-* container"
            exit 0
            ;;
        --db)
            DB=$2
            shift 2
            ;;
        --model)
            MODEL="$(cd "$2" && pwd)"
            shift 2
            ;;
        *)
            echo "usage: $0 [--db duckdb|postgres|bigquery|snowflake|redshift] [--model DIR] | --down" >&2
            exit 2
            ;;
    esac
done

# The warehouse each type renders for, described by nothing that reaches one: an
# address in TEST-NET-1 (RFC 5737), a project and an account that do not exist, a
# DuckDB file that is not there. Cube needs these set to start a driver; it never
# connects to plan a query.
case "$DB" in
    duckdb) PORT_DEFAULT=4000 DB_ENV=(-e CUBEJS_DB_DUCKDB_DATABASE_PATH=/nonexistent/render-only.duckdb) ;;
    postgres) PORT_DEFAULT=4001 DB_ENV=(-e CUBEJS_DB_HOST=192.0.2.1 -e CUBEJS_DB_NAME=render_only -e CUBEJS_DB_USER=render_only) ;;
    bigquery) PORT_DEFAULT=4002 DB_ENV=(-e CUBEJS_DB_BQ_PROJECT_ID=render-only-000000) ;;
    snowflake) PORT_DEFAULT=4003 DB_ENV=(-e CUBEJS_DB_SNOWFLAKE_ACCOUNT=render-only -e CUBEJS_DB_NAME=RENDER_ONLY -e CUBEJS_DB_USER=render_only -e CUBEJS_DB_SNOWFLAKE_WAREHOUSE=RENDER_ONLY) ;;
    redshift) PORT_DEFAULT=4004 DB_ENV=(-e CUBEJS_DB_HOST=192.0.2.1 -e CUBEJS_DB_NAME=render_only -e CUBEJS_DB_USER=render_only) ;;
    *)
        echo "--db must be one of duckdb, postgres, bigquery, snowflake, redshift" >&2
        exit 2
        ;;
esac
PORT="${DEX_CUBE_PORT:-$PORT_DEFAULT}"
CONTAINER="dex-cube-$DB"

if ! docker info >/dev/null 2>&1; then
    echo "docker daemon is not running; start Docker Desktop first" >&2
    exit 1
fi

install -d -m 700 "$STATE_DIR"
if [[ ! -s "$SECRET_FILE" ]]; then
    (
        umask 077
        openssl rand -hex 32 >"$SECRET_FILE"
    )
fi

# Recreated every run, so the container always serves the model on disk now.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
envfile="$(mktemp)"
trap 'rm -f "$envfile"' EXIT
chmod 600 "$envfile"
printf 'CUBEJS_API_SECRET=%s\n' "$(cat "$SECRET_FILE")" >"$envfile"
docker run -d --name "$CONTAINER" \
    "${HARDENING[@]}" \
    --env-file "$envfile" \
    -e CUBEJS_DEV_MODE=false \
    -e CUBEJS_TELEMETRY=false \
    -e CUBEJS_DB_TYPE="$DB" \
    "${DB_ENV[@]}" \
    -v "$MODEL:/cube/conf/model:ro" \
    -p "127.0.0.1:$PORT:4000" \
    "$IMAGE" >/dev/null
rm -f "$envfile"
"$SCRIPTS/check_container_hardening.sh" "$CONTAINER" "$DIGEST"
"$SCRIPTS/check_loopback_ports.sh" "$CONTAINER"

# Ready once the API answers at all: 403 without a token is an answer.
for _ in $(seq 1 120); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/cubejs-api/v1/meta" || true)
    [[ "$code" == "403" || "$code" == "200" ]] && break
    sleep 0.5
done
if [[ "${code:-}" != "403" && "${code:-}" != "200" ]]; then
    echo "Cube did not start; docker logs $CONTAINER" >&2
    exit 1
fi

(
    umask 077
    {
        printf 'export CUBEJS_API_SECRET="$(cat %q)"\n' "$SECRET_FILE"
        for db in duckdb postgres bigquery snowflake redshift; do
            if docker inspect "dex-cube-$db" >/dev/null 2>&1; then
                port=$(docker port "dex-cube-$db" 4000/tcp | head -1 | sed 's/.*://')
                upper=$(printf '%s' "$db" | tr '[:lower:]' '[:upper:]')
                printf 'export DEX_TEST_CUBE_URL_%s=http://127.0.0.1:%s/cubejs-api\n' "$upper" "$port"
            fi
        done
    } >"$EXPORTS"
)
echo "started $IMAGE as $CONTAINER on 127.0.0.1:$PORT, rendering for $DB"
echo "model: $MODEL (read-only)"
echo "exports: source $EXPORTS"
