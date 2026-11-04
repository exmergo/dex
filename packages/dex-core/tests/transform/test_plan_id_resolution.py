"""An empty plan id is refused; "no id" names the plan it picked (#1103).

Every verb that takes an optional plan id (`apply`, `export`, `ground`,
`classify`, `compare`) reads "no id" as "the latest unapplied plan". An empty
id, which is what a failed shell substitution yields, used to take the same
path, so `apply "$(dex transform plan ...)"` after a failed plan applied an
earlier one and said nothing about it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.errors import RequestError
from exmergo_dex_core.storage import FilesystemStore

#: The verbs sharing the "default: latest unapplied" resolution that run without
#: dbt or a connection. `compare` is covered below and in
#: `test_transform_compare.py`, since it needs a built dev target to get far.
VERBS = ("apply", "export", "ground", "classify")


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _make_plan(tmp_path: Path, capsys, name: str, intent: str) -> str:
    payload_file = tmp_path / f"edits-{name}.json"
    payload_file.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": f"models/marts/{name}.sql",
                        "kind": "model_sql",
                        "content": "select 1 as id\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            intent,
            "--edits-file",
            str(payload_file),
        ],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok", envelope
    return envelope["data"]["plan_id"]


@pytest.fixture
def two_plans(dbt_project_dir: Path, tmp_path: Path, capsys) -> tuple[str, str]:
    """An older unapplied plan and a newer one, the issue's setup."""

    older = _make_plan(tmp_path, capsys, "fct_older", "the older plan")
    newer = _make_plan(tmp_path, capsys, "fct_newer", "the newer plan")
    return older, newer


def _unapplied(tmp_path: Path, plan_id: str) -> bool:
    stored = tmp_path / ".dex" / "plans" / f"{plan_id}.json"
    return json.loads(stored.read_text())["applied_at"] is None


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_plan_id_is_refused(
    verb: str, empty: str, two_plans, dbt_project_dir: Path, tmp_path: Path, capsys
):
    older, newer = two_plans
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", verb, empty], capsys
    )
    assert rc != 0
    assert envelope["status"] == "error"
    assert envelope["reason"] == "request"
    assert "empty plan id" in envelope["errors"][0]
    assert "omit the argument" in envelope["errors"][0]
    # Nothing was picked, so nothing was written and nothing was marked.
    assert _unapplied(tmp_path, older) and _unapplied(tmp_path, newer)
    assert not (dbt_project_dir / "models/marts/fct_older.sql").exists()
    assert not (dbt_project_dir / "models/marts/fct_newer.sql").exists()


@pytest.mark.parametrize("verb", VERBS)
def test_no_plan_id_names_the_plan_it_picked(
    verb: str, two_plans, tmp_path: Path, capsys
):
    _, newer = two_plans
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", verb], capsys)
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    data = envelope["data"]
    assert data["plan_id"] == newer
    resolved = data["resolved_plan"]
    assert resolved["plan_id"] == newer
    assert resolved["intent"] == "the newer plan"
    assert resolved["created_at"]
    assert resolved["resolved_from"] == "latest_unapplied"
    named = [w for w in envelope["warnings"] if newer in w]
    assert len(named) == 1
    assert "no plan id given" in named[0] and "the newer plan" in named[0]
    # Placed beside the id it explains, ahead of the verb's own payload.
    assert list(data)[:2] == ["plan_id", "resolved_plan"]


@pytest.mark.parametrize("verb", VERBS)
def test_an_explicit_plan_id_reports_no_resolution(
    verb: str, two_plans, tmp_path: Path, capsys
):
    older, _ = two_plans
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", verb, older], capsys
    )
    assert rc == 0, envelope
    assert envelope["data"]["plan_id"] == older
    assert "resolved_plan" not in envelope["data"]
    assert not any("no plan id given" in w for w in envelope["warnings"])


def test_apply_with_no_id_applies_the_newer_plan_and_says_so(
    two_plans, dbt_project_dir: Path, tmp_path: Path, capsys
):
    older, newer = two_plans
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0 and envelope["status"] == "ok"
    assert envelope["data"]["written"] == ["models/marts/fct_newer.sql"]
    assert envelope["data"]["resolved_plan"]["plan_id"] == newer
    assert _unapplied(tmp_path, older) and not _unapplied(tmp_path, newer)


def test_a_conflicted_apply_with_no_id_still_names_the_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """The `needs_confirmation` envelope is the one a caller re-runs with
    `--confirm`, so it has to say which plan the confirmation would write."""

    target = dbt_project_dir / "models/staging/stg_customers.sql"
    payload_file = tmp_path / "edits.json"
    payload_file.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/staging/stg_customers.sql",
                        "kind": "model_sql",
                        "content": "select 1 as id\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _, planned = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "rewrite customers",
            "--edits-file",
            str(payload_file),
        ],
        capsys,
    )
    plan_id = planned["data"]["plan_id"]
    target.write_text("select 99 as id -- hand-tuned\n", encoding="utf-8")

    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert envelope["data"]["resolved_plan"]["plan_id"] == plan_id
    assert any(plan_id in w for w in envelope["warnings"])
    assert target.read_text(encoding="utf-8") == "select 99 as id -- hand-tuned\n"


def test_an_empty_plan_file_does_not_fall_through_to_the_latest_plan(
    two_plans, dbt_project_dir: Path, tmp_path: Path, capsys
):
    """`apply --plan-file "$(...)"` with an empty substitution used to route to
    the stored-plan path and apply the newest plan."""

    older, newer = two_plans
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", "--plan-file", ""],
        capsys,
    )
    assert rc != 0
    assert envelope["status"] == "error"
    assert envelope["reason"] == "request"
    assert "empty --plan-file" in envelope["errors"][0]
    assert _unapplied(tmp_path, older) and _unapplied(tmp_path, newer)
    assert not (dbt_project_dir / "models/marts/fct_newer.sql").exists()


@pytest.mark.parametrize("flag", ["--for-plan", "--for-plan-file"])
def test_an_empty_for_plan_is_refused_before_anything_runs(
    flag: str, dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Read as absent, an empty `--for-plan` builds with no coverage at all."""

    def no_dbt(*_args, **_kwargs):  # pragma: no cover - the assertion
        raise AssertionError("the refusal must come before dbt is invoked")

    monkeypatch.setattr("subprocess.run", no_dbt)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            flag,
            "",
        ],
        capsys,
    )
    assert rc != 0
    assert envelope["status"] == "error"
    assert envelope["reason"] == "request"
    assert f"empty {flag}" in envelope["errors"][0]


def test_compare_refuses_an_empty_plan_id(two_plans, tmp_path: Path, capsys):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "compare", ""], capsys
    )
    assert rc != 0
    assert envelope["reason"] == "request"
    assert "empty plan id" in envelope["errors"][0]


def test_the_library_refuses_an_empty_plan_id_too(
    two_plans, dbt_project_dir: Path, tmp_path: Path
):
    """A host calling the engine in process gets the same refusal, typed."""

    engine = DexEngine(
        connector="duckdb",
        repo_root=str(tmp_path),
        store=FilesystemStore(tmp_path),
        config=DexConfig(connector="duckdb"),
    )
    with pytest.raises(RequestError, match="empty plan id"):
        engine.apply("")
    with pytest.raises(RequestError, match="empty plan id"):
        engine.export_plan("")


def test_a_refusal_after_the_pick_still_names_the_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """A verb can refuse once "no id" has chosen a plan, and that error is where
    the caller most needs to know which plan was read. Here, apply's containment
    refusal on a stored plan whose path escapes the editing surface."""

    plan_id = _make_plan(tmp_path, capsys, "fct_escape", "an escaping plan")
    stored_path = tmp_path / ".dex" / "plans" / f"{plan_id}.json"
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    stored["edits"][0]["path"] = "models_backup/fct_escape.sql"
    stored_path.write_text(json.dumps(stored), encoding="utf-8")

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", "--confirm"], capsys
    )
    assert rc != 0 and envelope["status"] == "error"
    assert "editing surface" in json.dumps(envelope["errors"])
    assert envelope["data"]["resolved_plan"]["plan_id"] == plan_id
    assert [w for w in envelope["warnings"] if plan_id in w]


def test_compare_refusing_a_picked_plan_names_it(two_plans, tmp_path: Path, capsys):
    """The newest plan only adds a model, so there is no baseline to compare:
    the refusal says which plan it was looking at."""

    _, newer = two_plans
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "compare"], capsys)
    assert rc != 0 and envelope["status"] == "error"
    assert envelope["data"]["resolved_plan"]["plan_id"] == newer
    assert [w for w in envelope["warnings"] if newer in w]


def test_an_explicit_id_refusal_carries_no_resolution(
    two_plans, tmp_path: Path, capsys
):
    _, newer = two_plans
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "compare", newer], capsys
    )
    assert rc != 0 and envelope["status"] == "error"
    assert "resolved_plan" not in (envelope.get("data") or {})
    assert not any("no plan id given" in w for w in envelope.get("warnings") or [])
