# File exploration: the contract

**Status.** The contract exists in the engine as the `exmergo_dex_core.files`
package. No command uses it yet, and no connector implements it yet, so every
connector reports file exploration unavailable with a named reason. This page
describes the contract that the `explore files` commands and the connector
implementations will be built on. It is the reference for anyone implementing or
reviewing them.

## What it is for

A file collection is an index of documents in object storage: a BigQuery object
table, a Snowflake directory table, a Databricks volume, or a table listing files.
Teams that turn such a collection into staging and intermediate models usually
already have a table of processing results, written by an earlier pipeline that
ran a document parser. File exploration answers questions about those two things
together:

- What files the collection holds.
- How many of them have a processing result.
- Whether those results describe the files as they are now.
- Where processing failed or produced little usable structure.
- What the evidence does not establish.

## What it never does

- **It never reads document content out of the warehouse.** Every content
  computation is a warehouse expression, and only counts come back. Nothing that
  leaves the warehouse carries any of the following:
  - a document body, excerpt, heading, or extracted value
  - an image
  - a file name, path, signed URL, or individual file identifier
  - a provider's error text
- **It never invokes document processing.** Dex reads results that already exist.
  New processing charges would need a provider-enforced hard spend cap that
  covers the exact operation, holds against concurrent and in-flight work, and
  that dex can verify. No supported processing path offers one today. Estimates,
  page limits, delayed quotas, and user confirmation do not substitute for one,
  so native processing is reported unavailable everywhere and has no setting
  that changes that.
- **It never creates or refreshes anything.** Collections and result tables are
  read as configured. Dex does not create an object table, a stage, a volume, or
  a connection, and it does not refresh external metadata to improve its answer.
- **It never falls back.** A connector without a file source says so. Dex does
  not switch to another connector, list the bucket itself, or download a file.

## Formats are reported metadata

Files are counted into fixed buckets: `pdf`, `jpeg`, `png`, `tiff`, `other`,
`unknown`. The bucket comes from the content type the storage layer recorded,
never from the file's bytes and never from its name. A PNG uploaded under a `.pdf`
name counts as a PNG.

- A well-formed content type outside the supported families is `other`.
- A missing or malformed content type is `unknown`, and so is one that states the
  format is unknown (`application/octet-stream`).
- Parameters and case are ignored: `Application/PDF; version=1.7` is `pdf`.

Two document families can be assessed: `pdf`, and `scanned_image`, which covers
`jpeg`, `png`, and `tiff`. The second name describes a document-image input. It
does not claim dex checked that an image depicts a scanned document.

## Absent is not zero

Every aggregate field is either an observed number or an explicit unavailability
with its reason. No field is ever `null`:

```json
{"zero_byte": 0}
{"zero_byte": {"reason": "not_reported"}}
```

The first says no file is empty. The second says the collection's metadata does
not carry sizes, so emptiness was not measured. The reasons are:

- `not_reported`
- `not_bound`
- `not_supported_by_format`
- `unrecognized_shape`
- `no_evidence`
- `not_assessed`

Every rate carries both sides, for example
`{"numerator": 5, "denominator": 7, "fraction": 0.714}`.

## Capabilities

A connector's file capabilities are read off the protocols it implements. It
never declares them with a flag, so it cannot claim one it does not have. The
report has three capabilities and two lists:

| Entry | Meaning |
|---|---|
| `collection_discovery` | lists the collections inside the source scope from catalog metadata, without scanning any of them |
| `metadata_aggregation` | counts one collection's files, bytes, formats, and update times in one budgeted statement |
| `result_assessment` | matches a sample of the collection against a materialized results table |
| `document_families` | the families the connector's metadata can bucket |
| `result_formats` | the result formats written for the connector's dialect |

Each capability is either `{"available": true}` or unavailable with a named
limitation and a fixed explanation:

- `no_file_source`
- `no_collection_discovery`
- `no_result_source`
- `no_result_format`

`native_processing` is always `{"available": false, "reason":
"no_verified_hard_spend_cap"}`.

## Result bindings

A result table is never guessed from a similar name. It is bound to its
collection explicitly, and a binding names identifiers only. It never carries an
expression, a template, or a callback, so it cannot be used to run SQL. A binding
names:

- the collection and the materialized result table
- the column holding each result's source-file identity
- the result format
- where the format finds its evidence:
  - the stored parser payload (and status column) for a provider's native output
  - or plain diagnostic columns for a pipeline that kept only those
- optionally, a column with the processed file's version, and a processing
  timestamp

The formats dex knows are:

- `bigquery_document_ai`
- `snowflake_ai_parse_document`
- `databricks_ai_parse_document`
- `mapped_columns`

A result source must be a materialized table. A view or table function can hide
a call that spends money and returns content, so neither is accepted.

## What a profile reports

A profile assesses a deterministic sample of the collection: 200 files by
default, and at most 1,000. Files are ordered by a hash of their source identity
with the identity as the tie-breaker, and chosen from the collection before any
result is matched, so a file with no result stays in the sample. The sample
bounds the assessment, not the warehouse scan, which is priced before it runs. It
is reproducible for unchanged input and is not a claim of statistical
representativeness.

The report has five groups:

| Group | Contents |
|---|---|
| `collection` | the collection, the result table and format, the families assessed, the metadata available |
| `coverage` | the sample split three ways: one resolvable result (`matched`), no result, or duplicates that could not be resolved to one (`ambiguous`) |
| `currency` | of the matched files, how many results match the file's current version, how many describe another version, and how many cannot be compared |
| `processing` | statuses (`success`, `failure`, `partial`, `unknown`), payload validity, files with only some pages represented, and a distribution per diagnostic |
| `limitations` | fixed statements of what the profile does not establish |

The profile obeys these rules:

- **Coverage always adds back up to the sample.**
- **Currency is compared only where both sides carry compatible version
  evidence.** A processing timestamp alone does not prove which version was
  processed, so everything else is reported unknown, never current.
- **Ambiguous duplicates are never picked.** They are excluded from every content
  distribution.
- **Diagnostics have fixed bins.** The diagnostics are text characters, reported
  pages, pages represented in the result, tables, form fields, and paragraphs. The
  first bin holds exactly the observed zeros, and files with no value are counted
  separately.
- **Stored status values are never returned.** Any value outside the fixed
  status vocabulary is counted as `unknown`.

Every profile states that document content was not screened for personal data,
that no document processing was invoked, and that the sample is not
representative. There is no overall readiness score. Successful parsing, long
text, or detected tables do not make an extraction correct.

## Implementing a file source

A connector reaches a capability by implementing the matching protocol in
`exmergo_dex_core.files.contract`:

- **`FileCollectionSource`** for metadata aggregation.
  - It declares `name`, `collection_kind`, `metadata_fields`, and
    `document_families`.
  - It implements `file_collection_inventory(collection)`.
- **`DiscoveringFileSource`** for discovery, with `list_file_collections()`.
- **`FileResultSource`** for result assessment. It adds:
  - `quote_identifier`
  - `source_sample_sql`, the bounded, deterministic source selection
  - `run_file_aggregate`, which runs one engine-assembled aggregate statement
    through the adapter's own cost gate and returns only the declared aliases

A result format implements `ResultFormat`, which turns a binding into
per-row SQL expressions and never executes anything.

Only the aggregate types in `exmergo_dex_core.files.results` cross from a source
into the command layer. Those types refuse free text, unknown keys, and `None` by
construction, so a source cannot return content even by mistake.
