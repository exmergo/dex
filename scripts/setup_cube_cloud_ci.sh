#!/usr/bin/env bash
# Deploy the Cube Cloud fixture and wire it into the protected CI environment.
#
# The `cube_cloud` integration job (#914) asks a Cube Cloud deployment to answer
# `explore semantic` both ways: Cube Cloud executes (`--api`, /v1/load), and Cube
# Cloud renders for dex to execute (`--local`, /v1/sql). The deployment serves
# scripts/cube/model over an in-memory DuckDB built from scripts/cube/shop.sql
# (scripts/cube_cloud/cube.py), so it holds no warehouse credential and reaches
# nothing; its numbers are the rows dex's own DuckDB fixture holds.
#
# Usage:
#   scripts/setup_cube_cloud_ci.sh --deployment <id> [--repo owner/name]
#                                  [--environment name] [--no-deploy]
#                                  [--keep-secret FILE]
#
# Needs the `cube` CLI logged in (`cube login`), `gh` authenticated with admin
# on the repository, and python3. It:
#   1. assembles cube.py, shop.sql and the model into a fresh directory and
#      deploys it (`cube deploy`, which needs the deployment in CLI deploy mode),
#      pruning every other file of the deployment;
#   2. creates the protected environment (deployments from main only, checked
#      across every page of branch policies) and sets DEX_TEST_CUBE_CLOUD_URL;
#   3. rotates the deployment's CUBEJS_API_SECRET to a fresh random one, checks
#      that a token signed with it (scripts/cube_cloud/mint_token.py) is
#      accepted, and stores it as CUBE_CLOUD_API_SECRET. Tokens Cube Cloud
#      issues itself last a day, too short for a weekly schedule, so the job
#      mints a one-hour token per run instead. The new key waits in a private
#      pending file until GitHub holds it, so an interrupted run is resumed by
#      running the script again. Run it from one machine at a time: two runs
#      rotating the same deployment at once can leave CI with the older key.
# --keep-secret also writes the new secret to FILE (mode 0600) for local runs of
# the suite (CUBE_CLOUD_API_SECRET=$(cat FILE) scripts/cube_cloud/mint_token.py),
# since the GitHub secret cannot be read back. Neither the secret nor a token is
# printed. Rotating the secret ends every
# token signed with the old one, the Cube Cloud console's included until it
# reloads; re-run with --no-deploy to rotate alone.

set -euo pipefail

DEPLOYMENT=""
REPO="exmergo/dex"
ENVIRONMENT="cube-cloud-integration"
DEPLOY=true
KEEP=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --deployment) DEPLOYMENT="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --environment) ENVIRONMENT="$2"; shift 2 ;;
        --no-deploy) DEPLOY=false; shift ;;
        --keep-secret) KEEP="$2"; shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done
[[ "$DEPLOYMENT" =~ ^[0-9]+$ ]] || {
    echo "--deployment <id> is required (cube deployments list)" >&2; exit 2; }
for tool in cube gh jq openssl python3 curl; do
    command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

URL="$(cube deployments get "$DEPLOYMENT" --json | jq -er '.deploymentUrl')/cubejs-api"
echo "==> deployment $DEPLOYMENT serves $URL"

if [[ "$DEPLOY" == true ]]; then
    echo "==> deploying the fixture (scripts/cube/model over scripts/cube/shop.sql)"
    mkdir "$WORK/project"
    cp "$SCRIPTS/cube_cloud/cube.py" "$SCRIPTS/cube/shop.sql" "$WORK/project/"
    cp -R "$SCRIPTS/cube/model" "$WORK/project/model"
    cube deploy "$DEPLOYMENT" --directory "$WORK/project" \
        --message "dex integration fixture (scripts/setup_cube_cloud_ci.sh)"
fi

echo "==> creating the protected GitHub environment (deployments from main only)"
# Before the key is rotated, so nothing here can fail with a new key in Cube
# Cloud and the old one in CI.
gh api --method PUT "repos/$REPO/environments/$ENVIRONMENT" --input - >/dev/null <<JSON
{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON
policies() {
    gh api --paginate "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" \
        --jq '.branch_policies[]? | "\(.id) \(.type) \(.name)"'
}
# The whole listing first, and only then any deletion: deleting while a later
# page is still to be read would shift entries past it.
ALL_POLICIES="$(policies)"
while read -r policy_id kind name; do
    [[ -n "$policy_id" ]] || continue
    if [[ "$kind" != "branch" || "$name" != "main" ]]; then
        gh api --method DELETE \
            "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies/$policy_id" >/dev/null
    fi
done <<<"$ALL_POLICIES"
if ! policies | grep -q ' branch main$'; then
    gh api --method POST "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" \
        -f name=main -f type=branch >/dev/null
fi
REMAINING=$(policies | awk '{print $2" "$3}' | sort -u)
[[ "$REMAINING" == "branch main" ]] || {
    echo "the environment allows more than main: $REMAINING" >&2; exit 1; }
gh variable set DEX_TEST_CUBE_CLOUD_URL --env "$ENVIRONMENT" --repo "$REPO" --body "$URL"

# The new key lives in a private pending file until GitHub holds it, so a run
# that stops after Cube Cloud accepted the key and before GitHub stored it is
# resumed by running the script again, with the same key, instead of leaving CI
# signing with a key Cube Cloud no longer accepts.
STATE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/dex-dogfood"
PENDING="$STATE_DIR/cube_cloud_secret.$DEPLOYMENT.pending"
install -d -m 700 "$STATE_DIR"
if [[ -s "$PENDING" ]]; then
    echo "==> resuming: publishing the key an interrupted run left in $PENDING"
else
    echo "==> generating a new API secret"
    (
        umask 077
        openssl rand -hex 32 >"$PENDING.tmp"
    )
    mv "$PENDING.tmp" "$PENDING"
fi
echo "==> setting it on the deployment"
# `cube variables set` takes KEY=VALUE arguments only, so the value is on this
# one command line for the moment it runs.
cube variables set "$DEPLOYMENT" "CUBEJS_API_SECRET=$(cat "$PENDING")" >/dev/null

echo "==> checking a token signed with it against the deployment"
code=""
for _ in $(seq 1 30); do
    token=$(CUBE_CLOUD_API_SECRET="$(cat "$PENDING")" python3 "$SCRIPTS/cube_cloud/mint_token.py")
    # A bounded transfer, and a failed one is one more attempt, not an exit.
    code=$(curl -s --max-time 20 -o /dev/null -w '%{http_code}' \
        -H "Authorization: $token" "$URL/v1/meta") || code="000"
    unset token
    [[ "$code" == "200" ]] && break
    sleep 10
done
[[ "$code" == "200" ]] || {
    echo "the deployment answered /v1/meta with HTTP $code; the key stays in $PENDING, re-run to resume" >&2
    exit 1
}

echo "==> publishing it to the environment"
gh secret set CUBE_CLOUD_API_SECRET --env "$ENVIRONMENT" --repo "$REPO" <"$PENDING"
if [[ -n "$KEEP" ]]; then
    # Written private from the start, then moved over the destination.
    keep_tmp="$(mktemp "$(dirname "$KEEP")/.cube_cloud_secret.XXXXXX")"
    chmod 600 "$keep_tmp"
    cat "$PENDING" >"$keep_tmp"
    mv "$keep_tmp" "$KEEP"
fi
rm -f "$PENDING"
# The token secret an earlier version of this script stored, if it is there.
gh secret delete CUBE_CLOUD_API_TOKEN --env "$ENVIRONMENT" --repo "$REPO" >/dev/null 2>&1 || true

gh variable list --env "$ENVIRONMENT" --repo "$REPO" --json name --jq '.[].name' \
    | grep -qx DEX_TEST_CUBE_CLOUD_URL || { echo "variable missing" >&2; exit 1; }
gh secret list --env "$ENVIRONMENT" --repo "$REPO" --json name --jq '.[].name' \
    | grep -qx CUBE_CLOUD_API_SECRET || { echo "secret missing" >&2; exit 1; }
echo "done: environment $ENVIRONMENT holds DEX_TEST_CUBE_CLOUD_URL and CUBE_CLOUD_API_SECRET"
