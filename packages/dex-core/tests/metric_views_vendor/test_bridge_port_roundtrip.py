"""The converter's ``OssieConverterRoundTripSuite``, ported (#1260).

For any generated model in the round-trippable subset, converting one way and
back keeps its content. ``java.util.Random`` is ported too, so seed ``n`` builds
the model the Java suite builds for seed ``n``. Each property is checked twice:
over the faithful port, and over dex's bridge (the port plus dex's adaptations,
which a round trip through dex has to undo exactly).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from exmergo_dex_core.metric_views.loader import dump_yaml
from exmergo_dex_core.metric_views.ossie_bridge import (
    metric_view_to_ossie,
    metric_view_value_to_ossie,
    ossie_to_metric_view,
    ossie_value_to_metric_view,
)
from exmergo_dex_core.metric_views.ossie_bridge.adapt import (
    export_document,
    import_view,
)
from exmergo_dex_core.metric_views.ossie_bridge.common import (
    MV_VERSION,
    OSSIE_VERSION,
    parse_yaml,
)
from exmergo_dex_core.yaml_items import dump_items

NUM_SEEDS = 300
AGGS = ["SUM", "COUNT", "AVG", "MIN", "MAX"]


class JavaRandom:
    """``java.util.Random``: the 48-bit linear congruential generator."""

    _MULT = 0x5DEECE66D
    _MASK = (1 << 48) - 1

    def __init__(self, seed: int) -> None:
        self.seed = (seed ^ self._MULT) & self._MASK

    def _next(self, bits: int) -> int:
        self.seed = (self.seed * self._MULT + 0xB) & self._MASK
        value = self.seed >> (48 - bits)
        # `(int)` keeps the low 32 bits, signed: only a 32-bit draw can be negative.
        value &= 0xFFFFFFFF
        return value - (1 << 32) if value & 0x80000000 else value

    def next_int(self, bound: int) -> int:
        if bound & -bound == bound:
            return (bound * self._next(31)) >> 31
        u = self._next(31)
        r = u % bound
        while u - r + (bound - 1) > 2**31 - 1:
            u = self._next(31)
            r = u % bound
        return r

    def next_double(self) -> float:
        return ((self._next(26) << 27) + self._next(27)) * (1.0 / (1 << 53))


class Rnd:
    def __init__(self, seed: int) -> None:
        self.r = JavaRandom(seed)

    def chance(self, p: float) -> bool:
        return self.r.next_double() < p

    def count(self, lo: int, hi: int) -> int:
        return lo + self.r.next_int(hi - lo + 1)

    def pick(self, seq: list[Any]) -> Any:
        return seq[self.r.next_int(len(seq))]

    def text(self) -> str:
        alnum = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        n = self.r.next_int(11)
        out = alnum[self.r.next_int(len(alnum))]
        spaced = alnum + " "
        for _ in range(n):
            out += spaced[self.r.next_int(len(spaced))]
        return out.strip() or "x"

    def colname(self) -> str:
        lower = "abcdefghijklmnopqrstuvwxyz_"
        rest = lower + "0123456789"
        out = lower[self.r.next_int(len(lower))]
        for _ in range(self.r.next_int(8)):
            out += rest[self.r.next_int(len(rest))]
        return out


class Names:
    def __init__(self) -> None:
        self.n: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        i = self.n.get(prefix, 0)
        self.n[prefix] = i + 1
        return f"{prefix}{i}"


def three_part(rnd: Rnd) -> str:
    return f"{rnd.colname()}.{rnd.colname()}.{rnd.colname()}"


def maybe_meta(rnd: Rnd, target: dict[str, Any]) -> None:
    if rnd.chance(0.4):
        target["comment"] = rnd.text()
    if rnd.chance(0.3):
        target["display_name"] = rnd.text()
    if rnd.chance(0.3):
        target["synonyms"] = [rnd.text() for _ in range(rnd.count(1, 3))]
    if rnd.chance(0.25):
        kind = rnd.pick(["number", "currency", "date"])
        fmt: dict[str, Any] = {"type": kind}
        if kind == "currency":
            fmt["currency_code"] = "USD"
        target["format"] = fmt


def build_join(rnd, names, parent_alias, depth, ancestor_path, qual_paths, otm):
    name = names.next("j")
    path = [*ancestor_path, name]
    qual = ".".join(path)
    qual_paths.append(qual)
    join: dict[str, Any] = {"name": name, "source": three_part(rnd)}
    if rnd.chance(0.5):
        join["using"] = [f"u{i}_{rnd.colname()}" for i in range(rnd.count(1, 2))]
    else:
        clauses = []
        for i in range(rnd.count(1, 2)):
            pc = f"fk{i}_{rnd.colname()}"
            cc = f"pk{i}_{rnd.colname()}"
            clauses.append(f"{parent_alias}.{pc} = {name}.{cc}")
        join["on"] = " AND ".join(clauses)
    if otm:
        join["cardinality"] = "one_to_many"
    elif rnd.chance(0.4):
        join["cardinality"] = "many_to_one"
    if rnd.chance(0.3):
        join["rely"] = {"at_most_one_match": True}
    dims = []
    for _ in range(0 if otm else rnd.count(0, 2)):
        col = rnd.colname()
        expr = (
            f"{qual}.{col}"
            if rnd.chance(0.7)
            else f"{qual}.{col} + {qual}.{rnd.colname()}"
        )
        dim = {"name": names.next("c"), "expr": expr}
        maybe_meta(rnd, dim)
        dims.append(dim)
    if depth < 2 and rnd.chance(0.35):
        child, child_dims = build_join(
            rnd, names, name, depth + 1, path, qual_paths, otm
        )
        join["joins"] = [child]
        dims.extend(child_dims)
    return join, dims


def build_metric_view(rnd: Rnd) -> dict[str, Any]:
    names = Names()
    mv: dict[str, Any] = {"version": MV_VERSION, "source": three_part(rnd)}
    if rnd.chance(0.4):
        mv["comment"] = rnd.text()
    if rnd.chance(0.3):
        mv["filter"] = f"{rnd.colname()} > 0"
    fields: list[dict[str, Any]] = []
    joins = []
    for _ in range(rnd.count(0, 3)):
        col = rnd.colname()
        dim = {
            "name": names.next("c"),
            "expr": col if rnd.chance(0.7) else f"UPPER({col})",
        }
        maybe_meta(rnd, dim)
        fields.append(dim)
    qual_paths: list[str] = []
    for _ in range(rnd.count(0, 2)):
        join, jdims = build_join(
            rnd, names, "source", 0, [], qual_paths, rnd.chance(0.25)
        )
        joins.append(join)
        fields.extend(jdims)
    measures = []
    for _ in range(rnd.count(0, 2)):
        m: dict[str, Any] = {"name": names.next("c")}
        if qual_paths and rnd.chance(0.4):
            m["expr"] = f"{rnd.pick(AGGS)}({rnd.pick(qual_paths)}.{rnd.colname()})"
        else:
            m["expr"] = f"{rnd.pick(AGGS)}({rnd.colname()})"
        if rnd.chance(0.4):
            m["comment"] = rnd.text()
        if rnd.chance(0.3):
            m["synonyms"] = [rnd.text() for _ in range(rnd.count(1, 3))]
        if rnd.chance(0.3):
            m["window"] = [{"order": rnd.colname(), "range": "trailing 7 day"}]
        measures.append(m)
    if joins:
        mv["joins"] = joins
    if not fields and not measures:
        fields.append({"name": names.next("c"), "expr": rnd.colname()})
    if fields:
        mv["fields"] = fields
    if measures:
        mv["measures"] = measures
    if rnd.chance(0.2):
        mv["materialization"] = {
            "schedule": "every 6 hours",
            "mode": rnd.pick(["relaxed", "strict"]),
        }
    return mv


def ossie_field(name: str, expr: str) -> dict[str, Any]:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "DATABRICKS", "expression": expr}]},
    }


def build_ossie(rnd: Rnd) -> dict[str, Any]:
    names = Names()
    fact = "fact"
    datasets: list[dict[str, Any]] = [{"name": fact, "source": f"c.s.{fact}"}]
    relationships = []
    reachable = [fact]
    for i in range(rnd.count(0, 3)):
        dname = names.next("dim")
        parent = rnd.pick(reachable)
        datasets.append({"name": dname, "source": f"c.s.{rnd.colname()}{i}"})
        reachable.append(dname)
        rel: dict[str, Any] = {"name": names.next("r"), "from": parent, "to": dname}
        if rnd.chance(0.5):
            cols = [rnd.colname() for _ in range(rnd.count(1, 2))]
            rel["from_columns"] = list(cols)
            rel["to_columns"] = list(cols)
        else:
            n = rnd.count(1, 2)
            fcols, tcols = [], []
            for j in range(n):
                fcols.append(f"fk{j}_{rnd.colname()}")
                tcols.append(f"pk{j}_{rnd.colname()}")
            rel["from_columns"] = fcols
            rel["to_columns"] = tcols
        relationships.append(rel)
    for ds in datasets:
        flds = [
            ossie_field(names.next("c"), rnd.colname()) for _ in range(rnd.count(0, 3))
        ]
        if flds:
            ds["fields"] = flds
    metrics = []
    joined = reachable[1:]
    for _ in range(rnd.count(0, 2)):
        if joined and rnd.chance(0.4):
            expr = f"{rnd.pick(AGGS)}({rnd.pick(joined)}.{rnd.colname()})"
        else:
            expr = f"{rnd.pick(AGGS)}({rnd.colname()})"
        metrics.append(ossie_field(names.next("c"), expr))
    if not any(ds.get("fields") for ds in datasets) and not metrics:
        datasets[0]["fields"] = [ossie_field(names.next("c"), rnd.colname())]
    model: dict[str, Any] = {"name": names.next("m")}
    if rnd.chance(0.4):
        model["description"] = rnd.text()
    model["datasets"] = datasets
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics
    return {"version": OSSIE_VERSION, **model}


# --- the properties -------------------------------------------------------------------


def _java(value: Any) -> str:
    from exmergo_dex_core.metric_views.ossie_bridge._java import java_str

    return str(java_str(value))


def cond_canon(join: dict[str, Any]) -> str:
    using = join.get("using") or []
    if using:
        return "using:" + str(sorted(str(u) for u in using))
    on = join.get("on")
    if on is None:
        return "none"
    pairs = set()
    for clause in re.split(r"\s+AND\s+", str(on), flags=re.IGNORECASE):
        left, _, right = clause.partition("=")
        pairs.add(f"{left.strip()}={right.strip()}")
    return "on:" + str(sorted(pairs))


def flatten_joins(joins, parent, acc, edges):
    for j in joins or []:
        acc[j["name"]] = (
            j.get("source"),
            cond_canon(j),
            j.get("cardinality"),
            j.get("rely"),
        )
        edges.add(f"{parent}->{j['name']}")
        flatten_joins(j.get("joins"), j["name"], acc, edges)


def dims(mv):
    return mv.get("dimensions") or mv.get("fields") or []


def by_name(items, measure):
    keys = (
        ("expr", "comment", "synonyms", "format", "window")
        if measure
        else ("expr", "comment", "display_name", "synonyms", "format")
    )
    return {i["name"]: tuple(_java(i.get(k)) for k in keys) for i in items}


def assert_mv_round_trip(mv, mv2):
    for key in ("source", "comment", "filter", "materialization"):
        assert mv.get(key) == mv2.get(key), key
    assert by_name(dims(mv), False) == by_name(dims(mv2), False)
    assert by_name(mv.get("measures") or [], True) == by_name(
        mv2.get("measures") or [], True
    )
    a1, e1, a2, e2 = {}, set(), {}, set()
    flatten_joins(mv.get("joins"), "source", a1, e1)
    flatten_joins(mv2.get("joins"), "source", a2, e2)
    assert a1 == a2
    assert e1 == e2


CONVERTER_DIALECTS = {"DATABRICKS", "ANSI_SQL", "OSSIE_SQL_2026"}


def expr_of(obj):
    for d in (obj.get("expression") or {}).get("dialects") or []:
        if d.get("dialect") in CONVERTER_DIALECTS:
            return d.get("expression")
    return None


def fields_map(ds, *, skip_key_only=False):
    from exmergo_dex_core.metric_views.ossie_bridge.adapt import is_key_only

    return {
        f["name"]: expr_of(f)
        for f in ds.get("fields") or []
        if not (skip_key_only and is_key_only(f))
    }


def rel_set(model):
    return {
        f"{r['from']}->{r['to']}|{r.get('from_columns')}|{r.get('to_columns')}"
        for r in model.get("relationships") or []
    }


def assert_ossie_round_trip(m1, m2, *, skip_key_only=False):
    d1 = {ds["name"]: (ds["source"], fields_map(ds)) for ds in m1["datasets"]}
    d2 = {
        ds["name"]: (ds["source"], fields_map(ds, skip_key_only=skip_key_only))
        for ds in m2["datasets"]
    }
    assert d1 == d2
    assert rel_set(m1) == rel_set(m2)
    met1 = {m["name"]: expr_of(m) for m in m1.get("metrics") or []}
    met2 = {m["name"]: expr_of(m) for m in m2.get("metrics") or []}
    assert met1 == met2
    assert m1.get("description") == m2.get("description")


@pytest.mark.parametrize("seed", range(NUM_SEEDS))
def test_metric_view_round_trip_across_seeds(seed):
    mv = build_metric_view(Rnd(seed))
    ossie = metric_view_to_ossie(dump_yaml(mv)).value
    mv2 = parse_yaml(dump_yaml(ossie_to_metric_view(dump_items(ossie)).value))
    assert_mv_round_trip(mv, mv2)


@pytest.mark.parametrize("seed", range(NUM_SEEDS))
def test_ossie_round_trip_across_seeds(seed):
    ossie = build_ossie(Rnd(seed))
    mv = ossie_to_metric_view(dump_items(ossie)).value
    ossie2 = parse_yaml(dump_items(metric_view_to_ossie(dump_yaml(mv)).value))
    assert_ossie_round_trip(ossie, ossie2)


@pytest.mark.parametrize("seed", range(NUM_SEEDS))
def test_metric_view_round_trip_through_dex_is_exact(seed):
    """Through dex's bridge, with the key fields it adds, the view comes back as
    the converter's round trip brings it back: the additions are undone."""
    mv = build_metric_view(Rnd(seed))
    through_port = ossie_value_to_metric_view(
        metric_view_value_to_ossie(mv).value
    ).value
    imported = import_view(parse_yaml(dump_yaml(mv)))
    through_dex = export_document(parse_yaml(dump_items(imported.value))).value
    assert through_dex == through_port


@pytest.mark.parametrize("seed", range(NUM_SEEDS))
def test_ossie_round_trip_through_dex(seed):
    ossie = build_ossie(Rnd(seed))
    mv = export_document(ossie).value
    ossie2 = import_view(parse_yaml(dump_yaml(mv))).value
    assert_ossie_round_trip(ossie, _rel_columns_as_written(ossie2), skip_key_only=True)


def _rel_columns_as_written(model):
    """``model`` with each relationship's field names read back as the columns
    they expose: dex names fields where the converter names columns, so the
    property compares what the join reads."""
    columns = {
        ds["name"]: {f["name"]: expr_of(f) for f in ds.get("fields") or []}
        for ds in model["datasets"]
    }
    out = dict(model)
    out["relationships"] = [
        {
            **rel,
            "from_columns": [
                columns[rel["from"]].get(c, c) for c in rel["from_columns"]
            ],
            "to_columns": [columns[rel["to"]].get(c, c) for c in rel["to_columns"]],
        }
        for rel in model.get("relationships") or []
    ]
    return out


def test_java_random_matches_java_for_known_seeds():
    """The published first draws of ``new Random(42).nextInt()`` and
    ``new Random(0).nextInt()``, so the generator is Java's, seed for seed."""
    assert JavaRandom(42)._next(32) == -1170105035
    assert JavaRandom(0)._next(32) == -1155484576
