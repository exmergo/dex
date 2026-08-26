"""Explore: candidate keys, grain, and declared/inferred joins.

Inference is metadata-only: it reads the profiles already gathered (names, types,
uniqueness signals) and never scans data, which keeps it free at the cost of
confidence, so every inferred join carries a confidence the agent can weigh. The
one deliberate exception is the opt-in ``--verify`` pass
(:func:`verify_relationships`), which runs one bounded, engine-authored aggregate
probe per join to measure the actual key overlap. Declared joins come from the
dbt project; absent one, they are simply empty (explore is designed to work
without a dbt project). They are probed too: a declaration is a claim about the
data, so it is measurable, and only what the measurement may *change* differs by
kind (see :func:`verify_relationships`).
"""

from __future__ import annotations

import re
from typing import NamedTuple

from ..adapters.base import Adapter
from ..cache import (
    ColumnProfile,
    Dataset,
    Relationship,
    RelationshipKind,
    match_identifier,
)
from ..config import EntityAffixes
from ..dbt_project import ProjectDefinitions
from ..progress import ProgressReporter
from .profile import NEAR_UNIQUE_RATIO

# Warehouse-layer prefixes stripped from a table name before entity matching, so
# RAW_HOSTS, stg_races, and dim_customers all match FKs named after the bare entity.
_LAYER_PREFIX = re.compile(r"^(raw|stg|src|dim|fct|fact|mart|int)_", re.IGNORECASE)

# Suffixes that make a column id-shaped. `id` is the common convention; `key` is
# just as common in dimensional models (`customer_key` surrogate keys) and is the
# *only* FK convention in some warehouses (TPC-H: `o_custkey`, `l_orderkey`, ...).
# A new convention (e.g. a shop that suffixes `_pk`/`_fk`) is a one-line addition
# here, not a rewrite of the three shapes below.
_ID_SUFFIXES = ("id", "key")

# A short table-alias prefix on a key column, stripped before comparing FK and
# parent-key names so an alias convention (TPC-H: `o_custkey` vs `c_custkey`)
# doesn't hide a shared suffix. Bounded to 3 chars so a genuine entity name
# (`customer_id`) is never mistaken for an alias.
_COLUMN_ALIAS_PREFIX = re.compile(r"^[a-z]{1,3}_", re.IGNORECASE)

# A single-column key name held, verbatim, by this many or more datasets is a
# naming convention rather than a specific entity's identifier: CDC exports
# from Firestore/Mongo/DynamoDB-style sources routinely name every
# collection's own id column identically (`document_id` in all ~90 of them),
# and two tables merely sharing that name is not evidence they're related.
# Below this, a shared name is still a useful same-named-FK signal; at or
# above it, matching on the bare name alone degenerates into a near-complete
# cross product on this class of source (issue #77). Only the same-named-FK
# tier of `_match_parent` needs this guard: the entity-name tier already ties
# a match to one specific parent by name, and the dealiased tier already
# refuses a match that collapses to a bare suffix, so neither is vulnerable to
# a name that's merely popular.
_GENERIC_NAME_MIN_HOSTS = 3

# A trailing table-version marker (`_v2`, `_v3`, ...), stripped unconditionally
# when the configured `EntityAffixes` don't resolve a match on their own. This
# is a structural convention (like the `_ID_SUFFIXES` shapes), not a
# house-specific word, so it isn't part of the configurable affix lists.
_VERSION_SUFFIX = re.compile(r"_v\d+$", re.IGNORECASE)


class SuppressedMatch(NamedTuple):
    """A same-named-FK match withheld because the shared name is too generic
    to trust (see `_GENERIC_NAME_MIN_HOSTS`). Recorded so a caller can report
    what inference declined to verify, and why, instead of silently doing
    less."""

    shared_name: str
    host_count: int


class AffixMatch(NamedTuple):
    """A join matched only after stripping a configured entity affix (see
    `EntityAffixes`) from the parent's table name, because the exact-name tier
    missed. Recorded so a caller can report when a match relied on
    affix-stripping rather than an exact entity name; scored lower than an
    exact match to the same key (issue #208)."""

    child_column: str
    parent: str
    stripped_to: str


def _strip_configured_affixes(name: str, affixes: EntityAffixes) -> str:
    """Lowercased ``name`` with configured prefixes/suffixes, and a trailing
    version marker, stripped repeatedly until none remain.

    Repetition (rather than one pass) is what lets a name layered with more
    than one convention reduce fully: `conversation_history_data` sheds
    `_data` and then `_history` in two passes of the same suffix loop, in
    whatever order the config lists them.
    """

    stripped = name.lower()
    changed = True
    while changed:
        changed = False
        version = _VERSION_SUFFIX.search(stripped)
        if version is not None and version.start() > 0:
            stripped = stripped[: version.start()]
            changed = True
            continue
        for suffix in affixes.suffixes:
            tail = f"_{suffix.lower()}"
            if stripped.endswith(tail) and len(stripped) > len(tail):
                stripped = stripped[: -len(tail)]
                changed = True
                break
        if changed:
            continue
        for prefix in affixes.prefixes:
            head = f"{prefix.lower()}_"
            if stripped.startswith(head) and len(stripped) > len(head):
                stripped = stripped[len(head) :]
                changed = True
                break
    return stripped


def _fk_stem(column_name: str) -> str | None:
    """The entity stem of an id-shaped column, or None if not id-shaped.

    Recognizes each suffix in :data:`_ID_SUFFIXES` in the three naming shapes
    seen in real warehouses: underscore-separated, any case (`customer_id`,
    `HOST_ID`, `nation_key`), camelCase (`raceId`, `customerKey`), and a
    trailing suffix with no separator at all. The no-separator shape is only
    accepted for `key`: TPC-H's own FK convention is exactly that (`CUSTKEY`,
    `NATIONKEY`), and unlike `id` it isn't the tail of ordinary English words
    (`PAID`, `VALID`, `GRID`), so `HOSTID` stays ambiguous and skipped while
    `CUSTKEY` doesn't. A bare `id` or `key` is a key, not a foreign key, so it
    has no stem.
    """

    if column_name.lower() in _ID_SUFFIXES:
        return None
    for suffix in _ID_SUFFIXES:
        if re.search(rf"(?<=.)_{suffix}$", column_name, re.IGNORECASE):
            return column_name[: -(len(suffix) + 1)]
        camel_suffix = suffix[0].upper() + suffix[1:]
        if re.search(rf"(?<=[a-z0-9]){camel_suffix}$", column_name):
            return column_name[: -len(suffix)]
    if re.search(r"(?<=[A-Za-z0-9])KEY$", column_name, re.IGNORECASE):
        return column_name[:-3]
    return None


def _dealias(column_name: str) -> str:
    """Lowercased column name with a short table-alias prefix stripped."""

    return _COLUMN_ALIAS_PREFIX.sub("", column_name.lower())


def _entity(table_name: str) -> str:
    """The entity a table represents: layer prefix stripped, singularized, lowered."""

    return _singularize(_LAYER_PREFIX.sub("", table_name)).lower()


def _is_id_shaped(column_name: str) -> bool:
    return column_name.lower() in _ID_SUFFIXES or _fk_stem(column_name) is not None


def candidate_keys(dataset: Dataset) -> list[list[str]]:
    """Candidate keys: single columns first, proven composites after.

    Single-column keys are unique and non-null columns; uniqueness on
    near-unique columns is escalated to an exact COUNT(DISTINCT) at profile
    time (``distinct_count_exact``), so these are proven where it matters,
    while a column whose uniqueness still rests on the approximate count is a
    signal, not a proof. Composite keys come from ``dataset.composite_keys``,
    each one proven by an exact distinct-combination probe at profile time.
    """

    singles = [
        [col.name]
        for col in dataset.columns
        if col.is_unique and (col.null_fraction in (0.0, None))
    ]
    return singles + [list(key) for key in dataset.composite_keys]


def detect_grain(dataset: Dataset) -> list[str] | None:
    """The most likely grain: prefer an ``id`` / ``<entity>_id`` single-column
    candidate key, else the unique column with the smallest cardinality. A
    composite key is the grain only when no single column is one (the fact
    table shape); composites arrive best-ranked first from the profile probe.
    None if no key at all."""

    keys = candidate_keys(dataset)
    singles = [key for key in keys if len(key) == 1]
    if not singles:
        composites = [key for key in keys if len(key) > 1]
        return composites[0] if composites else None
    entity = _entity(dataset.identifier.rsplit(".", 1)[-1])
    for key in singles:
        name = key[0].lower()
        if name in ("id", f"{entity}_id", f"{entity}id") or _is_id_shaped(key[0]):
            return key
    # Fall back to the lowest-cardinality unique column.
    by_card = sorted(
        singles,
        key=lambda k: _distinct_of(dataset, k[0]) or float("inf"),
    )
    return by_card[0]


def _key_host_counts(keyed: dict[str, list[list[str]]]) -> dict[str, int]:
    """How many distinct datasets hold each single-column key name (lowercased).

    A name held as a key by only one or two datasets is a specific entity's own
    identifier; held by many, it is a naming convention rather than a reference
    (see `_GENERIC_NAME_MIN_HOSTS`). Metadata-only: derived entirely from the
    candidate keys already computed at profile time, no extra queries.
    """

    counts: dict[str, int] = {}
    for keys in keyed.values():
        names = {k[0].lower() for k in keys if len(k) == 1}
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


def infer_relationships(
    datasets: list[Dataset],
    *,
    suppressed: list[SuppressedMatch] | None = None,
    affixes: EntityAffixes | None = None,
    affix_matches: list[AffixMatch] | None = None,
) -> list[Relationship]:
    """Infer many-to-one joins from column names, type compatibility, and the
    aggregate signals already profiled (uniqueness, distinct counts, min/max).

    A parent whose key is not unique still yields a join, at reduced confidence:
    suppressing it entirely would hide a real join behind a data-quality problem
    that :func:`data_quality_notes` reports separately.

    A same-named-FK match withheld as a generic-name collision (see
    `_GENERIC_NAME_MIN_HOSTS`) is recorded to ``suppressed`` when a caller
    passes a list, so the withheld count and the names involved can be
    reported; the default ``None`` costs nothing extra.

    ``affixes`` (a project's configured :class:`EntityAffixes`, or ``None`` to
    skip the tier entirely) lets a parent whose name carries a house-convention
    suffix or prefix the exact entity-name tier can't see (a CDC history table,
    a landing-zone `_data`/`_raw` suffix, ...) still match, at a base confidence
    kept below the exact tier's; matches made this way are recorded to
    ``affix_matches`` the same way suppressions are.
    """

    keyed = {d.identifier: candidate_keys(d) for d in datasets}
    host_counts = _key_host_counts(keyed)
    relationships: list[Relationship] = []

    for child in datasets:
        for col in child.columns:
            stem = _fk_stem(col.name)
            if stem is None:
                continue
            for parent in datasets:
                if parent.identifier == child.identifier:
                    continue
                match = _match_parent(
                    col,
                    stem,
                    parent,
                    keyed[parent.identifier],
                    host_counts,
                    suppressed,
                    affixes,
                    affix_matches,
                )
                if match is not None:
                    to_columns, confidence = match
                    relationships.append(
                        Relationship(
                            from_dataset=child.identifier,
                            from_columns=[col.name],
                            to_dataset=parent.identifier,
                            to_columns=to_columns,
                            kind=RelationshipKind.INFERRED,
                            confidence=confidence,
                        )
                    )
    return relationships


def fold_replica_relationships(
    datasets: list[Dataset],
    relationships: list[Relationship],
    dev_schemas: frozenset[str] = frozenset(),
) -> tuple[list[Relationship], int, int]:
    """Fold same-lineage duplicate joins that appear when a dev/replica dataset is
    mapped alongside its source.

    A replica's models mirror source entities and keys, so one real foreign key
    inflates into the source edge, the replica edge, and cross-dataset lookalike
    edges. Returns ``(kept, folded_count, mirrored_object_count)``.

    A replica schema is one whose short name is in ``dev_schemas`` or one that
    structurally mirrors another (the same layer-stripped entity and column set
    living in a second schema). With no replica in scope nothing is folded, so a
    single-dataset map is untouched. Within each lineage signature that a replica
    edge participates in, the canonical (source-schema) edge is kept and the
    duplicates are dropped.
    """

    def schema_of(identifier: str) -> str:
        return identifier.rsplit(".", 1)[0]

    def bare(identifier: str) -> str:
        return identifier.rsplit(".", 1)[-1]

    # Compare case-insensitive short names on both sides: a BigQuery dev_dataset
    # may be configured qualified (`project.dataset`), and Snowflake/Redshift
    # schema identifiers are often cased differently from the configured value.
    dev_short = {s.rsplit(".", 1)[-1].casefold() for s in dev_schemas}

    def is_dev(schema: str) -> bool:
        return schema.rsplit(".", 1)[-1].casefold() in dev_short

    # An entity+columns fingerprint held by more than one schema is a mirrored
    # entity; a dev schema is a replica by declaration even without a structural
    # twin in scope.
    schemas_by_fingerprint: dict[tuple[str, frozenset[str]], set[str]] = {}
    tables_per_schema: dict[str, int] = {}
    present_schemas: set[str] = set()
    for dataset in datasets:
        schema = schema_of(dataset.identifier)
        present_schemas.add(schema)
        key = (
            _entity(bare(dataset.identifier)),
            frozenset(c.name.lower() for c in dataset.columns),
        )
        schemas_by_fingerprint.setdefault(key, set()).add(schema)
        if dataset.object_type == "table":
            tables_per_schema[schema] = tables_per_schema.get(schema, 0) + 1

    mirrored_schemas: set[str] = {s for s in present_schemas if is_dev(s)}
    for schemas in schemas_by_fingerprint.values():
        if len(schemas) > 1:
            mirrored_schemas |= schemas
    if not mirrored_schemas:
        return relationships, 0, 0

    # Canonical schema: prefer a non-dev schema, then the one with the most base
    # tables (a source has tables where a replica has staging views), then name.
    canonical = min(
        mirrored_schemas,
        key=lambda s: (is_dev(s), -tables_per_schema.get(s, 0), s),
    )
    replica_schemas = mirrored_schemas - {canonical}
    mirrored_object_count = sum(
        1 for d in datasets if schema_of(d.identifier) in replica_schemas
    )

    def replica_endpoints(rel: Relationship) -> int:
        return sum(
            schema_of(endpoint) in replica_schemas
            for endpoint in (rel.from_dataset, rel.to_dataset)
        )

    def signature(rel: Relationship) -> tuple:
        return (
            _entity(bare(rel.from_dataset)),
            tuple(c.lower() for c in rel.from_columns),
            _entity(bare(rel.to_dataset)),
            tuple(c.lower() for c in rel.to_columns),
        )

    groups: dict[tuple, list[Relationship]] = {}
    for rel in relationships:
        groups.setdefault(signature(rel), []).append(rel)

    kept: list[Relationship] = []
    folded = 0
    for members in groups.values():
        if len(members) == 1 or not any(replica_endpoints(r) for r in members):
            kept.extend(members)
            continue
        best = min(
            members,
            key=lambda r: (
                replica_endpoints(r),
                -(r.confidence or 0.0),
                r.from_dataset,
                r.to_dataset,
            ),
        )
        kept.append(best)
        folded += len(members) - 1
    return kept, folded, mirrored_object_count


def fk_candidate_count(datasets: list[Dataset]) -> int:
    """How many profiled columns look like foreign keys. Reported alongside the
    inference result so an empty relationships array is distinguishable from
    'nothing id-shaped to try'."""

    return sum(1 for d in datasets for c in d.columns if _fk_stem(c.name) is not None)


def data_quality_notes(dataset: Dataset) -> list[str]:
    """The interpretation an analyst would write from the aggregates already
    gathered: broken grain on the table's own key, and an unknown grain.

    Only the table's own key columns (bare ``id`` or ``<own entity>_id``) are
    checked for uniqueness; a repeated foreign key is the expected shape of a
    child table, not a defect.
    """

    notes: list[str] = []
    if not dataset.row_count:
        return notes

    entity = _entity(dataset.identifier.rsplit(".", 1)[-1])
    for col in dataset.columns:
        stem = _fk_stem(col.name)
        own_key = col.name.lower() == "id" or (
            stem is not None and _singularize(stem).lower() == entity
        )
        if not own_key or col.distinct_count is None:
            continue
        if (
            not col.distinct_count_exact
            and col.distinct_count >= NEAR_UNIQUE_RATIO * dataset.row_count
        ):
            # Within approximation noise of unique: unproven either way, so no
            # verdict. Exact counts always speak; a shortfall too large for
            # noise (an approx 500 distinct over 1,125 rows) still warns.
            continue
        if col.distinct_count < dataset.row_count:
            duplicates = dataset.row_count - col.distinct_count
            # An unescalated count is honest about being approximate.
            marker = "" if col.distinct_count_exact else "~"
            notes.append(
                f"{col.name} is not unique: {marker}{col.distinct_count} distinct "
                f"over {dataset.row_count} rows (~{duplicates} duplicate rows); "
                "joins on it will fan out"
            )

    if not candidate_keys(dataset):
        notes.append("no candidate key detected; grain unknown")
    return notes


# Below this, a high orphan rate is still just weaker evidence for the
# inferred join (verify_relationships already demotes confidence starting at
# 0.2). At or above it, the two columns are effectively disjoint: a shared
# name with this little shared data is not a shared key, and joining on it
# returns all-NULL parent attributes while looking like it worked (issue
# #207). Set well above the confidence-demotion tier so this fires only for
# the catastrophic case the issue is about, not every demoted edge.
_ORPHAN_FINDING_THRESHOLD = 0.9


def orphan_findings(
    relationships: list[Relationship],
) -> list[tuple[Relationship, str]]:
    """A verified join whose orphan fraction clears `_ORPHAN_FINDING_THRESHOLD`,
    paired with the finding text a caller (or a future drift-sweep detector)
    needs: both sides, named, plus the measured fraction. Anything not verified
    (nothing was measured) never qualifies; confidence arithmetic is unchanged,
    this only reports what `verify_relationships` already measured.

    Each kind gets its own text because each is a different finding. For an
    inferred join the claim under suspicion is dex's own: a shared column name
    turned out not to be a shared key. For a declared one the name is not the
    evidence at all, so there is nothing to disclaim; the project states this
    foreign key and the warehouse disagrees, which is a defect in the data or
    in the declaration rather than a weak guess (issue #163). For an
    overlap-inferred edge the name never mattered in the first place, so a
    later catastrophic orphan rate reads as the data drifting away from the
    containment a probe once measured, not as a naming coincidence failing.
    """

    findings = []
    for rel in relationships:
        if not rel.verified:
            continue
        if rel.orphan_fraction is None:
            continue  # verified but zero non-null FK values: nothing measured
        if rel.orphan_fraction < _ORPHAN_FINDING_THRESHOLD:
            continue
        edge = (
            f"{rel.from_dataset}.{rel.from_columns[0]} -> "
            f"{rel.to_dataset}.{rel.to_columns[0]}"
        )
        if rel.kind is RelationshipKind.DECLARED:
            text = (
                f"{edge} is declared as a foreign key but "
                f"{rel.orphan_fraction:.0%} of values have no match in the "
                "parent; the project and the warehouse disagree"
            )
        elif rel.kind is RelationshipKind.OVERLAP_INFERRED:
            text = (
                f"{edge} was proposed from measured value overlap but "
                f"{rel.orphan_fraction:.0%} of values now have no match; the "
                "containment that proposed this edge no longer holds"
            )
        else:
            text = (
                f"{edge} shares a column name but "
                f"{rel.orphan_fraction:.0%} of values have no match; the shared "
                "name is not evidence of a shared key"
            )
        findings.append((rel, text))
    return findings


def probe_candidates(relationships: list[Relationship]) -> list[Relationship]:
    """The joins an overlap probe would measure, declared and inferred alike.

    The single definition of "what verify runs on". :func:`verify_relationships`
    and :func:`probe_statements` both select through here so the set that gets
    priced and the set that gets run cannot drift apart: pricing N probes and
    then issuing N+M under-reports spend before it happens, which is the one
    thing the cost guardrail exists to prevent.

    A declared join is included because the overlap SQL does not care how the
    relationship was learned. Declaring a foreign key is a claim about the data,
    and a claim is exactly the kind of thing worth measuring (issue #163). What
    the measurement is *allowed to change* still depends on the kind: see
    :func:`verify_relationships` on confidence.

    A composite join is excluded, and the exclusion is load-bearing rather than
    an oversight. :func:`_overlap_probe_sql` joins on ``from_columns[0]`` and
    ``to_columns[0]`` only, which was total coverage while inference was the
    sole source (it emits single-column edges by construction) and stops being
    so now that declared edges qualify. Probing the first column of a composite
    key measures a different relationship than the one declared and would
    report its orphan count as though it were the join's: silently wrong beats
    unmeasured, so these stay unverified until the probe itself spans a key.
    """

    return [
        rel
        for rel in relationships
        if len(rel.from_columns) == 1 and len(rel.to_columns) == 1
    ]


def verify_relationships(
    adapter: Adapter,
    relationships: list[Relationship],
    *,
    timeout_seconds: float = 30.0,
    progress: ProgressReporter | None = None,
) -> None:
    """Measure each join with one overlap probe and adjust in place.

    The probe counts non-null foreign-key values and how many have no match in
    the parent (orphans). Aggregate counts only; no key value ever leaves the
    engine.

    **Measurement applies to every candidate; confidence arithmetic does not.**
    For an inferred join, full containment raises confidence and a high orphan
    rate demotes it well below the emission threshold rather than deleting it,
    so the agent still sees what was tried. A declared join is not a name-based
    guess whose confidence is up for revision: the dbt project asserts it at
    1.0, and a measurement that disagrees is a finding about the *data*, not
    weaker evidence for the join. So ``verified`` and ``orphan_fraction`` are
    set for both kinds and ``confidence`` moves only for inferred ones (issue
    #163); a declared join that fails its probe surfaces through
    :func:`orphan_findings`, where the disagreement can be stated plainly.

    An optional ``progress`` reporter emits a throttled stderr line per probed
    join, so its counts match a ``total`` taken from :func:`probe_candidates`;
    ``None`` (the default) keeps existing callers silent and unchanged.
    """

    for rel in probe_candidates(relationships):
        sql = _transpile_probe(_overlap_probe_sql(rel), adapter.dialect)
        result = adapter.run_query(sql, max_rows=1, timeout_seconds=timeout_seconds)
        values = dict(zip(result.columns, result.cells[0], strict=True))
        nonnull = int(values["nonnull_fk"] or 0)
        orphans = int(values["orphans"] or 0)

        rel.verified = True
        if nonnull == 0:
            rel.orphan_fraction = None
            if progress is not None:
                progress.advance()  # this iteration ran a probe; count it
            continue
        fraction = orphans / nonnull
        rel.orphan_fraction = round(fraction, 4)

        if rel.kind is RelationshipKind.INFERRED:
            confidence = rel.confidence or 0.5
            if fraction == 0.0:
                confidence += 0.1
            elif fraction <= 0.02:
                confidence += 0.05
            elif fraction >= 0.2:
                confidence -= 0.25
            else:
                confidence -= 0.1
            rel.confidence = round(min(0.95, max(0.05, confidence)), 4)

        if progress is not None:
            progress.advance()


def probe_statements(relationships: list[Relationship], dialect: str) -> list[str]:
    """The exact SQL :func:`verify_relationships` will run, one statement per
    probed join, in the adapter's dialect. Exists so a billed caller can
    dry-run the probes for a cost estimate before confirming the spend."""

    return [
        _transpile_probe(_overlap_probe_sql(rel), dialect)
        for rel in probe_candidates(relationships)
    ]


def _overlap_probe_sql(rel: Relationship) -> str:
    child = _quote_identifier(rel.from_dataset)
    parent = _quote_identifier(rel.to_dataset)
    fk = _quote_part(rel.from_columns[0])
    key = _quote_part(rel.to_columns[0])
    # Aggregate-only by construction: two counts, no value in the projection.
    # A LEFT JOIN against the DISTINCT parent keys keeps the orphan count
    # correct even when the parent key is not unique (a bare join would fan
    # out and inflate it). Deliberately portable SQL: CASE inside COUNT
    # rather than FILTER (which BigQuery lacks and sqlglot does not rewrite),
    # and a join rather than a projected NOT EXISTS, which Redshift refuses
    # outright (XX000: correlated subquery pattern not supported).
    return (
        f"SELECT COUNT(c.{fk}) AS nonnull_fk, "  # noqa: S608
        f"COUNT(CASE WHEN c.{fk} IS NOT NULL AND d.pk IS NULL THEN 1 END) "
        f"AS orphans "
        f"FROM {child} c LEFT JOIN ("
        f"SELECT DISTINCT {key} AS pk FROM {parent}) d ON d.pk = c.{fk}"
    )


def _transpile_probe(sql: str, dialect: str) -> str:
    """Render the DuckDB-flavored probe in the active connector's dialect.

    The probe is authored once in DuckDB SQL (double-quoted identifiers,
    ``COUNT(*) FILTER``); sqlglot rewrites it per connector (BigQuery gets
    backticks and COUNTIF). Identity on DuckDB itself.
    """

    if dialect == "duckdb":
        return sql
    import sqlglot

    return sqlglot.transpile(sql, read="duckdb", write=dialect)[0]


def _quote_identifier(identifier: str) -> str:
    return ".".join(_quote_part(p) for p in identifier.split("."))


def _quote_part(name: str) -> str:
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


# --- issue #220: propose joins from measured value overlap -------------------
#
# Name-based inference above exhausts every naming convention it knows before
# anything below here ever runs. What's left is exactly the case naming
# cannot help with (`acct_id_fk` to `ws_id`, or a source that names every key
# `id`). Value overlap is strictly stronger evidence than a name, since it is
# the thing a name is a proxy for, so an opt-in, bounded sweep proposes an
# edge here purely from measured containment, never from a name.
#
# The bound is the whole design: the candidate pool is a cross product over
# every unmatched key-shaped column, so it is opt-in, capped, and priced as a
# batch through the normal handshake before anything runs, the same as
# `--verify`. An edge this sweep proposes is always
# `RelationshipKind.OVERLAP_INFERRED`, distinguishable from a name-derived or
# declared edge in both the cache and the envelope.
#
# A composite key never enters the candidate pool, for the same reason
# `probe_candidates` excludes a composite join from `--verify`: probing one
# column of a composite key measures a different relationship than the whole
# key would, so a column that is only a composite-key member (never unique or
# near-unique on its own) is not "key-shaped" for this sweep's purposes.

# A hard ceiling on how many probes one sweep run issues. The candidate pool
# grows quadratically in the number of unmatched key-shaped columns, so an
# unbounded sweep on a wide warehouse would price, and eventually run, an
# enormous batch. Elided candidates are reported, never silently dropped.
_OVERLAP_SWEEP_CAP = 50

# A candidate's measured orphan fraction must clear this ceiling to be
# proposed as an edge. Much stricter than `_ORPHAN_FINDING_THRESHOLD` (which
# flags an *existing* edge gone bad): here there is no name-based prior at
# all backing the guess, only the measurement itself, so the bar for "this is
# real" is correspondingly higher. Matches this codebase's other "almost
# entirely" ceilings (e.g. `cumulative._DECREASE_FRACTION_CEILING`).
_OVERLAP_ORPHAN_CEILING = 0.05

# Below this many non-null values, a candidate's containment is not enough
# evidence either way; it is dropped rather than proposed on a handful of
# rows that happened to line up. Mirrors this codebase's other "not enough
# evidence, fail closed" floors (e.g. `cumulative._MIN_OBSERVATIONS`).
_OVERLAP_MIN_OBSERVATIONS = 20


def _is_key_or_near_key(col: ColumnProfile, row_count: int | None) -> bool:
    """A single column already established, at profile time, as a proven key
    or a near-key: the pool the issue restricts overlap-sweep candidates to.

    A proven key is exactly :func:`candidate_keys`'s single-column half:
    unique and non-null. A near-key widens that to a column whose distinct
    count alone (an escalation cap, or an adapter without
    ``exact_distinct_counts``, left it short of proof) already clears
    ``NEAR_UNIQUE_RATIO``, the same ratio ``profile.py``'s composite-key probe
    uses to decide a column is worth testing at all.

    PII-excluded: every other candidacy check in this codebase excludes a
    PII-flagged column from a probe pool, and this sweep is no exception even
    though its probe never projects a value.
    """

    if col.pii is not None:
        return False
    if col.null_fraction not in (0.0, None):
        return False
    if col.is_unique:
        return True
    if row_count and col.distinct_count is not None:
        return col.distinct_count >= NEAR_UNIQUE_RATIO * row_count
    return False


def _key_strength(col: ColumnProfile) -> int:
    """0 for a proven single-column key, 1 for a near-key. Lower is
    stronger, and decides which side of a sweep candidate plays parent."""

    return 0 if (col.is_unique and col.null_fraction in (0.0, None)) else 1


def _order_by_strength(
    a: tuple[Dataset, ColumnProfile], b: tuple[Dataset, ColumnProfile]
) -> tuple[tuple[Dataset, ColumnProfile], tuple[Dataset, ColumnProfile]]:
    """Which of two key-shaped columns plays parent in a sweep candidate.

    Neither side of an unmatched pair comes with a declared direction, since
    that is exactly what "no name matched" means, so this picks one
    deterministically: the proven key over the near-key, and between two
    equally strong, the smaller table, since a many-to-one join's "one" side
    is usually the smaller one. Ties break on identifier so the same
    warehouse always proposes the same direction.

    Returns ``(child, parent)``.
    """

    def rank(pair: tuple[Dataset, ColumnProfile]) -> tuple:
        dataset, col = pair
        return (_key_strength(col), dataset.row_count or 0, dataset.identifier.lower())

    if rank(a) <= rank(b):
        parent, child = a, b
    else:
        parent, child = b, a
    return child, parent


def _sweep_sort_key(rel: Relationship) -> tuple:
    return (
        rel.from_dataset.lower(),
        rel.from_columns[0].lower(),
        rel.to_dataset.lower(),
        rel.to_columns[0].lower(),
    )


def overlap_sweep_candidates(
    datasets: list[Dataset],
    matched: set[tuple[str, str]],
    *,
    cap: int = _OVERLAP_SWEEP_CAP,
) -> tuple[list[Relationship], int, int]:
    """Every key-or-near-key column pair, across different datasets, that no
    existing edge already covers on either endpoint, restricted to
    type-compatible pairs, sorted deterministically and capped at ``cap``.

    ``matched`` is the set of ``(dataset identifier, column name)``, both
    lowercased, that a cheaper rule (declared or name-inferred) already has
    an edge on; a key-shaped column already covered never re-enters here,
    which is the entire reason this sweep runs after inference rather than
    instead of it.

    Returns ``(kept, elided, cap)``: ``cap`` is echoed back (rather than left
    for the caller to import as a private constant) so a checkpoint payload
    can always report the bound that applied, even when the caller passes no
    explicit ``cap`` and gets the default. ``elided`` is how many candidates
    the cap dropped, per the issue's bounding requirement. Every returned
    candidate is a real ``Relationship`` (``kind=OVERLAP_INFERRED``,
    unmeasured: ``verified`` and ``orphan_fraction`` stay unset until a probe
    actually runs), so pricing (:func:`overlap_sweep_statements`) and running
    (:func:`probe_overlap_candidates`) both select from exactly this list, the
    same one-source-of-truth pattern :func:`probe_candidates` uses for
    ``--verify``.
    """

    pool: list[tuple[Dataset, ColumnProfile]] = []
    for dataset in datasets:
        for col in dataset.columns:
            if (dataset.identifier.lower(), col.name.lower()) in matched:
                continue
            if _is_key_or_near_key(col, dataset.row_count):
                pool.append((dataset, col))

    # Each unordered pair of pool entries is visited exactly once (`pool` has
    # no duplicate (dataset, column), and enumerate/pool[i+1:] never revisits
    # a pair), so no dedup is needed here the way `matched` needed one above.
    candidates: list[Relationship] = []
    for i, a in enumerate(pool):
        for b in pool[i + 1 :]:
            if a[0].identifier == b[0].identifier:
                continue
            if not _type_compatible(a[1].data_type, b[1].data_type):
                continue
            child, parent = _order_by_strength(a, b)
            candidates.append(
                Relationship(
                    from_dataset=child[0].identifier,
                    from_columns=[child[1].name],
                    to_dataset=parent[0].identifier,
                    to_columns=[parent[1].name],
                    kind=RelationshipKind.OVERLAP_INFERRED,
                )
            )

    candidates.sort(key=_sweep_sort_key)
    elided = max(0, len(candidates) - cap)
    return candidates[:cap], elided, cap


def overlap_sweep_statements(candidates: list[Relationship], dialect: str) -> list[str]:
    """The exact SQL :func:`probe_overlap_candidates` will run, one statement
    per candidate, in the adapter's dialect. Exists so a billed caller can
    dry-run the sweep for a cost estimate before confirming the spend.

    Every candidate :func:`overlap_sweep_candidates` returns is already
    single-column by construction, so this skips the `probe_candidates`
    composite-key filter ``--verify`` needs; there is nothing to filter."""

    return [_transpile_probe(_overlap_probe_sql(rel), dialect) for rel in candidates]


def probe_overlap_candidates(
    adapter: Adapter,
    candidates: list[Relationship],
    *,
    timeout_seconds: float = 30.0,
    progress: ProgressReporter | None = None,
) -> int:
    """Probe each sweep candidate for real containment, in place.

    A candidate whose containment clears both ``_OVERLAP_MIN_OBSERVATIONS``
    and ``_OVERLAP_ORPHAN_CEILING`` is proposed: mutated to ``verified=True``,
    its measured ``orphan_fraction``, and a ``confidence`` derived from it.
    Anything short of that is left exactly as :func:`overlap_sweep_candidates`
    built it (``verified=False``), which doubles as how a caller tells
    proposed from rejected afterward: filter ``candidates`` on ``verified``
    rather than trust this function's return value for that split.

    In-place mutation rather than a returned list of survivors is
    deliberate: it is what keeps a mid-loop ``OverCeilingError`` (this raises,
    it does not catch) non-destructive. Whatever this already decided about a
    candidate before the ceiling hit stays decided on the very object the
    caller is still holding, the same way :func:`verify_relationships`
    survives its own mid-loop ceiling by mutating in place rather than
    building a return value that a raise would discard.

    Only the two aggregate counts the probe itself computes are ever read;
    reuses :func:`_overlap_probe_sql` verbatim, so this is exactly as
    value-blind as :func:`verify_relationships`.

    Returns the count rejected by measurement (a candidate the ceiling cut
    off before it was probed at all is neither proposed nor counted here).
    """

    rejected = 0
    for rel in candidates:
        sql = _transpile_probe(_overlap_probe_sql(rel), adapter.dialect)
        result = adapter.run_query(sql, max_rows=1, timeout_seconds=timeout_seconds)
        values = dict(zip(result.columns, result.cells[0], strict=True))
        nonnull = int(values["nonnull_fk"] or 0)
        orphans = int(values["orphans"] or 0)
        if progress is not None:
            progress.advance()
        if nonnull < _OVERLAP_MIN_OBSERVATIONS:
            rejected += 1
            continue
        fraction = orphans / nonnull
        if fraction > _OVERLAP_ORPHAN_CEILING:
            rejected += 1
            continue
        rel.verified = True
        rel.orphan_fraction = round(fraction, 4)
        rel.confidence = round(min(0.95, max(0.05, 1.0 - fraction)), 4)
    return rejected


def declared_relationships(
    defs: ProjectDefinitions, known_identifiers: list[str]
) -> tuple[list[Relationship], list[str]]:
    """Declared joins from the dbt project, resolved against this connection's
    identifiers.

    A ``relationships`` test is the project's own statement of a foreign key, so
    every resolvable one is emitted at confidence 1.0. Resolution never guesses:
    an endpoint matching nothing or matching more than one object yields a note
    instead of an edge (a declared relation missing from the connection is a
    drift signal worth surfacing, not an error). Empty definitions (the common
    explore-without-dbt case) yield nothing.
    """

    relationships: list[Relationship] = []
    notes: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for fk in defs.foreign_keys:
        child, child_ambiguous = resolve_declared(
            fk.relation, fk.model, known_identifiers
        )
        parent, parent_ambiguous = resolve_declared(
            fk.to_relation, fk.to_model, known_identifiers
        )
        label = f"declared join {fk.model}.{fk.column} -> {fk.to_model}.{fk.to_column}"
        if child is None or parent is None:
            if child_ambiguous or parent_ambiguous:
                notes.append(
                    f"{label} matches more than one object here; skipped rather "
                    "than guessed"
                )
            else:
                notes.append(
                    f"{label} references a relation not in this connection's inventory"
                )
            continue
        key = (child.lower(), fk.column.lower(), parent.lower(), fk.to_column.lower())
        if key in seen:
            continue
        seen.add(key)
        relationships.append(
            Relationship(
                from_dataset=child,
                from_columns=[fk.column],
                to_dataset=parent,
                to_columns=[fk.to_column],
                kind=RelationshipKind.DECLARED,
                confidence=1.0,
            )
        )
    return relationships, notes


def resolve_declared(
    relation: str | None, name: str, known: list[str]
) -> tuple[str | None, bool]:
    """One declared endpoint as a unique known identifier, or why not.

    Tries the most specific form first (the manifest's quote-stripped
    ``db.schema.table``, or the model / ``source.table`` name from YAML), then
    progressively shorter dotted suffixes: the manifest's database component
    routinely disagrees with the adapter-normalized identifier (a DuckDB file
    stem, a profile database alias), while the suffix still pins the object.
    Returns ``(identifier, False)`` on a unique match, ``(None, True)`` when a
    suffix matched several objects (shorter suffixes only widen, so stop), and
    ``(None, False)`` when nothing matched at all.
    """

    parts = (relation or name).split(".")
    for start in range(len(parts)):
        matches = match_identifier(".".join(parts[start:]), known)
        if len(matches) == 1:
            return matches[0], False
        if len(matches) > 1:
            return None, True
    return None, False


def _match_parent(
    col: ColumnProfile,
    stem: str,
    parent: Dataset,
    parent_keys: list[list[str]],
    host_counts: dict[str, int],
    suppressed: list[SuppressedMatch] | None,
    affixes: EntityAffixes | None = None,
    affix_matches: list[AffixMatch] | None = None,
) -> tuple[list[str], float] | None:
    parent_table = parent.identifier.rsplit(".", 1)[-1]
    stripped = _LAYER_PREFIX.sub("", parent_table)
    # Match the raw stripped name too, not just its singular: an already-singular
    # table like `status` would otherwise be mangled by the heuristic inflector.
    parent_entities = {stripped.lower(), _singularize(stripped).lower()}

    parent_cols = {c.name.lower(): c for c in parent.columns}
    # Single-column keys only: a composite member alone is not unique, so
    # treating it as the parent's key would inflate join confidence and invent
    # many-to-one edges toward fact tables.
    parent_key_names = {k[0].lower() for k in parent_keys if len(k) == 1}
    fk = col.name.lower()
    stem_l = stem.lower()

    # Strongest: <entity>_id / <entity>Id (or the _key equivalents) pointing at
    # the parent named <entity>, joining to the parent's id-shaped key (bare
    # `id`/`key`, `<entity>_id`, or `<entity>id`, for each suffix in
    # `_ID_SUFFIXES`).
    if stem_l in parent_entities or _singularize(stem).lower() in parent_entities:
        targets = list(_ID_SUFFIXES)
        targets += [f"{stem_l}_{suffix}" for suffix in _ID_SUFFIXES]
        targets += [f"{stem_l}{suffix}" for suffix in _ID_SUFFIXES]
        for target in targets:
            pcol = parent_cols.get(target)
            if pcol is not None and _type_compatible(col.data_type, pcol.data_type):
                base = 0.85 if target in parent_key_names else 0.5
                return [pcol.name], _score(base, col, pcol)

    # Weaker: the exact-name tier above missed because the parent's name
    # carries a house-convention affix a bare layer prefix doesn't cover (a
    # CDC history table, a landing-zone `_data`/`_raw` suffix, a versioned
    # `_v2` table, ...). Stripping the configured affixes and retrying the
    # same comparison catches these (issue #208), at a base confidence kept
    # below every tier above so an unambiguous match is never re-ranked
    # behind a guess that needed help. Tried only when `affixes` is passed,
    # so a caller that doesn't configure it pays nothing extra.
    if affixes is not None:
        affix_stripped = _strip_configured_affixes(stripped, affixes)
        if affix_stripped and affix_stripped != stripped.lower():
            affix_entities = {affix_stripped, _singularize(affix_stripped).lower()}
            if stem_l in affix_entities or _singularize(stem).lower() in affix_entities:
                targets = list(_ID_SUFFIXES)
                targets += [f"{stem_l}_{suffix}" for suffix in _ID_SUFFIXES]
                targets += [f"{stem_l}{suffix}" for suffix in _ID_SUFFIXES]
                for target in targets:
                    pcol = parent_cols.get(target)
                    if pcol is not None and _type_compatible(
                        col.data_type, pcol.data_type
                    ):
                        base = 0.5 if target in parent_key_names else 0.25
                        if affix_matches is not None:
                            affix_matches.append(
                                AffixMatch(col.name, parent.identifier, affix_stripped)
                            )
                        return [pcol.name], _score(base, col, pcol)

    # Same-named foreign key shared by both tables (e.g. customer_id in both),
    # joining to the parent's key of that name. Trusted only when the name
    # isn't itself a generic convention shared by many unrelated datasets'
    # own keys (_GENERIC_NAME_MIN_HOSTS) — otherwise every table sharing the
    # convention would cross-match every other, as CDC exports do.
    if fk in parent_cols and fk in parent_key_names:
        pcol = parent_cols[fk]
        if _type_compatible(col.data_type, pcol.data_type):
            host_count = host_counts.get(fk, 0)
            if host_count >= _GENERIC_NAME_MIN_HOSTS:
                if suppressed is not None:
                    suppressed.append(SuppressedMatch(fk, host_count))
            else:
                return [pcol.name], _score(0.6, col, pcol)

    # Same key suffix once each side's table-alias prefix is stripped: TPC-H
    # names a foreign key after the *child's* alias, not the parent's entity
    # (LINEITEM.l_orderkey -> ORDERS.o_orderkey), which the entity-name branch
    # above can't see since "l" and "o" aren't ORDERS's entity name. Skipped
    # when stripping collapses the name to a bare suffix (e.g. "x_key" -> "key"),
    # which is too generic to trust as a match.
    fk_bare = _dealias(fk)
    if fk_bare != fk and fk_bare not in _ID_SUFFIXES:
        for pname, pcol in parent_cols.items():
            if (
                pname in parent_key_names
                and _dealias(pname) == fk_bare
                and _type_compatible(col.data_type, pcol.data_type)
            ):
                return [pcol.name], _score(0.55, col, pcol)

    return None


def _score(base: float, child: ColumnProfile, parent: ColumnProfile) -> float:
    """Refine a name-derived confidence with the aggregates already profiled.

    Containment is the cheap value-overlap check: a true FK's distinct count
    cannot exceed its parent key's, and (for numerics) its range sits inside the
    parent's. Both signals come from the profile pass, so this stays free and
    metadata-only, with no extra queries.
    """

    confidence = base
    if child.distinct_count is not None and parent.distinct_count is not None:
        if child.distinct_count <= parent.distinct_count:
            confidence += 0.05
        else:
            confidence -= 0.15

    bounds = (child.min_value, child.max_value, parent.min_value, parent.max_value)
    if all(_is_number(v) for v in bounds):
        contained = (
            parent.min_value <= child.min_value and child.max_value <= parent.max_value
        )
        confidence += 0.05 if contained else -0.1

    return round(min(0.95, max(0.05, confidence)), 4)


def _is_number(value: object | None) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_compatible(a: str, b: str) -> bool:
    return _type_family(a) == _type_family(b)


def _type_family(data_type: str) -> str:
    upper = data_type.upper()
    if any(h in upper for h in ("INT", "HUGEINT", "DECIMAL", "NUMERIC")):
        return "integer"
    if any(h in upper for h in ("CHAR", "TEXT", "STRING", "VARCHAR", "UUID")):
        return "text"
    if any(h in upper for h in ("DOUBLE", "FLOAT", "REAL")):
        return "float"
    return upper


def _singularize(name: str) -> str:
    """Best-effort singular of a table name for entity matching (orders -> order).

    A heuristic, not a real inflector: it covers the common -s/-es/-ies plurals and
    deliberately leaves -ss words (address, class) untouched, since those are
    singular nouns that a naive trailing-s strip would corrupt. Irregular plurals
    (people, data) are not inverted; matching simply falls through for those.
    """

    lower = name.lower()
    if lower.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if lower.endswith(("ses", "xes", "zes", "ches", "shes")):
        return name[:-2]
    if lower.endswith("s") and not lower.endswith("ss"):
        return name[:-1]
    return name


def _distinct_of(dataset: Dataset, column: str) -> int | None:
    for col in dataset.columns:
        if col.name == column:
            return col.distinct_count
    return None
