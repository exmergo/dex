# Bundled Apache Ossie schema

`ossie-schema.json` is `core-spec/ossie-schema.json` from Apache Ossie
(incubating), vendored verbatim. It is not modified, reformatted, or
re-serialized: the bytes are the upstream bytes, which is what lets a hash pin
them.

| | |
|---|---|
| Upstream | <https://github.com/apache/ossie> |
| Commit | `b5da5d66f0da4a0cd3388d52201dbf5523221a77` |
| Path | `core-spec/ossie-schema.json` |
| SHA-256 | `27aab111647b1e8d2229a2413e4682e459f43edc62e0eaadd497317754089e42` |
| Declared spec version | `0.2.0.dev0` |
| License | Apache License 2.0 |

Apache Ossie is an effort undergoing incubation at The Apache Software
Foundation. The schema is licensed to the ASF under one or more contributor
license agreements and distributed under the Apache License, Version 2.0. See
<http://www.apache.org/licenses/LICENSE-2.0>.

## Why the pin is on content and not on the version string

The schema declares `version` as `const: "0.2.0.dev0"`, and the specification
says the schema may change before 0.2.0 is released. So the version string does
not move when the schema does, which makes a version check worthless as a drift
signal. `SCHEMA_SHA256` in `exmergo_dex_core.ossie.loader` records the hash
above and a test asserts the bundled file against it, so a regeneration is a
reviewed diff in a commit rather than a quiet update. The document's own
`version` field is still required and still checked, because upstream requires
it, and the schema itself is what checks it.

## Upgrading

1. Copy the new `core-spec/ossie-schema.json` in verbatim.
2. Update the commit, hash, and declared version in the table above.
3. Update `SCHEMA_SHA256` in `exmergo_dex_core/ossie/loader.py`.
4. Run the Ossie fixture suite and read the diffs. A fixture that changes
   verdict is the upgrade telling you what moved; record it in the changelog
   with the behavior it changes.
