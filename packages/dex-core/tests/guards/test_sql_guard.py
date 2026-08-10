"""Cutting a file of SQL into statements: where one ends and the next begins,
which line each started on, and what the two spellings cost when they are wrong.

`assert_select_only` and `referenced_relations` are covered where the commands
that lean on them are; this file is about `split_statements`, whose failure mode
is silent (a statement split in the wrong place still runs) rather than loud.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.errors import RequestError
from exmergo_dex_core.guards.sql_guard import split_statements

# --- semicolon-separated files ---


def test_semicolons_separate_statements_and_carry_their_start_line():
    text = "select 1;\nselect 2\nfrom t;\n"
    assert split_statements(text) == [("select 1", 1), ("select 2\nfrom t", 2)]


def test_a_trailing_statement_without_a_semicolon_is_still_a_statement():
    assert split_statements("select 1;\nselect 2") == [("select 1", 1), ("select 2", 2)]


def test_a_semicolon_inside_a_string_literal_is_not_a_boundary():
    # The whole reason this goes through the tokenizer rather than str.split(";"):
    # a split here would hand the firewall two fragments, neither of which parses.
    assert split_statements("select ';' as x; select 2") == [
        ("select ';' as x", 1),
        ("select 2", 1),
    ]


def test_a_semicolon_inside_a_comment_is_not_a_boundary():
    assert split_statements("select 1 /* ; */ as x;") == [("select 1 /* ; */ as x", 1)]


def test_an_unparseable_statement_in_semicolon_mode_is_left_to_the_caller():
    # Boundaries are unambiguous here, so a bad statement is one statement's
    # problem: the firewall refuses it and its neighbors still run. Refusing the
    # whole file would take the neighbors down with it.
    assert split_statements("select 1; not sql at all;") == [
        ("select 1", 1),
        ("not sql at all", 1),
    ]


# --- one statement per line ---


def test_lines_separate_statements_when_the_file_has_no_semicolons():
    assert split_statements("select 1\nselect 2\n") == [
        ("select 1", 1),
        ("select 2", 2),
    ]


def test_blank_and_comment_only_lines_are_skipped_and_do_not_shift_numbering():
    text = "-- a note\nselect 1\n\n/* block */\nselect 2"
    assert split_statements(text) == [("select 1", 2), ("select 2", 5)]


def test_a_continuation_line_is_refused_naming_the_line_and_the_fix():
    # `from orders` parses as a complete query on DuckDB, so without this guard a
    # statement split across two unterminated lines becomes two statements that
    # both run, and the second answers a question nobody asked.
    with pytest.raises(RequestError) as excinfo:
        split_statements("select count(*)\nfrom orders")
    assert "line 2" in str(excinfo.value)
    assert "terminate statements with `;`" in str(excinfo.value)


def test_a_parenthesized_set_operation_still_begins_a_statement():
    assert split_statements("(select 1) union (select 2)") == [
        ("(select 1) union (select 2)", 1)
    ]


# --- nothing to cut ---


def test_an_empty_file_is_refused_rather_than_returning_nothing():
    with pytest.raises(RequestError, match="no SQL statements found"):
        split_statements("   \n\n-- only a comment\n")


def test_text_the_tokenizer_cannot_read_is_refused():
    with pytest.raises(RequestError, match="could not read the SQL"):
        split_statements("select 'unterminated")
