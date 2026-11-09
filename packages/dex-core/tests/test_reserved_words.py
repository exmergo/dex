"""Reserved-word column names per connector dialect (#651)."""

from __future__ import annotations

from exmergo_dex_core.reserved_words import reserved_column_names


def test_flags_reserved_words_in_any_case_and_keeps_order():
    names = ["id", "WINDOW", "order", "created_at"]
    assert reserved_column_names("duckdb", names) == ["WINDOW", "order"]


def test_ignores_keywords_the_dialect_does_not_reserve():
    # `date` is a sqlglot tokenizer keyword but a legal bare column name.
    assert reserved_column_names("duckdb", ["date", "name", "value"]) == []


def test_a_dialect_without_a_list_flags_nothing():
    assert reserved_column_names("postgres", ["window", "order"]) == []
