"""What a project declares, in terms no format owns.

Tier 1 of the project seam returns :class:`ProjectDefinitions`, and every format
that reaches that tier returns this same type. So it lives here rather than
beside the reader that happened to define it first: `dbt_project` imports the
dbt semantic reader and the MetricFlow dialect map at module scope, and a second
format returning its declarations from there would import dbt's reader to state
that it declares a unique key. That is not a hypothetical tidiness argument. The
rule the Ossie format is held to is that neither format imports the other's
reader, and an import that arrives transitively breaks it exactly as thoroughly
as one written by hand.

A leaf module, in the idiom :mod:`exmergo_dex_core.semantic_catalog` already
sets for the other neutral read model: pydantic and nothing else, so it stays
importable in a base install and cannot grow a dependency on any one format's
world. The names are re-exported from `dbt_project`, where they have been public
since v1.

**The vocabulary is the warehouse's, not any format's.** ``model`` is the
referable name the format uses for a relation, ``relation`` is the physical name
where the format could resolve one, and ``source`` records which channel of the
format stated it. A format with no equivalent of a field leaves it unset rather
than inventing one, because an absent declaration and a guessed one read the
same downstream and only one of them is true.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "DeclaredColumnMeaning",
    "DeclaredCompositeKey",
    "DeclaredForeignKey",
    "DeclaredHistory",
    "DeclaredHistoryMeasures",
    "DeclaredHistoryStatus",
    "DeclaredHistoryTime",
    "DeclaredKey",
    "DeclaredPopulation",
    "DeclaredPopulationFilter",
    "DeclaredRelationship",
    "ModelDeclaration",
    "ProjectDefinitions",
    "read_history",
    "read_population",
]


class DeclaredForeignKey(BaseModel):
    """One declared join: child column to parent column.

    ``relation`` / ``to_relation`` carry quote-stripped physical names when the
    format resolves them; a format that resolves by name alone leaves them None,
    and downstream resolution is name-based.
    """

    model: str
    relation: str | None = None
    column: str
    to_model: str
    to_relation: str | None = None
    to_column: str
    source: str


class DeclaredRelationship(BaseModel):
    """A declared physical relationship with every ordered key pair intact.

    Unlike :class:`DeclaredForeignKey`, this can state a composite join without
    pretending one member is independently a foreign key.
    """

    model: str
    relation: str | None = None
    to_model: str
    to_relation: str | None = None
    column_pairs: list[tuple[str, str]]
    source: str
    name: str | None = None


class DeclaredKey(BaseModel):
    """A column declared unique and/or not null on one model.

    ``primary`` says the column is the model's row identity, a native layer's
    ``primary_key``, as opposed to one more column that happens to be unique (a
    dbt ``unique`` test, an Ossie ``unique_keys`` entry). Only the first is a
    grain claim (#976).
    """

    model: str
    relation: str | None = None
    column: str
    unique: bool = False
    not_null: bool = False
    primary: bool = False
    source: str


class DeclaredCompositeKey(BaseModel):
    """A declared grain over several columns: the columns whose COMBINATION is
    unique, never any one of them alone.

    A distinct model from ``DeclaredKey`` rather than a widened ``column``:
    this declaration has no ``not_null`` variant and a different multiplicity
    (it is the model's own claim about several columns together, not one
    column's own test), so overloading ``column`` to sometimes hold a list would
    blur two different concepts into one field.

    ``claim`` says what the combination is (#976): ``primary`` is a native
    layer's ``primary_key``, the row identity; ``combination`` is a model-level
    test (dbt's ``unique_combination_of_columns``), which exploration takes as
    the grain unless measurement proved a single column; and ``unique`` is one
    further key (an Ossie ``unique_keys`` entry), which is measured for drift
    and never reported as the grain.
    """

    model: str
    relation: str | None = None
    columns: list[str]
    claim: Literal["primary", "combination", "unique"] = "combination"
    source: str


class DeclaredColumnMeaning(BaseModel):
    """What one column of a model is, in the model's own declaration (#491).

    The fields exploration reads back (#498): what part the column plays, the
    unit its values are in, how it adds up, and the ``<model>.<column>`` it
    references. Every one is optional because a declaration may be partial, and
    an unstated field stays unset rather than defaulted.
    """

    role: str | None = None
    unit: str | None = None
    additivity: str | None = None
    references: str | None = None


class DeclaredPopulationFilter(BaseModel):
    """One filter of a declared population: the values a column keeps or drops."""

    column: str
    includes: list[str | int | float | bool] | None = None
    excludes: list[str | int | float | bool] | None = None


class DeclaredPopulationParent(BaseModel):
    """The entity a model's rows are drawn from (#857): its parent and the
    output-column-to-parent-column key that identifies one row per parent."""

    model: str
    key: dict[str, str]


class DeclaredPopulation(BaseModel):
    """Which rows a model keeps: a sentence, structured filters, the parent its
    rows are drawn from, or any of them together."""

    rule: str | None = None
    filters: list[DeclaredPopulationFilter] = Field(default_factory=list)
    parent: DeclaredPopulationParent | None = None


class DeclaredHistoryStatus(BaseModel):
    """The status column and target value a history declaration tracks (#1039)."""

    column: str
    target: str | int | float | bool | None = None


class DeclaredHistoryTime(BaseModel):
    """The business or capture timestamp column of a history declaration (#1039)."""

    column: str
    provenance: Literal["business", "capture"] | None = None


class DeclaredHistoryMeasures(BaseModel):
    """The output measure columns a history declaration names (#1039)."""

    transition_count: str | None = None
    latest_target_transition_at: str | None = None
    first_target_at: str | None = None


class DeclaredHistory(BaseModel):
    """A model's history declaration as the project states it (#1039)."""

    snapshot: str
    entity: dict[str, str]
    status: DeclaredHistoryStatus
    initial_observation: Literal["baseline", "transition"] | None = None
    null_status: Literal["ignore", "state"] | None = None
    population: Literal["ever_target", "all_observed"] | None = None
    time: DeclaredHistoryTime | None = None
    measures: DeclaredHistoryMeasures = Field(default_factory=DeclaredHistoryMeasures)


def read_population_parent(raw: Any) -> DeclaredPopulationParent | None:
    """A declared ``population.parent`` from repository text, or None when it is
    absent or not shaped as ``{model: <name>, key: {<output>: <parent>}}``."""

    if not isinstance(raw, dict):
        return None
    model, key = raw.get("model"), raw.get("key")
    if not (isinstance(model, str) and model.strip() and isinstance(key, dict)):
        return None
    pairs = {
        k.strip(): v.strip()
        for k, v in key.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }
    if not pairs or len(pairs) != len(key):
        return None
    return DeclaredPopulationParent(model=model.strip(), key=pairs)


def read_population(raw: Any) -> DeclaredPopulation | None:
    """A declared population from repository text, or None.

    Lenient by design: this reads what a human or a tool wrote, so a field of the
    wrong shape is left out rather than raised on. Every format's declaration
    reader shares it, which is why it lives in this leaf.
    """

    if not isinstance(raw, dict):
        return None
    rule = raw.get("rule") if isinstance(raw.get("rule"), str) else None
    filters: list[DeclaredPopulationFilter] = []
    entries = raw.get("filters")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("column"), str):
            continue
        includes = _scalar_values(entry.get("includes"))
        excludes = _scalar_values(entry.get("excludes"))
        if includes or excludes:
            filters.append(
                DeclaredPopulationFilter(
                    column=entry["column"], includes=includes, excludes=excludes
                )
            )
    parent = read_population_parent(raw.get("parent"))
    if not (rule and rule.strip()) and not filters and parent is None:
        return None
    return DeclaredPopulation(
        rule=rule if rule and rule.strip() else None, filters=filters, parent=parent
    )


def _scalar_values(raw: Any) -> list[str | int | float | bool] | None:
    if not isinstance(raw, list):
        return None
    kept = [value for value in raw if isinstance(value, str | int | float | bool)]
    return kept or None


def read_history(raw: Any) -> DeclaredHistory | None:
    """A declared history contract from repository text, or None.

    Lenient by design: this reads what a human or a tool wrote, so an absent or
    malformed declaration is left out rather than raised on.
    """
    if not isinstance(raw, dict):
        return None
    snapshot = raw.get("snapshot")
    if not isinstance(snapshot, str) or not snapshot.strip():
        return None

    raw_entity = raw.get("entity")
    if not isinstance(raw_entity, dict):
        return None
    entity = {
        k.strip(): v.strip()
        for k, v in raw_entity.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }
    if not entity or len(entity) != len(raw_entity):
        return None

    raw_status = raw.get("status")
    if not isinstance(raw_status, dict):
        return None
    status_column = raw_status.get("column")
    if not isinstance(status_column, str) or not status_column.strip():
        return None
    target = raw_status.get("target")
    if not isinstance(target, str | int | float | bool):
        return None
    if isinstance(target, str):
        if not target.strip():
            return None
        target = target.strip()
    status = DeclaredHistoryStatus(column=status_column.strip(), target=target)

    raw_init = raw.get("initial_observation")
    initial_observation = raw_init if raw_init in ("baseline", "transition") else None

    raw_null = raw.get("null_status")
    null_status = raw_null if raw_null in ("ignore", "state") else None

    raw_pop = raw.get("population")
    history_population = raw_pop if raw_pop in ("ever_target", "all_observed") else None

    raw_time = raw.get("time")
    time_decl: DeclaredHistoryTime | None = None
    if isinstance(raw_time, dict):
        time_column = raw_time.get("column")
        if isinstance(time_column, str) and time_column.strip():
            raw_prov = raw_time.get("provenance")
            provenance = raw_prov if raw_prov in ("business", "capture") else None
            time_decl = DeclaredHistoryTime(
                column=time_column.strip(), provenance=provenance
            )

    raw_measures = raw.get("measures")
    measures_decl = DeclaredHistoryMeasures()
    if isinstance(raw_measures, dict):
        tc = raw_measures.get("transition_count")
        lt = raw_measures.get("latest_target_transition_at")
        ft = raw_measures.get("first_target_at")
        measures_decl = DeclaredHistoryMeasures(
            transition_count=(
                tc.strip() if isinstance(tc, str) and tc.strip() else None
            ),
            latest_target_transition_at=(
                lt.strip() if isinstance(lt, str) and lt.strip() else None
            ),
            first_target_at=(
                ft.strip() if isinstance(ft, str) and ft.strip() else None
            ),
        )

    return DeclaredHistory(
        snapshot=snapshot.strip(),
        entity=entity,
        status=status,
        initial_observation=initial_observation,
        null_status=null_status,
        population=history_population,
        time=time_decl,
        measures=measures_decl,
    )


class ModelDeclaration(BaseModel):
    """A model's declaration as the project states it (#491), read back (#498).

    The model's own statement of what one row is (``grain``), which rows are in
    (``population``), what each column means (``columns``) and history contract
    (``history``). ``source`` is the channel it was read from, as on every other
    declared object here. A format with no declarations leaves
    :attr:`ProjectDefinitions.model_declarations` empty rather than inventing one.
    """

    model: str
    relation: str | None = None
    grain: list[str] | None = None
    population: DeclaredPopulation | None = None
    columns: dict[str, DeclaredColumnMeaning] = Field(default_factory=dict)
    history: DeclaredHistory | None = None
    source: str


class ProjectDefinitions(BaseModel):
    """What the project declares, loaded once for consumers that must keep
    working without one.

    ``present`` False means no readable project: every collection is empty and
    consumers degrade instead of erroring. ``relationship_source`` and
    ``semantic_source`` record where each half came from (for dbt, ``"manifest"``
    is exact and ``"yaml"`` resolves by name; another format names its own
    channels). ``model_relations`` maps each model the project builds to its
    quote-stripped physical relation, keyed by the model's label (for dbt, the
    model name, and ``<name>.v<N>`` for a versioned model, one entry per
    version); every key is a model, so a consumer never has to tell a model
    from a source by its spelling. ``source_relations`` maps what the project
    reads but does not build (for dbt, ``source.table``) the same way.
    ``primary_entities``
    maps model names to their declared grain column; ``metric_models`` lists
    models reachable from any metric. ``declared_composite_keys`` carries
    multi-column grain declarations -- something a column-level test structurally
    cannot express. ``declared_grains`` maps a model label to the grain its
    declaration states (for dbt, ``config.meta.dex.grain``, #491): one column or
    several, the model's own statement of what one row is rather than a test's,
    and the first source `maintain verify` checks a grain from (#496).
    ``model_declarations`` maps a model label to its whole declaration
    (:class:`ModelDeclaration`), which `explore --use-project` reads back (#498).
    ``built_relation_names`` is bare table names (lowered) the
    project builds or sources, from files alone (populated even with no compiled
    artifact, unlike ``model_relations``) -- explore's orphan-relation
    down-ranking reads this, so a format that declares relations without building
    them leaves it empty and says so in a note. ``notes`` are analyst-readable
    caveats for the caller's envelope, and the channel a tier-1 read uses to say
    what it could not do, since the tier may not raise.
    """

    present: bool = False
    project_dir: str | None = None
    manifest_loaded: bool = False
    manifest_stale: bool = False
    relationship_source: str | None = None
    semantic_source: str | None = None
    foreign_keys: list[DeclaredForeignKey] = Field(default_factory=list)
    declared_relationships: list[DeclaredRelationship] = Field(default_factory=list)
    declared_keys: list[DeclaredKey] = Field(default_factory=list)
    declared_composite_keys: list[DeclaredCompositeKey] = Field(default_factory=list)
    model_relations: dict[str, str] = Field(default_factory=dict)
    #: Each versioned model's bare name -> the label of the version an
    #: unversioned ``ref()`` resolves to (its ``latest_version``), so a reader
    #: that holds a bare name resolves it the way dbt does (#895).
    version_defaults: dict[str, str] = Field(default_factory=dict)
    source_relations: dict[str, str] = Field(default_factory=dict)
    primary_entities: dict[str, str] = Field(default_factory=dict)
    declared_grains: dict[str, list[str]] = Field(default_factory=dict)
    model_declarations: dict[str, ModelDeclaration] = Field(default_factory=dict)
    #: What a native layer's ``dex`` metadata held that could not be read (#977).
    declaration_notes: list[str] = Field(default_factory=list)
    metric_models: list[str] = Field(default_factory=list)
    built_relation_names: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
