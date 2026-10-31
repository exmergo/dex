"""`transform plan --scaffold`: staging skeletons from the exploration cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cache import Dataset
from exmergo_dex_core.cli import main
from exmergo_dex_core.transform.scaffold import (
    ScaffoldError,
    ScaffoldPrerequisiteError,
    SourcesFileError,
    _merge_sources,
    _resolve_dataset,
)


def test_merge_preserves_existing_source_properties():
    original = """version: 2
# User configuration
sources:
  - name: main
    schema: main
    database: raw_db
    tables:
      - name: customers
        identifier: raw_customers
        description: Customer records
        columns:
          - name: id
            tests: [unique]
"""
    merged = _merge_sources(original, {"main": {"events"}})
    assert merged.replace("      - name: events\n", "") == original
    assert (
        yaml.safe_load(merged)["sources"][0]["tables"][1]["identifier"]
        == "raw_customers"
    )


def test_merge_preserves_sources_sharing_a_schema():
    original = """version: 2
sources:
  - name: app
    schema: main
    tables:
      - name: customers
  - name: billing
    schema: main
    tables:
      - name: invoices
"""
    merged = _merge_sources(original, {"main": {"events"}})
    sources = yaml.safe_load(merged)["sources"]
    assert sources[1:] == yaml.safe_load(original)["sources"]
    assert sources[0]["name"] == "main"
    assert original[original.index("  - name: app") :] in merged


def test_merge_existing_table_preserves_order_and_comments():
    original = """version: 2
sources:
  - name: main # keep this
    tables:
      - name: orders
      - name: customers
"""
    assert _merge_sources(original, {"main": {"customers"}}) == original


@pytest.mark.parametrize(
    "original",
    [
        "version: 2\nsources: []\n",
        "version: 2\n",
        "version: 2\nsources:\n  - name: main\n",
        "version: 2\nsources:\n  - name: main\n    tables: []\n",
    ],
)
def test_merge_empty_declarations(original):
    merged = _merge_sources(original, {"main": {"events"}})
    assert yaml.safe_load(merged)["sources"][0]["tables"] == [{"name": "events"}]


def test_merge_refuses_invalid_yaml():
    with pytest.raises(SourcesFileError, match="invalid YAML"):
        _merge_sources("version: 2\nsources: [\n", {"main": {"events"}})


def test_merge_missing_tables_does_not_modify_next_source():
    original = "version: 2\nsources:\n  - name: main\n  - name: other\n    tables: []\n"
    parsed = yaml.safe_load(_merge_sources(original, {"main": {"events"}}))
    assert parsed["sources"] == [
        {"name": "main", "tables": [{"name": "events"}]},
        {"name": "other", "tables": []},
    ]


def test_merge_refuses_alias_mutation():
    original = (
        "version: 2\nsources:\n  - name: main\n    tables: &tables\n"
        "      - name: customers\n  - name: other\n    tables: *tables\n"
    )
    with pytest.raises(SourcesFileError, match="anchors or aliases"):
        _merge_sources(original, {"main": {"events"}})


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _seed_cache(tmp_path: Path, duckdb_file: Path, capsys) -> None:
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "--path", str(duckdb_file), "explore", "map"],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok"


def test_scaffold_builds_staging_skeletons_with_pii_meta(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    # The fixture declares stg_customers in a shared schema.yml; a scaffold
    # beside that would be a second entry dbt refuses (see the test below).
    (dbt_project_dir / "models/staging/schema.yml").unlink()
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold staging",
            "--scaffold",
            "customers",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    paths = set(envelope["data"]["paths"])
    assert {
        "models/staging/_dex_sources.yml",
        "models/staging/stg_customers.sql",
        "models/staging/stg_customers.yml",
        "models/staging/stg_orders.sql",
        "models/staging/stg_orders.yml",
    } <= paths

    by_path = {d["path"]: d for d in envelope["diffs"]}
    orders_sql = by_path["models/staging/stg_orders.sql"]["unified"]
    assert "source('main', 'orders')" in orders_sql
    assert "customer_id" in orders_sql

    customers_yml = by_path["models/staging/stg_customers.yml"]["unified"]
    # The PII flag propagates into column meta as (category, confidence-derived
    # flag), never as an example value.
    assert "contains_pii: true" in customers_yml
    assert "pii_category: email" in customers_yml
    # No value from the warehouse appears in anything the scaffold GENERATED
    # (the fixture's own hand-written model legitimately shows on the removed
    # side of its replacement diff; that is repo content, not cache content).
    generated = [
        line
        for diff in envelope["diffs"]
        for line in diff["unified"].splitlines()
        if line.startswith("+")
    ]
    assert not any("a@example.com" in line for line in generated)

    # Key tests land on the candidate key.
    assert "unique" in customers_yml and "not_null" in customers_yml

    # Still a plan: nothing written into the project.
    assert not (dbt_project_dir / "models/staging/stg_orders.sql").exists()


def test_overridden_column_gets_no_pii_meta():
    """A column a human cleared via pii_overrides carries pii=None (with the
    audit field set), so the scaffold stamps no contains_pii, at either level."""

    from exmergo_dex_core.cache import ColumnProfile, Dataset
    from exmergo_dex_core.transform.scaffold import model_edits

    dataset = Dataset(
        identifier="db.main.region",
        columns=[
            ColumnProfile(name="r_regionkey", data_type="INTEGER", nullable=False),
            ColumnProfile(
                name="r_name",
                data_type="VARCHAR",
                pii=None,
                pii_overridden="name",
            ),
        ],
    )
    yaml_edit = next(e for e in model_edits(dataset) if e.path.endswith(".yml"))
    assert "contains_pii" not in yaml_edit.new_content
    assert "pii_category" not in yaml_edit.new_content


def test_scaffolded_plan_applies_cleanly(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    plan_id = envelope["data"]["plan_id"]
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert (dbt_project_dir / "models/staging/stg_orders.sql").is_file()
    assert (dbt_project_dir / "models/staging/_dex_sources.yml").is_file()


def test_the_pii_stamp_lands_under_config_meta_and_tests_under_data_tests():
    """dbt moved `meta` under `config` and `tests` to `data_tests` (#490).

    The stamp's keys are unchanged, because tools outside dex read them; only
    where they sit moves.
    """

    from exmergo_dex_core.cache import ColumnProfile, Dataset, PIICategory, PIIFlag
    from exmergo_dex_core.transform.scaffold import model_edits

    dataset = Dataset(
        identifier="db.main.customers",
        columns=[
            ColumnProfile(name="id", data_type="INTEGER", nullable=False),
            ColumnProfile(
                name="email",
                data_type="VARCHAR",
                pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.9),
            ),
        ],
        candidate_keys=[["id"]],
    )
    yaml_edit = next(e for e in model_edits(dataset) if e.path.endswith(".yml"))
    entry = yaml.safe_load(yaml_edit.new_content)["models"][0]
    columns = {c["name"]: c for c in entry["columns"]}

    assert "meta" not in entry
    assert entry["config"]["meta"] == {"contains_pii": True}
    assert "meta" not in columns["email"]
    assert columns["email"]["config"]["meta"] == {
        "contains_pii": True,
        "pii_category": "email",
    }
    assert columns["id"]["data_tests"] == ["unique", "not_null"]
    assert not any("tests" in c for c in entry["columns"])


def _plan_schema_edit(
    tmp_path: Path, capsys, path: str, content: str
) -> tuple[int, dict]:
    payload = tmp_path / "schema-edit.json"
    payload.write_text(
        json.dumps(
            {"edits": [{"path": path, "kind": "schema_yml", "content": content}]}
        ),
        encoding="utf-8",
    )
    return _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "edit the scaffolded declaration",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )


def test_a_scaffolded_duckdb_model_takes_config_meta_and_refuses_the_old_form(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """On DuckDB, where dbt raises no deprecation warning that would catch it.

    A human adding `config.meta` to a model dex scaffolded is what dbt's own
    documentation tells them to write, so it has to go through. A top-level
    `meta` beside it is a dbt parse error, so dex refuses it at plan time with the
    fix named instead of letting it reach `dbt parse`.
    """

    (dbt_project_dir / "models/staging/schema.yml").unlink()
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold",
            "--scaffold",
            "customers",
        ],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )
    assert rc == 0, envelope
    path = "models/staging/stg_customers.yml"
    parsed = yaml.safe_load((dbt_project_dir / path).read_text(encoding="utf-8"))
    entry = parsed["models"][0]
    assert "meta" not in entry
    assert entry["config"]["meta"]["contains_pii"] is True

    entry["config"]["meta"]["owner"] = "data-team"
    rc, envelope = _plan_schema_edit(
        tmp_path, capsys, path, yaml.safe_dump(parsed, sort_keys=False)
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"

    entry["meta"] = {"contains_pii": True}
    rc, envelope = _plan_schema_edit(
        tmp_path, capsys, path, yaml.safe_dump(parsed, sort_keys=False)
    )
    assert envelope["status"] == "error"
    assert "both a top-level 'meta' and 'config.meta'" in envelope["errors"][0]
    assert "Move the keys" in envelope["errors"][0]


def test_a_scaffold_never_writes_a_second_entry_for_a_declared_model(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """The fixture declares stg_customers in a shared models/staging/schema.yml.

    A `stg_customers.yml` beside it would be a second entry for one model, which
    dbt refuses (`DuplicatePatchPathError`). The scaffold's YAML is left out and
    the warning says how to add what it carried to the entry that exists.
    """

    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold",
            "--scaffold",
            "customers",
        ],
        capsys,
    )

    assert rc == 0, envelope
    assert "models/staging/stg_customers.yml" not in envelope["data"]["paths"]
    assert "models/staging/stg_customers.sql" in envelope["data"]["paths"]
    assert any(
        "already declared in models/staging/schema.yml" in w and "declarations" in w
        for w in envelope["warnings"]
    )


def test_scaffold_without_cache_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "x",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "explore map" in envelope["errors"][0]
    # The named command is the fix, and `reason` says so (#514).
    assert envelope["reason"] == "prerequisite"


def test_scaffold_sequential_calls_keep_earlier_sources(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """Scaffolding sources one table per call must not drop earlier tables.

    Regression for the shared sources file being reprinted from only the
    current call's tables (#439): a second `--scaffold` call for a different
    table used to overwrite the file rather than add to it.
    """

    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold customers",
            "--scaffold",
            "customers",
        ],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )
    assert rc == 0, envelope

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold orders",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    by_path = {d["path"]: d for d in envelope["diffs"]}
    sources_diff = by_path["models/staging/_dex_sources.yml"]["unified"]
    # The new table is the only addition; the earlier one is untouched context,
    # not removed and re-added.
    assert "      - name: customers" in sources_diff
    assert "+      - name: orders" in sources_diff
    assert "-      - name: customers" not in sources_diff

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )
    assert rc == 0, envelope
    content = (dbt_project_dir / "models/staging/_dex_sources.yml").read_text(
        encoding="utf-8"
    )
    assert "- name: customers" in content
    assert "- name: orders" in content


def test_scaffold_already_declared_table_is_a_no_op_on_sources(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """Re-scaffolding a table the shared sources file already declares must not
    even show up as a reordering: no diff for that file at all."""

    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold customers",
            "--scaffold",
            "customers",
        ],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "apply",
            envelope["data"]["plan_id"],
        ],
        capsys,
    )
    assert rc == 0, envelope

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "scaffold customers again",
            "--scaffold",
            "customers",
        ],
        capsys,
    )
    assert rc == 0, envelope
    paths = {d["path"] for d in envelope["diffs"]}
    assert "models/staging/_dex_sources.yml" not in paths


def test_scaffold_unknown_table_is_a_clean_error(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    _seed_cache(tmp_path, duckdb_file, capsys)
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "plan", "x", "--scaffold", "nope"],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "explore map" in envelope["errors"][0]
    assert envelope["reason"] == "prerequisite"


def test_an_object_the_cache_does_not_hold_names_the_command_that_maps_it():
    """`explore map` is what puts an object in the cache, so a name the cache
    does not hold is a prerequisite refusal, the same family `explore` raises
    for the same condition, and not the caller's input."""

    with pytest.raises(ScaffoldPrerequisiteError, match="explore map"):
        _resolve_dataset([Dataset(identifier="p.a.orders")], "nope")


@pytest.mark.parametrize(
    "name", ["orders", "ORDERS", "raw.orders", "DEX_DB.RAW.ORDERS"]
)
def test_a_scaffold_name_resolves_case_insensitively(name):
    """Snowflake folds an unquoted name to upper case, so `explore map` caches
    `DEX_DB.RAW.ORDERS`; `--scaffold orders` finds it, as `explore profile` does."""

    dataset = Dataset(identifier="DEX_DB.RAW.ORDERS")
    assert _resolve_dataset([dataset], name) is dataset


def test_an_upper_case_relation_scaffolds_a_lower_case_model():
    """The model and its files are `stg_orders`, as a dbt project names a model;
    the source() call keeps the table as the warehouse spells it."""

    from exmergo_dex_core.transform.scaffold import model_edits

    edits = model_edits(Dataset(identifier="DEX_DB.RAW.ORDERS"), "snowflake")
    assert [e.path for e in edits] == [
        "models/staging/stg_orders.sql",
        "models/staging/stg_orders.yml",
    ]
    sql, yml = (e.new_content for e in edits)
    assert "'ORDERS') }}" in sql
    assert "  - name: stg_orders" in yml


def test_tables_differing_only_in_case_collide_on_one_model():
    """`a.ORDERS` and `b.orders` both scaffold to `stg_orders`, so the collision
    refusal names the lower-case model they would share."""

    from exmergo_dex_core.transform.scaffold import _refuse_model_collisions

    datasets = [Dataset(identifier="db.a.ORDERS"), Dataset(identifier="db.b.orders")]
    with pytest.raises(ScaffoldError, match="would both scaffold stg_orders"):
        _refuse_model_collisions(datasets, None, "snowflake")


def test_an_ambiguous_name_is_the_callers_input():
    """Two cached objects share the short name. No `explore` command resolves
    that and no file needs editing; the caller qualifies the name. So the
    refusal is the bare base, `request`, and carries neither other family."""

    datasets = [Dataset(identifier="p.a.orders"), Dataset(identifier="p.b.orders")]
    with pytest.raises(ScaffoldError, match="ambiguous") as caught:
        _resolve_dataset(datasets, "orders")
    assert not isinstance(caught.value, ScaffoldPrerequisiteError)
    assert not isinstance(caught.value, SourcesFileError)


def test_scaffold_into_an_unmergeable_sources_file_is_a_configuration_refusal(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    """A flow-style `_dex_sources.yml` is a file the merge will not rewrite. The
    call's input is fine and no `explore` command repairs the file, so the
    envelope reads `configuration`, not `request` (#514): a host stops and
    shows the message rather than telling the caller to change the request."""

    _seed_cache(tmp_path, duckdb_file, capsys)
    (dbt_project_dir / "models" / "staging" / "_dex_sources.yml").write_text(
        "version: 2\nsources: [{name: main, tables: [{name: customers}]}]\n",
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "x",
            "--scaffold",
            "orders",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "flow-style" in envelope["errors"][0]
    assert envelope["reason"] == "configuration"


def _trino_store(*identifiers: str, connector: str = "trino"):
    from exmergo_dex_core.cache import CacheProvenance, ColumnProfile, DexCache
    from exmergo_dex_core.storage.memory import MemoryStore

    store = MemoryStore()
    store.save_cache(
        DexCache(
            datasets=[
                Dataset(
                    identifier=identifier,
                    columns=[ColumnProfile(name="id", data_type="bigint")],
                )
                for identifier in identifiers
            ],
            provenance=CacheProvenance(connector=connector),
        )
    )
    return store


def test_a_trino_source_names_its_catalog():
    """dbt-trino resolves a source with no `database` in the profile's, which is
    the dev catalog and never a source catalog, so a scaffold without it reads
    the wrong table. Catalogs also share schema names, so the source name
    carries the catalog as well."""

    from exmergo_dex_core.transform.scaffold import scaffold_edits

    edits = scaffold_edits(
        ["memory.app.orders"], _trino_store("memory.app.orders", "lake.app.orders")
    )
    sources = yaml.safe_load(next(e for e in edits if "sources" in e.path).new_content)
    assert sources["sources"] == [
        {
            "name": "memory_app",
            "database": "memory",
            "schema": "app",
            "tables": [{"name": "orders"}],
        }
    ]
    model = next(e for e in edits if e.path.endswith("stg_orders.sql"))
    assert "source('memory_app', 'orders')" in model.new_content


@pytest.mark.parametrize("connector", ["snowflake", "databricks", "bigquery"])
def test_a_source_in_another_database_names_it(connector):
    """#802: the dev target can sit in another database (catalog, project), and
    dbt resolves a source with no `database` in the target's, so the scaffolded
    model read a relation that does not exist. The source keeps its schema name,
    so existing `source()` calls still resolve."""

    from exmergo_dex_core.transform.scaffold import scaffold_edits

    identifier = "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER"
    edits = scaffold_edits([identifier], _trino_store(identifier, connector=connector))
    sources = yaml.safe_load(next(e for e in edits if "sources" in e.path).new_content)
    assert sources["sources"] == [
        {
            "name": "TPCH_SF1",
            "database": "SNOWFLAKE_SAMPLE_DATA",
            "schema": "TPCH_SF1",
            "tables": [{"name": "CUSTOMER"}],
        }
    ]
    model = next(e for e in edits if e.path.endswith("stg_customer.sql"))
    assert "source('TPCH_SF1', 'CUSTOMER')" in model.new_content


@pytest.mark.parametrize("connector", ["duckdb", "postgres", "redshift"])
def test_a_one_database_connection_writes_no_source_database(connector):
    """One connection is one database there, so the target's is the source's."""

    from exmergo_dex_core.transform.scaffold import scaffold_edits

    identifier = "warehouse.main.orders"
    edits = scaffold_edits([identifier], _trino_store(identifier, connector=connector))
    sources = yaml.safe_load(next(e for e in edits if "sources" in e.path).new_content)
    assert sources["sources"] == [
        {"name": "main", "schema": "main", "tables": [{"name": "orders"}]}
    ]


def test_two_relations_sharing_a_table_name_are_refused_in_one_call():
    from exmergo_dex_core.transform.scaffold import scaffold_edits

    store = _trino_store("memory.app.orders", "lake.app.orders")
    with pytest.raises(ScaffoldError, match="both scaffold stg_orders"):
        scaffold_edits(["memory.app.orders", "lake.app.orders"], store)


def test_a_model_reading_another_relation_is_not_repointed(dbt_project_dir: Path):
    """stg_orders already reads one relation; scaffolding a different relation
    with the same table name would silently repoint every model downstream.
    Re-scaffolding the relation it already reads is still an update."""

    from exmergo_dex_core.transform.scaffold import scaffold_edits

    store = _trino_store("memory.app.orders", "lake.app.orders")
    (dbt_project_dir / "models" / "staging" / "stg_orders.sql").write_text(
        "select * from {{ source('memory_app', 'orders') }}\n", encoding="utf-8"
    )
    with pytest.raises(ScaffoldError, match="already reads source"):
        scaffold_edits(["lake.app.orders"], store, dbt_project_dir)
    edits = scaffold_edits(["memory.app.orders"], store, dbt_project_dir)
    assert any(e.path.endswith("stg_orders.sql") for e in edits)
