#!/usr/bin/env bash
# Stand up a local Cube Core that renders SQL for dex, and nothing else; or, with
# --execute, one that runs queries itself for `explore semantic --api` (#914).
#
# dex's governed Cube query (#913) asks a Cube server for the SQL a query would
# run (`/v1/sql`) and executes it through its own connector. So the Cube server
# here is a renderer: it is told which kind of warehouse to render for
# (CUBEJS_DB_TYPE) and is given no warehouse credentials at all. It cannot run a
# query, and a test that passes against it proves dex never needed it to.
#
# Usage:
#   scripts/setup_cube_dev.sh [--db duckdb|postgres|bigquery|snowflake|redshift|
#                                   clickhouse|trino]
#                             [--model DIR]
#   scripts/setup_cube_dev.sh --execute --db duckdb|postgres|bigquery [--model DIR]
#   scripts/setup_cube_dev.sh --down          # remove every dex-cube-* container
#
# --execute starts an *executing* Core instead (container dex-cube-<db>-exec,
# ports 4010 duckdb, 4011 postgres, 4012 bigquery), configured by
# scripts/cube/execute.js. It is the only mode that gives Cube a warehouse:
#   duckdb    in-memory, built from scripts/cube/shop.sql (dex's own fixture rows)
#   postgres  the dogfood database as dex_ro, read-only: the dex-pg container
#             of scripts/setup_postgres_dev.sh over a private network
#             (DEX_CUBE_PG_CONTAINER names another), else a database published
#             on the host (CI's service container; DEX_CUBE_PG_PORT, default 5432)
#   bigquery  your Application Default Credentials, mounted read-only, on the
#             project DEX_CUBE_BQ_PROJECT names (dogfood only; CI never runs it)
# Its URL is exported as DEX_TEST_CUBE_EXEC_URL_<DB>.
#
# --db defaults to postgres. --model defaults to scripts/cube/model, the model the
# unit tests replay and the integration tests run; it is mounted read-only. One
# container per database type, each on its own loopback port:
#   duckdb 4000, postgres 4001, bigquery 4002, snowflake 4003, redshift 4004,
#   clickhouse 4005, trino 4006
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
CONF="$SCRIPTS/cube/cube.js"
EXECUTE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --execute)
            EXECUTE=1
            shift
            ;;
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
            echo "usage: $0 [--execute] [--db duckdb|postgres|bigquery|snowflake|redshift|clickhouse|trino] [--model DIR] | --down" >&2
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
    clickhouse) PORT_DEFAULT=4005 DB_ENV=(-e CUBEJS_DB_HOST=192.0.2.1 -e CUBEJS_DB_NAME=render_only -e CUBEJS_DB_USER=render_only) ;;
    trino) PORT_DEFAULT=4006 DB_ENV=(-e CUBEJS_DB_HOST=192.0.2.1 -e CUBEJS_DB_PRESTO_CATALOG=render_only -e CUBEJS_DB_SCHEMA=render_only -e CUBEJS_DB_USER=render_only) ;;
    *)
        echo "--db must be one of duckdb, postgres, bigquery, snowflake, redshift, clickhouse, trino" >&2
        exit 2
        ;;
esac
CONTAINER="dex-cube-$DB"
EXTRA=(-v "$CONF:/cube/conf/cube.js:ro")
SECRET_ENV=()
if [[ $EXECUTE == 1 ]]; then
    # The one mode that hands Cube a warehouse, each type reaching only its own:
    # nothing here is a credential dex itself uses.
    CONTAINER="dex-cube-$DB-exec"
    # Outside dev mode Cube queues and caches queries in Cube Store; one
    # container with no Cube Store keeps both in memory instead.
    EXTRA=(-v "$SCRIPTS/cube/execute.js:/cube/conf/cube.js:ro"
        -e CUBEJS_CACHE_AND_QUEUE_DRIVER=memory)
    case "$DB" in
        duckdb)
            PORT_DEFAULT=4010
            DB_ENV=()
            EXTRA+=(-v "$SCRIPTS/cube/shop.sql:/cube/conf/shop.sql:ro")
            ;;
        postgres)
            PORT_DEFAULT=4011
            PG_CONTAINER="${DEX_CUBE_PG_CONTAINER:-dex-pg}"
            if docker inspect "$PG_CONTAINER" >/dev/null 2>&1; then
                if [[ "$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER")" != "true" ]]; then
                    echo "$PG_CONTAINER exists but is not running; start it (scripts/setup_postgres_dev.sh) and re-run" >&2
                    exit 1
                fi
                # The dogfood database (scripts/setup_postgres_dev.sh) publishes
                # on 127.0.0.1 only, which a container cannot reach through the
                # host gateway on native Linux. Join it on a private network
                # instead, and leave its host binding as it is.
                docker network inspect dex-cube >/dev/null 2>&1 \
                    || docker network create dex-cube >/dev/null
                if ! docker network connect dex-cube "$PG_CONTAINER" 2>/dev/null; then
                    # Already a member is fine; anything else (the container
                    # went away) would leave Cube with an address it cannot reach.
                    pg_id=$(docker inspect -f '{{.Id}}' "$PG_CONTAINER")
                    members=$(docker network inspect dex-cube \
                        -f '{{range $id, $c := .Containers}}{{$id}} {{end}}' 2>/dev/null || true)
                    if [[ " $members " != *" $pg_id "* ]]; then
                        echo "could not attach $PG_CONTAINER to the dex-cube network" >&2
                        exit 1
                    fi
                fi
                DB_ENV=(-e CUBEJS_DB_HOST="$PG_CONTAINER" -e CUBEJS_DB_PORT=5432)
                EXTRA+=(--network dex-cube)
            else
                # A database published on the host's interfaces, as CI's
                # service container is.
                DB_ENV=(-e CUBEJS_DB_HOST=host.docker.internal
                    -e CUBEJS_DB_PORT="${DEX_CUBE_PG_PORT:-5432}")
                EXTRA+=(--add-host host.docker.internal:host-gateway)
            fi
            DB_ENV+=(-e CUBEJS_DB_NAME=dex_dogfood -e CUBEJS_DB_USER=dex_ro -e CUBEJS_DB_SSL=false)
            SECRET_ENV=("CUBEJS_DB_PASS=dex_ro")
            ;;
        bigquery)
            PORT_DEFAULT=4012
            ADC="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/.config/gcloud/application_default_credentials.json}"
            if [[ -z "${DEX_CUBE_BQ_PROJECT:-}" || ! -r "$ADC" ]]; then
                echo "--execute --db bigquery needs DEX_CUBE_BQ_PROJECT and readable ADC ($ADC)" >&2
                exit 2
            fi
            DB_ENV=(-e CUBEJS_DB_BQ_PROJECT_ID="$DEX_CUBE_BQ_PROJECT"
                -e GOOGLE_APPLICATION_CREDENTIALS=/cube/conf/adc.json)
            EXTRA+=(-v "$ADC:/cube/conf/adc.json:ro")
            ;;
        *)
            echo "--execute runs duckdb, postgres or bigquery" >&2
            exit 2
            ;;
    esac
fi
PORT="${DEX_CUBE_PORT:-$PORT_DEFAULT}"

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
for line in "${SECRET_ENV[@]+"${SECRET_ENV[@]}"}"; do
    printf '%s\n' "$line" >>"$envfile"
done
docker run -d --name "$CONTAINER" \
    "${HARDENING[@]}" \
    --env-file "$envfile" \
    -e CUBEJS_DEV_MODE=false \
    -e CUBEJS_TELEMETRY=false \
    -e CUBEJS_DB_TYPE="$DB" \
    "${DB_ENV[@]+"${DB_ENV[@]}"}" \
    -v "$MODEL:/cube/conf/model:ro" \
    "${EXTRA[@]}" \
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
        for db in duckdb postgres bigquery snowflake redshift clickhouse trino; do
            if docker inspect "dex-cube-$db" >/dev/null 2>&1; then
                port=$(docker port "dex-cube-$db" 4000/tcp | head -1 | sed 's/.*://')
                upper=$(printf '%s' "$db" | tr '[:lower:]' '[:upper:]')
                printf 'export DEX_TEST_CUBE_URL_%s=http://127.0.0.1:%s/cubejs-api\n' "$upper" "$port"
            fi
            if docker inspect "dex-cube-$db-exec" >/dev/null 2>&1; then
                port=$(docker port "dex-cube-$db-exec" 4000/tcp | head -1 | sed 's/.*://')
                upper=$(printf '%s' "$db" | tr '[:lower:]' '[:upper:]')
                printf 'export DEX_TEST_CUBE_EXEC_URL_%s=http://127.0.0.1:%s/cubejs-api\n' "$upper" "$port"
            fi
        done
    } >"$EXPORTS"
)
if [[ $EXECUTE == 1 ]]; then
    echo "started $IMAGE as $CONTAINER on 127.0.0.1:$PORT, executing on $DB"
else
    echo "started $IMAGE as $CONTAINER on 127.0.0.1:$PORT, rendering for $DB"
fi
echo "model: $MODEL (read-only)"
echo "exports: source $EXPORTS"
