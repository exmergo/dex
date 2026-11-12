# Ported from apache/ossie-converters, databricks/java at 82006ba2
# (OssieToMetricView.java). Licensed under the Apache License, Version 2.0.
"""EXPORT: an Apache Ossie semantic model to a Metric View v1.1 body.

The harder direction: it reassembles a relationship graph into a join tree.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ._java import JavaHashSet, java_str, trim
from .common import (
    CARD_ONE_TO_MANY,
    MAX_JOIN_NODES,
    MV_VERSION,
    OSSIE_VERSION,
    STASH_SOURCE_KEY,
    SYNONYM_LIMIT,
    ConversionError,
    Notices,
    as_list,
    as_map,
    cat,
    find_outside_literals,
    foreign_vendor_extensions,
    is_simple_identifier,
    merge_description,
    parse_root,
    pick_expression,
    qualifier_chain_pattern,
    read_stash,
    replace_outside_literals,
    require,
    require_str,
    rewrite_qualifiers,
    str_,
    str_list,
    synonyms_of,
    truthy,
    validate_source,
)


@dataclass
class _Node:
    dataset: str
    rel: dict[str, Any]  # empty for the fact
    parent_is_from: bool
    alias: str = ""
    is_otm: bool = False
    children: list[_Node] = field(default_factory=list)


@dataclass
class Exported:
    """An export's result: the Metric View body and the dataset it is on."""

    view: dict[str, Any]
    fact: str


def convert_ossie_to_metric_view(
    osi_yaml: str, source: str | None, notices: Notices
) -> Exported:
    """``convertOssieToMetricView``: parse ``osi_yaml`` and convert it."""
    root = parse_root(osi_yaml, "Apache Ossie")
    return convert_ossie_value(root, source, notices)


def check_root(root: dict[str, Any]) -> None:
    """The converter's document-level refusals, before any conversion."""
    version_value = root.get("version")
    version = java_str(version_value)
    if version_value is None or not trim(version or ""):
        raise ConversionError.invalid_input(
            "Invalid Apache Ossie YAML: missing required 'version' field",
            "it is missing the required 'version' field",
        )
    if version != OSSIE_VERSION:
        raise ConversionError.unsupported(
            f"Unsupported Apache Ossie version '{version}'. Supported: {OSSIE_VERSION}",
            version or "",
        )
    if "semantic_model" in root:
        reason = (
            "Legacy 'semantic_model' wrappers are not supported; place the model "
            "properties directly at the document root"
        )
        raise ConversionError.invalid_input(reason, reason)
    if "dialects" in root or "vendors" in root:
        reason = (
            "Root 'dialects' and 'vendors' are not supported by the Apache Ossie spec"
        )
        raise ConversionError.invalid_input(reason, reason)
    if not isinstance(root.get("name"), str):
        reason = "Apache Ossie model requires a string 'name' at the document root"
        raise ConversionError.invalid_input(reason, reason)


def convert_ossie_value(
    root: dict[str, Any], source: str | None, notices: Notices
) -> Exported:
    """The conversion of an already parsed Ossie document."""
    check_root(root)
    return _convert_model(root, source, notices)


# --- the model ------------------------------------------------------------------------


def _schema_list(parent: dict[str, Any], key: str, scope: str) -> list[Any]:
    if key not in parent:
        return []
    value = parent.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        reason = f"{scope}: '{key}' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    return value


def _schema_map_list(
    parent: dict[str, Any], key: str, scope: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, element in enumerate(_schema_list(parent, key, scope)):
        if not isinstance(element, dict):
            reason = f"{scope}: '{key}[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
        out.append(element)
    return out


@dataclass
class _Model:
    """What the conversion reads from a model before building the join tree."""

    name: str
    datasets: dict[str, dict[str, Any]]
    fields: dict[str, list[dict[str, Any]]]
    relationships: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    stash: dict[str, Any]


def _read_model(model: dict[str, Any], notices: Notices) -> _Model:
    name = str_(model.get("name")) if "name" in model else "<unnamed>"
    name = name if name is not None else "null"
    model_scope = f"Model '{name}'"
    dataset_list = _schema_map_list(model, "datasets", model_scope)
    if not dataset_list:
        raise ConversionError(f"Model '{name}' has no datasets")
    seen: set[str] = set()
    datasets: dict[str, dict[str, Any]] = {}
    fields: dict[str, list[dict[str, Any]]] = {}
    for d in dataset_list:
        ds_name = require_str(d, "name", f"Model '{name}': dataset")
        key = trim(ds_name).lower()
        if key in seen:
            raise ConversionError(f"Model '{name}': duplicate dataset name '{ds_name}'")
        seen.add(key)
        datasets[ds_name] = d
        fields[ds_name] = _schema_map_list(d, "fields", f"Dataset '{ds_name}'")
    relationships = _schema_map_list(model, "relationships", model_scope)
    metrics = _schema_map_list(model, "metrics", model_scope)
    stash = read_stash(model, notices, model_scope)
    return _Model(name, datasets, fields, relationships, metrics, stash)


def resolve_fact(
    root: dict[str, Any], explicit_source: str | None, notices: Notices | None = None
) -> str:
    """The dataset an export would put the view on, without converting it."""
    check_root(root)
    scratch = Notices() if notices is None else notices
    read = _read_model(root, scratch)
    fact_hint = explicit_source or str_(read.stash.get(STASH_SOURCE_KEY))
    _, fact = _build_join_tree(
        read.name, read.datasets, read.relationships, fact_hint, scratch
    )
    return fact


def _convert_model(
    model: dict[str, Any], explicit_source: str | None, notices: Notices
) -> Exported:
    read = _read_model(model, notices)
    name = read.name
    datasets = read.datasets
    dataset_fields = read.fields
    model_stash = read.stash
    fact_hint = explicit_source or str_(model_stash.get(STASH_SOURCE_KEY))
    root, fact = _build_join_tree(
        name, datasets, read.relationships, fact_hint, notices
    )
    counts = _assign_aliases(root, fact)
    _mark_otm(name, root)

    fact_ds = datasets[fact]
    view: dict[str, Any] = {
        "version": MV_VERSION,
        "source": validate_source(fact_ds.get("source"), fact),
    }
    comment = str_(model.get("description"))
    if truthy(comment):
        view["comment"] = comment
    if "filter" in model_stash:
        view["filter"] = model_stash["filter"]

    joins = [_build_join(child, "source", datasets, notices) for child in root.children]
    if joins:
        view["joins"] = joins

    dropped_dims = JavaHashSet()
    dropped_measures = JavaHashSet()
    dimensions: list[dict[str, Any]] = []
    seen_dims: set[str] = set()
    dataset_alias_path: dict[str, str] = {}
    for node, join_path in _node_order(root):
        is_fact = node is root
        prefix = node.alias if counts[node.dataset] > 1 else None
        qualifier = ".".join(join_path)
        if not is_fact:
            dataset_alias_path.setdefault(node.dataset, qualifier)
        for fld in dataset_fields[node.dataset]:
            fname = require_str(fld, "name", f"dataset '{node.dataset}': field")
            if node.is_otm:
                notices.warn(
                    f"field '{fname}'",
                    "column on a one-to-many-joined table cannot be a dimension (must "
                    "resolve to one value per source row); dropped",
                )
                dropped_dims.add(fname)
                continue
            dim = _convert_field(fld, fname, qualifier, is_fact, prefix, notices)
            if dim is None:
                dropped_dims.add(fname)
                continue
            dn = dim["name"]
            if dn.lower() in seen_dims:
                raise ConversionError(
                    f"dataset '{node.dataset}': dimension name '{dn}' collides with "
                    "another dimension/measure; Metric Views require unique "
                    "dimension/measure names -- rename before use"
                )
            seen_dims.add(dn.lower())
            dimensions.append(dim)

    measures: list[dict[str, Any]] = []
    fact_qualifier = re.compile(r"\b" + re.escape(fact) + r"\.", re.ASCII)
    dataset_head = qualifier_chain_pattern(dataset_alias_path.keys())
    diamond_datasets = {j for j in dataset_alias_path if counts.get(j, 1) > 1}
    for metric in read.metrics:
        measure = _convert_metric(
            metric,
            fact_qualifier,
            dataset_head,
            dataset_alias_path,
            diamond_datasets,
            seen_dims,
            notices,
        )
        if measure is None:
            dropped_measures.add(str_(metric.get("name")))
            continue
        measures.append(measure)

    _CascadeDropper(dimensions, measures, dropped_dims, dropped_measures, notices).run()

    if not dimensions and not measures:
        msg = (
            f"Model '{name}' produced no dimensions or measures; a Metric View "
            "requires at least one."
        )
        if len(dropped_dims) or len(dropped_measures):
            msg += " Dropped during conversion:"
            if len(dropped_dims):
                msg += " dimensions " + _sorted_names(dropped_dims)
            if len(dropped_measures):
                msg += " measures " + _sorted_names(dropped_measures)
            msg += " -- see the warnings for why each was dropped."
        raise ConversionError(msg)

    if dimensions:
        view["dimensions"] = dimensions
    if measures:
        view["measures"] = measures
    if "parameters" in model_stash:
        view["parameters"] = model_stash["parameters"]
    if "materialization" in model_stash:
        view["materialization"] = model_stash["materialization"]

    _warn_dropped_model(model, notices)
    return Exported(view, fact)


def _sorted_names(names: JavaHashSet) -> str:
    return "[" + ", ".join(sorted(n for n in names if n is not None)) + "]"


# --- the join tree --------------------------------------------------------------------


def _build_join_tree(
    model_name: str,
    datasets: dict[str, dict[str, Any]],
    relationships0: list[dict[str, Any]],
    fact_hint: str | None,
    notices: Notices,
) -> tuple[_Node, str]:
    for rel in relationships0:
        rel_name = cat(rel.get("name")) if "name" in rel else "<unnamed>"
        scope = f"Model '{model_name}': relationship '{rel_name}'"
        f = require(rel, "from", scope)
        t = require(rel, "to", scope)
        if str_(f) not in datasets or str_(t) not in datasets:
            raise ConversionError(
                f"Model '{model_name}': relationship '{cat(rel.get('name'))}' "
                "references an unknown dataset"
            )
        for key in ("from_columns", "to_columns"):
            if rel.get(key) is not None and not isinstance(rel.get(key), list):
                raise ConversionError(
                    f"{scope}: from_columns and to_columns must be lists"
                )
    relationships = [_orient_by_key(rel, datasets, notices) for rel in relationships0]
    if len(datasets) > MAX_JOIN_NODES:
        raise ConversionError(
            f"Model '{model_name}' has {len(datasets)} datasets; at most "
            f"{MAX_JOIN_NODES} are supported."
        )
    fact = _pick_fact(model_name, datasets, relationships, fact_hint)
    _reject_directed_cycle(model_name, datasets, relationships)

    adj: dict[str, list[str]] = {n: [] for n in datasets}
    for rel in relationships:
        adj[str(str_(rel.get("from")))].append(str(str_(rel.get("to"))))
        adj[str(str_(rel.get("to")))].append(str(str_(rel.get("from"))))
    dist = {fact: 0}
    queue = deque([fact])
    while queue:
        cur = queue.popleft()
        for nb in adj[cur]:
            if nb not in dist:
                dist[nb] = dist[cur] + 1
                queue.append(nb)
    unreachable = sorted(n for n in datasets if n not in dist)
    if unreachable:
        raise ConversionError(
            f"Model '{model_name}': datasets [{', '.join(unreachable)}] are not "
            f"reachable from fact '{fact}' via relationships."
        )
    children_of: dict[str, list[tuple[str, dict[str, Any], bool]]] = {
        n: [] for n in datasets
    }
    for rel in relationships:
        a = str(str_(rel.get("from")))
        b = str(str_(rel.get("to")))
        if dist[a] == dist[b]:
            raise ConversionError(
                f"Model '{model_name}': relationship '{cat(rel.get('name'))}' joins "
                "two datasets equidistant from the fact; the graph is not tree-shaped "
                "(it contains a cycle)."
            )
        parent, child = (a, b) if dist[a] < dist[b] else (b, a)
        children_of[parent].append((child, rel, parent == str_(rel.get("from"))))
    counter = [0]
    root = _build(model_name, fact, {}, False, children_of, counter)
    return root, fact


def _build(
    model_name: str,
    dataset: str,
    rel: dict[str, Any],
    parent_is_from: bool,
    children_of: dict[str, list[tuple[str, dict[str, Any], bool]]],
    counter: list[int],
) -> _Node:
    counter[0] += 1
    if counter[0] > MAX_JOIN_NODES:
        raise ConversionError(
            f"Model '{model_name}': join graph fans out to more than {MAX_JOIN_NODES} "
            "joins; check for an unintended diamond explosion."
        )
    node = _Node(dataset, rel, parent_is_from)
    for child, crel, is_from in children_of[dataset]:
        node.children.append(
            _build(model_name, child, crel, is_from, children_of, counter)
        )
    return node


def _assign_aliases(root: _Node, fact: str) -> dict[str, int]:
    counts: dict[str, int] = {}

    def count(node: _Node) -> None:
        counts[node.dataset] = counts.get(node.dataset, 0) + 1
        for c in node.children:
            count(c)

    count(root)
    used = {"source"}

    def assign(node: _Node, parent_alias: str | None) -> None:
        if node.dataset == fact:
            alias = "source"
        else:
            if counts[node.dataset] == 1:
                base = node.dataset
            elif parent_alias is not None and parent_alias != "source":
                base = f"{parent_alias}_{node.dataset}"
            else:
                base = node.dataset
            alias = base
            n = 2
            while alias in used:
                alias = f"{base}_{n}"
                n += 1
        node.alias = alias
        used.add(alias)
        for c in node.children:
            assign(c, alias)

    assign(root, None)
    return counts


def _pick_fact(
    model_name: str,
    datasets: dict[str, dict[str, Any]],
    relationships: list[dict[str, Any]],
    fact_hint: str | None,
) -> str:
    if fact_hint is not None:
        if fact_hint not in datasets:
            raise ConversionError(
                f"Model '{model_name}': requested source '{fact_hint}' is not a dataset"
            )
        return fact_hint
    if len(datasets) > 1 and not relationships:
        raise ConversionError(
            f"Model '{model_name}': {len(datasets)} datasets but no relationships; "
            "cannot determine the fact table."
        )
    incoming = dict.fromkeys(datasets, 0)
    for rel in relationships:
        to = str_(rel.get("to"))
        incoming[str(to)] = incoming.get(str(to), 0) + 1
    roots = [n for n, c in incoming.items() if c == 0]
    if not roots:
        raise ConversionError(
            f"Model '{model_name}': join graph contains a cycle (no root dataset). A "
            "Metric View requires an acyclic, tree-shaped graph."
        )
    if len(roots) > 1:
        raise ConversionError(
            f"Model '{model_name}': multiple candidate fact datasets "
            f"[{', '.join(sorted(roots))}]. Name the grain with --source."
        )
    return roots[0]


def _mark_otm(model_name: str, root: _Node) -> None:
    for top in root.children:
        branch_is_otm = not top.parent_is_from
        top.is_otm = branch_is_otm
        _mark_otm_visit(model_name, top, branch_is_otm)


def _mark_otm_visit(model_name: str, node: _Node, branch_is_otm: bool) -> None:
    for child in node.children:
        is_otm = not child.parent_is_from
        if is_otm != branch_is_otm:
            raise ConversionError(
                f"Model '{model_name}': join '{child.alias}' is "
                f"{_cardinality_name(is_otm)} but descends from a "
                f"{_cardinality_name(branch_is_otm)} join; a Metric View requires "
                "every join within one top-level branch to share the same cardinality."
            )
        child.is_otm = branch_is_otm
        _mark_otm_visit(model_name, child, branch_is_otm)


def _cardinality_name(is_otm: bool) -> str:
    return "one_to_many" if is_otm else "many_to_one"


def _reject_directed_cycle(
    model_name: str,
    datasets: dict[str, dict[str, Any]],
    relationships: list[dict[str, Any]],
) -> None:
    out: dict[str, list[str]] = {n: [] for n in datasets}
    for rel in relationships:
        out[str(str_(rel.get("from")))].append(str(str_(rel.get("to"))))
    done: set[str] = set()
    on_stack: dict[str, None] = {}

    def find(node: str) -> list[str] | None:
        if node in done:
            return None
        if node in on_stack:
            stack = list(on_stack)
            return [*stack[stack.index(node) :], node]
        on_stack[node] = None
        for nxt in out[node]:
            cycle = find(nxt)
            if cycle is not None:
                return cycle
        del on_stack[node]
        done.add(node)
        return None

    for n in datasets:
        cycle = find(n)
        if cycle is not None:
            raise ConversionError(
                f"Model '{model_name}': relationships form a directed cycle "
                f"{' -> '.join(cycle)}; a Metric View join graph must be acyclic."
            )


def _node_order(root: _Node) -> list[tuple[_Node, list[str]]]:
    order: list[tuple[_Node, list[str]]] = []

    def visit(node: _Node, path: list[str]) -> None:
        order.append((node, list(path)))
        for child in node.children:
            visit(child, [*path, child.alias])

    visit(root, [])
    return order


def _build_join(
    node: _Node,
    parent_alias: str,
    datasets: dict[str, dict[str, Any]],
    notices: Notices,
) -> dict[str, Any]:
    rel = node.rel
    alias = node.alias
    join: dict[str, Any] = {
        "name": alias,
        "source": validate_source(datasets[node.dataset].get("source"), node.dataset),
    }
    stash = read_stash(rel, notices, f"join '{alias}'")
    from_cols = str_list(rel.get("from_columns"))
    to_cols = str_list(rel.get("to_columns"))
    if "on" in stash:
        join["on"] = stash["on"]
    else:
        _validate_join_columns(rel, from_cols, to_cols)
        parent_cols = from_cols if node.parent_is_from else to_cols
        child_cols = to_cols if node.parent_is_from else from_cols
        if parent_cols == child_cols:
            join["using"] = list(parent_cols)
        else:
            join["on"] = " AND ".join(
                f"{parent_alias}.{p} = {alias}.{c}"
                for p, c in zip(parent_cols, child_cols, strict=True)
            )
    if "rely" in stash:
        join["rely"] = stash["rely"]
    elif node.parent_is_from and _covers_unique_key(datasets[node.dataset], to_cols):
        join["rely"] = {"at_most_one_match": True}
    if "cardinality" in stash:
        join["cardinality"] = stash["cardinality"]
    elif not node.parent_is_from:
        join["cardinality"] = CARD_ONE_TO_MANY
    nested = [_build_join(c, alias, datasets, notices) for c in node.children]
    if nested:
        join["joins"] = nested
    return join


def _covers_unique_key(dataset: dict[str, Any], join_cols: list[str]) -> bool:
    cols = set(join_cols)
    keys: list[list[str]] = []
    pk = str_list(dataset.get("primary_key"))
    if pk:
        keys.append(pk)
    keys.extend(str_list(k) for k in as_list(dataset.get("unique_keys")))
    return any(key and cols.issuperset(key) for key in keys)


def _orient_by_key(
    rel: dict[str, Any], datasets: dict[str, dict[str, Any]], notices: Notices
) -> dict[str, Any]:
    from_cols = str_list(rel.get("from_columns"))
    to_cols = str_list(rel.get("to_columns"))
    if not from_cols or not to_cols:
        return rel
    to_ds = datasets[str(str_(rel.get("to")))]
    to_has_keys = bool(str_list(to_ds.get("primary_key"))) or bool(
        as_list(to_ds.get("unique_keys"))
    )
    from_covers = _covers_unique_key(datasets[str(str_(rel.get("from")))], from_cols)
    scope = f"relationship '{cat(rel.get('name'))}'"
    if from_covers and to_has_keys and not _covers_unique_key(to_ds, to_cols):
        notices.warn(
            scope,
            "from/to looks mislabeled (the `from` columns are a declared key, the `to` "
            "columns are not); re-orienting so the key side is the `to`/one side",
        )
        swapped = dict(rel)
        swapped["from"] = rel.get("to")
        swapped["to"] = rel.get("from")
        swapped["from_columns"] = to_cols
        swapped["to_columns"] = from_cols
        return swapped
    if from_covers and not to_has_keys:
        notices.warn(
            scope,
            "the `from` columns are a declared key but the `to` side declares none, so "
            "from/to orientation can't be verified; using it as-is -- check the join "
            "direction if the resulting cardinality looks inverted",
        )
    return rel


def _validate_join_columns(
    rel: dict[str, Any], from_cols: list[str], to_cols: list[str]
) -> None:
    name = cat(rel.get("name"))
    if not from_cols or not to_cols:
        raise ConversionError(
            f"Relationship '{name}': from_columns and to_columns are required"
        )
    if len(from_cols) != len(to_cols):
        raise ConversionError(
            f"Relationship '{name}': from_columns ({len(from_cols)}) and to_columns "
            f"({len(to_cols)}) must have the same length"
        )


# --- columns --------------------------------------------------------------------------


def _convert_field(
    fld: dict[str, Any],
    name0: str,
    qualifier: str,
    is_fact: bool,
    prefix: str | None,
    notices: Notices,
) -> dict[str, Any] | None:
    scope = f"field '{name0}'"
    expr = pick_expression(fld.get("expression"), scope)
    if expr is None:
        notices.warn(
            scope, "no DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 dialect; dropping field"
        )
        return None
    if not is_fact:
        if is_simple_identifier(expr):
            expr = f"{qualifier}.{expr}"
        elif prefix is not None:
            notices.warn(
                scope,
                "complex expression on a fanned-out (diamond) join cannot be "
                "unambiguously qualified; dropped",
            )
            return None
        else:
            notices.warn(
                scope,
                "complex expression on a joined table; emitted as-is, verify "
                "qualification",
            )
    name = f"{prefix}_{name0}" if prefix is not None else name0
    dim: dict[str, Any] = {"name": name, "expr": expr}
    comment = merge_description(fld.get("description"), fld.get("ai_context"))
    if truthy(comment):
        dim["comment"] = comment
    label = str_(fld.get("label"))
    if truthy(label):
        dim["display_name"] = label
    syns = synonyms_of(fld.get("ai_context"))
    if syns:
        dim["synonyms"] = _truncate_synonyms(syns, scope, notices)
    stash = read_stash(fld, notices, scope)
    if "format" in stash:
        dim["format"] = stash["format"]
    _warn_dropped_field(fld, scope, notices)
    return dim


def _convert_metric(
    metric: dict[str, Any],
    fact_qualifier: re.Pattern[str],
    dataset_head: re.Pattern[str] | None,
    dataset_alias_path: dict[str, str],
    diamond_datasets: set[str],
    seen_names: set[str],
    notices: Notices,
) -> dict[str, Any] | None:
    name = require_str(metric, "name", "metric")
    scope = f"metric '{name}'"
    if name.lower() in seen_names:
        raise ConversionError(
            f"metric '{name}' collides with another dimension/measure; Metric Views "
            "require unique dimension/measure names -- rename before use"
        )
    seen_names.add(name.lower())
    expr = pick_expression(metric.get("expression"), scope)
    if expr is None:
        notices.warn(
            scope, "no DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 dialect; dropping metric"
        )
        return None
    diamond = _first_diamond_reference(expr, dataset_head, diamond_datasets)
    if diamond is not None:
        notices.warn(
            scope,
            f"expression references dataset '{diamond}', reached by more than one join "
            "path (diamond); cannot be unambiguously qualified; dropped",
        )
        return None
    expr = rewrite_qualifiers(expr, dataset_head, dataset_alias_path.get)
    expr = replace_outside_literals(expr, fact_qualifier, "")
    measure: dict[str, Any] = {"name": name, "expr": expr}
    comment = merge_description(metric.get("description"), metric.get("ai_context"))
    if truthy(comment):
        measure["comment"] = comment
    syns = synonyms_of(metric.get("ai_context"))
    if syns:
        measure["synonyms"] = _truncate_synonyms(syns, scope, notices)
    stash = read_stash(metric, notices, scope)
    for key in ("format", "window", "partition", "display_name"):
        if key in stash:
            measure[key] = stash[key]
    _warn_dropped_column(metric, scope, notices)
    return measure


def _first_diamond_reference(
    expr: str, dataset_head: re.Pattern[str] | None, diamond_datasets: set[str]
) -> str | None:
    if dataset_head is None or not diamond_datasets:
        return None
    found: list[str] = []

    def detect(leaf: str) -> str | None:
        if not found and leaf in diamond_datasets:
            found.append(leaf)
        return None

    rewrite_qualifiers(expr, dataset_head, detect)
    return found[0] if found else None


class _CascadeDropper:
    """Propagates dropped field and metric references, in the converter's phase
    order: dimensions then measures, each in source order, with a dependency
    found after its source position deferred to the next round."""

    _SEED, _DIMENSION, _MEASURE = 0, 1, 2

    def __init__(
        self,
        dimensions: list[dict[str, Any]],
        measures: list[dict[str, Any]],
        dropped_dims: JavaHashSet,
        dropped_measures: JavaHashSet,
        notices: Notices,
    ) -> None:
        self.dimensions = dimensions
        self.measures = measures
        self.dropped_dims = dropped_dims
        self.dropped_measures = dropped_measures
        self.notices = notices
        self.removed_dims: set[int] = set()
        self.removed_measures: set[int] = set()
        self.patterns: dict[tuple[str, bool], re.Pattern[str]] = {}
        self.dims_now: set[int] = set()
        self.dims_next: set[int] = set()
        self.measures_now: set[int] = set()
        self.measures_next: set[int] = set()

    def run(self) -> None:
        for name in list(self.dropped_measures):
            self._propagate(name, True, self._SEED, -1)
        for name in list(self.dropped_dims):
            self._propagate(name, False, self._SEED, -1)
        while self.dims_now or self.measures_now:
            self._process(False)
            self._process(True)
            self.dims_now, self.dims_next = self.dims_next, set()
            self.measures_now, self.measures_next = self.measures_next, set()
        self.dimensions[:] = [
            c for i, c in enumerate(self.dimensions) if i not in self.removed_dims
        ]
        self.measures[:] = [
            c for i, c in enumerate(self.measures) if i not in self.removed_measures
        ]

    def _process(self, measure: bool) -> None:
        # BitSet.nextSetBit walks ascending, and sees bits set behind the cursor
        # only on a later pass; bits set ahead of it are visited in this one.
        now = self.measures_now if measure else self.dims_now
        index = _next_set(now, 0)
        while index is not None:
            now.discard(index)
            self._drop(index, measure, self._MEASURE if measure else self._DIMENSION)
            index = _next_set(now, index + 1)

    def _drop(self, index: int, measure: bool, phase: int) -> None:
        removed = self.removed_measures if measure else self.removed_dims
        if index in removed:
            return
        column = (self.measures if measure else self.dimensions)[index]
        name = column["name"]
        reference = self._references_dropped(column["expr"], name, measure)
        if reference is None:
            return
        removed.add(index)
        kind = "measure" if measure else "dimension"
        self.notices.warn(
            f"{kind} '{name}'",
            f"references dropped '{reference}'; dropping (downstream of a dropped "
            "field/metric)",
        )
        dropped = self.dropped_measures if measure else self.dropped_dims
        if dropped.add(name):
            self._propagate(name, measure, phase, index)

    def _propagate(
        self, name: str | None, measure_reference: bool, phase: int, source_index: int
    ) -> None:
        if name is None:
            return
        pattern = self._pattern(name, measure_reference)
        for index, column in enumerate(self.dimensions):
            if not self._pending(index, False) and _matches(
                column, name, measure_reference, False, pattern
            ):
                self._schedule(index, False, phase, source_index)
        for index, column in enumerate(self.measures):
            if not self._pending(index, True) and _matches(
                column, name, measure_reference, True, pattern
            ):
                self._schedule(index, True, phase, source_index)

    def _pending(self, index: int, measure: bool) -> bool:
        if measure:
            return (
                index in self.removed_measures
                or index in self.measures_now
                or index in self.measures_next
            )
        return (
            index in self.removed_dims
            or index in self.dims_now
            or index in self.dims_next
        )

    def _schedule(
        self, index: int, measure: bool, phase: int, source_index: int
    ) -> None:
        if measure:
            if phase != self._MEASURE or index > source_index:
                self.measures_now.add(index)
            else:
                self.measures_next.add(index)
        elif phase == self._SEED or (phase == self._DIMENSION and index > source_index):
            self.dims_now.add(index)
        else:
            self.dims_next.add(index)

    def _references_dropped(
        self, expr: str, self_name: str, self_is_measure: bool
    ) -> str | None:
        lower_expr = expr.lower()
        for name in self.dropped_measures:
            if name is None or _is_self_reference(
                self_is_measure, True, self_name, name
            ):
                continue
            if name.lower() in lower_expr and find_outside_literals(
                expr, self._pattern(name, True)
            ):
                return name
        for name in self.dropped_dims:
            if name is None or _is_self_reference(
                self_is_measure, False, self_name, name
            ):
                continue
            if name.lower() in lower_expr and find_outside_literals(
                expr, self._pattern(name, False)
            ):
                return name
        return None

    def _pattern(self, name: str, measure: bool) -> re.Pattern[str]:
        key = (name, measure)
        if key not in self.patterns:
            body = (
                r"measure\(\s*" + re.escape(name) + r"\s*\)"
                if measure
                else r"(?<![\w.])" + re.escape(name) + r"(?![\w.])(?!\s*\()"
            )
            self.patterns[key] = re.compile(body, re.IGNORECASE | re.ASCII)
        return self.patterns[key]


def _next_set(bits: set[int], start: int) -> int | None:
    candidates = [i for i in bits if i >= start]
    return min(candidates) if candidates else None


def _is_self_reference(
    column_is_measure: bool,
    dropped_is_measure: bool,
    column_name: str,
    dropped_name: str,
) -> bool:
    return (
        column_is_measure == dropped_is_measure
        and dropped_name.lower() == column_name.lower()
    )


def _matches(
    column: dict[str, Any],
    reference: str,
    measure_reference: bool,
    column_is_measure: bool,
    pattern: re.Pattern[str],
) -> bool:
    if _is_self_reference(
        column_is_measure, measure_reference, column["name"], reference
    ):
        return False
    expr = column["expr"]
    return reference.lower() in expr.lower() and find_outside_literals(expr, pattern)


# --- notices for what has no Metric View slot -----------------------------------------


def _truncate_synonyms(syns: list[str], scope: str, notices: Notices) -> list[str]:
    if len(syns) > SYNONYM_LIMIT:
        notices.warn(
            scope,
            f"{len(syns)} synonyms exceeds Metric View limit; keeping first "
            f"{SYNONYM_LIMIT}",
        )
        return list(syns[:SYNONYM_LIMIT])
    return syns


def _warn_dropped_model(model: dict[str, Any], notices: Notices) -> None:
    if foreign_vendor_extensions(model):
        notices.warn("model", "foreign-vendor custom_extensions dropped")
    if truthy(model.get("ai_context")):
        notices.warn(
            "model",
            "model-level ai_context dropped (only the description maps to the view "
            "comment)",
        )
    for ds_obj in as_list(model.get("datasets")):
        ds = as_map(ds_obj)
        scope = f"dataset '{cat(ds.get('name'))}'"
        if str_list(ds.get("primary_key")) or as_list(ds.get("unique_keys")):
            notices.warn(
                scope,
                "primary_key/unique_keys not stored as columns; used to set "
                "rely.at_most_one_match on a matching many_to_one join where "
                "applicable",
            )
        if isinstance(ds.get("ai_context"), dict) and ds.get("ai_context"):
            notices.warn(scope, "dataset-level ai_context (object) dropped")
        if truthy(ds.get("description")):
            notices.warn(
                scope, "dataset-level description dropped (no per-source comment field)"
            )
        if foreign_vendor_extensions(ds):
            notices.warn(scope, "foreign-vendor custom_extensions dropped")
    for rel_obj in as_list(model.get("relationships")):
        rel = as_map(rel_obj)
        rn = cat(rel.get("name")) if "name" in rel else "<unnamed>"
        if truthy(rel.get("ai_context")):
            notices.warn(f"relationship '{rn}'", "relationship ai_context dropped")
        if foreign_vendor_extensions(rel):
            notices.warn(
                f"relationship '{rn}'", "foreign-vendor custom_extensions dropped"
            )


def _warn_dropped_field(fld: dict[str, Any], scope: str, notices: Notices) -> None:
    dim = fld.get("dimension")
    if isinstance(dim, dict) and "is_time" in dim:
        notices.warn(scope, "dimension.is_time has no Metric View counterpart; dropped")
    _warn_dropped_column(fld, scope, notices)


def _warn_dropped_column(column: dict[str, Any], scope: str, notices: Notices) -> None:
    if column.get("datatype") is not None:
        notices.warn(scope, "datatype has no Metric View counterpart; dropped")
    ai_context = column.get("ai_context")
    if isinstance(ai_context, dict):
        dropped = sorted(str(java_str(k)) for k in ai_context if k != "synonyms")
        if dropped:
            notices.warn(
                scope,
                f"ai_context [{', '.join(dropped)}] dropped (only 'synonyms' has a "
                "Metric View counterpart)",
            )
    if foreign_vendor_extensions(column):
        notices.warn(scope, "foreign-vendor custom_extensions dropped")
