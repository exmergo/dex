# Apache Ossie compatibility

What dex accepts from a native [Apache Ossie](https://github.com/apache/ossie)
(incubating) document, what it checks, what it makes of what it read, and what it
does not claim. Read it beside
[Native Apache Ossie](semantic-layer.md#native-apache-ossie-vendor-ossie), which
describes how to configure and use the layer, and
[A native Ossie layer end to end](ossie-walkthrough.md), which runs one document
through every command on a local warehouse. This page is the compatibility
statement behind both.

Every row here is backed by a case in the reviewed corpus at
`packages/dex-core/tests/ossie/fixtures/`, named by its case id. The corpus runs
offline against the bundled schema, and an automated test refuses a claim on this
page whose case does not exist.

## The pinned schema

dex vendors the upstream schema verbatim and pins it by content hash. Nothing at
runtime needs a checkout of Apache Ossie.

| | |
|---|---|
| Upstream | <https://github.com/apache/ossie> |
| Commit | `7a8de20f850dad3934e307a1ac7425b09e9ca89f` |
| Path | `core-spec/ossie-schema.json` |
| SHA-256 | `2cf11e3c555b767b2612a4e479545e55485d81af62c3bf465a03f33a861aae4c` |
| Declared spec version | `0.2.0.dev0` |
| License | Apache License 2.0 |

The pin is on content and not on the version string, because the schema declares
`version` as a constant that upstream does not move when the schema changes. The
same hash appears in the loader, in the bundled
`exmergo_dex_core/ossie/schema/PROVENANCE.md`, in the corpus manifest, and in the
table above; an offline test asserts all four agree.

`maintain snapshot` records the same hash as the layer's `format_version`, so a
baseline taken under one pin and compared under another reports
`semantic_format_changed` rather than a layer of changed definitions. A baseline
written before dex recorded the hash carries none, and that is "unrecorded", not
a change.

Under this pin a document is **one semantic model, flat at its root**:
`version`, `name`, `description`, `ai_context`, `datasets`, `relationships`,
`metrics` and `custom_extensions`. The earlier `semantic_model` list of models is
gone upstream, and dex refuses a document still written that way with a rule of
its own (`wrapped_document`) whose message names the migration: move the model's
properties to the root, remove `semantic_model`, drop any root-level `dialects`
and `vendors`, and split a file holding several models into one document per
model, each listed in `semantic.ossie.files`. dex does not rewrite the file for
you. Case: `wrapped_document_refused`.

The upstream documents in `examples/` and `compliance/portability/` at the pinned
commit are vendored into the corpus under `fixtures/upstream/`, and each one is
asserted to load with no error and no warning.

## Required extras

| capability | extra | absent |
|---|---|---|
| read, validate structure and integrity, catalog, snapshot, author | `[ossie]` (`jsonschema`) | refused by name; there is no weaker validator to fall back to |
| check that a SQL expression parses | `[sql]` (`sqlglot`), which every connector extra already brings | a named skipped-validation note on the document, never a silent pass |

`[ossie]` deliberately does not pull `[sql]`. Structure and integrity decide
whether a document is readable at all and are pure plus one validator; expression
syntax is a third layer that rides on a dependency a warehouse install already
has.

## What is accepted

| behavior | verdict | cases |
|---|---|---|
| `.ossie.yaml`, `.ossie.yml`, `.ossie.json` | equivalent semantics, whichever serialization | `minimal_yaml`, `minimal_yml`, `minimal_json` |
| several configured documents, one model each | compose into one layer | `compose_two_documents` |
| a document in the earlier wrapped `semantic_model: [...]` shape | refused by name, with the migration | `wrapped_document_refused` |
| any dialect token (`OSSIE_SQL_2026`, `DAX`, `THOUGHTSPOT`, or one no registry names) | accepted; only the SQL dialects are read | `opaque_dialects_are_metadata` |
| an expression stating only the top-level `sql` | read as `OSSIE_SQL_2026` | `ossie_sql_2026_top_level_sql_links` |
| dataset-scoped metrics (`datasets[].metrics`) | read into the catalog as `dataset.metric` | `dataset_metrics_read` |
| a YAML `---` stream inside one configured file | refused: one configured file is one document | `yaml_stream_in_one_file` |
| a document naming a semantic model another file already names | refused | `duplicate_model_across_files` |

## What is checked, and how hard

Three severities, and they are not three strengths of one verdict. An **error**
means dex could not read the document, which is then absent from the layer. A
**warning** means dex read it and has something to say about what it found. A
**note** means dex read it and is saying what it did not check.

| check | severity | origin | cases |
|---|---|---|---|
| bytes are not parseable | error | dex | `unparseable` |
| a configured file holding a YAML `---` stream | error | dex | `yaml_stream_in_one_file` |
| the document's own `version` is not the pinned constant | error | pinned schema | `wrong_version` |
| a document in the earlier wrapped shape | error | dex | `wrapped_document_refused` |
| a structural key the schema does not define | error | pinned schema | `unknown_structural_key` |
| two datasets, fields, or metrics sharing a name in one model | error | upstream integrity | `duplicate_names_within_a_document` |
| a relationship endpoint the model does not declare | error | upstream integrity | `relationship_endpoint_missing` |
| a key, or a relationship's `from_columns`/`to_columns`, naming no declared field | error | upstream integrity | `key_names_source_column_refused` |
| a semantic-model name declared in two configured files | error | dex | `duplicate_model_across_files` |
| a relationship whose two column arrays differ in length | error | upstream integrity | `relationship_arity_unequal` |
| a relationship target whose declared key does not cover the join | warning | upstream integrity | `target_key_coverage_warns` |
| expressions in a dialect dex does not read were not parsed | note | dex | `non_sql_dialects_are_metadata`, `opaque_dialects_are_metadata` |

Two of those are dex's own rules rather than upstream's, marked accordingly in
the corpus manifest: cross-file semantic-model uniqueness, since dex composes a
configured set of documents where upstream validates one at a time, and the
dedicated refusal of the wrapped shape, which upstream reports as a schema
error.

Upstream's validator also checks a few things dex does not yet: that a metric
expression names fields as `dataset.field` (a warning upstream), that a field
expression does not shadow another field, and the dataset-scoped metric rules
(an unqualified field of its own dataset only, and one namespace shared by a
dataset's fields and metrics). The last group is tracked as #957.

**What validation proves, and what it does not.** There is no external validator
here the way `dbt parse` is for dbt: neither `apache-ossie` nor
`apache-ossie-dbt` is published, and there is no Ossie runtime to load a document
into. A document that passes is schema-valid and internally consistent. That is
not a claim that any consumer can execute it, and no dex surface says otherwise.

## Which expression dex reads

An Ossie expression states a portable `sql` form, one expression per dialect
under `dialects`, or both. `Dialect` is an open string: any non-empty token is
valid, and `OSSIE_SQL_2026` is the one dialect the specification defines. dex
reads only SQL: `OSSIE_SQL_2026` and `ANSI_SQL` (both parsed with sqlglot's
default dialect, as upstream's validator parses them), `SNOWFLAKE`, `DATABRICKS`
and `BIGQUERY`. Selection is deterministic:

1. the active connector's own Ossie dialect, where the document declares one;
2. otherwise `OSSIE_SQL_2026`, from the top-level `sql` first, since upstream
   makes it authoritative over an `OSSIE_SQL_2026` entry in `dialects`;
3. otherwise `ANSI_SQL`;
4. otherwise the first other SQL dialect in declaration order.

Five of dex's eight connectors (DuckDB, Postgres, Redshift, ClickHouse, Trino)
have no Ossie dialect of their own. They fall to the portable forms rather than
to something close, because a wrong dialect is a worse answer than the portable
one. Every declared expression is preserved whichever one dex reads. Cases:
`dialect_selection`, `dialect_selection_falls_back_to_portable`,
`ossie_sql_2026_top_level_sql_links`.

Every other token, whether upstream's registry names it (`MDX`, `TABLEAU`,
`MAQL`, `SIGMA`, `THOUGHTSPOT`, `DAX`, `HOLISTICS_AQL`) or not, is preserved
verbatim, never parsed, never a source of a physical column, never reported as
SQL-validated, and named in the document's `non_sql_dialect` note. A bare token
in MAQL that looks like a column identifier means something else entirely, so
reading one would be worse than reading none. Cases:
`non_sql_dialects_are_metadata`, `opaque_dialects_are_metadata`.

## The physical link, and therefore the PII gate

A field resolves to a warehouse column only when **both** conditions hold: the
dataset's source is a relation this connector can address, and the field's
selected expression is a bare identifier. Everything else carries no column.

| shape | links | why |
|---|---|---|
| a bare identifier on a `database.schema.table` source | yes | the only shape where the column is stated rather than inferred |
| a computed expression | no | there is no single column behind it |
| a quoted identifier | no | quoting is the author asserting a spelling dex would have to fold |
| a qualified `dataset.column` expression | no | the qualifier names a dataset, not the relation dex would address |
| a field declaring only non-SQL dialects | no | dex does not read those languages |
| a query-valued source | no | Ossie documents a source as a relation **or** a query with no portable discriminator, and a query read as a relation reaches the gate as a relation that does not exist |
| a source that is not addressable on this connector | no | opaque rather than guessed at |

Case: `physical_linkage_is_direct_only`, which asserts the resulting link table by
equality rather than containment. A link too few costs a screening; a link too
many screens the wrong column and reports the verdict as evidence-backed.

PII linkage follows exactly this table. A dimension dex cannot resolve to a
column is screened by the name heuristic and says so, rather than being screened
against a column that is not behind it. The policy that linkage feeds is in
[`pii-policy.md`](pii-policy.md).

## Name resolution: keys and relationships name fields

Upstream's [Name Resolution](https://github.com/apache/ossie/blob/main/core-spec/spec.md#name-resolution)
gives every name in a document one meaning, and dex follows it:

| where the name appears | what it refers to | how dex reaches the warehouse |
|---|---|---|
| a field's `expression` | a column of the dataset's `source` | the physical link above |
| `primary_key`, `unique_keys` | a field of the same dataset | through that field's physical link |
| a relationship's `from_columns` / `to_columns` | a field of the `from` / `to` dataset | through each field's physical link |
| a dataset-scoped metric's expression | a field of the same dataset, unqualified | lineage to that dataset |
| a model-scoped metric's expression | a field, qualified `dataset.field` | lineage to each dataset named |

So a field `customer_id` defined as `o_custkey` is a key or a join on the column
`o_custkey`, and that is the column `explore relationships --verify`,
`explore profile --use-project`, `maintain grain` and `maintain semantic` read.
Field names are compared case-insensitively, as upstream compares them. Case:
`renamed_field_keys`.

A key or relationship naming something that is not a declared field of its
dataset is an integrity error: "metrics and relationships do not reach through a
dataset to its source columns". Case: `key_names_source_column_refused`.

A key or relationship any of whose fields links no physical column (a computed
field, a quoted identifier, a non-SQL-only field) is **opaque as a whole**: it
stays declared in the document, is named in the declarations channel's notes,
and is never verified or probed. A composite is never checked on part of its
tuple. Case: `computed_field_key_is_opaque`.

The snapshot keeps both names. A key's resolved columns are what drift compares
against the warehouse, and a relationship's fingerprint covers both the fields it
names and the columns they resolve to, so renaming either a field or the column
behind it is drift.

## Keys, relationships, snapshots, and authoring

| capability | supported | cases |
|---|---|---|
| a primary key, and further independent `unique_keys` beside it | yes, kept as separate claims | `keys_and_relationships` |
| a composite grain of three or more columns, in declared order | yes, and no member leaks into the single-key list | `keys_and_relationships` |
| a relationship whose two sides are spelled differently | yes, both sides retained | `keys_and_relationships` |
| keys and relationships on fields named differently from their columns | yes, resolved to the columns | `renamed_field_keys`, `adversarial_composite` |
| a composite relationship as one ordered tuple | yes, verified as a complete tuple and never one pair at a time | `adversarial_composite` |
| `maintain snapshot` baseline over the layer | yes | `drift_baseline` |
| free definition, key, and relationship drift against that baseline | yes | `drift_baseline`, `drift_changed` |
| `semantic ossie define\|update\|plan`, applied with `transform apply` | yes, confined to the exact paths in `semantic.ossie.files` | see [Authoring native documents](semantic-layer.md#authoring-native-documents) |
| model declarations derived into the layer, or annotating a dataset | yes, see [Declarations](#declarations) | see `tests/ossie/test_derivation.py` |

`adversarial_composite` is the case worth reading. Its child tuples are
`(1, 10, 100)` and `(2, 20, 200)`, its parent tuples `(1, 20, 200)` and
`(2, 10, 100)`. Every column overlaps independently, so a probe that measures one
column at a time reports a healthy join three times over; no complete tuple
matches, so the join finds nothing. The two sides are named differently so that
copying one side's columns onto the other cannot pass, and every field is named
differently from its column, so a relationship read as columns rather than fields
cannot pass either.

Dataset-scoped metrics are read into the catalog, named `dataset.metric`, with
lineage to their dataset when they name one of its fields. Their own integrity
rules, a per-metric fingerprint, and authoring are #957; until then a change to
one is drift on its dataset, whose fingerprint covers the whole dataset.

## Declarations

Model declarations (a dbt mart's grain, population, and what each column is)
reach a native Ossie layer through the same derivation hook the other semantic
layers implement. Two routes:

- **Derived.** In a repository that pairs a dbt project with an Ossie layer,
  `transform plan` (and `transform declare`) on a declared mart proposes an Ossie
  dataset for it under `data.semantic_proposal`.
- **Annotated.** `semantic ossie define|update|plan` accept a `declarations`
  payload beside (or instead of) `edits`, each naming a dataset the payload or
  the layer already defines, and render it into that dataset field by field.

| declared | Ossie |
|---|---|
| each declared column | a field of the same name, `expression: {sql: <column>}` (derived) |
| a column's type, where the SQL or the cache knows it | the field's `datatype` (`Integer`, `Decimal`, `Float`, `Boolean`, `Date`, `DateTime`, `String`) |
| `time` role | `dimension: {is_time: true}` |
| `grain` | `primary_key`, every grain field, composite included |
| `foreign_key` with `references` | a relationship `<mart>_to_<parent>` to the dataset in the same document whose field resolves to the referenced column |
| `measure` with `aggregation` | a dataset-scoped metric `<column>_<agg>` (`SUM`, `AVG`, `MIN`, `MAX`, `COUNT`, `COUNT(DISTINCT ...)`) |
| descriptions | the native `description` of the dataset and each field |
| `population`, `assumptions` | the dataset's `ai_context`: `instructions` carries the population sentence, and a structured `dex` key carries the declaration; the sentence also closes the description |
| `unit`, `null_rule`, `additivity`, `role`, `references` | the field's `ai_context.dex`, beside the native construct a role or a reference also renders |

Decisions behind the mapping:

- **A composite grain is a composite `primary_key`, not a `unique_keys` entry.**
  The grain is the dataset's row identity, upstream's `primary_key` takes a list,
  and the SLayer and Cube derivations put every grain column on the primary key
  too. `unique_keys` stays for further, independent keys.
- **The `primary_key` is what exploration reports as the grain, at either arity.**
  `explore profile|map --use-project` report it with `declaration.grain_status`,
  ahead of a measured key. A `unique_keys` entry is measured by `maintain grain`
  and is never the grain. A declared key that never held is
  `declared_grain_not_unique`, not `key_lost_uniqueness`.
- **`ai_context.dex` is read back, as untrusted text.** It reaches `declaration`
  on the dataset's relation, and a malformed part is left out and named in
  `notes`. Fields are reported under the physical column they link, and a field
  linking none is named instead.
- **`ai_context`, never `custom_extensions`.** `custom_extensions.data` is an
  opaque string, and a declaration encoded into a string inside YAML cannot be
  reviewed in a diff. `ai_context` takes an object with any further keys, so the
  declaration stays structured.
- **A population is metadata, never a filter.** Ossie has no filter on a dataset
  or a metric, and the dataset sits on the mart, whose SQL already enforces it.
- **Nothing is guessed.** `median` (no portable aggregate) and `semi_additive`
  (no exact Ossie equivalent) are named as limitations rather than rendered, and
  no metric beyond a declared measure is invented.
- **Grain columns name fields.** On the annotate route a grain column must be
  exposed by a declared field; one that is not is refused, naming the column, so
  the field is authored first.

**Where a derived dataset goes.** Relationships live inside one model, so a
derived dataset is *included* in the configured document that holds the datasets
its foreign keys reference, or, with no foreign keys and exactly one configured
document, in that document. It is spliced in as a new list item, so every other
byte of the document is kept, and stored as a companion plan beside the mart's
(`data.semantic_proposal.plan_id`); apply both. Where the parents span documents,
no document is configured, or the mart's relation already has a dataset, it is
*proposed* or *skipped* with the reason, and a proposal is a new document of its
own with the `dex config set semantic.ossie.files` that adopts it. A dataset name
another definition holds fails the derivation rather than being renamed.

Every derived or annotated document passes the same checks a hand-authored one
does (schema, integrity, expression syntax, the cache) before a plan is stored.

## What Ossie does not carry, declared rather than absent

These are properties of the format, not of how far the implementation got, and
they arrive in the catalog's `unavailable` block rather than as empty fields a
caller would read as facts about the layer.

| absent | consequence |
|---|---|
| measures | every declared field of a measure is unavailable; metrics are expressions rather than compositions over measures |
| entities | the layer's joins are explicit relationships, so there is no entity graph and no entity label |
| metric-to-dimension groupability | `explore semantic list --for-dimension` refuses by name rather than answering "no metric can be grouped that way", which would be a false statement about the layer |
| a portable query runtime | `explore semantic query` and `explore semantic values` refuse: Ossie defines no filter grammar, no join planning, and no execution semantics, so dex has nothing to render a governed statement from |
| a hosted deployment | `--api` is refused; a hosted Ossie would be some vendor's service speaking its own protocol, which is a different vendor rather than a second deployment of this one |

Ossie is a semantic layer and never a transformation project. `project.format:
ossie` is refused rather than translated, and the reader satisfies none of the
project tiers: it owns no model graph, no compilation, no targets, and no dbt
write surface. A repository may have dbt beside Ossie, Ossie alone, or dbt alone.

## What dex does not claim

- **No converter interoperability.** dex reads Ossie documents; it does not
  convert them to or from another semantic format, and says nothing about what
  another tool would make of one.
- **No execution assurance.** A document that validates here is schema-valid and
  internally consistent. Whether any engine can execute it is a separate
  question dex has no basis to answer.
- **Missing SQL validation is disclosed, not passed.** Without `[sql]` the
  expression layer does not run and the document says so. That note is the
  absence of a check, not a check that succeeded.
- **A warning is not a partial refusal.** A document that warns is read in full.

## Known deltas from upstream's current schema

Upstream moves; the pin does not, until somebody moves it deliberately.

| delta | effect under this pin |
|---|---|
| dex requires a semantic-model name to be unique across configured files | upstream validates one document at a time and has no cross-file rule |
| dex refuses the earlier wrapped shape with its own rule | upstream reports it as a schema error; dex names the migration instead |
| upstream checks metric expression references, field shadowing, and the dataset-scoped metric rules | dex reads dataset metrics but does not yet run these checks (#957), so a document upstream warns or errors on for one of them may read clean here |

Resolved by this pin, and no longer deltas: `THOUGHTSPOT` and every other dialect
token are accepted, since `Dialect` is an open string (the case once called
`post_pin_thoughtspot_is_refused` is now `opaque_dialects_are_metadata`), and
upstream's validator now requires a relationship's two arrays to have equal
length, as dex always did.

## Upgrading the pin

1. Copy the new `core-spec/ossie-schema.json` in verbatim.
2. Update the commit, hash, and declared version in `PROVENANCE.md`, in
   `SCHEMA_SHA256` in `exmergo_dex_core/ossie/loader.py`, in the corpus
   manifest's `schema:` block, and in the table on this page.
3. Run the corpus. **Read every case whose verdict moved**, decide for each one
   whether the new verdict is what upstream now intends, and record it in the
   changelog with the behavior it changes.
4. Update the tables on this page for anything that moved, and re-check the
   known-deltas list: a delta upstream has adopted stops being a delta.

Updating the hash alone is not an upgrade. The offline test checks that the four
places agree with each other; it cannot check that anybody read the diff, and it
should not be quoted as though it did.
