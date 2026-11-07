"""A live feature toggle across every producer, on real dbt and DuckDB (#1113).

The project mirrors the shape behind the issue: a mart and a transaction model
the author would gate, a ledger nothing in the marts reads, a model whose table
is always empty, and a model that inherits the fee column through a `*`. Every
expected column set is written by hand and the built relations are read back
with DuckDB directly, so nothing here trusts the fields under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

pytestmark = pytest.mark.live_open_adapter

FLAG = "enable_fees"
ON = "{% if var('" + FLAG + "') %}"

STG = (
    "select 1 as order_id, 100 as amount, 3 as fee_amount\nunion all select 2, 200, 6\n"
)
LINES = "select order_id, amount, fee_amount\nfrom {{ ref('stg_orders') }}\n"
REVENUE = (
    "select count(*) as orders, sum(amount) as revenue, sum(fee_amount) as fees\n"
    "from {{ ref('fct_order_lines') }}\n"
)
LEDGER = "select 'L1' as entry_id, 50 as debit, 5 as fee_amount\n"
DISPUTES = "select order_id, fee_amount\nfrom {{ ref('stg_orders') }}\nwhere false\n"
WIDE = "select * from {{ ref('fct_order_lines') }}\n"
ORIGINAL = {
    "stg_orders": STG,
    "fct_order_lines": LINES,
    "rpt_revenue": REVENUE,
    "ledger_entries": LEDGER,
    "rpt_fee_disputes": DISPUTES,
    "rpt_lines_wide": WIDE,
}

GATED = {
    "fct_order_lines": (
        "select order_id, amount\n" + ON + ", fee_amount{% endif %}\n"
        "from {{ ref('stg_orders') }}\n"
    ),
    "rpt_revenue": (
        "select count(*) as orders, sum(amount) as revenue\n"
        + ON
        + ", sum(fee_amount) as fees{% endif %}\n"
        "from {{ ref('fct_order_lines') }}\n"
    ),
    "ledger_entries": (
        "select 'L1' as entry_id, 50 as debit\n" + ON + ", 5 as fee_amount{% endif %}\n"
    ),
    "rpt_fee_disputes": (
        "select order_id\n" + ON + ", fee_amount{% endif %}\n"
        "from {{ ref('stg_orders') }}\nwhere false\n"
    ),
}
#: The failure in the issue: only the mart is gated.
MART_ONLY = {"rpt_revenue": GATED["rpt_revenue"]}

PROJECT = (
    'name: shop\nversion: "1.0"\nprofile: shop\nmodel-paths: [models]\n'
    "models:\n  +materialized: table\n"
)
PROJECT_OFF = PROJECT + f"vars:\n  {FLAG}: false\n"
PROJECT_ON = PROJECT + f"vars:\n  {FLAG}: true\n"
#: The empty model documents its columns as the feature on leaves them.
DOCS = (
    "version: 2\nmodels:\n  - name: rpt_fee_disputes\n    columns:\n"
    "      - name: order_id\n      - name: fee_amount\n"
)

TOGGLE = {
    "variable": FLAG,
    "on_value": True,
    "off_value": False,
    "columns": ["fee_amount", "fees"],
    "exempt_models": ["stg_orders"],
    "assumptions": [
        {
            "decision": "Whether staging keeps the raw fee column",
            "chosen": "staging passes the source through",
            "evidence": "default",
            "detail": "the request names the reporting models",
        }
    ],
}


def _write(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, creating its directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, capsys) -> Path:
    """A shop with fee columns in every layer, built once on DuckDB."""

    pytest.importorskip("dbt.adapters.duckdb")
    _write(
        tmp_path / ".dex/config.yml",
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        "dbt_project_dir: shop\ndbt_target: dev\n",
    )
    shop = tmp_path / "shop"
    _write(shop / "dbt_project.yml", PROJECT)
    _write(
        shop / "profiles.yml",
        "shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
    )
    for name, sql in ORIGINAL.items():
        _write(shop / f"models/{name}.sql", sql)
    _write(shop / "models/_docs.yml", DOCS)
    # An out-of-scope reader of the same flag: reported, never judged.
    _write(shop / "packages.yml", "packages:\n  - local: pkgs/tiny\n")
    _write(
        shop / "pkgs/tiny/dbt_project.yml",
        'name: tiny\nversion: "1.0"\nprofile: shop\nmodel-paths: [models]\n',
    )
    _write(
        shop / "pkgs/tiny/models/pkg_fee_reader.sql",
        f"select {{{{ var('{FLAG}', true) }}}} as fee_flag, 1 as fee_amount\n",
    )
    _run(tmp_path, capsys, "transform", "deps")
    built = _run(tmp_path, capsys, "transform", "build", "--target", "dev")
    assert built["status"] == "ok", built
    return tmp_path


def _run(repo: Path, capsys, *argv: str) -> dict:
    """Run one CLI command in ``repo`` and return its envelope."""

    capsys.readouterr()
    main(["--repo-root", str(repo), *argv])
    return json.loads(capsys.readouterr().out)


def _plan(
    repo: Path,
    capsys,
    sql: dict[str, str],
    *,
    project: str = PROJECT_OFF,
    toggle: dict | None = None,
    extra: list[dict] | None = None,
) -> dict:
    """Plan ``sql`` with the project's flag default and a toggle contract."""

    edits = [
        {"path": f"models/{n}.sql", "kind": "model_sql", "content": c}
        for n, c in sql.items()
    ]
    edits.append({"path": "dbt_project.yml", "kind": "project_yml", "content": project})
    payload = repo / "payload.json"
    payload.write_text(
        json.dumps({"edits": edits + (extra or []), "toggle": toggle or TOGGLE}),
        encoding="utf-8",
    )
    return _run(
        repo, capsys, "transform", "plan", "hide fees", "--edits-file", str(payload)
    )


def _models(envelope: dict) -> dict[str, dict]:
    """The plan's toggle records by model."""

    return {r["model"]: r for r in envelope["data"]["toggle"]["models"]}


def _columns(repo: Path, relation: str) -> list[str]:
    """A dev relation's columns, read with DuckDB directly."""

    import duckdb

    with duckdb.connect(str(repo / "dev.duckdb"), read_only=True) as con:
        return [r[0] for r in con.execute(f"describe {relation}").fetchall()]


def _build(repo: Path, capsys, plan_id: str) -> dict:
    """Build on dev with ``--verify`` for ``plan_id``."""

    return _run(
        repo,
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--verify",
        "--for-plan",
        plan_id,
    )


def test_gating_only_the_mart_names_every_other_producer(repo: Path, capsys) -> None:
    """Gating only the mart names every other producer."""

    out = _plan(repo, capsys, MART_ONLY)
    assert out["status"] == "ok", out
    toggle = out["data"]["toggle"]
    assert toggle["verdict"] == "contradicted"
    models = _models(out)
    # Every producer the edits did not touch, the disconnected ledger, the empty
    # table and the model that only inherits the column through a star.
    for name in (
        "fct_order_lines",
        "ledger_entries",
        "rpt_fee_disputes",
        "rpt_lines_wide",
    ):
        assert models[name]["finding"] == "governed_column_present_when_off", name
        assert "fee_amount" in models[name]["branches"]["off"], name
    assert models["rpt_revenue"]["status"] == "satisfied"
    assert models["rpt_revenue"]["produces"] == ["fees"]
    # Exempt: keeps the raw column off, and is still a producer on.
    assert models["stg_orders"]["status"] == "satisfied"
    assert models["stg_orders"]["exempt"] is True
    assert set(toggle["contract"]["producers"]) == {
        "stg_orders",
        "fct_order_lines",
        "rpt_revenue",
        "ledger_entries",
        "rpt_fee_disputes",
        "rpt_lines_wide",
    }
    assert any("ledger_entries" in w and "still outputs" in w for w in out["warnings"])
    # The package reads the flag and is reported, not judged.
    scope = toggle["package_scope"]
    assert [r["package"] for r in scope["readers"]] == ["tiny"]
    assert "pkg_fee_reader" not in models


def test_every_producer_gated_is_satisfied_and_builds_clean(repo: Path, capsys) -> None:
    """Every producer gated is satisfied and builds clean."""

    out = _plan(repo, capsys, GATED)
    assert out["status"] == "ok", out
    toggle = out["data"]["toggle"]
    assert toggle["verdict"] == "satisfied", toggle
    assert toggle["completeness"] == "complete"
    models = _models(out)
    assert models["fct_order_lines"]["branches"] == {
        "on": ["amount", "fee_amount", "order_id"],
        "off": ["amount", "order_id"],
    }
    assert models["rpt_lines_wide"]["branches"]["off"] == ["amount", "order_id"]
    assert [d["toggle"] for d in out["data"]["decisions"] if "toggle" in d] == [FLAG]
    plan_id = out["data"]["plan_id"]

    compared = _run(
        repo, capsys, "transform", "compare", plan_id, "--model", "fct_order_lines"
    )
    assert compared["data"]["status"] == "equivalent", compared
    assert compared["data"]["proposed_vars"] == {FLAG: True}
    assert compared["data"]["baseline_vars"] == {FLAG: True}

    applied = _run(repo, capsys, "transform", "apply", plan_id)
    assert applied["status"] == "ok", applied
    built = _build(repo, capsys, plan_id)
    assert built["status"] == "ok", built
    verification = built["data"]["verification"]
    records = verification["toggle"]
    off = {r["model"]: r for r in records if r.get("branch") == "off"}
    on = {r["model"]: r for r in records if r.get("branch") == "on"}
    assert all(r["status"] == "satisfied" for r in off.values()), off
    assert off["rpt_fee_disputes"]["status"] == "satisfied"
    assert all(r["reason_code"] == "branch_not_built" for r in on.values())
    # The empty table holds no fee column, read back directly.
    assert _columns(repo, "rpt_fee_disputes") == ["order_id"]
    assert "fee_amount" not in _columns(repo, "ledger_entries")
    assert "fee_amount" in _columns(repo, "stg_orders")
    # The docs describe the on branch; the off build is not a missing column.
    codes = {f["code"] for f in verification["findings"]}
    assert "column_missing" not in codes
    assert "toggle_column_present_when_off" not in codes
    obligations = built["data"]["completion"]["obligations"]
    assert all(o["kind"] != "toggle" for o in obligations.get("contradicted", []))
    assert any(
        o["kind"] == "toggle" and o["reason_code"] == "branch_not_built"
        for o in obligations.get("unchecked", [])
    )


def test_an_off_build_of_the_mart_only_plan_is_contradicted(repo: Path, capsys) -> None:
    """An off build of the mart only plan is contradicted."""

    out = _plan(repo, capsys, MART_ONLY)
    plan_id = out["data"]["plan_id"]
    assert _run(repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _build(repo, capsys, plan_id)
    verification = built["data"]["verification"]
    found = {
        f["identifier"]
        for f in verification["findings"]
        if f["code"] == "toggle_column_present_when_off"
    }
    assert found == {
        "fct_order_lines",
        "ledger_entries",
        "rpt_fee_disputes",
        "rpt_lines_wide",
    }
    # The empty relation fails on its schema alone.
    assert _columns(repo, "rpt_fee_disputes") == ["order_id", "fee_amount"]
    contradicted = built["data"]["completion"]["obligations"]["contradicted"]
    assert {o["model"] for o in contradicted if o["kind"] == "toggle"} == found


def test_an_on_build_keeps_every_producer(repo: Path, capsys) -> None:
    """An on build keeps every producer."""

    out = _plan(repo, capsys, GATED, project=PROJECT_ON)
    assert out["data"]["toggle"]["verdict"] == "satisfied"
    plan_id = out["data"]["plan_id"]
    assert _run(repo, capsys, "transform", "apply", plan_id)["status"] == "ok"
    built = _build(repo, capsys, plan_id)
    records = built["data"]["verification"]["toggle"]
    on = {r["model"]: r for r in records if r.get("branch") == "on"}
    off = {r["model"]: r for r in records if r.get("branch") == "off"}
    assert all(r["status"] == "satisfied" for r in on.values()), on
    assert set(on) == set(out["data"]["toggle"]["contract"]["producers"])
    assert all(r["reason_code"] == "branch_not_built" for r in off.values())
    assert "fee_amount" in _columns(repo, "rpt_fee_disputes")


def test_a_dropped_on_column_and_a_dynamic_read_are_reported(
    repo: Path, capsys
) -> None:
    """A dropped on column and a dynamic read are reported."""

    dropped = dict(GATED)
    dropped["ledger_entries"] = "select 'L1' as entry_id, 50 as debit\n"
    dynamic = {
        "path": "models/rpt_dynamic.sql",
        "kind": "model_sql",
        "content": "{% set name = 'enable_' ~ 'fees' %}\n"
        "select {{ var(name, true) }} as flagged\n",
    }
    out = _plan(repo, capsys, dropped, extra=[dynamic])
    toggle = out["data"]["toggle"]
    models = _models(out)
    assert models["ledger_entries"]["finding"] == "governed_column_lost_when_on"
    assert toggle["completeness"] == "incomplete"
    assert any("rpt_dynamic.sql" in limit for limit in toggle["limits"])


def test_refusals(repo: Path, capsys) -> None:
    """Refusals."""

    payload = repo / "payload.json"

    def run(body: dict, *argv: str) -> dict:
        """Run ``argv`` with ``body`` as its payload file."""

        payload.write_text(json.dumps(body), encoding="utf-8")
        return _run(repo, capsys, *argv, "--edits-file", str(payload))

    edits = [
        {"path": f"models/{n}.sql", "kind": "model_sql", "content": c}
        for n, c in GATED.items()
    ]
    missing = {k: v for k, v in TOGGLE.items() if k != "off_value"}
    out = run({"edits": edits, "toggle": missing}, "transform", "plan", "x")
    assert out["status"] == "error"
    assert "off_value" in out["errors"][0]

    unread = {**TOGGLE, "variable": "nobody_reads_this"}
    out = run({"edits": edits, "toggle": unread}, "transform", "plan", "x")
    assert out["status"] == "error"
    assert "nobody_reads_this" in out["errors"][0]

    out = run(
        {"edits": edits, "toggle": TOGGLE, "retirement": {"retained_value": False}},
        "transform",
        "plan",
        "x",
    )
    assert out["status"] == "error"
    assert "not both" in out["errors"][0]

    out = run({"edits": edits, "toggle": TOGGLE}, "transform", "remove", "var", FLAG)
    assert out["status"] == "error"
    assert "only `transform plan` takes a toggle" in out["errors"][0]

    out = run({"edits": [], "toggle": TOGGLE}, "transform", "declare")
    assert out["status"] == "error"
    assert "toggle" in out["errors"][0]


def test_the_plan_document_carries_the_toggle(repo: Path, capsys) -> None:
    """The plan document carries the toggle."""

    out = _plan(repo, capsys, GATED)
    plan_id = out["data"]["plan_id"]
    exported = _run(repo, capsys, "transform", "export", plan_id)
    document = exported["data"]["plan"]
    assert document["toggle"]["variable"] == FLAG
    assert document["toggle"]["producers"]["ledger_entries"] == ["fee_amount"]
    tampered = json.loads(json.dumps(document))
    tampered["toggle"]["off_value"] = True
    from exmergo_dex_core.transform.portable import plan_digest

    assert plan_digest(tampered) != plan_digest(document)


def test_a_failed_analysis_keeps_the_plan_and_its_contract(
    repo: Path, capsys, monkeypatch
) -> None:
    """The plan is stored before the analysis runs, so an analysis that raises
    reports unchecked and leaves the contract on the plan for a later build."""

    from exmergo_dex_core.transform import toggle as toggle_mod

    def broken(*_args, **_kwargs):
        """Fail the way a defect in the analysis would."""

        raise KeyError("boom")

    monkeypatch.setattr(toggle_mod, "analyze", broken)
    out = _plan(repo, capsys, GATED)
    assert out["status"] == "ok", out
    assert out["data"]["toggle"]["verdict"] == "unchecked"
    assert "KeyError" in out["data"]["toggle"]["limits"][0]
    exported = _run(repo, capsys, "transform", "export", out["data"]["plan_id"])
    assert exported["data"]["plan"]["toggle"]["variable"] == FLAG
    assert exported["data"]["plan"]["toggle"]["analysis_failed"] == "KeyError"


def test_compare_refuses_a_parent_that_moves_with_the_flag(repo: Path, capsys) -> None:
    """A parent the plan leaves alone that reads the flag compiles differently at
    the on value than the dev relation it was built as, so a comparison there
    cannot rest on it."""

    first = _plan(
        repo,
        capsys,
        {
            "stg_orders": STG.replace(
                "3 as fee_amount",
                "{% if var('"
                + FLAG
                + "', true) %}3{% else %}0{% endif %} as fee_amount",
            )
        },
        toggle={**TOGGLE, "exempt_models": ["stg_orders"]},
    )
    assert first["status"] == "ok", first
    assert (
        _run(repo, capsys, "transform", "apply", first["data"]["plan_id"])["status"]
        == "ok"
    )
    assert _run(repo, capsys, "transform", "build", "--target", "dev")["status"] == "ok"
    out = _plan(repo, capsys, GATED)
    compared = _run(
        repo,
        capsys,
        "transform",
        "compare",
        out["data"]["plan_id"],
        "--model",
        "fct_order_lines",
    )
    assert compared["data"]["status"] == "not_checked", compared
    assert compared["data"]["reason_code"] == "inputs_unsynchronized"
    assert "stg_orders" in compared["data"]["detail"]


def test_compare_refuses_a_parent_only_the_on_branch_reads(repo: Path, capsys) -> None:
    """A parent read only with the flag on is absent from the graph the dev
    relations were built from, so whatever relation an earlier build left under
    its name cannot stand for it."""

    rates = {
        "path": "models/fee_rates.sql",
        "kind": "model_sql",
        "content": "select 0.03 as rate\n",
    }
    payload = repo / "rates.json"
    payload.write_text(json.dumps({"edits": [rates]}), encoding="utf-8")
    added = _run(
        repo, capsys, "transform", "plan", "rates", "--edits-file", str(payload)
    )
    assert added["status"] == "ok", added
    assert (
        _run(repo, capsys, "transform", "apply", added["data"]["plan_id"])["status"]
        == "ok"
    )
    assert _run(repo, capsys, "transform", "build", "--target", "dev")["status"] == "ok"
    joined = dict(GATED)
    joined["fct_order_lines"] = (
        "select o.order_id, o.amount\n"
        + ON
        + ", o.fee_amount, r.rate as fee_rate{% endif %}\n"
        "from {{ ref('stg_orders') }} as o\n"
        + ON
        + " cross join {{ ref('fee_rates') }} as r{% endif %}\n"
    )
    out = _plan(
        repo,
        capsys,
        joined,
        toggle={**TOGGLE, "columns": ["fee_amount", "fees", "fee_rate"]},
    )
    assert out["status"] == "ok", out
    compared = _run(
        repo,
        capsys,
        "transform",
        "compare",
        out["data"]["plan_id"],
        "--model",
        "fct_order_lines",
    )
    assert compared["data"]["status"] == "not_checked", compared
    assert compared["data"]["reason_code"] == "inputs_unsynchronized"
    assert "read only with" in compared["data"]["detail"]
    assert "fee_rates" in compared["data"]["detail"]


# --- tests and semantic definitions over a governed column (#1169) -------------

#: A not_null on a gated producer's fee column, which still runs with the flag
#: off; the same test disabled at the off value; and one on the exempt staging
#: model, which keeps the column off.
FEE_TESTS = (
    "version: 2\n"
    "models:\n"
    "  - name: fct_order_lines\n"
    "    columns:\n"
    "      - name: fee_amount\n"
    "        data_tests:\n"
    "          - not_null\n"
    "  - name: ledger_entries\n"
    "    columns:\n"
    "      - name: fee_amount\n"
    "        data_tests:\n"
    "          - not_null:\n"
    "              config:\n"
    "                enabled: \"{{ var('" + FLAG + "') }}\"\n"
    "  - name: stg_orders\n"
    "    columns:\n"
    "      - name: fee_amount\n"
    "        data_tests:\n"
    "          - not_null\n"
)

#: A gated producer with a time column, so a semantic model can sit on it.
FEE_DAYS = (
    "select order_id, cast('2024-01-01' as date) as order_day\n"
    + ON
    + ", fee_amount{% endif %}\nfrom {{ ref('stg_orders') }}\n"
)
SPINE = "select cast('2024-01-01' as date) as date_day\n"
SPINE_DOCS = (
    "version: 2\nmodels:\n  - name: metricflow_time_spine\n    time_spine:\n"
    "      standard_granularity_column: date_day\n    columns:\n"
    "      - name: date_day\n        granularity: day\n"
)
FEE_SEMANTIC = (
    "version: 2\n"
    "semantic_models:\n"
    "  - name: fee_days\n"
    "    model: ref('fct_fee_days')\n"
    "    defaults:\n"
    "      agg_time_dimension: order_day\n"
    "    entities:\n"
    "      - name: order_id\n"
    "        type: primary\n"
    "    dimensions:\n"
    "      - name: order_day\n"
    "        type: time\n"
    "        type_params:\n"
    "          time_granularity: day\n"
    "      - name: fee_band\n"
    "        type: categorical\n"
    "        expr: fee_amount\n"
    "    measures:\n"
    "      - name: total_fees\n"
    "        agg: sum\n"
    "        expr: fee_amount\n"
    "      - name: doubled_fees\n"
    "        agg: sum\n"
    "        expr: fee_amount * 2\n"
    "metrics:\n"
    "  - name: fee_revenue\n"
    "    label: Fee revenue\n"
    "    type: simple\n"
    "    type_params:\n"
    "      measure: total_fees\n"
)


def _line(text: str, needle: str) -> int:
    """The 1-based line of ``needle``'s only occurrence in ``text``."""

    found = [n for n, line in enumerate(text.splitlines(), 1) if needle in line]
    assert len(found) == 1, (needle, found)
    return found[0]


def _checks(record: dict, code: str) -> list[dict]:
    """The checks of ``code`` a toggle record carries."""

    return [c for c in record.get("checks") or [] if c["check"] == code]


def test_a_test_still_running_off_on_a_governed_column_is_named(
    repo: Path, capsys
) -> None:
    """A not_null on a governed column fails the off build, so the plan names it
    with its file and line; one disabled at the off value, and one on an exempt
    model, are not reported."""

    tests = {
        "path": "models/_fee_tests.yml",
        "kind": "schema_yml",
        "content": FEE_TESTS,
    }
    out = _plan(repo, capsys, GATED, extra=[tests])
    assert out["status"] == "ok", out
    toggle = out["data"]["toggle"]
    assert toggle["verdict"] == "contradicted", toggle
    models = _models(out)
    found = _checks(models["fct_order_lines"], "governed_column_tested_when_off")
    assert len(found) == 1, models["fct_order_lines"]
    assert found[0]["path"] == "models/_fee_tests.yml"
    # The first bare not_null, under fct_order_lines.fee_amount.
    assert found[0]["line"] == FEE_TESTS.splitlines().index("          - not_null") + 1
    assert found[0]["column"] == "fee_amount"
    assert found[0]["test"] == "not_null"
    assert models["fct_order_lines"]["status"] == "contradicted"
    # Disabled with the flag off: dbt never runs it there.
    assert not _checks(
        models.get("ledger_entries", {}), "governed_column_tested_when_off"
    )
    # Exempt: the column stays with the flag off, so the test still passes.
    assert not _checks(models.get("stg_orders", {}), "governed_column_tested_when_off")
    assert any(
        "models/_fee_tests.yml:" in w and "not_null" in w for w in out["warnings"]
    ), out["warnings"]


def test_a_semantic_definition_over_a_governed_column_is_named(
    repo: Path, capsys
) -> None:
    """A dimension or measure over a governed column stops resolving with the flag
    off, so the plan names it, with the metric on it; a computed expression the
    index cannot resolve is listed in the limits."""

    extra = [
        {"path": "models/fct_fee_days.sql", "kind": "model_sql", "content": FEE_DAYS},
        {
            "path": "models/metricflow_time_spine.sql",
            "kind": "model_sql",
            "content": SPINE,
        },
        {"path": "models/_spine.yml", "kind": "schema_yml", "content": SPINE_DOCS},
        {
            "path": "models/_fee_semantic.yml",
            "kind": "semantic_yml",
            "content": FEE_SEMANTIC,
        },
    ]
    out = _plan(repo, capsys, GATED, extra=extra)
    assert out["status"] == "ok", out
    toggle = out["data"]["toggle"]
    assert toggle["verdict"] == "contradicted", toggle
    record = _models(out)["fct_fee_days"]
    found = {
        c["definition"]: c for c in _checks(record, "governed_column_in_semantic_layer")
    }
    assert set(found) == {"fee_band", "total_fees"}, record
    assert found["fee_band"]["kind"] == "dimension"
    assert found["fee_band"]["path"] == "models/_fee_semantic.yml"
    assert found["fee_band"]["line"] == _line(FEE_SEMANTIC, "- name: fee_band")
    assert found["total_fees"]["kind"] == "measure"
    assert found["total_fees"]["line"] == _line(FEE_SEMANTIC, "- name: total_fees")
    assert found["total_fees"]["metrics"] == ["fee_revenue"]
    assert all(c["column"] == "fee_amount" for c in found.values())
    # The computed measure is unresolved, so it is a limit, never dropped.
    assert toggle["completeness"] == "incomplete"
    assert any(
        "models/_fee_semantic.yml:" in limit and "doubled_fees" in limit
        for limit in toggle["limits"]
    ), toggle["limits"]
    assert any("fee_band" in w for w in out["warnings"]), out["warnings"]
