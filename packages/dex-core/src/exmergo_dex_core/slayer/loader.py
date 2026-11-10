"""Reading SLayer model files, and judging whether each one is a model.

One of the three modules in dex that import SLayer, beside :mod:`.schema` and
:mod:`.render`. Everything else reaches the validated models through
:func:`load_models`, which is reached only through a source that
:func:`.dependency.ensure_available` let construct, so a base install never
imports it.

**SLayer's own model is the validator, and SLayer's storage is never opened.**
Each configured file is read with ``yaml.safe_load`` and validated with SLayer's
``SlayerModel``, so what dex accepts is what the pinned release accepts, with no
second reading of a format that moves between releases. ``YAMLStorage`` is not
used: opening it runs layout migrations that move and rename files under the
storage root, and a read that rewrites the repository is not a read.

**A stale format is refused before SLayer sees it.** ``SlayerModel`` migrates an
older ``version`` while parsing and passes a newer one through on a best-effort
basis. Either would let dex report a file as current when SLayer's next save
would rewrite it, or read fields the pinned release does not know, so the
version is checked first and anything but the pinned release's current version
is an error naming both numbers.

**The same checks judge a set that is not on disk yet.**
:func:`validate_model_contents` takes the text of every model instead of reading
files, which is how authoring validates the set a plan would leave before it is
stored, with no file written and no storage opened.

**A key the pinned release does not declare is named.** ``SlayerModel`` and
the models nested in it ignore an unknown key, so a field SLayer does not have
validates and then does nothing. Every mapping is compared with the fields its
model declares, at every level, and each key outside them is an ``unknown_key``
diagnostic. Reading reports it as a warning, because SLayer reads the file;
authoring refuses it, because dex would be writing a key that does nothing.

**Problems are collected, not raised.** A file with two problems reports two,
and a caller on the declaration channel may not raise at all, so every finding
is a :class:`Diagnostic` naming the file and the element it sits at. A model
with an error is absent from :attr:`LoadResult.models` and present in
:attr:`LoadResult.diagnostics`, which is what keeps "this could not be read"
from looking like "this declares nothing".
"""

from __future__ import annotations

# First, before any SLayer import: importing SLayer rewrites sqlglot's reserved
# keywords process-wide, and this module undoes that (see `sqlglot_keywords`).
from . import sqlglot_keywords  # noqa: F401

# isort: split

import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Union, get_args, get_origin

import yaml
from pydantic import AliasChoices, BaseModel, ValidationError
from slayer.core.models import SlayerModel, join_key_error
from slayer.storage.migrations import CURRENT_VERSIONS

from .dependency import SLAYER_VERSION

__all__ = [
    "FORMAT_VERSION",
    "Diagnostic",
    "LoadResult",
    "LoadedModel",
    "UnaggregatableMeasure",
    "formula_problems",
    "load_models",
    "unaggregatable_measures",
    "validate_model_contents",
]

#: The model format version the pinned SLayer release writes. A file carrying
#: any other version is refused rather than migrated or read best-effort.
FORMAT_VERSION: int = CURRENT_VERSIONS["SlayerModel"]

# Severity vocabulary, matching the Ossie reader's: `error` means dex will not
# read the model, `warning` means it read it and something in it is questionable.
ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Diagnostic:
    """One thing wrong with a model file.

    ``element`` is where in the model the problem sits, named by the element's
    own name where it has one (``columns['order id'].name``), ``(root)`` for the
    model as a whole, and ``(config)`` for the coordinate that named the file.
    ``rule`` says which check spoke, so a caller can act on a class of finding
    without matching on prose.
    """

    file: str
    element: str
    rule: str
    severity: str
    message: str

    def render(self) -> str:
        """One line, for a note list or an exception message."""

        return f"{self.file}: {self.element}: [{self.rule}] {self.message}"


@dataclass(frozen=True)
class LoadedModel:
    """One model that passed every check, with the file it came from."""

    file: str
    model: SlayerModel

    def aggregated_columns(self, measure: Any) -> list[str] | None:
        """The column references a saved measure's formula aggregates, as
        SLayer's own parser reads it.

        A bare-name reference to a sibling measure is expanded first, so a ratio
        of two saved measures reports both sides' columns. ``*`` is a row count
        and a dotted name is a column reached through a join; both come back as
        written for the caller to resolve.

        ``None`` means the formula was not read: SLayer's parser refused it, or
        the ``[sql]`` extra it validates scalar calls with is absent (see
        :func:`formula_problems`). A caller must not read that as a formula that
        aggregates nothing.
        """

        if find_spec("sqlglot") is None:
            return None
        from slayer.core.formula import parse_formula

        named = {
            m.name: m.formula
            for m in self.model.measures
            if m.name and m.name != measure.name
        }
        try:
            spec = parse_formula(
                measure.formula,
                extra_agg_names=frozenset(a.name for a in self.model.aggregations),
                named_measures=named,
            )
        except (ValueError, RecursionError):
            return None
        refs = _aggregated_refs(spec)
        return None if refs is None else [ref.measure_name for ref in refs]


#: SLayer's built-in aggregations whose result can be one of their input's values
#: (`min`, `max`, `first`, `last`; `median` over an odd count, and `percentile`,
#: which at 0 or 1 is the minimum or maximum) or all of them (`collect`, a list).
#: A model's own custom aggregation is read as one too, since dex cannot say what
#: its SQL returns.
VALUE_AGGREGATIONS: frozenset[str] = frozenset(
    {"min", "max", "first", "last", "median", "percentile", "collect"}
)


def value_aggregated_columns(loaded: LoadedModel, measure: Any) -> list[str] | None:
    """The column references a saved measure returns values of, as written.

    A count or a sum answers a number about a column; ``max(email)`` or
    ``collect(email.value)`` answers with the column's own values, which is
    what the PII gate screens. So the references aggregated by a value-returning
    aggregation are returned, for the caller to resolve and screen as it would a
    grouped column (``email``, ``tags.value``, ``customers.email``). ``*`` reads
    no column. ``None`` means the formula was not read, which is unscreened
    rather than clean.
    """

    if find_spec("sqlglot") is None:
        return None
    from slayer.core.enums import normalize_aggregation_name
    from slayer.core.formula import parse_formula

    model = loaded.model
    custom = frozenset(a.name for a in model.aggregations)
    named = {
        m.name: m.formula for m in model.measures if m.name and m.name != measure.name
    }
    try:
        spec = parse_formula(
            measure.formula, extra_agg_names=custom, named_measures=named
        )
    except (ValueError, RecursionError):
        return None
    refs = _aggregated_refs(spec)
    if refs is None:
        return None
    found: list[str] = []
    for ref in refs:
        written = ref.aggregation_name
        agg = written if written in custom else normalize_aggregation_name(written)
        if (agg in VALUE_AGGREGATIONS or agg in custom) and ref.measure_name != "*":
            found.append(ref.measure_name)
    return found


def _aggregated_refs(spec: Any) -> list[Any] | None:
    """Every aggregated reference in a parsed formula, or ``None`` for a shape
    this release does not know, which is unread rather than empty."""

    from slayer.core.formula import (
        AggregatedMeasureRef,
        ArithmeticField,
        MixedArithmeticField,
        TransformField,
    )

    if isinstance(spec, AggregatedMeasureRef):
        return [spec]
    if isinstance(spec, TransformField):
        return _aggregated_refs(spec.inner)
    if isinstance(spec, ArithmeticField):
        return list(spec.agg_refs.values())
    if isinstance(spec, MixedArithmeticField):
        found = list(spec.agg_refs.values())
        for _, transform in spec.sub_transforms:
            inner = _aggregated_refs(transform)
            if inner is None:
                return None
            found.extend(inner)
        return found
    return None


@dataclass
class LoadResult:
    """Every configured model dex could read, and everything wrong with the rest."""

    models: list[LoadedModel] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == ERROR]

    def notes(self) -> list[str]:
        """Every diagnostic as a line, for a caller's note list."""

        return [d.render() for d in self.diagnostics]


def load_models(
    repo_root: Path | str, base_dir: str, models: Sequence[str]
) -> LoadResult:
    """Read, validate, and return every configured model file.

    Never raises for a bad file. ``models`` are named relative to ``base_dir``,
    and each diagnostic names the file that way too, because that is the
    spelling the configuration line uses.
    """

    root = Path(repo_root).resolve()
    storage = (root / base_dir).resolve()
    result = LoadResult()
    parsed: list[LoadedModel] = []
    for name in models:
        path, refusal = _resolve(root, storage, name)
        if refusal is not None:
            result.diagnostics.append(refusal)
            continue
        loaded, found = _read(path, name)
        result.diagnostics.extend(found)
        if loaded is not None:
            parsed.append(loaded)
    return _across_set(parsed, models, result)


def validate_model_contents(contents: Mapping[str, str]) -> LoadResult:
    """Judge a set of models given as text, keyed as ``semantic.slayer.models``.

    Every check :func:`load_models` runs, over content that need not exist on
    disk: authoring validates the set a plan would leave this way. Never raises
    for a bad model, for the same reason.
    """

    result = LoadResult()
    parsed: list[LoadedModel] = []
    for name, text in contents.items():
        loaded, found = _parse(text, name)
        result.diagnostics.extend(found)
        if loaded is not None:
            parsed.append(loaded)
    return _across_set(parsed, list(contents), result)


def _across_set(
    parsed: list[LoadedModel], models: Sequence[str], result: LoadResult
) -> LoadResult:
    """Finish a load: the checks that need every model, then the warnings."""

    # A file that failed before its name could be read still names its model in
    # its path, so a join to it is reported as unreadable rather than unknown.
    configured = {PurePosixPath(name).stem for name in models}
    result.models = _across_models(parsed, configured, result.diagnostics)
    for loaded in result.models:
        result.diagnostics.extend(_warnings(loaded))
    return result


def _resolve(
    root: Path, storage: Path, name: str
) -> tuple[Path | None, Diagnostic | None]:
    """The configured name as a file inside the storage root, or a refusal.

    Confinement is checked on the *resolved* path, so a symlink pointing out of
    the repository, or out of the storage root into a datasource file, is caught
    along with a ``..`` that walks out of it.
    """

    def refusal(rule: str, message: str) -> tuple[None, Diagnostic]:
        return None, Diagnostic(
            file=name, element="(config)", rule=rule, severity=ERROR, message=message
        )

    if PurePosixPath(name).is_absolute():
        return refusal(
            "confinement",
            f"a SLayer model is named relative to semantic.slayer.base_dir, and "
            f"'{name}' is an absolute path",
        )
    resolved = (storage / name).resolve()
    for boundary, what in ((root, "the repository"), (storage, "the storage root")):
        try:
            resolved.relative_to(boundary)
        except ValueError:
            return refusal(
                "confinement",
                f"'{name}' resolves outside {what} ({resolved}); dex reads SLayer "
                "models only from semantic.slayer.base_dir inside the repository",
            )
    if resolved.relative_to(storage).parts[:1] != ("models",):
        # Inside the storage root is not enough: a model symlinked to a
        # datasource file would hand its credentials to the parser.
        return refusal(
            "confinement",
            f"'{name}' resolves to {resolved}, which is not under the storage "
            "root's models/ directory; dex reads SLayer models only from there",
        )
    if not resolved.is_file():
        return refusal("missing", f"no such SLayer model file: '{name}'")
    return resolved, None


def _read(path: Path, name: str) -> tuple[LoadedModel | None, list[Diagnostic]]:
    """Parse and validate one file, collecting every problem with it."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, _refusal(
            name, "(root)", "unreadable", f"could not read the file: {exc}"
        )
    return _parse(text, name)


def _refusal(name: str, element: str, rule: str, message: str) -> list[Diagnostic]:
    return [
        Diagnostic(
            file=name, element=element, rule=rule, severity=ERROR, message=message
        )
    ]


def _parse(text: str, name: str) -> tuple[LoadedModel | None, list[Diagnostic]]:
    """Parse and validate one model's text, collecting every problem with it."""

    def refusal(element: str, rule: str, message: str) -> list[Diagnostic]:
        return _refusal(name, element, rule, message)

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # `yaml.YAMLError` descends from `Exception` rather than `ValueError`, so
        # it is caught by name.
        return None, refusal("(root)", "malformed", f"not valid YAML: {exc}")
    if not isinstance(raw, dict):
        return None, refusal(
            "(root)",
            "root",
            "a SLayer model is a mapping with `version` and `name` at its root, "
            f"and this parsed as {type(raw).__name__}",
        )

    stale = _version_problem(raw.get("version"))
    if stale is not None:
        return None, refusal("version", "format_version", stale)

    # Named whether or not the model validates, so an author fixing a refusal
    # sees every problem in the file at once.
    unknown = [
        Diagnostic(
            file=name,
            element=_element(raw, loc),
            rule="unknown_key",
            severity=WARNING,
            message=_unknown_key_message(loc[-1], owner),
        )
        for loc, owner in _unknown_keys(raw, SlayerModel, ())
    ]
    try:
        model = SlayerModel.model_validate(raw)
    except ValidationError as exc:
        return None, [
            *(
                Diagnostic(
                    file=name,
                    element=_element(raw, error["loc"]),
                    rule="invalid",
                    severity=ERROR,
                    message=_message(error),
                )
                for error in exc.errors()
            ),
            *unknown,
        ]
    except (ValueError, TypeError) as exc:
        # A SLayer validator that raises its own error outside pydantic's
        # wrapping still means the model is not one the pinned release accepts.
        return None, [*refusal("(root)", "invalid", str(exc)), *unknown]

    mismatch = _path_problem(name, model)
    if mismatch is not None:
        return None, [*refusal("(root)", "path", mismatch), *unknown]
    return LoadedModel(file=name, model=model), unknown


#: What each pinned model is, as a reader holding the file would call it.
_OWNERS = {
    "SlayerModel": "a model",
    "Column": "a column",
    "NumberFormat": "a column's format",
    "ModelMeasure": "a saved measure",
    "Aggregation": "an aggregation",
    "AggregationParam": "an aggregation parameter",
    "ModelJoin": "a join",
    "SourceModelOrigin": "a model's source_model_origin",
}

#: What SLayer uses instead of a key an author carried over from MetricFlow,
#: keyed by the pinned model the key was written on.
_INSTEAD = {
    ("SlayerModel", "entities"): (
        "SLayer declares no entities: a key is a column flagged "
        "`primary_key: true` (several make one composite key) or `unique: true`, "
        "and a relationship is a `joins:` entry"
    ),
    ("SlayerModel", "primary_entity"): (
        "SLayer declares no entities: a model's key is its columns flagged "
        "`primary_key: true`"
    ),
    ("SlayerModel", "dimensions"): "SLayer declares dimensions as `columns:`",
    ("SlayerModel", "metrics"): (
        "a SLayer metric is a saved measure under `measures:`, with a `formula`"
    ),
    ("SlayerModel", "defaults"): (
        "a model's time dimension is `default_time_dimension`, at the model's root"
    ),
    ("SlayerModel", "agg_time_dimension"): (
        "a model's time dimension is `default_time_dimension`"
    ),
    ("SlayerModel", "model"): (
        "the relation a model sits on is `sql_table`, fully qualified"
    ),
    ("ModelMeasure", "agg"): (
        "a saved measure's `formula` carries its aggregation, as in `amount:sum`"
    ),
    ("ModelMeasure", "expr"): (
        "a saved measure's expression is its `formula`, aggregation included, as "
        "in `amount:sum`"
    ),
    ("ModelMeasure", "agg_time_dimension"): (
        "every saved measure is bucketed by its model's `default_time_dimension`"
    ),
    ("ModelMeasure", "create_metric"): (
        "a saved measure is queryable as it stands, with no metric to create"
    ),
    ("Column", "expr"): "a column's expression is `sql`",
}


def _unknown_keys(
    raw: Mapping[Any, Any], owner: type[BaseModel], loc: tuple[Any, ...]
) -> Iterator[tuple[tuple[Any, ...], type[BaseModel]]]:
    """Every key in ``raw`` that ``owner`` declares no field for, at any depth.

    Yields each key's location and the model it was written on. A model that
    keeps extra keys (``extra="allow"``) or refuses them itself
    (``extra="forbid"``) is left to pydantic.
    """

    if owner.model_config.get("extra") not in (None, "ignore"):
        return
    accepted: dict[str, Any] = {}
    for name, info in owner.model_fields.items():
        for spelling in (name, info.alias, *_alias_choices(info.validation_alias)):
            if isinstance(spelling, str):
                accepted[spelling] = info.annotation
    for key, value in raw.items():
        if key not in accepted:
            yield (*loc, key), owner
            continue
        yield from _unknown_in(value, accepted[key], (*loc, key))


def _unknown_in(
    value: Any, annotation: Any, loc: tuple[Any, ...]
) -> Iterator[tuple[tuple[Any, ...], type[BaseModel]]]:
    """Descend into ``value`` wherever its field's type holds a pinned model."""

    origin = get_origin(annotation)
    if origin is Annotated:
        yield from _unknown_in(value, get_args(annotation)[0], loc)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if isinstance(value, Mapping):
            yield from _unknown_keys(value, annotation, loc)
        return
    args = [a for a in get_args(annotation) if a is not Ellipsis]
    if origin in (Union, types.UnionType):
        members = [a for a in args if a is not type(None)]
        models = [
            a for a in members if isinstance(a, type) and issubclass(a, BaseModel)
        ]
        # Two models in one union leave no way to tell which a mapping is meant
        # as, and a key one of them declares is not unknown.
        if len(models) <= 1:
            for member in members:
                yield from _unknown_in(value, member, loc)
        return
    if origin in (list, tuple, set, frozenset) or (
        isinstance(origin, type) and issubclass(origin, Sequence)
    ):
        if isinstance(value, list) and len(args) == 1:
            for index, item in enumerate(value):
                yield from _unknown_in(item, args[0], (*loc, index))
        return
    if (
        isinstance(origin, type)
        and issubclass(origin, Mapping)
        and isinstance(value, Mapping)
        and len(args) == 2
    ):
        for key, item in value.items():
            yield from _unknown_in(item, args[1], (*loc, key))


def _alias_choices(alias: Any) -> list[Any]:
    if isinstance(alias, AliasChoices):
        return list(alias.choices)
    return [alias]


def _unknown_key_message(key: Any, owner: type[BaseModel]) -> str:
    what = _OWNERS.get(owner.__name__, f"a {owner.__name__}")
    instead = _INSTEAD.get((owner.__name__, key))
    fix = (
        f"{instead[0].upper()}{instead[1:]}. Remove the key"
        if instead
        else "Correct it if it is a typo, or remove it"
    )
    return (
        f"`{key}` is not a field SLayer {SLAYER_VERSION} (the release dex pins) "
        f"declares on {what}, so SLayer ignores it and it does nothing. {fix}"
    )


def _version_problem(version: Any) -> str | None:
    """Why ``version`` is not the pinned format, or ``None`` when it is.

    A file with no ``version`` is stale rather than current: SLayer's storage
    stamps a stored model without one as version 1 and migrates it from there,
    so reading it as current would disagree with the tool that owns the format.
    """

    expected = (
        f"SLayer {SLAYER_VERSION} reads and writes model format version "
        f"{FORMAT_VERSION}"
    )
    if version is None:
        return (
            f"the model carries no `version`, which SLayer reads as format "
            f"version 1. {expected}; set `version: {FORMAT_VERSION}` once the "
            "model is current, rather than letting SLayer migrate it on its next "
            "save"
        )
    if isinstance(version, bool) or not isinstance(version, int):
        return f"`version` is {version!r}, not a format version number. {expected}"
    if version < FORMAT_VERSION:
        return (
            f"the model is written in format version {version}, which is stale. "
            f"{expected}, and SLayer would migrate this file on its next save. "
            "dex reads only the current format rather than migrating it in "
            "memory and reporting it as current"
        )
    if version > FORMAT_VERSION:
        return (
            f"the model is written in format version {version}, newer than this "
            f"dex reads. {expected}; a newer format may carry fields the pinned "
            "release does not know, so it is refused rather than read "
            "best-effort"
        )
    return None


def _path_problem(name: str, model: SlayerModel) -> str | None:
    """Whether the file sits where SLayer would keep this model.

    SLayer stores a model at ``models/<data_source>/<name>.yaml``, so a file
    whose path and content disagree is one SLayer would read under a different
    name, or not at all.
    """

    parts = PurePosixPath(name).parts
    if len(parts) != 3:
        return None
    data_source, stem = parts[1], PurePosixPath(parts[2]).stem
    if model.name != stem or model.data_source != data_source:
        return (
            f"the file is at models/{data_source}/{stem}, and the model inside "
            f"declares name '{model.name}' and data_source "
            f"'{model.data_source}'. SLayer keeps a model at "
            "models/<data_source>/<name>.yaml, so the two have to agree"
        )
    return None


def _across_models(
    parsed: list[LoadedModel], configured: set[str], diagnostics: list[Diagnostic]
) -> list[LoadedModel]:
    """The models that survive the checks that need the whole set.

    A duplicated name retracts **every** file declaring it: SLayer resolves a
    join and a dotted reference by model name, so either copy would silently
    mean the other. A join to a model the set does not hold retracts the model
    declaring it, and that runs to a fixed point, because a model joining to a
    retracted one now joins to nothing.
    """

    names: dict[str, list[str]] = {}
    for loaded in parsed:
        names.setdefault(loaded.model.name, []).append(loaded.file)
    for model_name, files in names.items():
        if len(files) < 2:
            continue
        for file in files:
            others = ", ".join(f for f in files if f != file)
            diagnostics.append(
                Diagnostic(
                    file=file,
                    element="name",
                    rule="duplicate_name",
                    severity=ERROR,
                    message=(
                        f"model '{model_name}' is also declared in {others}. SLayer "
                        "resolves joins and references by model name, so neither "
                        "copy is read"
                    ),
                )
            )
    surviving = [m for m in parsed if len(names[m.model.name]) == 1]
    configured = configured | set(names)

    while True:
        by_name = {m.model.name: m.model for m in surviving}
        broken: dict[str, list[Diagnostic]] = {}
        for loaded in surviving:
            found = _join_problems(loaded, by_name, configured)
            if found:
                broken[loaded.file] = found
        if not broken:
            return surviving
        for found in broken.values():
            diagnostics.extend(found)
        surviving = [m for m in surviving if m.file not in broken]


def _join_problems(
    loaded: LoadedModel, by_name: dict[str, SlayerModel], configured: set[str]
) -> list[Diagnostic]:
    """Every join of one model that does not resolve within the set.

    The source side of each pair is already checked by ``SlayerModel`` itself.
    The target side needs the other model, so SLayer checks it only when its
    storage loads the peer, which dex never opens; the same rule is applied here
    through SLayer's own ``join_key_error``.
    """

    model = loaded.model
    found: list[Diagnostic] = []
    for index, join in enumerate(model.joins):
        element = f"joins[{join.name or index}]"
        target = by_name.get(join.target_model)
        if target is None:
            reason = (
                "which is configured but could not be read (see its own diagnostic)"
                if join.target_model in configured
                else "which no configured model file declares"
            )
            found.append(
                Diagnostic(
                    file=loaded.file,
                    element=f"{element}.target_model",
                    rule="unknown_join_target",
                    severity=ERROR,
                    message=(
                        f"model '{model.name}' joins to '{join.target_model}', "
                        f"{reason}. A join dex cannot resolve would reach the "
                        "relationship channel as a declared edge to nothing"
                    ),
                )
            )
            continue
        for _, key in join.join_pairs:
            error = join_key_error(
                model=model.name,
                target=target.name,
                key=key,
                side=target.name,
                columns=target.columns,
            )
            if error is not None and not target.awaits_columns:
                found.append(
                    Diagnostic(
                        file=loaded.file,
                        element=f"{element}.join_pairs",
                        rule="join_key",
                        severity=ERROR,
                        message=str(error),
                    )
                )
    return found


def _warnings(loaded: LoadedModel) -> list[Diagnostic]:
    """What dex reads past and says so."""

    model = loaded.model
    found: list[Diagnostic] = []
    declared = model.default_time_dimension
    if declared and model.get_column(declared) is None:
        found.append(
            Diagnostic(
                file=loaded.file,
                element="default_time_dimension",
                rule="unknown_column",
                severity=WARNING,
                message=(
                    f"model '{model.name}' names default_time_dimension "
                    f"'{declared}', which is not one of its columns, so dex "
                    "reports no time dimension for it"
                ),
            )
        )
    found.extend(
        Diagnostic(
            file=loaded.file,
            element=f"measures[{problem.measure!r}].formula",
            rule="aggregation_not_applicable",
            severity=WARNING,
            message=(
                f"model '{model.name}' saves measure '{problem.measure}', "
                f"which aggregates column '{problem.column}' with "
                f"'{problem.aggregation}', and SLayer refuses that at query "
                f"time: {problem.reason} The metric is listed, and every "
                "query of it fails. Change the column's type, or remove or "
                "rewrite the measure"
            ),
        )
        for problem in unaggregatable_measures(loaded) or []
    )
    return found


@dataclass(frozen=True)
class UnaggregatableMeasure:
    """A saved measure SLayer would refuse to aggregate, and why."""

    measure: str
    column: str
    aggregation: str
    reason: str


def unaggregatable_measures(
    loaded: LoadedModel, retyped: Mapping[str, Any] | None = None
) -> list[UnaggregatableMeasure] | None:
    """Every saved measure whose aggregation SLayer refuses on its column.

    SLayer checks this only when it binds a query (``AggregationNotAllowedError``),
    so a model that saves ``amount:sum`` over a TEXT column validates, is listed,
    and fails on every query. The gate is restated here from SLayer's public
    pieces, in its order: a sole primary key takes only
    ``PRIMARY_KEY_AGGREGATIONS``; a column's ``allowed_aggregations`` is a
    whitelist; otherwise the type's ``DEFAULT_AGGREGATIONS_BY_TYPE`` decides, and
    a model's own custom aggregation is exempt. A test holds this to SLayer's
    binding gate, so a release that moves the rule fails there first.

    ``retyped`` overrides a column's declared type (``{column: DataType}``), which
    is how a retype asks what a proposed type would break. ``None`` means the
    formulas were not read (no ``[sql]`` extra): unchecked, not clean. A
    reference through a join, ``*``, and a formula SLayer's parser refuses are
    left to their own checks.
    """

    if find_spec("sqlglot") is None:
        return None
    from slayer.core.enums import (
        DEFAULT_AGGREGATIONS_BY_TYPE,
        PRIMARY_KEY_AGGREGATIONS,
        normalize_aggregation_name,
    )
    from slayer.core.formula import parse_formula
    from slayer.core.models import is_identifier

    model = loaded.model
    custom = frozenset(a.name for a in model.aggregations)
    named = {m.name: m.formula for m in model.measures if m.name}
    columns = {c.name: c for c in model.columns}
    found: list[UnaggregatableMeasure] = []
    for measure in model.measures:
        others = {k: v for k, v in named.items() if k != measure.name}
        try:
            spec = parse_formula(
                measure.formula, extra_agg_names=custom, named_measures=others
            )
        except (ValueError, RecursionError):
            continue
        for ref in _aggregated_refs(spec) or []:
            col = columns.get(ref.measure_name)
            if col is None:
                continue
            written = ref.aggregation_name
            agg = written if written in custom else normalize_aggregation_name(written)
            if agg in custom:
                continue
            col_type = (retyped or {}).get(col.name, col.type)
            if is_identifier(column=col, columns=model.columns):
                if agg in PRIMARY_KEY_AGGREGATIONS:
                    continue
                reason = (
                    f"'{col.name}' is the model's primary key, which aggregates "
                    f"only with {', '.join(sorted(PRIMARY_KEY_AGGREGATIONS))}."
                )
            elif col.allowed_aggregations is not None:
                if agg in col.allowed_aggregations:
                    continue
                reason = (
                    f"'{col.name}' restricts allowed_aggregations to "
                    f"{', '.join(sorted(col.allowed_aggregations))}."
                )
            else:
                allowed = DEFAULT_AGGREGATIONS_BY_TYPE.get(col_type, frozenset())
                if agg in allowed:
                    continue
                spelled = getattr(col_type, "value", col_type)
                reason = (
                    f"'{agg}' is not applicable to a {spelled} column, which "
                    f"aggregates only with {', '.join(sorted(allowed))}."
                )
            found.append(
                UnaggregatableMeasure(
                    measure=measure.name or "",
                    column=col.name,
                    aggregation=agg,
                    reason=reason,
                )
            )
    return found


def formula_problems(loaded: LoadedModel) -> list[Diagnostic] | None:
    """Every saved measure whose formula SLayer's own parser refuses.

    Parsed the way SLayer's dbt converter checks a measure it is about to keep:
    with the model's custom aggregations, and every other saved measure available
    for a bare-name reference, so a formula naming a sibling measure parses and
    one naming a missing measure does not. ``SlayerModel`` itself does not parse
    a formula, which is why this is a separate check.

    ``None`` means the check did not run: SLayer validates scalar calls inside a
    formula with sqlglot, the ``[sql]`` extra, and without it the parser would
    reject sound formulas, so a caller says it skipped rather than reporting
    either verdict.
    """

    if find_spec("sqlglot") is None:
        return None
    from slayer.core.formula import parse_formula

    model = loaded.model
    aggregations = frozenset(a.name for a in model.aggregations)
    named = {m.name: m.formula for m in model.measures if m.name}
    found: list[Diagnostic] = []
    for index, measure in enumerate(model.measures):
        others = {k: v for k, v in named.items() if k != measure.name}
        try:
            parse_formula(
                measure.formula, extra_agg_names=aggregations, named_measures=others
            )
        except (ValueError, RecursionError) as exc:
            label = measure.name or index
            found.append(
                Diagnostic(
                    file=loaded.file,
                    element=f"measures[{label!r}].formula",
                    rule="formula",
                    severity=ERROR,
                    message=(
                        f"model '{model.name}' saves measure '{label}' with "
                        f"formula {measure.formula!r}, which SLayer's formula "
                        f"parser refuses: {exc}"
                    ),
                )
            )
    return found


def _element(raw: dict[str, Any], loc: Sequence[Any]) -> str:
    """A pydantic error location, with list indices named by the element's name.

    ``columns -> 3 -> name`` reads as nothing to someone holding the file;
    ``columns['order id'].name`` says which column to open.
    """

    if not loc:
        return "(root)"
    rendered = ""
    node: Any = raw
    for part in loc:
        if isinstance(part, int):
            item = node[part] if isinstance(node, list) and part < len(node) else None
            label = item.get("name") if isinstance(item, dict) else None
            rendered += f"[{label!r}]" if isinstance(label, str) else f"[{part}]"
            node = item
        else:
            rendered += f".{part}" if rendered else str(part)
            node = node.get(part) if isinstance(node, dict) else None
    return rendered


def _message(error: Any) -> str:
    """Pydantic's message without its "Value error, " prefix."""

    message = str(error.get("msg", ""))
    return message.removeprefix("Value error, ")
