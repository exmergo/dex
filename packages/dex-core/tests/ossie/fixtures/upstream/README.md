# Upstream Apache Ossie documents

Copied verbatim from Apache Ossie (incubating) at commit
`7a8de20f850dad3934e307a1ac7425b09e9ca89f`, the commit whose
`core-spec/ossie-schema.json` dex pins. Licensed to the ASF under the Apache
License, Version 2.0; each file keeps its own license header.

| here | upstream |
|---|---|
| `examples/flights.semantic_model.ossie.yaml` | `examples/flights.semantic_model.yaml` |
| `examples/renamed_fields.ossie.yaml` | `examples/renamed_fields.yaml` |
| `examples/tpcds_semantic_model.ossie.yaml` | `examples/tpcds_semantic_model.yaml` |
| `compliance/*.ossie.yaml` | `compliance/portability/m*/*.ossie.yaml` |

The three examples gain the `.ossie.yaml` suffix dex requires of a configured
document; their bytes are unchanged. The ontology and mapping examples
(`flights.yaml`, `flights.ontology.yaml`, `flights.mapping.yaml`) are not
semantic model documents and are not copied.

`test_corpus.py` asserts that every document here loads with no error and no
warning. Re-copy them when the pin moves.
