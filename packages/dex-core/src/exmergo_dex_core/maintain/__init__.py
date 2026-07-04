"""Maintain: the snapshot and drift engine that keeps the project correct.

Maintenance is one job done on several axes: compare a known-good baseline (the
`.dex/` snapshot) against current reality (the warehouse and the dbt project),
classify what drifted, and propose the reconciling edit. The axes differ in where
they read from, what they cost, and how mechanical the fix is, which is why they
are distinct entry points rather than one flag:

- ``snapshot``   captures/refreshes the baseline in ``.dex/snapshot.json``: a
                 pinned copy of the explore cache (the warehouse side) plus
                 per-layer fingerprints of the project's definitions.
- ``check``      sweeps every axis against the snapshot and returns a
                 categorized, blast-radius-ranked drift report. On a billed
                 connector the free axes run immediately and the scanning axes
                 wait behind the usual confirm handshake, with a combined
                 estimate.
- ``schema``     structural drift: columns and tables added, dropped, retyped;
                 nullability changes; declared sources the warehouse no longer
                 honors. Metadata only, free on every connector.
- ``volume``     freshness drift: row counts and byte sizes that collapsed,
                 spiked, or went to zero. Metadata only, free on every
                 connector.
- ``grain``      cardinality and identity drift: keys that lost uniqueness and
                 verified joins whose fanout grew. SQL aggregates, billed on
                 metered connectors.
- ``semantic``   impact analysis for the semantic layer: definitions that
                 changed against the baseline and references that no longer
                 resolve (free), plus categorical dimensions whose cardinality
                 moved (a distinct count, billed on metered connectors; no
                 value is ever read into the report).
- ``reconcile``  proposes the dbt edits that bring the project back in sync, as
                 a stored plan of reviewable diffs (never applied here); each
                 proposal is tagged mechanical or advisory.

Detection is read-only against data. Only reconcile emits diffs, and it emits
them into the same plan store ``transform apply`` writes from, so there is
exactly one apply door and one human-edit conflict handshake
(propose-don't-impose).
"""
