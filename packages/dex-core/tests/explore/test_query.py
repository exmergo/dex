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


def _query(sql: str, db: Path, repo: Path, capsys, *, expect_error: bool = False):
    return _run(
        ["explore", "query", sql, "--path", str(db), "--repo-root", str(repo)],
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
