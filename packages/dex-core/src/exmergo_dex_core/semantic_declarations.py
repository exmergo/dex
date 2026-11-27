"""How a model declaration reads inside any derived semantic definition (#927).

Shared by every vendor's derivation, so a population reads the same in a
MetricFlow semantic model, a SLayer model, and whatever implements the hook
next. A leaf: pydantic and dex's errors, nothing else, so a semantic layer's own
authoring route (which may run where neither dbt nor sqlglot is installed) can
read a declaration. The declaration model itself lives here for that reason and
is re-exported by ``transform.declarations``, where the dbt route renders it.

**A population is metadata, never a filter.** A derived definition sits on the
declared model itself, whose SQL already enforces the population, so a filter
would apply the rule twice and silently diverge from the model the day the
declared rule and the SQL disagree, which is what declaration contradictions
exist to catch. It is carried twice over instead: structured, under the
vendor's metadata key ``dex`` (where a reviewer or a tool reads it exactly), and
as one sentence at the end of the definition's description (where an agent
reviewing the file meets it). Native catalogs omit that generated sentence: population
values belong to the separately screened declaration surface.

**Every declared field is accounted for.** :func:`declared_fields` lists each
field the declaration states, as a path (``population``,
``columns.amount.unit``), and a derivation reports each as carried or not,
with why, so nothing a declaration says vanishes on the way into a layer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import DexError
from .project_definitions import (
    DeclaredColumnMeaning,
    ModelDeclaration,
    read_population,
)

# --- the declaration model (#491) ----------------------------------------------------


class DeclarationError(DexError, ValueError):
    """A declaration dex refuses, with the fix named in the message.

    A ``ValueError`` as well, so it reaches the same request-error envelope every
    other malformed payload does on the commands that already catch that.
    """


class Role(str, Enum):
    KEY = "key"
    FOREIGN_KEY = "foreign_key"
    DIMENSION = "dimension"
    TIME = "time"
    MEASURE = "measure"
    ATTRIBUTE = "attribute"


class Additivity(str, Enum):
    ADDITIVE = "additive"
    # Does not add across time: a balance, a running or season-to-date total.
    SEMI_ADDITIVE = "semi_additive"
    # Does not add at all: a ratio, a share, a distinct count.
    NON_ADDITIVE = "non_additive"


class Evidence(str, Enum):
    """Where a decision came from, strongest first."""

    REQUEST = "request"
    CONTRACT = "contract"
    PRECEDENT = "precedent"
    DATA = "data"
    DEFAULT = "default"


class _Strict(BaseModel):
    # An unknown key is a typo far more often than a deliberate extension, and a
    # typo silently dropped is a declaration that says less than its author meant.
    model_config = ConfigDict(extra="forbid")


class PopulationFilter(_Strict):
    column: str
    includes: list[str | int | float | bool] | None = None
    excludes: list[str | int | float | bool] | None = None


class PopulationParent(_Strict):
    """The entity a model's rows are drawn from (#857): one output row per
    parent key, so a parent key absent from the output is a missing entity and
    an output key absent from the parent is an unexpected one.

    ``key`` maps each output column to the parent column it carries, in the
    order the composite key is written. A parent the model only joins to is a
    column's ``references``, not this.
    """

    model: str
    key: dict[str, str]

    @field_validator("model")
    @classmethod
    def _model_named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name the parent model, seed or snapshot")
        return value.strip()

    @field_validator("key")
    @classmethod
    def _key_pairs(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError(
                "map at least one output column to the parent column it carries, "
                "as {<output column>: <parent column>}"
            )
        if any(not k.strip() or not v.strip() for k, v in value.items()):
            raise ValueError("a key column name is empty")
        return {k.strip(): v.strip() for k, v in value.items()}


class Population(_Strict):
    rule: str | None = None
    filters: list[PopulationFilter] = []
    parent: PopulationParent | None = None


class InitialObservation(str, Enum):
    # The first observation establishes where an entity starts; nothing changed.
    BASELINE = "baseline"
    # A first observation at the target counts as a transition into it. This
    # assumes a prior non-target state, so it applies only when selected.
    TRANSITION = "transition"


class NullStatus(str, Enum):
    # A version with a NULL status is not an observation of the status.
    IGNORE = "ignore"
    # NULL is a state of its own, so moving into or out of it is a change.
    STATE = "state"


class HistoryPopulation(str, Enum):
    EVER_TARGET = "ever_target"
    ALL_OBSERVED = "all_observed"


class TimeProvenance(str, Enum):
    # A timestamp the source system recorded for the business event.
    BUSINESS = "business"
    # The time the snapshot observed the change.
    CAPTURE = "capture"


class HistoryStatus(_Strict):
    column: str
    target: str | int | float | bool


class HistoryTime(_Strict):
    column: str
    provenance: TimeProvenance | None = None


class HistoryMeasures(_Strict):
    """The model's output columns the declared policy gives an expected value for."""

    transition_count: str | None = None
    latest_target_transition_at: str | None = None
    first_target_at: str | None = None


class History(_Strict):
    """The temporal contract of a model derived from a snapshot's status history
    (#1027). One output row per entity, against versions the snapshot recorded.

    Only ``snapshot``, ``entity`` and ``status`` are required. A policy left out
    is intent still owed: planning warns and verification reports what depends
    on it as unchecked, because no policy is ever assumed on the author's behalf.
    """

    snapshot: str
    #: {<output column>: <snapshot column>}, in the order the key is written.
    entity: dict[str, str]
    status: HistoryStatus
    initial_observation: InitialObservation | None = None
    null_status: NullStatus | None = None
    population: HistoryPopulation | None = None
    time: HistoryTime | None = None
    measures: HistoryMeasures = HistoryMeasures()

    @field_validator("snapshot")
    @classmethod
    def _snapshot_named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name the snapshot the history is read from")
        return value.strip()

    @field_validator("entity")
    @classmethod
    def _entity_pairs(cls, value: dict[str, str]) -> dict[str, str]:
        if not value or any(not k.strip() or not v.strip() for k, v in value.items()):
            raise ValueError(
                "map each output key column to the snapshot column it carries, as "
                "{<output column>: <snapshot column>}"
            )
        return {k.strip(): v.strip() for k, v in value.items()}


#: The units a freshness ``max_lag`` may be written in, with their seconds,
#: largest first so a normalized lag names the largest unit that divides it.
LAG_UNITS: tuple[tuple[str, int], ...] = (
    ("w", 604800),
    ("d", 86400),
    ("h", 3600),
    ("m", 60),
    ("s", 1),
)
_LAG = re.compile(r"^\s*([0-9]+)\s*([a-zA-Z]+)\s*$")


def lag_seconds(value: str) -> int:
    """The seconds a ``max_lag`` such as ``6h`` states. Refuses anything but a
    positive whole number followed by one of :data:`LAG_UNITS`."""

    match = _LAG.match(value) if isinstance(value, str) else None
    units = dict(LAG_UNITS)
    if match is None or match.group(2).lower() not in units:
        raise ValueError(
            f"max_lag '{value}' is not a lag; write a whole number and a unit, "
            "s, m, h, d or w (for example 30m, 6h or 2d)"
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError(f"max_lag '{value}' must be longer than zero")
    return amount * units[match.group(2).lower()]


def normalized_lag(seconds: int) -> str:
    """``seconds`` in the largest unit that divides it: ``21600`` is ``6h``."""

    for unit, size in LAG_UNITS:
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"  # pragma: no cover - a second divides every whole number


#: The days a freshness ``deadline`` may be limited to, Monday first, by the
#: three-letter spelling a declaration stores, with the full English name
#: Elementary's ``day_of_week`` takes (#1486).
WEEKDAYS: tuple[tuple[str, str], ...] = (
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
)
_DEADLINE = re.compile(r"^\s*([0-9]{1,2}):([0-9]{2})\s*$")


def deadline_parts(value: str) -> tuple[int, int]:
    """The hour and minute a ``deadline`` such as ``07:00`` states. Refuses
    anything but a 24-hour ``H:MM`` or ``HH:MM`` time of day."""

    match = _DEADLINE.match(value) if isinstance(value, str) else None
    if match is None or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError(
            f"deadline '{value}' is not a time of day; write it as 24-hour HH:MM "
            "(for example '07:00' or '14:30')"
        )
    return int(match.group(1)), int(match.group(2))


def valid_timezone(value: str) -> str:
    """``value`` where it names an IANA time zone, else a refusal. A key under
    ``posix/`` or ``right/`` is a system file, not a zone name."""

    from zoneinfo import ZoneInfo

    name = value.strip() if isinstance(value, str) else ""
    if (
        not name
        or name.startswith(("posix/", "right/"))
        or not re.fullmatch(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*", name)
    ):
        raise ValueError(
            f"timezone '{value}' is not an IANA time zone name (for example "
            "'America/Chicago' or 'Europe/London')"
        )
    try:
        ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, or a path the OS refuses
        raise ValueError(
            f"timezone '{value}' is not an IANA time zone name (for example "
            "'America/Chicago' or 'Europe/London')"
        ) from exc
    return name


class Freshness(_Strict):
    """How old the newest value of a declared time column may be (#1460), and
    by when each day it must be current (#1486).

    ``max_lag`` is the longest the newest row may trail the warehouse's clock,
    ``basis`` whether the column is the event's own time (``business``, which a
    backfill can leave legitimately old) or when the row was loaded
    (``capture``, which nothing excuses). The lag is normalized to the largest
    unit that divides it, so ``360m`` and ``6h`` are one declaration.

    ``deadline`` is a time of day (``07:00``) in ``timezone`` by which the
    column must hold a value from that day, on every day or only on ``days``.
    A lag is not a deadline: "no more than six hours behind" is not "updated
    by 07:00", so neither is read as the other. A block states a lag, a
    deadline, or both; ``timezone`` comes with a deadline and never without."""

    max_lag: str | None = None
    basis: TimeProvenance
    deadline: str | None = None
    timezone: str | None = None
    days: list[str] | None = None

    @field_validator("max_lag", mode="before")
    @classmethod
    def _lag(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                "max_lag is a whole number and a unit, s, m, h, d or w, written as "
                "text (for example '6h')"
            )
        return normalized_lag(lag_seconds(value))

    @field_validator("deadline", mode="before")
    @classmethod
    def _deadline(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            # YAML 1.1 reads an unquoted 07:00 as the number 420.
            raise ValueError(
                "deadline is a 24-hour time of day written as quoted text "
                "(for example '07:00'); unquoted, YAML reads 07:00 as a number"
            )
        hour, minute = deadline_parts(value)
        return f"{hour:02d}:{minute:02d}"

    @field_validator("timezone", mode="before")
    @classmethod
    def _timezone(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                "timezone is an IANA time zone name written as text (for example "
                "'America/Chicago')"
            )
        return valid_timezone(value)

    @field_validator("days", mode="before")
    @classmethod
    def _days(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str) or not isinstance(value, list) or not value:
            raise ValueError(
                "days is a list of weekdays, such as [mon, tue, wed, thu, fri]"
            )
        spelled = {short: short for short, _ in WEEKDAYS}
        spelled.update({full.lower(): short for short, full in WEEKDAYS})
        given: set[str] = set()
        for day in value:
            key = day.strip().lower() if isinstance(day, str) else None
            if key not in spelled:
                raise ValueError(
                    f"days holds '{day}', which is not a weekday; write mon, tue, "
                    "wed, thu, fri, sat or sun (or the full name)"
                )
            given.add(spelled[key])
        return [short for short, _ in WEEKDAYS if short in given]

    @model_validator(mode="after")
    def _a_lag_or_a_deadline(self) -> Freshness:
        if self.max_lag is None and self.deadline is None:
            raise ValueError(
                "freshness states a max_lag, a deadline, or both (for example "
                "{max_lag: '6h', basis: capture} or {deadline: '07:00', timezone: "
                "'America/Chicago', basis: capture})"
            )
        if self.deadline is not None and self.timezone is None:
            raise ValueError(
                "freshness.timezone is required with freshness.deadline: a time of "
                "day means nothing without the zone it is read in (for example "
                "timezone: 'America/Chicago')"
            )
        if self.deadline is None and self.timezone is not None:
            raise ValueError(
                "freshness.timezone is only read with freshness.deadline; declare "
                "the deadline, or drop the timezone"
            )
        if self.deadline is None and self.days is not None:
            raise ValueError(
                "freshness.days is only read with freshness.deadline; declare the "
                "deadline, or drop the days"
            )
        return self

    @property
    def max_lag_seconds(self) -> int | None:
        return lag_seconds(self.max_lag) if self.max_lag is not None else None

    @property
    def day_names(self) -> list[str] | None:
        """``days`` as the full English names Elementary's ``day_of_week``
        takes, or ``None`` where every day is checked."""

        if self.days is None:
            return None
        full = dict(WEEKDAYS)
        return [full[day] for day in self.days]


class ContinuityGrain(str, Enum):
    """The bucket a declared continuity searches for gaps in (#1461)."""

    SECOND = "second"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


#: The grains finer than a day, which a date column cannot be bucketed at.
SUB_DAY_GRAINS: frozenset[str] = frozenset({"second", "minute", "hour"})


class Continuity(_Strict):
    """That a declared time column has no gap (#1461): no bucket at ``grain``
    missing between two buckets holding rows, for the whole model or, with
    ``by``, for each value of those columns. A gap at the edge of the data is
    no gap, so the answer never depends on today's date (that is freshness's
    question). ``week`` is the ISO week, starting Monday; every bucket is read
    in UTC."""

    grain: ContinuityGrain = ContinuityGrain.DAY
    #: The key columns gaps are searched per value of, in the order written.
    by: list[str] = []

    @field_validator("by", mode="before")
    @classmethod
    def _keys(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError("by is a list of column names, for example [region]")
        keys = [v.strip() for v in value]
        if any(not key for key in keys):
            raise ValueError("by names a column with an empty name")
        seen: set[str] = set()
        for key in keys:
            if key.lower() in seen:
                raise ValueError(f"by names '{key}' twice")
            seen.add(key.lower())
        return keys


class ColumnDeclaration(_Strict):
    role: Role | None = None
    description: str | None = None
    references: str | None = None
    # `never`, or a sentence saying what NULL means in this column.
    null_rule: str | None = None
    aggregation: str | None = None
    additivity: Additivity | None = None
    # `currency:<ISO code>`, `fraction`, `percent`, `count`, or a plain unit.
    unit: str | None = None
    # How old the newest value may be, on a `time` column only (#1460).
    freshness: Freshness | None = None
    # That the column has no gap at a grain, per key (#1461), on `time` only.
    continuity: Continuity | None = None


class Assumption(_Strict):
    decision: str | None = None
    chosen: str | None = None
    evidence: Evidence | None = None
    detail: str | None = None
    # The column the decision governs (#861), output or source. Optional, and
    # the link a diagnostic reads to tell a recorded choice (excluding a second
    # identifier scheme, merging `7` and `007`) from one nobody made.
    column: str | None = None

    @field_validator("column")
    @classmethod
    def _column_named(cls, value: str | None) -> str | None:
        """Strip the governed column's name, refusing an empty one rather than reading
        it as absent.
        """
        if value is None:
            return None
        if not value.strip():
            raise ValueError(
                "`column` names the column this decision governs; leave it out "
                "rather than empty"
            )
        return value.strip()


class Declaration(_Strict):
    model: str
    description: str | None = None
    grain: list[str] | None = None
    population: Population | None = None
    columns: dict[str, ColumnDeclaration] = {}
    assumptions: list[Assumption] = []
    history: History | None = None


# --- reading the payload ------------------------------------------------------


def parse_declarations(entries: Any) -> list[Declaration]:
    """The payload's ``declarations`` list, validated, or a refusal naming the fix."""

    if entries is None:
        return []
    if not isinstance(entries, list):
        raise DeclarationError(
            'declarations must be a list: {"declarations": [{"model": ...}, ...]}'
        )
    parsed: list[Declaration] = []
    for index, entry in enumerate(entries):
        try:
            parsed.append(Declaration.model_validate(entry))
        except ValidationError as exc:
            raise DeclarationError(_validation_message(index, entry, exc)) from exc
    seen: set[str] = set()
    for declaration in parsed:
        if declaration.model in seen:
            raise DeclarationError(
                f"'{declaration.model}' is declared twice in this payload; merge the "
                "two into one declaration"
            )
        seen.add(declaration.model)
    return parsed


def _validation_message(index: int, entry: Any, exc: ValidationError) -> str:
    model = entry.get("model") if isinstance(entry, dict) else None
    where = f"declarations[{index}]" + (f" ('{model}')" if model else "")
    problems = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        if error["type"] == "extra_forbidden":
            problems.append(f"{loc} is not a declaration field")
        elif error["type"] == "missing":
            problems.append(f"{loc} is required")
        else:
            problems.append(f"{loc}: {error['msg']}")
    return f"{where} is malformed: " + "; ".join(problems)


def declared_shape(declaration: Declaration) -> dict[str, Any]:
    """A payload declaration in the shape ``drafting.read_declared`` reads back
    from a model's YAML, which is the shape every derivation takes."""

    out: dict[str, Any] = {}
    if declaration.description:
        out["description"] = declaration.description
    if declaration.grain:
        out["grain"] = list(declaration.grain)
    if declaration.population is not None:
        out["population"] = declaration.population.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )
    if declaration.assumptions:
        out["assumptions"] = [
            a.model_dump(mode="json", exclude_none=True)
            for a in declaration.assumptions
        ]
    if declaration.history is not None:
        out["history"] = declaration.history.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )
    columns = {
        name: column.model_dump(mode="json", exclude_none=True)
        for name, column in declaration.columns.items()
    }
    columns = {name: fields for name, fields in columns.items() if fields}
    if columns:
        out["columns"] = columns
    return out


# --- rendering a declaration inside a derived definition ----------------------------

#: The key a derivation writes declared metadata under, in every vendor's
#: free-form metadata, matching the dbt model YAML's own ``config.meta.dex``.
DEX_META_KEY = "dex"

#: Model-level fields a declaration may state.
MODEL_FIELDS = ("description", "grain", "population", "assumptions", "history")

#: Column-level fields a declaration may state.
COLUMN_FIELDS = (
    "role",
    "description",
    "references",
    "null_rule",
    "aggregation",
    "additivity",
    "unit",
    "freshness",
    "continuity",
)


def population_sentence(population: Any) -> str | None:
    """One sentence stating which rows the model holds, or None.

    The rule in the author's words where there is one, then each filter as the
    values it keeps or drops. This is repository prose, not a screened result:
    native catalogs remove it and profile/map screen the structured population.
    """

    if not isinstance(population, Mapping):
        return None
    parts: list[str] = []
    rule = population.get("rule")
    if isinstance(rule, str) and rule.strip():
        parts.append(rule.strip().rstrip("."))
    for entry in population.get("filters") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("column"), str):
            continue
        column = entry["column"]
        if entry.get("includes"):
            parts.append(f"{column} is one of {_values(entry['includes'])}")
        if entry.get("excludes"):
            parts.append(f"{column} is never {_values(entry['excludes'])}")
    if not parts:
        return None
    return "Population: " + "; ".join(parts) + "."


def with_population(description: Any, population: Any) -> str | None:
    """``description`` with the population sentence after it, or either alone."""

    sentence = population_sentence(population)
    text = description.strip() if isinstance(description, str) else ""
    if sentence is None:
        return text or None
    if not text:
        return sentence
    return f"{text} {sentence}"


# Any mention of the word counts once population metadata is present: a
# suffix that drifted from the writer's exact form (another separator, none,
# a plural or a key-style spelling) still carries its values.
_POPULATION_MARKER = re.compile(r"population", re.IGNORECASE)


def catalog_description(
    description: Any, metadata: Any, notes: list[str]
) -> str | None:
    """Remove Dex's generated population prose, including from older documents.

    Catalogs are connectionless and cannot prove a population's values safe.
    Match the writer's exact suffix to preserve the authored prefix. A stale
    sentence cannot be separated reliably; withhold it rather than repeating
    values the structured declaration would hide. No file is rewritten.
    """

    if not isinstance(description, str):
        return None
    dex = metadata.get(DEX_META_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(dex, Mapping) or "population" not in dex:
        return description
    if not _POPULATION_MARKER.search(description):
        return description
    try:
        sentence = population_sentence(dex["population"])
    except (TypeError, ValueError):
        sentence = None
    if sentence and description == sentence:
        return None
    if sentence and description.endswith(" " + sentence):
        prefix = description[: -len(sentence)].rstrip()
        # An older sentence may sit ahead of the current one.
        if not _POPULATION_MARKER.search(prefix):
            return prefix or None
    notes.append(
        "a description with population metadata has a stale or unrecognized "
        "population sentence; the description is withheld"
    )
    return None


def model_metadata(declared: Mapping[str, Any]) -> dict[str, Any]:
    """The model-level facts a derived definition carries as metadata."""

    out: dict[str, Any] = {}
    for key in ("population", "assumptions"):
        if declared.get(key):
            out[key] = declared[key]
    return out


def column_metadata(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """The column-level facts a derived element carries as metadata."""

    out: dict[str, Any] = {}
    for key in ("unit", "null_rule"):
        value = (fields or {}).get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def native_column_metadata(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """The column-level facts a native layer's own metadata carries (#977).

    More than :func:`column_metadata`, because a native layer has no model YAML
    behind it: this metadata is the only place ``explore --use-project`` can read
    a column's role and its reference back from. A native construct (a time
    dimension, a saved measure, a join) renders a role and does not state it.
    """

    out = column_metadata(fields)
    for key in ("additivity", "role", "references"):
        value = (fields or {}).get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def read_native_declaration(
    model: str,
    relation: str | None,
    grain: list[str] | None,
    model_metadata_block: Any,
    columns: Iterable[tuple[str, str | None, Any]],
    *,
    source: str,
) -> tuple[ModelDeclaration | None, list[str]]:
    """A native definition's declaration as the neutral read model, with notes.

    ``grain`` is the definition's own primary key as physical columns, the native
    field being the authority, so a definition with no ``dex`` metadata still
    states what one row is. ``model_metadata_block`` is the definition's metadata
    (``meta`` or ``ai_context``), and ``columns`` is ``(member, physical column or
    None, member metadata)``.

    The metadata is untrusted repository text: anything of the wrong shape, or
    outside the declaration's vocabulary, is left out and named in a note. Never
    raised on, and a note never repeats the value, since the value is what is not
    trusted.
    """

    notes: list[str] = []
    dex = _dex_block(model_metadata_block, model, notes)
    if "grain" in dex:
        notes.append(
            f"{model}: the `grain` in its dex metadata is ignored; the definition's "
            "own primary key states the grain"
        )
    population = _read_native_population(dex.get("population"), model, notes)

    meanings: dict[str, DeclaredColumnMeaning] = {}
    for member, column, metadata in columns:
        where = f"{model}.{member}"
        stated = _read_native_meaning(_dex_block(metadata, where, notes), where, notes)
        if not stated:
            continue
        if column is None:
            notes.append(
                f"{where}: links no physical column, so its dex metadata is not "
                "reported"
            )
            continue
        meanings[column] = DeclaredColumnMeaning(**stated)

    if not (grain or population or meanings):
        return None, notes
    return (
        ModelDeclaration(
            model=model,
            relation=relation,
            grain=list(grain) if grain else None,
            population=population,
            columns=meanings,
            source=source,
        ),
        notes,
    )


def _dex_block(metadata: Any, where: str, notes: list[str]) -> Mapping[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    dex = metadata.get(DEX_META_KEY)
    if dex is None:
        return {}
    if not isinstance(dex, Mapping):
        notes.append(f"{where}: its dex metadata is not a mapping, so it is not read")
        return {}
    return dex


def _read_native_population(raw: Any, where: str, notes: list[str]) -> Any:
    if raw is None:
        return None
    population = read_population(raw)
    stated = raw.get("filters") if isinstance(raw, dict) else None
    read = len(population.filters) if population is not None else 0
    stated_parent = raw.get("parent") if isinstance(raw, dict) else None
    malformed = (
        population is None
        or (isinstance(stated, list) and read < len(stated))
        or (bool(stated) and not isinstance(stated, list))
        or (stated_parent is not None and population.parent is None)
    )
    if malformed:
        notes.append(
            f"{where}: part of the population in its dex metadata is malformed and "
            "is not reported"
        )
    return population


def _read_native_meaning(
    dex: Mapping[str, Any], where: str, notes: list[str]
) -> dict[str, str]:
    allowed = {
        "role": {item.value for item in Role},
        "additivity": {item.value for item in Additivity},
    }
    stated: dict[str, str] = {}
    for key in ("role", "unit", "additivity", "references"):
        value = dex.get(key)
        if value is None:
            continue
        text = value.strip() if isinstance(value, str) else ""
        if not text or (key in allowed and text not in allowed[key]):
            notes.append(
                f"{where}: the `{key}` in its dex metadata is malformed and is not "
                "reported"
            )
            continue
        stated[key] = text
    return stated


def declared_fields(declared: Mapping[str, Any]) -> list[str]:
    """Every field the declaration states, as a path, in declaration order."""

    out = [key for key in MODEL_FIELDS if declared.get(key)]
    for name, fields in (declared.get("columns") or {}).items():
        if not isinstance(fields, Mapping):
            continue
        out.extend(
            f"columns.{name}.{key}"
            for key in COLUMN_FIELDS
            if fields.get(key) not in (None, "")
        )
    return out


def account(
    declared: Mapping[str, Any],
    carried: Iterable[str],
    reasons: Mapping[str, str],
    *,
    default: str,
) -> tuple[list[str], dict[str, str]]:
    """``(carried, not_carried)`` over every declared field.

    ``carried`` is what the derivation placed; ``reasons`` says why a field was
    not, where the derivation knows, and ``default`` covers the rest.
    """

    placed = set(carried)
    stated = declared_fields(declared)
    out_carried = [path for path in stated if path in placed]
    not_carried = {
        path: reasons.get(path) or reasons.get(_column_of(path), default)
        for path in stated
        if path not in placed
    }
    return out_carried, not_carried


# --- where a derived definition reads its table from ---------------------------------


def table_name(table: Any) -> str:
    """The last part of a qualified table, unquoted and lowered; ``""`` if none."""

    if not isinstance(table, str):
        return ""
    return table.rpartition(".")[2].strip('"`[]').lower()


def sibling_prefix(tables: Iterable[Any], identifiers: Iterable[str]) -> str | None:
    """The ``database.schema`` every sibling definition reads from, or ``None``.

    A definition whose table ends in one this dbt project builds is a sibling;
    where every sibling agrees on what precedes the table name, a new mart from
    the same project is read from there too. The semantic layer's own
    convention wins over dbt's, because dbt knows a relation per target and a
    layer usually reads one the derivation cannot see (often prod).
    """

    built = {identifier.lower() for identifier in identifiers}
    prefixes = set()
    for table in tables:
        if not isinstance(table, str) or "." not in table:
            continue
        head, _, _last = table.rpartition(".")
        if table_name(table) in built:
            prefixes.add(head)
    return next(iter(prefixes)) if len(prefixes) == 1 else None


def placed_table(
    identifier: str, relation: str | None, prefix: str | None
) -> tuple[str, str]:
    """The table a derived definition reads, and where it came from.

    ``siblings`` (the prefix every sibling shares), then ``manifest`` (the
    relation dbt last compiled), then ``bare`` (the table name alone). Each
    vendor says what the choice means in its own words.
    """

    if prefix is not None:
        return f"{prefix}.{identifier}", "siblings"
    if relation:
        return relation, "manifest"
    return identifier, "bare"


def _column_of(path: str) -> str:
    # `columns.amount.unit` -> `columns.amount`, so one reason covers a column.
    return path.rsplit(".", 1)[0] if path.startswith("columns.") else path


def _values(values: Iterable[Any]) -> str:
    rendered = [_value(v) for v in values]
    if len(rendered) == 1:
        return rendered[0]
    return ", ".join(rendered[:-1]) + f" or {rendered[-1]}"


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)
