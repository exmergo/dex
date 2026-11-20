"""Each test a declaration renders names the claim it checks (#1448).

The id sits at ``config.meta.dex.claim`` on the test. It is derived from the
declared fact and never from the test's name, written once, and kept as written
after that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.transform.claims import claim_id, claim_of, without_claim
from exmergo_dex_core.transform.declarations import parse_declarations, render
from exmergo_dex_core.transform.rewrite import (
    column_tests,
    column_tests_span,
    splice,
)

DECLARATION = {
    "model": "fct_orders",
    "grain": ["order_id"],
    "columns": {
        "customer_id": {
            "role": "foreign_key",
            "null_rule": "never",
            "references": "dim_customers.customer_id",
        }
    },
}


def _render(content: str | None, payload: dict = DECLARATION) -> str:
    [declaration] = parse_declarations([payload])
    return render(content, declaration)


def _tests(content: str, column: str) -> list:
    [entry] = yaml.safe_load(content)["models"]
    found = next(c for c in entry["columns"] if c["name"] == column)
    return found.get("data_tests") or found["tests"]


def _claims(content: str, column: str) -> list[str | None]:
    return [claim_of(test) for test in _tests(content, column)]


# --- the derivation -----------------------------------------------------------


def test_the_id_is_a_short_stable_hash_of_the_fact():
    first = claim_id("fct_orders", "grain", ["order_id"], {"check": "unique"})

    assert len(first) == 16 and int(first, 16) >= 0
    assert first == claim_id("fct_orders", "grain", ["order_id"], {"check": "unique"})
    # A respelling of the same names states the same fact.
    assert first == claim_id(" FCT_Orders", "grain", ["Order_ID"], {"check": "unique"})


def test_every_part_of_the_fact_separates_two_claims():
    base = claim_id("fct_orders", "grain", ["order_id"], {"check": "unique"})

    assert base != claim_id("fct_orders", "grain", ["order_id"], {"check": "not_null"})
    assert base != claim_id(
        "fct_orders", "null_rule", ["order_id"], {"check": "unique"}
    )
    assert base != claim_id("fct_orders", "grain", ["customer_id"], {"check": "unique"})
    assert base != claim_id("dim_orders", "grain", ["order_id"], {"check": "unique"})
    assert base != claim_id("fct_orders.v2", "grain", ["order_id"], {"check": "unique"})


def test_without_claim_restores_the_render_before_claims():
    claimed = {
        "not_null": {
            "config": {"meta": {"disable_test_samples": True, "dex": {"claim": "ab"}}}
        }
    }
    other = {"not_null": {"config": {"meta": {"dex": {"claim": "ab", "note": "x"}}}}}

    assert without_claim(claimed) == {
        "not_null": {"config": {"meta": {"disable_test_samples": True}}}
    }
    # A `dex` key holding anything else keeps it.
    assert without_claim(other) == {
        "not_null": {"config": {"meta": {"dex": {"note": "x"}}}}
    }
    assert without_claim("not_null") == "not_null"
    assert claim_of(claimed) == "ab"
    assert claim_of({"not_null": {"meta": {"dex": {"claim": "cd"}}}}) == "cd"
    assert claim_of("not_null") is None


# --- rendering ----------------------------------------------------------------


def test_each_rendered_test_carries_the_claim_of_the_fact_it_checks():
    out = _render(None)

    assert _claims(out, "order_id") == [
        claim_id("fct_orders", "grain", ["order_id"], {"check": "unique"}),
        claim_id("fct_orders", "grain", ["order_id"], {"check": "not_null"}),
    ]
    assert _claims(out, "customer_id") == [
        claim_id("fct_orders", "null_rule", ["customer_id"], {"null_rule": "never"}),
        claim_id(
            "fct_orders",
            "references",
            ["customer_id"],
            {"references": "dim_customers.customer_id"},
        ),
    ]
    # Beside Elementary's flag, under the `dex` key, never at the test's top.
    [not_null, _relationships] = _tests(out, "customer_id")
    assert set(not_null["not_null"]) == {"config"}
    assert set(not_null["not_null"]["config"]["meta"]) == {
        "disable_test_samples",
        "dex",
    }


def test_two_facts_on_one_column_get_two_claims():
    out = _render(None)

    for column in ("order_id", "customer_id"):
        claims = _claims(out, column)
        assert len(set(claims)) == len(claims) == 2


def test_re_rendering_an_unchanged_declaration_changes_no_byte():
    once = _render(None)

    assert _render(once) == once


def test_a_renamed_test_keeps_its_claim():
    # dbt's test name moves with its `name:` (and with the model, the column and
    # the arguments); the claim was never derived from it, and the test Dex finds
    # listed by name is left exactly as written.
    once = _render(None)
    renamed = once.replace(
        "          - unique:\n              config:\n",
        "          - unique:\n              name: orders_are_one_row_each\n"
        "              config:\n",
    )
    assert renamed != once

    again = _render(renamed)

    assert again == renamed
    assert _claims(again, "order_id")[0] == _claims(once, "order_id")[0]


def test_an_edited_declaration_keeps_the_claim_already_written():
    # Adding a description re-renders the entry; every claim stays.
    once = _render(None)
    edited = {**DECLARATION, "description": "One row per placed order."}

    again = _render(once, edited)

    assert again != once
    for column in ("order_id", "customer_id"):
        assert _claims(again, column) == _claims(once, column)


def test_a_test_dex_rendered_before_claims_gains_its_claim_and_nothing_else():
    # The render #1348 wrote, block and flow: the flag and nothing else.
    before = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        data_tests:\n"
        "          - unique:\n"
        "              config:\n"
        "                meta:\n"
        "                  disable_test_samples: true\n"
        "          - not_null  # mine\n"
        "      - name: customer_id\n"
        "        data_tests: [{not_null: {config: {meta: {disable_test_samples: "
        "true}}}}]  # flow\n"
    )
    payload = {
        "model": "fct_orders",
        "grain": ["order_id"],
        "columns": {"customer_id": {"null_rule": "never"}},
    }

    out = _render(before, payload)

    assert _claims(out, "order_id") == [
        claim_id("fct_orders", "grain", ["order_id"], {"check": "unique"}),
        # A bare test cannot be told from a hand-written one (#1350): kept bare.
        None,
    ]
    assert "          - not_null  # mine\n" in out
    assert _claims(out, "customer_id") == [
        claim_id("fct_orders", "null_rule", ["customer_id"], {"null_rule": "never"})
    ]
    assert "}}}}}]  # flow\n" in out
    # The upgrade added only the claim: the rest of the test is as it was.
    assert [without_claim(t) for t in _tests(out, "order_id")] == _tests(
        before, "order_id"
    )
    assert _render(out, payload) == out


def test_a_test_carrying_its_own_config_or_claim_is_left_as_written():
    configured = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        data_tests:\n"
        "          - unique:\n"
        "              config:\n"
        "                severity: warn\n"
        "                meta:\n"
        "                  disable_test_samples: true\n"
        "          - not_null:\n"
        "              config:\n"
        "                meta:\n"
        "                  disable_test_samples: true\n"
        "                  dex:\n"
        "                    claim: set-by-hand\n"
    )

    out = _render(configured, {"model": "fct_orders", "grain": ["order_id"]})

    assert _claims(out, "order_id") == [None, "set-by-hand"]
    assert out.split("    columns:\n")[1] == configured.split("    columns:\n")[1]


def test_maintain_reconcile_still_moves_a_claimed_not_null():
    # A rendered test with a claim is still the shape Dex owns: reconcile can
    # remove the not_null when the column starts accepting nulls (#1306).
    out = _render(None, {"model": "fct_orders", "grain": ["order_id"]})
    current = column_tests(out, "fct_orders", "order_id")
    assert current is not None and set(current.spans) == {"unique", "not_null"}

    span = column_tests_span(out, current, ["unique"], key="data_tests")
    assert span is not None
    removed = splice(out, [span])

    assert [next(iter(t)) for t in _tests(removed, "order_id")] == ["unique"]


# --- end to end ---------------------------------------------------------------


def _dex(argv: list[str], capsys) -> dict:
    main(argv)
    return json.loads(capsys.readouterr().out)


def test_a_plan_that_re_renders_an_entry_shows_no_diff_on_an_unchanged_claim(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    def plan(declaration: dict) -> dict:
        payload = tmp_path / "payload.json"
        payload.write_text(json.dumps({"declarations": [declaration]}))
        return _dex(
            [
                "--repo-root",
                str(tmp_path),
                "transform",
                "plan",
                "declare stg_customers",
                "--edits-file",
                str(payload),
            ],
            capsys,
        )

    declaration = {"model": "stg_customers", "grain": ["id"]}
    first = plan(declaration)
    assert first["status"] == "ok", first
    applied = _dex(
        ["--repo-root", str(tmp_path), "transform", "apply", first["data"]["plan_id"]],
        capsys,
    )
    assert applied["status"] == "ok", applied
    written = (dbt_project_dir / "models/staging/schema.yml").read_text()
    # The column's own bare not_null stays bare; the unique Dex added is claimed.
    assert "claim: " in written

    second = plan({**declaration, "description": "One row per customer."})

    assert second["status"] == "ok", second
    [diff] = second["diffs"]
    changed = [
        line
        for line in diff["unified"].splitlines()
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]
    assert changed and not any("claim" in line for line in changed)


def test_a_test_whose_yaml_another_test_aliases_is_left_as_written():
    # A claim written into a shared anchor would land on both tests.
    shared = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        data_tests:\n"
        "          - unique: &rendered\n"
        "              config:\n"
        "                meta:\n"
        "                  disable_test_samples: true\n"
        "          - not_null: *rendered\n"
    )

    out = _render(shared, {"model": "fct_orders", "grain": ["order_id"]})

    assert _claims(out, "order_id") == [None, None]
    assert out.split("    columns:\n")[1] == shared.split("    columns:\n")[1]


SHARED_LIST = (
    "version: 2\n"
    "models:\n"
    "  - name: fct_orders\n"
    "    columns:\n"
    "      - name: order_id\n"
    "        data_tests: &keys\n"
    "          - unique:\n"
    "              config:\n"
    "                meta:\n"
    "                  disable_test_samples: true\n"
    "      - name: order_key\n"
    "        data_tests: *keys\n"
    "      - name: order_ref\n"
    "        data_tests: *keys\n"
)


@pytest.mark.parametrize("column", ["order_id", "order_key", "order_ref"])
def test_a_test_added_to_a_list_columns_share_lands_on_the_declared_column_only(
    column: str,
):
    # The list is aliased: the declared column gets its own copy, whether it
    # holds the anchor or an alias, and the others parse to the same tests (#1496).
    out = _render(SHARED_LIST, {"model": "fct_orders", "grain": [column]})

    others = {"order_id", "order_key", "order_ref"} - {column}
    for other in others:
        assert _tests(out, other) == _tests(SHARED_LIST, other)
    declared = _tests(out, column)
    assert [next(iter(test)) for test in declared] == ["unique", "not_null"]
    assert all(claim_of(test) for test in declared)
