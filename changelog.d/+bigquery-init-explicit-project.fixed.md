**`transform init --connector bigquery --project <id>` now honors the explicit
project.** Init rendered its dev profile from configuration and credential
discovery only, so a fresh repository with no `bigquery.project` and no gcloud
default refused with "no GCP project to wire the dev target to" even though the
command named one. The explicit project now wins over a configured or discovered
one, and the generated profile and the saved `.dex/config.yml` both record it,
so the initialized project needs no prior config edit. Without the flag, the
existing configured and discovered resolution, and its actionable refusal when
nothing resolves, are unchanged.
