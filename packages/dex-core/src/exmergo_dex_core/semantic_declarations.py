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
reading the catalog meets it).

**Every declared field is accounted for.** :func:`declared_fields` lists each
field the declaration states, as a path (``population``,
``columns.amount.unit``), and a derivation reports each as carried or not,
with why, so nothing a declaration says vanishes on the way into a layer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

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


class Population(_Strict):
    rule: str | None = None
    filters: list[PopulationFilter] = []


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


class Assumption(_Strict):
    decision: str | None = None
    chosen: str | None = None
    evidence: Evidence | None = None
    detail: str | None = None


class Declaration(_Strict):
    model: str
    description: str | None = None
    grain: list[str] | None = None
    population: Population | None = None
    columns: dict[str, ColumnDeclaration] = {}
    assumptions: list[Assumption] = []


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
MODEL_FIELDS = ("description", "grain", "population", "assumptions")

#: Column-level fields a declaration may state.
COLUMN_FIELDS = (
    "role",
    "description",
    "references",
    "null_rule",
    "aggregation",
    "additivity",
    "unit",
)


def population_sentence(population: Any) -> str | None:
    """One sentence stating which rows the model holds, or None.

    The rule in the author's words where there is one, then each filter as the
    values it keeps or drops. A filter's values are already committed in the
    model's YAML (and refused there for a column that looks like personal
    data), so stating them again leaks nothing new.
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
    malformed = (
        population is None
        or (isinstance(stated, list) and read < len(stated))
        or (bool(stated) and not isinstance(stated, list))
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
