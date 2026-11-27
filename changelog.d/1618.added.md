**`dex init` proposes the schema the Elementary package writes in production
for the source allowlist.** Where the package is present or offered,
`data.elementary.allowlist` says whether the committed allowlist reaches that
schema, and where it does not, init lists the entry as a non-blocking missing
input with the `dex config set ... --comment` that adds it. Init never writes
it: `config set` shows the diff and writes on `--confirm`. A schema the
connection cannot open yet (the package has not run in production) is reported
`not_created` and not proposed, since an allowlist entry naming a schema that
does not exist makes every command refuse. The schema is dbt's
naming of the package's `+schema` for production, read from `<connector>.prod`
or the profile output `dbt_prod_target` names, never from the dev namespace;
where a project's own `generate_schema_name` or a Jinja `+schema` keeps it
unknown, the missing input says what to commit. A missing input marked
`blocking: false` no longer makes init an error. `dex config set` gains
`--comment`, which writes the reason beside the value, and a list it extends
keeps every line it had, comments included.
