"""Anchors and aliases are read from the parse, never from the text (#1255)."""

from __future__ import annotations

import pytest
import yaml

from exmergo_dex_core.yaml_spans import uses_anchors_or_aliases


@pytest.mark.parametrize(
    "text",
    [
        "# see the *column* descriptions\nsemantic_models: []\n",
        "metrics:\n  - name: m\n    description: counts *active* users\n",
        "metrics:\n  - name: m\n    description: '*quoted* &kept'\n",
        "note: a & b, a * b\n",
        "# &comment_anchor and *comment_alias\nkey: value\n",
        "",
    ],
    ids=["comment", "plain_scalar", "quoted", "operators", "comment_tokens", "empty"],
)
def test_text_that_only_looks_like_an_alias_is_not_one(text: str):
    """Emphasis, operators and comment tokens are text; only the parser says alias."""

    assert uses_anchors_or_aliases(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "a: &x 1\nb: *x\n",
        "base: &b {k: 1}\nm:\n  <<: *b\n",
        "a: &unused 1\n",
        "- &item {name: m}\n",
    ],
    ids=["alias", "merge_key", "anchor_only", "anchored_item"],
)
def test_a_real_anchor_or_alias_is_found(text: str):
    """Every anchor and alias form counts, a merge key and an unused anchor included."""

    assert uses_anchors_or_aliases(text) is True


def test_a_document_that_does_not_parse_is_the_callers_to_name():
    """A parse error propagates, so each caller words its own refusal."""

    with pytest.raises(yaml.YAMLError):
        uses_anchors_or_aliases("a: [1\n")
