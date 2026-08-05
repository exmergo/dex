"""Serialize the exploration cache as a Mermaid entity-relationship diagram.

This is a renderer, not a visualization: it serializes structure dex already
computed (objects, keys, grain, join edges, PII flags) into a text format other
tools draw. It never renders a data value into a visual encoding, which is the
line that keeps charting out of this engine. Concretely: no value from any
column reaches the output, and ``min_value`` / ``max_value`` are never read.

The whole module is a pure function of :class:`~..cache.DexCache`, so it imports
nothing but the cache types. That keeps it callable by a host holding a cache
and no warehouse connection, no dialect engine, and no credentials.

**The honesty rules are the reason this lives in the engine rather than in a
prompt.** Mermaid demands a cardinality glyph on every edge, and a
:class:`~..cache.Relationship` has no cardinality field, so a naive renderer
invents one. A diagram is trusted more readily than the JSON it came from, so an
invented crow's foot is worse here than anywhere else in the product:

* the parent side claims "exactly one" only when the cache proves the parent key
  unique **and** the edge was declared or verified with no orphans, degrades to
  "zero or one" when uniqueness is proven but nothing measured the overlap, and
  degrades again to "zero or many" when uniqueness was never established;
* declared edges are solid and inferred edges dotted, so a reader sees which
  joins were measured without reading a label;
* the label states the kind, the confidence, and the orphan fraction, so an
  unverified guess says out loud that it is one.

**Mermaid dialect.** Deliberately the conservative ``erDiagram`` subset: plain
entity names with no alias syntax, one key mark per attribute, quoted attribute
comments. Newer spellings render in the current Mermaid and fail in the one
embedded in whatever tool the reader actually has, and a diagram that does not
draw is worth less than a plainer diagram that does.

Because dex emits Mermaid and never renders it, no dependency here pins that
dialect; the compatibility contract is with a parser living in someone else's
tool. ``scripts/dump_mermaid_fixtures.py`` and ``scripts/check_mermaid_syntax.mjs``
are the standing check, running this renderer's own output through the real
Mermaid parser in CI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..cache import ColumnProfile, Dataset, DexCache, Relationship, RelationshipKind

# Past roughly this many entities a Mermaid ER diagram stops being readable and
# starts being a schema dump in agent context, which is the thing explore exists
# to avoid. The cap binds in every mode; `full` widens what is *eligible*, never
# how much is drawn. Whatever it cuts is always reported, never silently lost.
MAX_ENTITIES = 40

_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")
# Type parameterization is noise in an ERD, and the angle-bracket form is a hard
# syntax error in the attribute grammar (`STRUCT<a INT> col` fails to parse).
# Parentheses happen to be tolerated, but both are stripped: that distinction is
# an artifact of the current grammar rather than something worth depending on.
_TYPE_DETAIL = re.compile(r"[(<].*$", re.DOTALL)


@dataclass(frozen=True)
class MermaidDiagram:
    """A rendered diagram plus everything a caller needs to explain it.

    ``entities`` maps the Mermaid entity name back to the fully-qualified
    warehouse identifier it stands for. Entities are drawn under their bare object
    name for legibility, so this mapping is how a caller resolves a box back to a
    real object; it is also emitted as a comment legend inside ``mermaid`` itself
    for a reader who only ever sees the picture.

    ``notes`` names every elision. A diagram that quietly dropped half the
    warehouse looks exactly like a diagram of a small warehouse.
    """

    mermaid: str
    entities: dict[str, str] = field(default_factory=dict)
    entity_count: int = 0
    edge_count: int = 0
    elided_entity_count: int = 0
    elided_column_count: int = 0
    elided_edge_count: int = 0
    notes: list[str] = field(default_factory=list)


def render_er_mermaid(
    cache: DexCache,
    *,
    full: bool = False,
    max_entities: int = MAX_ENTITIES,
) -> MermaidDiagram:
    """Render ``cache`` as a Mermaid ``erDiagram``.

    By default this draws the part of the map worth looking at: objects that
    were profiled *and* participate in a join, carrying their key, grain, join,
    and PII columns. That is a selection rather than a truncation, and it is
    what makes the output readable on a warehouse of any size.

    ``full`` widens eligibility to unprofiled and isolated objects and to every
    column. It does not lift ``max_entities``, which always binds.
    """

    ranked = _eligible_datasets(cache, full=full)
    kept = ranked[:max_entities]
    kept_names = {d.identifier for d in kept}
    lowered = {d.identifier.lower() for d in kept}

    edges = [
        rel
        for rel in cache.relationships
        if rel.from_dataset.lower() in lowered and rel.to_dataset.lower() in lowered
    ]
    edges.sort(key=lambda r: (r.to_dataset, r.from_dataset, tuple(r.from_columns)))

    ids = _entity_ids(kept)
    join_columns = _join_columns(edges)

    lines = ["erDiagram"]
    lines.extend(_header_comments(cache, ids))

    rendered_entities: set[str] = set()
    elided_columns = 0
    attribute_blocks: list[str] = []
    for dataset in kept:
        attributes, dropped = _attributes(
            dataset, join_columns.get(dataset.identifier.lower(), set()), full=full
        )
        elided_columns += dropped
        if not attributes:
            continue
        name = ids[dataset.identifier]
        rendered_entities.add(name)
        attribute_blocks.append(f"    {name} {{")
        attribute_blocks.extend(f"        {attr}" for attr in attributes)
        attribute_blocks.append("    }")
    lines.extend(attribute_blocks)

    for rel in edges:
        parent = ids[_match_identifier(rel.to_dataset, kept_names)]
        child = ids[_match_identifier(rel.from_dataset, kept_names)]
        rendered_entities.add(parent)
        rendered_entities.add(child)
        lines.append(f"    {parent} {_edge(rel, kept)} {child} : {_edge_label(rel)}")

    notes: list[str] = []
    elided_entities = len(ranked) - len(kept)
    if elided_entities:
        notes.append(
            f"{elided_entities} eligible object(s) were not drawn: the diagram is "
            f"capped at {max_entities} entities, kept by rank. Narrow the map or "
            "read the cache directly for the rest"
        )
    if elided_columns:
        notes.append(
            f"{elided_columns} column(s) were not drawn. The default keeps grain, "
            "key, join, and PII-flagged columns; --full draws every column"
        )
    silent = [d for d in kept if ids[d.identifier] not in rendered_entities]
    if silent:
        notes.append(
            f"{len(silent)} object(s) carry neither a profile nor a join and so "
            "have no diagram content: "
            + ", ".join(sorted(d.identifier for d in silent))
        )
    dropped_edges = len(cache.relationships) - len(edges)
    if dropped_edges:
        notes.append(
            f"{dropped_edges} relationship(s) were not drawn because at least one "
            "endpoint is not an entity in this diagram"
        )

    return MermaidDiagram(
        mermaid="\n".join(lines) + "\n",
        entities={
            name: identifier
            for identifier, name in ids.items()
            if name in rendered_entities
        },
        entity_count=len(rendered_entities),
        edge_count=len(edges),
        elided_entity_count=elided_entities,
        elided_column_count=elided_columns,
        elided_edge_count=dropped_edges,
        notes=notes,
    )


# --- selection ----------------------------------------------------------------


def _eligible_datasets(cache: DexCache, *, full: bool) -> list[Dataset]:
    """The datasets worth drawing, best first.

    An empty ``columns`` list is the codebase's own test for "inventoried but
    never profiled" (see ``_compose_datasets``), so the default set is the
    intersection of profiled and connected: an unprofiled box carries no
    attributes, and an isolated one carries no edge, so together they are the
    two ways a box says nothing.

    Order is rank first and identifier second, never cache order, so the same
    cache renders byte-identically however its datasets happen to be stored.
    """

    connected = {r.from_dataset.lower() for r in cache.relationships}
    connected |= {r.to_dataset.lower() for r in cache.relationships}
    candidates = [
        d
        for d in cache.datasets
        if full or (d.columns and d.identifier.lower() in connected)
    ]
    return sorted(candidates, key=lambda d: (-(d.rank_score or 0.0), d.identifier))


def _attributes(
    dataset: Dataset, join_columns: set[str], *, full: bool
) -> tuple[list[str], int]:
    """One Mermaid attribute line per column worth drawing, plus the drop count.

    Warehouse column order is preserved rather than sorted: it is how the table
    reads in every other tool, and it is already stable in the cache.
    """

    keyed = {c.lower() for group in dataset.candidate_keys for c in group}
    keyed |= {c.lower() for group in dataset.composite_keys for c in group}
    grain = {c.lower() for c in (dataset.grain or [])}

    lines: list[str] = []
    dropped = 0
    for column in dataset.columns:
        lowered = column.name.lower()
        wanted = (
            full
            or lowered in grain
            or lowered in keyed
            or lowered in join_columns
            or column.pii is not None
            or column.is_unique is True
        )
        if not wanted:
            dropped += 1
            continue

        mark = ""
        if lowered in grain:
            mark = " PK"
        elif lowered in join_columns:
            mark = " FK"
        elif lowered in keyed or column.is_unique is True:
            mark = " UK"

        data_type = _safe(
            column.data_type or "unknown", fallback="unknown", strip_detail=True
        )
        name = _safe(column.name, fallback="column")
        comment = _comment(column)
        suffix = f' "{comment}"' if comment else ""
        lines.append(f"{data_type} {name}{mark}{suffix}")
    return lines, dropped


def _comment(column: ColumnProfile) -> str:
    """The annotation that rides along with an attribute.

    Aggregates and flags only, and no value ever: a PII flag is (category,
    confidence) by construction, and min/max are deliberately not read here even
    though the cache holds them for non-flagged columns.
    """

    parts: list[str] = []
    if column.pii is not None:
        parts.append(f"pii:{column.pii.category.value} {column.pii.confidence:.2f}")
    if column.null_fraction:
        parts.append(f"{column.null_fraction:.1%} null")
    if column.distinct_count is not None:
        # The tilde is load-bearing: distinct counts are HyperLogLog estimates
        # unless the profile escalated the column to an exact count, and a
        # sketch wobble read as a fact is how a false grain claim starts.
        prefix = "" if column.distinct_count_exact else "~"
        parts.append(f"{prefix}{column.distinct_count} distinct")
    return ", ".join(parts).replace('"', "'")


# --- edges --------------------------------------------------------------------


def _edge(rel: Relationship, kept: list[Dataset]) -> str:
    """The Mermaid cardinality glyph pair for one relationship.

    ``from_*`` is the child (FK) side and ``to_*`` the parent (key) side by
    construction, so the diagram is written parent-first. What the glyph may
    claim is bounded by what the cache proved; see the module docstring.
    """

    parent = _find(rel.to_dataset, kept)
    child = _find(rel.from_dataset, kept)

    if parent is not None and _is_proven_key(parent, rel.to_columns):
        declared = rel.kind is RelationshipKind.DECLARED
        measured_clean = rel.verified and not rel.orphan_fraction
        left = "||" if declared or measured_clean else "|o"
    else:
        # Nothing established that the parent side is unique, so "one" is not a
        # claim this cache supports. Many-to-many is the weakest thing to say.
        left = "}o"

    unique_child = child is not None and _is_proven_key(child, rel.from_columns)
    link = "--" if rel.kind is RelationshipKind.DECLARED else ".."
    return f"{left}{link}{'o|' if unique_child else 'o{'}"


def _edge_label(rel: Relationship) -> str:
    parts = [", ".join(rel.from_columns) or "join", rel.kind.value]
    if rel.kind is not RelationshipKind.DECLARED and rel.confidence is not None:
        parts[-1] = f"{rel.kind.value} {rel.confidence:.2f}"
    if rel.verified:
        if rel.orphan_fraction is None:
            parts.append("verified, no non-null keys")
        elif not rel.orphan_fraction:
            parts.append("verified, no orphans")
        else:
            parts.append(f"verified {rel.orphan_fraction:.1%} orphans")
    return '"' + ", ".join(parts).replace('"', "'") + '"'


def _is_proven_key(dataset: Dataset, columns: list[str]) -> bool:
    """Whether ``dataset`` is known to hold at most one row per ``columns``.

    Three independent proofs, in descending strength: a probe-proven composite,
    a derived candidate key, or a single column the profile measured unique.
    Absence of proof is never read as absence of uniqueness; it just means the
    diagram may not claim it.
    """

    wanted = tuple(sorted(c.lower() for c in columns))
    if not wanted:
        return False
    for group in (*dataset.composite_keys, *dataset.candidate_keys):
        if tuple(sorted(c.lower() for c in group)) == wanted:
            return True
    if len(wanted) == 1:
        return any(
            c.name.lower() == wanted[0] and c.is_unique is True for c in dataset.columns
        )
    return False


def _join_columns(edges: list[Relationship]) -> dict[str, set[str]]:
    """Columns that carry a drawn join, per dataset, for the FK mark."""

    columns: dict[str, set[str]] = {}
    for rel in edges:
        columns.setdefault(rel.from_dataset.lower(), set()).update(
            c.lower() for c in rel.from_columns
        )
        columns.setdefault(rel.to_dataset.lower(), set()).update(
            c.lower() for c in rel.to_columns
        )
    return columns


def _find(identifier: str, kept: list[Dataset]) -> Dataset | None:
    lowered = identifier.lower()
    return next((d for d in kept if d.identifier.lower() == lowered), None)


def _match_identifier(identifier: str, kept_names: set[str]) -> str:
    """The kept dataset's own spelling of ``identifier``.

    Relationship endpoints and dataset identifiers agree in every connector dex
    ships, but the cache's own edge key is case-insensitive, so resolving the
    same way keeps a case-differing backend from raising a KeyError here.
    """

    if identifier in kept_names:
        return identifier
    lowered = identifier.lower()
    return next((name for name in kept_names if name.lower() == lowered), identifier)


# --- naming -------------------------------------------------------------------


def _entity_ids(kept: list[Dataset]) -> dict[str, str]:
    """Map each fully-qualified identifier to a legible, unique Mermaid name.

    The bare object name is used because legibility is the whole point of a
    diagram: a wall of boxes labelled ``project.dataset.table`` is harder to read
    than the schema listing it replaces, and the comment legend keeps the full
    identifier available. Collisions widen one namespace segment at a time, since
    two schemas each holding a ``customers`` must not merge into one box.

    Iteration follows the caller's already-sorted order, so the names a given
    cache produces are stable across runs.
    """

    ids: dict[str, str] = {}
    taken: set[str] = set()
    for dataset in kept:
        segments = dataset.identifier.split(".")
        name = ""
        for depth in range(1, len(segments) + 1):
            name = _safe("_".join(segments[-depth:]), fallback="object")
            if name not in taken:
                break
        while name in taken:
            name = f"{name}_"
        taken.add(name)
        ids[dataset.identifier] = name
    return ids


def _safe(raw: str, *, fallback: str, strip_detail: bool = False) -> str:
    """One identifier-safe Mermaid token.

    This is what makes an arbitrary warehouse name safe to emit, and each rule
    answers a verified parse failure rather than a guess: a leading digit
    (``2fa_code``), an embedded quote, stray punctuation (``weird-name!``), and a
    space inside a type name (``TIMESTAMP WITH TIME ZONE``) each break the
    attribute grammar. Whitespace becomes an underscore rather than being dropped,
    so a multi-word type stays readable.

    ``strip_detail`` additionally drops type parameterization; see
    :data:`_TYPE_DETAIL`.
    """

    text = _TYPE_DETAIL.sub("", raw) if strip_detail else raw
    text = _UNSAFE.sub("_", text.strip()).strip("_")
    if not text:
        return fallback
    return f"_{text}" if text[0].isdigit() else text


def _header_comments(cache: DexCache, ids: dict[str, str]) -> list[str]:
    """The legend, and the provenance that says how old this picture is.

    A diagram rendered from a cache is a snapshot of whenever that cache was
    written, and it will be pasted somewhere that carries no other context, so
    the staleness has to travel inside the artifact itself.
    """

    provenance = cache.provenance
    stamp = provenance.updated_at or provenance.created_at or "unknown"
    lines = [
        f"    %% dex exploration map, connector: {provenance.connector or 'unknown'}, "
        f"cached: {stamp}",
        "    %% solid lines are relationships declared in the dbt project; "
        "dotted lines are inferred",
        "    %% a cardinality is drawn only where the cache proved it; "
        "}o..o{ means unproven",
    ]
    lines.extend(
        f"    %% {name} = {identifier}"
        for identifier, name in ids.items()
        if name != identifier
    )
    return lines
