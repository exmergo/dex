**`transform plan` stamps PII metadata on the models it creates.** A new model
that carries a PII-flagged input column through, unchanged, under an alias or
through a value-preserving function, gets `config.meta.contains_pii` and
`pii_category` on that column and `contains_pii` on the model, as a scaffolded
model always did, with or without a declaration. `data.pii_metadata` reports
every judged column: measuring aggregates (`measured`) and constants standing
in for a flagged column (`redacted`) are not stamped, a hash or other
expression is `unresolved` with a warning, a hand-written mark is kept, and a
model that already exists is reported rather than written (#1100).
