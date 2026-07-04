"""Fixtures for the maintain suite: a baseline world plus drift induction.

``maintain_repo`` builds the state maintain measures against: a DuckDB
warehouse (raw tables plus a "built" staging table), a dbt project (staging
model, declared sources, semantic layer), and an ``explore map --verify``
cache. Tests snapshot it, induce drift with writable SQL or project edits, and
re-run detection through the CLI like every other command suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

SEMANTIC_YAML = """\
semantic_models:
  - name: orders
    model: ref('stg_orders')
    defaults:
      agg_time_dimension: ordered_at
    entities:
      - name: order_id
        type: primary
    dimensions:
      - name: status
        type: categorical
      - name: ordered_at
        type: time
        type_params:
          time_granularity: day
    measures:
      - name: order_amount
        agg: sum
        expr: amount
      - name: order_count
        agg: count
        expr: order_id

metrics:
  - name: revenue
    label: Revenue
    type: simple
    type_params:
      measure: order_amount
  - name: order_volume
    label: Order volume
    type: simple
    type_params:
      measure: order_count
"""


class MaintainRepo:
    """Handle for one baseline repo: paths, a CLI runner, and drift inducers."""

    def __init__(self, root: Path, db_path: Path, run):
        self.root = root
        self.db_path = db_path
        self.project_dir = root
        self._run = run

    def dex(self, *argv: str) -> tuple[int, dict]:
        return self._run("--repo-root", str(self.root), *argv)

    def snapshot(self) -> dict:
        rc, payload = self.dex("maintain", "snapshot")
        assert rc == 0 and payload["status"] == "ok", payload
        return payload

    def sql(self, *statements: str) -> None:
        """Mutate the warehouse directly (the fixture owns a writable handle;
        the engine under test still opens it read-only)."""

        import duckdb

        conn = duckdb.connect(str(self.db_path))
        for statement in statements:
            conn.execute(statement)
        conn.close()

    def edit(self, rel_path: str, content: str) -> None:
        target = self.project_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def dex(capsys):
    """Run the CLI and return (rc, parsed envelope), asserting the one-line
    stdout contract on every call."""

    def run(*argv: str) -> tuple[int, dict]:
        rc = main(list(argv))
        out = capsys.readouterr().out
        assert out.count("\n") == 1, "exactly one line on stdout"
        return rc, json.loads(out)

    return run


@pytest.fixture
def maintain_repo(tmp_path: Path, dex) -> MaintainRepo:
    duckdb = pytest.importorskip("duckdb")

    root = tmp_path / "repo"
    root.mkdir()
    db_path = root / "warehouse.duckdb"

    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE customers ("
        "id INTEGER, name VARCHAR, email VARCHAR, created_at DATE)"
    )
    conn.execute(
        "INSERT INTO customers "
        "SELECT i, 'name_' || i, 'user' || i || '@example.com', "
        "DATE '2024-01-01' + i::INTEGER "
        "FROM range(1, 41) t(i)"
    )
    conn.execute(
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "amount DOUBLE, status VARCHAR, ordered_at DATE)"
    )
    conn.execute(
        "INSERT INTO orders "
        "SELECT i, (i % 40) + 1, (i % 50) * 2.5, "
        "(['placed','paid','shipped','delivered','returned'])[(i % 5) + 1], "
        "DATE '2024-02-01' + (i % 60)::INTEGER "
        "FROM range(1, 201) t(i)"
    )
    # The staging model as dbt would have built it, so the semantic layer's
    # ref('stg_orders') resolves to a real warehouse object.
    conn.execute("CREATE TABLE stg_orders AS SELECT * FROM orders")
    conn.close()

    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db_path}\n", encoding="utf-8"
    )

    (root / "models" / "staging").mkdir(parents=True)
    (root / "models" / "marts").mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        "name: maintain_test\n"
        'version: "1.0.0"\n'
        "profile: maintain_test\n"
        'model-paths: ["models"]\n',
        encoding="utf-8",
    )
    (root / "profiles.yml").write_text(
        "maintain_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (root / "models" / "staging" / "_dex_sources.yml").write_text(
        "version: 2\n"
        "\n"
        "sources:\n"
        "  - name: main\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n"
        "        columns:\n"
        "          - name: id\n"
        "          - name: name\n"
        "          - name: email\n"
        "          - name: created_at\n"
        "      - name: orders\n"
        "        columns:\n"
        "          - name: order_id\n"
        "          - name: customer_id\n"
        "          - name: amount\n"
        "          - name: status\n"
        "          - name: ordered_at\n",
        encoding="utf-8",
    )
    (root / "models" / "staging" / "stg_orders.sql").write_text(
        "with source as (\n"
        "    select * from {{ source('main', 'orders') }}\n"
        "),\n\n"
        "renamed as (\n"
        "    select\n"
        "        order_id,\n"
        "        customer_id,\n"
        "        amount,\n"
        "        status,\n"
        "        ordered_at\n"
        "    from source\n"
        ")\n\n"
        "select * from renamed\n",
        encoding="utf-8",
    )
    (root / "models" / "staging" / "stg_orders.yml").write_text(
        "version: 2\n"
        "\n"
        "models:\n"
        "  - name: stg_orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        tests: [unique, not_null]\n"
        "      - name: customer_id\n"
        "        tests: [not_null]\n",
        encoding="utf-8",
    )
    (root / "models" / "marts" / "orders_semantic.yml").write_text(
        SEMANTIC_YAML, encoding="utf-8"
    )

    repo = MaintainRepo(root, db_path, dex)
    rc, payload = repo.dex("explore", "map", "--verify")
    assert rc == 0 and payload["status"] == "ok", payload
    return repo
