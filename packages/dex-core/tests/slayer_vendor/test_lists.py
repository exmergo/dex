"""SLayer list columns: ``type: ARRAY``, ``element_type``, ``.value`` (#1246).

SLayer 1.8 declares a list column as ``type: ARRAY`` with its ``element_type``,
groups it by its elements through ``<column>.value``, and tests membership with
``contains``, ``contains_any`` and ``contains_all``. What dex promises over one:

- the catalog lists the whole list but groups nothing by it, and lists the
  ``.value`` token as groupable, typed from ``element_type``;
- a query grouped by ``.value`` counts each parent once under each element it
  holds (SLayer's ``associate``), never the whole total on every row, and says
  the rows overlap;
- grouping by, or reading the values of, the whole list is refused before
  anything renders, naming the ``.value`` token;
- the three membership filters are read and screened, so they run;
- the PII gate screens ``.value`` as the list column it reads: the profile's
  flag and any ``pii_overrides`` entry apply, and a refusal never names the
  placeholder SLayer resolves an element to.

Needs the real ``[slayer]`` extra, so it skips without it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

from exmergo_dex_core.cli import main
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic import SemanticBackendError
from exmergo_dex_core.explore.semantic.backend import resolve_semantic_layer
from exmergo_dex_core.explore.semantic.model import SemanticQuery
from exmergo_dex_core.explore.semantic.slayer import LocalSlayerLayer
from exmergo_dex_core.slayer import render
from exmergo_dex_core.slayer.loader import FORMAT_VERSION
from exmergo_dex_core.slayer_dialect import filter_refs

duckdb = pytest.importorskip("duckdb")

BASE = "semantics/slayer"

POSTS = {
    "version": FORMAT_VERSION,
    "name": "posts",
    "sql_table": "shop.main.posts",
    "data_source": "wh",
    "filters": ["status <> 'test'"],
    "columns": [
        {"name": "post_id", "type": "INT", "primary_key": True},
        {"name": "status", "type": "TEXT"},
        {"name": "score", "type": "INT"},
        {"name": "tags", "type": "ARRAY", "element_type": "TEXT"},
        {"name": "posted_on", "type": "ARRAY", "element_type": "DATE"},
        # Named like PII: the name heuristic alone refuses it.
        {"name": "email", "type": "ARRAY", "element_type": "TEXT"},
        # Named nothing like PII, over a column the profile flags: only the
        # catalog's physical column can tell the gate what `.value` reads.
        {
            "name": "handles",
            "type": "ARRAY",
            "element_type": "TEXT",
            "sql": "contact_email",
        },
        # A list of structs: SLayer has no element to group by.
        {"name": "payload", "type": "ARRAY", "element_type": "UNKNOWN"},
    ],
    "measures": [
        {"name": "post_count", "formula": "*:count"},
        {"name": "total_score", "formula": "score:sum"},
        # Value-returning aggregations: they answer with the column's values.
        {"name": "tag_list", "formula": "tags.value:collect"},
        {"name": "addresses", "formula": "email.value:collect"},
        {"name": "last_address", "formula": "email.value:max"},
    ],
}

ROWS = [
    (1, "published", 10, ["dbt", "sql"], ["2026-01-03", "2026-02-01"]),
    (2, "published", 20, ["dbt"], ["2026-01-20"]),
    (3, "draft", 5, ["python"], ["2026-02-14"]),
    (4, "published", 7, [], []),
    (5, "published", 3, None, None),
    # A repeated element counts its post once.
    (7, "published", 1, ["python", "python"], ["2026-03-09"]),
    # Excluded by the model's own filter, so a number that counts it is wrong.
    (6, "test", 1000, ["dbt"], ["2026-03-10"]),
]


def _warehouse(root: Path) -> Path:
    path = root / "shop.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE posts (post_id INT, status VARCHAR, score INT, "
        "tags VARCHAR[], posted_on DATE[], email VARCHAR[], "
        "contact_email VARCHAR[], payload STRUCT(a INT)[])"
    )
    for post_id, status, score, tags, dates in ROWS:
        conn.execute(
            "INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            [
                post_id,
                status,
                score,
                tags,
                dates,
                [f"user{post_id}@example.com"],
                [f"person{post_id}@example.com"],
            ],
        )
    conn.close()
    return path


def _repo(root: Path, *, extra: str = "") -> Path:
    name = "models/wh/posts.yaml"
    path = root / BASE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(POSTS, sort_keys=False), encoding="utf-8")
    warehouse = _warehouse(root)
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {warehouse}\n"
        f"semantic:\n  vendor: slayer\n  slayer:\n    base_dir: {BASE}\n"
        f"    models:\n      - {name}\n" + extra,
        encoding="utf-8",
    )
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _repo(tmp_path)


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def _query(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    return _run(capsys, root, "explore", "semantic", "query", *argv)


def _values(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    return _run(capsys, root, "explore", "semantic", "values", *argv)


def _by_hand(root: Path, sql: str) -> list[list[Any]]:
    conn = duckdb.connect(str(root / "shop.duckdb"), read_only=True)
    try:
        return [list(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _errors(envelope: dict) -> str:
    return " ".join(envelope["errors"])


def _layer(root: Path) -> LocalSlayerLayer:
    return resolve_semantic_layer(DexEngine.from_repo(root))


# Per element, the posts that hold it, each once: the association SLayer's
# `associate` computes.
PER_ELEMENT = (
    "SELECT tag, {agg} FROM (SELECT DISTINCT post_id, score, tag FROM posts, "
    "UNNEST(tags) AS u(tag) WHERE status <> 'test' AND tag IS NOT NULL) "
    "GROUP BY tag"
)


# --- the catalog -------------------------------------------------------------------


def test_the_catalog_lists_a_list_but_groups_by_its_elements(repo, capsys):
    rc, envelope = _run(capsys, repo, "explore", "semantic", "list", "--full")

    assert rc == 0, envelope["errors"]
    dimensions = {row["name"]: row for row in envelope["data"]["dimensions"]}
    whole = dimensions["posts.tags"]
    assert whole["vendor_params"]["data_type"] == "ARRAY"
    assert whole["vendor_params"]["element_type"] == "TEXT"
    assert whole["vendor_params"]["groupable"] is False

    element = dimensions["posts.tags.value"]
    assert element["type"] == "categorical"
    assert element["definition"] == "tags.value"
    assert element["vendor_params"] == {"data_type": "TEXT", "element_of": "tags"}
    # A DATE element is a time dimension.
    assert dimensions["posts.posted_on.value"]["type"] == "time"
    # An opaque element has no `.value`.
    assert "posts.payload.value" not in dimensions

    metrics = {row["name"]: row for row in envelope["data"]["metrics"]}
    groupable = metrics["posts.post_count"]["dimensions"]
    assert "posts.tags.value" in groupable
    assert "posts.tags" not in groupable
    assert "posts.payload" not in groupable


def test_for_dimension_answers_for_an_element_token(repo, capsys):
    rc, envelope = _run(
        capsys,
        repo,
        "explore",
        "semantic",
        "list",
        "--for-dimension",
        "posts.tags.value",
    )

    assert rc == 0, envelope["errors"]
    names = {row["name"] for row in envelope["data"]["metrics"]}
    assert names == {
        "posts.post_count",
        "posts.total_score",
        "posts.tag_list",
        "posts.addresses",
        "posts.last_address",
    }


# --- grouping by elements ----------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "agg"),
    [("posts.post_count", "count(*)"), ("posts.total_score", "sum(score)")],
)
def test_grouping_by_elements_counts_each_parent_under_each_element(
    repo, capsys, metric, agg
):
    rc, envelope = _query(capsys, repo, metric, "--group-by", "posts.tags.value")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert data["columns"] == ["posts.tags.value", metric]
    assert sorted(data["cells"]) == sorted(_by_hand(repo, PER_ELEMENT.format(agg=agg)))
    # Not SLayer's broadcast default, which repeats the total on every row.
    totals = {row[1] for row in data["cells"]}
    assert len(totals) > 1
    assert any("do not sum to the metric's total" in n for n in data["notes"])


def test_an_element_time_dimension_buckets_at_a_grain(repo, capsys):
    rc, envelope = _values(capsys, repo, "posts.posted_on.value__month")

    assert rc == 0, envelope["errors"]
    months = sorted(str(row[0])[:7] for row in envelope["data"]["cells"])
    assert months == ["2026-01", "2026-02", "2026-03"]


def test_an_element_s_values_match_hand_written_sql(repo, capsys):
    rc, envelope = _values(capsys, repo, "posts.tags.value")

    assert rc == 0, envelope["errors"]
    assert sorted(envelope["data"]["cells"]) == sorted(
        _by_hand(
            repo,
            "SELECT DISTINCT tag FROM posts, UNNEST(tags) AS u(tag) "
            "WHERE status <> 'test' AND tag IS NOT NULL",
        )
    )


@pytest.mark.parametrize(
    "run",
    [
        lambda capsys, repo: _query(
            capsys, repo, "posts.post_count", "--group-by", "posts.tags"
        ),
        lambda capsys, repo: _values(capsys, repo, "posts.tags"),
    ],
    ids=["group-by", "values"],
)
def test_the_whole_list_is_refused_naming_its_element_token(
    repo, capsys, monkeypatch, run
):
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    rc, envelope = run(capsys, repo)

    assert rc != 0
    assert "posts.tags.value" in _errors(envelope)
    assert "list column" in _errors(envelope)


def test_a_list_with_no_typed_element_is_refused_by_name(repo, capsys):
    rc, envelope = _values(capsys, repo, "posts.payload")

    assert rc != 0
    assert "element type is UNKNOWN" in _errors(envelope)


def test_a_list_column_on_redshift_is_refused_before_it_renders(repo, monkeypatch):
    monkeypatch.setattr(LocalSlayerLayer, "_connector", lambda self: "redshift")
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)
    layer = _layer(repo)

    with pytest.raises(SemanticBackendError, match="no native lists"):
        layer.query(
            SemanticQuery(metrics=["posts.post_count"], where=["contains(tags, 'dbt')"])
        )
    with pytest.raises(SemanticBackendError, match="no native lists"):
        layer.values("posts.tags.value", [])


# --- membership filters ------------------------------------------------------------


@pytest.mark.parametrize(
    ("clause", "predicate"),
    [
        ("contains(tags, 'dbt')", "list_contains(tags, 'dbt')"),
        (
            "contains_any(tags, ['python', 'sql'])",
            "list_has_any(tags, ['python', 'sql'])",
        ),
        ("contains_all(tags, ['dbt', 'sql'])", "list_has_all(tags, ['dbt', 'sql'])"),
        ("tags.value = 'sql'", "list_contains(tags, 'sql')"),
    ],
)
def test_a_membership_filter_is_read_and_runs(repo, capsys, clause, predicate):
    rc, envelope = _query(capsys, repo, "posts.post_count", "--where", clause)

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["cells"] == _by_hand(
        repo,
        f"SELECT count(*) FROM posts WHERE status <> 'test' AND {predicate}",  # noqa: S608
    )


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("contains(tags, 'dbt')", ["posts.tags"]),
        ("contains_any(tags, ['a', 'b'])", ["posts.tags"]),
        ("contains_all(email, ['a'])", ["posts.email"]),
        ("tags.value = 'a'", ["posts.tags.value"]),
        ("posts.email.value = 'a'", ["posts.email.value"]),
    ],
)
def test_the_filter_reader_names_the_list_column_never_a_placeholder(
    repo, clause, expected
):
    models = {
        loaded.model.name: loaded.model
        for loaded in DexEngine.from_repo(repo)
        .semantic_catalog_source()
        .validated_models()
    }

    assert filter_refs([clause], "posts", models=models) == expected


def test_a_variable_in_a_list_slot_is_still_refused(repo, capsys):
    rc, envelope = _query(
        capsys, repo, "posts.post_count", "--where", "contains_any(tags, {wanted})"
    )

    assert rc != 0
    assert "cannot read the dimensions" in _errors(envelope)


# --- the PII gate ------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("--group-by", "posts.email.value"),
        ("--where", "email.value = 'a@example.com'"),
        ("--where", "contains_any(email, ['a@example.com'])"),
    ],
)
def test_a_pii_list_is_refused_by_its_element_token(repo, capsys, argv):
    rc, envelope = _query(capsys, repo, "posts.post_count", *argv)

    assert rc != 0
    assert "would surface PII" in _errors(envelope)
    assert "posts.email" in _errors(envelope)
    assert "__slayer_list__" not in _errors(envelope)


def test_an_element_is_screened_by_its_column_s_profile(repo, capsys):
    """`handles` reads `contact_email`: the name passes, the profile refuses."""

    rc, profiled = _run(capsys, repo, "explore", "profile", "shop.main.posts")
    assert rc == 0, profiled["errors"]

    for argv in (
        ("--group-by", "posts.handles.value"),
        ("--where", "handles.value = 'x'"),
    ):
        rc, envelope = _query(capsys, repo, "posts.post_count", *argv)
        assert rc != 0, argv
        assert "profiled and flagged" in _errors(envelope), argv
        assert "__slayer_list__" not in _errors(envelope)

    rc, envelope = _values(capsys, repo, "posts.handles.value")
    assert rc != 0
    assert "posts.handles.value" in _errors(envelope)


def test_a_pii_override_on_the_list_column_clears_its_elements(tmp_path, capsys):
    root = _repo(
        tmp_path,
        extra=(
            "pii_overrides:\n"
            "  - column: shop.main.posts.contact_email\n"
            "    reason: synthetic handles in a test fixture\n"
        ),
    )
    rc, profiled = _run(capsys, root, "explore", "profile", "shop.main.posts")
    assert rc == 0, profiled["errors"]

    rc, envelope = _query(
        capsys, root, "posts.post_count", "--group-by", "posts.handles.value"
    )

    assert rc == 0, envelope["errors"]


def test_a_collect_measure_returns_the_elements(repo, capsys):
    rc, envelope = _query(capsys, repo, "posts.tag_list", "--group-by", "posts.status")

    assert rc == 0, envelope["errors"]
    assert [row[0] for row in sorted(envelope["data"]["cells"])] == [
        "draft",
        "published",
    ]


@pytest.mark.parametrize("metric", ["posts.addresses", "posts.last_address"])
def test_a_measure_returning_a_pii_column_s_values_is_refused(
    repo, capsys, monkeypatch, metric
):
    """`collect(email.value)` and `max(email.value)` answer with addresses, so
    their input is screened as a grouped column would be, before rendering."""

    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    rc, envelope = _query(capsys, repo, metric)

    assert rc != 0
    assert "would surface PII" in _errors(envelope)
    assert "posts.email.value" in _errors(envelope)


def test_the_gate_runs_before_anything_renders(repo, monkeypatch):
    monkeypatch.setattr(render.SlayerRenderer, "render", _never_rendered)

    with pytest.raises(SemanticBackendError, match="would surface PII"):
        _layer(repo).query(
            SemanticQuery(metrics=["posts.post_count"], group_by=["posts.email.value"])
        )


def _never_rendered(self, query):  # pragma: no cover - the assertion is the point
    raise AssertionError("SLayer rendered a query dex should have refused first")
