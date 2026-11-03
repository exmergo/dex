"""Live transform against the seeded Postgres: init renders a secret-free
dbt-postgres profile from the discovered connection, and a confirmed build
lands only in the dedicated dbt_dev schema as the dbt_dev role, with actual
database-seconds recorded to the ledger and the ceiling injected as a
statement_timeout via PGOPTIONS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from .conftest import (
    assert_downstream_run,
    assert_ok,
    assert_unpivot_build,
    downstream_probe_edits,
    unpivot_fixture_edits,
)
from .test_postgres_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def dev_dsn(pg_dsn, pg_dev_password, monkeypatch) -> str:
    """The pg_dsn rewritten to the dbt_dev role (builds need write access to
    the dev schema; dex_ro deliberately has none). Password travels via
    PGPASSWORD, exactly as the rendered profile expects."""

    if not pg_dev_password:
        pytest.skip("set DEX_TEST_PG_DEV_PASSWORD (the dbt_dev role's password)")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    fields = {
        k: v for k, v in conninfo_to_dict(pg_dsn).items() if k not in ("password",)
    }
    fields["user"] = "dbt_dev"
    dsn = make_conninfo(**fields)
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("PGPASSWORD", pg_dev_password)
    return dsn


def test_init_and_build_write_only_the_dev_schema(
    tmp_path: Path, capsys, dev_dsn, pg_dev_password
):
    seed_repo(tmp_path, schemas=["app"], budget=300)
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "init",
            "analytics",
            "--connector",
            "postgres",
        ],
        capsys,
    )
    assert_ok(rc, envelope)

    rendered = (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    profile = yaml.safe_load(rendered)["analytics"]["outputs"]["dev"]
    assert profile["type"] == "postgres"
    assert profile["schema"] == "dbt_dev"
    assert profile["threads"] == 1
    # Never a secret in the rendered profile: the password field is an
    # env_var reference, not a value. (A substring check on the password
    # would false-positive here: the seeded dev password equals the role and
    # schema name, which legitimately appear in the profile.)
    assert profile["password"] == "{{ env_var('PGPASSWORD', '') }}"  # noqa: S105

    (tmp_path / "analytics" / "models" / "staging" / "stg_orders.sql").write_text(
        "SELECT id AS order_id, customer_id, status, total\nFROM app.orders\n",
        encoding="utf-8",
    )

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--confirm",
            "--budget",
            "300",
        ],
        capsys,
    )
    assert_ok(rc, envelope)
    data = envelope["data"]
    assert data["success"] is True
    assert any(n["name"] == "stg_orders" for n in data["nodes"])
    # The build is priced upfront now: a database-seconds estimate from the free
    # EXPLAIN planner preflight is surfaced before the run.
    assert envelope["cost"]["estimate"] is not None
    assert envelope["cost"]["estimate"] > 0
    assert data["spend"]["seconds_billed"] > 0
    assert any("statement_timeout" in w for w in envelope["warnings"])

    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text(encoding="utf-8")
    entry = json.loads(ledger.splitlines()[-1])
    assert entry["connector"] == "postgres"
    assert entry["command"] == "transform build"
    assert entry["billed_seconds"] > 0


def test_unpivot_json_object_macro_builds_live(tmp_path: Path, capsys, dev_dsn):
    seed_repo(tmp_path, schemas=["app"], budget=300)
    root = str(tmp_path)
    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "init",
            "analytics",
            "--connector",
            "postgres",
        ],
        capsys,
    )
    assert_ok(rc, envelope)
    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "macro", "unpivot_json_object"], capsys
    )
    assert_ok(rc, envelope)
    rc, envelope = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert_ok(rc, envelope)

    edits_file = tmp_path / "edits.json"
    edits_file.write_text(
        json.dumps(unpivot_fixture_edits(lambda d: f"'{d}'::jsonb", "null::jsonb")),
        encoding="utf-8",
    )
    rc, planned = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "plan",
            "unpivot fixture",
            "--edits-file",
            str(edits_file),
        ],
        capsys,
    )
    assert rc == 0, planned
    rc, applied = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert rc == 0, applied

    rc, built = run_cli(
        ["--repo-root", root, "transform", "build", "--confirm", "--budget", "300"],
        capsys,
    )
    assert rc == 0, built
    assert_unpivot_build(built)


def test_an_unwritable_dev_schema_is_refused_before_the_cost_gate(
    tmp_path: Path, capsys, dev_dsn
):
    """dbt creates its dev schema, but only if the role may. The seeded dbt_dev
    role holds CREATE on the dbt_dev schema and nothing else, so a dev schema that
    does not exist yet cannot be created: the first build would die on a bare
    permission error naming neither the schema nor the grant. The preflight is
    free (catalog lookups and privilege predicates) and runs before the confirm
    handshake, so this refuses without `--confirm` and without load."""

    pytest.importorskip("dbt.adapters.postgres")
    root = str(tmp_path)
    seed_repo(tmp_path, schemas=["app"], budget=300)
    config_path = tmp_path / ".dex" / "config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["postgres"]["dev_schema"] = "dex_absent_dev_schema"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "init",
            "analytics",
            "--connector",
            "postgres",
        ],
        capsys,
    )
    assert_ok(rc, envelope)

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "build", "--target", "dev"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"
    error = envelope["errors"][0]
    assert "dex_absent_dev_schema" in error
    assert "may not create it" in error
    assert "CREATE SCHEMA IF NOT EXISTS dex_absent_dev_schema AUTHORIZATION" in error
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


def test_mutation_equivalence_labels_survivors_live(
    tmp_path: Path, capsys, dev_dsn, pg_dev_password
):
    """transform test --mutate: Postgres equivalence check labels equivalent
    and distinguishable survivors (including one turning a column to NULL),
    and prices the comparison lines in the compute estimate upfront."""

    seed_repo(tmp_path, budget=300)
    root = str(tmp_path)

    # 1. Initialize dbt project for postgres
    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "init",
            "analytics",
            "--connector",
            "postgres",
        ],
        capsys,
    )
    assert_ok(rc, envelope)

    # 2. Write our test model and schema
    # - "amount > 5" gives a comparison mutant (values are 10 and 200, so
    #   it's EQUIVALENT)
    # - "case when flag then 'positive' end" gives a case_branch mutant that
    #   turns the category column into NULL (so it's DISTINGUISHABLE)
    model = "dex_mutation_probe"
    staging_dir = tmp_path / "analytics" / "models" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    (staging_dir / f"{model}.sql").write_text(
        "SELECT id, amount, CASE WHEN flag THEN 'positive' END AS category\n"
        "FROM (VALUES (1, 10, true), (2, 200, true)) AS t(id, amount, flag)\n"
        "WHERE amount > 5\n",
        encoding="utf-8",
    )
    (staging_dir / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        f"  - name: {model}\n"
        "    columns:\n"
        "      - name: id\n"
        "        data_tests: [not_null, unique]\n",
        encoding="utf-8",
    )

    equivalence_run = [
        "--repo-root",
        root,
        "transform",
        "test",
        "--mutate",
        model,
        "--check-equivalence",
    ]

    # 3. Unconfirmed preflight: verify estimate includes the two comparison lines
    rc, unconfirmed = run_cli(equivalence_run, capsys)
    assert unconfirmed["status"] == "needs_confirmation", unconfirmed
    assert unconfirmed["cost"]["estimate"] is not None
    lines = unconfirmed["data"].get("per_table_seconds") or {}
    assert "(equivalence self-check)" in lines, lines
    assert "(equivalence checks, if every mutant survives)" in lines, lines

    # 4. Confirmed run: verify survivor classifications on Postgres dev data
    rc, labelled = run_cli(
        [*equivalence_run, "--confirm", "--budget", "300"],
        capsys,
    )
    assert rc == 0, labelled
    assert labelled["status"] == "ok"
    data = labelled["data"]
    assert data["equivalence"]["self_check"] == "reproducible", data

    labels = {m["operator"]: m["equivalence"] for m in data["mutants"]}
    assert labels["comparison"]["status"] == "equivalent"
    assert labels["case_branch"]["status"] == "distinguishable"


def test_mutation_downstream_tests_catch_what_the_models_own_miss_live(
    tmp_path: Path, capsys, dev_dsn, pg_dev_password
):
    """transform test --mutate --downstream (#648) on Postgres: opt-in, priced
    into the one estimate, and the mart's count test catches the dropped
    filter the intermediate model's own tests miss. The dev schema holds the
    same relations afterwards: the mart is routed through as ephemeral."""

    import psycopg

    seed_repo(tmp_path, budget=300)
    root = str(tmp_path)
    rc, envelope = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "init",
            "analytics",
            "--connector",
            "postgres",
        ],
        capsys,
    )
    assert_ok(rc, envelope)
    edits_file = tmp_path / "edits.json"
    edits_file.write_text(
        json.dumps(
            downstream_probe_edits(
                "select * from (values (1, 10), (2, 200), (3, 1)) as v(id, amount)"
            )
        ),
        encoding="utf-8",
    )
    rc, planned = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "plan",
            "downstream mutation probe",
            "--edits-file",
            str(edits_file),
        ],
        capsys,
    )
    assert rc == 0, planned
    rc, applied = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert rc == 0, applied

    def relations() -> set:
        with psycopg.connect(dev_dsn, password=pg_dev_password) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "select table_name from information_schema.tables "
                    "where table_schema = 'dbt_dev'"
                ).fetchall()
            }

    before = relations()
    assert_downstream_run(run_cli, capsys, root, 300)
    assert relations() == before


def test_a_cast_key_is_measured_through_a_guard_live(pg_dsn):
    """Postgres has no cast that returns NULL, so a key cast to an integer is
    measured through a guard (#1075). Run as the probes `transform plan` and
    `maintain verify` build: a value that is not an integer, one out of range
    for `int`, and one padded with a Unicode space the integer parser rejects
    each count as failed, and none fails the statement. ASCII whitespace,
    which the parser skips, still converts."""

    import psycopg
    import sqlglot

    from exmergo_dex_core.cache import Relationship, RelationshipKind
    from exmergo_dex_core.explore import relationships as rel_mod
    from exmergo_dex_core.transform import identifier_conversion as ic

    def row(conn, sql: str) -> dict:
        cursor = conn.execute(sql)
        names = [c.name for c in cursor.description]
        return dict(zip(names, cursor.fetchone(), strict=True))

    def plan_probe(conn, relation: str) -> dict:
        authored = sqlglot.parse_one(
            f"select cast(code as int) as code_id from {relation}",  # noqa: S608
            read="postgres",
        )
        built = ic.conversion_probe(authored, 0, "postgres")
        assert built.sql is not None, built
        return row(conn, built.sql)

    em_space, no_break_space = "\u2003", "\u00a0"
    with psycopg.connect(pg_dsn) as conn:
        conn.execute("create temp table dex_codes (code text)")
        values = ["1", " 7 ", "\t7", "x", "99999999999", f"{em_space}7"]
        with conn.cursor() as cursor:
            cursor.executemany(
                "insert into dex_codes values (%s)", [(v,) for v in values]
            )
        conn.execute("create temp table dex_ids (id int)")
        conn.execute("insert into dex_ids values (1), (7)")

        plan = plan_probe(conn, "pg_temp.dex_codes")
        assert plan["input_rows"] == 6
        assert plan["failed_rows"] == 3
        assert plan["failed_entities"] == 3
        assert plan["retained_entities"] == 3

        rel = Relationship(
            from_dataset="pg_temp.dex_codes",
            from_columns=["code"],
            to_dataset="pg_temp.dex_ids",
            to_columns=["id"],
            kind=RelationshipKind.DECLARED,
        )
        exprs = {id(rel): (("TRY_CAST({} AS INT)",), (None,))}
        (statement,) = rel_mod.coverage_statements([rel], "postgres", key_exprs=exprs)
        verify = row(conn, statement)
        assert verify["dex_cov_eligible_0"] == 6
        assert verify["dex_cov_matched_0"] == 3
        assert verify["dex_cov_failed_0"] == 3
        assert verify["dex_cov_failed_keys_0"] == 3

        # An ICU collation counts more characters as space, the no-break
        # space among them; the parser still rejects every one.
        conn.execute('create temp table dex_icu_codes (code text collate "und-x-icu")')
        icu_values = ["7", f"{no_break_space}7", f"7{em_space}"]
        with conn.cursor() as cursor:
            cursor.executemany(
                "insert into dex_icu_codes values (%s)", [(v,) for v in icu_values]
            )
        icu = plan_probe(conn, "pg_temp.dex_icu_codes")
        assert icu["input_rows"] == 3
        assert icu["failed_rows"] == 2

        # Postgres refuses a regex under a nondeterministic collation, so the
        # shape test reads the key as "C" and still runs.
        conn.execute(
            "create collation pg_temp.dex_ci (provider = icu,"
            " locale = 'und-u-ks-level2', deterministic = false)"
        )
        conn.execute(
            "create temp table dex_ci_codes (code text collate pg_temp.dex_ci)"
        )
        conn.execute("insert into dex_ci_codes values ('7'), ('x')")
        ci = plan_probe(conn, "pg_temp.dex_ci_codes")
        assert ci["input_rows"] == 2
        assert ci["failed_rows"] == 1
