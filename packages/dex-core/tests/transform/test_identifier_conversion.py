# ruff: noqa: S608 -- fixture SQL over test-owned relation names
"""`transform plan` names each identifier conversion an edit adds and measures
what it does to the entities a model holds (#861).

The fixture (``products_project``) is the issue's reproduction: text product
keys mixing numeric-looking identifiers, a zero-padded twin (`7`, `007`), opaque
identifiers of a second scheme, two spellings of one opaque identifier, a hashed
key the model's own supplier filter already excludes, and a NULL, joined to
integer-keyed prices. Every expected count below is worked out by hand from
``conftest.PRODUCTS``, never from the SQL under test.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest
import sqlglot

from exmergo_dex_core.cli import main
from exmergo_dex_core.transform import identifier_conversion as ic

from .conftest import PRODUCTS, PRODUCTS_PATH


def _plan(
    repo: Path,
    warehouse: Path,
    content: str,
    capsys,
    *flags: str,
    declarations: list | None = None,
) -> dict:
    payload = repo / "edits.json"
    body: dict = {
        "edits": [{"path": PRODUCTS_PATH, "kind": "model_sql", "content": content}]
    }
    if declarations is not None:
        body["declarations"] = declarations
    payload.write_text(json.dumps(body), encoding="utf-8")
    rc = main(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            "repair the product join",
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
    envelope = json.loads(out)
    assert rc == 0, envelope
    return envelope


def _conversions(envelope: dict) -> list[dict]:
    return envelope["data"].get("identifier_conversions") or []


def _at(envelope: dict, site: str) -> dict:
    (found,) = [c for c in _conversions(envelope) if c["site"] == site]
    return found


_SRC = "{{ source('raw', 'raw_products') }}"
_PRICES = "{{ source('raw', 'product_prices') }}"
_HEAD = "{{ config(materialized='table') }}\n"

#: The repair that compiles by filtering every identifier that is not an
#: integer: what the 1.17.1 benchmark agent wrote.
FILTER_REPAIR = (
    _HEAD + "select p.product_id, p.product_name, p.email, l.price\n"
    f"from {_SRC} p\n"
    f"left join {_PRICES} l on try_cast(p.product_id as integer) = l.product_id\n"
    "where p.supplier <> 'retired' and try_cast(p.product_id as integer) is not null\n"
)

#: The repair that keeps both schemes: compare as text on the integer side.
TEXT_REPAIR = (
    _HEAD + "select p.product_id, p.product_name, p.email, l.price\n"
    f"from {_SRC} p\n"
    f"left join {_PRICES} l on p.product_id = cast(l.product_id as varchar)\n"
    "where p.supplier <> 'retired'\n"
)


# --- the issue's validation cases ------------------------------------------------


def test_filtering_a_second_identifier_scheme_is_undeclared_entity_loss(
    products_project, capsys
):
    """Mixed valid schemes, a NULL key and a legitimate prior filter at once.

    Rows reaching the filter: the 9 products minus the retired one (the model's
    own supplier filter, kept), so 8 rows, one with a NULL key. 7 distinct
    identifiers; `org_cedar`, `ORG_ELM` and `org_elm` do not convert (3), the
    NULL is not a failure. `7` and `007` collapse into one key."""

    repo, warehouse = products_project
    envelope = _plan(repo, warehouse, FILTER_REPAIR, capsys)
    found = _at(envelope, "filter")
    assert found["status"] == "undeclared"
    assert found["column"] == "product_id"
    assert found["steps"] == ["try_cast(INT)"]
    assert found["measured"] == {
        "input_rows": 8,
        "null_key_rows": 1,
        "input_entities": 7,
        "failed_rows": 3,
        "failed_entities": 3,
        "retained_entities": 4,
        "converted_entities": 3,
        "representation_changed_entities": 1,
        "collided_keys": 1,
        "colliding_source_entities": 2,
    }
    assert "other filters applied" in found["denominator"]
    warning = next(w for w in envelope["warnings"] if "3 of 7 distinct" in w)
    assert "column: product_id" in warning
    assert "invalid" not in warning.split("Nothing declared")[0]


def test_a_join_conversion_reports_coverage_against_the_other_side(
    products_project, capsys
):
    """The ON's own conversion, measured over the rows of its side that pass
    the scope's filters on that side alone (the supplier filter, which keeps
    the retired hashed id out) minus the filter that converts the key: 8 rows,
    one NULL, 7 distinct identifiers of which 3 do not convert; the 4 that do
    all find a price. The same cast in the WHERE is one warning, not two."""

    repo, warehouse = products_project
    envelope = _plan(repo, warehouse, FILTER_REPAIR, capsys)
    found = _at(envelope, "join")
    assert found["key_basis"] == "join"
    assert found["partner"] == "l.product_id"
    measured = found["measured"]
    assert measured["input_rows"] == 8
    assert measured["input_entities"] == 7
    assert measured["failed_entities"] == 3
    assert measured["retained_entities"] == 4
    assert measured["matched_entities"] == 4
    assert found["denominator"].startswith("rows of raw_products that pass")
    assert "product_prices" in found["denominator"]
    (warning,) = [w for w in envelope["warnings"] if "changes which" in w]
    assert "in the join," in warning and "in the filter," in warning


def test_a_broken_prior_leaves_identity_unchecked_while_the_conversion_reports(
    products_project, capsys
):
    """The prior model cannot run (its join compares text to integers), so
    there is no prior identity set; the conversion's own counts still stand."""

    repo, warehouse = products_project
    found = _at(_plan(repo, warehouse, FILTER_REPAIR, capsys), "filter")
    assert "identity" not in found
    assert found["identity_reason_code"] == "baseline_failed"
    assert found["measured"]["failed_entities"] == 3


def test_comparing_as_text_is_lossless(products_project, capsys):
    """Casting the integer side to text converts every price key, merges
    nothing and changes no spelling: lossless, with no warning."""

    repo, warehouse = products_project
    envelope = _plan(repo, warehouse, TEXT_REPAIR, capsys)
    found = _at(envelope, "join")
    assert found["column"] == "product_id"
    assert found["status"] == "lossless", found
    assert found["measured"]["input_entities"] == 3
    assert found["measured"]["failed_entities"] == 0
    assert found["measured"]["colliding_source_entities"] == 0
    assert found["measured"]["matched_entities"] == 3
    assert not [w for w in envelope["warnings"] if "changes which" in w]


def test_an_exclusion_recorded_against_its_column_is_declared(products_project, capsys):
    """Genuinely malformed identifiers with a declared policy: the same counts,
    but an assumption names the column, so the loss is `declared` and carries
    that assumption's evidence tier."""

    repo, warehouse = products_project
    declaration = {
        "model": "dim_products",
        "description": "One row per product with an integer catalogue id.",
        "assumptions": [
            {
                "decision": "Which product identifiers count",
                "chosen": "integer catalogue ids only; text ids are legacy rows",
                "evidence": "request",
                "column": "product_id",
            }
        ],
    }
    envelope = _plan(repo, warehouse, FILTER_REPAIR, capsys, declarations=[declaration])
    found = _at(envelope, "filter")
    assert found["status"] == "declared"
    assert found["assumption"]["evidence"] == "request"
    assert found["assumption"]["column"] == "product_id"
    assert found["measured"]["failed_entities"] == 3
    (decision,) = envelope["data"]["decisions"]
    assert decision["column"] == "product_id"
    assert not [w for w in envelope["warnings"] if "changes which" in w]


def test_an_assumption_on_another_column_does_not_declare_the_loss(
    products_project, capsys
):
    repo, warehouse = products_project
    declaration = {
        "model": "dim_products",
        "assumptions": [
            {
                "decision": "Which suppliers count",
                "chosen": "all but retired",
                "evidence": "request",
                "column": "supplier",
            }
        ],
    }
    envelope = _plan(repo, warehouse, FILTER_REPAIR, capsys, declarations=[declaration])
    assert _at(envelope, "filter")["status"] == "undeclared"


def test_a_leading_zero_collision_is_counted_even_without_a_filter(
    products_project, capsys
):
    """`7` and `007` become one integer key. Nothing is filtered, so rows are
    kept; the identity change is in the merge and the respelling."""

    repo, warehouse = products_project
    content = (
        _HEAD + "select try_cast(p.product_id as integer) as product_id, "
        "p.product_name\n"
        f"from {_SRC} p\nwhere p.supplier <> 'retired'\n"
    )
    found = _at(_plan(repo, warehouse, content, capsys), "projection")
    assert found["output_column"] == "product_id"
    assert found["measured"]["collided_keys"] == 1
    assert found["measured"]["colliding_source_entities"] == 2
    assert found["measured"]["representation_changed_entities"] == 1
    assert found["status"] == "undeclared"


def test_a_case_normalization_merges_two_spellings_of_one_identifier(
    products_project, capsys
):
    """Namespaced duplicates: `ORG_ELM` and `org_elm` are two identifiers that
    `lower()` merges. Nothing fails to convert; one key absorbs two."""

    repo, warehouse = products_project
    content = (
        _HEAD + "select lower(p.product_id) as product_id, p.product_name\n"
        f"from {_SRC} p\nwhere p.supplier <> 'retired'\n"
    )
    found = _at(_plan(repo, warehouse, content, capsys), "projection")
    assert found["steps"] == ["lower"]
    assert found["measured"]["failed_entities"] == 0
    assert found["measured"]["collided_keys"] == 1
    assert found["measured"]["colliding_source_entities"] == 2
    assert found["measured"]["representation_changed_entities"] == 1
    assert found["status"] == "undeclared"


def test_identity_change_is_detected_when_the_row_count_is_unchanged(
    products_project, capsys
):
    """A working prior and an authored model with the same 8 rows, whose key
    is read back through an integer: 7 identities before, 3 after (`7`, `12`,
    `13`); `007` is respelled and the three text identifiers become NULL."""

    repo, warehouse = products_project
    prior = (
        _HEAD + "select p.product_id, p.product_name\n"
        f"from {_SRC} p\nwhere p.supplier <> 'retired'\n"
    )
    (repo / "analytics" / PRODUCTS_PATH).write_text(prior, encoding="utf-8")
    authored = prior.replace(
        "select p.product_id,",
        "select cast(try_cast(p.product_id as integer) as varchar) as product_id,",
    )
    envelope = _plan(repo, warehouse, authored, capsys)
    found = _at(envelope, "projection")
    identity = found["identity"]
    assert identity["prior_rows"] == identity["authored_rows"] == 8
    assert identity["prior_entities"] == 7
    assert identity["authored_entities"] == 3
    assert identity["kept"] == 3
    assert identity["lost"] == 4
    assert identity["gained"] == 0
    assert identity["identity_changed"] is True
    assert any("identities in `product_id` are gone" in w for w in envelope["warnings"])


def test_a_conversion_the_prior_already_had_is_not_reported_again(
    products_project, capsys
):
    """An edit is judged on what it adds: a conversion already in the prior
    model, untouched, is the existing contract rather than this repair."""

    repo, warehouse = products_project
    (repo / "analytics" / PRODUCTS_PATH).write_text(FILTER_REPAIR, encoding="utf-8")
    envelope = _plan(repo, warehouse, FILTER_REPAIR.replace("p.email, ", ""), capsys)
    assert _conversions(envelope) == []


def test_protected_values_never_reach_the_envelope(products_project, capsys):
    """A PII-flagged pass-through (`email`) stays in the authored SQL, the plan
    is stored and measured, and no product identifier or email from the data
    appears anywhere in the envelope: the counts are the only thing returned,
    and a failed statement reports its exception type, not the warehouse's
    message (which for a failed cast names the value)."""

    repo, warehouse = products_project
    envelope = _plan(repo, warehouse, FILTER_REPAIR, capsys)
    assert envelope["data"]["plan_id"]
    text = json.dumps(envelope)
    for product_id, _supplier, email, _name in PRODUCTS:
        assert email not in text
        if product_id and product_id not in ("7", "12", "13"):
            # Bounded, so `007` inside a temporary path is not a match.
            bounded = re.compile(rf"(?<![\w/-]){re.escape(product_id)}(?![\w/-])")
            assert not bounded.search(text), product_id


# --- what is not measured says why --------------------------------------------------


def test_an_unmeasured_rewrite_of_an_identifier_is_unchecked(products_project, capsys):
    repo, warehouse = products_project
    content = (
        _HEAD + "select regexp_replace(p.product_id, '^0+', '') as product_id, "
        "p.product_name\n"
        f"from {_SRC} p\nwhere p.supplier <> 'retired'\n"
    )
    found = _at(_plan(repo, warehouse, content, capsys), "projection")
    assert found["status"] == "unchecked"
    assert found["reason_code"] == "unsupported_expression"
    assert "measured" not in found


def test_a_regex_filter_on_an_identifier_is_unchecked_not_silent(
    products_project, capsys
):
    repo, warehouse = products_project
    content = (
        _HEAD + "select p.product_id, p.product_name\n"
        f"from {_SRC} p\n"
        "where p.supplier <> 'retired' and regexp_matches(p.product_id, '^[0-9]+$')\n"
    )
    found = _at(_plan(repo, warehouse, content, capsys), "filter")
    assert found["reason_code"] == "unsupported_expression"


def test_an_unprofiled_input_is_unchecked_with_the_command_to_run(
    products_project, capsys
):
    duckdb = pytest.importorskip("duckdb")
    repo, warehouse = products_project
    conn = duckdb.connect(str(warehouse))
    conn.execute("CREATE TABLE raw_products_v2 AS SELECT * FROM raw_products")
    conn.close()
    sources = repo / "analytics" / "models" / "marts" / "sources.yml"
    sources.write_text(
        sources.read_text() + "      - name: raw_products_v2\n", encoding="utf-8"
    )
    content = FILTER_REPAIR.replace("'raw_products'", "'raw_products_v2'")
    for found in _conversions(_plan(repo, warehouse, content, capsys)):
        assert found["status"] == "unchecked"
        assert found["reason_code"] == "unprofiled_input"
        assert "explore profile raw_products_v2" in found["reason"]


def test_counting_off_names_the_conversion_and_measures_nothing(
    products_project, capsys
):
    repo, warehouse = products_project
    envelope = _plan(repo, warehouse, FILTER_REPAIR, capsys, "--no-attribute-rows")
    found = _conversions(envelope)
    assert len(found) == 2
    assert {c["reason_code"] for c in found} == {"counting_off"}
    assert all("measured" not in c for c in found)


def test_conversions_beyond_the_cap_are_named_and_not_measured(
    products_project, capsys
):
    repo, warehouse = products_project
    types = ["integer", "bigint", "smallint", "double", "decimal(18, 0)"]
    projections = ", ".join(
        f"try_cast(p.product_id as {t}) as key_{i}_id" for i, t in enumerate(types)
    )
    content = _HEAD + f"select {projections}\nfrom {_SRC} p\n"
    found = _conversions(_plan(repo, warehouse, content, capsys))
    assert len(found) == ic.MAX_PER_MODEL + 1
    capped = [c for c in found if c.get("reason_code") == "cap_reached"]
    assert len(capped) == 1
    assert all("measured" in c for c in found if c not in capped)


def test_a_cast_on_a_column_that_is_no_identifier_is_not_reported(
    products_project, capsys
):
    repo, warehouse = products_project
    content = PRODUCTS_PRIOR_TEXT_JOIN.replace(
        "l.price", "cast(l.price as decimal(10, 2)) as price"
    )
    found = _conversions(_plan(repo, warehouse, content, capsys))
    assert all(c["column"] != "price" for c in found)


PRODUCTS_PRIOR_TEXT_JOIN = TEXT_REPAIR


# --- detection and the statements, without a warehouse --------------------------


def _tree(sql: str, dialect: str = "duckdb"):
    return sqlglot.parse_one(sql, read=dialect)


def test_detection_reports_the_outermost_step_of_a_chain_once():
    found = ic.find_conversions(
        _tree(
            "select cast(lower(trim(p.k)) as int) as k from raw p "
            "join prices l on try_cast(p.k as int) = l.k "
            "where some_check(p.code) and try_cast(p.k as int) is not null"
        )
    )
    supported = sorted(
        (f.site, [s.label() for s in f.steps]) for f in found if not f.unsupported
    )
    assert supported == [
        ("filter", ["try_cast(INT)"]),
        ("join", ["try_cast(INT)"]),
        ("projection", ["cast(INT)", "lower", "trim"]),
    ]
    # A function dex does not measure, over one column, is named as such.
    assert [(f.site, f.column.name) for f in found if f.unsupported] == [
        ("filter", "code")
    ]


def test_bigquery_safe_cast_is_a_safe_conversion():
    (found,) = ic.find_conversions(
        _tree("select safe_cast(k as int64) as k from t", "bigquery")
    )
    assert found.steps[0].kind == "try_cast"


@pytest.mark.parametrize(
    "dialect",
    [
        "duckdb",
        "bigquery",
        "snowflake",
        "databricks",
        "redshift",
        "postgres",
        "clickhouse",
        "trino",
    ],
)
def test_the_probes_are_aggregate_selects_on_every_dialect(dialect):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    authored = _tree(
        "with base as (select * from db.s.raw where supplier <> 'retired') "
        "select b.product_id, l.price from base b "
        "left join db.s.prices l on cast(b.product_id as int) = l.product_id "
        "where cast(b.product_id as int) is not null",
    )
    prior = _tree("select product_id from db.s.raw")
    for index, _found in enumerate(ic.find_conversions(authored)):
        built = ic.conversion_probe(authored, index, dialect)
        assert built.sql is not None, built
        assert assert_select_only(built.sql, dialect=dialect)
        assert "dex_raw" in built.sql
        # The hard cast is measured in its safe form, and it survives the
        # dialect (sqlglot renders a fresh TryCast as CAST on some). Postgres
        # has none, so an integer cast is guarded by its shape and range.
        safe = {
            "bigquery": "SAFE_CAST(",
            "clickhouse": "accurateCastOrNull(",
            "postgres": "BETWEEN -2147483648 AND 2147483647",
        }
        assert safe.get(dialect, "TRY_CAST(") in built.sql
        if dialect == "snowflake":
            assert "TRY_CAST(CAST(" in built.sql
    statement = ic.identity_probe(prior, authored, "product_id", dialect)
    assert assert_select_only(statement, dialect=dialect)


def test_a_postgres_cast_it_cannot_guard_is_unchecked():
    # Only the integer family is guarded on Postgres (#1075).
    tree = _tree("select cast(k as date) as k from raw")
    built = ic.conversion_probe(tree, 0, "postgres")
    assert built.sql is None
    assert built.code == "no_safe_cast_on_connector"


def test_an_aggregating_scope_is_unchecked_rather_than_counted():
    tree = _tree("select try_cast(k as int) as k, count(*) as n from raw group by 1")
    built = ic.conversion_probe(tree, 0, "duckdb")
    assert built.sql is None and built.code == "unsupported_scope"


def test_a_conversion_inside_a_quoted_mixed_case_cte_preserves_the_name():
    """a quoted mixed-case cte name is re-emitted with quotes and casing."""
    authored = _tree(
        'with "Orders_Clean" as ('
        "    select lower(order_id) as id from raw_orders"
        ') select id from "Orders_Clean"'
    )
    for dialect in ("postgres", "snowflake", "duckdb"):
        built = ic.conversion_probe(authored, 0, dialect)
        assert built.sql is not None
        assert 'FROM "Orders_Clean"' in built.sql
        assert "FROM orders_clean" not in built.sql


def test_a_filter_conversion_inside_a_quoted_mixed_case_cte_preserves_the_name():
    """filter conversion inside a quoted mixed-case cte preserves quotes."""
    authored = _tree(
        'with "Orders_Clean" as ('
        "    select order_id from raw_orders where lower(order_id) <> 'retired'"
        ') select order_id from "Orders_Clean"'
    )
    for dialect in ("postgres", "snowflake", "duckdb"):
        built = ic.conversion_probe(authored, 0, dialect)
        assert built.sql is not None
        assert 'FROM "Orders_Clean"' in built.sql
        assert "FROM orders_clean" not in built.sql


def test_a_cast_conversion_inside_a_quoted_mixed_case_cte_preserves_the_name():
    """cast conversion inside a quoted mixed-case cte preserves quotes."""
    authored = _tree(
        'with "Orders_Clean" as ('
        "    select cast(order_id as varchar) as id from raw_orders"
        ') select id from "Orders_Clean"'
    )
    built = ic.conversion_probe(authored, 0, "snowflake")
    assert built.sql is not None
    assert 'FROM "Orders_Clean"' in built.sql
    assert "FROM orders_clean" not in built.sql


# --- a metered connector -----------------------------------------------------------

from exmergo_dex_core.adapters.bigquery import BigQueryAdapter  # noqa: E402
from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache  # noqa: E402
from exmergo_dex_core.config import BigQueryTarget, DexConfig  # noqa: E402
from exmergo_dex_core.engine import DexEngine  # noqa: E402
from exmergo_dex_core.envelope import Paradigm  # noqa: E402
from exmergo_dex_core.guards.cost_guard import CostGate  # noqa: E402
from exmergo_dex_core.storage import FilesystemStore  # noqa: E402

BQ_PRIOR = "select product_id from {{ source('shop', 'products') }}\n"
BQ_REPAIR = (
    "select product_id from {{ source('shop', 'products') }}\n"
    "where safe_cast(product_id as int64) is not null\n"
)


@pytest.fixture
def metered_products(tmp_path: Path, monkeypatch):
    bigquery = pytest.importorskip("google.cloud.bigquery")
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    statements: list[str] = []

    def answer(sql: str):
        statements.append(sql)
        return [dict.fromkeys(ic._COUNT_FIELDS, 0) | {"dex_rows": 1}]

    client = FakeBigQueryClient(
        project="test-proj",
        tables=[
            FakeTable(
                project="test-proj",
                dataset_id="shop",
                table_id="products",
                schema=[bigquery.SchemaField("product_id", "STRING")],
                num_rows=100,
                num_bytes=50_000,
            )
        ],
        row_resolver=answer,
    )
    project = tmp_path / "analytics"
    (project / "models" / "marts").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0.0"\nprofile: shop\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / PRODUCTS_PATH).write_text(BQ_PRIOR, encoding="utf-8")
    store = FilesystemStore(tmp_path)
    store.save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier="test-proj.shop.products",
                    row_count=100,
                    columns=[ColumnProfile(name="product_id", data_type="STRING")],
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
            client=client,
            principal_type="user",
        )

    monkeypatch.setattr(DexEngine, "_adapter", opener)

    def run(*, attribute_rows=None, confirm=False, budget=None, broken_prior=False):
        if broken_prior:
            # The prior model does not compile, so every statement that reads
            # it fails its dry run, which is how BigQuery prices.
            from google.api_core import exceptions as api_exceptions

            client.dry_run_error = lambda sql: (
                None
                if "SAFE_CAST" in sql and "prior_rows" not in sql
                else api_exceptions.BadRequest("No matching signature for operator =")
            )
        from exmergo_dex_core.cli import dispatch

        payload = tmp_path / "edits.json"
        payload.write_text(
            json.dumps(
                {
                    "edits": [
                        {
                            "path": PRODUCTS_PATH,
                            "kind": "model_sql",
                            "content": BQ_REPAIR,
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
            argument="repair the key",
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
        return dispatch(args, engine), opened, statements

    return run


def test_a_metered_plan_names_the_conversion_and_scans_nothing_by_default(
    metered_products,
):
    envelope, opened, statements = metered_products()
    assert envelope.status.value == "ok"
    assert opened == [] and statements == []
    (found,) = envelope.data["identifier_conversions"]
    assert found["reason_code"] == "counting_off"
    assert "--attribute-rows" in found["reason"]


def test_a_metered_plan_prices_conversions_in_the_one_attribution_handshake(
    metered_products,
):
    envelope, opened, _statements = metered_products(attribute_rows=True)
    assert envelope.status.value == "needs_confirmation"
    assert opened == ["transform plan"]
    assert "(identifier conversions)" in json.dumps(envelope.data)
    (found,) = envelope.data["identifier_conversions"]
    assert found["reason_code"] == "awaiting_confirmation"


def test_a_confirmed_metered_plan_measures_the_conversion(metered_products):
    envelope, _opened, statements = metered_products(
        attribute_rows=True, confirm=True, budget=10_000_000_000
    )
    assert envelope.status.value == "ok"
    (found,) = envelope.data["identifier_conversions"]
    assert found["status"] == "lossless"
    assert any("SAFE_CAST" in sql for sql in statements)
    assert envelope.data["spend"]["bytes_billed"] > 0


def test_a_conversion_inside_a_cte_is_measured_where_it_sits(products_project, capsys):
    """The filter sits in `base`; `final` reads columns the probe replaces, so
    only the CTEs up to `base` are kept. The supplier filter is downstream of
    the conversion, so the retired row does reach it: 9 rows, 8 identifiers,
    4 that do not convert."""

    repo, warehouse = products_project
    content = (
        _HEAD + "with base as (\n"
        f"    select product_id, product_name, supplier from {_SRC}\n"
        "    where try_cast(product_id as integer) is not null\n"
        "), final as (\n"
        "    select product_id, product_name from base where supplier <> 'retired'\n"
        ")\nselect * from final\n"
    )
    found = _at(_plan(repo, warehouse, content, capsys), "filter")
    assert found["scope"] == "base"
    assert found["measured"]["input_rows"] == 9
    assert found["measured"]["input_entities"] == 8
    assert found["measured"]["failed_entities"] == 4
    assert "`base`" in found["denominator"]


def test_a_prior_that_does_not_compile_degrades_alone_on_a_dry_run_connector(
    metered_products,
):
    """BigQuery prices by dry run, and a repair's prior model often does not
    compile. Pricing used to raise out of the whole batch, so nothing was
    measured; now the unpriceable statements are named and the rest run."""

    envelope, _opened, statements = metered_products(
        attribute_rows=True, confirm=True, budget=10_000_000_000, broken_prior=True
    )
    assert envelope.status.value == "ok"
    (found,) = envelope.data["identifier_conversions"]
    assert found["status"] == "lossless", found
    assert found["identity_reason_code"] == "baseline_failed"
    assert statements, "the conversion probe ran"
    assert not any("could not analyse" in w for w in envelope.warnings)
    (model,) = envelope.data["row_attribution"]
    assert all(not c["attributed"] for c in model["changes"])
    assert any("could not be priced" in c["reason"] for c in model["changes"])
    assert "No matching signature" not in json.dumps(envelope.data)


# --- what the independent review found ---------------------------------------------


def test_orphan_keys_on_the_joined_side_do_not_make_a_lossless_cast_lossy(
    products_project, capsys
):
    """Comparing as text casts the price keys. A price row for a product that
    does not exist is unmatched before and after the cast: it is reported, and
    it is not this conversion's loss."""

    duckdb = pytest.importorskip("duckdb")
    repo, warehouse = products_project
    conn = duckdb.connect(str(warehouse))
    conn.execute("INSERT INTO product_prices VALUES (99, 1.0)")
    conn.close()
    found = _at(_plan(repo, warehouse, TEXT_REPAIR, capsys), "join")
    assert found["measured"]["input_entities"] == 4
    assert found["measured"]["matched_entities"] == 3
    assert found["status"] == "lossless"


def test_a_separate_filter_in_the_same_edit_is_not_blamed_on_the_conversion(
    products_project, capsys
):
    """The edit adds a lossless cast and, separately, the supplier filter. The
    identity comparison sees the retired product gone; that is reported, and
    the cast stays lossless, because the cast did not remove it."""

    repo, warehouse = products_project
    prior = _HEAD + f"select p.product_id, p.product_name\nfrom {_SRC} p\n"
    (repo / "analytics" / PRODUCTS_PATH).write_text(prior, encoding="utf-8")
    found = _at(_plan(repo, warehouse, TEXT_REPAIR, capsys), "join")
    assert found["status"] == "lossless"
    assert found["identity"]["lost"] == 1
    assert found["identity"]["identity_changed"] is True


def test_a_conversion_in_a_union_branch_is_unchecked_not_absent(
    products_project, capsys
):
    repo, warehouse = products_project
    content = (
        _HEAD
        + f"select try_cast(p.product_id as integer) as product_id from {_SRC} p\n"
        f"union all\nselect l.product_id from {_PRICES} l\n"
    )
    found = _conversions(_plan(repo, warehouse, content, capsys))
    assert len(found) == 1
    assert found[0]["scope"] == ic.UNALIGNED
    assert found[0]["reason_code"] == "unsupported_scope"


def test_aggregates_over_an_identifier_are_not_conversions(products_project, capsys):
    repo, warehouse = products_project
    content = (
        _HEAD + "select p.supplier, count(distinct p.product_id) as product_count, "
        "max(p.product_id) as last_id\n"
        f"from {_SRC} p\ngroup by 1\n"
    )
    assert _conversions(_plan(repo, warehouse, content, capsys)) == []


def test_a_merge_inside_a_filter_changes_no_identity(products_project, capsys):
    """`lower()` in a WHERE merges `ORG_ELM` and `org_elm` only inside the
    predicate; nothing fails to convert and no output key changes."""

    repo, warehouse = products_project
    content = (
        _HEAD + f"select p.product_id, p.product_name\nfrom {_SRC} p\n"
        "where lower(p.product_id) <> 'org_cedar'\n"
    )
    found = _at(_plan(repo, warehouse, content, capsys), "filter")
    assert found["measured"]["colliding_source_entities"] == 2
    assert found["status"] == "lossless"


def test_a_numeric_source_is_not_counted_as_respelled(products_project, capsys):
    repo, warehouse = products_project
    content = (
        _HEAD + "select try_cast(l.product_id as bigint) as product_id, l.price\n"
        f"from {_PRICES} l\n"
    )
    found = _at(_plan(repo, warehouse, content, capsys), "projection")
    assert "representation_changed_entities" not in found["measured"]
    assert found["status"] == "lossless"


def test_a_failure_reading_the_counts_back_never_fails_the_plan(
    products_project, capsys, monkeypatch
):
    repo, warehouse = products_project

    def broken(self):
        raise RuntimeError("defect")

    monkeypatch.setattr(ic.Diagnosis, "settle", broken)
    envelope = _plan(repo, warehouse, FILTER_REPAIR, capsys)
    assert envelope["status"] == "ok" and envelope["data"]["plan_id"]
    assert any("RuntimeError" in w for w in envelope["warnings"])
