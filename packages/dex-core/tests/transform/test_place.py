"""`transform place`: where a shared derived column belongs, and the case for it.

The proposal is the product here, so the tests assert the *reasoning* as much as
the edits. "Propose, do not impose" is only worth saying if the caller can see
which ancestor was chosen, why it is the lowest, and what the fallback would cost
them, and none of that is checkable by looking at the diff alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import load
from exmergo_dex_core.transform.place import (
    PlacementRefusedError,
    derivation_inputs,
    place,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Two marts descending from one intermediate parent.

    `stg_orders` and `stg_customers` feed `int_orders`, which feeds `mid`, which
    feeds `mart_a` and `mart_b`. Three levels, so there is a hop between the
    ancestor and the targets that must be threaded and must not be documented.

    `mart_events` descends from a source nothing else touches, so a request
    naming it and a mart shares no lineage at all and has to fall back.
    """

    root = tmp_path / "shop"
    _write(
        root / "dbt_project.yml",
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n',
    )
    _write(
        root / "models/stg_orders.sql",
        "select\n    order_id,\n    customer_id,\n    amount\n"
        "from {{ source('raw', 'orders') }}\n",
    )
    _write(
        root / "models/stg_customers.sql",
        "select\n    customer_id,\n    region\nfrom {{ source('raw', 'customers') }}\n",
    )
    _write(
        root / "models/int_orders.sql",
        "select\n    o.order_id,\n    o.amount,\n    c.region\n"
        "from {{ ref('stg_orders') }} o\njoin {{ ref('stg_customers') }} c "
        "using (customer_id)\n",
    )
    _write(
        root / "models/mid.sql",
        "select\n    order_id,\n    amount,\n    region\n"
        "from {{ ref('int_orders') }}\n",
    )
    _write(
        root / "models/mart_a.sql",
        "select\n    order_id,\n    amount\nfrom {{ ref('mid') }}\n",
    )
    _write(
        root / "models/mart_b.sql",
        "select\n    order_id,\n    region\nfrom {{ ref('mid') }}\n",
    )
    _write(
        root / "models/stg_events.sql",
        "select\n    event_id,\n    region\nfrom {{ source('raw', 'events') }}\n",
    )
    _write(
        root / "models/mart_events.sql",
        "select\n    event_id,\n    region\nfrom {{ ref('stg_events') }}\n",
    )
    _write(
        root / "models/schema.yml",
        "version: 2\nmodels:\n"
        "  - name: int_orders\n    columns:\n      - name: order_id\n"
        "  - name: mid\n    columns:\n      - name: order_id\n"
        "  - name: mart_a\n    columns:\n      - name: order_id\n"
        "  - name: mart_b\n    columns:\n      - name: order_id\n",
    )
    return root


def _place(root: Path, column, targets, expr):
    return place(load(root), root, column, targets, expr)


class TestSharedAncestor:
    def test_two_marts_share_a_parent_and_the_column_lands_there(self, project: Path):
        """The issue's acceptance case."""

        result = _place(project, "geo_segment", ["mart_a", "mart_b"], "upper(region)")

        assert result.strategy == "common_ancestor"
        # `mid`, not `int_orders`: both are common ancestors and `mid` is the
        # lower one, which is the entire question this command answers.
        assert result.ancestor == "mid"
        paths = {edit.path for edit in result.edits}
        assert "models/mid.sql" in paths
        assert "models/mart_a.sql" in paths
        assert "models/mart_b.sql" in paths
        assert "models/int_orders.sql" not in paths

    def test_the_ancestor_gets_the_derivation_and_the_targets_get_a_passthrough(
        self, project: Path
    ):
        result = _place(project, "geo_segment", ["mart_a", "mart_b"], "upper(region)")

        ancestor = next(e for e in result.edits if e.path.endswith("mid.sql"))
        target = next(e for e in result.edits if e.path.endswith("mart_a.sql"))
        assert "upper(region) as geo_segment" in ancestor.new_content
        assert "    geo_segment\n" in target.new_content
        assert "upper(region)" not in target.new_content

    def test_the_proposal_names_the_ancestor_and_the_chain(self, project: Path):
        """A caller has to be able to disagree, which means seeing the case."""

        result = _place(project, "geo_segment", ["mart_a", "mart_b"], "upper(region)")

        joined = " ".join(result.reasoning)
        assert "mid" in joined
        assert "lowest" in joined
        assert result.chain["mart_a"] == ["mid", "mart_a"]
        assert result.chain["mart_b"] == ["mid", "mart_b"]

    def test_only_the_ends_of_the_chain_are_documented(self, project: Path):
        """The hops in between carry the column and declare nothing.

        Documenting them would fill the diff with entries for a column that is
        only passing through.
        """

        result = _place(
            project, "geo_segment", ["mart_a", "mart_b"], "upper(region) || order_id"
        )

        assert result.ancestor == "mid"
        assert result.chain["mart_a"] == ["mid", "mart_a"]
        yml = next(e for e in result.edits if e.path.endswith("schema.yml"))
        # `mid` (the ancestor) and both targets, and not `int_orders`, which is
        # only in the lineage rather than on the chain.
        assert yml.new_content.count("- name: geo_segment") == 3
        assert "int_orders" in yml.new_content
        before_mid = yml.new_content.split("- name: mid")[0]
        assert "geo_segment" not in before_mid


class TestFallback:
    def test_no_shared_lineage_falls_back_per_target_with_the_reason(
        self, project: Path
    ):
        """Proposed, not refused: it does reach the outcome that was asked for.

        Named as the worse answer, because the copies drift and somebody should
        know that before applying it.
        """

        result = _place(
            project, "geo_segment", ["mart_a", "mart_events"], "upper(region)"
        )

        assert result.strategy == "per_target"
        assert result.ancestor is None
        assert "drift" in " ".join(result.reasoning)

    def test_an_ancestor_missing_an_input_is_named_rather_than_widened(
        self, project: Path
    ):
        """dex will not hunt further upstream to pull an input down.

        One placement request would become an unbounded rewrite of the graph
        above it, so the near miss is reported and the caller decides.
        """

        result = _place(
            project, "big_spender", ["mart_a", "mart_b"], "upper(customer_id)"
        )

        assert result.strategy == "per_target"
        reason = " ".join(result.reasoning)
        assert "mid" in reason
        assert "customer_id" in reason
        assert "further upstream" in reason


class TestRefusals:
    def test_one_target_is_not_a_graph_question(self, project: Path):
        with pytest.raises(PlacementRefusedError, match="at least two"):
            _place(project, "x", ["mart_a"], "upper(region)")

    def test_a_model_the_project_does_not_have_is_named(self, project: Path):
        with pytest.raises(PlacementRefusedError, match="nope"):
            _place(project, "x", ["mart_a", "nope"], "upper(region)")

    def test_an_expression_reading_no_columns_has_no_placement_question(
        self, project: Path
    ):
        with pytest.raises(PlacementRefusedError, match="reads no columns"):
            _place(project, "x", ["mart_a", "mart_b"], "1")

    def test_an_aggregating_target_refuses_rather_than_breaking_the_build(
        self, project: Path
    ):
        """A bare column in a grouped SELECT is neither grouped nor aggregated.

        Caught here rather than at `dbt run`, and the message names both ways to
        resolve it because dex will not choose between them.
        """

        _write(
            project / "models/mart_a.sql",
            "select\n    order_id,\n    count(*) as n\nfrom {{ ref('mid') }}\n"
            "group by order_id\n",
        )

        with pytest.raises(PlacementRefusedError, match="GROUP BY"):
            _place(project, "geo_segment", ["mart_a", "mart_b"], "upper(region)")


class TestInputs:
    def test_inputs_come_from_the_expression_not_from_the_caller(self):
        """One source of truth, so the two cannot disagree.

        A hand-supplied input list that did not match the expression would
        produce a confidently wrong ancestor with no way for dex to notice.
        """

        assert derivation_inputs("case when a > b then c end") == ["a", "b", "c"]

    def test_an_unreadable_expression_is_refused(self):
        with pytest.raises(PlacementRefusedError, match="could not read"):
            derivation_inputs("select from from")


class TestCommandSurface:
    def test_explain_answers_the_question_and_stores_nothing(
        self, project: Path, capsys
    ):
        rc = main(
            [
                "--repo-root",
                str(project.parent),
                "transform",
                "place",
                "geo_segment",
                "--targets",
                "mart_a,mart_b",
                "--expr",
                "upper(region)",
                "--explain",
            ]
        )
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["data"]["ancestor"] == "mid"
        assert payload["data"]["reasoning"]
        assert "plan_id" not in payload["data"]
        assert not (project.parent / ".dex" / "plans").exists()

    def test_without_explain_the_same_answer_arrives_as_a_plan(
        self, project: Path, capsys
    ):
        rc = main(
            [
                "--repo-root",
                str(project.parent),
                "transform",
                "place",
                "geo_segment",
                "--targets",
                "mart_a",
                "--targets",
                "mart_b",
                "--expr",
                "upper(region)",
            ]
        )
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["data"]["ancestor"] == "mid"
        assert payload["data"]["plan_id"]
        assert len(payload["data"]["paths"]) == 4
