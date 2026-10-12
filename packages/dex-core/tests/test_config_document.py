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
        ("budget", "set its keys one at a time: budget.paradigm, budget.ceiling"),
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
        "budget.paradigm",
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
