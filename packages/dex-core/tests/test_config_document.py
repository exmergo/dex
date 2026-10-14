"""`.dex/config.yml` as a document: what it says, what is wrong with it, and edits
that change only the bytes a setting occupies.

The edit cases are byte-exact on purpose. The whole promise is that a reviewer's
diff shows the setting that changed and nothing else, so "the result loads to the
right settings" is not enough; the comments, the order, the blank lines and the
line endings around the change are what is under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.config_document import (
    ABSENT,
    ConfigDocument,
    ConfigEditError,
    InvalidConfigError,
    parse_value,
)
from exmergo_dex_core.errors import ConfigurationError, RequestError

COMMENTED = """\
# dex settings for this repo. Comments carry the reasoning.
connector: duckdb

budget:
  ceiling: 2000000000   # 2 GB per command
  session_celing: 5     # a typo the loader used to drop without a word

duckdb:
  path: demo.duckdb

query:
  max_rows: 50

cluster:
  sample_seed: null     # a fresh draw per run, on purpose
# end of settings
"""


# --- edits: byte-exact ------------------------------------------------------------

_SET_CASES = [
    pytest.param(
        "budget:\n  ceiling: 5   # why this cap\n",
        "budget.ceiling",
        7,
        "budget:\n  ceiling: 7   # why this cap\n",
        id="scalar-keeps-trailing-comment",
    ),
    pytest.param(
        "budget:\n  ceiling: 5\n\nquery:\n  max_rows: 50\n",
        "budget.session_ceiling",
        9,
        "budget:\n  ceiling: 5\n  session_ceiling: 9\n\nquery:\n  max_rows: 50\n",
        id="new-key-ends-its-block-before-the-separator",
    ),
    pytest.param(
        "budget:\n  ceiling: 5\n  # the next block caps rows\nquery:\n  max_rows: 1\n",
        "budget.session_ceiling",
        9,
        "budget:\n  ceiling: 5\n  session_ceiling: 9\n"
        "  # the next block caps rows\nquery:\n  max_rows: 1\n",
        id="comment-at-the-child-column-belongs-to-what-follows",
    ),
    pytest.param(
        "connector: duckdb\n\nquery:\n  max_rows: 50\n",
        "bigquery.project",
        "p",
        "connector: duckdb\n\nquery:\n  max_rows: 50\n\nbigquery:\n  project: p\n",
        id="missing-section-follows-blank-line-separation",
    ),
    pytest.param(
        "connector: duckdb\nquery:\n  max_rows: 50\n",
        "bigquery.project",
        "p",
        "connector: duckdb\nquery:\n  max_rows: 50\nbigquery:\n  project: p\n",
        id="missing-section-in-a-dense-file",
    ),
    pytest.param(
        "budget:\nconnector: duckdb\n",
        "budget.ceiling",
        5,
        "budget:\n  ceiling: 5\nconnector: duckdb\n",
        id="empty-parent",
    ),
    pytest.param(
        "budget: ~  # nothing yet\n",
        "budget.ceiling",
        5,
        "budget:  # nothing yet\n  ceiling: 5\n",
        id="null-parent-keeps-its-comment",
    ),
    pytest.param(
        "budget: {}\n",
        "budget.ceiling",
        5,
        "budget: {ceiling: 5}\n",
        id="empty-flow-parent",
    ),
    pytest.param(
        "budget: {ceiling: 4}\n",
        "budget.session_ceiling",
        5,
        "budget: {ceiling: 4, session_ceiling: 5}\n",
        id="flow-parent-with-entries",
    ),
    pytest.param(
        "bigquery:\n  datasets: [a]  # raw only\n",
        "bigquery.datasets",
        ["a", "b"],
        "bigquery:\n  datasets: [a, b]  # raw only\n",
        id="flow-list",
    ),
    pytest.param(
        "bigquery:\n  datasets:\n    - a\n    - b\n  project: p\n",
        "bigquery.datasets",
        ["c"],
        "bigquery:\n  datasets:\n    - c\n  project: p\n",
        id="block-list-keeps-its-indentation",
    ),
    pytest.param(
        "bigquery:\n  datasets:\n  - a\n  - b\n  project: p\n",
        "bigquery.datasets",
        ["c", "d"],
        "bigquery:\n  datasets:\n  - c\n  - d\n  project: p\n",
        id="indentless-block-list",
    ),
    pytest.param(
        "bigquery:\n  datasets:  # none yet\n    - a\n  project: p\n",
        "bigquery.datasets",
        [],
        "bigquery:\n  datasets: []  # none yet\n  project: p\n",
        id="block-list-emptied",
    ),
    pytest.param(
        "connector: duckdb\n",
        "pii_overrides",
        [{"column": "a.b.c", "reason": "an id"}],
        "connector: duckdb\npii_overrides:\n  - column: a.b.c\n    reason: an id\n",
        id="new-list-of-mappings",
    ),
    pytest.param(
        "pii_overrides: []  # reviewed\n",
        "pii_overrides",
        [{"column": "x.y.z"}],
        "pii_overrides:  # reviewed\n  - column: x.y.z\n",
        id="flow-value-becomes-a-block",
    ),
    pytest.param(
        "dbt_target: 'dev'\n",
        "dbt_target",
        "prod",
        "dbt_target: 'prod'\n",
        id="quoted-scalar-keeps-its-quotes",
    ),
    pytest.param(
        "connector: duckdb\n",
        "dbt_target",
        "no",
        "connector: duckdb\ndbt_target: 'no'\n",
        id="string-yaml-would-misread-is-quoted",
    ),
    pytest.param("", "connector", "duckdb", "connector: duckdb\n", id="empty-file"),
    pytest.param(
        "---\n", "connector", "duckdb", "---\nconnector: duckdb\n", id="start-marker"
    ),
    pytest.param(
        "# nothing yet\n",
        "connector",
        "duckdb",
        "# nothing yet\nconnector: duckdb\n",
        id="comments-only",
    ),
    pytest.param(
        "connector: duckdb",
        "dbt_target",
        "dev",
        "connector: duckdb\ndbt_target: dev",
        id="no-trailing-newline-stays-that-way",
    ),
    pytest.param(
        "connector: duckdb\r\nbudget:\r\n  ceiling: 1\r\n",
        "budget.session_ceiling",
        2,
        "connector: duckdb\r\nbudget:\r\n  ceiling: 1\r\n  session_ceiling: 2\r\n",
        id="crlf",
    ),
    pytest.param(
        "﻿connector: duckdb\n",
        "connector",
        "bigquery",
        "﻿connector: bigquery\n",
        id="byte-order-mark",
    ),
    pytest.param(
        "connector: duckdb\n# end of settings\n",
        "dbt_target",
        "dev",
        "connector: duckdb\ndbt_target: dev\n# end of settings\n",
        id="footer-comment-stays-last",
    ),
    pytest.param(
        "cluster:\n  sample_seed: null  # fresh draw\n",
        "cluster.sample_seed",
        3,
        "cluster:\n  sample_seed: 3  # fresh draw\n",
        id="explicit-null",
    ),
    pytest.param(
        "budget:\n    ceiling: 5\n",
        "query.max_rows",
        1,
        "budget:\n    ceiling: 5\nquery:\n    max_rows: 1\n",
        id="file-indent-unit",
    ),
    pytest.param(
        "cache:\n  backend: x\n",
        "cache.options.region",
        "eu",
        "cache:\n  backend: x\n  options:\n    region: eu\n",
        id="free-form-option",
    ),
]


@pytest.mark.parametrize(("before", "key", "value", "after"), _SET_CASES)
def test_a_set_changes_only_the_bytes_the_setting_occupies(before, key, value, after):
    assert ConfigDocument(before).with_value(key, value).text == after


_REFUSED = [
    pytest.param(
        "budget: &b\n  ceiling: 5\nother: *b\n",
        "budget.ceiling",
        id="anchor-and-alias",
    ),
    pytest.param("budget:\n\tceiling: 5\n", "budget.ceiling", id="tab-indentation"),
    pytest.param("dbt_target: |\n  dev\n", "dbt_target", id="block-scalar"),
    pytest.param(
        "budget: {ceiling: 4,\n  session_ceiling: 5}\n",
        "budget.session_ceiling_declined",
        id="multi-line-flow-parent",
    ),
    pytest.param("connector: a\nconnector: b\n", "connector", id="duplicated-key"),
    pytest.param("- a\n", "connector", id="not-a-mapping"),
    pytest.param("budget: 5\n", "budget.ceiling", id="parent-holds-a-value"),
    pytest.param("budget: [\n", "budget.ceiling", id="does-not-parse"),
]


@pytest.mark.parametrize(("text", "key"), _REFUSED)
def test_a_layout_dex_cannot_span_is_refused_rather_than_rewritten(text, key):
    with pytest.raises(ConfigEditError, match="by hand"):
        ConfigDocument(text).with_value(key, True)


def test_setting_a_value_back_restores_the_file_byte_for_byte():
    original = ConfigDocument(COMMENTED)
    changed = original.with_value("query.max_rows", 5)
    assert changed.text != COMMENTED
    assert changed.with_value("query.max_rows", 50).text == COMMENTED


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("query.max_rows", 5),
        ("budget.session_ceiling", 100),
        ("bigquery.datasets", ["raw", "staging"]),
        ("pii_overrides", [{"column": "a.b.c", "reason": "an id"}]),
    ],
)
def test_every_line_the_file_had_survives_in_order(key, value):
    after = ConfigDocument(COMMENTED).with_value(key, value).text.splitlines()
    remaining = list(after)
    for line in COMMENTED.splitlines():
        if key == "query.max_rows" and line.strip() == "max_rows: 50":
            continue
        assert line in remaining, line
        remaining = remaining[remaining.index(line) + 1 :]


def test_replacing_a_block_list_says_which_comments_it_drops():
    text = "bigquery:\n  datasets:\n    - a  # raw\n    - b\n"
    edited = ConfigDocument(text).with_value("bigquery.datasets", ["c"])
    assert edited.edit_warnings == [
        "replacing datasets drops 1 comment line written inside it"
    ]


def test_without_removes_the_key_and_nothing_else():
    text = (
        "budget:\n  ceiling: 5\n  session_ceiling: 9  # cap\n  paradigm: free_local\n"
        "query:\n  max_rows: 1\n"
    )
    after = ConfigDocument(text).without("budget.session_ceiling").text
    assert after == (
        "budget:\n  ceiling: 5\n  paradigm: free_local\nquery:\n  max_rows: 1\n"
    )


def test_without_the_last_key_removes_its_block_rather_than_leaving_a_null():
    text = "connector: duckdb\nbudget:\n  session_ceiling: 9\nquery:\n  max_rows: 1\n"
    after = ConfigDocument(text).without("budget.session_ceiling").text
    assert after == "connector: duckdb\nquery:\n  max_rows: 1\n"


def test_without_an_absent_key_is_the_same_document():
    document = ConfigDocument(COMMENTED)
    assert document.without("budget.session_ceiling") is document


_UNSET_CASES = [
    pytest.param(
        COMMENTED,
        "budget.session_celing",
        COMMENTED.replace(
            "  session_celing: 5     # a typo the loader used to drop without a word\n",
            "",
        ),
        id="unknown-key-goes-with-its-comment-and-nothing-else",
    ),
    pytest.param(
        "a:\n  x: 1\n\nbudget:\n  y: 1\n\nquery:\n  z: 1\n",
        "budget.y",
        "a:\n  x: 1\n\nquery:\n  z: 1\n",
        id="emptied-block-between-separators-keeps-one",
    ),
    pytest.param(
        "budget:\n  y: 1\n\nquery:\n  z: 1\n",
        "budget",
        "query:\n  z: 1\n",
        id="first-block-leaves-no-blank-at-the-top",
    ),
    pytest.param(
        "a:\n  x: 1\n\nbudget:\n  y: 1\n",
        "budget",
        "a:\n  x: 1\n",
        id="last-block-leaves-no-blank-at-the-end",
    ),
    pytest.param(
        "# settings\n\nbudget:\n  y: 1\n\nquery:\n  z: 1\n",
        "budget",
        "# settings\n\nquery:\n  z: 1\n",
        id="header-comment-keeps-its-separator",
    ),
    pytest.param(
        "a:\n  x: 1\n\nbudget:\n  y: 1\n# end of settings\n",
        "budget",
        "a:\n  x: 1\n\n# end of settings\n",
        id="footer-comment-stays",
    ),
    pytest.param(
        "connector: duckdb\nbudget:\n  y: 1\n\nquery:\n  z: 1\n",
        "budget",
        "connector: duckdb\n\nquery:\n  z: 1\n",
        id="separator-below-only-stays",
    ),
    pytest.param(
        "semantic:\n  ossie:\n    files: [a.yaml]\nquery:\n  z: 1\n",
        "semantic.ossie.files",
        "query:\n  z: 1\n",
        id="climbs-two-levels",
    ),
    pytest.param(
        "connector: duckdb\n\nbigqeury:   # a copy-paste\n  project: p   # old\n"
        "  datasets:\n    - raw   # the only one\n\nquery:\n  z: 1\n",
        "bigqeury",
        "connector: duckdb\n\nquery:\n  z: 1\n",
        id="unknown-section-with-comments-inside",
    ),
    pytest.param(
        "budget:\n  # the cap finance agreed\n  ceiling: 5\n  paradigm: free_local\n",
        "budget.ceiling",
        "budget:\n  # the cap finance agreed\n  paradigm: free_local\n",
        id="comment-above-the-key-stays",
    ),
    pytest.param(
        "budget: {ceiling: 4, session_ceiling: 5, paradigm: free_local}\n",
        "budget.ceiling",
        "budget: {session_ceiling: 5, paradigm: free_local}\n",
        id="first-flow-entry",
    ),
    pytest.param(
        "budget: {ceiling: 4, session_ceiling: 5, paradigm: free_local}\n",
        "budget.session_ceiling",
        "budget: {ceiling: 4, paradigm: free_local}\n",
        id="middle-flow-entry",
    ),
    pytest.param(
        "budget: {ceiling: 4, session_ceiling: 5}\n",
        "budget.session_ceiling",
        "budget: {ceiling: 4}\n",
        id="last-flow-entry",
    ),
    pytest.param(
        "connector: duckdb\nbudget: {ceiling: 4}\n",
        "budget.ceiling",
        "connector: duckdb\n",
        id="only-flow-entry-removes-its-key",
    ),
    pytest.param("connector: duckdb\n", "connector", "", id="only-key-in-the-file"),
    pytest.param(
        "a:\r\n  x: 1\r\n\r\nbudget:\r\n  y: 1\r\n\r\nquery:\r\n  z: 1\r\n",
        "budget",
        "a:\r\n  x: 1\r\n\r\nquery:\r\n  z: 1\r\n",
        id="crlf",
    ),
    pytest.param(
        "connector: duckdb\ndbt_target: dev",
        "dbt_target",
        "connector: duckdb\n",
        id="no-trailing-newline",
    ),
]


@pytest.mark.parametrize(("before", "key", "after"), _UNSET_CASES)
def test_an_unset_removes_only_the_bytes_the_setting_occupies(before, key, after):
    assert ConfigDocument(before).without(key).text == after


@pytest.mark.parametrize(
    ("text", "key"),
    [
        pytest.param(
            "budget: &b\n  ceiling: 5\nother: *b\n", "budget.ceiling", id="anchor"
        ),
        pytest.param("budget:\n\tceiling: 5\n", "budget.ceiling", id="tabs"),
        pytest.param("connector: a\nconnector: b\n", "connector", id="duplicated-key"),
        pytest.param("budget: [\n", "budget", id="does-not-parse"),
    ],
)
def test_a_removal_dex_cannot_span_is_refused_rather_than_rewritten(text, key):
    with pytest.raises(ConfigEditError, match="by hand"):
        ConfigDocument(text).without(key)


def test_the_nearest_written_key_is_named_for_one_the_file_does_not_write():
    document = ConfigDocument(COMMENTED)
    assert document.closest_written("budget.session_celling") == (
        "budget.session_celing"
    )
    assert document.closest_written("nothing.like.it") is None


# --- what a reviewer is shown -----------------------------------------------------

_COMMITTED_CREDENTIAL = (
    "connector: duckdb\ncache:\n  options:\n    api_token: hunter2   # oops\n"
    "query:\n  max_rows: 50\n"
)


def test_a_diff_withholds_a_credential_in_its_context_lines():
    before = ConfigDocument(_COMMITTED_CREDENTIAL)
    diff = before.with_value("query.max_rows", 5).diff_from(before)
    assert "hunter2" not in diff["unified"]
    assert "    api_token: <withheld>   # oops\n" in diff["unified"]
    assert (diff["additions"], diff["deletions"]) == (1, 1)


def test_a_diff_withholds_a_credential_it_removes():
    before = ConfigDocument(_COMMITTED_CREDENTIAL)
    diff = before.without("cache.options.api_token").diff_from(before)
    assert "hunter2" not in diff["unified"]
    assert "-    api_token: <withheld>   # oops\n" in diff["unified"]
    assert (diff["additions"], diff["deletions"]) == (0, 3)


def test_a_credential_block_hides_its_values_and_keeps_its_keys_and_lines():
    text = (
        "snowflake:\n  warehouse: wh\n  credentials:\n    user: a\n"
        "    hosts: [h1, 'h2']\n    password: |\n      x-line\n      y-line\n"
    )
    before = ConfigDocument(text)
    context = before.with_value("snowflake.warehouse", "other").diff_from(before)
    assert "@@ -1,5 +1,5 @@" in context["unified"]
    assert "     user: <withheld>\n" in context["unified"]
    assert "     hosts: [<withheld>, <withheld>]\n" in context["unified"]
    removed = before.without("snowflake.credentials").diff_from(before)
    # A value spanning lines keeps them, so the counts are the file's own.
    assert removed["deletions"] == 6
    assert "-    password: <withheld>\n" in removed["unified"]
    for hidden in ("user: a", "h1", "h2", "x-line", "y-line"):
        assert hidden not in context["unified"] + removed["unified"]


def test_a_diff_with_no_credential_is_the_plain_diff():
    from exmergo_dex_core.diffs import file_diff

    before = ConfigDocument(COMMENTED)
    after = before.with_value("query.max_rows", 5)
    assert after.diff_from(before) == file_diff(
        ".dex/config.yml", before.text, after.text
    )


# --- what a change reports --------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "key", "rows"),
    [
        pytest.param(
            "query:\n  max_rows: 50\n",
            "query:\n  max_rows: 5\n",
            "query.max_rows",
            [{"key": "query.max_rows", "before": 50, "after": 5}],
            id="scalar",
        ),
        pytest.param(
            "connector: duckdb\n",
            "connector: duckdb\nquery:\n  max_rows: 5\n",
            "query.max_rows",
            [{"key": "query.max_rows", "after": 5}],
            id="added",
        ),
        pytest.param(
            "bigqeury:\n  project: p\n  rows: [{a: 1}]\n",
            "",
            "bigqeury",
            [
                {"key": "bigqeury.project", "before": "p"},
                {"key": "bigqeury.rows[0].a", "before": 1},
            ],
            id="removed-block-flattened",
        ),
        pytest.param(
            "cache:\n  options:\n    api_token: hunter2\n    region: eu\n",
            "cache:\n  options:\n    region: us\n",
            "cache.options",
            [
                {"key": "cache.options.api_token", "withheld": True},
                {"key": "cache.options.region", "before": "eu", "after": "us"},
            ],
            id="credential-withheld",
        ),
        pytest.param(
            "bigquery:\n  datasets: [a]\n",
            "bigquery:\n  datasets: [a, b]\n",
            "bigquery.datasets",
            [{"key": "bigquery.datasets", "before": ["a"], "after": ["a", "b"]}],
            id="list-of-scalars-is-one-value",
        ),
        pytest.param(
            "pii_overrides:\n  - column: a.b.c\n",
            "pii_overrides:\n  - column: a.b.c\n  - column: x.y.z\n",
            "pii_overrides",
            [{"key": "pii_overrides[1].column", "after": "x.y.z"}],
            id="list-of-mappings-by-item",
        ),
        pytest.param(
            "auto_profile: 1\n",
            "auto_profile: true\n",
            "auto_profile",
            [{"key": "auto_profile", "before": 1, "after": True}],
            id="one-is-not-true",
        ),
        pytest.param(
            "cluster:\n  sample_seed: null\n",
            "",
            "cluster.sample_seed",
            [{"key": "cluster.sample_seed", "before": None}],
            id="explicit-null-removed",
        ),
    ],
)
def test_a_change_reports_one_row_per_value(before, after, key, rows):
    assert ConfigDocument(after).changes_from(ConfigDocument(before), [key]) == rows


def test_read_and_write_keep_line_endings_and_permissions(tmp_path: Path):
    path = tmp_path / "config.yml"
    path.write_bytes(b"connector: duckdb\r\nquery:\r\n  max_rows: 1\r\n")
    path.chmod(0o640)
    document = ConfigDocument.read(path).with_value("query.max_rows", 2)
    document.write(path)
    assert path.read_bytes() == b"connector: duckdb\r\nquery:\r\n  max_rows: 2\r\n"
    assert path.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["config.yml"]


# --- reading a value the way the schema reads it ----------------------------------


@pytest.mark.parametrize(
    ("key", "text", "expected"),
    [
        ("dbt_target", "no", "no"),
        ("dbt_target", "dev", "dev"),
        ("snowflake.dev_schema", "123", "123"),
        ("budget.ceiling", "5", 5),
        ("budget.ceiling", "1e12", 1e12),
        ("cluster.sample_seed", "null", None),
        ("bigquery.datasets", "[raw, staging]", ["raw", "staging"]),
        ("auto_profile", "false", False),
        ("cache.options.region", "eu", "eu"),
    ],
)
def test_a_value_is_read_the_way_its_field_reads_it(key, text, expected):
    parsed = parse_value(key, text)
    assert parsed == expected
    assert type(parsed) is type(expected)


@pytest.mark.parametrize(
    ("key", "match"),
    [
        (
            "budget",
            "set its keys one at a time: budget.ceiling, budget.session_ceiling, "
            "budget.session_ceiling_declined",
        ),
        ("budget.celing", "did you mean budget.ceiling"),
        ("session_ceiling", "did you mean budget.session_ceiling"),
        ("pii_overrides[0].column", "a list is set as a whole"),
        ("cache.options.api_token", "reads as a credential"),
        ("budget.ceiling.cap", "holds a single value"),
        ("budget..ceiling", "not a setting key"),
    ],
)
def test_a_key_dex_cannot_set_is_refused_by_name(key, match):
    with pytest.raises(RequestError, match=match):
        parse_value(key, "1")


@pytest.mark.parametrize(
    ("key", "text", "named"),
    [
        ("cache.options", "{api_token: hunter2}", "api_token"),
        ("cache.options", "{auth: {client_secret: hunter2}}", "client_secret"),
        ("pii_overrides", "[{column: a.b.c, secret_note: hunter2}]", "secret_note"),
    ],
)
def test_a_value_carrying_a_credential_is_refused_without_repeating_it(
    key, text, named
):
    with pytest.raises(RequestError, match="reads as a credential") as refused:
        parse_value(key, text)
    assert named in str(refused.value)
    assert "hunter2" not in str(refused.value)


# --- problems ---------------------------------------------------------------------


def _problems(text: str) -> list[tuple]:
    return [
        (p.kind, p.key, p.line, p.suggestion, p.fatal)
        for p in ConfigDocument(text).problems()
    ]


def test_an_unknown_key_is_located_and_named_with_the_key_it_was_meant_to_be():
    assert _problems(COMMENTED) == [
        ("unknown_key", "budget.session_celing", 6, "budget.session_ceiling", False)
    ]


def test_a_key_at_the_wrong_level_is_pointed_at_where_it_belongs():
    assert _problems("session_ceiling: 5\n") == [
        ("unknown_key", "session_ceiling", 1, "budget.session_ceiling", False)
    ]


def test_a_key_inside_a_list_item_is_checked_against_the_item_schema():
    problems = _problems("pii_overrides:\n  - colum: a.b.c\n")
    assert (
        "unknown_key",
        "pii_overrides[0].colum",
        2,
        "pii_overrides[0].column",
        False,
    ) in problems
    # And the item itself is invalid, since it names no column at all.
    assert ("invalid", "pii_overrides[0]", 2, None, True) in problems


def test_a_key_written_twice_is_reported_because_yaml_keeps_only_the_last():
    (problem,) = ConfigDocument("connector: a\nconnector: b\n").problems()
    assert (problem.kind, problem.key, problem.line, problem.fatal) == (
        "duplicate_key",
        "connector",
        2,
        False,
    )
    assert "line 2 wins" in problem.message


def test_a_value_the_schema_refuses_is_fatal_and_says_what_it_got():
    (problem,) = ConfigDocument("query:\n  max_rows: lots\n").problems()
    assert (problem.kind, problem.key, problem.line, problem.fatal) == (
        "invalid",
        "query.max_rows",
        2,
        True,
    )
    assert "(got 'lots')" in problem.message


def test_a_syntax_error_is_one_fatal_problem_with_its_line():
    (problem,) = ConfigDocument("connector: duckdb\nbudget: [\n").problems()
    assert problem.kind == "syntax"
    assert problem.fatal
    assert problem.line == 3


def test_a_document_that_is_not_a_mapping_is_refused():
    (problem,) = ConfigDocument("- a\n").problems()
    assert problem.kind == "invalid"
    assert "mapping of settings" in problem.message


def test_a_credential_like_key_is_flagged_and_its_value_never_repeated():
    (problem,) = ConfigDocument(
        "cache:\n  options:\n    api_token: hunter2\n"
    ).problems()
    assert (problem.kind, problem.key, problem.line, problem.fatal) == (
        "secret_like_key",
        "cache.options.api_token",
        3,
        False,
    )
    assert "hunter2" not in problem.describe()


def _pinned(root: Path, text: str) -> list[tuple]:
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(text, encoding="utf-8")
    document = ConfigDocument.read(root / ".dex" / "config.yml")
    return [(p.kind, p.key, p.line, p.fatal, p.describe()) for p in document.problems()]


def test_a_pin_to_a_missing_directory_is_reported_on_its_line(tmp_path: Path):
    assert _pinned(tmp_path, "connector: duckdb\ndbt_project_dir: no_such_dir\n") == [
        (
            "project_not_found",
            "dbt_project_dir",
            2,
            False,
            "line 2: dbt_project_dir names no_such_dir, which holds no dbt_project.yml",
        )
    ]


def test_a_pin_to_a_directory_without_dbt_project_yml_is_reported(tmp_path: Path):
    (tmp_path / "transform").mkdir()
    (problem,) = _pinned(tmp_path, "dbt_project_dir: transform\n")
    assert problem[:4] == ("project_not_found", "dbt_project_dir", 1, False)


def test_a_pin_to_a_real_project_or_no_pin_is_not_a_problem(tmp_path: Path):
    (tmp_path / "transform").mkdir()
    (tmp_path / "transform" / "dbt_project.yml").write_text("name: things\n")
    assert _pinned(tmp_path, "dbt_project_dir: transform\n") == []
    (tmp_path / ".dex" / "config.yml").write_text("connector: duckdb\n")
    assert ConfigDocument.read(tmp_path / ".dex" / "config.yml").problems() == []


def test_config_refuses_a_fatal_problem_by_setting_and_line():
    document = ConfigDocument("connector: duckdb\nquery:\n  max_rows: lots\n")
    with pytest.raises(InvalidConfigError) as raised:
        document.config()
    message = str(raised.value)
    assert "line 3: query.max_rows" in message
    assert "dex config validate" in message
    assert isinstance(raised.value, ConfigurationError)
    assert isinstance(raised.value, ValueError)


def test_a_non_fatal_problem_still_loads_and_becomes_a_warning():
    document = ConfigDocument(COMMENTED)
    assert document.config().budget.ceiling == 2_000_000_000
    assert document.warnings() == [
        ".dex/config.yml line 6: budget.session_celing is not a setting dex reads, "
        "so it is ignored; did you mean budget.session_ceiling?"
    ]


# --- effective settings -----------------------------------------------------------

_SOURCED = """\
semantic:
  backend: api
  host: from-the-file.example
budget:
  ceiling: 5
cache:
  options:
    region: eu
    api_token_env: DEX_STORE_TOKEN
"""


def _rows(text: str, key: str | None = None, **environ: str) -> dict[str, dict]:
    settings = ConfigDocument(text).settings(key, environ=environ)
    return {setting.key: setting.data() for setting in settings}


def test_every_setting_says_where_its_value_came_from():
    rows = _rows(_SOURCED, DBT_SL_HOST="from-the-env.example")
    assert rows["budget.ceiling"] == {
        "key": "budget.ceiling",
        "source": "file",
        "value": 5.0,
        "line": 5,
    }
    assert rows["budget.session_ceiling"] == {
        "key": "budget.session_ceiling",
        "source": "default",
        "value": None,
    }
    # Read as the canonical spelling, and the file's own spelling kept beside it.
    assert rows["semantic.backend"]["value"] == "dbt_cloud"
    assert rows["semantic.backend"]["as_written"] == "api"
    # Never written, but computed by the validator from `backend`.
    assert rows["semantic.deployment"]["source"] == "derived"
    assert rows["semantic.deployment"]["value"] == "dbt_cloud"
    assert rows["semantic.host"] == {
        "key": "semantic.host",
        "source": "environment",
        "value": "from-the-env.example",
        "line": 3,
        "as_written": "from-the-file.example",
        "variable": "DBT_SL_HOST",
    }
    assert rows["cache.options.region"]["value"] == "eu"
    assert rows["cache.options.api_token_env"] == {
        "key": "cache.options.api_token_env",
        "source": "file",
        "withheld": True,
        "line": 9,
    }
    assert rows["bigquery"] == {"key": "bigquery", "source": "default", "value": None}


def test_a_key_narrows_the_rows_to_its_own():
    assert set(_rows(_SOURCED, "budget")) == {
        "budget.ceiling",
        "budget.session_ceiling",
        "budget.session_ceiling_declined",
    }


def test_a_setting_of_an_undeclared_section_reads_as_its_default():
    assert _rows(_SOURCED, "bigquery.datasets") == {
        "bigquery.datasets": {
            "key": "bigquery.datasets",
            "source": "default",
            "value": [],
        }
    }


def test_settings_refuse_a_key_dex_does_not_read():
    with pytest.raises(RequestError, match=r"did you mean budget\.ceiling"):
        ConfigDocument(_SOURCED).settings("budget.celing", environ={})


def test_settings_refuse_a_file_no_command_could_load():
    with pytest.raises(InvalidConfigError):
        ConfigDocument("query:\n  max_rows: lots\n").settings(environ={})


def test_written_tells_an_absent_key_from_one_written_as_null():
    document = ConfigDocument(COMMENTED)
    assert document.written("cluster.sample_seed") is None
    assert document.written("budget.session_ceiling") is ABSENT
    assert document.written("query.max_rows") == 50
