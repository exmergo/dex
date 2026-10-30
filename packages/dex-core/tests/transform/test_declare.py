"""`transform declare` proposes declarations for models that already exist (#501).

The unit half drives :func:`declare.select_models` and :func:`declare.propose`
over an in-memory project and a hand-written draft, one rule per case, because
each rule is a claim about what may be written into a model a human already
owns. The end-to-end half runs the shipped commands over a real DuckDB
warehouse, so the draft is the one `explore` and `transform plan` produce.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
from exmergo_dex_core.transform import declare as declare_mod
from exmergo_dex_core.transform.declarations import parse_declarations

ORDERS = "select order_id, customer_id, amount from {{ ref('stg_orders') }}\n"
CUSTOMERS = "select customer_id, region from {{ source('raw', 'customers') }}\n"


def _view(files: dict[str, str]) -> DbtProjectView:
    return DbtProjectView(
        root=".",
        project_name="p",
        profile_name="p",
        files={
            path: SourceFile(path=path, content=content, sha256="x")
            for path, content in files.items()
        },
    )


def _fact(value: Any, evidence: str = "measured") -> dict[str, Any]:
    return {"value": value, "evidence": evidence}


def _draft(
    *,
    grain: list[str] | None = None,
    columns: dict[str, dict[str, Any]] | None = None,
    declared: dict[str, Any] | None = None,
    inputs: list[dict[str, Any]] | None = None,
    incomplete: list[str] | None = None,
) -> dict[str, Any]:
    drafted: dict[str, Any] = {}
    if grain:
        drafted["grain"] = _fact(grain, "a proven key")
    if columns:
        drafted["columns"] = columns
    if inputs:
        drafted["inputs"] = inputs
    if incomplete:
        drafted["incomplete"] = incomplete
    return {"drafted": drafted, "declared": declared or {}, "missing": ["description"]}


def _propose(
    files: dict[str, str],
    drafts: dict[str, dict[str, Any]],
    *,
    models: list[str] | None = None,
    supplied: list[dict[str, Any]] | None = None,
    cap: int = declare_mod.MAX_MODELS,
) -> declare_mod.Outcome:
    view = _view(files)
    return declare_mod.propose(
        view,
        models if models is not None else sorted(drafts),
        parse_declarations(supplied or []),
        draft=lambda model: (drafts.get(model), []),
        cap=cap,
    )


# --- the selector -----------------------------------------------------------------


def _shop() -> dict[str, str]:
    return {
        "models/staging/stg_orders.sql": "select 1 as order_id\n",
        "models/staging/stg_customers.sql": CUSTOMERS,
        "models/marts/fct_orders.sql": ORDERS,
    }


def test_no_selector_selects_every_model_in_path_order():
    selection = declare_mod.select_models(_view(_shop()), [])
    assert selection.models == ["fct_orders", "stg_customers", "stg_orders"]
    assert selection.skipped == []


def test_the_selector_reads_names_the_way_maintain_verify_does():
    selection = declare_mod.select_models(
        _view(_shop()), ["STG_ORDERS,fct_orders", "stg_orders"]
    )
    # Case-insensitive, comma-split, de-duplicated, in the order named.
    assert selection.models == ["stg_orders", "fct_orders"]


def test_a_name_that_matches_no_model_is_named_rather_than_dropped():
    selection = declare_mod.select_models(_view(_shop()), ["fct_ordrs"])
    assert selection.models == []
    assert selection.skipped == [
        {
            "model": "fct_ordrs",
            "reason": "not_a_model",
            "detail": "no model named 'fct_ordrs' is defined under models",
        }
    ]


def test_a_python_model_is_skipped_with_its_file(tmp_path: Path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "scores.py").write_text("def model(dbt, s): ...\n")
    selection = declare_mod.select_models(_view(_shop()), ["scores"], tmp_path)
    assert selection.skipped[0]["reason"] == "python_model"
    assert "models/scores.py" in selection.skipped[0]["detail"]


def test_a_versioned_model_is_skipped_however_it_is_named():
    files = {
        **_shop(),
        "models/marts/dim_customers_v2.sql": "select 1 as customer_id\n",
        "models/marts/_marts.yml": (
            "version: 2\nmodels:\n  - name: dim_customers\n    latest_version: 2\n"
            "    versions:\n      - v: 2\n"
        ),
    }
    everything = declare_mod.select_models(_view(files), [])
    assert "dim_customers_v2" not in everything.models
    assert [s["reason"] for s in everything.skipped] == ["versioned"]
    named = declare_mod.select_models(_view(files), ["dim_customers.v2"])
    assert named.models == []
    assert named.skipped[0]["reason"] == "versioned"


# --- what is proposed -------------------------------------------------------------


def test_drafted_facts_become_one_declaration_with_their_evidence():
    outcome = _propose(
        _shop(),
        {
            "stg_customers": _draft(
                grain=["customer_id"],
                columns={
                    "customer_id": {
                        "from": "customers.customer_id",
                        "role": _fact("key"),
                    }
                },
            )
        },
    )
    (proposal,) = outcome.proposals
    assert proposal.path == "models/staging/stg_customers.yml"
    assert proposal.fields == {
        "grain": _fact(["customer_id"], "a proven key"),
        "columns": {"customer_id": {"role": _fact("key")}},
    }
    assert proposal.declaration.grain == ["customer_id"]
    assert proposal.supplied == []


def test_a_field_the_yaml_already_states_is_never_proposed():
    files = {
        **_shop(),
        "models/staging/_staging.yml": (
            "version: 2\nmodels:\n  - name: stg_customers\n    config:\n"
            "      meta:\n        dex:\n          grain: [region]\n"
            "    columns:\n      - name: customer_id\n        config:\n"
            "          meta:\n            dex:\n              role: dimension\n"
        ),
    }
    outcome = _propose(
        files,
        {
            "stg_customers": _draft(
                grain=["customer_id"],
                columns={
                    "customer_id": {
                        "role": _fact("foreign_key"),
                        "references": _fact("stg_orders.order_id"),
                    },
                    "region": {"additivity": _fact("semi_additive")},
                },
                declared={
                    "grain": ["region"],
                    "columns": {"customer_id": {"role": "dimension"}},
                },
            )
        },
    )
    (proposal,) = outcome.proposals
    # The human's grain and role stand; a reference would contradict the role
    # they chose, so it is not proposed either. Only the absent fact is.
    assert proposal.fields == {
        "columns": {"region": {"additivity": _fact("semi_additive")}}
    }


def test_a_reference_is_proposed_only_to_something_the_project_can_ref():
    drafts = {
        "fct_orders": _draft(
            columns={
                "customer_id": {
                    "role": _fact("foreign_key"),
                    "references": _fact("stg_customers.customer_id"),
                },
                "order_id": {
                    "role": _fact("foreign_key"),
                    "references": _fact("raw_orders.order_id"),
                },
            }
        )
    }
    (proposal,) = _propose(_shop(), drafts).proposals
    columns = proposal.fields["columns"]
    assert columns["customer_id"]["references"]["value"] == "stg_customers.customer_id"
    assert "references" not in columns["order_id"]
    assert columns["order_id"]["role"]["value"] == "foreign_key"


def test_unit_and_nullable_stay_evidence_and_are_never_written():
    drafts = {
        "fct_orders": _draft(
            columns={
                "amount": {
                    "unit": _fact("fraction"),
                    "nullable": _fact(False),
                    "pii": _fact({"category": "email", "confidence": 0.9}),
                }
            }
        )
    }
    outcome = _propose(_shop(), drafts)
    assert outcome.proposals == []
    assert outcome.skipped[0]["reason"] == "nothing_drafted"


def test_supplied_intent_wins_field_by_field_over_the_draft():
    outcome = _propose(
        _shop(),
        {
            "fct_orders": _draft(
                grain=["order_id"],
                columns={"customer_id": {"role": _fact("foreign_key")}},
            )
        },
        supplied=[
            {
                "model": "fct_orders",
                "description": "One row per order.",
                "columns": {"customer_id": {"role": "dimension"}},
            }
        ],
    )
    (proposal,) = outcome.proposals
    assert proposal.declaration.description == "One row per order."
    assert proposal.declaration.grain == ["order_id"]
    assert proposal.declaration.columns["customer_id"].role.value == "dimension"
    assert proposal.supplied == ["description", "columns.customer_id.role"]


def test_a_supplied_model_is_considered_first_and_is_never_elided():
    drafts = {
        name: _draft(grain=["order_id"])
        for name in ("fct_orders", "stg_customers", "stg_orders")
    }
    outcome = _propose(
        {
            **_shop(),
            "models/staging/stg_customers.sql": "select 1 as order_id\n",
            "models/marts/fct_orders.sql": "select 1 as order_id\n",
        },
        drafts,
        models=["fct_orders", "stg_customers", "stg_orders"],
        supplied=[{"model": "stg_orders", "description": "One row per order."}],
        cap=1,
    )
    assert [p.model for p in outcome.proposals] == ["stg_orders"]
    assert outcome.elided == ["fct_orders", "stg_customers"]


def test_skipped_models_do_not_count_against_the_cap():
    drafts = {
        "fct_orders": _draft(),
        "stg_customers": _draft(grain=["customer_id"]),
        "stg_orders": _draft(grain=["order_id"]),
    }
    outcome = _propose(
        _shop(), drafts, models=["fct_orders", "stg_customers", "stg_orders"], cap=1
    )
    assert [p.model for p in outcome.proposals] == ["stg_customers"]
    assert [s["model"] for s in outcome.skipped] == ["fct_orders"]
    assert outcome.elided == ["stg_orders"]


@pytest.mark.parametrize(
    ("draft", "reason"),
    [
        (
            _draft(
                inputs=[
                    {
                        "relation": "stg_orders",
                        "profiled": False,
                        "command": "explore profile stg_orders",
                    }
                ]
            ),
            "unprofiled_inputs",
        ),
        (_draft(incomplete=["the SQL did not parse"]), "sql_unreadable"),
        (_draft(), "nothing_drafted"),
        (
            _draft(grain=["order_id"], declared={"grain": ["order_id"]}),
            "complete",
        ),
    ],
)
def test_every_model_with_nothing_to_propose_says_why(draft, reason):
    outcome = _propose(_shop(), {"fct_orders": draft}, models=["fct_orders"])
    assert outcome.proposals == []
    (skipped,) = outcome.skipped
    assert skipped["reason"] == reason
    assert skipped["missing"] == ["description"]
    if reason == "unprofiled_inputs":
        assert skipped["commands"] == ["explore profile stg_orders"]
        assert skipped["detail"] == (
            "nothing was drafted from stg_orders: the cache has not profiled it. "
            "Run `explore profile stg_orders` and declare again"
        )


def test_an_unprofiled_input_keeps_the_drafter_s_reason():
    # Found dogfooding #501: with no cache at all, the reason to give is "run
    # `explore map`", not that one relation was missed.
    draft = _draft(
        inputs=[
            {
                "relation": "orders",
                "profiled": False,
                "reason": "there is no exploration cache yet; run `explore map` first",
                "command": "explore profile orders",
            }
        ]
    )
    outcome = _propose(_shop(), {"stg_orders": draft}, models=["stg_orders"])
    assert "no exploration cache yet" in outcome.skipped[0]["detail"]


def test_an_input_no_command_can_profile_is_stated_without_a_command():
    """#893: a name matching several profiled relations has no command that
    helps, so the skip states why instead of naming one that fails."""

    reason = (
        "it matches 2 relations in the cache (shop.dbt_dev.stg_orders, "
        "shop.public.stg_orders), and a ref() is read by name, so no profile run "
        "can settle which one it means"
    )
    draft = _draft(
        inputs=[{"relation": "stg_orders", "profiled": False, "reason": reason}]
    )
    outcome = _propose(_shop(), {"fct_orders": draft}, models=["fct_orders"])
    (skipped,) = outcome.skipped
    assert skipped["reason"] == "unprofiled_inputs"
    assert skipped["commands"] == []
    assert skipped["detail"] == f"nothing was drafted from stg_orders: {reason}"


def test_a_dev_only_input_is_skipped_with_the_dev_target_command():
    command = "explore profile SHOP.DBT_DEV.stg_orders --target dev"
    draft = _draft(
        inputs=[
            {
                "relation": "stg_orders",
                "profiled": False,
                "reason": (
                    "the cache has not profiled it, and its dev build in "
                    "SHOP.DBT_DEV is outside the source allowlist"
                ),
                "command": command,
            }
        ]
    )
    outcome = _propose(_shop(), {"fct_orders": draft}, models=["fct_orders"])
    (skipped,) = outcome.skipped
    assert skipped["commands"] == [command]
    assert skipped["detail"].endswith(f"Run `{command}` and declare again")


def test_a_model_dex_cannot_read_is_skipped_not_guessed():
    outcome = _propose(_shop(), {}, models=["fct_orders"])
    assert outcome.skipped[0]["reason"] == "sql_unreadable"


def test_an_entry_dex_cannot_splice_skips_that_model_and_plans_the_rest():
    files = {
        **_shop(),
        "models/marts/_marts.yml": "version: 2\nmodels: [{name: fct_orders}]\n",
    }
    outcome = _propose(
        files,
        {
            "fct_orders": _draft(grain=["order_id"]),
            "stg_customers": _draft(grain=["customer_id"]),
        },
    )
    assert [p.model for p in outcome.proposals] == ["stg_customers"]
    assert outcome.skipped[0]["model"] == "fct_orders"
    assert outcome.skipped[0]["reason"] == "not_writable"
    assert "flow style" in outcome.skipped[0]["detail"]


def test_a_drafted_fact_the_declaration_check_refuses_skips_the_model():
    # The SELECT list does not produce the drafted grain column, so the draft
    # does not fit this model; refusing the whole batch for it would be worse.
    outcome = _propose(_shop(), {"stg_customers": _draft(grain=["customer_key"])})
    assert outcome.proposals == []
    assert outcome.skipped[0]["reason"] == "not_writable"


# --- end to end -------------------------------------------------------------------


STAGING_YML = """version: 2

models:
  # hand-written, keep me
  - name: stg_customers
    description: One row per customer.
    columns:
      - name: customer_id
        tests:
          - unique
  - name: stg_orders
"""


@pytest.fixture
def shop(tmp_path: Path, capsys) -> Path:
    """A dbt project over a DuckDB warehouse, explored and join-verified, with
    three models and no declarations."""

    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(tmp_path / "shop.duckdb"))
    conn.execute(
        "create table raw_orders as select i as order_id, (i % 20) + 1 as "
        "customer_id, case when i % 9 = 0 then 'cancelled' else 'shipped' end "
        "as status, i * 3.5 as amount from range(1, 401) t(i)"
    )
    conn.execute(
        "create table customers as select i as customer_id, 'r' || (i % 3) as "
        "region from range(1, 21) t(i)"
    )
    conn.close()

    project = tmp_path / "analytics"
    staging = project / "models" / "staging"
    staging.mkdir(parents=True)
    (project / "models" / "marts").mkdir()
    (project / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0.0"\nprofile: shop\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (staging / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    schema: main\n    tables:\n"
        "      - name: raw_orders\n      - name: customers\n",
        encoding="utf-8",
    )
    (staging / "stg_orders.sql").write_text(
        "select order_id, customer_id, status, amount "
        "from {{ source('raw', 'raw_orders') }}\n",
        encoding="utf-8",
    )
    (staging / "stg_customers.sql").write_text(
        "select customer_id, region from {{ source('raw', 'customers') }}\n",
        encoding="utf-8",
    )
    (staging / "_staging.yml").write_text(STAGING_YML, encoding="utf-8")
    (project / "models" / "marts" / "fct_orders.sql").write_text(
        "select o.order_id, o.customer_id, o.amount\n"
        "from {{ ref('stg_orders') }} o\nwhere o.status <> 'cancelled'\n",
        encoding="utf-8",
    )
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'shop.duckdb'}\n",
        encoding="utf-8",
    )
    for argv in (
        ["explore", "map", "--full"],
        ["explore", "relationships", "--verify"],
    ):
        assert main([*argv, "--repo-root", str(tmp_path)]) == 0, argv
        capsys.readouterr()
    return tmp_path


def _dex(repo: Path, argv: list[str], capsys) -> dict:
    main(["--repo-root", str(repo), *argv])
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    return json.loads(out)


def test_declare_proposes_the_measured_facts_and_names_what_it_skipped(
    shop: Path, capsys, monkeypatch
):
    import exmergo_dex_core.connect as connect
    from exmergo_dex_core.engine import DexEngine

    def refuse(*_args, **_kwargs):
        raise AssertionError("declare opened a warehouse connection")

    monkeypatch.setattr(connect, "open_adapter", refuse)
    monkeypatch.setattr(DexEngine, "_adapter", refuse)

    envelope = _dex(shop, ["transform", "declare"], capsys)
    assert envelope["status"] == "ok", envelope
    data = envelope["data"]
    assert data["selected_count"] == 3
    assert [p["model"] for p in data["proposed"]] == ["stg_customers", "stg_orders"]
    (skipped,) = data["skipped"]
    assert skipped["model"] == "fct_orders"
    assert skipped["reason"] == "unprofiled_inputs"
    assert skipped["commands"] == ["explore profile stg_orders"]
    assert data["paths"] == ["models/staging/_staging.yml"]
    assert data["elided_count"] == 0
    assert data["notes"] == []
    assert set(data["declarations"]) == {"stg_customers", "stg_orders"}
    assert envelope["data"]["next"].startswith("review the diffs")

    orders = next(p for p in data["proposed"] if p["model"] == "stg_orders")
    assert orders["fields"]["grain"]["value"] == ["order_id"]
    # The join is verified, but to a raw table no model stands for, so the role is
    # proposed and `references` is not.
    customer = orders["fields"]["columns"]["customer_id"]
    assert customer["role"]["value"] == "foreign_key"
    assert "references" not in customer

    (diff,) = envelope["diffs"]
    # The human's comment, description and test stay; only absent fields arrive.
    assert diff["deletions"] == 0
    assert "+          - not_null" in diff["unified"]
    assert "description" not in "".join(
        line for line in diff["unified"].splitlines() if line.startswith("+")
    )


def test_apply_then_declare_again_is_a_clean_no_op(shop: Path, capsys):
    planned = _dex(shop, ["transform", "declare", "stg_orders,stg_customers"], capsys)
    assert planned["status"] == "ok", planned
    applied = _dex(shop, ["transform", "apply", planned["data"]["plan_id"]], capsys)
    assert applied["status"] == "ok", applied

    written = (shop / "analytics/models/staging/_staging.yml").read_text()
    assert written.startswith(STAGING_YML.split("  - name: stg_customers")[0])
    assert "# hand-written, keep me" in written
    entry = next(
        m for m in yaml.safe_load(written)["models"] if m["name"] == "stg_customers"
    )
    assert entry["description"] == "One row per customer."
    assert entry["columns"][0]["tests"] == ["unique", "not_null"]

    again = _dex(shop, ["transform", "declare", "stg_orders,stg_customers"], capsys)
    assert again["status"] == "ok", again
    assert again["data"]["plan_id"] is None
    assert again["diffs"] == []
    assert {s["reason"] for s in again["data"]["skipped"]} == {"complete"}
    assert "nothing to propose" in again["data"]["notes"][0]


def test_supplied_intent_rides_in_the_same_plan(shop: Path, capsys):
    payload = shop / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "declarations": [
                    {
                        "model": "fct_orders",
                        "description": "One row per order that was not cancelled.",
                        "grain": ["order_id"],
                        "assumptions": [
                            {
                                "decision": "Whether cancelled orders count",
                                "chosen": "they do not",
                                "evidence": "request",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    envelope = _dex(
        shop,
        ["transform", "declare", "--edits-file", str(payload)],
        capsys,
    )
    assert envelope["status"] == "ok", envelope
    data = envelope["data"]
    assert data["proposed"][0]["model"] == "fct_orders"
    assert data["decisions"] == [
        {
            "model": "fct_orders",
            "decision": "Whether cancelled orders count",
            "chosen": "they do not",
            "evidence": "request",
        }
    ]
    assert "models/marts/fct_orders.yml" in data["paths"]


def test_the_cap_elides_with_a_note_and_the_next_run_moves_on(shop: Path, capsys):
    first = _dex(shop, ["transform", "declare", "--limit", "1"], capsys)
    assert [p["model"] for p in first["data"]["proposed"]] == ["stg_customers"]
    assert first["data"]["elided_count"] == 1
    assert "capped at 1" in first["data"]["notes"][0]
    _dex(shop, ["transform", "apply", first["data"]["plan_id"]], capsys)

    second = _dex(shop, ["transform", "declare", "--limit", "1"], capsys)
    assert [p["model"] for p in second["data"]["proposed"]] == ["stg_orders"]
    assert second["data"]["elided_count"] == 0


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--limit", "26"], "can only narrow it"),
        (["--limit", "0"], "outside 1 to 25"),
    ],
)
def test_the_cap_only_narrows(shop: Path, capsys, argv, message):
    envelope = _dex(shop, ["transform", "declare", *argv], capsys)
    assert envelope["status"] == "error"
    assert message in envelope["errors"][0]


def test_edits_and_unselected_declarations_are_refused(shop: Path, capsys):
    payload = shop / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/x.sql",
                        "kind": "model_sql",
                        "content": "select 1 as a",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    edits = _dex(shop, ["transform", "declare", "--edits-file", str(payload)], capsys)
    assert "takes no `edits`" in edits["errors"][0]

    payload.write_text(
        json.dumps({"declarations": [{"model": "fct_orders", "description": "x"}]}),
        encoding="utf-8",
    )
    outside = _dex(
        shop,
        ["transform", "declare", "stg_orders", "--edits-file", str(payload)],
        capsys,
    )
    assert outside["status"] == "error"
    assert "does not select" in outside["errors"][0]


def test_a_supplied_declaration_the_check_refuses_refuses_the_command(
    shop: Path, capsys
):
    # Unlike a drafted fact, a malformed declaration the caller wrote is theirs
    # to fix, so it refuses exactly as it would in `transform plan`.
    payload = shop / "payload.json"
    payload.write_text(
        json.dumps({"declarations": [{"model": "stg_orders", "grain": ["order_key"]}]}),
        encoding="utf-8",
    )
    envelope = _dex(
        shop, ["transform", "declare", "--edits-file", str(payload)], capsys
    )
    assert envelope["status"] == "error"
    assert "does not produce" in envelope["errors"][0]
    assert not (shop / ".dex" / "plans").exists() or not any(
        (shop / ".dex" / "plans").iterdir()
    )


def test_a_build_in_its_own_schema_gets_references_to_the_built_parent(
    shop: Path, capsys
):
    """#892: on a warehouse the sources and the build live in different schemas,
    and the replica fold kept only a built foreign key's join to the raw table,
    so no `references` was ever drafted off DuckDB. Here the build sits in its
    own schema beside the sources, as it does on every warehouse."""

    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(shop / "shop.duckdb"))
    conn.execute("create schema dbt_dev")
    conn.execute(
        "create table dbt_dev.stg_customers as select customer_id, region "
        "from main.customers"
    )
    conn.execute(
        "create table dbt_dev.stg_orders as select order_id, customer_id, status, "
        "amount from main.raw_orders"
    )
    conn.execute(
        "create table dbt_dev.fct_orders as select order_id, customer_id, amount "
        "from dbt_dev.stg_orders where status <> 'cancelled'"
    )
    conn.close()
    for argv in (
        ["explore", "map", "--full"],
        ["explore", "relationships", "--verify"],
    ):
        assert main([*argv, "--repo-root", str(shop)]) == 0, argv
        capsys.readouterr()

    envelope = _dex(shop, ["transform", "declare", "fct_orders"], capsys)
    assert envelope["status"] == "ok", envelope
    (proposed,) = envelope["data"]["proposed"]
    customer = proposed["fields"]["columns"]["customer_id"]
    assert customer["role"]["value"] == "foreign_key"
    assert customer["references"]["value"] == "stg_customers.customer_id"
    assert "dbt_dev.stg_customers" in customer["references"]["evidence"]
