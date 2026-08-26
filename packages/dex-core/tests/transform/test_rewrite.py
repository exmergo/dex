"""The surgical rewrite primitive: change the name, move nothing else.

Every test here is really the same assertion in a different surface: the bytes
that named the thing changed, and every other byte is where it was. That is what
makes a propagation plan reviewable, so it is what gets asserted rather than the
rewrite merely producing valid SQL.

The last group covers the post-condition, which exists because a splice at a
wrong offset produces SQL that still parses. Nothing else would catch that.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.transform.rewrite import (
    RewriteError,
    jinja_names,
    output_columns,
    project_column_in_sql,
    rename_column_in_sql,
    splice,
    unproject_column_in_sql,
    yaml_blocks,
    yaml_names,
)

MODEL = """{{ config(materialized='table') }}

with base as (
    select
        order_id,          -- the natural key
        amount
    from {{ ref('raw_orders') }}
)

select
    b.order_id,
    b.amount as order_id_amount,
    upper(b.status) as order_id
from base b
join {{ ref('order_id') }} o on o.order_id = b.order_id
"""


class TestColumnRename:
    def test_renames_every_column_use_and_nothing_else(self):
        result = rename_column_in_sql(MODEL, "m.sql", "order_id", "order_key")

        assert result.changed == 5
        # The comment, the config header and the indentation are byte-identical.
        assert "order_key,          -- the natural key" in result.content
        assert result.content.startswith("{{ config(materialized='table') }}")
        # A model called `order_id` is not a column called `order_id`.
        assert "{{ ref('order_id') }}" in result.content
        # A different column that merely contains the name is untouched.
        assert "as order_id_amount" in result.content

    def test_leaves_a_table_that_shares_the_column_name_alone(self):
        sql = "select order_id from order_id\n"

        result = rename_column_in_sql(sql, "m.sql", "order_id", "order_key")

        assert result.content == "select order_key from order_id\n"

    def test_a_star_carries_the_column_through_without_an_edit(self):
        result = rename_column_in_sql(
            "select * from {{ ref('x') }}\n", "m.sql", "a", "b"
        )

        assert result.star is True
        assert result.changed == 0
        assert result.content == "select * from {{ ref('x') }}\n"

    def test_a_set_operation_is_refused_rather_than_guessed_at(self):
        sql = "select a from x\nunion all\nselect b from y\n"

        with pytest.raises(RewriteError, match="set operation"):
            rename_column_in_sql(sql, "m.sql", "a", "c")

    def test_unparseable_sql_names_the_file(self):
        with pytest.raises(RewriteError, match=r"m\.sql"):
            rename_column_in_sql("select from from from\n", "m.sql", "a", "b")


class TestProjection:
    def test_adds_a_column_at_the_existing_indentation(self):
        sql = "select\n    a,\n    b\nfrom {{ ref('x') }}\n"

        result = project_column_in_sql(sql, "m.sql", "upper(a)", "shout")

        assert result.content == (
            "select\n    a,\n    b,\n    upper(a) as shout\nfrom {{ ref('x') }}\n"
        )

    def test_a_bare_passthrough_carries_no_redundant_alias(self):
        result = project_column_in_sql(
            "select a\nfrom {{ ref('x') }}\n", "m.sql", "z", "z"
        )

        assert "z\nfrom" in result.content
        assert " as z" not in result.content

    def test_a_column_already_projected_is_left_alone(self):
        sql = "select a, z from {{ ref('x') }}\n"

        assert project_column_in_sql(sql, "m.sql", "q", "z").changed == 0

    def test_an_aggregating_model_refuses_a_bare_column(self):
        """A GROUP BY makes a bare column neither grouped nor aggregated.

        Which of the two it should be is a question about the model's grain, so
        the refusal names both options rather than picking one and producing SQL
        that fails at `dbt run`.
        """

        sql = "select region, count(*) as n\nfrom {{ ref('x') }}\ngroup by region\n"

        with pytest.raises(RewriteError, match="GROUP BY"):
            project_column_in_sql(sql, "m.sql", "upper(region)", "shout")

    def test_an_aggregating_model_accepts_an_aggregate(self):
        sql = "select region, count(*) as n\nfrom {{ ref('x') }}\ngroup by region\n"

        result = project_column_in_sql(sql, "m.sql", "max(amount)", "biggest")

        assert "max(amount) as biggest" in result.content


class TestProjectionRemoval:
    SQL = "select\n    a,\n    b,          -- money\n    c\nfrom {{ ref('x') }}\n"

    @pytest.mark.parametrize(
        ("column", "expected"),
        [
            ("a", "select\n    b,          -- money\n    c\nfrom {{ ref('x') }}\n"),
            ("b", "select\n    a,\n    c\nfrom {{ ref('x') }}\n"),
            ("c", "select\n    a,\n    b          -- money\nfrom {{ ref('x') }}\n"),
        ],
    )
    def test_takes_the_column_its_comma_and_its_own_comment(self, column, expected):
        assert unproject_column_in_sql(self.SQL, "m.sql", column).content == expected

    def test_refuses_to_empty_a_model(self):
        with pytest.raises(RewriteError, match="only column"):
            unproject_column_in_sql("select a from {{ ref('x') }}\n", "m.sql", "a")


SCHEMA = """version: 2
models:
  - name: stg_orders
    description: "keyed by order_id"
    columns:
      - name: order_id
        description: the order_id column
        tests:
          - unique
      - name: amount

  - name: stg_customers
"""


class TestYaml:
    def test_a_description_containing_the_name_is_not_a_reference(self):
        """The reason this walks the structure instead of scanning the text.

        A column's description routinely contains the column's own name, and a
        rename that rewrote prose would change what the project says about itself
        without changing what it computes.
        """

        named = [n.name for n in yaml_names(SCHEMA)]

        assert named.count("order_id") == 1
        spans = [n for n in yaml_names(SCHEMA) if n.name == "order_id"]
        assert SCHEMA[spans[0].span[0] : spans[0].span[1]] == "order_id"
        assert spans[0].form == "yaml_column"

    def test_a_column_entry_ends_before_its_sibling(self):
        """PyYAML's own `end_mark` runs past a block node into the next entry.

        Trusting it makes removing one column take the column below it with it,
        in a file the reviewer sees as a single deletion.
        """

        block = next(
            b
            for b in yaml_blocks(SCHEMA)
            if b.form == "yaml_column" and b.name == "order_id"
        )

        removed = splice(SCHEMA, [(block.span[0], block.span[1], "")])

        assert "- name: amount" in removed
        assert "- name: order_id" not in removed
        assert "name: stg_customers" in removed

    def test_a_model_entry_ends_before_the_next_model(self):
        block = next(
            b
            for b in yaml_blocks(SCHEMA)
            if b.form == "yaml_model_entry" and b.name == "stg_orders"
        )

        removed = splice(SCHEMA, [(block.span[0], block.span[1], "")])

        assert "stg_orders" not in removed
        assert "stg_customers" in removed


class TestJinja:
    SOURCE = (
        "{% macro shout(column_name) %}{{ column_name }}{% endmacro %}\n"
        "select {{ shout('amount') }} from {{ source('raw', 'orders') }}\n"
        "join {{ ref(var('other')) }} x on true\n"
    )

    def test_reports_a_definition_and_a_call_as_different_forms(self):
        forms = {(n.name, n.form) for n in jinja_names(self.SOURCE)}

        assert ("shout", "definition") in forms
        assert ("shout", "macro_call") in forms

    def test_a_source_names_each_half_separately(self):
        halves = {
            n.form: self.SOURCE[n.span[0] : n.span[1]]
            for n in jinja_names(self.SOURCE)
            if n.kind == "source"
        }

        assert halves == {"source_call_namespace": "raw", "source_call": "orders"}

    def test_an_unresolved_ref_contributes_no_name(self):
        """`{{ ref(var('x')) }}` names a model only dbt can know.

        The inner `var` is reported because it *is* resolvable; the outer `ref`
        is not, and inventing a span for it would give a rewriter somewhere to
        write a name that was never there.
        """

        names = jinja_names(self.SOURCE)

        assert not [n for n in names if n.form == "ref_call"]
        assert next(n for n in names if n.form == "var_call").name == "other"


class TestPostCondition:
    def test_a_rewrite_that_changed_the_wrong_thing_is_caught(self, monkeypatch):
        """The only thing standing between an offset bug and a plausible plan.

        A splice at a wrong offset still parses, so the guarantee cannot come
        from the rewrite succeeding. Here the splice is corrupted to disturb a
        *neighbouring* column, which is exactly the shape an off-by-one produces
        and the reason the check compares the whole output set rather than only
        the column it was asked to rename.
        """

        import exmergo_dex_core.transform.rewrite as rewrite

        real = rewrite.splice
        monkeypatch.setattr(
            rewrite,
            "splice",
            lambda content, edits: real(content, edits).replace(" b ", " zzz "),
        )

        with pytest.raises(RewriteError, match="defect in dex"):
            rewrite.rename_column_in_sql(
                "select a, b from {{ ref('x') }}\n", "m.sql", "a", "c"
            )


class TestSplice:
    def test_overlapping_edits_are_refused_rather_than_reconciled(self):
        with pytest.raises(RewriteError, match="overlapping"):
            splice("abcdef", [(0, 4, "x"), (2, 6, "y")])

    def test_edits_apply_right_to_left_so_offsets_stay_valid(self):
        assert splice("abcdef", [(0, 1, "AAA"), (4, 5, "EEE")]) == "AAAbcdEEEf"


class TestOutputColumns:
    def test_a_star_is_unknown_not_empty(self):
        assert output_columns("select * from {{ ref('x') }}\n", "m.sql") is None

    def test_an_alias_is_the_output_name(self):
        assert output_columns("select a as b from {{ ref('x') }}\n", "m.sql") == {"b"}
