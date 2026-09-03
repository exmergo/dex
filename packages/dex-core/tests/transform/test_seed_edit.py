"""`seed_csv` end to end: CSV shape, the size caps, containment to the seed
paths, and the PII gate.

The gate is the reason this kind is different in kind rather than in degree from
every other one: a seed puts **values** into a reviewable diff, and a diff goes
into git and stays there. So the refusal has to fire before any diff exists,
which is what :mod:`tests.test_safety_spine` asserts; here we pin the behavior
and the wording of the fix it names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import PIIOverride, pii_override_paths
from exmergo_dex_core.dbt_project import DbtProjectError, node_files
from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform.plans import EditKind, PlanEdit, PlanError
from exmergo_dex_core.transform.plans import plan as make_plan
from exmergo_dex_core.transform.validate import (
    MAX_SEED_BYTES,
    MAX_SEED_ROWS,
    EditValidationError,
    validate_edit,
    validate_seed,
)

SEED = "country_code,currency,vat_rate\nIT,EUR,0.22\nFR,EUR,0.20\nUS,USD,0.00\n"


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _edits_file(tmp_path: Path, *entries: dict) -> Path:
    payload = tmp_path / "edits.json"
    payload.write_text(json.dumps({"edits": list(entries)}), encoding="utf-8")
    return payload


def _seed(content: str = SEED, path: str = "seeds/country_vat.csv") -> PlanEdit:
    return PlanEdit(path=path, kind=EditKind.SEED_CSV, new_content=content)


def _cache_flagging(column: str, category: PIICategory, confidence: float) -> DexCache:
    return DexCache(
        connector="duckdb",
        datasets=[
            Dataset(
                identifier="db.main.customers",
                columns=[
                    ColumnProfile(
                        name=column,
                        data_type="VARCHAR",
                        pii=PIIFlag(category=category, confidence=confidence),
                    )
                ],
            )
        ],
    )


# --- CSV shape -------------------------------------------------------------------


def test_a_well_formed_seed_validates():
    assert validate_edit(_seed()) == []


@pytest.mark.parametrize(
    ("content", "fix_named"),
    [
        pytest.param("", "header row", id="empty"),
        pytest.param("\n", "header row is empty", id="blank_header"),
        pytest.param("a,,c\n1,2,3\n", "header column 2 has no name", id="unnamed"),
        pytest.param("code,CODE\n1,2\n", "duplicate column", id="duplicate"),
        pytest.param("a,b\n1,2\n3\n", "row 3 breaks at column 2", id="short_row"),
        pytest.param("a,b\n1,2,3\n", "row 2 breaks at column 3", id="long_row"),
    ],
)
def test_a_malformed_seed_names_the_row_and_column_at_fault(
    content: str, fix_named: str
):
    with pytest.raises(EditValidationError, match=fix_named):
        validate_edit(_seed(content))


def test_the_row_cap_is_enforced_and_named():
    oversized = "code\n" + "x\n" * (MAX_SEED_ROWS + 1)
    with pytest.raises(EditValidationError, match=f"over the {MAX_SEED_ROWS} row cap"):
        validate_edit(_seed(oversized))
    # And exactly at the cap is fine: the refusal is for going over, not near.
    assert validate_edit(_seed("code\n" + "x\n" * MAX_SEED_ROWS)) == []


def test_the_byte_cap_is_enforced_and_named():
    wide = "code\n" + ("x" * 200 + "\n") * 6_000
    assert len(wide.encode("utf-8")) > MAX_SEED_BYTES
    with pytest.raises(
        EditValidationError, match=f"over the {MAX_SEED_BYTES // 1024} KiB cap"
    ):
        validate_edit(_seed(wide))


# --- the PII gate ----------------------------------------------------------------


def test_a_pii_named_column_is_refused_and_the_refusal_carries_no_value():
    content = "id,email\n1,someone@example.com\n2,other@example.com\n"
    with pytest.raises(EditValidationError) as excinfo:
        validate_edit(_seed(content))
    message = str(excinfo.value)
    assert "'email' looks like email" in message
    # The whole point: the column name and its category, never a value.
    assert "someone@example.com" not in message
    assert "other@example.com" not in message


def test_the_refusal_names_the_override_that_clears_the_column():
    with pytest.raises(EditValidationError) as excinfo:
        validate_edit(_seed("id,email\n1,a@b.c\n"))
    assert "`- {column: country_vat.email}`" in str(excinfo.value)
    assert "pii_overrides" in str(excinfo.value)


def test_the_named_override_actually_clears_it():
    overrides = pii_override_paths([PIIOverride(column="country_vat.email")])
    assert validate_edit(_seed("id,email\n1,a@b.c\n"), pii_overrides=overrides) == []


def test_a_column_the_cache_already_flagged_is_refused_by_name():
    # The seed's own header says nothing (`ref_code` matches no pattern); the
    # cache is what knows this column carries personal data.
    cache = _cache_flagging("ref_code", PIICategory.GOVERNMENT_ID, 0.9)
    with pytest.raises(
        EditValidationError, match="'ref_code' looks like government_id"
    ):
        validate_edit(_seed("ref_code,label\nA,x\n"), cache=cache)


def test_a_cache_flag_matches_a_header_case_insensitively():
    cache = _cache_flagging("ref_code", PIICategory.GOVERNMENT_ID, 0.9)
    with pytest.raises(EditValidationError, match="REF_CODE"):
        validate_edit(_seed("REF_CODE,label\nA,x\n"), cache=cache)


def test_a_sub_threshold_cache_flag_warns_rather_than_refuses():
    cache = _cache_flagging("ref_code", PIICategory.NAME, 0.3)
    warnings = validate_edit(_seed("ref_code,label\nA,x\n"), cache=cache)
    assert warnings and "under the 0.5 block threshold" in warnings[0]


def test_the_provisional_generic_name_match_warns_rather_than_refuses():
    # `country_name` matches the generic `*_name` pattern, which the detector
    # itself calls provisional: on a warehouse column `explore` refines it
    # against value shape, and dex never reads a seed's values, so blocking on
    # it would claim a certainty the detector disclaims.
    warnings = validate_seed(
        "seeds/countries.csv", "country_code,country_name\nIT,Italy\n"
    )
    assert warnings and "cannot refine on a seed" in warnings[0]


def test_an_explicit_person_name_pattern_still_refuses():
    # The generic match is provisional; `full_name` is not a guess.
    with pytest.raises(EditValidationError, match="'full_name' looks like name"):
        validate_seed("seeds/people.csv", "id,full_name\n1,x\n")


def test_detection_reads_names_and_types_and_never_values():
    # The standing limit of the whole PII subsystem, stated rather than papered
    # over: a column named `code` holding email addresses passes this gate.
    assert validate_seed("seeds/x.csv", "code\nsomeone@example.com\n") == []


# --- containment, kind agreement, and the project view ---------------------------


def test_a_seed_plans_applies_and_lands_as_a_create_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = _edits_file(
        tmp_path,
        {"path": "seeds/country_vat.csv", "kind": "seed_csv", "content": SEED},
        {
            "path": "seeds/schema.yml",
            "kind": "schema_yml",
            "content": (
                "version: 2\n"
                "seeds:\n"
                "  - name: country_vat\n"
                "    config:\n"
                "      column_types:\n"
                "        vat_rate: double\n"
            ),
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "add the VAT lookup",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["paths"] == ["seeds/country_vat.csv", "seeds/schema.yml"]
    created = {d["path"]: d for d in envelope["diffs"]}["seeds/country_vat.csv"]
    assert created["op"] == "create"
    assert "IT,EUR,0.22" in created["unified"]

    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope["errors"]
    assert (dbt_project_dir / "seeds" / "country_vat.csv").read_text() == SEED


def test_custom_seed_paths_are_honored(dbt_project_dir: Path, tmp_path: Path):
    project_yml = dbt_project_dir / "dbt_project.yml"
    project_yml.write_text(
        project_yml.read_text(encoding="utf-8") + 'seed-paths: ["reference"]\n',
        encoding="utf-8",
    )
    store = FilesystemStore(tmp_path)
    stored, _diffs, _warnings = make_plan(
        "add the lookup",
        [_seed(path="reference/country_vat.csv")],
        dbt_project_dir,
        tmp_path,
        store=store,
    )
    assert stored.edits[0].path == "reference/country_vat.csv"

    with pytest.raises(DbtProjectError, match=r"seed \(reference\)"):
        make_plan("add", [_seed()], dbt_project_dir, tmp_path, store=store)


def test_kind_and_surface_must_agree_in_both_directions(
    dbt_project_dir: Path, tmp_path: Path
):
    store = FilesystemStore(tmp_path)
    with pytest.raises(PlanError, match="seed paths"):
        make_plan(
            "bad",
            [_seed(path="models/staging/country_vat.csv")],
            dbt_project_dir,
            tmp_path,
            store=store,
        )

    model_in_seeds = PlanEdit(
        path="seeds/x.sql", kind=EditKind.MODEL_SQL, new_content="select 1\n"
    )
    with pytest.raises(PlanError, match="seed path"):
        make_plan("bad", [model_in_seeds], dbt_project_dir, tmp_path, store=store)


def test_a_schema_yml_beside_a_seed_is_accepted(dbt_project_dir: Path, tmp_path: Path):
    edit = PlanEdit(
        path="seeds/schema.yml",
        kind=EditKind.SCHEMA_YML,
        new_content="version: 2\nseeds:\n  - name: country_vat\n",
    )
    stored, _diffs, _warnings = make_plan(
        "declare the seed's column types",
        [edit],
        dbt_project_dir,
        tmp_path,
        store=FilesystemStore(tmp_path),
    )
    assert stored.edits[0].path == "seeds/schema.yml"


def test_a_seed_is_loaded_and_counts_as_a_node(dbt_project_dir: Path):
    seeds = dbt_project_dir / "seeds"
    seeds.mkdir()
    (seeds / "country_vat.csv").write_text(SEED, encoding="utf-8")
    (seeds / "schema.yml").write_text("version: 2\n", encoding="utf-8")

    view = load_project(dbt_project_dir)
    # Load-bearing, not bookkeeping: a seed missing from `files` hashes as a
    # create on every re-plan and the apply after it conflicts on a file nobody
    # touched.
    assert "seeds/country_vat.csv" in view.files
    assert set(node_files(view)) == {
        "models/staging/stg_customers.sql",
        "seeds/country_vat.csv",
    }


def test_deleting_a_seed_a_model_still_refs_is_refused(
    dbt_project_dir: Path, tmp_path: Path
):
    # `.csv` never matched the old `.endswith(".sql")` test, so this delete used
    # to be accepted and would break the build.
    seeds = dbt_project_dir / "seeds"
    seeds.mkdir()
    (seeds / "country_vat.csv").write_text(SEED, encoding="utf-8")
    (dbt_project_dir / "models" / "staging" / "uses_seed.sql").write_text(
        "select * from {{ ref('country_vat') }}\n", encoding="utf-8"
    )
    delete = PlanEdit(path="seeds/country_vat.csv", kind=EditKind.SEED_CSV, op="delete")
    with pytest.raises(PlanError, match="country_vat"):
        make_plan(
            "drop the lookup",
            [delete],
            dbt_project_dir,
            tmp_path,
            store=FilesystemStore(tmp_path),
        )


# --- the build ------------------------------------------------------------------


def test_a_dev_build_materializes_an_applied_seed(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # The acceptance criterion: `dbt build` runs seeds natively, so applying one
    # and building is all it takes. No new command, and no `dbt seed` step for
    # the caller to remember.
    duckdb = pytest.importorskip("duckdb")

    payload = _edits_file(
        tmp_path,
        {"path": "seeds/country_vat.csv", "kind": "seed_csv", "content": SEED},
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "add the VAT lookup",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    rc, _envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    statuses = {n["name"]: n["status"] for n in envelope["data"]["nodes"]}
    assert statuses["country_vat"] == "success"

    con = duckdb.connect(str(tmp_path / "dev.duckdb"), read_only=True)
    try:
        rows = con.execute(
            "select country_code, currency from country_vat order by country_code"
        ).fetchall()
        assert rows == [("FR", "EUR"), ("IT", "EUR"), ("US", "USD")]
    finally:
        con.close()
