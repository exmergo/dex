# Ported from apache/ossie-converters, databricks/java at 82006ba2
# (MetricViewToOssie.java). Licensed under the Apache License, Version 2.0.
"""IMPORT: a Metric View v1.1 body to an Apache Ossie semantic model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ._java import java_str, split_dropping_trailing, trim
from .common import (
    CARD_MANY_TO_ONE,
    CARD_ONE_TO_MANY,
    DIALECT_DATABRICKS,
    JAVA_DOT,
    MAX_JOIN_NODES,
    MV_VERSION,
    OSSIE_VERSION,
    SELECT_WITH_RE,
    STASH_SOURCE_KEY,
    ConversionError,
    Notices,
    as_list,
    as_map,
    cat,
    is_simple_identifier,
    last_identifier,
    parse_root,
    qualifier_chain_pattern,
    require_str,
    rewrite_qualifiers,
    str_,
    str_list,
    truthy,
    validate_source,
    write_stash,
)
from .dialect import classify

MODEL_STASH_KEYS = ("filter", "parameters", "materialization")
JOIN_STASH_KEYS = ("rely", "cardinality")
COLUMN_STASH_KEYS = ("format", "window", "partition")
MEASURE_STASH_KEYS = ("display_name",)

NON_EQUI_RE = re.compile(r"[<>!]=|<>|[<>]")
AND_SPLIT_RE = re.compile(r"\s+AND\s+", re.IGNORECASE | re.ASCII)
EQ_CLAUSE_RE = re.compile(rf"\s*({JAVA_DOT}+?)\s*=\s*({JAVA_DOT}+?)\s*", re.ASCII)
FACT_WILDCARD_RE = re.compile(
    rf"\s*(?:source\s*\.\s*)?\*(?:\s+EXCEPT\s*\({JAVA_DOT}*\))?\s*",
    re.IGNORECASE | re.ASCII,
)


@dataclass
class Imported:
    """An import's result: the Ossie model and the fact dataset's name."""

    model: dict[str, Any]
    fact: str


def convert_metric_view_to_ossie(
    mv_yaml: str, model_name: str | None, notices: Notices
) -> Imported:
    """``convertMetricViewToOssie``: parse ``mv_yaml`` and convert it."""
    view = parse_root(mv_yaml, "Metric View")
    return convert_metric_view_value(view, model_name, notices)


def convert_metric_view_value(
    view: dict[str, Any], model_name: str | None, notices: Notices
) -> Imported:
    """The conversion of an already parsed Metric View body."""
    version_value = view.get("version")
    version = java_str(version_value)
    if version_value is None or not trim(version or ""):
        raise ConversionError.invalid_input(
            "Invalid Metric View YAML: missing required 'version' field",
            "it is missing the required 'version' field",
        )
    if version != MV_VERSION:
        raise ConversionError.unsupported(
            f"Unsupported Metric View version '{version}'. This converter targets "
            f"v{MV_VERSION} only.",
            version or "",
        )
    model, fact = _convert_view(view, model_name, notices)
    return Imported({"version": OSSIE_VERSION, **model}, fact)


def _convert_view(
    view: dict[str, Any], model_name: str | None, notices: Notices
) -> tuple[dict[str, Any], str]:
    source = view.get("source")
    if source is None or java_str(source) == "":
        raise ConversionError("Metric View is missing required 'source'")
    is_sql = SELECT_WITH_RE.search(trim(str(java_str(source)))) is not None
    last_id = last_identifier(source)
    if model_name:
        fact_name = model_name
    elif not is_sql and last_id is not None and is_simple_identifier(last_id):
        fact_name = last_id
    else:
        fact_name = "metric_view"
    validate_source(source, fact_name)

    fact_ds: dict[str, Any] = {"name": fact_name, "source": source}
    datasets: list[dict[str, Any]] = [fact_ds]
    relationships: list[dict[str, Any]] = []
    alias_to_dataset = {"source": fact_name, fact_name: fact_name}
    seen_names = {trim(fact_name).lower()}

    _walk(
        fact_name,
        "source",
        as_list(view.get("joins")),
        datasets,
        relationships,
        alias_to_dataset,
        seen_names,
    )
    if len(datasets) > MAX_JOIN_NODES:
        raise ConversionError(
            f"Metric View '{fact_name}' has {len(datasets)} datasets; at most "
            f"{MAX_JOIN_NODES} are supported."
        )

    dim_list = as_list(view.get("dimensions"))
    field_list = as_list(view.get("fields"))
    if dim_list and field_list:
        notices.warn(
            "view",
            "both 'dimensions' and 'fields' are set; 'fields' is a v1.1 alias for "
            "'dimensions', so the 'fields' list is ignored",
        )
    fields_by_dataset: dict[str, list[Any]] = {d["name"]: [] for d in datasets}
    dims = dim_list if dim_list else field_list
    dataset_names = {d["name"].lower() for d in datasets}
    scope = _DialectScope(
        dataset_names, _view_defined_names(view, dims), _has_hidden_names(dims)
    )
    for dim_obj in dims:
        dim = as_map(dim_obj)
        if _is_wildcard(dim):
            notices.warn(
                "dimension",
                f"wildcard column '{cat(dim.get('expr'))}' has no Apache Ossie "
                "field representation; skipped",
            )
            continue
        ds_name, field = _convert_dimension(dim, alias_to_dataset, fact_name, scope)
        fields_by_dataset[ds_name].append(field)
    for d in datasets:
        flds = fields_by_dataset[d["name"]]
        if flds:
            d["fields"] = flds

    metrics: list[Any] = []
    alias_head = qualifier_chain_pattern(alias_to_dataset.keys())
    filtered = "filter" in view
    for m_obj in as_list(view.get("measures")):
        m = as_map(m_obj)
        if _is_wildcard(m):
            notices.warn(
                "measure",
                f"wildcard measure '{cat(m.get('expr'))}' has no Apache Ossie metric "
                "representation; skipped",
            )
            continue
        metrics.append(
            _convert_measure(m, alias_head, alias_to_dataset, scope, filtered)
        )

    model: dict[str, Any] = {"name": fact_name}
    if truthy(view.get("comment")):
        model["description"] = view.get("comment")
    model["datasets"] = datasets
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics

    model_stash = {k: view[k] for k in MODEL_STASH_KEYS if k in view}
    if _has_otm(as_list(view.get("joins"))):
        model_stash[STASH_SOURCE_KEY] = fact_name
    write_stash(model, model_stash)
    return model, fact_name


def _walk(
    parent_name: str,
    parent_alias: str,
    joins: list[Any],
    datasets: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    alias_to_dataset: dict[str, str],
    seen_names: set[str],
) -> None:
    for join_obj in joins:
        join = as_map(join_obj)
        child = require_str(join, "name", "join")
        if trim(child).lower() == "source":
            raise ConversionError(
                "Join name 'source' is reserved for the fact source; rename the join."
            )
        key = trim(child).lower()
        if key in seen_names:
            raise ConversionError(
                f"Duplicate dataset/join name '{child}'; Metric View join names and "
                "the source must be distinct (case-insensitively)."
            )
        seen_names.add(key)
        child_source = require_str(join, "source", f"join '{child}'")
        validate_source(child_source, child)
        child_ds: dict[str, Any] = {"name": child, "source": child_source}
        datasets.append(child_ds)
        alias_to_dataset[child] = child
        rel = _convert_join(join, parent_name, parent_alias, child)
        relationships.append(rel)
        rely = as_map(join.get("rely"))
        to_cols = str_list(rel.get("to_columns"))
        if (
            child == str_(rel.get("to"))
            and to_cols
            and rely.get("at_most_one_match") is True
        ):
            child_ds["unique_keys"] = [list(to_cols)]
        _walk(
            child,
            child,
            as_list(join.get("joins")),
            datasets,
            relationships,
            alias_to_dataset,
            seen_names,
        )


def _has_otm(joins: list[Any]) -> bool:
    for j_obj in joins:
        j = as_map(j_obj)
        if (str_(j.get("cardinality")) or "").lower() == CARD_ONE_TO_MANY:
            return True
        if _has_otm(as_list(j.get("joins"))):
            return True
    return False


def _convert_join(
    join: dict[str, Any], parent_name: str, parent_alias: str, child: str
) -> dict[str, Any]:
    has_using = join.get("using") is not None and bool(as_list(join.get("using")))
    on_text = str_(join.get("on"))
    has_on = on_text is not None and on_text != ""
    if not has_using and not has_on:
        raise ConversionError(
            f"Join '{child}' has no join condition (empty or absent 'on'/'using'); "
            "condition-less (cross) joins have no Apache Ossie relationship "
            "representation."
        )
    parent_cols, child_cols, raw_on = _decompose_on(
        join, parent_alias, parent_name, child
    )
    if raw_on is not None:
        raise ConversionError(
            f"Join '{child}' uses a non-equi or unsupported join condition ('on: "
            f"{raw_on}') that an Apache Ossie relationship cannot represent. Apache "
            "Ossie joins are equi-joins of simple `alias.column` pairs (the fact side "
            "may be qualified with `source`, the source table name, or left bare). "
            "Cannot import."
        )
    parent_cols = parent_cols or []
    child_cols = child_cols or []
    if not has_on and has_using and not parent_cols:
        using = str_list(join.get("using"))
        parent_cols = list(using)
        child_cols = list(using)

    cardinality = str_(join.get("cardinality"))
    if cardinality is None:
        cardinality = CARD_MANY_TO_ONE
    child_is_from = cardinality.lower() == CARD_ONE_TO_MANY
    rel: dict[str, Any] = {}
    if child_is_from:
        rel["name"] = f"{child}_to_{parent_name}"
        rel["from"] = child
        rel["to"] = parent_name
    else:
        rel["name"] = f"{parent_name}_to_{child}"
        rel["from"] = parent_name
        rel["to"] = child
    rel["from_columns"] = child_cols if child_is_from else parent_cols
    rel["to_columns"] = parent_cols if child_is_from else child_cols
    stash = {k: join[k] for k in JOIN_STASH_KEYS if k in join}
    if has_on and on_text != _rebuilt_on(parent_alias, child, parent_cols, child_cols):
        stash["on"] = on_text
    write_stash(rel, stash)
    return rel


def _rebuilt_on(
    parent_alias: str, child: str, parent_cols: list[str], child_cols: list[str]
) -> str | None:
    if parent_cols == child_cols:
        return None
    return " AND ".join(
        f"{parent_alias}.{p} = {child}.{c}"
        for p, c in zip(parent_cols, child_cols, strict=True)
    )


def _decompose_on(
    join: dict[str, Any], parent_alias: str, parent_name: str, child_alias: str
) -> tuple[list[str] | None, list[str] | None, str | None]:
    """``(parent columns, child columns, raw on)``; a raw ``on`` means refuse."""
    on = str_(join.get("on"))
    if not on:
        return [], [], None
    parent_aliases = {parent_alias, parent_name}
    allow_bare = parent_alias == "source"
    from_cols: list[str] = []
    to_cols: list[str] = []
    for clause in split_dropping_trailing(AND_SPLIT_RE.split(on)):
        if NON_EQUI_RE.search(clause):
            return None, None, on
        m = EQ_CLAUSE_RE.fullmatch(clause)
        if m is None:
            return None, None, on
        la, lc = _split_alias(m.group(1))
        ra, rc = _split_alias(m.group(2))
        if not (is_simple_identifier(lc) and is_simple_identifier(rc)):
            return None, None, on
        l_parent = la in parent_aliases or (la is None and allow_bare)
        r_parent = ra in parent_aliases or (ra is None and allow_bare)
        if child_alias == la and r_parent:
            from_cols.append(rc)
            to_cols.append(lc)
        elif child_alias == ra and l_parent:
            from_cols.append(lc)
            to_cols.append(rc)
        else:
            return None, None, on
    return from_cols, to_cols, None


def _split_alias(operand: str) -> tuple[str | None, str]:
    operand = trim(operand)
    dot = operand.find(".")
    if dot >= 0:
        return trim(operand[:dot]), trim(operand[dot + 1 :])
    return None, operand


def _convert_dimension(
    dim: dict[str, Any],
    alias_to_dataset: dict[str, str],
    fact_name: str,
    scope: _DialectScope,
) -> tuple[str, dict[str, Any]]:
    name = require_str(dim, "name", "dimension")
    expr = require_str(dim, "expr", f"dimension '{name}'")
    ds_name, ossie_expr = _resolve_column(expr, alias_to_dataset, fact_name)
    field: dict[str, Any] = {
        "name": name,
        "expression": _dialect_expr(
            ossie_expr, scope.dialect_of(ossie_expr, name, False)
        ),
    }
    if truthy(dim.get("comment")):
        field["description"] = dim.get("comment")
    if truthy(dim.get("display_name")):
        field["label"] = dim.get("display_name")
    if truthy(dim.get("synonyms")):
        field["ai_context"] = {"synonyms": list(as_list(dim.get("synonyms")))}
    write_stash(field, {k: dim[k] for k in COLUMN_STASH_KEYS if k in dim})
    return ds_name, field


def _resolve_column(
    expr: str, alias_to_dataset: dict[str, str], fact_name: str
) -> tuple[str, str]:
    segments = [trim(s) for s in expr.split(".")]
    ds: str | None = None
    i = 0
    while i < len(segments) - 1 and segments[i] in alias_to_dataset:
        ds = alias_to_dataset[segments[i]]
        i += 1
    if ds is None:
        return fact_name, expr
    rest = ".".join(segments[i:])
    return (ds, rest) if is_simple_identifier(rest) else (ds, expr)


def _convert_measure(
    measure: dict[str, Any],
    alias_head: re.Pattern[str] | None,
    alias_to_dataset: dict[str, str],
    scope: _DialectScope,
    filtered: bool,
) -> dict[str, Any]:
    name = require_str(measure, "name", "measure")
    raw_expr = require_str(measure, "expr", f"measure '{name}'")
    expr = rewrite_qualifiers(raw_expr, alias_head, alias_to_dataset.get)
    portable = not filtered and "window" not in measure and "partition" not in measure
    metric: dict[str, Any] = {
        "name": name,
        "expression": _dialect_expr(
            expr, scope.dialect_of(expr, name, True) if portable else DIALECT_DATABRICKS
        ),
    }
    if truthy(measure.get("comment")):
        metric["description"] = measure.get("comment")
    if truthy(measure.get("synonyms")):
        metric["ai_context"] = {"synonyms": list(as_list(measure.get("synonyms")))}
    stash = {k: measure[k] for k in COLUMN_STASH_KEYS if k in measure}
    stash.update({k: measure[k] for k in MEASURE_STASH_KEYS if k in measure})
    write_stash(metric, stash)
    return metric


def _dialect_expr(expr: str, label: str) -> dict[str, Any]:
    return {"dialects": [{"dialect": label, "expression": expr}]}


def _is_wildcard(col: dict[str, Any]) -> bool:
    return "name" not in col


def _view_defined_names(view: dict[str, Any], dims: list[Any]) -> set[str]:
    names: set[str] = set()
    for p in as_list(view.get("parameters")):
        name = str_(as_map(p).get("name"))
        if name is not None:
            names.add(name.lower())
    for d in dims:
        dim = as_map(d)
        name = str_(dim.get("name"))
        expr = str_(dim.get("expr"))
        if name is not None and (
            expr is None or trim(expr).lower() != trim(name).lower()
        ):
            names.add(name.lower())
    return names


def _has_hidden_names(dims: list[Any]) -> bool:
    for d in dims:
        dim = as_map(d)
        expr = str_(dim.get("expr"))
        if _is_wildcard(dim) and (expr is None or not FACT_WILDCARD_RE.fullmatch(expr)):
            return True
    return False


@dataclass(frozen=True)
class _DialectScope:
    datasets: set[str]
    view_names: set[str]
    hidden_names: bool

    def dialect_of(self, expr: str, self_name: str, measure: bool) -> str:
        if self.hidden_names:
            return DIALECT_DATABRICKS
        opaque = set(self.view_names)
        opaque.discard(self_name.lower())
        return classify(expr, self.datasets, opaque, measure)


def dialect_scope(view: dict[str, Any], model: dict[str, Any]) -> _DialectScope:
    """The scope ``view``'s import labelled its expressions' dialects with, so
    a field added after the conversion is labelled the same way."""
    dims = as_list(view.get("dimensions")) or as_list(view.get("fields"))
    names = {
        str(str_(as_map(d).get("name"))).lower() for d in as_list(model.get("datasets"))
    }
    return _DialectScope(
        names, _view_defined_names(view, dims), _has_hidden_names(dims)
    )
