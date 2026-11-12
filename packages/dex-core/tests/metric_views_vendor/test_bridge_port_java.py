"""The Java behaviours dex's port of the converter restates (#1260)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from exmergo_dex_core.metric_views.ossie_bridge import ConversionError, Kind
from exmergo_dex_core.metric_views.ossie_bridge._java import (
    JavaHashSet,
    dumps_stash,
    get_simple_name,
    is_whitespace,
    java_double,
    java_str,
    split_dropping_trailing,
    trim,
)
from exmergo_dex_core.metric_views.ossie_bridge.common import load_yaml, parse_yaml


def test_hash_set_iterates_by_bucket_then_insertion():
    # Single characters hash to their code point: buckets 1, 2, 3 of 16.
    assert list(JavaHashSet(["c", "b", "a"])) == ["a", "b", "c"]
    # "Aa" and "BB" share String.hashCode 2112, so they keep insertion order.
    assert list(JavaHashSet(["BB", "Aa"])) == ["BB", "Aa"]
    assert list(JavaHashSet(["Aa", "BB"])) == ["Aa", "BB"]


def test_hash_set_order_survives_a_resize():
    names = [f"n{i}" for i in range(40)]
    ordered = list(JavaHashSet(names))
    assert sorted(ordered) == sorted(names)
    assert len(ordered) == 40
    # Capacity is 64 by now; every name's bucket is ascending in the order.
    from exmergo_dex_core.metric_views.ossie_bridge._java import _spread, _string_hash

    buckets = [_spread(_string_hash(n)) & 63 for n in ordered]
    assert buckets == sorted(buckets)


def test_trim_strips_controls_and_spaces_only():
    assert trim("\t a \x00") == "a"
    assert trim("\u00a0a\u00a0") == "\u00a0a\u00a0"


def test_whitespace_excludes_the_no_break_spaces():
    assert is_whitespace(" ") and is_whitespace("\u2003") and is_whitespace("\x1f")
    assert not is_whitespace("\u00a0") and not is_whitespace("\u202f")
    assert not is_whitespace("\x85")


def test_split_drops_trailing_empty_strings_only():
    assert split_dropping_trailing(["", "a", "", ""]) == ["", "a"]


@pytest.mark.parametrize(
    ("value", "text"),
    [(1.0, "1.0"), (0.001, "0.001"), (1e-4, "1.0E-4"), (1e7, "1.0E7"), (-2.5, "-2.5")],
)
def test_doubles_print_as_java_does(value, text):
    assert java_double(value) == text


def test_to_string_and_simple_names_follow_java():
    assert java_str(True) == "true"
    assert java_str({"a": [1, 2.0]}) == "{a=[1, 2.0]}"
    assert java_str(None) is None
    assert [get_simple_name(v) for v in (5, 2**40, 2**70, 1.5, True, {}, [])] == [
        "Integer",
        "Long",
        "BigInteger",
        "Double",
        "Boolean",
        "LinkedHashMap",
        "ArrayList",
    ]


def test_the_stash_writer_matches_jackson_bytes():
    assert dumps_stash({"_v": 1, "f": "caf\u00e9"}) == '{"_v": 1, "f": "caf\\u00e9"}'
    # DEL is written raw; other controls are escaped, in the short form where
    # JSON has one.
    assert dumps_stash("\x7f\x01\n\t") == '"\x7f\\u0001\\n\\t"'
    # An astral character is two escaped surrogates.
    assert dumps_stash("\U0001f600") == '"\\ud83d\\ude00"'
    assert dumps_stash([1.0, 1e20, None, False]) == "[1.0, 1.0E20, null, false]"
    assert dumps_stash(float("nan")) == '"NaN"'


def test_the_reader_keeps_yaml_1_2_booleans_and_refuses_duplicates():
    assert load_yaml("on: yes\nb: true\n") == {"on": "yes", "b": True}
    with pytest.raises(ConversionError) as caught:
        parse_yaml("a: 1\na: 2\n")
    assert caught.value.kind is Kind.INVALID_INPUT
    assert "found duplicate key a" in caught.value.reason


def test_the_reader_refuses_an_alias_bomb():
    lines = ["a0: &a0 [x]"] + [
        f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 30)
    ]
    with pytest.raises(ConversionError) as caught:
        parse_yaml("\n".join(lines))
    assert "max=50" in caught.value.reason


def test_the_reader_refuses_a_recursive_alias():
    with pytest.raises(ConversionError) as caught:
        parse_yaml("a: &a [*a]\n")
    assert "recursive alias" in caught.value.reason


def test_the_reader_resolves_scalars_as_the_metric_view_reader_does():
    from exmergo_dex_core.metric_views.loader import read_yaml

    text = "a: 1\nb: 1.10\nc: on\nd: '1.1'\ne: 2024-01-15\nf: ~\ng: 0x1f\nh: 1_000\n"
    assert load_yaml(text) == read_yaml(text)


def test_the_port_needs_no_sqlglot_and_no_jsonschema():
    """The bridge runs under either target's extra alone."""
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('sqlglot', 'jsonschema'):\n"
        "            raise ImportError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "from exmergo_dex_core.metric_views.ossie_bridge.adapt import (\n"
        "    export_document, import_view)\n"
        "view = {'version': '1.1', 'source': 'c.s.t', 'joins': [{'name': 'd',"
        " 'source': 'c.s.d', 'using': ['k']}],"
        " 'dimensions': [{'name': 'x', 'expr': 'x'}]}\n"
        "doc = import_view(view).value\n"
        "assert export_document(doc).value == view, export_document(doc).value\n"
        "print('ok')\n"
    )
    done = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert done.stdout.strip() == "ok", done.stderr


def test_shared_aliases_are_walked_once():
    """48 aliases fanning out three ways would be 3^16 paths walked per path."""
    import time

    lines = ["a0: &a0 [x]"] + [
        f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}, *a{i - 1}]" for i in range(1, 17)
    ]
    started = time.monotonic()
    parse_yaml("\n".join(lines))
    assert time.monotonic() - started < 5
