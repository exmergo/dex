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
#   2. rotates the deployment's CUBEJS_API_SECRET to a fresh random one, and
#      checks that a token signed with it (scripts/cube_cloud/mint_token.py) is
#      accepted. Tokens Cube Cloud issues itself last a day, too short for a
#      weekly schedule, so the job mints a one-hour token per run instead;
#   3. creates the protected environment (deployments from main only), and sets
#      DEX_TEST_CUBE_CLOUD_URL (a variable) and CUBE_CLOUD_API_SECRET (a secret).
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

echo "==> rotating the deployment's API secret"
SECRET_FILE="$WORK/secret"
(
    umask 077
    openssl rand -hex 32 >"$SECRET_FILE"
)
# `cube variables set` takes KEY=VALUE arguments only, so the value is on this
# one command line for the moment it runs.
cube variables set "$DEPLOYMENT" "CUBEJS_API_SECRET=$(cat "$SECRET_FILE")" >/dev/null

echo "==> checking a token signed with it against the deployment"
code=""
for _ in $(seq 1 30); do
    token=$(CUBE_CLOUD_API_SECRET="$(cat "$SECRET_FILE")" python3 "$SCRIPTS/cube_cloud/mint_token.py")
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: $token" "$URL/v1/meta")
    unset token
    [[ "$code" == "200" ]] && break
    sleep 10
done
[[ "$code" == "200" ]] || { echo "the deployment answered /v1/meta with HTTP $code" >&2; exit 1; }

echo "==> creating the protected GitHub environment and writing configuration"
gh api --method PUT "repos/$REPO/environments/$ENVIRONMENT" --input - >/dev/null <<JSON
{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON
POLICIES_JSON=$(gh api "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies")
while IFS= read -r policy_id; do
    [[ -n "$policy_id" ]] || continue
    gh api --method DELETE \
        "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies/$policy_id" >/dev/null
done < <(jq -r '.branch_policies[]? | select(.name != "main" or .type != "branch") | .id' \
    <<<"$POLICIES_JSON")
if ! jq -e 'any(.branch_policies[]?; .name == "main" and .type == "branch")' \
    >/dev/null <<<"$POLICIES_JSON"; then
    gh api --method POST "repos/$REPO/environments/$ENVIRONMENT/deployment-branch-policies" \
        -f name=main -f type=branch >/dev/null
fi
gh variable set DEX_TEST_CUBE_CLOUD_URL --env "$ENVIRONMENT" --repo "$REPO" --body "$URL"
gh secret set CUBE_CLOUD_API_SECRET --env "$ENVIRONMENT" --repo "$REPO" <"$SECRET_FILE"
if [[ -n "$KEEP" ]]; then
    (
        umask 077
        cp "$SECRET_FILE" "$KEEP"
    )
    chmod 600 "$KEEP"
fi
rm -f "$SECRET_FILE"
# The token secret an earlier version of this script stored, if it is there.
gh secret delete CUBE_CLOUD_API_TOKEN --env "$ENVIRONMENT" --repo "$REPO" >/dev/null 2>&1 || true

gh variable list --env "$ENVIRONMENT" --repo "$REPO" --json name --jq '.[].name' \
    | grep -qx DEX_TEST_CUBE_CLOUD_URL || { echo "variable missing" >&2; exit 1; }
gh secret list --env "$ENVIRONMENT" --repo "$REPO" --json name --jq '.[].name' \
    | grep -qx CUBE_CLOUD_API_SECRET || { echo "secret missing" >&2; exit 1; }
echo "done: environment $ENVIRONMENT holds DEX_TEST_CUBE_CLOUD_URL and CUBE_CLOUD_API_SECRET"
