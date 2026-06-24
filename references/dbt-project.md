# dbt as a first-class input (stub)

Detailed in Phase 2. dex loads an existing dbt project (`dbt_project.yml`, the
compiled `manifest.json`, models, `schema.yml`, sources, and existing semantic
models) as first-class context, and resolves the dev target from `profiles.yml`
for any gated build. Human edits to dbt are authoritative on read. Absent a dbt
project, dex degrades gracefully to raw introspection. dex never builds to a
non-dev target. See v8 system design sections 8 and 11.
