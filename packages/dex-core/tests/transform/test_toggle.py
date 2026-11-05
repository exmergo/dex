"""The toggle contract and its judgment, without dbt (#1113)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from exmergo_dex_core.transform import toggle
from exmergo_dex_core.transform.native_semantic import (
    edits_from_payload,
    payload_from_file,
    plan_payload,
    removal_payload,
)

CONTRACT = {
    "variable": "enable_fees",
    "on_value": True,
    "off_value": False,
    "columns": ["fee_amount"],
}


def _payload(tmp_path: Path, body: dict) -> str:
    """Write ``body`` as a payload file and return its path."""

    path = tmp_path / "payload.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


# --- the contract --------------------------------------------------------------


def test_a_contract_states_both_values() -> None:
    """A contract states both values."""

    variable, contract = toggle.parse_contract(CONTRACT)
    assert variable == "enable_fees"
    assert (contract.on_value, contract.off_value) == (True, False)
    for missing in ("on_value", "off_value"):
        raw = {k: v for k, v in CONTRACT.items() if k != missing}
        with pytest.raises(ValueError, match=missing):
            toggle.parse_contract(raw)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"off_value": True}, "must differ"),
        ({"columns": []}, "columns"),
        ({"columns": ["Fee", "fee"]}, "twice"),
        ({"on_value": [1]}, "scalar"),
        ({"packages": "in_scope"}, "packages"),
        ({"variable": ""}, "variable"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_a_malformed_contract_is_refused(change: dict, message: str) -> None:
    """A malformed contract is refused."""

    with pytest.raises(ValueError, match=message):
        toggle.parse_contract({**CONTRACT, **change})


def test_the_stored_form_round_trips_with_its_producers() -> None:
    """The stored form round trips with its producers."""

    variable, contract = toggle.parse_contract(CONTRACT)
    form = toggle.stored_form(variable, contract)
    form["producers"] = {"fct": ["fee_amount"]}
    assert toggle.contract_of(form) == contract


def test_decisions_name_the_toggle() -> None:
    """Decisions name the toggle."""

    stored = {
        "variable": "enable_fees",
        "assumptions": [
            {"decision": "d", "chosen": "c", "evidence": "request", "column": "x"}
        ],
    }
    assert toggle.decisions(stored) == [
        {
            "toggle": "enable_fees",
            "decision": "d",
            "chosen": "c",
            "evidence": "request",
            "column": "x",
        }
    ]
    assert toggle.decisions(None) == []


@pytest.mark.parametrize(
    ("value", "wanted", "same"),
    [
        (False, False, True),
        ("false", False, True),
        ("False", False, True),
        (0, False, False),
        (1, True, False),
        ("on", "on", True),
    ],
)
def test_values_match_as_yaml_reads_them(value, wanted, same: bool) -> None:
    """Values match as yaml reads them."""

    assert toggle.matches(value, wanted) is same


# --- only `transform plan` reads it ---------------------------------------------


def test_only_the_plan_reads_the_toggle(tmp_path: Path) -> None:
    """Only the plan reads the toggle."""

    body = {"edits": [], "toggle": CONTRACT}
    path = _payload(tmp_path, body)
    _edits, _declarations, raw = plan_payload(path)
    assert raw == CONTRACT
    for reader in (payload_from_file, edits_from_payload, removal_payload):
        with pytest.raises(ValueError, match="only `transform plan` takes a toggle"):
            reader(path)


def test_a_payload_holds_one_contract(tmp_path: Path) -> None:
    """A payload holds one contract."""

    path = _payload(tmp_path, {"edits": [], "toggle": CONTRACT, "retirement": {"x": 1}})
    for reader in (plan_payload, payload_from_file, removal_payload):
        with pytest.raises(ValueError, match="not both"):
            reader(path)


def test_the_plan_still_refuses_a_retirement(tmp_path: Path) -> None:
    """The plan still refuses a retirement."""

    path = _payload(tmp_path, {"edits": [], "retirement": {"retained_value": 1}})
    with pytest.raises(ValueError, match="transform remove var"):
        plan_payload(path)


# --- judging one model ----------------------------------------------------------


def _model(columns: list[str] | None, sql: str = "", reason: str | None = None):
    """A compiled model with the given columns, or none and a reason."""

    model = toggle._Model(sql, None, "shop", "")
    model.columns = columns
    model.reason = reason
    return model


def _branches(on=None, off=None, before=None, *, disabled_off=()):
    """The three compiled branches a model is judged across."""

    out = {}
    for name, models in (("on", on), ("off", off), ("before", before)):
        branch = toggle._Branch()
        branch.models = models or {}
        out[name] = branch
    out["off"].disabled = set(disabled_off)
    return out


def _judge(branches, *, exempt=(), label="m") -> tuple[dict, dict]:
    """Judge one model across ``branches`` and return its record and producers."""

    _variable, contract = toggle.parse_contract(
        {**CONTRACT, "columns": ["fee_amount", "fees"]}
    )
    record = toggle._record(label)
    producers: dict = {}
    toggle._judge_model(
        label,
        record,
        branches,
        {"fee_amount", "fees"},
        set(exempt),
        "duckdb",
        contract,
        producers,
    )
    return record, producers


def _checks(record: dict) -> list[str]:
    """The check codes a record carries, in order."""

    return [c["check"] for c in record["checks"]]


def test_a_model_gated_on_both_values_is_satisfied() -> None:
    """A model gated on both values is satisfied."""

    record, producers = _judge(
        _branches(
            on={"m": _model(["id", "fee_amount"])},
            off={"m": _model(["id"], "select id from t")},
            before={"m": _model(["id", "fee_amount"])},
        )
    )
    assert record["status"] == "satisfied"
    assert record["produces"] == ["fee_amount"]
    assert producers == {"m": ["fee_amount"]}


def test_a_governed_column_left_on_the_off_branch_is_contradicted() -> None:
    """A governed column left on the off branch is contradicted."""

    record, _ = _judge(
        _branches(
            on={"m": _model(["id", "fee_amount"])},
            off={"m": _model(["id", "fee_amount"], "select id, fee_amount from t")},
        )
    )
    assert _checks(record) == ["governed_column_present_when_off"]


def test_a_governed_column_read_under_another_name_is_contradicted() -> None:
    """A governed column read under another name is contradicted."""

    record, _ = _judge(
        _branches(
            on={"m": _model(["id", "net"])},
            off={
                "m": _model(
                    ["id", "net"], "select id, amount - fee_amount as net from t"
                )
            },
        )
    )
    assert _checks(record) == ["governed_column_read_when_off"]


def test_an_exempt_model_may_keep_the_column_off() -> None:
    """An exempt model may keep the column off."""

    record, producers = _judge(
        _branches(
            on={"m": _model(["id", "fee_amount"])},
            off={"m": _model(["id", "fee_amount"], "select id, fee_amount from t")},
        ),
        exempt={"m"},
    )
    assert record["status"] == "satisfied"
    assert record["exempt"] is True
    assert producers == {"m": ["fee_amount"]}


def test_a_column_lost_on_the_on_branch_is_contradicted() -> None:
    """A column lost on the on branch is contradicted."""

    record, _ = _judge(
        _branches(
            on={"m": _model(["id"])},
            off={"m": _model(["id"], "select id from t")},
            before={"m": _model(["id", "fee_amount"])},
        )
    )
    assert _checks(record) == ["governed_column_lost_when_on"]


def test_an_ungoverned_column_lost_or_differing_is_contradicted() -> None:
    """An ungoverned column lost or differing is contradicted."""

    record, _ = _judge(
        _branches(
            on={"m": _model(["id", "region", "fee_amount"])},
            off={"m": _model(["id"], "select id from t")},
            before={"m": _model(["id", "region", "fee_amount"])},
        )
    )
    assert _checks(record) == ["ungoverned_column_lost", "ungoverned_columns_differ"]


def test_an_unreadable_branch_is_unchecked_never_clean() -> None:
    """An unreadable branch is unchecked never clean."""

    record, _ = _judge(
        _branches(
            on={"m": _model(None, reason="a `*` over raw.x")},
            off={"m": _model(["id"], "select id from t")},
        )
    )
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "unreadable_select"
    assert record["branches"]["on"] is None


def test_a_model_disabled_off_holds_nothing_off() -> None:
    """A model disabled off holds nothing off."""

    record, _ = _judge(
        _branches(on={"m": _model(["id", "fee_amount"])}, off={}, disabled_off={"m"})
    )
    assert record["status"] == "satisfied"
    assert record["branches"]["off"] == "disabled"


def test_a_report_with_limits_is_never_satisfied() -> None:
    """A report with limits is never satisfied."""

    clean = [toggle._record("m")]
    assert toggle._report(clean, [], [], 1)["verdict"] == "satisfied"
    limited = toggle._report(clean, ["a dynamic var() read"], [], 1)
    assert limited["verdict"] == "unchecked"
    assert limited["completeness"] == "incomplete"


# --- after a build ---------------------------------------------------------------


class _Adapter:
    def __init__(self, columns: dict[str, list[str]]) -> None:
        """Serve these columns per relation name."""

        self.columns = columns

    def table_metadata(self, identifier: str):
        """The columns of the relation ``identifier`` names, as metadata."""

        name = identifier.split(".")[-1]
        return None, [SimpleNamespace(name=c) for c in self.columns[name]]


def _verify(value, columns, producers=None, exempt=()):
    """Run the build-time check at ``value`` over relations with these columns."""

    stored = {
        **CONTRACT,
        "columns": ["fee_amount"],
        "exempt_models": list(exempt),
        "producers": producers or {"fct": ["fee_amount"], "empty": ["fee_amount"]},
    }
    built = {f"model.shop.{m}": m for m in columns}
    relations = {m: f"dev.main.{m}" for m in columns}
    adapter = _Adapter(columns)
    return toggle.verify_outputs(
        stored,
        built,
        set(built),
        relations,
        list(relations.values()),
        lambda: adapter,
        value,
        "shop",
    )


def test_an_off_build_judges_an_empty_table_by_its_columns() -> None:
    """An off build judges an empty table by its columns."""

    findings, records, branch = _verify(
        (True, False), {"fct": ["id"], "empty": ["id", "fee_amount"], "other": ["id"]}
    )
    assert branch == "off"
    assert [f.code for f in findings] == ["toggle_column_present_when_off"]
    assert findings[0].identifier == "empty"
    by = {(r["model"], r["branch"]): r for r in records}
    assert by[("fct", "off")]["status"] == "satisfied"
    assert by[("empty", "off")]["status"] == "contradicted"
    assert by[("fct", "on")]["reason_code"] == "branch_not_built"
    assert ("other", "off") not in by


def test_an_on_build_holds_every_producer() -> None:
    """An on build holds every producer."""

    findings, _records, branch = _verify(
        (True, True), {"fct": ["id", "fee_amount"], "empty": ["id"]}
    )
    assert branch == "on"
    assert [f.code for f in findings] == ["toggle_column_missing_when_on"]
    assert findings[0].identifier == "empty"


def test_an_unknown_value_judges_nothing() -> None:
    """An unknown value judges nothing."""

    findings, records, branch = _verify((False, None), {"fct": ["id"]})
    assert branch is None
    assert findings == []
    assert {r["reason_code"] for r in records} == {"value_unknown"}


def test_a_producer_not_built_is_named() -> None:
    """A producer not built is named."""

    _findings, records, _branch = _verify((True, False), {"fct": ["id"]})
    by = {(r["model"], r["branch"]): r for r in records}
    assert by[("empty", "off")]["reason_code"] == "not_built"


def test_an_off_build_absorbs_the_documented_governed_columns() -> None:
    """An off build absorbs the documented governed columns."""

    from exmergo_dex_core.maintain.drift import DriftFinding

    def missing(columns):
        """A ``column_missing`` finding on model ``m`` naming ``columns``."""

        return DriftFinding(
            axis="build",
            code="column_missing",
            identifier="m",
            severity="high",
            detail="",
            data={"model": "m", "columns": columns},
        )

    stored = {**CONTRACT}
    kept, notes = toggle.absorb_column_contract(
        [missing(["fee_amount"]), missing(["fee_amount", "region"])], stored, "off"
    )
    assert [f.data["columns"] for f in kept] == [["region"]]
    assert notes
    kept, notes = toggle.absorb_column_contract([missing(["fee_amount"])], stored, "on")
    assert len(kept) == 1
    assert notes == []


def test_a_driver_error_while_verifying_is_reported_not_raised(tmp_path: Path) -> None:
    """The build has run and been billed, so a driver error reading the warehouse
    leaves the contract unchecked rather than failing the command."""

    from exmergo_dex_core.transform.verify import BuildVerification, _toggle_contract

    (tmp_path / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0"\nprofile: shop\n', encoding="utf-8"
    )

    def broken_adapter():
        """Fail the way a database driver does."""

        raise RuntimeError("driver went away")

    result = BuildVerification(ran=True)
    stored = {**CONTRACT, "producers": {"fct": ["fee_amount"]}}
    _toggle_contract(
        tmp_path,
        stored,
        {"model.shop.fct": "fct"},
        {"model.shop.fct"},
        broken_adapter,
        result,
    )
    assert "toggle" in result.suppressed
    assert "RuntimeError" in result.suppressed["toggle"]
    assert result.toggle == [
        {"model": "fct", "status": "unchecked", "reason_code": "unreadable"}
    ]


def test_an_analysis_failure_reads_as_unchecked() -> None:
    """An analysis failure reads as unchecked."""

    report = toggle.analysis_failed("KeyError")
    assert report["verdict"] == "unchecked"
    assert report["completeness"] == "incomplete"
    assert report["producers"] == {}
    assert report["analysis_failed"] == "KeyError"
    assert "KeyError" in report["limits"][0]


def test_a_build_after_a_failed_analysis_keeps_an_unchecked_obligation() -> None:
    """With no producers recorded, an on build would judge nothing and say
    nothing; the marker turns that into an unchecked record."""

    stored = {**CONTRACT, "producers": {}, "analysis_failed": "KeyError"}
    assert toggle.contract_of(stored).columns == ["fee_amount"]
    adapter = _Adapter({"fct": ["id", "fee_amount"]})
    findings, records, branch = toggle.verify_outputs(
        stored,
        {"model.shop.fct": "fct"},
        {"model.shop.fct"},
        {"fct": "dev.main.fct"},
        ["dev.main.fct"],
        lambda: adapter,
        (True, True),
        "shop",
    )
    assert branch == "on"
    assert findings == []
    assert records == [
        {
            "model": "var:enable_fees",
            "status": "unchecked",
            "reason_code": "analysis_failed",
            "reason": records[0]["reason"],
        }
    ]
    assert "KeyError" in records[0]["reason"]


def test_what_the_plan_could_not_judge_is_owed_by_every_build() -> None:
    """A model the plan could not read is no known producer, so without the
    stored gaps a build would hold nothing to it and say nothing."""

    analysis = toggle._report(
        [
            {
                "model": "wide",
                "status": "unchecked",
                "reason_code": "unreadable_select",
            },
            {"model": "fct", "status": "satisfied", "produces": ["fee_amount"]},
        ],
        ["a dynamic var() read"],
        [],
        2,
    )
    gaps = toggle.plan_gaps(analysis)
    assert gaps == {
        "models": {"wide": "unreadable_select"},
        "limits": ["a dynamic var() read"],
    }
    assert toggle.plan_gaps(toggle._report([toggle._record("fct")], [], [], 1)) is None
    stored = {**CONTRACT, "producers": {"fct": ["fee_amount"]}, "plan_gaps": gaps}
    assert toggle.contract_of(stored).columns == ["fee_amount"]
    adapter = _Adapter({"fct": ["id", "fee_amount"]})
    _findings, records, branch = toggle.verify_outputs(
        stored,
        {"model.shop.fct": "fct"},
        {"model.shop.fct"},
        {"fct": "dev.main.fct"},
        ["dev.main.fct"],
        lambda: adapter,
        (True, True),
        "shop",
    )
    by = {
        (r["model"], r.get("reason_code"))
        for r in records
        if r["status"] == "unchecked"
    }
    assert ("wide", "unchecked_at_plan") in by
    assert ("var:enable_fees", "plan_incomplete") in by
    assert branch == "on"


def test_a_verification_failure_still_owes_the_plan_gaps(tmp_path: Path) -> None:
    """A verification failure still owes the plan gaps."""

    from exmergo_dex_core.transform.verify import BuildVerification, _toggle_contract

    (tmp_path / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0"\nprofile: shop\n', encoding="utf-8"
    )

    def broken_adapter():
        """Fail the way a database driver does."""

        raise RuntimeError("driver went away")

    result = BuildVerification(ran=True)
    stored = {**CONTRACT, "producers": {}, "analysis_failed": "KeyError"}
    _toggle_contract(tmp_path, stored, {}, set(), broken_adapter, result)
    assert [r["reason_code"] for r in result.toggle] == ["analysis_failed"]
    assert result.toggle[0]["model"] == "var:enable_fees"
