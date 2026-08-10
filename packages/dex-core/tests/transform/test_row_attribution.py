"""`transform plan` row-population attribution: name every change that can move
rows, measure each one against the prior model, and say so when it cannot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.transform.row_attribution import (
    MAX_CHANGES_PER_MODEL,
    naming_resolver,
    render_model_sql,
)

from .conftest import MODEL_PATH, PRIOR_MODEL


def _plan(repo: Path, warehouse: Path, payload: Path, capsys, *flags: str) -> dict:
    rc = main(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            "swap the cancellation filter",
            "--edits-file",
            str(payload),
            "--connector",
            "duckdb",
            "--path",
            str(warehouse),
            *flags,
        ]
    )
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    envelope = json.loads(out)
    assert rc == 0, envelope
    return envelope


def _attribution(envelope: dict) -> dict:
    models = envelope["data"]["row_attribution"]
    assert len(models) == 1, models
    return models[0]


def _by_kind(model: dict, kind: str) -> list[dict]:
    return [c for c in model["changes"] if c["kind"] == kind]


def _untouched(repo: Path) -> None:
    """Propose-don't-impose: attribution reads the project and never writes it."""

    assert (repo / "analytics" / MODEL_PATH).read_text() == PRIOR_MODEL


# --- attribution, the whole point ------------------------------------------------


def test_a_swapped_source_predicate_reports_its_own_delta(
    attributable_project, plan_edit, capsys
):
    """The benchmark failure: a predicate nobody asked to change, swapped for a
    defensible-looking one, moving rows on its own."""

    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "where status != 'CANCELLED' and total_revenue > 0",
            "where not is_cancelled and total_revenue > 0",
        )
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))

    swap = _by_kind(model, "predicate_replaced")
    assert len(swap) == 1, model["changes"]
    assert swap[0]["prior"] == "status <> 'CANCELLED'"
    assert swap[0]["authored"] == "NOT is_cancelled"
    assert swap[0]["attributed"] is True
    # The delta is this predicate's alone, measured against the prior model.
    assert swap[0]["delta"] != 0
    assert swap[0]["rows"] == model["baseline_rows"] + swap[0]["delta"]
    assert swap[0]["scope"] == "base", "the change is attributed to the CTE it is in"
    _untouched(repo)


def test_several_row_affecting_changes_are_attributed_independently(
    attributable_project, plan_edit, capsys
):
    """A net figure cannot separate a requested change from a side effect, so
    each change is measured on its own against a common baseline."""

    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "where status != 'CANCELLED' and total_revenue > 0",
            "where not is_cancelled and customer_id is not null",
        )
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))

    measured = [c for c in model["changes"] if c["attributed"]]
    assert len(measured) >= 3, model["changes"]
    assert all(c["delta"] is not None for c in measured)
    assert all(c["rows"] == model["baseline_rows"] + c["delta"] for c in measured), (
        "every delta is measured against the same prior-model baseline"
    )
    # The whole-model net is measured, never summed from the parts.
    assert model["net_delta"] == model["authored_rows"] - model["baseline_rows"]
    _untouched(repo)


def test_interacting_changes_are_declared_rather_than_left_to_add_up(
    attributable_project, plan_edit, capsys
):
    """Isolated counterfactuals do not compose. When they disagree with the
    whole-edit net, the report says so instead of letting a reader total the
    column and trust a number the warehouse would never produce."""

    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "where status != 'CANCELLED' and total_revenue > 0",
            "where not is_cancelled and customer_id is not null",
        )
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))

    summed = sum(c["delta"] for c in model["changes"] if c["delta"] is not None)
    assert model["interacts"] is (summed != model["net_delta"])
    assert model["interacts"] is True, (
        "these four predicate changes do interact, so the fixture proves the flag "
        "fires rather than only that the arithmetic matches"
    )


def test_a_join_retyped_to_left_is_reported_like_a_predicate_change(
    attributable_project, plan_edit, capsys
):
    repo, warehouse = attributable_project
    inner = PRIOR_MODEL.replace(
        "select * from {{ source('raw', 'raw_orders') }}",
        "select raw_orders.* from {{ source('raw', 'raw_orders') }}\n"
        "    inner join {{ source('raw', 'customers') }} "
        "on customers.id = raw_orders.customer_id",
    )
    (repo / "analytics" / MODEL_PATH).write_text(inner, encoding="utf-8")
    payload = plan_edit(inner.replace("inner join", "left join"))

    model = _attribution(_plan(repo, warehouse, payload, capsys))
    retyped = _by_kind(model, "join_side_changed")
    assert len(retyped) == 1, model["changes"]
    assert "inner" in retyped[0]["detail"] and "left" in retyped[0]["detail"]
    assert retyped[0]["attributed"] is True
    assert retyped[0]["delta"] is not None


def test_an_added_source_relation_is_reported_on_the_same_footing(
    attributable_project, plan_edit, capsys
):
    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "select * from {{ source('raw', 'raw_orders') }}",
            "select raw_orders.* from {{ source('raw', 'raw_orders') }}\n"
            "    inner join {{ source('raw', 'customers') }} "
            "on customers.id = raw_orders.customer_id",
        )
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    added = _by_kind(model, "join_added")
    assert len(added) == 1, model["changes"]
    assert "customers" in added[0]["detail"]
    assert added[0]["delta"] is not None


def test_a_changed_driving_relation_is_reported(
    attributable_project, plan_edit, capsys
):
    repo, warehouse = attributable_project
    # `select *` on both sides, so the counterfactual stays valid SQL and the
    # swap is measured rather than degrading to "could not attribute".
    trivial = (
        "with base as (\n"
        "    select * from {{ source('raw', 'customers') }}\n"
        ")\nselect * from base\n"
    )
    (repo / "analytics" / MODEL_PATH).write_text(trivial, encoding="utf-8")
    payload = plan_edit(
        trivial.replace("source('raw', 'customers')", "source('raw', 'raw_orders')")
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    swapped = _by_kind(model, "source_changed")
    assert len(swapped) == 1, model["changes"]
    assert swapped[0]["delta"] is not None


def test_a_grain_change_is_reported(attributable_project, plan_edit, capsys):
    """DISTINCT is measured over the projection the prior model already had, so
    the fixture narrows that projection first; adding DISTINCT to a select that
    still carries a unique key deduplicates nothing, which is correct and would
    make this test prove nothing."""

    repo, warehouse = attributable_project
    narrow = PRIOR_MODEL.replace(
        "select order_id, customer_id, total_revenue from base",
        "select customer_id from base",
    )
    (repo / "analytics" / MODEL_PATH).write_text(narrow, encoding="utf-8")
    payload = plan_edit(
        narrow.replace(
            "select customer_id from base", "select distinct customer_id from base"
        )
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    distinct = _by_kind(model, "distinct_added")
    assert len(distinct) == 1, model["changes"]
    assert distinct[0]["delta"] < 0, "deduplicating to one row per customer loses rows"


# --- what must stay silent -------------------------------------------------------


def test_an_edit_to_column_expressions_alone_reports_nothing(
    attributable_project, plan_edit, capsys
):
    """Aliases, casts, added expressions and ordering cannot change which rows
    enter a model, so they produce no key at all rather than an empty one."""

    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "select order_id, customer_id, total_revenue from base",
            "select\n"
            "    order_id as id,\n"
            "    customer_id,\n"
            "    round(total_revenue, 1) as revenue,\n"
            "    cast(order_id as varchar) as order_key\n"
            "from base\norder by 1",
        )
    )
    envelope = _plan(repo, warehouse, payload, capsys)
    assert "row_attribution" not in envelope["data"]
    assert envelope["status"] == "ok"


def test_re_aliasing_a_relation_is_not_a_source_change(
    attributable_project, plan_edit, capsys
):
    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "select * from {{ source('raw', 'raw_orders') }}",
            "select o.* from {{ source('raw', 'raw_orders') }} o",
        ).replace("where status", "where o.status")
    )
    envelope = _plan(repo, warehouse, payload, capsys)
    models = envelope["data"].get("row_attribution") or []
    assert not any(_by_kind(model, "source_changed") for model in models), (
        "an alias is notation, not a different relation"
    )


def test_authoring_a_new_model_produces_no_findings_and_opens_nothing(
    attributable_project, plan_edit, capsys, monkeypatch
):
    """A model with no prior version has no behaviour to compare against, and
    must not be a reason to touch the warehouse."""

    from exmergo_dex_core.engine import DexEngine

    repo, warehouse = attributable_project
    monkeypatch.setattr(
        DexEngine,
        "_adapter",
        lambda *a, **k: pytest.fail("planning a new model opened a connection"),
    )
    payload = plan_edit("select 1 as id\n", path="models/marts/fct_new.sql")
    envelope = _plan(repo, warehouse, payload, capsys)
    assert envelope["status"] == "ok"
    assert "row_attribution" not in envelope["data"]


# --- honest degradation ----------------------------------------------------------


def test_a_macro_generated_predicate_says_it_could_not_be_attributed(
    attributable_project, plan_edit, capsys
):
    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace("status != 'CANCELLED'", "{{ not_cancelled('status') }}")
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    assert all(c["attributed"] is False for c in model["changes"])
    reasons = " ".join(c["reason"] for c in model["changes"])
    assert "not_cancelled" in reasons and "only dbt can render" in reasons


def test_a_jinja_conditional_says_it_could_not_be_attributed(
    attributable_project, plan_edit, capsys
):
    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "and total_revenue > 0",
            "{% if is_incremental() %} and order_id > 100 {% endif %}",
        )
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    assert all(c["attributed"] is False for c in model["changes"])
    assert "jinja statement block" in model["changes"][0]["reason"]


def test_an_unprofiled_parent_names_the_command_that_fixes_it(
    attributable_project, plan_edit, capsys
):
    """The counting half runs through the query firewall, so a relation the
    cache cannot speak for degrades exactly the way it does everywhere else."""

    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "source('raw', 'raw_orders')", "source('raw', 'never_profiled')"
        )
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    reasons = " ".join(c["reason"] or "" for c in model["changes"])
    assert "never_profiled" in reasons
    assert "explore profile" in reasons
    assert all(c["delta"] is None for c in model["changes"])


def test_a_renamed_cte_is_declared_rather_than_paired_by_guess(
    attributable_project, plan_edit, capsys
):
    """An unalignable scope still gets the whole-model net, which is the number
    that says how much the rename moved even though nothing can say what did."""

    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace("base", "filtered").replace("and total_revenue > 0", "")
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    unattributable = _by_kind(model, "unattributable")
    assert len(unattributable) == 2, model["changes"]
    assert {c["scope"] for c in unattributable} == {"base", "filtered"}
    assert not _by_kind(model, "source_changed"), (
        "a renamed CTE is bookkeeping, not the model being repointed"
    )
    assert model["net_delta"] is not None


def test_a_change_that_cannot_stand_alone_says_so_and_spares_the_others(
    attributable_project, plan_edit, capsys
):
    """A predicate over a column an added join brings in cannot be evaluated
    against the prior model by itself. That one change reports why; the rest of
    the edit is still measured."""

    repo, warehouse = attributable_project
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "select * from {{ source('raw', 'raw_orders') }}",
            "select raw_orders.* from {{ source('raw', 'raw_orders') }}\n"
            "    inner join {{ source('raw', 'customers') }} "
            "on customers.id = raw_orders.customer_id",
        ).replace("and total_revenue > 0", "and customers.region = 'r1'")
    )
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    stranded = [c for c in model["changes"] if not c["attributed"]]
    assert stranded, model["changes"]
    assert any(
        "does not provide everything it references" in c["reason"] for c in stranded
    )
    # The net is still measured: the authored model is valid, only the isolated
    # counterfactual is not.
    assert model["net_delta"] is not None


def test_the_per_model_cap_is_stated_rather_than_silently_applied(
    attributable_project, plan_edit, capsys
):
    repo, warehouse = attributable_project
    predicates = " and ".join(
        f"order_id != {n}" for n in range(MAX_CHANGES_PER_MODEL + 2)
    )
    payload = plan_edit(
        PRIOR_MODEL.replace(
            "where status != 'CANCELLED' and total_revenue > 0", f"where {predicates}"
        )
    )
    envelope = _plan(repo, warehouse, payload, capsys)
    model = _attribution(envelope)
    assert any("named only" in w for w in envelope["warnings"]), envelope["warnings"]
    assert sum(1 for c in model["changes"] if c["delta"] is not None) == (
        MAX_CHANGES_PER_MODEL
    )
    assert any(
        c["reason"] == "beyond the per-model attribution cap" for c in model["changes"]
    )


# --- the free/gated split --------------------------------------------------------


def test_no_attribute_rows_names_the_change_without_measuring_it(
    attributable_project, plan_edit, capsys, monkeypatch
):
    from exmergo_dex_core.engine import DexEngine

    repo, warehouse = attributable_project
    monkeypatch.setattr(
        DexEngine,
        "_adapter",
        lambda *a, **k: pytest.fail("--no-attribute-rows opened a connection"),
    )
    payload = plan_edit(PRIOR_MODEL.replace(" and total_revenue > 0", ""))
    model = _attribution(_plan(repo, warehouse, payload, capsys, "--no-attribute-rows"))
    assert model["changes"], "the change is still named"
    assert all(c["delta"] is None for c in model["changes"])
    assert all(c["attributed"] is False for c in model["changes"])
    assert model["baseline_rows"] is None


def test_a_free_connector_measures_without_being_asked(
    attributable_project, plan_edit, capsys
):
    repo, warehouse = attributable_project
    payload = plan_edit(PRIOR_MODEL.replace(" and total_revenue > 0", ""))
    model = _attribution(_plan(repo, warehouse, payload, capsys))
    assert model["baseline_rows"] is not None
    assert any(c["delta"] is not None for c in model["changes"])


# --- the rendering seam ----------------------------------------------------------


@pytest.mark.parametrize(
    ("model_sql", "expected"),
    [
        ("{{ config(materialized='view') }}\nselect 1", "select 1"),
        ("select * from {{ ref('stg_orders') }}", "select * from stg_orders"),
        ("select * from {{ source('raw', 'orders') }}", "select * from orders"),
        ("select * from {{ ref('pkg', 'stg_orders') }}", "select * from stg_orders"),
    ],
)
def test_rendering_reduces_a_model_to_comparable_sql(model_sql, expected):
    assert render_model_sql(model_sql, naming_resolver) == expected


# --- the metered half ------------------------------------------------------------

pytest.importorskip("google.cloud.bigquery")

from exmergo_dex_core.adapters.bigquery import BigQueryAdapter  # noqa: E402
from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache  # noqa: E402
from exmergo_dex_core.config import BigQueryTarget, DexConfig  # noqa: E402
from exmergo_dex_core.engine import DexEngine  # noqa: E402
from exmergo_dex_core.envelope import Paradigm  # noqa: E402
from exmergo_dex_core.guards.cost_guard import CostGate  # noqa: E402
from exmergo_dex_core.storage import FilesystemStore  # noqa: E402

BQ_MODEL = (
    "with base as (\n"
    "    select * from {{ source('shop', 'orders') }}\n"
    "    where status != 'CANCELLED'\n"
    ")\nselect order_id from base\n"
)


@pytest.fixture
def orders_bq_client():
    """A fake BigQuery holding the one table the metered model reads, and
    answering every count with a row so the settle path is exercised."""

    bigquery = pytest.importorskip("google.cloud.bigquery")
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    return FakeBigQueryClient(
        project="test-proj",
        tables=[
            FakeTable(
                project="test-proj",
                dataset_id="shop",
                table_id="orders",
                schema=[
                    bigquery.SchemaField("order_id", "INTEGER"),
                    bigquery.SchemaField("status", "STRING"),
                    bigquery.SchemaField("is_cancelled", "BOOLEAN"),
                ],
                num_rows=100,
                num_bytes=50_000,
            )
        ],
        row_resolver=lambda sql: [{"dex_rows": 42}],
    )


@pytest.fixture
def metered_project(tmp_path: Path, orders_bq_client, monkeypatch):
    """A dbt project on BigQuery, cache and all, with the engine's one adapter
    funnel routed at the fake. Returns a callable running `transform plan`."""

    fake_bq_client = orders_bq_client
    project = tmp_path / "analytics"
    (project / "models" / "marts").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0.0"\nprofile: shop\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / MODEL_PATH).write_text(BQ_MODEL, encoding="utf-8")

    store = FilesystemStore(tmp_path)
    store.save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="test-proj.shop.orders",
                    row_count=100,
                    columns=[
                        ColumnProfile(name="order_id", data_type="INT64"),
                        ColumnProfile(name="status", data_type="STRING"),
                        ColumnProfile(name="is_cancelled", data_type="BOOL"),
                    ],
                )
            ]
        )
    )

    opened: list[str] = []

    def opener(self, command=None, *, budget=None, confirmed=None):
        opened.append(command or "")
        gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=self.budget if budget is None else budget,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=self.confirmed if confirmed is None else confirmed,
            connector="bigquery",
            command=command,
        )
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=gate,
            target=BigQueryTarget(),
            client=fake_bq_client,
            principal_type="user",
        )

    monkeypatch.setattr(DexEngine, "_adapter", opener)

    def run(*, attribute_rows=None, confirm=False, budget=None):
        from exmergo_dex_core.cli import dispatch

        payload = tmp_path / "edits.json"
        payload.write_text(
            json.dumps(
                {
                    "edits": [
                        {
                            "path": MODEL_PATH,
                            "kind": "model_sql",
                            "content": BQ_MODEL.replace(
                                "status != 'CANCELLED'", "not is_cancelled"
                            ),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            connector="bigquery",
            path=None,
            repo_root=str(tmp_path),
            confirm=confirm,
            budget=budget,
            group="transform",
            subcommand="plan",
            argument="swap the filter",
            edits_file=str(payload),
            scaffold=None,
            attribute_rows=attribute_rows,
        )
        engine = DexEngine(
            connector="bigquery",
            repo_root=str(tmp_path),
            store=store,
            config=DexConfig(connector="bigquery"),
            confirmed=confirm,
            budget=budget,
        )
        return dispatch(args, engine), opened

    return run


def test_a_metered_plan_names_the_change_and_opens_nothing_by_default(
    metered_project,
):
    """The default on a billed connector is exactly today's behaviour plus a
    sentence, because a handshake on a command that mostly costs nothing trains
    people to click through the ones that do."""

    envelope, opened = metered_project()
    assert envelope.status.value == "ok"
    assert opened == [], "no connection is opened without --attribute-rows"
    model = envelope.data["row_attribution"][0]
    assert all(c["attributed"] is False for c in model["changes"])
    assert any("--attribute-rows" in (c["reason"] or "") for c in model["changes"])
    assert envelope.data["spend"] is None if "spend" in envelope.data else True


def test_a_metered_plan_asked_to_measure_prices_it_and_keeps_the_plan(
    metered_project,
):
    """The plan is stored before any of this, so the ask rides back beside it
    rather than replacing it: re-running confirmed measures without re-planning."""

    envelope, opened = metered_project(attribute_rows=True)
    assert envelope.status.value == "needs_confirmation"
    assert envelope.cost.estimate > 0
    assert opened == ["transform plan"]
    # The plan itself survives the ask, diffs and all.
    assert envelope.data["plan_id"]
    assert envelope.data["paths"] == [MODEL_PATH]
    assert envelope.diffs and envelope.diffs[0]["unified"]
    assert "--confirm" in envelope.data["hint"]


def test_a_confirmed_metered_plan_measures_and_settles_its_spend(metered_project):
    envelope, _opened = metered_project(
        attribute_rows=True, confirm=True, budget=10_000_000_000
    )
    assert envelope.status.value == "ok"
    model = envelope.data["row_attribution"][0]
    assert model["baseline_rows"] is not None
    assert envelope.data["spend"]["bytes_billed"] > 0


def test_an_over_ceiling_attribution_is_refused_and_confirm_cannot_override(
    metered_project,
):
    envelope, _opened = metered_project(attribute_rows=True, confirm=True, budget=1)
    assert envelope.status.value == "error"
    assert "ceiling" in " ".join(envelope.errors).lower()
