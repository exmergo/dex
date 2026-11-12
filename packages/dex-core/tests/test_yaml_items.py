"""The surgical YAML splicers, with a format's own reader and writer (#1258).

A Metric View is YAML 1.2, where a join's bare `on:` key is text. The default
reader and writer are PyYAML's YAML 1.1 ones, which read that key as `True` and
write it back as `true:`; a format that is not 1.1 passes its own.
"""

from __future__ import annotations

from exmergo_dex_core.metric_views.loader import Dumper, read_yaml
from exmergo_dex_core.yaml_items import append_item, dump_items, locate

TEXT = (
    "# kept\n"
    "joins:\n"
    "  - name: customer\n"
    "    source: demo.main.customers\n"
    "    on: source.customer_id = customer.id\n"
)


def test_locate_reads_the_item_with_the_loader_it_is_given():
    _start, _end, indent, parsed = locate(TEXT, "customer", "joins", load=read_yaml)
    assert indent == 2
    assert parsed["on"] == "source.customer_id = customer.id"


def test_the_default_loader_is_still_yaml_1_1():
    _start, _end, _indent, parsed = locate(TEXT, "customer", "joins")
    assert True in parsed


def test_append_item_writes_with_the_dumper_it_is_given():
    item = {"name": "nation", "source": "demo.main.nations", "on": "source.n = n.n"}
    out = append_item(TEXT, "joins", item, dumper=Dumper)
    assert out is not None
    assert out.startswith(TEXT)
    assert "    on: source.n = n.n\n" in out
    assert read_yaml(out)["joins"][1]["on"] == "source.n = n.n"


def test_append_item_adds_a_missing_section_with_the_dumper():
    out = append_item(
        "version: 1.1\n", "joins", {"name": "a", "on": "x"}, dumper=Dumper
    )
    assert out == "version: 1.1\njoins:\n  - name: a\n    on: x\n"


def test_dump_items_defaults_to_quoting_what_yaml_1_1_would_misread():
    assert "'on'" in dump_items({"on": "x"})
    assert dump_items({"on": "x"}, dumper=Dumper) == "on: x\n"
