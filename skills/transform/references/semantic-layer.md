# Authoring the semantic layer

What you must and must not do is in `SKILL.md`; this file is the detail of each
command and payload.

## dbt semantic models: `semantic define|update|plan`

`define` and `update` author and evolve semantic models (entities, dimensions,
measures, metrics) as plans. `define` refuses names that already exist; `update`
refuses names that do not. `plan` accepts a mix and reports the split as
`defined`, `updated`, `unchanged`, and `removed`. A semantic plan is applied like
any other, with `transform apply [plan-id]`.

### `--definitions-file`

A real project keeps its metrics in one shared file, so a whole-file
`--edits-file` payload means restating every definition you are not touching:
the diff and the `updated` list then describe the whole file, and every restated
line is a chance to corrupt a definition. Send only what changes:

```json
{"definitions": [{"kind": "metric", "content": "name: ...\n..."}]}
```

`kind` is `semantic_model` or `metric`, and `content` is that definition's YAML
body with no leading `- `. The name comes from the content, and `path` can be
omitted for anything the project already declares (the engine rewrites it where
it lives). Everything else in the file, comments included, is preserved byte for
byte. Use `--edits-file` to create a file, move a definition between files, or
empty one, and when the engine refuses a layout it will not splice into.

### Removing a definition

```json
{"definitions": [{"kind": "metric", "name": "doubled", "op": "delete"}]}
```

The name is declared (there is no content to read it from) and there is no
`content`. Nothing is removed for going unmentioned, so a removal and an edit can
share one payload. Use `update` or `plan`, not `define`. The envelope reports it
under `removed`.

If a metric still reads what you remove (its input is that metric, or a measure
of the semantic model you remove), the plan is refused naming the reader; add the
reader's own delete or update to the same payload, in any order. A removal that
would leave a file with no semantic model or metric is refused too: deleting or
emptying a file is a whole-file edit, done with `transform plan --edits-file` and
`"op": "delete"`.

### Validation

Layered so a plan that validates will build:

1. MetricFlow's schemas check the shape.
2. The engine resolves every metric input. Ratio and derived metrics reference
   **metrics**, not measures; a measure becomes a metric only through
   `create_metric: true`, and the error names that fix.
3. The emitted YAML is parsed by **dbt's own parser** against a throwaway copy of
   the project. A plan that fails parse is refused, not stored. Without dbt the
   parse degrades to a warning; `--no-parse` skips it explicitly.

dbt cannot parse semantic models without a MetricFlow **time spine**. The engine
warns when one is missing and defers the parse gate until one exists.

## Native Apache Ossie: `semantic ossie define|update|plan`

Pass `--edits-file <path|->` with whole documents whose paths are listed in
`semantic.ossie.files`. The command implies the `semantic_document` kind,
validates the complete prospective layer (your edits overlaid on the other
configured documents), and writes the accepted bytes exactly when the plan is
applied. This is a semantic-layer write surface; it does not make Ossie the
transformation project.

- Namespace guards match the dbt ones: `define` refuses a semantic-model name the
  layer already has, `update` one it does not, and `plan` accepts both. Neither
  removes a model. A configured file may be absent before `define`, so a new
  document is planned once its path is committed to config.
- There is no external parser to gate on. dex checks the document's structure
  against the Ossie schema it pins (needs `[ossie]`), its internal consistency,
  and each SQL expression's syntax through the dialect engine (needs `[sql]`,
  which every connector extra carries). Without `[sql]` the third layer degrades
  to a named skipped-validation note, never a silent pass.
- References are checked against the exploration cache, opening no connection. A
  source relation the cached inventory positively lacks, or a column absent from
  a relation the cache profiled, refuses and stores no plan. Anything the cache
  cannot speak to is a named note instead: an unprofiled relation, a computed or
  non-SQL expression, a quoted identifier, a query-backed source.
- An unknown key is refused as incompatible with the pinned schema: it may be a
  typo or written for a newer Ossie draft. The diagnostic names the pinned spec
  version.
- On apply, dex does not re-serialize the document, so comments, key order,
  quoting, and whitespace survive, and a configured document the payload did not
  mention is untouched. A target file that changed after planning refuses the
  whole apply unless the overwrite is confirmed deliberately.

A worked end-to-end example on a local warehouse, for a human reader:
<https://github.com/exmergo/dex/blob/main/references/ossie-walkthrough.md>.
