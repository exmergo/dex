**`explore --use-project` carries declared temporal history contracts.**
when a dbt model declares a `history` contract block under `config.meta.dex`,
`explore profile --use-project` and `explore map --use-project` now report it as
`history` on the model's `declaration`. under `--values-free` or when profiling
flags the status column as personal data, `status.target` is withheld as null
and recorded in `withheld_fields`.
