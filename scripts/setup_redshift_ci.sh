#!/usr/bin/env bash
# One-time provisioning for the Redshift connector: the local dev/test loop
# and the live integration suite (.github/workflows/integration.yml). Run by a
# maintainer whose AWS credentials can administer Redshift Serverless, IAM, and
# Secrets Manager (the seed connects as the namespace admin, whose password is
# read from the managed secret; creating database users is superuser-only on
# Redshift), with psql on PATH (Redshift speaks the Postgres wire protocol) and
# gh authenticated against the repository.
#
# What it sets up, keyless wherever the platform allows it:
#   - a Redshift Serverless namespace + workgroup at the smallest base
#     capacity (8 RPUs), created only when absent
#   - a daily RPU-hours usage limit with a query-deactivating breach action:
#     the hard cost backstop; nothing dex does can outspend it
#   - the seeded `app` schema (the same shape as scripts/postgres_seed.sql:
#     PII columns for flag-not-surface, a deliberately undeclared FK for
#     relationship inference, a SUPER column for type degradation, an events
#     table big enough to exercise the cost gate) plus the empty dbt_dev
#     schema
#   - dex_ro: the read-only database user dex connects as (USAGE + SELECT on
#     app only)
#   - dbt_dev: the database user dbt builds as (reads app, writes only schema
#     dbt_dev, a durable statement_timeout as the per-statement cap dex
#     cannot inject through dbt-redshift). Its password is rotated on every
#     run and goes straight into the GitHub environment as a secret; it is
#     never written to this machine.
#   - an IAM role for CI whose trust policy accepts only GitHub OIDC tokens
#     minted for this repository's redshift-integration environment, holding
#     GetWorkgroup/GetNamespace/GetCredentials on this workgroup only (the
#     engine then authenticates exactly as a developer laptop does: the AWS
#     default chain plus IAM temporary database credentials; no stored keys)
#   - dex_ci_reader: a database role with the same read access as dex_ro,
#     granted to the CI role's minted database user (IAMR:<role-name>), which
#     is what GetCredentials mints when the job assumes the role. Without it
#     the CI identity would connect with no privileges; dex_ro's grants do not
#     reach it because Serverless names the user after the IAM identity.
#   - the GitHub environment (deployments restricted to main) carrying the
#     variables the workflow reads and the dbt password secret
#
# Everything account-specific is a parameter, so nothing private lives in this
# script. Idempotent: safe to re-run; existing resources are left in place and
# the trust pinning, the seed, and the grants are refreshed. The admin password
# is managed in Secrets Manager (enabled on first run, rotating any manually
# set one) and fetched with the operator's AWS credentials, never stored here.
# The dbt_dev password is rotated on every run: both halves (the database user
# and the GitHub secret) are replaced together, so they cannot drift apart.
#
# Usage:
#   scripts/setup_redshift_ci.sh \
#     --region us-east-1 \
#     [--workgroup dex-ci] [--namespace dex-ci] [--database dev] \
#     [--repo exmergo/dex] [--environment redshift-integration] \
#     [--daily-rpu-hours 4] [--skip-github]

set -euo pipefail

REGION="us-east-1"
WORKGROUP="dex-ci"
NAMESPACE="dex-ci"
DATABASE="dev"
REPO="exmergo/dex"
ENVIRONMENT="redshift-integration"
DAILY_RPU_HOURS="4"
SKIP_GITHUB="false"
ROLE_NAME="dex-redshift-integration"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --workgroup) WORKGROUP="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --database) DATABASE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --daily-rpu-hours) DAILY_RPU_HOURS="$2"; shift 2 ;;
    --skip-github) SKIP_GITHUB="true"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$REGION" ]] || { echo "--region is required" >&2; exit 2; }
for tool in aws psql python3; do
  command -v "$tool" >/dev/null || { echo "$tool is required on PATH" >&2; exit 2; }
done
if [[ "$SKIP_GITHUB" != "true" ]]; then
  command -v gh >/dev/null || { echo "gh is required (or pass --skip-github)" >&2; exit 2; }
fi

export AWS_DEFAULT_REGION="$REGION"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "==> namespace + workgroup (smallest base capacity)"
if ! aws redshift-serverless get-namespace --namespace-name "$NAMESPACE" >/dev/null 2>&1; then
  aws redshift-serverless create-namespace \
    --namespace-name "$NAMESPACE" --db-name "$DATABASE" >/dev/null
fi
if ! aws redshift-serverless get-workgroup --workgroup-name "$WORKGROUP" >/dev/null 2>&1; then
  aws redshift-serverless create-workgroup \
    --workgroup-name "$WORKGROUP" --namespace-name "$NAMESPACE" \
    --base-capacity 8 --publicly-accessible >/dev/null
fi
echo "    waiting for the workgroup to become AVAILABLE..."
for _ in $(seq 1 60); do
  STATUS=$(aws redshift-serverless get-workgroup --workgroup-name "$WORKGROUP" \
    --query 'workgroup.status' --output text)
  [[ "$STATUS" == "AVAILABLE" ]] && break
  sleep 10
done
[[ "$STATUS" == "AVAILABLE" ]] || { echo "workgroup never became AVAILABLE" >&2; exit 1; }

WORKGROUP_ARN=$(aws redshift-serverless get-workgroup --workgroup-name "$WORKGROUP" \
  --query 'workgroup.workgroupArn' --output text)
HOST=$(aws redshift-serverless get-workgroup --workgroup-name "$WORKGROUP" \
  --query 'workgroup.endpoint.address' --output text)

echo "==> daily RPU-hours usage limit (the hard cost backstop)"
EXISTING_LIMIT=$(aws redshift-serverless list-usage-limits --resource-arn "$WORKGROUP_ARN" \
  --query 'usageLimits[?usageType==`serverless-compute`].usageLimitId' --output text)
if [[ -z "$EXISTING_LIMIT" ]]; then
  aws redshift-serverless create-usage-limit \
    --resource-arn "$WORKGROUP_ARN" --usage-type serverless-compute \
    --amount "$DAILY_RPU_HOURS" --period daily --breach-action deactivate >/dev/null
else
  aws redshift-serverless update-usage-limit \
    --usage-limit-id "$EXISTING_LIMIT" \
    --amount "$DAILY_RPU_HOURS" --breach-action deactivate >/dev/null
fi

echo "==> GitHub OIDC provider + the CI role (trust pinned to repo + environment)"
OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
if ! aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com >/dev/null
fi
TRUST=$(python3 - "$OIDC_ARN" "$REPO" "$ENVIRONMENT" <<'PY'
import json, sys
oidc, repo, environment = sys.argv[1:4]
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Federated": oidc},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {
            "StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub":
                    f"repo:{repo}:environment:{environment}",
            }
        },
    }],
}))
PY
)
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
    --policy-document "$TRUST"
else
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST" >/dev/null
fi
NAMESPACE_ARN="arn:aws:redshift-serverless:${REGION}:${ACCOUNT_ID}:namespace/$(aws redshift-serverless get-namespace --namespace-name "$NAMESPACE" --query 'namespace.namespaceId' --output text)"
POLICY=$(python3 - "$WORKGROUP_ARN" "$NAMESPACE_ARN" <<'PY'
import json, sys
workgroup_arn, namespace_arn = sys.argv[1:3]
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "redshift-serverless:GetWorkgroup",
            "redshift-serverless:GetNamespace",
            "redshift-serverless:GetCredentials",
        ],
        "Resource": [workgroup_arn, namespace_arn],
    }],
}))
PY
)
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name dex-redshift-integration --policy-document "$POLICY"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# The seed creates database users, which on Redshift is a superuser-only
# operation, and the only superuser on a Serverless namespace is its admin
# (an IAM-minted user cannot be one: Redshift forbids a superuser with the
# disabled password IAM identities carry). So the seed connects as the admin.
# It stays keyless for the operator: the admin password is managed in Secrets
# Manager and fetched with the operator's own AWS credentials, never stored on
# this machine. Enabling management here is idempotent and rotates any
# manually-set admin password into the secret.
echo "==> ensuring the namespace admin password is managed in Secrets Manager"
SECRET_ARN=$(aws redshift-serverless get-namespace --namespace-name "$NAMESPACE" \
  --query 'namespace.adminPasswordSecretArn' --output text)
if [[ "$SECRET_ARN" == "None" || -z "$SECRET_ARN" ]]; then
  aws redshift-serverless update-namespace --namespace-name "$NAMESPACE" \
    --manage-admin-password >/dev/null
  echo "    waiting for the managed secret to populate..."
  for _ in $(seq 1 30); do
    NS_STATUS=$(aws redshift-serverless get-namespace --namespace-name "$NAMESPACE" \
      --query 'namespace.status' --output text)
    SECRET_ARN=$(aws redshift-serverless get-namespace --namespace-name "$NAMESPACE" \
      --query 'namespace.adminPasswordSecretArn' --output text)
    [[ "$NS_STATUS" == "AVAILABLE" && "$SECRET_ARN" != "None" && -n "$SECRET_ARN" ]] && break
    sleep 10
  done
  [[ "$SECRET_ARN" != "None" && -n "$SECRET_ARN" ]] || {
    echo "managed admin secret never appeared" >&2; exit 1; }
fi

echo "==> seeding the database as the namespace admin (superuser)"
DBT_DEV_PASSWORD=$(python3 -c "import secrets, string; print('Aa1' + ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(29)))")
ADMIN_SECRET=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" \
  --query SecretString --output text)
DB_USER=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['username'])" "$ADMIN_SECRET")
DB_PASSWORD=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['password'])" "$ADMIN_SECRET")

# An idle Serverless workgroup drops the first connection while it resumes
# from cold ("server closed the connection unexpectedly"), so wait for it to
# accept a trivial query before seeding; raw psql, unlike redshift_connector,
# does not retry the resume itself.
echo "    waiting for the workgroup to accept connections (resume from cold)..."
for _ in $(seq 1 18); do
  if PGPASSWORD="$DB_PASSWORD" psql \
      "host=$HOST port=5439 dbname=$DATABASE user=$DB_USER sslmode=require" \
      -tAc "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 10
done
PGPASSWORD="$DB_PASSWORD" psql \
  "host=$HOST port=5439 dbname=$DATABASE user=$DB_USER sslmode=require" \
  -tAc "SELECT 1" >/dev/null || {
    echo "workgroup did not accept connections; check it is AVAILABLE and the" >&2
    echo "security group allows 5439 from this host" >&2
    exit 1
  }

# Redshift has no CREATE USER/ROLE IF NOT EXISTS, and users and roles survive
# the schema rebuild below, so creation is tolerated on its own: a re-run
# reaches the strict block either way, which refreshes the grants and rotates
# the dbt password (the header's idempotency contract). Only an "already
# exists" failure is forgiven; anything else (a connection timeout, a
# permission error) still aborts, rather than being mislabeled as existing.
run_tolerating_exists() {
  local statement="$1" label="$2" err
  if err=$(PGPASSWORD="$DB_PASSWORD" psql \
      "host=$HOST port=5439 dbname=$DATABASE user=$DB_USER sslmode=require" \
      -v ON_ERROR_STOP=1 -c "$statement" 2>&1); then
    return 0
  fi
  if [[ "$err" == *"already exists"* ]]; then
    echo "    $label already exists; refreshed below"
    return 0
  fi
  echo "$err" >&2
  return 1
}
run_tolerating_exists "CREATE USER dex_ro PASSWORD DISABLE" dex_ro
run_tolerating_exists "CREATE USER dbt_dev PASSWORD '${DBT_DEV_PASSWORD}'" dbt_dev
# The CI job authenticates by assuming the OIDC role, so GetCredentials mints
# the database user IAMR:<role-name>, not dex_ro (Serverless derives the user
# from the IAM identity and cannot mint a PASSWORD DISABLE user by name). Give
# that identity the same read access through a database role, pre-creating the
# user so the grant lands before its first login.
CI_DB_USER="IAMR:${ROLE_NAME}"
run_tolerating_exists "CREATE ROLE dex_ci_reader" "role dex_ci_reader"
run_tolerating_exists "CREATE USER \"${CI_DB_USER}\" PASSWORD DISABLE" "CI role db user"

PGPASSWORD="$DB_PASSWORD" psql "host=$HOST port=5439 dbname=$DATABASE user=$DB_USER sslmode=require" \
  -v ON_ERROR_STOP=1 <<SQL
-- The same shape as scripts/postgres_seed.sql, in Redshift vocabulary (no
-- enums or arrays; SUPER carries the semi-structured column). Idempotent:
-- schemas are rebuilt from scratch on every run.
DROP SCHEMA IF EXISTS app CASCADE;
CREATE SCHEMA app;
CREATE SCHEMA IF NOT EXISTS dbt_dev;

CREATE TABLE app.customers (
    id          bigint IDENTITY(1, 1) PRIMARY KEY,
    email       varchar(256) NOT NULL,
    first_name  varchar(128) NOT NULL,
    last_name   varchar(128) NOT NULL,
    phone       varchar(64),
    address     varchar(256),
    city        varchar(128),
    created_at  timestamptz NOT NULL DEFAULT sysdate
);

CREATE TABLE app.products (
    id        bigint IDENTITY(1, 1) PRIMARY KEY,
    name      varchar(256) NOT NULL,
    category  varchar(32) NOT NULL,
    price     numeric(10, 2) NOT NULL,
    attrs     super
);

CREATE TABLE app.orders (
    id           bigint IDENTITY(1, 1) PRIMARY KEY,
    customer_id  bigint NOT NULL REFERENCES app.customers (id),
    status       varchar(16) NOT NULL DEFAULT 'pending',
    total        numeric(12, 2) NOT NULL DEFAULT 0,
    ordered_at   timestamptz NOT NULL DEFAULT sysdate
);

-- product_id deliberately carries no foreign key: relationship inference must
-- find it from names and profiles.
CREATE TABLE app.order_items (
    id          bigint IDENTITY(1, 1) PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES app.orders (id),
    product_id  bigint NOT NULL,
    quantity    integer NOT NULL,
    unit_price  numeric(10, 2) NOT NULL
);

CREATE VIEW app.v_order_totals AS
    SELECT o.id AS order_id, o.customer_id, o.status, SUM(i.quantity * i.unit_price) AS total
    FROM app.orders o JOIN app.order_items i ON i.order_id = o.id
    GROUP BY o.id, o.customer_id, o.status;

INSERT INTO app.customers (email, first_name, last_name, phone, address, city) VALUES
    ('ada@example.com',   'Ada',   'Lovelace',  '+1-555-0100', '1 Analytical Way', 'London'),
    ('grace@example.com', 'Grace', 'Hopper',    '+1-555-0101', '2 Compiler Ct',    'Arlington'),
    ('edgar@example.com', 'Edgar', 'Codd',      '+1-555-0102', '3 Relational Rd',  'Fortuneswell'),
    ('barbara@example.com', 'Barbara', 'Liskov', NULL,          NULL,               'Los Angeles'),
    ('margaret@example.com', 'Margaret', 'Hamilton', '+1-555-0104', '5 Apollo Ave', 'Paoli');

INSERT INTO app.products (name, category, price, attrs) VALUES
    ('Laptop',   'electronics', 1200.00, JSON_PARSE('{"warranty_months": 24}')),
    ('Coffee',   'grocery',        9.50, JSON_PARSE('{"organic": true}')),
    ('T-shirt',  'apparel',       19.90, JSON_PARSE('{"sizes": ["S", "M", "L"]}')),
    ('Desk',     'home',         310.00, JSON_PARSE('{"material": "oak"}'));

INSERT INTO app.orders (customer_id, status, total) VALUES
    (1, 'paid', 1209.50), (1, 'shipped', 19.90), (2, 'paid', 310.00),
    (3, 'pending', 9.50), (4, 'cancelled', 1200.00), (5, 'paid', 39.80);

INSERT INTO app.order_items (order_id, product_id, quantity, unit_price) VALUES
    (1, 1, 1, 1200.00), (1, 2, 1, 9.50), (2, 3, 1, 19.90),
    (3, 4, 1, 310.00), (4, 2, 1, 9.50), (5, 1, 1, 1200.00),
    (6, 3, 2, 19.90);

-- ~65k rows: enough that an unbudgeted full profile is visibly refused and
-- the statement_timeout has something to kill. Built by cross join because
-- generate_series is leader-node-only on Redshift.
CREATE TABLE app.events AS
    SELECT
        ROW_NUMBER() OVER () AS id,
        (ROW_NUMBER() OVER ()) % 5 + 1 AS customer_id,
        CASE (ROW_NUMBER() OVER ()) % 3 WHEN 0 THEN 'view' WHEN 1 THEN 'cart' ELSE 'purchase' END AS kind,
        sysdate AS occurred_at
    FROM app.order_items a, app.order_items b, app.order_items c,
         app.order_items d, app.order_items e;

-- The read-only user dex connects as (created above). svv_table_info is not
-- readable by default for non-admin users, and without it table sizes are
-- unknown and cost estimates degrade to minimums.
GRANT USAGE ON SCHEMA app TO dex_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO dex_ro;
GRANT SELECT ON svv_table_info TO dex_ro;

-- The CI identity (IAMR:${ROLE_NAME}, created above) gets the same read access
-- through the dex_ci_reader role. The table grants are re-applied here because
-- DROP SCHEMA app CASCADE above dropped them with the old tables; the role
-- grant to the user is cluster-level and survives, re-granted idempotently.
GRANT USAGE ON SCHEMA app TO ROLE dex_ci_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO ROLE dex_ci_reader;
GRANT SELECT ON svv_table_info TO ROLE dex_ci_reader;
GRANT ROLE dex_ci_reader TO "IAMR:${ROLE_NAME}";

-- The dbt user (created above): reads app, writes only its dev schema, and
-- carries the durable per-statement cap dex cannot inject through
-- dbt-redshift. The ALTER is the rotation: it lands together with the GitHub
-- secret update below, so the two halves cannot drift apart.
ALTER USER dbt_dev PASSWORD '${DBT_DEV_PASSWORD}';
GRANT USAGE ON SCHEMA app TO dbt_dev;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO dbt_dev;
GRANT USAGE, CREATE ON SCHEMA dbt_dev TO dbt_dev;
ALTER USER dbt_dev SET statement_timeout TO 300000;
SQL

if [[ "$SKIP_GITHUB" != "true" ]]; then
  echo "==> GitHub environment, variables, and the dbt password secret"
  gh api -X PUT "repos/${REPO}/environments/${ENVIRONMENT}" \
    -F "deployment_branch_policy[protected_branches]=false" \
    -F "deployment_branch_policy[custom_branch_policies]=true" >/dev/null
  gh api -X POST "repos/${REPO}/environments/${ENVIRONMENT}/deployment-branch-policies" \
    -f name=main >/dev/null 2>&1 || true
  for pair in \
    "DEX_TEST_REDSHIFT_WORKGROUP=$WORKGROUP" \
    "DEX_TEST_REDSHIFT_DATABASE=$DATABASE" \
    "DEX_TEST_REDSHIFT_HOST=$HOST" \
    "DEX_TEST_REDSHIFT_ROLE_ARN=$ROLE_ARN" \
    "DEX_TEST_REDSHIFT_REGION=$REGION"; do
    gh variable set "${pair%%=*}" --env "$ENVIRONMENT" --repo "$REPO" --body "${pair#*=}"
  done
  gh secret set DEX_TEST_REDSHIFT_DEV_PASSWORD --env "$ENVIRONMENT" --repo "$REPO" \
    --body "$DBT_DEV_PASSWORD"
fi

echo "==> done"
echo "    workgroup: $WORKGROUP ($HOST)"
echo "    role:      $ROLE_ARN"
echo "    local runs: DEX_TEST_REDSHIFT_WORKGROUP=$WORKGROUP DEX_TEST_REDSHIFT_DATABASE=$DATABASE uv run pytest tests/integration -q -m redshift"
