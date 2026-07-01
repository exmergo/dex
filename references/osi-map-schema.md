# OSI: a dormant, future exporter (stub)

dbt is the source of truth (see `canonical-model.md`). OSI is not emitted in v1; it
is a future exporter that will read the dbt semantic model and write an OSI
document once the format matures. To keep the mechanism ready, dex ships a dormant
validator: OSI documents validate against the pinned
`packages/dex-core/src/exmergo_dex_core/schemas/osi-schema.json` via `jsonschema`,
and richness OSI cannot express rides in `custom_extensions` under the `DEX` vendor
name. The pin and its provenance are documented in `schemas/PINNED.md`. The
exporter (`exporters/osi.py`) is switched on when OSI matures. See the v9 system
design, §8.
