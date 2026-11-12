**`semantic snowflake define|update|plan` authors Snowflake Semantic View files,
and a declared mart derives a logical table.** Whole files listed in
`semantic.snowflake.files` are validated together with dex's port of Snowflake's
rules and stored as a reviewable plan that `transform apply` writes byte for
byte, keeping each file's mode. A path off the list, a `.sql` file, another
suffix and a delete are refused by name, the first with the `dex config set`
that adopts it. Every error anywhere in the set refuses the plan. In a file the
plan writes, an unknown key, `access_modifier` on a dimension, and a read
qualified with its own table that names no member also refuse it. Reading
reports that last one as the new `unresolved_member` warning.

Each logical table and derived metric is reported `defined`, `updated` or
`unchanged`, compared as YAML 1.2, so reformatting is not a change. Nothing is
deployed: dex runs no `CREATE SEMANTIC VIEW`, no YAML procedure and no grant, and
opens no Snowflake connection.

On `transform plan`, a declared mart becomes a logical table:
- the grain as its primary key;
- dimensions and time dimensions from its roles;
- a fact per measure, and a metric per declared aggregation, held non-additive
  over time where the measure is semi-additive;
- a relationship to each parent table keyed on its foreign key.

The table is spliced into the file that holds its parents, or proposed as a view
of its own. A `declarations` payload on `semantic snowflake plan` annotates an
existing table, rewriting only the lines it changes, comments inside the table
included. The population is the last sentence of the table's description, which
the catalog withholds. Units, NULL rules and assumptions are reported as not
carried.
