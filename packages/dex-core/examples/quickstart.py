"""Using exmergo-dex-core as a library, end to end.

    pip install "exmergo-dex-core[duckdb]"
    python quickstart.py

Everything below runs against a throwaway DuckDB file this script creates, so it
is safe to run anywhere. Point `Engine` at your own warehouse by changing the
connector and config; nothing else in the flow changes.

Run by `tests/test_packaging.py` against a freshly built wheel, so if this stops
working the suite says so.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb

from exmergo_dex_core import Engine, QueryRefusedError


def make_warehouse(directory: Path) -> Path:
    """A two-table shop: customers with an email column, and their orders."""

    path = directory / "shop.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("""
        CREATE TABLE customers AS SELECT * FROM (VALUES
            (1, 'ada@example.com', 'EU'),
            (2, 'grace@example.com', 'US'),
            (3, 'alan@example.com', 'EU')
        ) AS t(id, email, region)
    """)
    conn.execute("""
        CREATE TABLE orders AS SELECT * FROM (VALUES
            (1, 1, 10.50), (2, 1, 20.00), (3, 2, 5.25), (4, 3, 7.00)
        ) AS t(id, customer_id, total)
    """)
    conn.close()
    return path


def main() -> None:
    workspace = Path(tempfile.mkdtemp())
    warehouse = make_warehouse(workspace)

    # No store passed, so state lives in this process and nothing is written to
    # disk. Pass store=FilesystemStore(repo_root) to keep a .dex/ cache between
    # runs, which is what the CLI does.
    with Engine(connector="duckdb", path=str(warehouse)) as dex:
        # 1. Map the warehouse: inventory, profiling, and join inference in one
        #    pass. On a big warehouse this profiles the top-ranked tables only.
        mapped = dex.map()
        print(
            f"mapped {mapped.object_count} objects, "
            f"{mapped.relationship_count} relationship(s)"
        )
        for note in mapped.notes:
            print(f"  note: {note}")

        # 2. Results carry domain objects, not JSON. `cache` is the composed
        #    DexCache this run wrote.
        for dataset in mapped.cache.datasets:
            columns = ", ".join(c.name for c in dataset.columns)
            print(f"  {dataset.identifier}: {dataset.row_count} rows ({columns})")

        for rel in mapped.cache.relationships:
            print(
                f"  join: {rel.from_dataset}.{rel.from_columns[0]} -> "
                f"{rel.to_dataset}.{rel.to_columns[0]}"
            )

        # 3. PII is detected during profiling and reported as a flag, never as a
        #    value. No email address appears anywhere in the result.
        for dataset in mapped.cache.datasets:
            for column in dataset.columns:
                if column.pii is not None:
                    print(
                        f"  PII: {dataset.identifier}.{column.name} is "
                        f"{column.pii.category.value} "
                        f"(confidence {column.pii.confidence:.2f})"
                    )

        # 4. Ask a question. The query firewall checks it against what profiling
        #    learned, and results come back columnar and row-capped.
        result = dex.query(
            "select region, count(*) as customers from customers group by region"
        )
        print(f"query -> {result.columns}: {result.cells}")

        # 5. The same firewall refuses to project a PII column, so a careless
        #    query cannot pull personal data into your program.
        try:
            dex.query("select email from customers")
            print("  unexpected: the PII query was allowed")
        except QueryRefusedError as refusal:
            print(f"  refused, as designed: {refusal}")

        # 6. Profile a single table on demand. Objects already profiled this
        #    session are served from the cache rather than scanned again.
        profiled = dex.profile("orders")
        print(
            f"profiled {profiled.profiled_count}, "
            f"reused {profiled.cache_hit_count} from cache"
        )

    # The engine is closed by the `with` block, and the working directory is
    # exactly as we left it: no .dex/ directory, no files but the warehouse.
    leftovers = sorted(p.name for p in workspace.iterdir())
    print(f"files in the workspace: {leftovers}")


if __name__ == "__main__":
    main()
