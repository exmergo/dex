#!/usr/bin/env bash
# One-time provisioning for the live BigQuery integration suite
# (.github/workflows/integration.yml). Run by a maintainer with owner-level
# gcloud credentials and gh authenticated against the repository.
#
# What it sets up, all identity-based (no key is ever created or stored):
#   - the dex-ci service account, allowed to run BigQuery jobs on the project
#     and to write ONLY inside the scratch dataset (the IAM enforcement of
#     "dex never writes outside the dev dataset")
#   - the scratch dataset, with a default table TTL so crashed runs self-clean
#   - a dedicated GitHub OIDC provider inside an EXISTING Workload Identity
#     Pool, pinned to this repository and to the gcp-integration environment
#   - the GitHub environment (deployments restricted to main) carrying the
#     three variables the workflow reads
#
# Everything project-specific is a parameter, so nothing private lives in this
# script. Idempotent: safe to re-run; existing resources are left in place.
#
# Usage:
#   scripts/setup_bigquery_ci.sh <gcp-project-id> [workload-identity-pool]
#
# Defaults: pool github-actions-pool, repo taken from `gh repo view`,
# dataset dex_ci, environment gcp-integration, location US.

set -euo pipefail

PROJECT_ID="${1:-}"
POOL="${2:-github-actions-pool}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "usage: $0 <gcp-project-id> [workload-identity-pool]" >&2
  exit 1
fi

REPO="${DEX_CI_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
PROVIDER="dex-github-oidc"
SA_NAME="dex-ci"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
DATASET="dex_ci"
LOCATION="US"
GH_ENV="gcp-integration"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

echo "== dex BigQuery CI setup =="
echo "   project:     ${PROJECT_ID} (${PROJECT_NUMBER})"
echo "   repository:  ${REPO}"
echo "   pool:        ${POOL} (must already exist)"
echo "   provider:    ${PROVIDER}"
echo "   service acct ${SA}"
echo "   dataset:     ${DATASET} (${LOCATION})"
echo "   github env:  ${GH_ENV}"
echo

echo "-- Enabling required APIs"
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com \
  --project="$PROJECT_ID"

echo "-- Service account"
if gcloud iam service-accounts describe "$SA" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "   ${SA} already exists"
else
  gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT_ID" \
    --display-name="dex BigQuery integration CI (GitHub Actions via WIF)"
fi

echo "-- Scratch dataset (24h table TTL)"
if bq show --project_id="$PROJECT_ID" "$DATASET" >/dev/null 2>&1; then
  echo "   ${PROJECT_ID}:${DATASET} already exists"
else
  bq mk --dataset --location="$LOCATION" --default_table_expiration=86400 \
    --description="dex integration scratch; tables expire after 24h" \
    "${PROJECT_ID}:${DATASET}"
fi

echo "-- OIDC provider in the existing pool (pinned to ${REPO} + ${GH_ENV})"
if ! gcloud iam workload-identity-pools describe "$POOL" \
  --project="$PROJECT_ID" --location=global >/dev/null 2>&1; then
  echo "   pool '${POOL}' does not exist; create it first or pass its name" >&2
  exit 1
fi
if gcloud iam workload-identity-pools providers describe "$PROVIDER" \
  --project="$PROJECT_ID" --location=global \
  --workload-identity-pool="$POOL" >/dev/null 2>&1; then
  echo "   provider ${PROVIDER} already exists"
else
  # The attribute condition is the load-bearing line: only tokens minted for
  # this repository's gcp-integration environment are accepted. Other repos
  # sharing the pool keep their own providers and conditions.
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --project="$PROJECT_ID" --location=global \
    --workload-identity-pool="$POOL" \
    --display-name="GitHub OIDC (${REPO})" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.environment=assertion.environment" \
    --attribute-condition="assertion.repository == '${REPO}' && assertion.environment == '${GH_ENV}'"
fi

echo "-- Impersonation binding (repo-scoped principalSet; never the bare pool)"
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"

echo "-- BigQuery grants"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" \
  --role="roles/bigquery.jobUser" \
  --condition=None
# Dataset-level write access via SQL GRANT: `bq add-iam-policy-binding` on
# datasets is allowlist-gated, and dataset ACL WRITER is the same role.
bq query --project_id="$PROJECT_ID" --use_legacy_sql=false \
  "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`${PROJECT_ID}.${DATASET}\` TO 'serviceAccount:${SA}'"

echo "-- GitHub environment (deployments restricted to main)"
printf '{ "deployment_branch_policy": { "protected_branches": false, "custom_branch_policies": true } }' \
  | gh api -X PUT "repos/${REPO}/environments/${GH_ENV}" --input - >/dev/null
# Adding the same branch policy twice errors; tolerate re-runs.
gh api -X POST "repos/${REPO}/environments/${GH_ENV}/deployment-branch-policies" \
  -f name=main >/dev/null 2>&1 || echo "   branch policy for main already present"

echo "-- GitHub environment variables (identifiers, not secrets: WIF stores no credential)"
gh variable set DEX_TEST_BQ_PROJECT --repo "$REPO" --env "$GH_ENV" --body "$PROJECT_ID"
gh variable set GCP_INTEGRATION_SA --repo "$REPO" --env "$GH_ENV" --body "$SA"
gh variable set GCP_WIF_PROVIDER --repo "$REPO" --env "$GH_ENV" \
  --body "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

echo
echo "== Done. Verify with:"
echo "   gh workflow run integration.yml --repo ${REPO} && gh run watch --repo ${REPO}"
echo "== Recommended backstop (Console, no CLI): set the BigQuery 'Query usage"
echo "   per day' quota on ${PROJECT_ID} so even a compromised token is capped."
