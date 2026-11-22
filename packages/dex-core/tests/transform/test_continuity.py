"""``continuity`` on a declared time column, and ``dex_continuity`` (#1461).

A declaration states that its time column has no gap at a grain, for the whole
model or per value of ``by`` columns. With ``declarations.render_tests`` on it
renders ``dex_continuity``, a count-only column test tagged ``dex_scheduled``
that fails with the number of gaps: runs of empty buckets between two buckets
holding rows. An edge is never a gap. There is no ``maintain verify`` twin, so
the macro is held here to a Python count of the same gaps, and by the parity
harness's own pairs on every dialect.
"""

from __future__ import annotations

import itertools
import json
import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main
from exmergo_dex_core.semantic_declarations import DeclarationError
from exmergo_dex_core.transform import scaffold
from exmergo_dex_core.transform.claims import claim_id
from exmergo_dex_core.transform.declarations import parse_declarations, render
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

NAME = gt.CONTINUITY
GRAINS = ("second", "minute", "hour", "day", "week", "month", "quarter", "year")


def _declared(continuity=None, role="time", column="ordered_at") -> dict:
    fields: dict = {"continuity": {} if continuity is None else continuity}
    if role is not None:
        fields["role"] = role
    return {"model": "fct_orders", "columns": {column: fields}}


DAILY = _declared()
BY_REGION = _declared({"grain": "day", "by": ["region"]})


def _render(payload: dict, render_tests, *, generated=None, outputs=None, content=None):
    [declaration] = parse_declarations([payload])
    return render(
        content,
        declaration,
        render_tests=render_tests,
        generated=generated,
        outputs=outputs,
    )


def _column(content: str, column: str = "ordered_at") -> dict:
    entry = next(
        e for e in yaml.safe_load(content)["models"] if e["name"] == "fct_orders"
    )
    return next(c for c in entry.get("columns") or [] if c["name"] == column)


def _tests(content: str, column: str = "ordered_at") -> list:
    return [
        t
        for t in _column(content, column).get("data_tests") or []
        if isinstance(t, dict) and NAME in t
    ]


# --- the field ----------------------------------------------------------------------


def test_the_grain_defaults_to_a_day_with_no_keys():
    [declaration] = parse_declarations([DAILY])
    continuity = declaration.columns["ordered_at"].continuity
    assert continuity is not None
    assert (continuity.grain.value, continuity.by) == ("day", [])


@pytest.mark.parametrize("grain", GRAINS)
def test_every_common_grain_is_accepted(grain):
    [declaration] = parse_declarations([_declared({"grain": grain})])
    assert declaration.columns["ordered_at"].continuity.grain.value == grain


@pytest.mark.parametrize("grain", ["days", "fortnight", "1d", "", 1])
def test_another_grain_is_refused(grain):
    with pytest.raises(DeclarationError, match="grain"):
        parse_declarations([_declared({"grain": grain})])


@pytest.mark.parametrize(
    ("by", "message"),
    [
        (["region", "REGION"], "twice"),
        (["region", " "], "empty"),
        ([1], "list of column names"),
        ({"region": 1}, "list of column names"),
    ],
)
def test_a_malformed_by_is_refused(by, message):
    with pytest.raises(DeclarationError, match=message):
        parse_declarations([_declared({"by": by})])


def test_one_key_written_bare_is_a_list_of_one():
    [declaration] = parse_declarations([_declared({"by": " region "})])
    assert declaration.columns["ordered_at"].continuity.by == ["region"]


def test_an_unknown_continuity_key_is_refused():
    with pytest.raises(DeclarationError, match="not a declaration field"):
        parse_declarations([_declared({"grain": "day", "window": "7d"})])


def test_it_renders_under_the_columns_dex_meta():
    content = _render(BY_REGION, False)
    assert _column(content)["config"]["meta"]["dex"] == {
        "role": "time",
        "continuity": {"grain": "day", "by": ["region"]},
    }
    assert _tests(content) == []


# --- the generated test ------------------------------------------------------------


def test_it_ships_as_a_scheduled_test_on_every_connector():
    kind = next(k for k in gt.kinds() if k.name == NAME)
    assert kind.cadence == gt.SCHEDULED
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS


def test_a_continuity_renders_one_count_only_test_under_its_column():
    generated: list = []
    content = _render(BY_REGION, True, generated=generated)
    [test] = _tests(content)
    body = test[NAME]
    assert body["arguments"] == {"grain": "day", "by": ["region"]}
    assert body["config"]["tags"] == [gt.SCHEDULED]
    assert body["config"]["meta"]["disable_test_samples"] is True
    claim = claim_id(
        "fct_orders",
        "continuity",
        ["ordered_at", "region"],
        {"check": NAME, "grain": "day", "by": ["region"]},
    )
    assert body["config"]["meta"]["dex"]["claim"] == claim
    [row] = generated
    assert (row["test"], row["column"], row["variant"], row["status"]) == (
        NAME,
        "ordered_at",
        "day",
        "added",
    )
    assert row["cadence"] == gt.SCHEDULED


def test_no_keys_render_no_by_argument():
    [test] = _tests(_render(DAILY, True))
    assert test[NAME]["arguments"] == {"grain": "day"}


def test_a_grain_and_a_key_set_are_each_their_own_claim():
    def claim(continuity):
        return _tests(_render(_declared(continuity), True))[0][NAME]["config"]["meta"][
            "dex"
        ]["claim"]

    daily = claim({"grain": "day"})
    assert claim({}) == daily
    assert claim({"grain": "week"}) != daily
    assert claim({"by": ["region"]}) != daily
    assert claim({"by": ["Region"]}) == claim({"by": ["region"]})


def test_rendering_again_adds_nothing():
    content = _render(BY_REGION, True)
    generated: list = []
    again = _render(BY_REGION, True, generated=generated, content=content)
    assert again == content
    assert [g["status"] for g in generated] == ["already_listed"]


@pytest.mark.parametrize(
    "changed", [{"grain": "week", "by": ["region"]}, {"grain": "day"}]
)
def test_a_changed_grain_or_key_set_reports_the_old_test_stale_and_keeps_it(changed):
    content = _render(BY_REGION, True)
    generated: list = []
    again = _render(_declared(changed), True, generated=generated, content=content)
    tests = _tests(again)
    assert len(tests) == 2
    stale = [g for g in generated if g["status"] == "stale"]
    assert [(g["test"], g["reason"], g["cadence"]) for g in stale] == [
        (NAME, "continuity_changed", gt.SCHEDULED)
    ]


def test_an_unchanged_continuity_reports_nothing_stale():
    content = _render(BY_REGION, True)
    generated: list = []
    _render(BY_REGION, True, generated=generated, content=content)
    assert not [g for g in generated if g["status"] == "stale"]


def test_the_switch_off_renders_nothing_and_says_why():
    generated: list = []
    content = _render(DAILY, False, generated=generated)
    assert _tests(content) == []
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", "render_tests_off")
    ]


@pytest.mark.parametrize("setting", [[NAME], [gt.SCHEDULED]])
def test_a_list_naming_the_kind_or_its_cadence_turns_it_on(setting):
    assert len(_tests(_render(DAILY, setting))) == 1


def test_a_list_naming_only_the_other_cadence_leaves_it_off():
    generated: list = []
    _render(DAILY, [gt.EVERY_RUN], generated=generated)
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", "kind_not_enabled")
    ]


def test_a_key_the_model_provably_does_not_output_renders_nothing():
    generated: list = []
    content = _render(
        BY_REGION, True, generated=generated, outputs=frozenset({"ordered_at"})
    )
    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", gt.COLUMN_NOT_OUTPUT)
    ]


def test_a_key_the_model_outputs_renders():
    outputs = frozenset({"ordered_at", "region"})
    assert len(_tests(_render(BY_REGION, True, outputs=outputs))) == 1


# --- the plan ---------------------------------------------------------------------


def _repo(tmp_path: Path, render_tests: str) -> Path:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        f"declarations:\n  render_tests: {render_tests}\n",
        encoding="utf-8",
    )
    return tmp_path


ORDERS_SQL = "select 1 as order_id, 'emea' as region, current_timestamp as ordered_at\n"


def _plan(tmp_path: Path, capsys, declaration: dict, sql: str = ORDERS_SQL) -> dict:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/fct_orders.sql",
                        "kind": "model_sql",
                        "content": sql,
                    }
                ],
                "declarations": [declaration],
            }
        ),
        encoding="utf-8",
    )
    main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "orders",
            "--edits-file",
            str(payload),
        ]
    )
    return json.loads(capsys.readouterr().out)


def test_the_plan_that_renders_it_writes_its_macro_as_a_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, BY_REGION)
    assert planned["status"] == "ok", planned["errors"]
    assert f"macros/{NAME}.sql" in planned["data"]["paths"]
    assert planned["data"]["generated_tests"]["counts"] == {"added": 1}


@pytest.mark.parametrize("role", ["dimension", None])
def test_a_continuity_off_the_time_column_is_refused_naming_the_fix(
    dbt_project_dir: Path, tmp_path: Path, capsys, role
):
    planned = _plan(_repo(tmp_path, "false"), capsys, _declared(role=role))
    assert planned["status"] == "error"
    message = " ".join(str(e) for e in planned["errors"])
    assert "declares continuity" in message
    assert "role `time`" in message
    assert "fct_orders.ordered_at" in message


def test_a_continuity_by_its_own_time_column_is_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "false"), capsys, _declared({"by": ["Ordered_At"]}))
    assert planned["status"] == "error"
    assert "by itself" in " ".join(str(e) for e in planned["errors"])


def test_a_key_the_model_does_not_output_is_refused_naming_it(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, _declared({"by": ["country"]}))
    assert planned["status"] == "error"
    message = " ".join(str(e) for e in planned["errors"])
    assert "continuity by country" in message
    assert "does not produce" in message


def test_a_new_role_on_a_column_that_keeps_its_continuity_is_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path, "false")
    planned = _plan(repo, capsys, DAILY)
    assert planned["status"] == "ok", planned["errors"]
    main(["--repo-root", str(repo), "transform", "apply", planned["data"]["plan_id"]])
    capsys.readouterr()

    moved = {"model": "fct_orders", "columns": {"ordered_at": {"role": "dimension"}}}
    refused = _plan(repo, capsys, moved)
    assert refused["status"] == "error"
    assert "already declares continuity" in " ".join(map(str, refused["errors"]))


def test_transform_macro_lists_it(dbt_project_dir: Path, tmp_path: Path, capsys):
    _repo(tmp_path, "false")
    main(["--repo-root", str(tmp_path), "transform", "macro"])
    listed = json.loads(capsys.readouterr().out)
    assert NAME in {m["name"] for m in listed["data"]["macros"]}


# --- the macro file -----------------------------------------------------------------


def _asset(name: str = NAME) -> str:
    return scaffold.macro_edit(name, "macros").new_content or ""


def test_the_shipped_macro_passes_the_macro_file_check():
    validate_edit(
        PlanEdit(
            path=f"macros/{NAME}.sql", kind=EditKind.MACRO_SQL, new_content=_asset()
        )
    )


def _named_macros(text: str, piece: str) -> dict[str, str]:
    """Each ``<prefix>__<piece>`` macro's body in ``text``, by prefix, with the
    piece's name taken out so two assets' copies compare."""

    out = {}
    pattern = rf"\{{% macro (\w+)__{piece}\((.*?)\) -%\}}(.*?)\{{%- endmacro %\}}"
    for prefix, args, body in re.findall(pattern, text, re.DOTALL):
        out[prefix] = (args, body.replace(piece, "<piece>"))
    return out


def test_each_dialects_epoch_is_the_one_dex_freshness_sends():
    ours = _named_macros(_asset(), "dex_continuity_epoch")
    theirs = _named_macros(_asset(gt.FRESHNESS), "dex_freshness_epoch")
    assert set(ours) == set(theirs)
    assert ours == theirs


def test_a_type_is_read_as_dex_freshness_reads_it():
    def body(text: str, name: str) -> str:
        match = re.search(
            rf"\{{% macro {name}\(dtype\) -%\}}(.*?)\{{%- endmacro %\}}", text, re.S
        )
        assert match is not None
        return match.group(1)

    assert body(_asset(), "dex_continuity_kind") == body(
        _asset(gt.FRESHNESS), "dex_freshness_kind"
    )


# --- the macro under plain Jinja, against a Python count of the gaps ---------------


class _ReturnedError(Exception):
    def __init__(self, value):
        self.value = value


class _CompilerError(Exception):
    pass


class _Column:
    def __init__(self, name: str, dtype: str):
        self.name, self.dtype = name, dtype


def _module(columns: list[tuple[str, str]], target_type: str = "duckdb"):
    """The asset's macros under plain Jinja, with dbt's ``adapter``,
    ``exceptions``, ``return`` and ``target`` stood in, so the test's SQL can be
    rendered and run on DuckDB without dbt."""

    import jinja2

    def _return(value):
        raise _ReturnedError(value)

    def _raise(message):
        raise _CompilerError(message)

    text = re.sub(r"\{% test .*?\{% endtest %\}", "", _asset(), flags=re.DOTALL)
    env = jinja2.Environment(autoescape=False, extensions=["jinja2.ext.do"])  # noqa: S701
    module_box: dict = {}

    def _wrap(macro):
        def call(*args):
            try:
                return str(macro(*args))
            except _ReturnedError as returned:
                return returned.value

        return call

    class _Adapter:
        @staticmethod
        def get_columns_in_relation(_model):
            return [_Column(n, t) for n, t in columns]

        @staticmethod
        def quote(name):
            return f'"{name}"'

        @staticmethod
        def dispatch(name):
            module = module_box["module"]
            prefix = "default" if target_type == "duckdb" else target_type
            macro = getattr(module, f"{prefix}__{name}", None) or getattr(
                module, f"default__{name}"
            )
            return _wrap(macro)

    env.globals.update(
        {
            "return": _return,
            "target": {"type": target_type},
            "adapter": _Adapter(),
            "exceptions": type("E", (), {"raise_compiler_error": staticmethod(_raise)}),
            "execute": True,
        }
    )
    # A macro calling another of the same file by name reaches the template's
    # own macro, not a global; dbt catches a `return` there, so the call goes
    # through a wrapper that does the same.
    text = text.replace("dex_continuity_kind(resolved", "_kind(resolved")
    module = env.from_string(text).module
    module_box["module"] = module
    env.globals["_kind"] = _wrap(module.dex_continuity_kind)
    module = env.from_string(text).module
    module_box["module"] = module
    return module


def _sql(columns, grain: str, by=(), target_type: str = "duckdb") -> str:
    module = _module(columns, target_type)
    return str(module.default__dex_continuity("t", "ts", grain, list(by)))


def _bucket(value: datetime, grain: str) -> int:
    """The Python oracle: the bucket number of a UTC value at ``grain``."""

    seconds = int((value - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() // 1)
    if grain in ("second", "minute", "hour", "day"):
        return seconds // {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[grain]
    if grain == "week":
        # Mondays' ordinals are 7k + 1, so consecutive weeks are k and k + 1.
        monday = (value.date() - timedelta(days=value.weekday())).toordinal()
        return monday // 7
    if grain == "month":
        return value.year * 12 + value.month - 1
    if grain == "quarter":
        return value.year * 4 + (value.month - 1) // 3
    return value.year


def _gaps(rows: list[tuple[str | None, datetime | None]], grain: str) -> int:
    by_key: dict = {}
    for key, value in rows:
        if value is not None:
            by_key.setdefault(key, set()).add(_bucket(value, grain))
    gaps = 0
    for buckets in by_key.values():
        ordered = sorted(buckets)
        gaps += sum(1 for a, b in itertools.pairwise(ordered) if b - a > 1)
    return gaps


def _count(sql: str, rows, *, sql_type: str, keyed: bool) -> int:
    import duckdb

    con = duckdb.connect()
    con.execute("set timezone = 'UTC'")
    con.execute(f"create table t (region varchar, ts {sql_type})")
    if rows:
        con.executemany(
            "insert into t values (?, ?)",
            [
                (
                    key if keyed else None,
                    None
                    if value is None
                    else (value.date() if sql_type == "date" else value),
                )
                for key, value in rows
            ],
        )
    found = con.execute(sql).fetchall()
    return int(found[0][0]) if found else 0


def _random_rows(seed: int, grain: str, *, dates: bool) -> list:
    rng = random.Random(seed)  # noqa: S311 - fixture data, seeded
    unit = {
        "second": timedelta(seconds=1),
        "minute": timedelta(minutes=1),
        "hour": timedelta(hours=1),
        "day": timedelta(days=1),
        "week": timedelta(days=3),
        "month": timedelta(days=13),
        "quarter": timedelta(days=40),
        "year": timedelta(days=150),
    }[grain]
    # Around the epoch, a leap day and a century boundary, so a floor that
    # truncates or a civil date off by one shows.
    start = rng.choice(
        [
            datetime(1969, 12, 20, tzinfo=UTC),
            datetime(2000, 2, 20, tzinfo=UTC),
            datetime(1899, 12, 1, tzinfo=UTC),
            datetime(2023, 12, 25, 22, 59, 58, tzinfo=UTC),
        ]
    )
    rows = []
    for _ in range(rng.randint(0, 40)):
        step = rng.randint(0, 30)
        value = start + unit * step + timedelta(seconds=rng.randint(0, 59))
        if dates:
            value = datetime(value.year, value.month, value.day, tzinfo=UTC)
        rows.append(
            (rng.choice(["a", "b", None]), value if rng.random() > 0.05 else None)
        )
    return rows


@pytest.mark.parametrize("grain", GRAINS)
@pytest.mark.parametrize("keyed", [False, True])
def test_the_gap_count_is_pythons_for_every_grain(grain, keyed):
    pytest.importorskip("duckdb")
    columns = [("ts", "TIMESTAMP"), ("region", "VARCHAR")]
    sql = _sql(columns, grain, ["region"] if keyed else [])
    for seed in range(25):
        rows = _random_rows(seed, grain, dates=False)
        if not keyed:
            rows = [(None, value) for _key, value in rows]
        expected = _gaps(rows, grain)
        assert _count(sql, rows, sql_type="timestamp", keyed=keyed) == expected, (
            grain,
            seed,
        )


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_a_date_column_is_bucketed_as_its_utc_midnight(grain):
    pytest.importorskip("duckdb")
    sql = _sql([("ts", "DATE")], grain)
    for seed in range(15):
        rows = [(None, v) for _k, v in _random_rows(seed, grain, dates=True)]
        assert _count(sql, rows, sql_type="date", keyed=False) == _gaps(rows, grain)


def _day(day: int) -> datetime:
    return datetime(2024, 3, day, 9, tzinfo=UTC)


def test_one_missing_day_between_two_is_one_gap_and_the_edges_are_none():
    pytest.importorskip("duckdb")
    sql = _sql([("ts", "TIMESTAMP")], "day")

    def count(days):
        rows = [(None, _day(d)) for d in days]
        return _count(sql, rows, sql_type="timestamp", keyed=False)

    assert count([1, 2, 4, 5]) == 1
    assert count([1, 2, 3, 4, 5]) == 0
    assert count([2, 3, 4]) == 0  # the 1st and the 5th missing: edges
    assert count([1, 4, 5, 9]) == 2  # two runs, five missing days
    assert count([]) == 0


def test_a_key_missing_a_day_the_others_have_is_a_gap_for_that_key():
    pytest.importorskip("duckdb")
    sql = _sql([("ts", "TIMESTAMP"), ("region", "VARCHAR")], "day", ["region"])
    rows = [("a", _day(d)) for d in (1, 2, 3)] + [("b", _day(d)) for d in (1, 3)]
    assert _count(sql, rows, sql_type="timestamp", keyed=True) == 1
    whole = _sql([("ts", "TIMESTAMP")], "day")
    assert _count(whole, rows, sql_type="timestamp", keyed=True) == 0


def test_the_iso_week_starts_on_monday():
    pytest.importorskip("duckdb")
    sql = _sql([("ts", "DATE")], "week")
    # Sunday 7 January 2024 and Monday 8 January are two ISO weeks; Monday 22
    # leaves the week of the 15th empty.
    rows = [(None, datetime(2024, 1, d, tzinfo=UTC)) for d in (7, 8, 22)]
    assert _count(sql, rows, sql_type="date", keyed=False) == 1
    rows = [(None, datetime(2024, 1, d, tzinfo=UTC)) for d in (8, 14, 15)]
    assert _count(sql, rows, sql_type="date", keyed=False) == 0


def test_the_sql_returns_a_count_and_never_a_value():
    sql = _sql([("ts", "TIMESTAMP"), ("region", "VARCHAR")], "month", ["region"])
    assert _norm_head(sql) == "select dex_gaps"


def _norm_head(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.split("from", 1)[0]).strip()


def test_a_sub_day_grain_on_a_date_column_is_an_error():
    with pytest.raises(_CompilerError, match="is a date"):
        _sql([("ts", "DATE")], "hour")


def test_a_column_that_is_no_date_or_timestamp_is_an_error():
    with pytest.raises(_CompilerError, match="not a date or timestamp"):
        _sql([("ts", "VARCHAR")], "day")


def test_a_key_the_relation_lacks_is_an_error():
    with pytest.raises(_CompilerError, match="does not have"):
        _sql([("ts", "TIMESTAMP")], "day", ["region"])


def test_an_unknown_grain_is_an_error():
    with pytest.raises(_CompilerError, match="takes a grain"):
        _sql([("ts", "TIMESTAMP")], "fortnight")


@pytest.mark.parametrize(
    "dialect",
    [
        "postgres",
        "redshift",
        "bigquery",
        "snowflake",
        "databricks",
        "clickhouse",
        "trino",
    ],
)
def test_each_dialect_renders_its_own_pieces(dialect):
    sql = _sql(
        [("ts", "TIMESTAMP"), ("region", "VARCHAR")], "month", ["region"], dialect
    )
    if dialect == "clickhouse":
        assert "lagInFrame(toNullable(dex_bucket), 1, NULL)" in sql
        assert "toFloat64(" in sql
    else:
        assert "lag(dex_bucket) over (partition by" in sql
    if dialect == "bigquery":
        assert "as float64" in sql and "as int64" in sql
    if dialect in ("postgres", "redshift"):
        assert "double precision" in sql


# --- what dbt makes of it, against DuckDB --------------------------------------------


def _run(tmp_path: Path, select: str, arguments: dict) -> tuple[str, int]:
    """Build ``select`` as a table, run the shipped test on its ``ts`` column,
    and return dbt's status and failures."""

    import subprocess
    import sys

    pytest.importorskip("dbt.adapters.duckdb")
    project = tmp_path / "proj"
    (project / "models").mkdir(parents=True)
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: p\nversion: '1.0'\nprofile: p\nconfig-version: 2\n", encoding="utf-8"
    )
    (project / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'w.duckdb'}\n      threads: 1\n",
        encoding="utf-8",
    )
    (project / "macros" / f"{NAME}.sql").write_text(_asset(), encoding="utf-8")
    (project / "models" / "m.sql").write_text(
        f"{{{{ config(materialized='table') }}}}\n{select}\n", encoding="utf-8"
    )
    (project / "models" / "schema.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": "m",
                        "columns": [
                            {
                                "name": "ts",
                                "data_tests": [{NAME: {"arguments": arguments}}],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    dbt = Path(sys.executable).parent / "dbt"
    if not dbt.is_file():
        pytest.skip("needs dbt beside the interpreter")
    args = ["--project-dir", str(project), "--profiles-dir", str(project)]
    subprocess.run(  # noqa: S603 - the venv's own dbt, on a fixture project
        [str(dbt), "build", *args], cwd=project, capture_output=True, check=False
    )
    results = json.loads((project / "target" / "run_results.json").read_text())
    [result] = [r for r in results["results"] if r["unique_id"].startswith("test.")]
    return result["status"], int(result["failures"] or 0)


def _dates(*days: int, region: str = "a") -> str:
    return " union all ".join(
        f"select '{region}' as region, date '2024-03-{d:02d}' as ts" for d in days
    )


def test_dbt_fails_a_model_missing_one_day_between_two(tmp_path: Path):
    assert _run(tmp_path, _dates(1, 2, 4, 5), {"grain": "day"}) == ("fail", 1)


def test_dbt_passes_the_same_model_with_the_day_present(tmp_path: Path):
    assert _run(tmp_path, _dates(1, 2, 3, 4, 5), {"grain": "day"}) == ("pass", 0)


def test_dbt_passes_a_model_missing_only_its_edges(tmp_path: Path):
    assert _run(tmp_path, _dates(2, 3, 4), {"grain": "day"}) == ("pass", 0)


def test_dbt_counts_a_gap_per_key(tmp_path: Path):
    select = _dates(1, 2, 3, region="a") + " union all " + _dates(1, 3, region="b")
    assert _run(tmp_path, select, {"grain": "day", "by": ["region"]}) == ("fail", 1)


def test_dbt_errors_on_a_text_column_rather_than_passing(tmp_path: Path):
    status, _failures = _run(tmp_path, "select 'x' as ts", {"grain": "day"})
    assert status == "error"
