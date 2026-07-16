"""Explore: candidate keys, grain, and declared/inferred joins.

Inference is metadata-only: it reads the profiles already gathered (names, types,
uniqueness signals) and never scans data, which keeps it free at the cost of
confidence, so every inferred join carries a confidence the agent can weigh. The
one deliberate exception is the opt-in ``--verify`` pass
(:func:`verify_relationships`), which runs one bounded, engine-authored aggregate
probe per inferred join to measure the actual key overlap. Declared joins come
from the dbt project; absent one, they are simply empty (explore is designed to
work without a dbt project).
"""

from __future__ import annotations

import re

from ..adapters.base import Adapter
from ..cache import (
    ColumnProfile,
    Dataset,
    Relationship,
    RelationshipKind,
    match_identifier,
)
from ..dbt_project import ProjectDefinitions
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


def infer_relationships(datasets: list[Dataset]) -> list[Relationship]:
    """Infer many-to-one joins from column names, type compatibility, and the
    aggregate signals already profiled (uniqueness, distinct counts, min/max).

    A parent whose key is not unique still yields a join, at reduced confidence:
    suppressing it entirely would hide a real join behind a data-quality problem
    that :func:`data_quality_notes` reports separately.
    """

    keyed = {d.identifier: candidate_keys(d) for d in datasets}
    relationships: list[Relationship] = []

    for child in datasets:
        for col in child.columns:
            stem = _fk_stem(col.name)
            if stem is None:
                continue
            for parent in datasets:
                if parent.identifier == child.identifier:
                    continue
                match = _match_parent(col, stem, parent, keyed[parent.identifier])
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


def verify_relationships(
    adapter: Adapter,
    relationships: list[Relationship],
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Measure each inferred join with one overlap probe and adjust in place.

    The probe counts non-null foreign-key values and how many have no match in
    the parent (orphans). Full containment raises confidence; a high orphan rate
    is strong evidence the name-based guess was wrong and demotes it well below
    the emission threshold rather than deleting it, so the agent still sees what
    was tried. Aggregate counts only; no key value ever leaves the engine.
    """

    for rel in relationships:
        if rel.kind is not RelationshipKind.INFERRED:
            continue
        sql = _transpile_probe(_overlap_probe_sql(rel), adapter.dialect)
        result = adapter.run_query(sql, max_rows=1, timeout_seconds=timeout_seconds)
        values = dict(zip(result.columns, result.cells[0], strict=True))
        nonnull = int(values["nonnull_fk"] or 0)
        orphans = int(values["orphans"] or 0)

        rel.verified = True
        if nonnull == 0:
            rel.orphan_fraction = None
            continue
        fraction = orphans / nonnull
        rel.orphan_fraction = round(fraction, 4)

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


def probe_statements(relationships: list[Relationship], dialect: str) -> list[str]:
    """The exact SQL :func:`verify_relationships` will run, one statement per
    inferred join, in the adapter's dialect. Exists so a billed caller can
    dry-run the probes for a cost estimate before confirming the spend."""

    return [
        _transpile_probe(_overlap_probe_sql(rel), dialect)
        for rel in relationships
        if rel.kind is RelationshipKind.INFERRED
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

    # Same-named foreign key shared by both tables (e.g. customer_id in both),
    # joining to the parent's key of that name.
    if fk in parent_cols and fk in parent_key_names:
        pcol = parent_cols[fk]
        if _type_compatible(col.data_type, pcol.data_type):
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
