"""PII detection and profile-level interpretation (grain and data-quality notes).

The fixtures mirror the shapes that produced false negatives in the field: an
Airbnb-style raw export with bare `NAME`, `REVIEWER_NAME`, and free-text
`COMMENTS` columns, and a non-unique `ID` on an un-deduplicated snapshot feed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    DexStore,
    PIICategory,
    Relationship,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.explore.profile import detect_pii, is_min_max_safe, profile
from exmergo_dex_core.progress import PROGRESS_FIRST_AFTER, ProgressReporter


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


# --- detect_pii: name patterns ------------------------------------------------


@pytest.mark.parametrize(
    ("column", "data_type", "category"),
    [
        # Exact person tokens, any type, unchanged behavior.
        ("first_name", "VARCHAR", PIICategory.NAME),
        ("surname", "VARCHAR", PIICategory.NAME),
        ("dob", "DATE", PIICategory.DOB),
        ("email", "VARCHAR", PIICategory.EMAIL),
        # camelCase matches the same patterns as snake_case.
        ("firstName", "VARCHAR", PIICategory.NAME),
        ("emailAddress", "VARCHAR", PIICategory.EMAIL),
        # Generic name columns: the field false negatives.
        ("NAME", "VARCHAR", PIICategory.NAME),
        ("REVIEWER_NAME", "VARCHAR", PIICategory.NAME),
        ("host_name", "VARCHAR", PIICategory.NAME),
        # Free-text fields reliably carry PII in their values.
        ("COMMENTS", "VARCHAR", PIICategory.FREE_TEXT),
        ("comment", "TEXT", PIICategory.FREE_TEXT),
        ("notes", "VARCHAR", PIICategory.FREE_TEXT),
        ("review_text", "VARCHAR", PIICategory.FREE_TEXT),
        ("feedback", "STRING", PIICategory.FREE_TEXT),
        # Must-not-regress: genuinely sensitive numeric/temporal columns whose
        # category legitimately lives off strings keep flagging (the type gate
        # is per-category impossibility, not blanket).
        ("zip", "INT64", PIICategory.ADDRESS),
        ("ssn", "INT64", PIICategory.GOVERNMENT_ID),
        ("salary", "NUMERIC", PIICategory.FINANCIAL),
        ("latitude", "FLOAT64", PIICategory.LOCATION),
        ("dob", "DATE", PIICategory.DOB),
        ("phone", "INT64", PIICategory.PHONE),
    ],
)
def test_detect_pii_flags(column: str, data_type: str, category: PIICategory):
    flag = detect_pii(column, data_type)
    assert flag is not None, column
    assert flag.category == category
    assert 0 < flag.confidence <= 0.95


@pytest.mark.parametrize(
    ("column", "data_type"),
    [
        # Technical/organizational qualifiers are not person names.
        ("table_name", "VARCHAR"),
        ("column_name", "VARCHAR"),
        ("file_name", "VARCHAR"),
        ("product_name", "VARCHAR"),
        ("model_name", "VARCHAR"),
        # The weak patterns are string-only: a numeric `comments` is a count.
        ("comments", "INTEGER"),
        ("name", "INTEGER"),
        # Exact-token categories that cannot hold a value off a string: an
        # integer email/name count is the PII-safe derived replacement.
        ("user_email_count", "INT64"),
        ("email_count", "INTEGER"),
        ("contact_email_flag", "BOOL"),
        ("phone_verified", "BOOLEAN"),
        # Aggregate-suffixed numeric columns are derived statistics even where
        # the category (GOVERNMENT_ID, ADDRESS) still permits a numeric value.
        ("ssn_count", "BIGINT"),
        ("zip_count", "INT64"),
        # Substrings without a word boundary do not over-trigger.
        ("username_hash", "VARCHAR"),
        ("emailable", "BOOLEAN"),
        ("filename", "VARCHAR"),
        ("total", "DECIMAL(10,2)"),
    ],
)
def test_detect_pii_does_not_flag(column: str, data_type: str):
    assert detect_pii(column, data_type) is None, column


def test_generic_name_is_weaker_than_exact_person_tokens():
    exact = detect_pii("last_name", "VARCHAR")
    generic = detect_pii("reviewer_name", "VARCHAR")
    free_text = detect_pii("comments", "VARCHAR")
    assert exact.confidence > generic.confidence > free_text.confidence


def test_new_flags_suppress_min_max():
    """Broader detection must tighten the envelope: every newly flagged column
    loses its min/max, same as the exact-token categories always did."""

    for column in ("NAME", "REVIEWER_NAME", "COMMENTS"):
        flag = detect_pii(column, "VARCHAR")
        assert flag is not None
        assert not is_min_max_safe("VARCHAR", flag)


def test_derated_count_column_restores_min_max():
    """The fix must restore min/max, not merely unblock the firewall: an integer
    `_email_count` is unflagged, so its extreme is a plain non-sensitive number."""

    assert detect_pii("user_email_count", "INT64") is None
    assert is_min_max_safe("INT64", detect_pii("user_email_count", "INT64")) is True


def test_snowflake_fixed_type_reads_as_numeric():
    """Snowflake surfaces every integer/NUMBER as FIXED; without this the
    type-aware gates and min/max safety are inert on Snowflake."""

    from exmergo_dex_core.explore.profile import is_numeric_type

    assert is_numeric_type("FIXED") is True


# --- envelope: the Airbnb-shaped session --------------------------------------


def test_airbnb_pii_columns_are_flagged_with_min_max_suppressed(
    airbnb_duckdb: Path, capsys
):
    payload = _run(
        [
            "explore",
            "profile",
            "RAW_HOSTS",
            "RAW_REVIEWS",
            "--path",
            str(airbnb_duckdb),
        ],
        capsys,
    )
    ds = {d["identifier"].split(".")[-1]: d for d in payload["data"]["datasets"]}
    hosts = {c["name"]: c for c in ds["RAW_HOSTS"]["columns"]}
    reviews = {c["name"]: c for c in ds["RAW_REVIEWS"]["columns"]}

    assert hosts["NAME"]["pii"]["category"] == "name"
    assert reviews["REVIEWER_NAME"]["pii"]["category"] == "name"
    assert reviews["COMMENTS"]["pii"]["category"] == "free_text"
    for col in (hosts["NAME"], reviews["REVIEWER_NAME"], reviews["COMMENTS"]):
        assert col["min_value"] is None and col["max_value"] is None
        assert set(col["pii"]) == {"category", "confidence"}  # never a value


def test_tpch_reference_names_derate_below_threshold_and_person_names_hold(
    tpch_names_duckdb: Path, capsys
):
    """Issue 54's exact shapes: the flag is never removed, but value-shape
    evidence de-rates reference vocabularies (R_NAME, N_NAME) and long labels
    (P_NAME) below the firewall threshold, while full person names corroborate
    up to the exact-token confidence."""

    payload = _run(
        [
            "explore",
            "profile",
            "region",
            "nation",
            "part",
            "hosts",
            "--path",
            str(tpch_names_duckdb),
        ],
        capsys,
    )
    ds = {d["identifier"].split(".")[-1]: d for d in payload["data"]["datasets"]}
    r_name = {c["name"]: c for c in ds["region"]["columns"]}["R_NAME"]
    n_name = {c["name"]: c for c in ds["nation"]["columns"]}["N_NAME"]
    p_name = {c["name"]: c for c in ds["part"]["columns"]}["P_NAME"]
    person = {c["name"]: c for c in ds["hosts"]["columns"]}["name"]

    for col in (r_name, n_name, p_name):
        assert col["pii"]["category"] == "name", "the flag is never removed"
        assert col["pii"]["confidence"] < 0.5
    assert person["pii"]["category"] == "name"
    assert person["pii"]["confidence"] == 0.75, "person shape corroborates"
    # De-rating never weakens min/max suppression: string columns stay hidden.
    for col in (r_name, n_name, p_name, person):
        assert col["min_value"] is None and col["max_value"] is None
        assert set(col["pii"]) == {"category", "confidence"}


def test_single_first_names_keep_base_confidence(airbnb_duckdb: Path, capsys):
    """Single-token first names ('Grace', 'Alan') match no shape rule in either
    direction: ambiguity keeps the name-derived 0.6, which blocks."""

    payload = _run(
        ["explore", "profile", "RAW_REVIEWS", "--path", str(airbnb_duckdb)], capsys
    )
    (dataset,) = payload["data"]["datasets"]
    reviewer = {c["name"]: c for c in dataset["columns"]}["REVIEWER_NAME"]
    assert reviewer["pii"]["confidence"] == 0.6


# --- _refine_confidence: the shape rules ---------------------------------------


def _aggregate(**kwargs):
    from exmergo_dex_core.adapters.base import ColumnAggregate

    defaults = {
        "name": "x",
        "null_fraction": 0.0,
        "distinct_count": 100,
        "is_unique": False,
        "min_value": None,
        "max_value": None,
    }
    return ColumnAggregate(**{**defaults, **kwargs})


def _generic_name_flag():
    from exmergo_dex_core.cache import PIIFlag

    return PIIFlag(category=PIICategory.NAME, confidence=0.6)


@pytest.mark.parametrize(
    ("aggregate", "expected"),
    [
        # Person-shaped distribution corroborates to the exact-token level.
        ({"person_shape_fraction": 0.5}, 0.75),
        ({"person_shape_fraction": 1.0}, 0.75),
        # Tiny closed all-caps vocabulary: the R_NAME shape (5/5 distinct, so
        # only value shape, never cardinality, can clear it).
        (
            {
                "person_shape_fraction": 0.0,
                "upper_vocab_fraction": 1.0,
                "distinct_count": 5,
            },
            0.3,
        ),
        # The distinct cap bounds the all-caps rule: a large all-caps vocabulary
        # (an uppercased person-name column defeats the person-shape check).
        (
            {
                "person_shape_fraction": 0.0,
                "upper_vocab_fraction": 1.0,
                "distinct_count": 33,
            },
            0.6,
        ),
        # Long multi-token labels: the P_NAME / product-title shape.
        ({"person_shape_fraction": 0.0, "avg_token_count": 5.0}, 0.3),
        ({"person_shape_fraction": 0.0, "avg_token_count": 3.5}, 0.3),
        # Ambiguity blocks: person shape present but not dominant.
        ({"person_shape_fraction": 0.2, "avg_token_count": 5.0}, 0.6),
        # Two-token title case ('Australian Grand Prix' averages ~3 tokens,
        # 'Memphis TN' fails the person shape): no rule fires, stays blocked.
        ({"person_shape_fraction": 0.0, "avg_token_count": 2.0}, 0.6),
        # Fail closed: no evidence moves nothing.
        ({}, 0.6),
    ],
)
def test_shape_rules_move_generic_name_confidence(aggregate: dict, expected: float):
    from exmergo_dex_core.explore.profile import _refine_confidence

    refined = _refine_confidence(
        _generic_name_flag(), _aggregate(**aggregate), generic=True
    )
    assert refined.category == PIICategory.NAME, "the flag is never removed"
    assert refined.confidence == expected


def test_shape_rules_apply_only_to_generic_marked_name_flags():
    from exmergo_dex_core.cache import PIIFlag
    from exmergo_dex_core.explore.profile import _refine_confidence

    reference_shaped = _aggregate(
        person_shape_fraction=0.0, upper_vocab_fraction=1.0, distinct_count=5
    )
    # An exact person token is not generic: shape evidence never de-rates it.
    exact = PIIFlag(category=PIICategory.NAME, confidence=0.75)
    assert _refine_confidence(exact, reference_shaped, generic=False).confidence == 0.75
    # Other categories are untouched by the shape rules even if marked.
    free_text = PIIFlag(category=PIICategory.FREE_TEXT, confidence=0.5)
    assert (
        _refine_confidence(free_text, reference_shaped, generic=True).confidence == 0.5
    )


def test_shape_stats_requested_only_for_generic_name_string_columns():
    from exmergo_dex_core.adapters.base import ColumnAggregate, ColumnMeta, ObjectMeta
    from exmergo_dex_core.explore import profile as profile_mod

    class _Recorder:
        name = "stub"
        dialect = "duckdb"

        def __init__(self):
            self.shape_requests: list[set[str]] = []
            self.columns = [
                ColumnMeta("id", "INTEGER", False, 0),
                ColumnMeta("reviewer_name", "VARCHAR", True, 1),
                ColumnMeta("first_name", "VARCHAR", True, 2),
                ColumnMeta("product_name", "VARCHAR", True, 3),
                ColumnMeta("email", "VARCHAR", True, 4),
            ]

        def table_metadata(self, identifier):
            meta = ObjectMeta(
                identifier=identifier,
                object_type="table",
                schema="s",
                name="t",
                row_count=1,
                byte_size=None,
                column_count=len(self.columns),
            )
            return meta, self.columns

        def column_aggregates(
            self, identifier, columns, *, safe_min_max=None, shape_stats=None
        ):
            self.shape_requests.append(set(shape_stats or set()))
            return [ColumnAggregate(c.name, 0.0, 1, False, None, None) for c in columns]

    adapter = _Recorder()
    profile_mod.profile(adapter, ["db.s.t"])
    # Only the generic `*_name` flag buys shape SQL: not the exact token
    # (first_name), not the denylisted qualifier (product_name), not another
    # category (email), not a numeric column.
    assert adapter.shape_requests == [{"reviewer_name"}]


# --- pii_overrides: the durable human decision ---------------------------------


def _write_overrides(entries: list[str]) -> None:
    from exmergo_dex_core.config import DexConfig, PIIOverride, save_config

    save_config(
        DexConfig(pii_overrides=[PIIOverride(column=e) for e in entries]),
    )


def test_pii_override_clears_flag_with_audit_and_survives_reprofile(
    tpch_names_duckdb: Path, capsys
):
    _write_overrides(["tpch_names.main.hosts.name"])
    for _ in range(2):  # the second run proves the override survives re-profiling
        payload = _run(
            ["explore", "profile", "hosts", "--path", str(tpch_names_duckdb)], capsys
        )
        (dataset,) = payload["data"]["datasets"]
        person = {c["name"]: c for c in dataset["columns"]}["name"]
        assert person["pii"] is None
        assert person["pii_overridden"] == "name", "the audit trail"
        assert any("pii_overrides" in n for n in payload["data"]["notes"])


def test_pii_override_matching_no_column_warns(tpch_names_duckdb: Path, capsys):
    _write_overrides(["tpch_names.main.hosts.nmae"])
    rc = main(["explore", "profile", "hosts", "--path", str(tpch_names_duckdb)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any("matches no column" in w for w in payload["warnings"])
    (dataset,) = payload["data"]["datasets"]
    person = {c["name"]: c for c in dataset["columns"]}["name"]
    assert person["pii"] is not None, "a typo must not clear anything"


# --- pii_overrides pattern form (column_name + scope): issue #106 --------------


def _write_pattern_override(column_name: str, scope: str) -> None:
    from exmergo_dex_core.config import DexConfig, PIIOverride, save_config

    save_config(
        DexConfig(pii_overrides=[PIIOverride(column_name=column_name, scope=scope)]),
    )


def test_pii_override_pattern_clears_flag_with_audit(tpch_names_duckdb: Path, capsys):
    _write_pattern_override("name", "tpch_names.main.*")
    payload = _run(
        ["explore", "profile", "hosts", "--path", str(tpch_names_duckdb)], capsys
    )
    (dataset,) = payload["data"]["datasets"]
    person = {c["name"]: c for c in dataset["columns"]}["name"]
    assert person["pii"] is None
    assert person["pii_overridden"] == "name", "the audit trail"
    assert any("pii_overrides" in n for n in payload["data"]["notes"])


def test_pii_override_pattern_scope_excludes_non_matching_dataset(
    tpch_names_duckdb: Path, capsys
):
    _write_pattern_override("name", "tpch_names.main.other_*")
    payload = _run(
        ["explore", "profile", "hosts", "--path", str(tpch_names_duckdb)], capsys
    )
    (dataset,) = payload["data"]["datasets"]
    person = {c["name"]: c for c in dataset["columns"]}["name"]
    assert person["pii"] is not None, "a non-matching scope must not clear anything"


def test_pii_override_pattern_matching_no_column_warns(tpch_names_duckdb: Path, capsys):
    _write_pattern_override("nmae", "tpch_names.main.*")
    rc = main(["explore", "profile", "hosts", "--path", str(tpch_names_duckdb)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any("matches no column" in w for w in payload["warnings"])


def test_pii_override_pattern_matching_zero_tables_stays_silent(
    tpch_names_duckdb: Path, capsys
):
    # A scope naming an entity that hasn't landed yet must not warn: that's
    # the whole point of the pattern form (new tables land under scope later).
    _write_pattern_override("name", "no_such_db.*")
    payload = _run(
        ["explore", "profile", "hosts", "--path", str(tpch_names_duckdb)], capsys
    )
    assert payload["warnings"] == []


def test_pii_override_pattern_clears_across_multiple_tables():
    """The point of the pattern form: one config entry clears the same
    structurally identical column on every table its scope glob matches, and
    leaves it alone outside that scope."""

    from exmergo_dex_core.adapters.base import ColumnAggregate, ColumnMeta, ObjectMeta
    from exmergo_dex_core.config import PIIOverride, pii_override_paths
    from exmergo_dex_core.explore import profile as profile_mod

    class _Recorder:
        name = "stub"
        dialect = "duckdb"

        def __init__(self):
            self.shape_requests: list[set[str]] = []

        def table_metadata(self, identifier):
            columns = [ColumnMeta("document_name", "VARCHAR", True, 0)]
            meta = ObjectMeta(
                identifier=identifier,
                object_type="table",
                schema="s",
                name=identifier,
                row_count=1,
                byte_size=None,
                column_count=len(columns),
            )
            return meta, columns

        def column_aggregates(
            self, identifier, columns, *, safe_min_max=None, shape_stats=None
        ):
            self.shape_requests.append(set(shape_stats or set()))
            return [ColumnAggregate(c.name, 0.0, 1, False, None, None) for c in columns]

    adapter = _Recorder()
    matcher = pii_override_paths(
        [PIIOverride(column_name="document_name", scope="db.raw_*")]
    )
    datasets = profile_mod.profile(
        adapter,
        ["db.raw_orders_dev", "db.raw_users_qa", "db.other_table"],
        pii_overrides=matcher,
    )
    by_id = {d.identifier: d for d in datasets}
    assert by_id["db.raw_orders_dev"].columns[0].pii is None
    assert by_id["db.raw_orders_dev"].columns[0].pii_overridden is not None
    assert by_id["db.raw_users_qa"].columns[0].pii is None
    assert by_id["db.raw_users_qa"].columns[0].pii_overridden is not None
    # Outside the scope glob, the same-named column is untouched.
    assert by_id["db.other_table"].columns[0].pii is not None
    assert by_id["db.other_table"].columns[0].pii_overridden is None
    # No shape SQL spent on the two overridden columns; only the unmatched one.
    assert adapter.shape_requests == [set(), set(), {"document_name"}]


# --- blob-type column exclusion -------------------------------------------------


def test_blob_column_excluded_by_default_with_note(blob_duckdb: Path, capsys):
    payload = _run(
        ["explore", "profile", "sessions", "--path", str(blob_duckdb)], capsys
    )
    (dataset,) = payload["data"]["datasets"]
    columns = {c["name"]: c for c in dataset["columns"]}
    payload_col = columns["payload"]
    assert payload_col["null_fraction"] is None
    assert payload_col["distinct_count"] is None
    # The informative sibling column is untouched by the exclusion.
    assert columns["id"]["distinct_count"] == 3
    note = next(n for n in dataset["data_quality"] if "blob-type column" in n)
    assert "payload" in note
    assert "blob_overrides" in note


def _write_blob_overrides(entries: list[str]) -> None:
    from exmergo_dex_core.config import BlobOverride, DexConfig, save_config

    save_config(DexConfig(blob_overrides=[BlobOverride(column=e) for e in entries]))


def test_blob_override_restores_stats(blob_duckdb: Path, capsys):
    _write_blob_overrides(["blob.main.sessions.payload"])
    payload = _run(
        ["explore", "profile", "sessions", "--path", str(blob_duckdb)], capsys
    )
    (dataset,) = payload["data"]["datasets"]
    payload_col = {c["name"]: c for c in dataset["columns"]}["payload"]
    assert payload_col["null_fraction"] == pytest.approx(1 / 3)
    assert payload_col["distinct_count"] == 2
    assert not any("blob-type column" in n for n in dataset["data_quality"])


def test_profile_include_blobs_param_matches_by_identifier_column(blob_duckdb: Path):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    adapter = DuckDBAdapter(blob_duckdb)
    try:
        (dataset,) = profile(
            adapter,
            ["blob.main.sessions"],
            include_blobs={"blob.main.sessions.payload"},
        )
    finally:
        adapter.close()
    payload_col = {c.name: c for c in dataset.columns}["payload"]
    assert payload_col.null_fraction is not None
    assert payload_col.distinct_count is not None


def test_non_unique_id_gets_fan_out_warning(airbnb_duckdb: Path, capsys):
    payload = _run(
        ["explore", "profile", "RAW_HOSTS", "--path", str(airbnb_duckdb)], capsys
    )
    hosts = payload["data"]["datasets"][0]
    warning = next(n for n in hosts["data_quality"] if "not unique" in n)
    assert "ID is not unique: ~2 distinct over 3 rows" in warning
    assert "fan out" in warning
    # With no unique column at all, the grain is explicitly unknown, not silent.
    assert any("grain unknown" in n for n in hosts["data_quality"])
    assert hosts["candidate_keys"] == []
    assert hosts["grain"] is None


# --- progress reporting -------------------------------------------------------


def test_fast_run_emits_no_stderr_and_one_envelope(airbnb_duckdb: Path, capfd):
    """The contract guard: a fast local run stays completely silent on stderr and
    still emits exactly one JSON envelope on stdout, so the progress plumbing
    never contaminates the boundary."""

    rc = main(
        [
            "explore",
            "profile",
            "RAW_HOSTS",
            "RAW_LISTINGS",
            "RAW_REVIEWS",
            "--path",
            str(airbnb_duckdb),
        ]
    )
    captured = capfd.readouterr()
    assert rc == 0, captured.out
    assert captured.err == ""  # fast run → no progress lines
    assert captured.out.count("\n") == 1  # exactly one envelope
    assert json.loads(captured.out)["status"] == "ok"


def test_profile_reporter_emits_progress_on_a_slow_run(airbnb_duckdb: Path):
    """Drive the emission path directly: a reporter forced past first_after over a
    StringIO stream produces the throttled per-object lines, proving profile()
    advances it once per profiled object."""

    import io

    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    now = [0.0]
    stream = io.StringIO()
    reporter = ProgressReporter(
        3,
        "profiled",
        "objects",
        stream=stream,
        clock=lambda: now[0],
        interval=0.0,  # no throttle: every advance past first_after emits
    )
    now[0] = PROGRESS_FIRST_AFTER + 0.1  # every advance is past the threshold

    adapter = DuckDBAdapter(airbnb_duckdb)
    try:
        identifiers = [m.identifier for m in adapter.list_objects()]
        profile(adapter, identifiers[:3], progress=reporter)
    finally:
        adapter.close()

    lines = stream.getvalue().splitlines()
    # The final object is announced by done(), not advance(); advance() fires for
    # the first two of three objects.
    assert lines == ["dex: profiled 1/3 objects", "dex: profiled 2/3 objects"]
    reporter.done()
    assert stream.getvalue().endswith("dex: profiled 3/3 objects\n")


def test_clean_table_gets_no_warnings_and_a_grain(airbnb_duckdb: Path, capsys):
    payload = _run(
        ["explore", "profile", "RAW_LISTINGS", "--path", str(airbnb_duckdb)], capsys
    )
    listings = payload["data"]["datasets"][0]
    assert listings["data_quality"] == []
    assert ["ID"] in listings["candidate_keys"]
    assert listings["grain"] == ["ID"]


def test_repeated_foreign_key_is_not_warned_as_broken_grain(duckdb_file: Path, capsys):
    """orders.customer_id repeats by design (a child table); only the table's own
    key column may trigger the fan-out warning."""

    payload = _run(["explore", "profile", "orders", "--path", str(duckdb_file)], capsys)
    orders = payload["data"]["datasets"][0]
    assert not any("customer_id" in n for n in orders["data_quality"])


def test_profile_accepts_comma_separated_objects(airbnb_duckdb: Path, capsys):
    payload = _run(
        [
            "explore",
            "profile",
            "RAW_HOSTS,RAW_LISTINGS, RAW_REVIEWS",
            "--path",
            str(airbnb_duckdb),
        ],
        capsys,
    )
    names = {d["identifier"].split(".")[-1] for d in payload["data"]["datasets"]}
    assert names == {"RAW_HOSTS", "RAW_LISTINGS", "RAW_REVIEWS"}


# --- persistence: profile writes through to the .dex/ cache ---------------------


def _profile(objects: list[str], db: Path, repo: Path, capsys, *flags: str) -> dict:
    return _run(
        [
            "explore",
            "profile",
            *objects,
            "--path",
            str(db),
            "--repo-root",
            str(repo),
            *flags,
        ],
        capsys,
    )


def _map(db: Path, repo: Path, capsys) -> dict:
    return _run(["explore", "map", "--path", str(db), "--repo-root", str(repo)], capsys)


def _dataset(cache: DexCache, suffix: str) -> Dataset:
    return next(d for d in cache.datasets if d.identifier.endswith(f".{suffix}"))


def test_profile_writes_cache_when_none_exists(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _profile(["RAW_HOSTS"], airbnb_duckdb, repo, capsys)

    assert (repo / ".dex" / "cache.json").is_file()
    assert payload["data"]["cache_path"].endswith("cache.json")
    assert payload["data"]["updated_at"]
    assert any("created .dex/cache.json" in n for n in payload["data"]["notes"])
    assert any("explore map" in n for n in payload["data"]["notes"])

    cache = DexStore(repo).load_cache()
    assert [d.identifier.split(".")[-1] for d in cache.datasets] == ["RAW_HOSTS"]
    assert cache.datasets[0].columns, "the firewall needs columns to allow queries"
    assert cache.relationships == []
    assert cache.provenance.connector == "duckdb"
    assert cache.provenance.created_at == cache.provenance.updated_at


def test_profile_merges_into_existing_map_preserving_relationships_and_rank(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _map(airbnb_duckdb, repo, capsys)
    store = DexStore(repo)
    before = store.load_cache()
    rels_before = [r.model_dump() for r in before.relationships]
    assert rels_before, "the airbnb fixture has inferable joins"
    hosts_before = _dataset(before, "RAW_HOSTS")
    assert hosts_before.rank_score is not None

    # --refresh forces the re-profile: without it the just-mapped profile is a
    # fresh cache hit, which the reuse tests below cover; here the merge path is
    # what's under test.
    payload = _profile(["RAW_HOSTS"], airbnb_duckdb, repo, capsys, "--refresh")
    after = store.load_cache()

    assert [r.model_dump() for r in after.relationships] == rels_before
    assert {d.identifier for d in after.datasets} == {
        d.identifier for d in before.datasets
    }
    hosts_after = _dataset(after, "RAW_HOSTS")
    assert hosts_after.rank_score == hosts_before.rank_score
    assert after.provenance.created_at == before.provenance.created_at
    assert after.provenance.updated_at >= before.provenance.updated_at
    note = next(n for n in payload["data"]["notes"] if "merged" in n)
    assert "1 refreshed, 0 added" in note
    assert "relationships preserved" in note


def test_profile_refresh_forces_reprofile_updates_profiled_at(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _map(airbnb_duckdb, repo, capsys)
    store = DexStore(repo)
    before = store.load_cache()
    old = _dataset(before, "RAW_HOSTS")
    reviews_stamp = _dataset(before, "RAW_REVIEWS").profiled_at
    assert old.profiled_at is not None

    _profile(["RAW_HOSTS"], airbnb_duckdb, repo, capsys, "--refresh")
    after = store.load_cache()
    new = _dataset(after, "RAW_HOSTS")
    assert new.profiled_at > old.profiled_at, "the fresh measurement wins"
    assert new.rank_score == old.rank_score
    # A table neither re-profiled nor requested keeps its older stamp.
    assert _dataset(after, "RAW_REVIEWS").profiled_at == reviews_stamp


def test_profile_reuses_fresh_cached_profile(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """`explore profile` on a table `explore map` just profiled serves the
    cached profile free: nothing is re-scanned, the profile is still returned,
    and the stamp is untouched (issue #128)."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _map(airbnb_duckdb, repo, capsys)
    store = DexStore(repo)
    stamp_before = _dataset(store.load_cache(), "RAW_HOSTS").profiled_at

    payload = _profile(["RAW_HOSTS"], airbnb_duckdb, repo, capsys)
    assert payload["data"]["profiled_count"] == 0
    assert payload["data"]["cache_hit_count"] == 1
    # The cached profile is still served, columns and all, so a follow-on
    # `explore query` on it works without a second scan.
    served = payload["data"]["datasets"]
    assert [d["identifier"].split(".")[-1] for d in served] == ["RAW_HOSTS"]
    assert served[0]["columns"]
    assert any("reused 1 fresh cached profile" in n for n in payload["data"]["notes"])
    assert _dataset(store.load_cache(), "RAW_HOSTS").profiled_at == stamp_before

    # --refresh forces the re-scan even though the cache is fresh.
    refreshed = _profile(["RAW_HOSTS"], airbnb_duckdb, repo, capsys, "--refresh")
    assert refreshed["data"]["profiled_count"] == 1
    assert refreshed["data"]["cache_hit_count"] == 0


def test_profile_freshness_zero_disables_reuse(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    save_config(DexConfig(profile_freshness_hours=0.0), repo)
    _map(airbnb_duckdb, repo, capsys)
    payload = _profile(["RAW_HOSTS"], airbnb_duckdb, repo, capsys)
    assert payload["data"]["profiled_count"] == 1
    assert payload["data"]["cache_hit_count"] == 0


def test_profile_reprofiles_only_the_schema_changed_object(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """When one requested table's schema drifts between runs, `explore profile`
    re-scans only it; the rest stay fresh cache hits."""

    duckdb = pytest.importorskip("duckdb")
    repo = tmp_path / "repo"
    repo.mkdir()
    _map(airbnb_duckdb, repo, capsys)

    conn = duckdb.connect(str(airbnb_duckdb))
    conn.execute("ALTER TABLE RAW_HOSTS ADD COLUMN extra VARCHAR")
    conn.close()

    payload = _profile(["RAW_HOSTS", "RAW_LISTINGS"], airbnb_duckdb, repo, capsys)
    assert payload["data"]["profiled_count"] == 1
    assert payload["data"]["cache_hit_count"] == 1
    hosts = next(
        d for d in payload["data"]["datasets"] if d["identifier"].endswith(".RAW_HOSTS")
    )
    assert any(c["name"] == "extra" for c in hosts["columns"]), (
        "the re-profile saw the new column"
    )


def test_profile_inserts_new_dataset(airbnb_duckdb: Path, tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _profile(["RAW_HOSTS"], airbnb_duckdb, repo, capsys)
    payload = _profile(["RAW_LISTINGS"], airbnb_duckdb, repo, capsys)

    cache = DexStore(repo).load_cache()
    names = {d.identifier.split(".")[-1] for d in cache.datasets}
    assert names == {"RAW_HOSTS", "RAW_LISTINGS"}
    assert _dataset(cache, "RAW_LISTINGS").rank_score is None
    note = next(n for n in payload["data"]["notes"] if "merged" in n)
    assert "0 refreshed, 1 added" in note


def test_profile_connector_mismatch_replaces_cache(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    seeded = DexCache(
        datasets=[
            Dataset(
                identifier="proj.ds.t",
                columns=[ColumnProfile(name="id", data_type="INTEGER")],
            )
        ],
        relationships=[
            Relationship(
                from_dataset="proj.ds.t",
                from_columns=["id"],
                to_dataset="proj.ds.u",
                to_columns=["id"],
            )
        ],
    )
    seeded.provenance.connector = "bigquery"
    seeded.provenance.created_at = "2020-01-01T00:00:00+00:00"
    DexStore(repo).save_cache(seeded)

    payload = _profile(["RAW_HOSTS"], airbnb_duckdb, repo, capsys)
    cache = DexStore(repo).load_cache()
    assert cache.provenance.connector == "duckdb"
    assert [d.identifier.split(".")[-1] for d in cache.datasets] == ["RAW_HOSTS"]
    assert cache.relationships == []
    assert cache.provenance.created_at != "2020-01-01T00:00:00+00:00"
    note = next(n for n in payload["data"]["notes"] if "bigquery" in n)
    assert "explore map" in note


def test_merge_profiles_carries_rank_and_preserves_relationships():
    from exmergo_dex_core.explore.commands import _merge_profiles

    def _prior() -> DexCache:
        prior = DexCache(
            datasets=[
                Dataset(
                    identifier="db.s.a",
                    rank_score=0.9,
                    columns=[ColumnProfile(name="id", data_type="INTEGER")],
                    profiled_at="2026-01-01T00:00:00+00:00",
                ),
                Dataset(
                    identifier="db.s.b",
                    rank_score=0.5,
                    columns=[ColumnProfile(name="id", data_type="INTEGER")],
                    profiled_at="2026-01-01T00:00:00+00:00",
                ),
            ],
            relationships=[
                Relationship(
                    from_dataset="db.s.b",
                    from_columns=["a_id"],
                    to_dataset="db.s.a",
                    to_columns=["id"],
                )
            ],
        )
        prior.provenance.connector = "duckdb"
        prior.provenance.created_at = "2026-01-01T00:00:00+00:00"
        return prior

    now = datetime(2026, 7, 13, tzinfo=UTC)

    def _fresh() -> list[Dataset]:
        # A fresh list per scenario: the merge carries rank onto these objects.
        return [
            Dataset(
                identifier="db.s.a",
                columns=[ColumnProfile(name="id", data_type="INTEGER")],
                profiled_at=now.isoformat(),
            ),
            Dataset(
                identifier="db.s.c",
                columns=[ColumnProfile(name="id", data_type="INTEGER")],
                profiled_at=now.isoformat(),
            ),
        ]

    cache, stats = _merge_profiles(_prior(), _fresh(), "duckdb", now)
    by_id = {d.identifier: d for d in cache.datasets}
    assert by_id["db.s.a"].rank_score == 0.9, "map's ranking is carried forward"
    assert by_id["db.s.a"].profiled_at == now.isoformat()
    assert by_id["db.s.b"].profiled_at == "2026-01-01T00:00:00+00:00", "untouched"
    assert by_id["db.s.c"].rank_score is None, "never ranked"
    assert len(cache.relationships) == 1, "preserved by default"
    assert cache.provenance.created_at == "2026-01-01T00:00:00+00:00"
    assert stats["merged"] is True
    assert stats["refreshed"] == 1 and stats["added"] == 1
    assert stats["replaced_connector"] is None

    # Connector mismatch: the prior is dropped wholesale, created_at resets.
    cache, stats = _merge_profiles(_prior(), [_fresh()[0]], "postgres", now)
    assert [d.identifier for d in cache.datasets] == ["db.s.a"]
    assert cache.datasets[0].rank_score is None
    assert cache.relationships == []
    assert cache.provenance.connector == "postgres"
    assert cache.provenance.created_at == now.isoformat()
    assert stats["replaced_connector"] == "duckdb"

    # Passing relationships replaces the prior set (the relationships command).
    replacement = [
        Relationship(
            from_dataset="db.s.c",
            from_columns=["a_id"],
            to_dataset="db.s.a",
            to_columns=["id"],
        )
    ]
    cache, _ = _merge_profiles(
        _prior(), _fresh(), "duckdb", now, relationships=replacement
    )
    assert cache.relationships == replacement


# --- exact-count escalation ----------------------------------------------------


def test_near_unique_key_escalates_to_exact_and_confirms_grain(
    near_unique_duckdb: Path, capsys
):
    payload = _run(
        ["explore", "profile", "results", "--path", str(near_unique_duckdb)], capsys
    )
    results = payload["data"]["datasets"][0]
    key = {c["name"]: c for c in results["columns"]}["resultId"]
    assert key["distinct_count"] == 50000
    assert key["distinct_count_exact"] is True
    assert key["is_unique"] is True
    assert ["resultId"] in results["candidate_keys"]
    assert results["grain"] == ["resultId"]
    assert not any("not unique" in n for n in results["data_quality"])
    assert not any("grain unknown" in n for n in results["data_quality"])


def test_true_duplicates_still_warn_with_exact_counts(near_unique_duckdb: Path, capsys):
    payload = _run(
        ["explore", "profile", "dupes", "--path", str(near_unique_duckdb)], capsys
    )
    dupes = payload["data"]["datasets"][0]
    warning = next(n for n in dupes["data_quality"] if "not unique" in n)
    assert "id is not unique: 45000 distinct over 50000 rows" in warning
    assert "fan out" in warning


class _StubAdapter:
    """Metadata-only double: crafted approximate aggregates, recorded escalations
    and composite probes. ``combos`` maps a column tuple to its exact distinct
    combination count; unlisted tuples come back just below the row count, so
    they read as probed-but-not-unique."""

    name = "stub"
    dialect = "duckdb"

    def __init__(
        self,
        rows: int,
        approx: dict[str, int],
        nulls: dict[str, float] | None = None,
        combos: dict[tuple[str, ...], int] | None = None,
    ):
        self.rows = rows
        self.approx = approx
        self.nulls = nulls or {}
        self.combos = combos or {}
        self.calls: list[list[str]] = []
        self.combo_calls: list[list[list[str]]] = []

    def table_metadata(self, identifier):
        from exmergo_dex_core.adapters.base import ColumnMeta, ObjectMeta

        meta = ObjectMeta(
            identifier=identifier,
            object_type="table",
            schema="s",
            name=identifier.rsplit(".", 1)[-1],
            row_count=self.rows,
            byte_size=None,
            column_count=len(self.approx),
        )
        columns = [
            ColumnMeta(name=n, data_type="INTEGER", nullable=True, ordinal=i)
            for i, n in enumerate(self.approx)
        ]
        return meta, columns

    def column_aggregates(
        self, identifier, columns, *, safe_min_max=None, shape_stats=None
    ):
        from exmergo_dex_core.adapters.base import ColumnAggregate

        return [
            ColumnAggregate(
                name=c.name,
                null_fraction=self.nulls.get(c.name, 0.0),
                distinct_count=self.approx[c.name],
                is_unique=False,
                min_value=None,
                max_value=None,
            )
            for c in columns
        ]

    def exact_distinct_counts(self, identifier, columns):
        self.calls.append(list(columns))
        return {n: (self.rows if n == "overshoot" else self.rows - 10) for n in columns}

    def distinct_combination_counts(self, identifier, combinations):
        self.combo_calls.append([list(c) for c in combinations])
        return {
            tuple(c): self.combos.get(tuple(c), self.rows - 10) for c in combinations
        }


def test_escalation_policy_is_bounded_and_targeted():
    from exmergo_dex_core.explore import profile as profile_mod

    # An approx overshooting the row count (the field signature of a real key),
    # ten in-band columns to overflow the cap, and one far below the band.
    approx = {
        "overshoot": 1010,
        **{f"near_{i}": 950 + i for i in range(10)},
        "low": 600,
    }
    adapter = _StubAdapter(rows=1000, approx=approx)
    datasets = profile_mod.profile(adapter, ["db.s.t"])

    assert len(adapter.calls) == 1, "all escalations batch into one adapter call"
    chosen = adapter.calls[0]
    assert len(chosen) == 8
    assert "overshoot" in chosen, "smallest gaps win and overshoot's gap is 10"
    assert "low" not in chosen
    assert "near_0" not in chosen and "near_1" not in chosen

    cols = {c.name: c for c in datasets[0].columns}
    assert cols["overshoot"].distinct_count == 1000
    assert cols["overshoot"].distinct_count_exact is True
    assert cols["overshoot"].is_unique is True
    assert cols["near_9"].distinct_count == 990
    assert cols["near_9"].is_unique is False
    assert cols["low"].distinct_count == 600
    assert cols["low"].distinct_count_exact is False


def test_adapter_without_exact_counts_degrades_gracefully():
    from exmergo_dex_core.explore import profile as profile_mod

    adapter = _StubAdapter(rows=1000, approx={"id": 990})
    adapter.exact_distinct_counts = None  # shadow the method: adapter can't escalate
    datasets = profile_mod.profile(adapter, ["db.s.t"])
    col = datasets[0].columns[0]
    assert col.distinct_count == 990
    assert col.distinct_count_exact is False
    # In the noise band and unproven: no non-uniqueness verdict.
    assert not any("not unique" in n for n in datasets[0].data_quality)


# --- composite-key probing ------------------------------------------------------


def test_composite_probe_is_bounded_and_targeted():
    """Pairs are pruned on the distinct-product necessary condition, nullable
    columns never enter a key, the id-shaped/smallest-product ranking picks the
    grain-shaped pair first, and only the pair whose exact combination count
    equals the row count is proven."""

    from exmergo_dex_core.explore import profile as profile_mod
    from exmergo_dex_core.explore import relationships as rel_mod

    adapter = _StubAdapter(
        rows=1000,
        approx={
            "order_key": 250,
            "line_no": 4,
            "qty": 30,
            "filler": 2,
            "commentId": 900,  # high-cardinality but nullable: never a key member
        },
        nulls={"commentId": 0.2},
        combos={("order_key", "line_no"): 1000},
    )
    datasets = profile_mod.profile(adapter, ["db.s.line_items"])

    assert len(adapter.combo_calls) == 1, "all pairs batch into one adapter call"
    probed = adapter.combo_calls[0]
    # Survivors of the product test only (250*4 and 250*30 reach the row count
    # within HLL slack; every filler pair falls short), best-ranked first,
    # members ordered by descending cardinality.
    assert probed == [["order_key", "line_no"], ["order_key", "qty"]]
    assert not any("commentId" in pair for pair in probed)

    ds = datasets[0]
    assert ds.composite_keys == [["order_key", "line_no"]]
    assert ["order_key", "line_no"] in rel_mod.candidate_keys(ds)
    assert rel_mod.detect_grain(ds) == ["order_key", "line_no"]
    assert not any("grain unknown" in n for n in rel_mod.data_quality_notes(ds))


def test_composite_probe_caps_the_pair_count():
    from exmergo_dex_core.explore import profile as profile_mod

    # Five interchangeable mid-cardinality columns: every pair survives the
    # product test, so only the cap keeps the probe bounded.
    adapter = _StubAdapter(rows=1000, approx={f"c{i}_id": 100 + i for i in range(5)})
    profile_mod.profile(adapter, ["db.s.t"])
    assert len(adapter.combo_calls) == 1
    assert len(adapter.combo_calls[0]) == 3


def test_composite_probe_skipped_when_single_key_exists():
    from exmergo_dex_core.explore import profile as profile_mod

    # "overshoot" escalates to a proven unique single key, so the composite
    # probe would be pure waste and must not fire.
    adapter = _StubAdapter(
        rows=1000, approx={"overshoot": 1010, "order_key": 250, "line_no": 4}
    )
    datasets = profile_mod.profile(adapter, ["db.s.t"])
    assert adapter.combo_calls == []
    assert datasets[0].composite_keys == []


def test_adapter_without_combination_counts_degrades_gracefully():
    from exmergo_dex_core.explore import profile as profile_mod
    from exmergo_dex_core.explore import relationships as rel_mod

    adapter = _StubAdapter(rows=1000, approx={"order_key": 250, "line_no": 4})
    adapter.distinct_combination_counts = None  # shadow: adapter can't probe
    datasets = profile_mod.profile(adapter, ["db.s.t"])
    ds = datasets[0]
    assert ds.composite_keys == []
    assert rel_mod.candidate_keys(ds) == []
    assert any("grain unknown" in n for n in rel_mod.data_quality_notes(ds))


def test_composite_grain_detected_end_to_end(composite_grain_duckdb: Path, capsys):
    """The TPCH LINEITEM shape: no single column is unique, the true grain is
    (order_key, line_number), and the profile proves it instead of reporting
    an unknown grain."""

    payload = _run(
        [
            "explore",
            "profile",
            "orders,line_items",
            "--path",
            str(composite_grain_duckdb),
        ],
        capsys,
    )
    ds = {d["identifier"].split(".")[-1]: d for d in payload["data"]["datasets"]}

    line_items = ds["line_items"]
    assert line_items["composite_keys"] == [["order_key", "line_number"]]
    assert ["order_key", "line_number"] in line_items["candidate_keys"]
    assert line_items["grain"] == ["order_key", "line_number"]
    assert not any("grain unknown" in n for n in line_items["data_quality"])

    # The sibling with a clean surrogate key keeps its single-column grain.
    orders = ds["orders"]
    assert orders["grain"] == ["order_key"]
    assert orders["composite_keys"] == []


def test_row_count_refreshes_after_the_aggregate_scan():
    """Adapters whose free row counts are planner estimates (Postgres
    reltuples) upgrade to the exact COUNT(*) the aggregate scan paid for; the
    profile engine must re-read the metadata so uniqueness proofs and the
    dataset row count compare against real rows, not the estimate."""

    from exmergo_dex_core.adapters.base import (
        ColumnAggregate,
        ColumnMeta,
        ObjectMeta,
    )
    from exmergo_dex_core.explore import profile as profile_mod

    class EstimatingAdapter:
        name = "stub"
        dialect = "duckdb"

        def __init__(self):
            self.scanned = False

        def table_metadata(self, identifier):
            rows = 1000 if self.scanned else 1200  # estimate is stale-high
            meta = ObjectMeta(
                identifier=identifier,
                object_type="table",
                schema="s",
                name="t",
                row_count=rows,
                byte_size=None,
                column_count=1,
            )
            return meta, [
                ColumnMeta(name="id", data_type="INTEGER", nullable=False, ordinal=0)
            ]

        def column_aggregates(
            self, identifier, columns, *, safe_min_max=None, shape_stats=None
        ):
            self.scanned = True
            return [
                ColumnAggregate(
                    name="id",
                    null_fraction=0.0,
                    distinct_count=990,  # near-unique against the REAL count
                    is_unique=None,
                    min_value=None,
                    max_value=None,
                )
            ]

        def exact_distinct_counts(self, identifier, columns):
            return dict.fromkeys(columns, 1000)

    datasets = profile_mod.profile(EstimatingAdapter(), ["db.s.t"])
    assert datasets[0].row_count == 1000  # the exact count, not the estimate
    id_col = datasets[0].columns[0]
    # 990 approx over 1000 real rows is in the escalation band; the exact scan
    # returns 1000 == 1000, a proof that would be missed against 1200.
    assert id_col.distinct_count == 1000
    assert id_col.distinct_count_exact is True
    assert id_col.is_unique is True
