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
| Commit | `b5da5d66f0da4a0cd3388d52201dbf5523221a77` |
| Path | `core-spec/ossie-schema.json` |
| SHA-256 | `27aab111647b1e8d2229a2413e4682e459f43edc62e0eaadd497317754089e42` |
| Declared spec version | `0.2.0.dev0` |
| License | Apache License 2.0 |

The pin is on content and not on the version string, because the schema declares
`version` as a constant that upstream does not move when the schema changes. The
same hash appears in the loader, in the bundled
`exmergo_dex_core/ossie/schema/PROVENANCE.md`, in the corpus manifest, and in the
table above; an offline test asserts all four agree.

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
| several configured documents | compose into one layer | `compose_two_documents` |
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
| a structural key the schema does not define | error | pinned schema | `unknown_structural_key` |
| a dialect token the pinned schema's enum does not define | error | pinned schema | `post_pin_thoughtspot_is_refused` |
| two datasets, fields, or metrics sharing a name in one model | error | upstream integrity | `duplicate_names_within_a_document` |
| a relationship endpoint the model does not declare | error | upstream integrity | `relationship_endpoint_missing` |
| a semantic-model name declared in two configured files | error | dex | `duplicate_model_across_files` |
| a relationship whose two column arrays differ in length | error | dex | `relationship_arity_unequal` |
| a relationship target whose declared key does not cover the join | warning | upstream integrity | `target_key_coverage_warns` |
| expressions in a non-SQL dialect were not parsed | note | dex | `non_sql_dialects_are_metadata` |

Two of those are dex's own rules rather than upstream's, marked accordingly in
the corpus manifest and worth carrying upstream: cross-file semantic-model
uniqueness, and equal arity between a relationship's two column arrays. The
schema constrains both arrays but not their lengths against each other, and a
relationship whose sides differ in arity has no complete tuple to join on.

**What validation proves, and what it does not.** There is no external validator
here the way `dbt parse` is for dbt: neither `apache-ossie` nor
`apache-ossie-dbt` is published, and there is no Ossie runtime to load a document
into. A document that passes is schema-valid and internally consistent. That is
not a claim that any consumer can execute it, and no dex surface says otherwise.

## Which expression dex reads

An Ossie field or metric may declare one expression per dialect, and the enum
mixes SQL dialects with expression languages that are not SQL. Selection is
deterministic:

1. the active connector's own Ossie dialect, where the document declares one;
2. otherwise `ANSI_SQL`;
3. otherwise the first supported SQL dialect in declaration order.

Four of dex's seven connectors (DuckDB, Postgres, Redshift, ClickHouse) have no
token in Ossie's enum. They fall to `ANSI_SQL` rather than to something close,
because a wrong dialect is a worse answer than the portable one. Every declared
expression is preserved whichever one dex reads. Cases:
`dialect_selection`, `dialect_selection_falls_back_to_portable`.

`MDX`, `TABLEAU` and `MAQL` are preserved verbatim, never parsed, never a source
of a physical column, and never reported as SQL-validated. A bare token in MAQL
that looks like a column identifier means something else entirely, so reading one
would be worse than reading none. Case: `non_sql_dialects_are_metadata`.

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
against a column that is not behind it.

## Keys, relationships, snapshots, and authoring

| capability | supported | cases |
|---|---|---|
| a primary key, and further independent `unique_keys` beside it | yes, kept as separate claims | `keys_and_relationships` |
| a composite grain of three or more columns, in declared order | yes, and no member leaks into the single-key list | `keys_and_relationships` |
| a relationship whose two sides are spelled differently | yes, both sides retained | `keys_and_relationships` |
| a composite relationship as one ordered tuple | yes, verified as a complete tuple and never one pair at a time | `adversarial_composite` |
| `maintain snapshot` baseline over the layer | yes | `drift_baseline` |
| free definition, key, and relationship drift against that baseline | yes | `drift_baseline`, `drift_changed` |
| `semantic ossie define\|update\|plan`, applied with `transform apply` | yes, confined to the exact paths in `semantic.ossie.files` | see [Authoring native documents](semantic-layer.md#authoring-native-documents) |

`adversarial_composite` is the case worth reading. Its child tuples are
`(1, 10, 100)` and `(2, 20, 200)`, its parent tuples `(1, 20, 200)` and
`(2, 10, 100)`. Every column overlaps independently, so a probe that measures one
column at a time reports a healthy join three times over; no complete tuple
matches, so the join finds nothing. The two sides are named differently so that
copying one side's columns onto the other cannot pass.

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
| upstream [added the `THOUGHTSPOT` dialect](https://github.com/apache/ossie/commit/211ce662c2e122e3410ad6c737248e9516a06d3b) after the pinned commit | a document declaring it is refused, since the pinned enum does not define it. Case: `post_pin_thoughtspot_is_refused` |
| dex requires a semantic-model name to be unique across configured files | upstream validates one document at a time and has no cross-file rule |
| dex requires a relationship's two column arrays to have equal length | upstream's schema constrains each array but not their lengths against each other |

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
