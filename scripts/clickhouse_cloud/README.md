# ClickHouse Cloud integration operations

These helpers manage the dedicated, repository-owned Cloud test service. They
never discover an arbitrary service, and teardown never stops or deletes one.
The dedicated organization, service, AWS region, Scale-tier prices, repository,
environment, `dex_ci_*` resource names, and CI policy are stable non-secret
constants in the scripts. Flags exist for deliberate migration/testing; normal
setup does not scatter those values through an operator's shell.

Bootstrap administration uses only `CLICKHOUSE_CLOUD_API_KEY`,
`CLICKHOUSE_CLOUD_API_SECRET`, `CLICKHOUSE_USER`, and `CLICKHOUSE_PASSWORD` from
the process environment. Do not pass them on the command line or commit them.

First setup (the explicit adoption flag is needed only while the ownership tag
is absent):

```bash
scripts/setup_clickhouse_cloud_ci.sh \
  --adopt-service
```

Rerun the same command without `--adopt-service` to rotate both database
passwords and the scoped API key. A new API key is verified and installed in
GitHub before older `dex-ci-usage-*` keys are removed. Ensure no Cloud
integration workflow is active while rotating.

The scoped control-plane key accepts IPv4 from any runner because GitHub-hosted
runners have no stable egress address. Its custom role can only read the exact
service and organization billing usage; setup verifies that a service update is
denied. This does not modify or broaden the service's SQL endpoint allowlist.

Pass `--dogfood` on a setup/rotation run to execute the narrow live suite with
the newly generated credentials while they exist only in that process. This is
the local validation path; GitHub secrets remain write-only and are never read
back onto the machine.

`preflight.sh` reads the exact service and UTC-day usage through the Cloud API,
performs no SQL connection, and refuses at the configured CHC threshold.
`run_integration.sh` requires the same protected environment values as CI, runs
that preflight, and then invokes only the `clickhouse_cloud` pytest marker.
Neither helper echoes credentials.

Bounded teardown requires an explicit confirmation and the ownership tag:

```bash
scripts/clickhouse_cloud/teardown.sh \
  --confirm
```

Organization, service, repository, and environment default to the same
committed constants as setup. Their flags are migration/testing overrides, not
ordinary operator inputs.

It removes only the two `dex_ci_*` databases, the two users and settings
profiles, the custom usage-reader role, `dex-ci-usage-*` API keys, the named
GitHub environment, and the two attribution tags. It does not touch other
databases, users, keys, roles, billing configuration, or the service lifecycle.
