# Vendored OSI schema (pinned)

`osi-schema.json` is a vendored, pinned copy of the Open Semantic Interchange
core schema. dex validates emitted OSI documents against this file directly via
`jsonschema` (draft 2020-12). It does **not** depend on an OSI library, because
OSI ships no PyPI package and no tagged releases.

## Pin

- **Source:** `open-semantic-interchange/OSI`, path `core-spec/osi-schema.json`
- **Commit:** `c2233f0255ba5ba2dbda1afb50e54f8302930a63`
- **Raw URL:**
  `https://raw.githubusercontent.com/open-semantic-interchange/OSI/c2233f0255ba5ba2dbda1afb50e54f8302930a63/core-spec/osi-schema.json`
- **Schema `version` const at this pin:** `0.2.0.dev0`
- **Top-level required:** `version`, `semantic_model`
- **License upstream:** Apache-2.0

## Why this pin (not a stable tag)

The latest *released* OSI spec is 0.1.1 (December 2025), but the project publishes
no git tags and no package, and `main` currently tracks `0.2.0.dev0` with a draft
warning that it may change before 0.2.0 releases. We therefore pin the exact
commit we tested against rather than a version string, and bump it deliberately.
Richness this schema cannot express rides in OSI's `custom_extensions` under the
`DEX` vendor name (see `osi.py`), validated against dex's own schema, not OSI's.

## Bumping

Re-download from the raw URL at a newer commit, update the Commit/version fields
above, and re-run the OSI validation tests before committing the bump.
