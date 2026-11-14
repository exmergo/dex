"""What dex adds around the ported converter, so a proposal passes dex's own
validator for the layer it targets.

The port answers what the converter answers; these passes change only what dex
has to, and each change is a notice of origin ``dex``:

- **(a) Key fields, on import.** dex's Ossie reader follows upstream's Name
  Resolution: a key or a relationship names *fields*, never columns of the
  source. The converter writes columns. A field already exposing the column is
  named instead; otherwise a field is added, named after the column, carrying
  ``{"_v": 1, "key_only": true}`` in its DATABRICKS stash, and dex's export
  leaves it out again so a round trip through dex is exact.
- **(b) Fields to columns, on export.** The converter reads key and relationship
  names as columns. A name that is a field exposing a bare column is resolved to
  that column first.
- **(c) ``expression.sql``, on export.** The converter reads only
  ``expression.dialects``; the Ossie schema's ``sql`` is the portable
  OSSIE_SQL_2026 expression and is authoritative, so dex reads it in that slot.
- **(d) Dataset-scoped metrics (#957).** On export, a metric of the fact dataset
  is a measure, and one of a joined dataset is dropped by name (the converter
  ignores both). On import, ``dataset_metrics`` moves each measure that reads
  only the fact source into ``datasets[fact].metrics``.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from . import Conversion
from .common import (
    DIALECT_OSSIE_SQL,
    VENDOR,
    Notices,
    as_list,
    as_map,
    find_outside_literals,
    is_simple_identifier,
    pick_expression,
    qualifier_chain_pattern,
    read_stash,
    replace_outside_literals,
    stash_blob,
    str_,
    str_list,
)

KEY_ONLY = "key_only"

__all__ = ["KEY_ONLY", "export_document", "import_view", "is_key_only"]


# --- import: Metric View to Ossie -----------------------------------------------------


def import_view(
    view: dict[str, Any],
    model_name: str | None = None,
    *,
    dataset_metrics: bool = False,
) -> Conversion:
    """A Metric View body as an Ossie document dex's own reader accepts."""
    from . import metric_view_value_to_ossie
    from .to_ossie import dialect_scope

    converted = metric_view_value_to_ossie(view, model_name)
    notices = Notices(list(converted.notices))
    model = copy.deepcopy(converted.value)
    scope = dialect_scope(view, model)
    _add_key_fields(model, scope, notices)
    _distinct_relationship_names(model, notices)
    _name_metric_fields(model, converted.fact, notices)
    if dataset_metrics:
        _scope_fact_metrics(model, converted.fact, notices)
    return Conversion(model, tuple(notices.items), converted.fact)


def _field_index(dataset: dict[str, Any]) -> dict[str, str]:
    """Each field's bare column (lower-cased) to the field's name, first wins."""
    out: dict[str, str] = {}
    for fld in as_list(dataset.get("fields")):
        fld = as_map(fld)
        expr = _expression_text(fld)
        name = str_(fld.get("name"))
        if name is not None and expr is not None and is_simple_identifier(expr):
            out.setdefault(expr.strip().lower(), name)
    return out


def _expression_text(item: dict[str, Any]) -> str | None:
    try:
        return pick_expression(_with_sql(item.get("expression")), "")
    except ValueError:
        return None


def _names(dataset: dict[str, Any]) -> set[str]:
    taken = {
        str(str_(as_map(f).get("name"))).lower() for f in as_list(dataset.get("fields"))
    }
    taken |= {
        str(str_(as_map(m).get("name"))).lower()
        for m in as_list(dataset.get("metrics"))
    }
    return taken


def _add_key_fields(model: dict[str, Any], scope: Any, notices: Notices) -> None:
    datasets = {str_(d.get("name")): d for d in as_list(model.get("datasets"))}
    # One notice per scope, naming every column it reads, rather than one per
    # column: a star schema has a key on each side of every join.
    named: dict[str, list[str]] = {}
    added: dict[str, list[str]] = {}

    def field_for(dataset_name: Any, column: str, used_by: str) -> str:
        dataset = datasets[dataset_name]
        index = _field_index(dataset)
        found = index.get(column.lower())
        if found is not None:
            if found != column:
                named.setdefault(used_by, []).append(
                    f"'{dataset_name}.{found}' for column '{column}'"
                )
            return found
        name = column
        taken = _names(dataset)
        if name.lower() in taken:
            base = f"{column}_key"
            name, n = base, 2
            while name.lower() in taken:
                name = f"{base}_{n}"
                n += 1
        dataset.setdefault("fields", []).append(
            {
                "name": name,
                "expression": {
                    "dialects": [
                        {
                            "dialect": scope.dialect_of(column, name, False),
                            "expression": column,
                        }
                    ]
                },
                "custom_extensions": [
                    {"vendor_name": VENDOR, "data": stash_blob({KEY_ONLY: True})}
                ],
            }
        )
        label = f"'{name}'" if name == column else f"'{name}' (column '{column}')"
        added.setdefault(f"dataset '{dataset_name}'", []).append(label)
        return name

    for rel in as_list(model.get("relationships")):
        used_by = f"relationship '{str_(rel.get('name'))}'"
        for side, columns_key in (("from", "from_columns"), ("to", "to_columns")):
            dataset_name = str_(rel.get(side))
            if dataset_name not in datasets:
                continue
            rel[columns_key] = [
                field_for(dataset_name, c, used_by)
                for c in str_list(rel.get(columns_key))
            ]
    for dataset_name, dataset in datasets.items():
        keys = as_list(dataset.get("unique_keys"))
        if keys:
            dataset["unique_keys"] = [
                [
                    field_for(dataset_name, c, f"a unique key of '{dataset_name}'")
                    for c in str_list(k)
                ]
                for k in keys
            ]
        _move_fields_last(dataset)
    for used_by, fields in named.items():
        notices.warn(
            used_by,
            "names the fields exposing its columns, since an Ossie key or relationship "
            "names fields: " + ", ".join(fields),
            "dex",
        )
    for where, fields in added.items():
        notices.warn(
            where,
            "added key_only fields " + ", ".join(fields) + ", since an Ossie key or "
            "relationship names fields and no field exposed these columns; dex's "
            "export leaves them out of the Metric View",
            "dex",
        )


def _move_fields_last(dataset: dict[str, Any]) -> None:
    # Keep the converter's key order (`unique_keys` before `fields`) where a
    # dataset gained its first field here.
    if "fields" in dataset:
        dataset["fields"] = dataset.pop("fields")


def _distinct_relationship_names(model: dict[str, Any], notices: Notices) -> None:
    seen: set[str] = set()
    for rel in as_list(model.get("relationships")):
        name = str(str_(rel.get("name")))
        if name.lower() not in seen:
            seen.add(name.lower())
            continue
        candidate, n = f"{name}_2", 3
        while candidate.lower() in seen:
            candidate = f"{name}_{n}"
            n += 1
        seen.add(candidate.lower())
        rel["name"] = candidate
        notices.warn(
            f"relationship '{name}'",
            f"renamed '{candidate}': the converter named two joins alike, and an Ossie "
            "model's relationship names are unique",
            "dex",
        )


_IDENTIFIER_REF = re.compile(
    r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?"
    r"(?![\w.])(?!\s*[('])",
    re.ASCII,
)


def _name_metric_fields(model: dict[str, Any], fact: str, notices: Notices) -> None:
    """A model metric's columns read as the fields that expose them.

    An Ossie model metric names fields as ``dataset.field``; the converter
    writes the Metric View's columns, bare for the fact source. Each column a
    field exposes becomes that field, so dex's catalog traces the metric to its
    dataset. A metric that reads another measure is left as written: a bare name
    outside an aggregate is that measure, and telling it from a column is not
    something a pattern can do.
    """
    from .dialect import CONTEXT_KEYWORDS, KEYWORDS, KEYWORDS_BEFORE_PAREN

    keywords = KEYWORDS | KEYWORDS_BEFORE_PAREN | CONTEXT_KEYWORDS
    datasets = {str_(d.get("name")): d for d in as_list(model.get("datasets"))}
    indexes = {name: _field_index(d) for name, d in datasets.items()}
    metric_names = {
        str(str_(as_map(m).get("name"))).lower() for m in as_list(model.get("metrics"))
    }
    measure_call = re.compile(r"\bmeasure\s*\(", re.IGNORECASE | re.ASCII)
    for metric in as_list(model.get("metrics")):
        metric = as_map(metric)
        dialects = as_list(as_map(metric.get("expression")).get("dialects"))
        if len(dialects) != 1 or not isinstance(
            as_map(dialects[0]).get("expression"), str
        ):
            continue
        expr = as_map(dialects[0])["expression"]
        own = str(str_(metric.get("name"))).lower()
        if find_outside_literals(expr, measure_call) or any(
            find_outside_literals(expr, _bare_reference(n))
            for n in metric_names
            if n != own
        ):
            continue
        renamed: list[str] = []

        def replace(m: re.Match[str], renamed: list[str] = renamed) -> str:
            head, tail = m.group(1), m.group(2)
            if tail is not None:
                field = indexes.get(head, {}).get(tail.lower())
                if field is None:
                    return m.group(0)
                renamed.append(f"'{head}.{tail}' -> '{head}.{field}'")
                return f"{head}.{field}"
            if head.upper() in keywords or _is_sql_word(m):
                return m.group(0)
            field = indexes.get(fact, {}).get(head.lower())
            if field is None:
                return m.group(0)
            renamed.append(f"'{head}' -> '{fact}.{field}'")
            return f"{fact}.{field}"

        rewritten = replace_outside_literals(expr, _IDENTIFIER_REF, replace)
        if rewritten == expr:
            continue
        as_map(dialects[0])["expression"] = rewritten
        notices.warn(
            f"metric '{str_(metric.get('name'))}'",
            "names the fields exposing its columns, as an Ossie model metric names "
            "fields: " + ", ".join(dict.fromkeys(renamed)),
            "dex",
        )


def _bare_reference(name: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![\w.])" + re.escape(name) + r"(?![\w.])(?!\s*\()",
        re.IGNORECASE | re.ASCII,
    )


def _scope_fact_metrics(model: dict[str, Any], fact: str, notices: Notices) -> None:
    datasets = {str_(d.get("name")): d for d in as_list(model.get("datasets"))}
    fact_ds = datasets.get(fact)
    if fact_ds is None:
        return
    joined = [n for n in datasets if n is not None and n != fact]
    joined_head = qualifier_chain_pattern(joined)
    fact_head = re.compile(r"\b" + re.escape(fact) + r"\.", re.ASCII)
    measure_call = re.compile(r"\bmeasure\s*\(", re.IGNORECASE | re.ASCII)
    kept: list[Any] = []
    moved: list[dict[str, Any]] = []
    taken = _names(fact_ds)
    for metric in as_list(model.get("metrics")):
        metric = as_map(metric)
        name = str(str_(metric.get("name")))
        dialects = as_list(as_map(metric.get("expression")).get("dialects"))
        expr = (
            str_(as_map(dialects[0]).get("expression")) if len(dialects) == 1 else None
        )
        if (
            expr is None
            or find_outside_literals(expr, measure_call)
            or (joined_head is not None and find_outside_literals(expr, joined_head))
        ):
            kept.append(metric)
            continue
        if name.lower() in taken:
            kept.append(metric)
            notices.warn(
                f"metric '{name}'",
                f"kept model-scoped: dataset '{fact}' already has a field of that "
                "name, and a dataset's fields and metrics share one namespace",
                "dex",
            )
            continue
        as_map(dialects[0])["expression"] = replace_outside_literals(
            expr, fact_head, ""
        )
        taken.add(name.lower())
        moved.append(metric)
        notices.warn(
            f"metric '{name}'",
            f"written as a metric of dataset '{fact}' (it reads only the fact source)",
            "dex",
        )
    if not moved:
        return
    fact_ds["metrics"] = moved
    if kept:
        model["metrics"] = kept
    else:
        model.pop("metrics", None)
    # The converter's key order puts the model's stash last.
    if "custom_extensions" in model:
        model["custom_extensions"] = model.pop("custom_extensions")


# --- export: Ossie to Metric View -----------------------------------------------------


def is_key_only(item: dict[str, Any]) -> bool:
    """Whether a field is one dex added on import to carry a key (see (a))."""
    try:
        stash = read_stash(item, Notices(), "")
    except ValueError:
        return False
    return stash.get(KEY_ONLY) is True


def export_document(document: dict[str, Any], fact: str | None = None) -> Conversion:
    """An Ossie document as a Metric View body dex's own reader accepts."""
    from . import ossie_value_to_metric_view
    from .to_metric_view import check_root, resolve_fact

    check_root(document)
    doc = copy.deepcopy(document)
    notices = Notices()
    _read_sql(doc, notices)
    _resolve_key_fields(doc, notices)
    _resolve_metric_fields(doc, notices)
    # Dataset metrics first: one may read a key-only field, which has to resolve
    # to its column before the field goes.
    _dataset_metrics(doc, fact, notices, resolve_fact)
    _drop_key_only(doc)
    converted = ossie_value_to_metric_view(doc, fact)
    return Conversion(
        converted.value, (*notices.items, *converted.notices), converted.fact
    )


def _with_sql(expression: Any) -> Any:
    """``expression`` with ``sql`` read into the OSSIE_SQL_2026 dialect entry."""
    if not isinstance(expression, dict):
        return expression
    sql = expression.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return expression
    out = dict(expression)
    dialects = [d for d in as_list(expression.get("dialects")) if isinstance(d, dict)]
    rest = [d for d in dialects if str_(d.get("dialect")) != DIALECT_OSSIE_SQL]
    out["dialects"] = [*rest, {"dialect": DIALECT_OSSIE_SQL, "expression": sql}]
    out.pop("sql", None)
    return out


def _items(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every field and metric of ``doc``, dataset-scoped metrics included."""
    out: list[tuple[str, dict[str, Any]]] = []
    for dataset in as_list(doc.get("datasets")):
        dataset = as_map(dataset)
        fields = as_list(dataset.get("fields"))
        metrics = as_list(dataset.get("metrics"))
        out.extend(("field", f) for f in fields if isinstance(f, dict))
        out.extend(("metric", m) for m in metrics if isinstance(m, dict))
    out.extend(
        ("metric", m) for m in as_list(doc.get("metrics")) if isinstance(m, dict)
    )
    return out


def _read_sql(doc: dict[str, Any], notices: Notices) -> None:
    """(c): ``expression.sql`` read, with one notice naming every item whose
    expression the converter alone would have read differently."""
    dropped: list[str] = []
    changed: list[str] = []
    for _, item in _items(doc):
        expression = item.get("expression")
        updated = _with_sql(expression)
        if updated is expression:
            continue
        try:
            before = pick_expression(expression, "")
        except ValueError:
            before = None
        after = pick_expression(updated, "")
        item["expression"] = updated
        if before != after:
            (dropped if before is None else changed).append(str(str_(item.get("name"))))
    if dropped:
        notices.warn(
            "model",
            "read expression.sql as the OSSIE_SQL_2026 expression of "
            f"{_listed(dropped)}, which the converter would have dropped (it reads "
            "only expression.dialects)",
            "dex",
        )
    if changed:
        notices.warn(
            "model",
            "read expression.sql as the OSSIE_SQL_2026 expression of "
            f"{_listed(changed)}, where it is authoritative over the dialects entry "
            "the converter would have read",
            "dex",
        )


def _listed(names: list[str]) -> str:
    return ", ".join(f"'{n}'" for n in names)


def _bare_columns(dataset: dict[str, Any]) -> dict[str, str | None]:
    """Each field's name (lower-cased) to its bare column, or ``None`` where its
    expression is not one."""
    out: dict[str, str | None] = {}
    for fld in as_list(dataset.get("fields")):
        fld = as_map(fld)
        name = str_(fld.get("name"))
        if name is None:
            continue
        expr = _expression_text(fld)
        out.setdefault(
            name.lower(),
            expr.strip() if expr is not None and is_simple_identifier(expr) else None,
        )
    return out


def _resolve_key_fields(doc: dict[str, Any], notices: Notices) -> None:
    """(b): each key and relationship name that is a field, read as its column."""
    datasets = {
        str_(as_map(d).get("name")): as_map(d) for d in as_list(doc.get("datasets"))
    }
    columns = {name: _bare_columns(d) for name, d in datasets.items()}
    key_only = {
        (name, str(str_(as_map(f).get("name"))).lower())
        for name, d in datasets.items()
        for f in as_list(d.get("fields"))
        if isinstance(f, dict) and is_key_only(f)
    }

    def resolve(dataset_name: Any, names: list[str], used_by: str) -> list[str]:
        resolved: list[str] = []
        renamed: list[str] = []
        opaque: list[str] = []
        known = columns.get(dataset_name, {})
        for name in names:
            if name.lower() not in known:
                resolved.append(name)
                continue
            column = known[name.lower()]
            if column is None:
                opaque.append(name)
                resolved.append(name)
                continue
            if column != name and (dataset_name, name.lower()) not in key_only:
                renamed.append(f"'{name}' -> '{column}'")
            resolved.append(column)
        if renamed:
            notices.warn(
                used_by,
                f"fields of dataset '{dataset_name}' resolved to their columns: "
                + ", ".join(renamed),
                "dex",
            )
        if opaque:
            notices.warn(
                used_by,
                f"names {_listed(opaque)} of dataset '{dataset_name}', whose "
                "expression is not a bare column; the name is kept as the join "
                "column",
                "dex",
            )
        return resolved

    for rel in as_list(doc.get("relationships")):
        rel = as_map(rel)
        used_by = f"relationship '{str_(rel.get('name'))}'"
        for side, key in (("from", "from_columns"), ("to", "to_columns")):
            if isinstance(rel.get(key), list):
                rel[key] = resolve(str_(rel.get(side)), str_list(rel[key]), used_by)
    for name, dataset in datasets.items():
        used_by = f"dataset '{name}'"
        if isinstance(dataset.get("primary_key"), list):
            dataset["primary_key"] = resolve(
                name, str_list(dataset["primary_key"]), used_by
            )
        if isinstance(dataset.get("unique_keys"), list):
            dataset["unique_keys"] = [
                resolve(name, str_list(k), used_by) for k in dataset["unique_keys"]
            ]


def _resolve_metric_fields(doc: dict[str, Any], notices: Notices) -> None:
    """(b) for a model metric: ``dataset.field`` read as ``dataset.column``.

    A model-scoped metric names fields by ``dataset.field``, and the converter
    keeps the name, which is a column only where the field is named after it. A
    field whose expression is not a bare column is left as written, named. One
    pass over the expression, so a column written in is never read again as a
    field of the same name.
    """
    datasets = {
        str_(as_map(d).get("name")): as_map(d) for d in as_list(doc.get("datasets"))
    }
    columns = {
        name.lower(): (name, _bare_columns(d))
        for name, d in datasets.items()
        if name is not None
    }
    for metric in as_list(doc.get("metrics")):
        metric = as_map(metric)
        expression = as_map(metric.get("expression"))
        renamed: list[str] = []
        opaque: list[str] = []

        def replace(
            m: re.Match[str], renamed: list[str] = renamed, opaque: list[str] = opaque
        ) -> str:
            head, tail = m.group(1), m.group(2)
            found = columns.get(head.lower()) if tail is not None else None
            if found is None or tail.lower() not in found[1]:
                return m.group(0)
            dataset_name, fields = found
            column = fields[tail.lower()]
            if column is None:
                opaque.append(f"{dataset_name}.{tail}")
                return m.group(0)
            if column.lower() == tail.lower():
                return m.group(0)
            renamed.append(f"'{dataset_name}.{tail}' -> '{column}'")
            return f"{head}.{column}"

        for entry in as_list(expression.get("dialects")):
            entry = as_map(entry)
            text = entry.get("expression")
            if isinstance(text, str):
                entry["expression"] = replace_outside_literals(
                    text, _IDENTIFIER_REF, replace
                )
        scope = f"metric '{str_(metric.get('name'))}'"
        if renamed:
            notices.warn(
                scope,
                "fields resolved to their columns: "
                + ", ".join(dict.fromkeys(renamed)),
                "dex",
            )
        if opaque:
            notices.warn(
                scope,
                f"reads {_listed(list(dict.fromkeys(opaque)))}, whose expression is "
                "not a bare column; the measure names the field, which a Metric View "
                "reads as a column of that name",
                "dex",
            )


def _drop_key_only(doc: dict[str, Any]) -> None:
    for dataset in as_list(doc.get("datasets")):
        dataset = as_map(dataset)
        fields = dataset.get("fields")
        if isinstance(fields, list):
            kept = [f for f in fields if not (isinstance(f, dict) and is_key_only(f))]
            if kept:
                dataset["fields"] = kept
            else:
                dataset.pop("fields", None)


def _dataset_metrics(
    doc: dict[str, Any], fact_hint: str | None, notices: Notices, resolve_fact: Any
) -> None:
    holders = [
        as_map(d)
        for d in as_list(doc.get("datasets"))
        if as_list(as_map(d).get("metrics"))
    ]
    if not holders:
        return
    fact = resolve_fact(doc, fact_hint)
    lifted: list[dict[str, Any]] = []
    for dataset in holders:
        dataset_name = str_(dataset.get("name"))
        for metric in as_list(dataset.pop("metrics")):
            metric = as_map(metric)
            scope = f"metric '{str_(metric.get('name'))}'"
            if dataset_name != fact:
                notices.warn(
                    scope,
                    f"a metric of joined dataset '{dataset_name}' has no Metric View "
                    f"counterpart (a measure aggregates the fact source, '{fact}'); "
                    "dropped",
                    "dex",
                )
                continue
            rewritten = _fields_to_columns(metric, dataset, scope, notices)
            if rewritten is not None:
                lifted.append(rewritten)
                notices.warn(
                    scope,
                    f"a metric of the fact dataset '{fact}', exported as a measure",
                    "dex",
                )
    if lifted:
        doc["metrics"] = [*lifted, *as_list(doc.get("metrics"))]


def _fields_to_columns(
    metric: dict[str, Any], dataset: dict[str, Any], scope: str, notices: Notices
) -> dict[str, Any] | None:
    """``metric`` with each unqualified field name replaced by its column, or
    ``None`` (and a notice) when it reads a field that is not a bare column.

    One pass over the expression, so a column written in is never read again as
    a field; a SQL keyword or date part (``EXTRACT(YEAR FROM ...)``) is never
    read as a field, whatever the dataset names one.
    """
    expression = metric.get("expression")
    try:
        expr = pick_expression(expression, scope)
    except ValueError:
        return metric
    if expr is None:
        return metric
    fields = _bare_columns(dataset)
    names = {
        str(str_(as_map(f).get("name"))).lower(): str(str_(as_map(f).get("name")))
        for f in as_list(dataset.get("fields"))
    }
    opaque: list[str] = []

    def replace(m: re.Match[str]) -> str:
        head, tail = m.group(1), m.group(2)
        if tail is not None or head.lower() not in fields:
            return m.group(0)
        if _is_sql_word(m):
            return m.group(0)
        column = fields[head.lower()]
        if column is None:
            opaque.append(names.get(head.lower(), head))
            return m.group(0)
        return column

    rewritten = replace_outside_literals(expr, _IDENTIFIER_REF, replace)
    if opaque:
        notices.warn(
            scope,
            f"reads field {_listed(list(dict.fromkeys(opaque)))}, whose expression is "
            "not a bare column, and a measure cannot name a field; dropped",
            "dex",
        )
        return None
    out = dict(metric)
    out["expression"] = {
        "dialects": [{"dialect": _chosen_dialect(expression), "expression": rewritten}]
    }
    return out


_DATE_PART_CALL = re.compile(
    r"\b(?:DATEADD|DATEDIFF|DATE_ADD|DATE_DIFF|DATE_PART|TIMESTAMPADD|TIMESTAMPDIFF)"
    r"\s*\(\s*$",
    re.IGNORECASE | re.ASCII,
)
_INTERVAL_PREFIX = re.compile(r"\bINTERVAL\s+\S+\s+$", re.IGNORECASE | re.ASCII)
_FROM_NEXT = re.compile(r"\s+FROM\b", re.IGNORECASE | re.ASCII)


def _is_sql_word(m: re.Match[str]) -> bool:
    """Whether a bare name sits where SQL reads a keyword or a date part, not a
    column: ``EXTRACT(YEAR FROM d)``, ``DATEADD(day, 1, d)``, ``INTERVAL 7 DAY``."""
    from .dialect import CONTEXT_KEYWORDS, KEYWORDS, KEYWORDS_BEFORE_PAREN

    word = m.group(1).upper()
    if word in KEYWORDS or word in KEYWORDS_BEFORE_PAREN or word in CONTEXT_KEYWORDS:
        return True
    before = m.string[: m.start()]
    after = m.string[m.end() :]
    return bool(
        _FROM_NEXT.match(after)
        or _DATE_PART_CALL.search(before)
        or _INTERVAL_PREFIX.search(before)
    )


def _chosen_dialect(expression: Any) -> str:
    for label in ("DATABRICKS", "ANSI_SQL", DIALECT_OSSIE_SQL):
        for d in as_list(as_map(expression).get("dialects")):
            if str_(as_map(d).get("dialect")) == label and as_map(d).get("expression"):
                return label
    return DIALECT_OSSIE_SQL
