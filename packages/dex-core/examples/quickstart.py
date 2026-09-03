"""Using exmergo-dex-core as a library, end to end.

    pip install "exmergo-dex-core[duckdb]"
    python quickstart.py

Everything below runs against the same seeded demo warehouse `dex demo` builds,
generated here into a throwaway directory, so it is safe to run anywhere and the
findings it reports are the ones the documentation quotes. Point `DexEngine` at
your own warehouse by changing the connector and config; nothing else in the flow
changes.

Run by `tests/test_packaging.py` against a freshly built wheel, so if this stops
working the suite says so.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from exmergo_dex_core import DexEngine, QueryRefusedError, generate_demo_warehouse


def main() -> None:
    workspace = Path(tempfile.mkdtemp())

    # The same generator behind `dex demo`: a small e-commerce warehouse with
    # deliberately seeded flaws, so this run reports real findings rather than a
    # clean bill of health.
    warehouse = generate_demo_warehouse(workspace / "shop.duckdb")
    print(f"generated {warehouse.row_count} rows across {len(warehouse.tables)} tables")

    # No store passed, so state lives in this process and nothing is written to
    # disk. Pass store=FilesystemStore(repo_root) to keep a .dex/ cache between
    # runs, which is what the CLI does.
    with DexEngine(connector="duckdb", path=str(warehouse.path)) as dex:
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
            print(f"  {dataset.identifier}: {dataset.row_count} rows")

        # 3. Profiling reports what it found wrong, not just what is there: a key
        #    that is not unique, a column whose declared type contradicts its
        #    content, a table that a half-failed load left empty.
        for dataset in mapped.cache.datasets:
            for finding in dataset.data_quality:
                print(f"  finding: {dataset.identifier}: {finding}")

        # 4. PII is detected during profiling and reported as a flag, never as a
        #    value. No email address appears anywhere in the result.
        for dataset in mapped.cache.datasets:
            for column in dataset.columns:
                if column.pii is not None:
                    print(
                        f"  PII: {dataset.identifier}.{column.name} is "
                        f"{column.pii.category.value} "
                        f"(confidence {column.pii.confidence:.2f})"
                    )

        # 5. Ask a question. The query firewall checks it against what profiling
        #    learned, and results come back columnar and row-capped. Ordering is
        #    the query's job, not the engine's: a bare GROUP BY returns groups in
        #    whatever order the aggregate produced them, which varies run to run.
        result = dex.query(
            "select status, count(*) as orders from orders "
            "group by status order by status"
        )
        print(f"query -> {result.columns}: {result.cells}")

        # 6. The same firewall refuses to project a PII column, so a careless
        #    query cannot pull personal data into your program.
        try:
            dex.query("select email from customers")
            print("  unexpected: the PII query was allowed")
        except QueryRefusedError as refusal:
            print(f"  refused, as designed: {refusal}")

        # 7. Profile a single table on demand. Objects already profiled this
        #    session are served from the cache rather than scanned again.
        profiled = dex.profile("order_items")
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
