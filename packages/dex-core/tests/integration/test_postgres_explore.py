"""Live explore against the seeded Postgres: free inventory, the strict
db-load handshake, PII flag-not-surface, relationship inference on the
deliberately undeclared foreign key, and the query firewall. Loads nothing
beyond the confirmed budgets; every scanning statement is capped server-side
by the budget-derived statement_timeout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import assert_ok
from .test_postgres_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture(autouse=True)
def _dsn_env(pg_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_dsn)


def test_inventory_is_free_and_scoped(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "inventory"], capsys
    )
    assert_ok(rc, envelope)
    identifiers = {o["identifier"] for o in envelope["data"]["objects"]}
    assert any(i.endswith("app.customers") for i in identifiers)
    assert all(".app." in i for i in identifiers)
    # Free: no confirmation was required and nothing was billed.
    assert envelope["status"] == "ok"
    assert "spend" not in envelope["data"]


def test_unconfirmed_profile_returns_the_handshake(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"])
    _rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "profile", "app.customers"],
        capsys,
    )
    assert envelope["status"] == "needs_confirmation"
    assert envelope["data"]["estimated_seconds"] > 0
    assert envelope["data"]["estimate_quality"] == "heuristic"
    assert "--confirm --budget" in envelope["data"]["hint"]


def test_over_ceiling_refusal_is_live_and_loads_nothing(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"])
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            "app.events",
            "--confirm",
            "--budget",
            "0.01",
        ],
        capsys,
    )
    assert rc == 1
    assert "exceeds the ceiling" in envelope["errors"][0]


def test_a_host_cap_binds_over_a_generous_budget_live(
    tmp_path: Path, capsys, monkeypatch
):
    """#787: the project's own config declines a cumulative ceiling and the
    command passes a generous --budget; the host's cap still binds, and the
    refusal names it as the only thing that can raise it."""

    seed_repo(tmp_path, schemas=["app"])
    monkeypatch.setenv("DEX_SESSION_CEILING_CAP", "0.01")
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "profile",
            "app.events",
            "--confirm",
            "--budget",
            "600",
        ],
        capsys,
    )
    assert rc == 1
    assert "DEX_SESSION_CEILING_CAP=0.01" in envelope["errors"][0]
    assert envelope["data"]["session_ceiling_cap"]["source"] == "environment"


def test_confirmed_map_profiles_flags_pii_and_infers_the_missing_fk(
    tmp_path: Path, capsys
):
    seed_repo(tmp_path, schemas=["app"], budget=120)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--confirm"], capsys
    )
    assert_ok(rc, envelope)
    data = envelope["data"]
    assert data["profiled_count"] >= 6
    assert data["pii_column_count"] >= 3  # email, names, phone, address
    assert data["spend"]["seconds_billed"] > 0

    cache = json.loads((tmp_path / ".dex" / "cache.json").read_text(encoding="utf-8"))
    # The deliberately undeclared FK is inferred from names and profiles.
    assert any(
        r["from_dataset"].endswith("app.order_items")
        and r["from_columns"] == ["product_id"]
        and r["to_dataset"].endswith("app.products")
        for r in cache["relationships"]
    )
    # PII is flagged with min/max suppressed: no example value in the cache.
    customers = next(
        d for d in cache["datasets"] if d["identifier"].endswith("app.customers")
    )
    email = next(c for c in customers["columns"] if c["name"] == "email")
    assert email["pii"]["category"] == "email"
    assert email["min_value"] is None and email["max_value"] is None
    assert "@example.com" not in json.dumps(cache)


def test_query_firewall_allows_measuring_and_refuses_pii_values(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"], budget=120)
    rc, _ = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--confirm"], capsys
    )
    assert rc == 0

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT status, count(*) AS n FROM app.orders GROUP BY status",
            "--confirm",
            "--budget",
            "30",
        ],
        capsys,
    )
    assert_ok(rc, envelope)
    assert envelope["data"]["row_count"] >= 1

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT email FROM app.customers LIMIT 5",
            "--confirm",
            "--budget",
            "30",
        ],
        capsys,
    )
    assert rc == 1
    assert "PII" in envelope["errors"][0]


def test_query_firewall_unnests_the_seeded_jsonb_column(tmp_path: Path, capsys):
    seed_repo(tmp_path, schemas=["app"], budget=120)
    rc, _ = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--confirm"], capsys
    )
    assert rc == 0

    # The schemaless-exploration probe the firewall exists to admit: every
    # distinct top-level key of a JSON column, with its frequency.
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT k, count(*) AS n FROM app.products, "
            "jsonb_object_keys(attrs) AS k GROUP BY k ORDER BY n DESC",
            "--confirm",
            "--budget",
            "30",
        ],
        capsys,
    )
    assert_ok(rc, envelope)
    keys = {row[0] for row in envelope["data"]["cells"]}
    assert {"weight_g", "in_stock"} <= keys

    # Smuggling a subquery through the unnest stays refused, before execution.
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "query",
            "SELECT k FROM app.products, "
            "jsonb_object_keys((SELECT attrs FROM app.products LIMIT 1)) AS k",
            "--confirm",
            "--budget",
            "30",
        ],
        capsys,
    )
    assert rc == 1
    assert "query refused" in envelope["errors"][0]


# --- scope resolution against the live database (free: one pg_catalog SELECT) ---------


def test_a_bogus_scope_is_refused_for_free(tmp_path: Path, capsys, pg_dsn):
    """Postgres was the worst of the connectors here: the allowlist was echoed
    back without ever asking the server, and the inventory filter then dropped the
    unmatched entry, so a typo simply returned nothing at all."""

    seed_repo(tmp_path, schemas=["__no_such_schema__"])
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "__no_such_schema__" in error
    # The refusal names what does exist, which the silent empty inventory never did.
    assert "app" in error
    assert "[from postgres.schemas in .dex/config.yml]" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_scope_cannot_widen_the_committed_allowlist_live(
    tmp_path: Path, capsys, pg_dsn
):
    seed_repo(tmp_path, schemas=["app"])
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "explore", "map", "--scope", "public"], capsys
    )
    assert rc == 1
    assert "never widens" in envelope["errors"][0]
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


# --- value-shape probes under a nondeterministic collation (#1096) -------------------


def test_shape_probes_run_under_a_nondeterministic_collation(pg_dsn):
    """Postgres refuses a regex and a substring search (``replace``) under a
    nondeterministic collation, and one such column used to fail the profile
    statement for the whole table. The probes read the column as C-collated
    text, so they run, and the cast to ``text`` also stops a ``char(n)``
    column's padding from defeating the patterns' end anchors."""

    psycopg = pytest.importorskip("psycopg")
    from exmergo_dex_core.adapters.base import ColumnMeta
    from exmergo_dex_core.adapters.postgres import PostgresAdapter
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    with psycopg.connect(pg_dsn) as conn:
        database = conn.execute("select current_database()").fetchone()[0]
        conn.execute(
            "create collation pg_temp.dex_ci (provider = icu,"
            " locale = 'und-u-ks-level2', deterministic = false)"
        )
        conn.execute(
            "create temp table dex_ci_shapes ("
            " code text collate pg_temp.dex_ci,"
            " padded char(6) collate pg_temp.dex_ci,"
            " plain_padded char(6),"
            " control text)"
        )
        rows = [
            ("123", "ab", "12", "x"),
            ("550e8400-e29b-41d4-a716-446655440000", "cd", "34", "y"),
            ("deadbeef01", "12", "56", "z"),
            ("ALPHA", None, None, None),
            ("Ada Lovelace", None, None, None),
            (None, None, None, None),
        ]
        with conn.cursor() as cursor:
            cursor.executemany(
                "insert into dex_ci_shapes values (%s, %s, %s, %s)", rows
            )

        adapter = PostgresAdapter(
            connection=conn,
            owns_connection=False,
            cost_gate=CostGate(
                paradigm=Paradigm.DB_LOAD,
                ceiling=60.0,
                session_ceiling=None,
                session_spent=0.0,
                confirmed=True,
                connector="postgres",
                command="explore profile",
            ),
        )
        columns = [
            ColumnMeta(name, data_type, True, ordinal)
            for ordinal, (name, data_type) in enumerate(
                [
                    ("code", "text"),
                    ("padded", "character(6)"),
                    ("plain_padded", "character(6)"),
                    ("control", "text"),
                ],
                start=1,
            )
        ]
        every = {c.name for c in columns}
        sql, plan = adapter._build_aggregate_sql(
            f"{database}.pg_temp.dex_ci_shapes",
            columns,
            safe=every,
            shape=every,
            type_req=every,
            key_shape_req=every,
            temporal_req=set(),
        )
        values, labels = adapter._execute(sql, estimate=1.0)
        record = dict(zip(labels, values[0], strict=True))
        aggregates = {
            a.name: a
            for a in adapter._read_aggregates(
                record, plan, {}, row_basis=None, sampled=False
            )
        }

    code = aggregates["code"]
    assert code.null_fraction == pytest.approx(1 / 6)
    assert code.uuid_string_fraction == pytest.approx(1 / 5)
    assert code.hex_string_fraction == pytest.approx(1 / 5)
    assert code.numeric_string_fraction == pytest.approx(1 / 5)
    # Case-sensitive: only ALPHA is upper vocabulary, only Ada Lovelace a name.
    assert code.upper_vocab_fraction == pytest.approx(1 / 5)
    assert code.person_shape_fraction == pytest.approx(1 / 5)
    assert code.avg_token_count == pytest.approx(6 / 5)
    # char(6) padding no longer hides a two-digit value from `^-?[0-9]+$`.
    assert aggregates["padded"].integer_string_fraction == pytest.approx(1 / 3)
    assert aggregates["plain_padded"].integer_string_fraction == pytest.approx(1.0)
    assert aggregates["control"].integer_string_fraction == pytest.approx(0.0)
