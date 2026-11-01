"""`transform build --verify` reports the column contract `maintain verify` does
(#1002), composed through the real CLI and a real dbt on DuckDB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core import engine as engine_mod
from exmergo_dex_core.cli import main
from exmergo_dex_core.guards.cost_guard import CostGuardError
from exmergo_dex_core.transform import verify as verify_mod

pytest.importorskip("dbt.cli.main")
pytest.importorskip("dbt.adapters.duckdb")

_CODES = {"column_missing", "column_undeclared", "column_type_mismatch"}


def _run(argv: list[str], capsys) -> dict:
    main(argv)
    return json.loads(capsys.readouterr().out)


def _model(project: Path, name: str, sql: str, yml: str | None = None) -> None:
    folder = project / "models" / "probe"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.sql").write_text(sql, encoding="utf-8")
    if yml is not None:
        (folder / f"{name}.yml").write_text(
            f"version: 2\nmodels:\n  - name: {name}\n{yml}", encoding="utf-8"
        )


@pytest.fixture
def repo(dbt_project_dir: Path, tmp_path: Path) -> Path:
    # Only the models each test writes: the fixture's own one reads a table this
    # dev database does not have.
    for path in (dbt_project_dir / "models" / "staging").iterdir():
        path.unlink()
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    return tmp_path


def _build(repo: Path, capsys, *select: str) -> dict:
    return _run(
        [
            "--repo-root",
            str(repo),
            "transform",
            "build",
            "--target",
            "dev",
            "--verify",
            *(("--select", " ".join(select)) if select else ()),
        ],
        capsys,
    )


def _contract(findings: list[dict]) -> list[tuple]:
    return sorted(
        (
            f["code"],
            f["identifier"],
            f["severity"],
            json.dumps(f["data"], sort_keys=True),
        )
        for f in findings
        if f["code"] in _CODES
    )


def test_the_issue_reproduction_matches_maintain_verify(
    repo: Path, dbt_project_dir: Path, capsys
):
    _model(
        dbt_project_dir,
        "contract_probe",
        "select 1 as id, 'safe' as extra_column\n",
        "    columns:\n      - name: id\n",
    )
    built = _build(repo, capsys, "contract_probe")
    assert built["status"] == "ok", built
    verification = built["data"]["verification"]
    assert [f["code"] for f in verification["findings"]] == ["column_undeclared"]
    assert built["data"]["outcome"] != "failed"
    assert verification["column_contract"] == {
        "checked": ["contract_probe"],
        "no_declared_columns": [],
        "unchecked": [],
    }
    assert "column_contract" not in verification["suppressed"]

    swept = _run(
        [
            "--repo-root",
            str(repo),
            "maintain",
            "verify",
            "--target",
            "dev",
            "contract_probe",
        ],
        capsys,
    )
    assert _contract(verification["findings"]) == _contract(swept["data"]["findings"])


def test_every_code_matches_and_an_unrelated_model_contributes_nothing(
    repo: Path, dbt_project_dir: Path, capsys
):
    _model(
        dbt_project_dir,
        "selected",
        "select 1 as id, 'x' as extra_column\n",
        "    columns:\n"
        "      - name: id\n        data_type: varchar\n"
        "      - name: gone\n",
    )
    _model(
        dbt_project_dir,
        "unrelated",
        "select 1 as id, 2 as undocumented\n",
        "    columns:\n      - name: id\n",
    )
    _build(repo, capsys, "unrelated")  # so it has a relation that could be judged
    verification = _build(repo, capsys, "selected")["data"]["verification"]
    assert {f["code"] for f in verification["findings"]} == _CODES
    assert {f["identifier"] for f in verification["findings"]} == {"selected"}
    assert verification["column_contract"]["checked"] == ["selected"]

    swept = _run(
        ["--repo-root", str(repo), "maintain", "verify", "--target", "dev", "selected"],
        capsys,
    )
    assert _contract(verification["findings"]) == _contract(swept["data"]["findings"])


def test_only_the_selected_version_is_judged(repo: Path, dbt_project_dir: Path, capsys):
    folder = dbt_project_dir / "models" / "probe"
    folder.mkdir(parents=True)
    (folder / "m_v1.sql").write_text("select 1 as id, 2 as v1_only\n", encoding="utf-8")
    (folder / "m_v2.sql").write_text("select 1 as id, 2 as v2_only\n", encoding="utf-8")
    (folder / "m.yml").write_text(
        "version: 2\nmodels:\n  - name: m\n    latest_version: 2\n"
        "    columns:\n      - name: id\n    versions:\n      - v: 1\n      - v: 2\n",
        encoding="utf-8",
    )
    verification = _build(repo, capsys, "m.v2")["data"]["verification"]
    assert verification["column_contract"]["checked"] == ["m.v2"]
    assert [
        (f["identifier"], f["data"]["columns"]) for f in verification["findings"]
    ] == [("m.v2", ["v2_only"])]


def test_a_failed_model_is_not_judged_on_the_relation_it_left_behind(
    repo: Path, dbt_project_dir: Path, capsys
):
    yml = "    columns:\n      - name: id\n"
    _model(dbt_project_dir, "parent", "select 1 as id, 2 as stale_extra\n", yml)
    _model(
        dbt_project_dir,
        "child",
        "select id, 2 as stale_extra from {{ ref('parent') }}\n",
        yml,
    )
    _model(dbt_project_dir, "fine", "select 1 as id, 2 as real_extra\n", yml)
    assert _build(repo, capsys)["status"] == "ok"

    _model(dbt_project_dir, "parent", "select no_such_column as id\n")
    failed = _build(repo, capsys)
    assert failed["status"] == "error"
    verification = failed["data"]["verification"]
    assert verification["column_contract"]["checked"] == ["fine"]
    assert verification["column_contract"]["unchecked"] == [
        {"model": "child", "reason_code": "not_built"},
        {"model": "parent", "reason_code": "not_built"},
    ]
    judged = [f for f in verification["findings"] if f["code"] in _CODES]
    assert [f["identifier"] for f in judged] == ["fine"]


def test_nothing_built_suppresses_the_contract_with_the_reason(
    repo: Path, dbt_project_dir: Path, capsys
):
    yml = "    columns:\n      - name: id\n"
    _model(dbt_project_dir, "only", "select 1 as id, 2 as extra\n", yml)
    assert _build(repo, capsys)["status"] == "ok"
    _model(dbt_project_dir, "only", "select no_such_column as id\n")
    verification = _build(repo, capsys)["data"]["verification"]
    assert "earlier run" in verification["suppressed"]["column_contract"]
    assert verification["column_contract"]["unchecked"] == [
        {"model": "only", "reason_code": "not_built"}
    ]
    assert not [f for f in verification["findings"] if f["code"] in _CODES]


def test_a_failing_test_still_has_its_built_model_checked(
    repo: Path, dbt_project_dir: Path, capsys
):
    _model(
        dbt_project_dir,
        "tested",
        "select cast(null as integer) as id, 2 as extra\n",
        "    columns:\n      - name: id\n        data_tests: [not_null]\n",
    )
    built = _build(repo, capsys)
    assert built["status"] == "error"
    verification = built["data"]["verification"]
    assert verification["column_contract"]["checked"] == ["tested"]
    assert "column_undeclared" in {f["code"] for f in verification["findings"]}


def test_a_model_declaring_no_columns_is_named_and_nothing_declared_is_suppressed(
    repo: Path, dbt_project_dir: Path, capsys
):
    _model(dbt_project_dir, "bare", "select 1 as id\n")
    verification = _build(repo, capsys)["data"]["verification"]
    assert verification["column_contract"]["no_declared_columns"] == ["bare"]
    assert "declares columns" in verification["suppressed"]["column_contract"]


def test_a_metadata_failure_keeps_the_build_and_explains_the_gap(
    repo: Path, dbt_project_dir: Path, capsys, monkeypatch
):
    _model(
        dbt_project_dir,
        "probe",
        "select 1 as id\n",
        "    columns:\n      - name: id\n",
    )
    real = engine_mod.DexEngine._adapter

    def failing(self, *args, **kwargs):
        adapter = real(self, *args, **kwargs)

        def boom(_identifier):
            raise RuntimeError("metadata down")

        monkeypatch.setattr(adapter, "table_metadata", boom)
        return adapter

    monkeypatch.setattr(engine_mod.DexEngine, "_adapter", failing)
    built = _build(repo, capsys)
    assert built["status"] == "ok"
    verification = built["data"]["verification"]
    assert verification["column_contract"]["unchecked"] == [
        {"model": "probe", "reason_code": "metadata_unreadable"}
    ]
    assert "column_contract" in verification["suppressed"]
    assert any("metadata down" in w for w in built["warnings"])


def test_a_cost_refusal_reaches_the_caller(tmp_path: Path, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise CostGuardError("over the ceiling")

    monkeypatch.setattr(
        "exmergo_dex_core.maintain.verify.column_contract_plan",
        lambda *_a, **_k: ({"a": {"id": None}}, 0, []),
    )
    result = verify_mod.BuildVerification(ran=True)
    with pytest.raises(CostGuardError):
        verify_mod._column_contract(
            tmp_path,
            {"success": True},
            {"model.p.a": "a"},
            {"model.p.a"},
            refuse,
            result,
        )


def test_the_contract_runs_no_statement(tmp_path: Path, monkeypatch):
    """Metadata only, through the adapter it was handed: nothing to price."""

    class Adapter:
        opened = 0

        def list_objects(self):
            return [type("M", (), {"identifier": "db.main.a"})()]

        def table_metadata(self, _identifier):
            column = type("C", (), {"name": "id", "data_type": "INTEGER"})()
            return object(), [column]

        def __getattr__(self, name):
            raise AssertionError(f"the column contract used adapter.{name}")

    monkeypatch.setattr(
        "exmergo_dex_core.maintain.verify.column_contract_plan",
        lambda *_a, **_k: ({"a": {"id": None}}, 0, []),
    )
    monkeypatch.setattr(
        "exmergo_dex_core.maintain.verify.built_model_relations",
        lambda *_a, **_k: {"a": "db.main.a"},
    )
    result = verify_mod.BuildVerification(ran=True)
    adapter = Adapter()
    reason = verify_mod._column_contract(
        tmp_path,
        {"success": True},
        {"model.p.a": "a"},
        {"model.p.a"},
        lambda: adapter,
        result,
    )
    assert reason is None
    assert result.findings == []
    assert result.column_contract["checked"] == ["a"]


def test_equivalent_type_spelling_int_matches_duckdb_integer(
    repo: Path, dbt_project_dir: Path, capsys
):
    """#1033: declaring `data_type: int` on DuckDB does not trigger a mismatch
    against the adapter's reported `INTEGER`."""
    _model(
        dbt_project_dir,
        "type_alias_model",
        "select 1 as id, 'hello' as note\n",
        "    columns:\n"
        "      - name: id\n        data_type: int\n"
        "      - name: note\n        data_type: varchar(10)\n",
    )
    built = _build(repo, capsys, "type_alias_model")
    assert built["status"] == "ok", built
    verification = built["data"]["verification"]
    assert verification["column_contract"]["checked"] == ["type_alias_model"]
    findings = [
        f for f in verification["findings"] if f["code"] == "column_type_mismatch"
    ]
    assert findings == []
