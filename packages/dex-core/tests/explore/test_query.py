"""`explore query` end to end: the cache gate, the firewall at the envelope
boundary, result shaping (columnar, capped, truncation-announced), and the
`.dex/queries.jsonl` audit log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, QueryLimits, save_config
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.storage.filesystem import QUERIES_FILE


def _run(argv: list[str], capsys, *, expect_error: bool = False) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    payload = json.loads(out)
    if expect_error:
        assert rc == 1 and payload["status"] == "error", payload
    else:
        assert rc == 0 and payload["status"] == "ok", payload
    return payload


def _mapped_repo(airbnb_duckdb: Path, tmp_path: Path, capsys) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(
        ["explore", "map", "--path", str(airbnb_duckdb), "--repo-root", str(repo)],
        capsys,
    )
    return repo


def _query(
    sql: str | list[str],
    db: Path,
    repo: Path,
    capsys,
    *,
    expect_error: bool = False,
):
    """One statement, or several in one call: the command takes either."""

    statements = [sql] if isinstance(sql, str) else list(sql)
    return _run(
        ["explore", "query", *statements, "--path", str(db), "--repo-root", str(repo)],
        capsys,
        expect_error=expect_error,
    )


def _log_entries(repo: Path) -> list[dict]:
    path = repo / ".dex" / QUERIES_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- the cache gate --------------------------------------------------------------


def test_query_without_cache_is_refused_with_the_fix(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """A statement naming no object has nothing to profile, so the cache gate
    still binds and still writes nothing."""

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _query("SELECT 1", airbnb_duckdb, repo, capsys, expect_error=True)
    assert "explore map" in payload["errors"][0]
    assert not (repo / ".dex").exists(), "a refused gate writes nothing"


def test_no_auto_profile_refuses_a_cold_start_without_touching_anything(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The strict path is the old contract entire: refused, and no connection
    opened to find that out."""

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        [
            "explore",
            "query",
            "SELECT COUNT(*) FROM RAW_LISTINGS",
            "--no-auto-profile",
            "--path",
            str(airbnb_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
        expect_error=True,
    )
    assert "explore map" in payload["errors"][0]
    assert payload["reason"] == "prerequisite"
    assert not (repo / ".dex").exists(), "a refused gate writes nothing"


# --- profiling on demand ---------------------------------------------------------


def test_cold_start_profiles_what_the_query_names(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """No cache at all is not a hard stop: the objects the statement names are
    profiled and the query answers in one call."""

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _query(
        "SELECT COUNT(*) AS n FROM RAW_LISTINGS", airbnb_duckdb, repo, capsys
    )
    assert payload["data"]["cells"] == [[2]]
    assert payload["data"]["profiled_on_demand"] == ["airbnb.main.RAW_LISTINGS"]
    assert any("profiled 1 object(s) on demand" in w for w in payload["warnings"])
    # A real profile, written to the real cache: the next query reuses it.
    again = _query(
        "SELECT COUNT(*) AS n FROM RAW_LISTINGS", airbnb_duckdb, repo, capsys
    )
    assert again["data"]["profiled_on_demand"] == []


def test_a_relation_newer_than_the_cache_is_profiled_not_refused(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The case the field evidence is about: a relation built after the inventory.

    An agent that just ran `dbt run` queries its own new model. It is in neither
    the inventory nor the profiles, so a cache miss is the only thing dex has to
    go on, and concluding "no such table" from that would refuse the majority of
    real ad-hoc probes.
    """

    duckdb = pytest.importorskip("duckdb")
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    conn = duckdb.connect(str(airbnb_duckdb))
    conn.execute("CREATE TABLE stg_listings AS SELECT * FROM RAW_LISTINGS")
    conn.close()

    payload = _query(
        "SELECT COUNT(*) AS n FROM stg_listings", airbnb_duckdb, repo, capsys
    )
    assert payload["data"]["cells"] == [[2]]
    assert payload["data"]["profiled_on_demand"] == ["airbnb.main.stg_listings"]


def test_an_absent_table_is_refused_naming_the_connection(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """A table the warehouse does not have is still refused, and the message no
    longer sends the caller to profile something that is not there."""

    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        "SELECT COUNT(*) FROM nope", airbnb_duckdb, repo, capsys, expect_error=True
    )
    message = payload["errors"][0]
    assert "no object named 'nope' in this connection" in message
    assert "exploration cache" not in message
    assert _log_entries(repo)[-1]["decision"] == "refused"


def test_a_schema_change_under_a_cached_profile_is_reprofiled(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """Flags describing a shape the table has moved away from are not flags for
    that table, so a drifted column signature re-profiles before adjudicating."""

    duckdb = pytest.importorskip("duckdb")
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    conn = duckdb.connect(str(airbnb_duckdb))
    conn.execute("ALTER TABLE RAW_LISTINGS ADD COLUMN reviewer_email VARCHAR")
    conn.close()

    payload = _query(
        "SELECT COUNT(*) AS n FROM RAW_LISTINGS", airbnb_duckdb, repo, capsys
    )
    assert payload["data"]["profiled_on_demand"] == ["airbnb.main.RAW_LISTINGS"]
    # The new column is now known, and its PII flag governs it like any other.
    refused = _query(
        "SELECT reviewer_email FROM RAW_LISTINGS",
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    assert "PII-flagged" in refused["errors"][0]


def test_an_unchanged_cached_profile_is_not_re_profiled(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The signature check is the only staleness test on this path. Age is not:
    a probe must not silently turn into a billed re-scan because a day passed."""

    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    store = FilesystemStore(repo)
    cache = store.load_cache()
    for dataset in cache.datasets:
        dataset.profiled_at = "2020-01-01T00:00:00+00:00"
    store.save_cache(cache)

    payload = _query(
        "SELECT COUNT(*) AS n FROM RAW_LISTINGS", airbnb_duckdb, repo, capsys
    )
    assert payload["data"]["profiled_on_demand"] == []


def test_an_on_demand_profile_still_enforces_pii(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """Profiling on demand is not a way around the policy: the flags it writes
    are the flags a deliberate profile would have written, and they block."""

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _query(
        "SELECT NAME FROM RAW_HOSTS", airbnb_duckdb, repo, capsys, expect_error=True
    )
    message = payload["errors"][0]
    assert "PII-flagged" in message
    # The scan happened and is saved, and the caller is told so rather than
    # paying again for a corrected query.
    assert "profiled before this refusal" in message
    entry = _log_entries(repo)[-1]
    assert entry["decision"] == "refused"
    assert entry["profiled_on_demand"] == ["airbnb.main.RAW_HOSTS"]


# --- a profile-built cache unblocks query -----------------------------------------


def _profiled_repo(
    objects: list[str], airbnb_duckdb: Path, tmp_path: Path, capsys
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(
        [
            "explore",
            "profile",
            *objects,
            "--path",
            str(airbnb_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    return repo


def test_profile_then_query_with_no_prior_map_succeeds(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The scan `profile` paid for must be enough: no `explore map` ever ran."""

    repo = _profiled_repo(["RAW_LISTINGS"], airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        "SELECT HOST_ID, COUNT(*) AS n FROM RAW_LISTINGS GROUP BY 1 ORDER BY 1",
        airbnb_duckdb,
        repo,
        capsys,
    )
    assert payload["data"]["cells"] == [[1, 1], [2, 1]]


def test_query_on_unprofiled_table_is_refused_under_no_auto_profile(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The strict prerequisite, word for word, still reachable behind the flag.

    A partial cache scopes the firewall to exactly the profiled tables, and the
    refusal names the command that widens it. This is the contract a caller opts
    back into when it would rather be refused than have dex spend on its behalf,
    so the wording is asserted as it shipped.
    """

    repo = _profiled_repo(["RAW_LISTINGS"], airbnb_duckdb, tmp_path, capsys)
    payload = _run(
        [
            "explore",
            "query",
            "SELECT COUNT(*) FROM RAW_HOSTS",
            "--no-auto-profile",
            "--path",
            str(airbnb_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
        expect_error=True,
    )
    message = payload["errors"][0]
    assert "not in the exploration cache" in message
    assert "explore profile" in message


def test_profile_built_cache_enforces_pii(airbnb_duckdb: Path, tmp_path: Path, capsys):
    """PII flags must survive the profile -> cache -> firewall path."""

    repo = _profiled_repo(["RAW_HOSTS"], airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        "SELECT MIN(NAME) FROM RAW_HOSTS",
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    message = payload["errors"][0]
    assert "RAW_HOSTS.NAME" in message and "(name)" in message


# --- allowed queries -------------------------------------------------------------


def test_allowed_query_returns_columnar_cells(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        "SELECT HOST_ID, COUNT(*) AS n FROM RAW_LISTINGS GROUP BY 1 ORDER BY 1",
        airbnb_duckdb,
        repo,
        capsys,
    )
    data = payload["data"]
    assert data["columns"] == ["HOST_ID", "n"]
    assert data["cells"] == [[1, 1], [2, 1]]
    assert data["row_count"] == 2
    assert data["truncated"] is False
    assert len(data["tables"]) == 1 and data["tables"][0].endswith(".RAW_LISTINGS")
    # Columnar means lists of lists, never lists of dicts: the sanitizer's
    # raw-row backstop stays intact, and this envelope passes it.
    assert all(isinstance(row, list) for row in data["cells"])
    env.sanitize(env.ok(data))


def test_measuring_aggregates_over_pii_are_allowed(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        "SELECT COUNT(DISTINCT REVIEWER_NAME) AS reviewers, "
        "AVG(LENGTH(COMMENTS)) AS avg_len FROM RAW_REVIEWS",
        airbnb_duckdb,
        repo,
        capsys,
    )
    assert payload["data"]["row_count"] == 1
    reviewers, avg_len = payload["data"]["cells"][0]
    assert reviewers == 2
    assert avg_len > 0


# --- refusals at the boundary ------------------------------------------------------


def test_pii_carrying_query_is_refused_and_logged(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        "SELECT MIN(NAME) FROM RAW_HOSTS",
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    message = payload["errors"][0]
    assert "query refused" in message
    assert "RAW_HOSTS.NAME" in message and "(name)" in message

    entries = _log_entries(repo)
    assert entries[-1]["decision"] == "refused"
    assert "NAME" in entries[-1]["reason"]


def _unreachable_warehouse(monkeypatch) -> None:
    """Every attempt to open a connection fails, the way an uninstalled connector
    extra or an absent credential fails. Applied AFTER the cache exists, because
    building one legitimately needs the warehouse this then takes away."""
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.errors import ConnectorError

    def _refuse(self, *args, **kwargs):
        raise ConnectorError("the connector extra is not installed")

    monkeypatch.setattr(DexEngine, "_adapter", _refuse)


def test_a_pii_refusal_needs_no_connection(
    airbnb_duckdb: Path, tmp_path: Path, capsys, monkeypatch
):
    """The firewall decides from cached flags, so it must decide with the
    warehouse unreachable.

    Regression: the object-gap probe added in #269 opened a connection before
    this guard ran, which turned a policy refusal into a connectivity error and
    closed the firewall wherever no connector is installed -- CI, an offline
    box, any caller holding only a cache. `_object_gap` already treats an
    unreadable column signature as settling nothing; an absent connection is the
    same kind of doubt and now falls through the same way.
    """
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    _unreachable_warehouse(monkeypatch)

    payload = _query(
        "SELECT MIN(NAME) FROM RAW_HOSTS",
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )

    message = payload["errors"][0]
    assert "query refused" in message, (
        "the PII refusal did not survive an unreachable warehouse; the guard is "
        f"being gated on a connection it does not need. Got: {message!r}"
    )
    assert "RAW_HOSTS.NAME" in message and "(name)" in message
    assert _log_entries(repo)[-1]["decision"] == "refused"


def test_a_query_that_passes_the_guard_still_reports_an_unreachable_warehouse(
    airbnb_duckdb: Path, tmp_path: Path, capsys, monkeypatch
):
    """The control for the test above, and the reason it is not a swallowed error.

    Tolerating a failed open would be worth nothing if it hid the failure from a
    caller about to run SQL. It does not: only a refusal returns without a
    connection, and anything still live reaches the opener again and raises there.
    """
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    _unreachable_warehouse(monkeypatch)

    payload = _query(
        "SELECT COUNT(*) FROM RAW_HOSTS",
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )

    assert "connector extra is not installed" in payload["errors"][0]


def test_write_and_pragma_are_refused(airbnb_duckdb: Path, tmp_path: Path, capsys):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    for sql in ("INSERT INTO RAW_HOSTS VALUES (9, 'x')", "PRAGMA database_list"):
        payload = _query(sql, airbnb_duckdb, repo, capsys, expect_error=True)
        assert "query refused" in payload["errors"][0]


# --- shaping and caps ---------------------------------------------------------------


def test_engine_row_cap_truncates_and_says_so(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    save_config(DexConfig(query=QueryLimits(max_rows=1)), repo)
    payload = _query(
        "SELECT ID FROM RAW_REVIEWS ORDER BY ID",
        airbnb_duckdb,
        repo,
        capsys,
    )
    data = payload["data"]
    assert data["row_count"] == 1
    assert data["truncated"] is True
    assert any("truncated to 1 rows" in n for n in data["notes"])


def test_agents_own_limit_is_not_reported_as_truncation(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        "SELECT ID FROM RAW_REVIEWS ORDER BY ID LIMIT 1",
        airbnb_duckdb,
        repo,
        capsys,
    )
    assert payload["data"]["row_count"] == 1
    assert payload["data"]["truncated"] is False
    assert payload["data"]["notes"] == []


def test_cell_and_payload_caps_apply_with_notes(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    save_config(
        DexConfig(query=QueryLimits(max_cell_chars=4, max_payload_bytes=40)), repo
    )
    payload = _query(
        "SELECT 'abcdefghij' AS s FROM RAW_REVIEWS",
        airbnb_duckdb,
        repo,
        capsys,
    )
    data = payload["data"]
    assert all(cell == "abcd..." for (cell,) in data["cells"])
    assert any("truncated to 4 chars" in n for n in data["notes"])
    assert len(json.dumps(data["cells"])) <= 40


# --- the audit log -------------------------------------------------------------------


def test_allowed_queries_are_logged_with_tables_and_counts(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    _query("SELECT COUNT(*) AS n FROM RAW_HOSTS", airbnb_duckdb, repo, capsys)
    entry = _log_entries(repo)[-1]
    assert entry["decision"] == "allowed"
    assert entry["row_count"] == 1
    assert entry["truncated"] is False
    assert entry["tables"] and entry["tables"][0].endswith(".RAW_HOSTS")
    assert "LIMIT" in entry["sql"], "the log records the rewritten SQL"


def test_log_never_contains_result_values(airbnb_duckdb: Path, tmp_path: Path, capsys):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    _query("SELECT ID FROM RAW_LISTINGS ORDER BY ID", airbnb_duckdb, repo, capsys)
    for entry in _log_entries(repo):
        assert set(entry) <= {
            "at",
            "sql",
            "decision",
            "reason",
            "tables",
            "row_count",
            "truncated",
            "pii_warnings",
            "batch_index",
            "batch_size",
        }


# --- sub-threshold flags at the envelope boundary ---------------------------------


def test_sub_threshold_projection_runs_with_warning_and_audit(
    tpch_names_duckdb: Path, tmp_path: Path, capsys
):
    """Issue 54 end to end: after profiling, the region labels are projectable,
    the envelope warns, and the audit log records the sub-threshold projection."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _run(
        [
            "explore",
            "profile",
            "region",
            "hosts",
            "--path",
            str(tpch_names_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    payload = _query(
        "SELECT R_NAME FROM region ORDER BY R_NAME",
        tpch_names_duckdb,
        repo,
        capsys,
    )
    values = [row[0] for row in payload["data"]["cells"]]
    assert values == ["AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST"]
    assert any("region.R_NAME" in w for w in payload["warnings"])

    (allowed,) = [e for e in _log_entries(repo) if e["decision"] == "allowed"]
    assert any("region.R_NAME" in w for w in allowed["pii_warnings"])

    # The person-name table profiled alongside it still refuses.
    refusal = _query(
        "SELECT name FROM hosts", tpch_names_duckdb, repo, capsys, expect_error=True
    )
    assert "hosts.name" in refusal["errors"][0]


def test_override_unblocks_at_query_time_without_reprofiling(
    tpch_names_duckdb: Path, tmp_path: Path, capsys
):
    """An override added after profiling works immediately: demanding a billed
    re-profile before a reviewed column unblocks would tax the review."""

    from exmergo_dex_core.config import PIIOverride

    repo = tmp_path / "repo"
    repo.mkdir()
    _run(
        [
            "explore",
            "profile",
            "hosts",
            "--path",
            str(tpch_names_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    _query("SELECT name FROM hosts", tpch_names_duckdb, repo, capsys, expect_error=True)
    save_config(
        DexConfig(pii_overrides=[PIIOverride(column="tpch_names.main.hosts.name")]),
        repo,
    )
    payload = _query(
        "SELECT name FROM hosts ORDER BY id LIMIT 1",
        tpch_names_duckdb,
        repo,
        capsys,
    )
    assert payload["data"]["cells"] == [["Ada Lovelace"]]
    assert payload["warnings"] == [], "an overridden column is clear, not weak"


def test_query_log_helper_appends(tmp_path: Path):
    store = FilesystemStore(tmp_path)
    store.append_query_log({"at": "t1", "sql": "SELECT 1", "decision": "allowed"})
    store.append_query_log({"at": "t2", "sql": "SELECT 2", "decision": "refused"})
    lines = (tmp_path / ".dex" / QUERIES_FILE).read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["decision"] == "refused"


def test_auto_profile_can_be_turned_off_durably_in_config(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """A repo that wants the strict prerequisite sets it once, rather than every
    caller remembering the flag. Config turns it off; nothing turns it back on."""

    repo = _profiled_repo(["RAW_LISTINGS"], airbnb_duckdb, tmp_path, capsys)
    save_config(DexConfig(connector="duckdb", auto_profile=False), repo)
    payload = _query(
        "SELECT COUNT(*) FROM RAW_HOSTS", airbnb_duckdb, repo, capsys, expect_error=True
    )
    assert "not in the exploration cache" in payload["errors"][0]


# --- several statements in one call -----------------------------------------------


def test_two_statements_return_two_results(airbnb_duckdb: Path, tmp_path: Path, capsys):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        [
            "SELECT COUNT(*) AS n FROM RAW_HOSTS",
            "SELECT COUNT(*) AS n FROM RAW_LISTINGS",
        ],
        airbnb_duckdb,
        repo,
        capsys,
    )
    data = payload["data"]
    assert data["statement_count"] == 2 and data["ok_count"] == 2
    assert [r["index"] for r in data["results"]] == [0, 1]
    assert [r["cells"] for r in data["results"]] == [[[3]], [[2]]]
    assert all(r["status"] == "ok" for r in data["results"])
    # Columnar all the way down: the sanitizer's raw-row rule has to survive the
    # extra nesting a batch introduces.
    env.sanitize(env.ok(data))


def test_a_single_statement_keeps_the_envelope_it_always_had(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The batch shape is additive. One statement is not wrapped in a list of one,
    because every caller written against the old envelope still reads this one."""

    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query("SELECT COUNT(*) AS n FROM RAW_HOSTS", airbnb_duckdb, repo, capsys)
    assert set(payload["data"]) == {
        "columns",
        "types",
        "cells",
        "row_count",
        "truncated",
        "tables",
        "profiled_on_demand",
        "notes",
    }
    assert payload["data"]["cells"] == [[3]]


def test_a_refusal_leaves_its_neighbours_intact_and_is_reported_against_it(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The whole point of per-statement status: paying for two answers and losing
    both because a third was refused is what N separate calls already avoided."""

    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        [
            "SELECT COUNT(*) AS n FROM RAW_HOSTS",
            "SELECT NAME FROM RAW_HOSTS",
            "SELECT COUNT(*) AS n FROM RAW_LISTINGS",
        ],
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    results = payload["data"]["results"]
    assert [r["status"] for r in results] == ["ok", "refused", "ok"]
    assert [r["cells"] for r in results] == [[[3]], [], [[2]]]
    assert payload["data"]["ok_count"] == 2 and payload["data"]["failed_count"] == 1
    assert payload["reason"] == "guard"
    assert payload["errors"] == [f"statement 2 refused: {results[1]['error']}"], (
        "the refusal names the statement that caused it"
    )
    assert "PII-flagged" in results[1]["error"]


def test_every_statement_of_a_batch_is_ledgered_with_its_place_in_it(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    _query(
        ["SELECT COUNT(*) FROM RAW_HOSTS", "SELECT NAME FROM RAW_HOSTS"],
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    entries = _log_entries(repo)[-2:]
    assert {e["batch_size"] for e in entries} == {2}
    # One call, one authorization event: the shared timestamp is what groups them,
    # and batch_index is what orders them. Each decision is written when it is
    # made rather than held until the call ends, so the guard's refusal of the
    # second statement lands ahead of the first statement's run. An audit trail
    # that records a refusal late is one that loses it if the process dies.
    assert len({e["at"] for e in entries}) == 1
    by_index = {e["batch_index"]: e["decision"] for e in entries}
    assert by_index == {0: "allowed", 1: "refused"}


def test_a_lone_statement_carries_no_batch_marks(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    _query("SELECT COUNT(*) FROM RAW_HOSTS", airbnb_duckdb, repo, capsys)
    assert "batch_index" not in _log_entries(repo)[-1]


def test_a_semicolon_joined_argument_is_still_refused_beside_a_clean_one(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """Batching is about call count, not about relaxing the gate. Arguments are
    never joined, so smuggling a second statement into one of them changes
    nothing."""

    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        ["SELECT 1; DROP TABLE RAW_HOSTS", "SELECT COUNT(*) AS n FROM RAW_HOSTS"],
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    results = payload["data"]["results"]
    assert [r["status"] for r in results] == ["refused", "ok"]
    assert "expected exactly one statement" in results[0]["error"]


def test_an_absent_table_refuses_only_the_statement_that_named_it(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query(
        [
            "SELECT COUNT(*) AS n FROM RAW_HOSTS",
            "SELECT COUNT(*) AS n FROM NOT_A_TABLE",
        ],
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    results = payload["data"]["results"]
    assert [r["status"] for r in results] == ["ok", "refused"]
    assert results[0]["cells"] == [[3]]


def test_a_cold_batch_profiles_a_shared_table_once(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """Two questions about the same cold table are one scan, which is the saving
    a caller cannot get by issuing the two calls separately."""

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _query(
        [
            "SELECT COUNT(*) AS n FROM RAW_HOSTS",
            "SELECT COUNT(DISTINCT ID) AS n FROM RAW_HOSTS",
        ],
        airbnb_duckdb,
        repo,
        capsys,
    )
    assert payload["data"]["profiled_on_demand"] == ["airbnb.main.RAW_HOSTS"]
    assert any("profiled 1 object(s) on demand" in w for w in payload["warnings"])
    assert [r["cells"] for r in payload["data"]["results"]] == [[[3]], [[2]]]


def test_the_payload_budget_is_the_calls_not_each_statements(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The byte cap protects agent context, so N statements must not be allowed to
    emit N times what one is. The budget is spent in order and announced when it
    runs out."""

    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    save_config(
        DexConfig(connector="duckdb", query=QueryLimits(max_payload_bytes=12)), repo
    )
    payload = _query(
        [
            "SELECT ID FROM RAW_REVIEWS ORDER BY ID",
            "SELECT ID FROM RAW_LISTINGS ORDER BY ID",
        ],
        airbnb_duckdb,
        repo,
        capsys,
    )
    first, second = payload["data"]["results"]
    assert first["row_count"] > 0, "the first statement draws on a full budget"
    assert second["row_count"] == 0 and second["truncated"] is True
    assert any("payload budget" in n for n in second["notes"])


def test_a_refused_statements_cold_table_is_not_scanned_for_it(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """Profiling is a spend, so it follows the statements that can still run.

    The first statement reads a cold table and an absent one, so it is refused
    whatever happens next. Scanning its cold table anyway would bill a metered
    caller for a profile that no surviving statement will ever read.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _query(
        [
            "SELECT (SELECT COUNT(*) FROM RAW_HOSTS) "
            "+ (SELECT COUNT(*) FROM nope) AS n",
            "SELECT COUNT(*) AS n FROM RAW_LISTINGS",
        ],
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    results = payload["data"]["results"]
    assert [r["status"] for r in results] == ["refused", "ok"]
    assert payload["data"]["profiled_on_demand"] == ["airbnb.main.RAW_LISTINGS"]


def test_a_batch_larger_than_the_configured_limit_is_refused(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    save_config(
        DexConfig(connector="duckdb", query=QueryLimits(max_statements=2)), repo
    )
    payload = _query(
        ["SELECT 1 AS a", "SELECT 2 AS a", "SELECT 3 AS a"],
        airbnb_duckdb,
        repo,
        capsys,
        expect_error=True,
    )
    assert payload["reason"] == "request"
    assert "query.max_statements limit of 2" in payload["errors"][0]


def test_naming_no_statement_at_all_is_a_request_error(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _query([], airbnb_duckdb, repo, capsys, expect_error=True)
    assert payload["reason"] == "request"
    assert "at least one statement" in payload["errors"][0]


# --- --sql-file -------------------------------------------------------------------


def _sql_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "probes.sql"
    path.write_text(text, encoding="utf-8")
    return path


def _file_query(
    path: Path, db: Path, repo: Path, capsys, *, expect_error: bool = False
):
    return _run(
        [
            "explore",
            "query",
            "--sql-file",
            str(path),
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
        expect_error=expect_error,
    )


def test_a_semicolon_separated_file_runs_every_statement(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    path = _sql_file(
        tmp_path,
        "SELECT COUNT(*) AS n FROM RAW_HOSTS;\n"
        "SELECT COUNT(*) AS n\nFROM RAW_LISTINGS;\n",
    )
    payload = _file_query(path, airbnb_duckdb, repo, capsys)
    results = payload["data"]["results"]
    assert [r["cells"] for r in results] == [[[3]], [[2]]]
    assert [r["line"] for r in results] == [1, 2], "each result locates its source"


def test_a_line_per_statement_file_runs_every_statement(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    path = _sql_file(
        tmp_path,
        "-- two probes\nSELECT COUNT(*) AS n FROM RAW_HOSTS\n"
        "SELECT COUNT(*) AS n FROM RAW_LISTINGS\n",
    )
    payload = _file_query(path, airbnb_duckdb, repo, capsys)
    assert [r["line"] for r in payload["data"]["results"]] == [2, 3]


def test_a_file_that_cannot_be_split_is_refused_naming_the_line(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    path = _sql_file(tmp_path, "SELECT COUNT(*) AS n\nFROM RAW_HOSTS\n")
    payload = _file_query(path, airbnb_duckdb, repo, capsys, expect_error=True)
    assert payload["reason"] == "request"
    assert "line 2" in payload["errors"][0]


def test_an_unreadable_file_names_the_path(airbnb_duckdb: Path, tmp_path: Path, capsys):
    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    payload = _file_query(
        tmp_path / "nowhere.sql", airbnb_duckdb, repo, capsys, expect_error=True
    )
    assert payload["reason"] == "request"
    assert "could not read --sql-file" in payload["errors"][0]


def test_arguments_and_a_file_together_are_refused(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """Two ordered sources with no defined order between them. Refusing keeps the
    statement numbering a refusal reports against unambiguous."""

    repo = _mapped_repo(airbnb_duckdb, tmp_path, capsys)
    path = _sql_file(tmp_path, "SELECT 1 AS a;\n")
    payload = _run(
        [
            "explore",
            "query",
            "SELECT 2 AS a",
            "--sql-file",
            str(path),
            "--path",
            str(airbnb_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
        expect_error=True,
    )
    assert payload["reason"] == "request"
    assert "not both" in payload["errors"][0]
