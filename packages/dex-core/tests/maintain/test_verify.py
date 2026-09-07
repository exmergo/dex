"""maintain verify: a baseline-free sweep (#224), starting with build-status
gaps read from the compiled manifest and the last run's run_results.json,
plus a project that fails to compile (#225)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.maintain import verify as verify_mod
from exmergo_dex_core.maintain.verify import (
    build_status_findings,
    compile_check,
    missing_relation_findings,
)


def _write_artifacts(project_dir: Path, nodes: dict, results: list[dict]) -> None:
    target = project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )
    (target / "run_results.json").write_text(
        json.dumps({"results": results}), encoding="utf-8"
    )


# --- build_status_findings: pure, reads only artifacts already on disk ----------


def test_no_run_results_is_reported_as_a_note_not_a_finding(tmp_path: Path):
    findings, notes = build_status_findings(tmp_path)
    assert findings == []
    assert notes and "no dbt run results found" in notes[0]


def test_a_failed_node_is_reported(tmp_path: Path):
    _write_artifacts(
        tmp_path,
        nodes={"model.p.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.p.a", "status": "error", "message": "boom"}],
    )
    findings, notes = build_status_findings(tmp_path)
    assert notes == []
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "node_failed"
    assert finding.identifier == "a"
    assert finding.severity == "high"
    assert "boom" in finding.detail


def test_one_failed_model_and_two_skipped_children_reports_all_three(tmp_path: Path):
    """#225's own acceptance bullet, verbatim: one failed model and two
    skipped children report all three findings and the causal link -- each
    child on its own, not collapsed into one."""

    _write_artifacts(
        tmp_path,
        nodes={
            "model.p.parent": {
                "name": "parent",
                "resource_type": "model",
                "depends_on": {"nodes": []},
            },
            "model.p.child_a": {
                "name": "child_a",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.p.parent"]},
            },
            "model.p.child_b": {
                "name": "child_b",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.p.parent"]},
            },
        },
        results=[
            {"unique_id": "model.p.parent", "status": "error", "message": "boom"},
            {"unique_id": "model.p.child_a", "status": "skipped"},
            {"unique_id": "model.p.child_b", "status": "skipped"},
        ],
    )
    findings, _notes = build_status_findings(tmp_path)
    assert len(findings) == 3
    by_identifier = {f.identifier: f for f in findings}
    assert set(by_identifier) == {"parent", "child_a", "child_b"}
    assert by_identifier["parent"].code == "node_failed"
    for name in ("child_a", "child_b"):
        skipped = by_identifier[name]
        assert skipped.code == "node_skipped"
        assert skipped.severity == "medium"
        assert skipped.data["caused_by"] == "parent"
        assert "'parent' failed to build" in skipped.detail


def test_a_transitively_skipped_node_traces_back_to_the_real_failure(tmp_path: Path):
    """A grandchild skipped because its parent was itself only skipped (the
    parent's own parent is the one that actually failed) still names the
    real cause, not the also-skipped intermediate node."""

    _write_artifacts(
        tmp_path,
        nodes={
            "model.p.grandparent": {"name": "grandparent", "resource_type": "model"},
            "model.p.parent": {
                "name": "parent",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.p.grandparent"]},
            },
            "model.p.child": {
                "name": "child",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.p.parent"]},
            },
        },
        results=[
            {"unique_id": "model.p.grandparent", "status": "error", "message": "boom"},
            {"unique_id": "model.p.parent", "status": "skipped"},
            {"unique_id": "model.p.child", "status": "skipped"},
        ],
    )
    findings, _notes = build_status_findings(tmp_path)
    by_identifier = {f.identifier: f for f in findings}
    assert by_identifier["child"].data["caused_by"] == "grandparent"
    assert by_identifier["parent"].data["caused_by"] == "grandparent"


def test_a_skipped_node_with_no_identifiable_cause_is_still_reported(tmp_path: Path):
    """A selector exclusion or an upstream error dbt did not attribute still
    reports the skip, just at a lower severity than a definite causal chain."""

    _write_artifacts(
        tmp_path,
        nodes={"model.p.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.p.a", "status": "skipped"}],
    )
    findings, _notes = build_status_findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].severity == "low"
    assert findings[0].data == {}


def test_a_successful_node_reports_nothing(tmp_path: Path):
    _write_artifacts(
        tmp_path,
        nodes={"model.p.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.p.a", "status": "success"}],
    )
    findings, notes = build_status_findings(tmp_path)
    assert findings == [] and notes == []


# --- missing_relation_findings: pure ---------------------------------------------


def test_a_model_with_no_matching_relation_is_reported():
    findings = missing_relation_findings(
        model_relations={"stg_orders": "db.main.stg_orders"},
        live_identifiers=["db.main.customers"],
        already_reported=set(),
    )
    assert len(findings) == 1
    assert findings[0].code == "no_relation"
    assert findings[0].identifier == "stg_orders"
    assert findings[0].data["relation_name"] == "db.main.stg_orders"


def test_a_model_with_a_matching_relation_is_quiet():
    findings = missing_relation_findings(
        model_relations={"stg_orders": "db.main.stg_orders"},
        live_identifiers=["db.main.stg_orders"],
        already_reported=set(),
    )
    assert findings == []


def test_a_model_already_explained_by_a_build_status_finding_is_not_repeated():
    """A node that failed or was skipped never produced a relation either;
    reporting that twice under a different code would say the same thing
    about the same node in two places."""

    findings = missing_relation_findings(
        model_relations={"stg_orders": "db.main.stg_orders"},
        live_identifiers=[],
        already_reported={"stg_orders"},
    )
    assert findings == []


# --- compile_check: wraps shadow_parse -------------------------------------------


def test_compile_check_is_quiet_when_dbt_is_unavailable(tmp_path: Path, monkeypatch):
    import exmergo_dex_core.maintain.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": False,
            "reason": "dbt is not installed",
            "success": None,
            "messages": [],
        },
    )
    finding, notes = compile_check(tmp_path)
    assert finding is None
    assert notes == ["compile check skipped (dbt is not installed)"]


def test_compile_check_is_quiet_on_a_passing_parse(tmp_path: Path, monkeypatch):
    import exmergo_dex_core.maintain.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": True,
            "messages": [],
        },
    )
    finding, notes = compile_check(tmp_path)
    assert finding is None and notes == []


def test_compile_check_reports_the_first_parse_message(tmp_path: Path, monkeypatch):
    import exmergo_dex_core.maintain.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": False,
            "messages": ["Compilation Error in model stg_orders: syntax error"],
        },
    )
    finding, notes = compile_check(tmp_path)
    assert notes == []
    assert finding is not None
    assert finding.code == "project_does_not_compile"
    assert finding.severity == "high"
    assert "syntax error" in finding.detail


# --- end to end through the CLI (the degraded, dbt-artifact-free default) -------
#
# `maintain_repo` is shared with the drift suites, which never invoke a real
# `dbt parse` against it (they work entirely from the `.dex/` cache), so its
# own compile-cleanliness is untested territory. The compile check is faked
# to a clean pass here so these tests exercise this module's own wiring
# (manifest/run-results reading through `engine.project_dir()`, scoping)
# rather than depending on the fixture project being dbt-parseable, which
# `compile_check` already has its own direct unit tests for above.


@pytest.fixture
def _assume_the_project_compiles(monkeypatch: pytest.MonkeyPatch):
    import exmergo_dex_core.maintain.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": True,
            "messages": [],
        },
    )


def test_verify_on_a_project_with_no_build_yet_is_clean_and_says_so(
    maintain_repo, _assume_the_project_compiles
):
    """`maintain_repo` never runs real dbt, so this is the ordinary state a
    fresh checkout is in: no baseline needed (unlike every other maintain
    subcommand), and the absence of run results is named rather than read as
    a clean bill of health."""

    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert payload["data"]["finding_count"] == 0
    assert "build_status" in payload["data"]["suppressed"]
    assert any("no dbt run results found" in w for w in payload["warnings"])


def test_verify_reports_a_failed_node_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """The manifest/run_results wiring through engine.project_dir(), not just
    the pure function tested above in isolation."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
            }
        },
        results=[
            {
                "unique_id": "model.maintain_test.stg_orders",
                "status": "error",
                "message": "boom",
            }
        ],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = payload["data"]["findings"]
    assert len(findings) == 1
    assert findings[0]["code"] == "node_failed"
    assert findings[0]["identifier"] == "stg_orders"
    assert "build_status" not in payload["data"]["suppressed"]


def test_verify_reports_a_model_with_no_relation_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """The other manifest-reading path, through `ProjectDefinitions.
    model_relations` rather than `build_status_findings`'s own manifest
    read: a model the project declares that never built anything."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_missing": {
                "name": "stg_missing",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."stg_missing"',
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = payload["data"]["findings"]
    assert len(findings) == 1
    assert findings[0]["code"] == "no_relation"
    assert findings[0]["identifier"] == "stg_missing"
    assert "no_relation" not in payload["data"]["suppressed"]


def test_verify_reports_a_compile_failure_first_and_suppresses_the_rest(
    maintain_repo, monkeypatch: pytest.MonkeyPatch
):
    """#225's third acceptance bullet: a project that does not compile
    reports that first, and nothing else runs against its (untrustworthy)
    manifest."""

    import exmergo_dex_core.maintain.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": False,
            "messages": ["Compilation Error in model stg_broken: syntax error"],
        },
    )
    # A failed node in run_results, which should never be reached: the
    # compile failure must suppress build-status checking entirely.
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.maintain_test.a", "status": "error"}],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = payload["data"]["findings"]
    assert len(findings) == 1
    assert findings[0]["code"] == "project_does_not_compile"
    assert "syntax error" in findings[0]["detail"]
    assert set(payload["data"]["suppressed"]) == {
        "build_status",
        "no_relation",
        "row_population",
    }


def test_verify_scopes_to_the_named_object(maintain_repo, _assume_the_project_compiles):
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.a": {"name": "a", "resource_type": "model"},
            "model.maintain_test.b": {"name": "b", "resource_type": "model"},
        },
        results=[
            {"unique_id": "model.maintain_test.a", "status": "error"},
            {"unique_id": "model.maintain_test.b", "status": "error"},
        ],
    )
    rc, payload = maintain_repo.dex("maintain", "verify", "a")
    assert rc == 0 and payload["status"] == "ok", payload
    assert [f["identifier"] for f in payload["data"]["findings"]] == ["a"]


# --- row population: a model against the relation it is built from (#226) ------
#
# Pure, like the build-status half above: a manifest on disk in, findings out,
# with the counts passed in rather than measured, so every threshold and every
# suppression is asserted without a warehouse in the way.

WAREHOUSE = "wh.main"


def _model(name: str, sql: str, **extra) -> dict:
    return {
        "name": name,
        "resource_type": "model",
        "relation_name": f'"wh"."main"."{name}"',
        "compiled_code": sql,
        **extra,
    }


def _plan(tmp_path: Path, nodes: dict, sources: dict | None = None):
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes, "sources": sources or {}}), encoding="utf-8"
    )
    return verify_mod.row_population_plan(tmp_path, "duckdb")


def _judge(checks, counts: dict[str, int], counted: set[str] | None = None):
    keyed = {f"{WAREHOUSE}.{name}": rows for name, rows in counts.items()}
    measured = set(keyed) if counted is None else {f"{WAREHOUSE}.{n}" for n in counted}
    return verify_mod.row_population_findings(checks, keyed, measured)


def test_an_inner_join_that_loses_a_fifth_of_the_rows_is_reported(tmp_path: Path):
    """#226's first acceptance bullet, in the shape a dbt model actually
    compiles to: the driving parent is behind two CTEs, and the join that
    dropped the rows is named."""

    checks, notes = _plan(
        tmp_path,
        {
            "m.1": _model("stg_orders", 'select * from "wh"."main"."orders"'),
            "m.2": _model("stg_customers", 'select * from "wh"."main"."customers"'),
            "m.3": _model(
                "fct_orders",
                'with source as (select * from "wh"."main"."stg_orders"), '
                "joined as (select s.order_id, c.name from source s inner join "
                '"wh"."main"."stg_customers" c on s.customer_id = c.customer_id) '
                "select * from joined",
            ),
        },
    )
    assert notes == []
    findings, _ = _judge(
        checks,
        {
            "orders": 200,
            "customers": 40,
            "stg_orders": 200,
            "stg_customers": 40,
            "fct_orders": 160,
        },
    )
    assert [f.code for f in findings] == ["row_loss"]
    finding = findings[0]
    assert finding.identifier == "fct_orders"
    assert finding.severity == "medium"
    assert finding.data["driving_parent"] == "stg_orders"
    assert "inner join to 'stg_customers'" in finding.detail


def test_the_finding_states_both_counts_so_a_reader_can_judge_it(tmp_path: Path):
    """#226's fourth acceptance bullet. The threshold is dex's opinion; the two
    counts are the evidence, and they have to be in the sentence."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model("stg_orders", 'select * from "wh"."main"."orders"'),
            "m.2": _model(
                "fct_orders",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."stg_customers" c on o.customer_id = c.id',
            ),
        },
    )
    findings, _ = _judge(
        checks,
        {"orders": 200, "stg_orders": 200, "fct_orders": 160, "stg_customers": 40},
    )
    assert "160 rows" in findings[0].detail and "200" in findings[0].detail
    assert findings[0].data["row_count"] == 160
    assert findings[0].data["parent_row_count"] == 200
    assert findings[0].data["change_fraction"] == -0.2


def test_a_group_by_model_with_fewer_rows_is_not_reported(tmp_path: Path):
    """#226's second acceptance bullet, and the one that decides whether the
    detector survives contact with a real project: an aggregate is *supposed*
    to hold fewer rows than its parent."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "daily",
                'select d, count(*) from "wh"."main"."stg_orders" group by d',
            )
        },
    )
    assert checks[0].reducers == ("a GROUP BY",)
    findings, _ = _judge(checks, {"stg_orders": 200, "daily": 12})
    assert findings == []


@pytest.mark.parametrize(
    ("sql", "reducer"),
    [
        ('select * from "wh"."main"."stg_orders" where status = 1', "a WHERE filter"),
        ('select distinct id from "wh"."main"."stg_orders"', "a DISTINCT"),
        ('select * from "wh"."main"."stg_orders" limit 10', "a LIMIT"),
        (
            'select * from "wh"."main"."stg_orders" qualify '
            "row_number() over (partition by id order by d) = 1",
            "a QUALIFY filter",
        ),
    ],
)
def test_every_clause_that_explains_a_shortfall_silences_the_finding(
    tmp_path: Path, sql: str, reducer: str
):
    checks, _ = _plan(tmp_path, {"m.1": _model("narrowed", sql)})
    assert reducer in checks[0].reducers
    findings, _ = _judge(checks, {"stg_orders": 200, "narrowed": 20})
    assert findings == []


def test_a_model_that_fans_out_is_reported_with_its_join_key(tmp_path: Path):
    """#226's third acceptance bullet: the key is what the reader acts on, so
    it is named rather than left to be found by opening the file."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "order_items",
                'select o.order_id, i.sku from "wh"."main"."stg_orders" o '
                'left join "wh"."main"."stg_items" i on o.order_id = i.order_id',
            )
        },
    )
    findings, _ = _judge(
        checks, {"stg_orders": 200, "stg_items": 500, "order_items": 500}
    )
    assert [f.code for f in findings] == ["row_fanout"]
    assert findings[0].severity == "high"
    assert "o.order_id = i.order_id" in findings[0].detail
    assert findings[0].data["join_keys"] == ["o.order_id = i.order_id"]


def test_a_union_is_not_mistaken_for_fanout(tmp_path: Path):
    """A set operation has more rows than either side by construction, and no
    single driving parent to compare against, so it is declared rather than
    reported."""

    checks, notes = _plan(
        tmp_path,
        {
            "m.1": _model(
                "everything",
                'select id from "wh"."main"."stg_orders" union all '
                'select id from "wh"."main"."stg_items"',
            )
        },
    )
    assert checks == []
    assert "set operation" in notes[0]


def test_an_unnest_is_not_mistaken_for_fanout(tmp_path: Path):
    """A construct that turns one row into several is doing its job."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "exploded",
                'select o.id, t.tag from "wh"."main"."stg_orders" o '
                "cross join unnest(o.tags) as t(tag)",
            )
        },
    )
    assert checks[0].multiplies is True
    findings, _ = _judge(checks, {"stg_orders": 200, "exploded": 900})
    assert findings == []


def test_row_growth_with_no_join_at_all_is_not_reported(tmp_path: Path):
    """Fanout is a claim about a join. Without one, dex has no mechanism to
    name and says nothing rather than guessing."""

    checks, _ = _plan(
        tmp_path, {"m.1": _model("copy", 'select * from "wh"."main"."stg_orders"')}
    )
    findings, _ = _judge(checks, {"stg_orders": 200, "copy": 400})
    assert findings == []


def test_a_difference_inside_the_reporting_band_is_chatter(tmp_path: Path):
    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    findings, _ = _judge(checks, {"stg_orders": 200, "d": 40, "fct": 195})
    assert findings == []


def test_losing_most_of_the_rows_ranks_high(tmp_path: Path):
    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    findings, _ = _judge(checks, {"stg_orders": 200, "d": 40, "fct": 40})
    assert findings[0].severity == "high"


def test_an_incremental_model_is_skipped_and_the_note_says_why(tmp_path: Path):
    """The largest false-positive source there is: an incremental model holds
    what previous runs loaded, so it has no reason to match its parent."""

    checks, notes = _plan(
        tmp_path,
        {
            "m.1": _model(
                "events",
                'select * from "wh"."main"."raw_events"',
                config={"materialized": "incremental"},
            )
        },
    )
    assert checks == []
    assert "incremental" in notes[0] and "events" in notes[0]


def test_an_ephemeral_model_contributes_nothing(tmp_path: Path):
    """It compiles with no relation of its own, so there is no count to
    compare; dbt inlines it into whatever reads it."""

    checks, notes = _plan(
        tmp_path,
        {
            "m.1": {
                "name": "helper",
                "resource_type": "model",
                "relation_name": None,
                "compiled_code": 'select * from "wh"."main"."orders"',
            }
        },
    )
    assert checks == [] and notes == []


def test_unparseable_compiled_sql_becomes_a_note_not_an_exception(tmp_path: Path):
    checks, notes = _plan(tmp_path, {"m.1": _model("broken", "select from from")})
    assert checks == []
    assert "broken" in notes[0]


def test_a_parent_with_no_row_count_is_named_rather_than_dropped(tmp_path: Path):
    """#172's inertness requirement applied here: an absent finding must not be
    indistinguishable from a clean one."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    findings, notes = _judge(checks, {"fct": 40})
    assert findings == []
    assert "fct" in notes[0] and "stg_orders" in notes[0]


def test_a_verdict_on_a_catalog_estimate_is_not_reported_as_exact(tmp_path: Path):
    """A catalog row count is an estimate, and the honesty flag is how a reader
    tells a proof from a strong signal, exactly as the volume axis does."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    counts = {"stg_orders": 200, "d": 40, "fct": 100}
    estimated, _ = _judge(checks, counts, counted=set())
    measured, _ = _judge(checks, counts, counted=set(counts))
    assert estimated[0].exact is False
    assert measured[0].exact is True


def test_a_driving_parent_outside_the_project_still_counts(tmp_path: Path):
    """A model reading a relation the project never declares (a table loaded by
    something else) has a driving parent like any other; only the name dex can
    call it changes."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."external_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    assert checks[0].parent == "wh.main.external_orders"
    findings, _ = _judge(checks, {"external_orders": 200, "d": 40, "fct": 100})
    assert findings[0].code == "row_loss"


def test_verify_reports_row_loss_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """The whole path against a real warehouse: manifest on disk, counts read
    from DuckDB, finding out. `fct_orders` holds 160 of `orders`' 200 rows and
    its SQL says it should hold all of them."""

    maintain_repo.sql(
        "CREATE TABLE fct_orders AS SELECT o.*, c.name FROM orders o "
        "JOIN customers c ON o.customer_id = c.id WHERE o.order_id <= 160"
    )
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.fct_orders": {
                "name": "fct_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."fct_orders"',
                "compiled_code": (
                    "select o.*, c.name from "
                    '"warehouse"."main"."orders" o inner join '
                    '"warehouse"."main"."customers" c on o.customer_id = c.id'
                ),
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = [f for f in payload["data"]["findings"] if f["code"] == "row_loss"]
    assert len(findings) == 1, payload["data"]["findings"]
    assert findings[0]["identifier"] == "fct_orders"
    assert findings[0]["data"]["row_count"] == 160
    assert findings[0]["data"]["parent_row_count"] == 200
    assert findings[0]["data"]["driving_parent"] == "warehouse.main.orders"
    # DuckDB bills nothing, so the counts were measured rather than estimated
    # and the verdict is a proof.
    assert findings[0]["exact"] is True
    assert payload["cost"]["paradigm"] == "free_local"
    assert "row_population" not in payload["data"]["suppressed"]


def test_verify_says_so_when_no_model_can_be_lined_up(
    maintain_repo, _assume_the_project_compiles
):
    """A manifest with nothing comparable in it must not read as a clean bill
    of health for row population."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.events": {
                "name": "events",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."events"',
                "compiled_code": 'select * from "warehouse"."main"."orders"',
                "config": {"materialized": "incremental"},
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert "row_population" in payload["data"]["suppressed"]
    assert any("incremental" in w for w in payload["warnings"])


def test_a_join_is_named_by_the_model_it_reads_not_the_cte_alias(tmp_path: Path):
    """What a compiled dbt model actually looks like: the join in the final
    select reads a CTE called `lines`, which reads `stg_order_items`. Reporting
    `lines` would hand the reader an alias they cannot look up."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model("stg_orders", 'select * from "wh"."main"."orders"'),
            "m.2": _model("stg_order_items", 'select * from "wh"."main"."items"'),
            "m.3": _model(
                "fct_order_lines",
                'with orders as (select * from "wh"."main"."stg_orders"), '
                'lines as (select * from "wh"."main"."stg_order_items"), '
                "final as (select orders.order_id, lines.sku from orders "
                "left join lines on orders.order_id = lines.order_id) "
                "select * from final",
            ),
        },
    )
    fanned = next(c for c in checks if c.model == "fct_order_lines")
    assert fanned.joins == ("left join to 'stg_order_items'",)
    # The key keeps the aliases the SQL uses, because that is the text the
    # reader will find when they open the file.
    assert fanned.join_keys == ("orders.order_id = lines.order_id",)


def test_a_project_that_was_never_compiled_says_so(
    maintain_repo, _assume_the_project_compiles
):
    """No manifest is not a clean project. The reason names the command that
    would produce one, the way the missing-run-results note does."""

    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert "no compiled manifest" in payload["data"]["suppressed"]["row_population"]
    assert any("dbt compile" in w for w in payload["warnings"])
