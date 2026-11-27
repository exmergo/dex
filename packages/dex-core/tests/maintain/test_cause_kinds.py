"""The cause kinds ``dex demo`` does not seed, decided from their evidence (#1476).

Pure: each decision function is handed what git, the warehouse or Elementary's
table answered, and each is held on both sides, a case that confirms and one
that does not.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from exmergo_dex_core.maintain import cause_kinds as ck
from exmergo_dex_core.maintain import diagnosis as diag
from exmergo_dex_core.maintain.commit_history import GitHistory, git_env
from exmergo_dex_core.maintain.timeline import RunPoint

T0 = datetime(2026, 11, 1, tzinfo=UTC)
BOUNDS = ck.Bounds(through="b" * 40, after=("a" * 40,), last_good="a" * 40)


def _lineage(**extra) -> ck.Lineage:
    held = {
        "test": "test.shop.t",
        "model": "model.shop.fct",
        "project": "shop",
        "models": {"model.shop.fct": "fct", "model.shop.dim": "dim"},
        "files": {
            "models/fct.sql": ("model.shop.fct",),
            "models/schema.yml": ("model.shop.fct", "test.shop.t"),
            "tests/t.sql": ("test.shop.t",),
        },
    }
    held.update(extra)
    return ck.Lineage(**held)  # type: ignore[arg-type]


# --- the owner partition --------------------------------------------------------------


def test_each_file_of_the_range_has_one_owner():
    owned = ck.partition(
        [
            "seeds/codes.csv",
            "seeds/other.csv",
            "package-lock.yml",
            "packages.yml",
            "models/fct.sql",
            "sub/packages.yml",
            "data/notes.txt",
        ],
        ["seeds/codes.csv"],
        ["seeds"],
    )

    assert owned["seed"] == ("seeds/codes.csv", "seeds/other.csv")
    assert owned["package"] == ("package-lock.yml", "packages.yml")
    # A packages.yml below the root is not dbt's, and a non-CSV under a seed
    # path is not a seed.
    assert owned["project"] == ("models/fct.sql", "sub/packages.yml", "data/notes.txt")


# --- the lineage ---------------------------------------------------------------------


def _manifest() -> dict:
    return {
        "metadata": {"project_name": "shop", "adapter_type": "duckdb"},
        "nodes": {
            "test.shop.t": {
                "resource_type": "test",
                "package_name": "shop",
                "original_file_path": "models/schema.yml",
                "attached_node": "model.shop.fct",
                "test_metadata": {"name": "relationships", "namespace": None},
                "depends_on": {
                    "nodes": ["model.shop.fct", "model.shop.dim"],
                    "macros": ["macro.dbt.test_relationships"],
                },
            },
            "model.shop.fct": {
                "resource_type": "model",
                "package_name": "shop",
                "name": "fct",
                "original_file_path": "models/fct.sql",
                "patch_path": "shop://models/schema.yml",
                "raw_code": "select amount from {{ ref('stg') }}",
                "depends_on": {
                    "nodes": ["model.shop.stg", "model.shop.dim"],
                    "macros": ["macro.dbt_utils.star", "macro.dbt_duckdb.x"],
                },
            },
            "model.shop.dim": {
                "resource_type": "model",
                "package_name": "shop",
                "name": "dim",
                "original_file_path": "models/dim.sql",
                "depends_on": {"nodes": ["seed.shop.codes"], "macros": []},
            },
            "model.shop.stg": {
                "resource_type": "model",
                "package_name": "shop",
                "name": "stg",
                "original_file_path": "models/stg.sql",
                "depends_on": {
                    "nodes": ["source.shop.raw.o"],
                    "macros": ["macro.shop.m"],
                },
            },
            "seed.shop.codes": {
                "resource_type": "seed",
                "package_name": "shop",
                "original_file_path": "seeds/codes.csv",
            },
            "model.other.unread": {
                "resource_type": "model",
                "package_name": "shop",
                "original_file_path": "models/unread.sql",
            },
        },
        "sources": {"source.shop.raw.o": {"package_name": "shop"}},
        "macros": {"macro.shop.m": {"original_file_path": "macros/m.sql"}},
    }


def test_the_lineage_holds_what_the_test_reads_and_nothing_else():
    found = ck.lineage(_manifest(), "test.shop.t", columns=["status"])

    assert found is not None
    assert found.model == "model.shop.fct"
    assert set(found.models) == {"model.shop.fct", "model.shop.dim", "model.shop.stg"}
    assert found.seeds == {"seed.shop.codes": "seeds/codes.csv"}
    assert found.sources == ("source.shop.raw.o",)
    # dbt's own macros and the adapter's are not packages; dbt_utils is.
    assert found.packages == ("dbt_utils",)
    assert set(found.files) == {
        "models/schema.yml",
        "models/fct.sql",
        "models/dim.sql",
        "models/stg.sql",
        "seeds/codes.csv",
        "macros/m.sql",
    }
    # The tested model pairs with each model it reads.
    assert found.pairs == (
        ("model.shop.fct", "model.shop.stg"),
        ("model.shop.fct", "model.shop.dim"),
    )
    assert found.readers["source.shop.raw.o"] == ("",)
    assert not found.truncated


def test_a_model_a_test_reads_beside_its_own_is_no_pair_unless_it_reads_it():
    manifest = _manifest()
    fct = manifest["nodes"]["model.shop.fct"]
    fct["depends_on"]["nodes"] = ["model.shop.stg"]
    found = ck.lineage(manifest, "test.shop.t")

    # The relationships test reads dim; fct does not, so no build order of
    # dim can stale fct.
    assert found is not None
    assert found.pairs == (("model.shop.fct", "model.shop.stg"),)


def test_a_macro_reached_through_another_is_read_with_its_package():
    manifest = _manifest()
    manifest["nodes"]["model.shop.stg"]["depends_on"]["macros"] = ["macro.shop.wrap"]
    manifest["macros"] = {
        "macro.shop.wrap": {
            "original_file_path": "macros/wrap.sql",
            "depends_on": {"macros": ["macro.dbt_expectations.expect"]},
        }
    }
    found = ck.lineage(manifest, "test.shop.t")

    assert found is not None
    assert "dbt_expectations" in found.packages
    # A changed project macro is judged on the model that calls it.
    assert found.macro_callers["macro.shop.wrap"] == ("model.shop.stg",)
    change = ck.project_change(found, ["macros/wrap.sql"], 1)
    assert change.models == ("model.shop.stg",)


def test_a_walk_cut_at_its_limit_rules_nothing_out_by_absence():
    found = ck.lineage(_manifest(), "test.shop.t", limit=2)

    assert found is not None
    assert found.truncated
    [held] = ck.seed_candidates(found, {}, BOUNDS, {})
    unproven = ck.unproven(held, "lineage_truncated", "the walk stopped")
    assert (unproven.status, unproven.reason) == ("not_checked", "lineage_truncated")
    assert unproven.evidence["would_rule_out"] == "no_seed_in_lineage"
    # A positive answer stands.
    seed = ck.Candidate(owner="seed", kind="seed_edited", status="confirmed")
    assert ck.unproven(seed, "lineage_truncated", "x") is seed


def test_a_test_on_one_model_pairs_it_with_each_parent_model():
    manifest = _manifest()
    manifest["nodes"]["test.shop.t"]["depends_on"]["nodes"] = ["model.shop.fct"]
    found = ck.lineage(manifest, "test.shop.t")

    assert found is not None
    assert found.pairs == (
        ("model.shop.fct", "model.shop.stg"),
        ("model.shop.fct", "model.shop.dim"),
    )


def test_a_test_the_manifest_does_not_hold_has_no_lineage():
    assert ck.lineage(_manifest(), "test.shop.gone") is None


# --- the project ---------------------------------------------------------------------


def _finding(code: str, model: str, severity: str = "high"):
    return SimpleNamespace(
        code=code, identifier=model, severity=severity, axis="build", column=None
    )


def test_a_changed_model_verify_finds_broken_confirms_the_project():
    change = ck.project_change(_lineage(), ["models/fct.sql"], 1)
    held = ck.project_verified(
        change,
        {"model.shop.fct": "fct"},
        [_finding("column_missing", "fct"), _finding("node_failed", "a_test")],
        offered=False,
        suppressed={},
    )

    assert held.status == "confirmed"
    assert held.finding == "column_missing"
    assert [f["code"] for f in held.evidence["findings"]] == ["column_missing"]


def test_a_low_finding_or_one_on_another_node_never_confirms_the_project():
    change = ck.project_change(_lineage(), ["models/fct.sql"], 1)
    held = ck.project_verified(
        change,
        {"model.shop.fct": "fct"},
        [_finding("column_undeclared", "fct", "low"), _finding("node_failed", "x")],
        offered=False,
        suppressed={},
    )

    assert held.status == "ruled_out"
    assert held.reason == "verify_clean"


def test_verify_offering_its_scan_leaves_the_project_not_checked():
    change = ck.project_change(_lineage(), ["models/fct.sql"], 1)
    held = ck.project_verified(
        change, {"model.shop.fct": "fct"}, [], offered=True, suppressed={}
    )

    assert (held.status, held.reason) == ("not_checked", "scan_offered")


def test_verify_running_no_data_check_leaves_the_project_not_checked():
    change = ck.project_change(_lineage(), ["models/fct.sql"], 1)
    suppressed = dict.fromkeys(
        ("grain", "row_population", "join_contract", "column_contract"), "x"
    )
    held = ck.project_verified(
        change, {"model.shop.fct": "fct"}, [], offered=False, suppressed=suppressed
    )

    assert (held.status, held.reason) == ("not_checked", "verify_suppressed")


def test_only_the_test_file_changing_is_named_not_confirmed():
    change = ck.project_change(_lineage(), ["tests/t.sql"], 1)

    assert change.models == ()
    assert change.test_changed
    held = ck.project_test_only(change)
    assert (held.status, held.reason) == ("not_checked", "test_definition_changed")


def test_dbt_project_yml_changing_judges_the_tested_model():
    change = ck.project_change(_lineage(), ["dbt_project.yml"], 1)

    assert change.models == ("model.shop.fct",)
    assert change.project_wide == ("dbt_project.yml",)


def test_files_outside_the_lineage_change_nothing():
    change = ck.project_change(_lineage(), ["models/unread.sql"], 2)

    assert change.files == ()
    held = ck.project_not_in_lineage(change, 1)
    assert (held.status, held.reason) == ("ruled_out", "not_in_lineage")


# --- the loader: a retyped column -----------------------------------------------------


def _retyped(column: str):
    return SimpleNamespace(
        column=column, data={"type_before": "INTEGER", "type_after": "VARCHAR"}
    )


def test_a_retyped_column_the_lineage_reads_confirms_the_loader():
    [held] = ck.retype_candidates(
        _lineage(code=("select amount from x",)),
        {"dev.raw.o": [_retyped("amount")]},
        {"snapshot_before_created_at": "t"},
        checked=["dev.raw.o"],
    )

    assert held.status == "confirmed"
    assert held.columns == ("amount",)
    assert held.evidence["type_after"] == "VARCHAR"


def test_a_retyped_column_no_model_reads_rules_the_loader_out():
    [held] = ck.retype_candidates(
        _lineage(code=("select amount_usd from x",)),
        {"dev.raw.o": [_retyped("amount")]},
        {},
        checked=["dev.raw.o"],
    )

    assert (held.status, held.reason) == ("ruled_out", "no_retype")
    assert held.evidence["retyped_not_read"] == ["dev.raw.o.amount"]


def test_with_no_model_code_a_retype_is_named_not_confirmed():
    [held] = ck.retype_candidates(
        _lineage(), {"dev.raw.o": [_retyped("amount")]}, {}, checked=["dev.raw.o"]
    )

    assert (held.status, held.reason) == ("not_checked", "column_read_unknown")
    assert held.evidence["type_after"] == "VARCHAR"


def test_a_retype_is_matched_against_the_models_reading_that_source():
    found = _lineage(
        readers={
            "source.shop.raw.o": ("select * from raw_o",),
            "source.shop.raw.p": ("select amount from raw_p",),
        },
        code=("select * from raw_o", "select amount from raw_p"),
    )
    # raw_o is read through a wildcard: its amount is read unspelled, so the
    # retype is not ruled out on the word missing, and a reader of raw_p
    # spelling amount does not confirm raw_o's.
    [held] = ck.retype_candidates(
        found,
        {"dev.raw.o": [_retyped("amount")]},
        {},
        checked=["dev.raw.o"],
        sources={"dev.raw.o": "source.shop.raw.o"},
    )
    assert (held.status, held.reason) == ("not_checked", "column_read_unknown")

    [spelled] = ck.retype_candidates(
        found,
        {"dev.raw.p": [_retyped("amount")]},
        {},
        checked=["dev.raw.p"],
        sources={"dev.raw.p": "source.shop.raw.p"},
    )
    assert spelled.status == "confirmed"


# --- a package ------------------------------------------------------------------------


LOCK = """packages:
  - package: dbt-labs/dbt_utils
    version: {v}
    name: dbt_utils
  - git: https://github.com/acme/our-macros.git
    revision: abc
"""


def test_the_lock_is_read_by_name_or_by_its_source():
    pins = ck.parse_lock(LOCK.format(v="1.1.1"))

    assert pins is not None
    assert pins["dbt_utils"].version == "1.1.1"
    assert pins["our_macros"].named is False
    assert ck.parse_lock(None) is None
    assert ck.parse_lock("not: a lock") is None


def test_a_version_the_lineage_reads_moving_confirms_the_package():
    held = ck.package_candidate(
        _lineage(packages=("dbt_utils",)),
        ck.parse_lock(LOCK.format(v="1.1.1")),
        ck.parse_lock(LOCK.format(v="1.3.0")),
        declared=True,
        bounds=BOUNDS,
    )

    assert held.status == "confirmed"
    assert held.evidence["packages"] == ["dbt_utils"]


def test_the_same_lock_at_both_commits_rules_the_package_out():
    held = ck.package_candidate(
        _lineage(packages=("dbt_utils",)),
        ck.parse_lock(LOCK.format(v="1.1.1")),
        ck.parse_lock(LOCK.format(v="1.1.1")),
        declared=True,
        bounds=BOUNDS,
    )

    assert (held.status, held.reason) == ("ruled_out", "same_versions")


def test_an_unnamed_package_moving_beside_an_unmatched_one_is_not_checked():
    held = ck.package_candidate(
        _lineage(packages=("acme_macros",)),
        ck.parse_lock(LOCK.format(v="1.1.1")),
        ck.parse_lock(LOCK.format(v="1.1.1").replace("revision: abc", "revision: def")),
        declared=True,
        bounds=BOUNDS,
    )

    assert (held.status, held.reason) == ("not_checked", "package_unmapped")


@pytest.mark.parametrize(
    ("declared", "status", "reason"),
    [(True, "not_checked", "unpinned"), (False, "ruled_out", "no_packages")],
)
def test_no_lock_is_unpinned_where_packages_are_declared(
    declared: bool, status: str, reason: str
):
    held = ck.package_candidate(
        _lineage(), None, None, declared=declared, bounds=BOUNDS
    )

    assert (held.status, held.reason) == (status, reason)


# --- a seed ---------------------------------------------------------------------------


def test_a_seed_hashing_differently_from_every_bound_is_edited():
    found = _lineage(seeds={"seed.shop.codes": "seeds/codes.csv"})
    [held] = ck.seed_candidates(
        found, {"seed.shop.codes": (["1" * 40], "2" * 40)}, BOUNDS, {}
    )

    assert held.status == "confirmed"
    assert (held.evidence["hash_before"], held.evidence["hash_after"]) == (
        "1" * 40,
        "2" * 40,
    )


def test_a_seed_one_bound_already_held_was_not_edited_in_the_range():
    bounds = ck.Bounds(through="c" * 40, after=("a" * 40, "b" * 40), last_good="a" * 40)
    found = _lineage(seeds={"seed.shop.codes": "seeds/codes.csv"})
    [held] = ck.seed_candidates(
        found, {"seed.shop.codes": (["1" * 40, "2" * 40], "2" * 40)}, bounds, {}
    )

    assert (held.status, held.reason) == ("ruled_out", "same_hash")


def test_a_seed_absent_at_a_bound_is_not_checked():
    found = _lineage(seeds={"seed.shop.codes": "seeds/codes.csv"})
    [held] = ck.seed_candidates(
        found, {"seed.shop.codes": ([None], "2" * 40)}, BOUNDS, {}
    )

    assert (held.status, held.reason) == ("not_checked", "seed_file_absent")


def test_no_seed_in_the_lineage_rules_the_seed_out():
    [held] = ck.seed_candidates(_lineage(), {}, BOUNDS, {})

    assert (held.status, held.reason) == ("ruled_out", "no_seed_in_lineage")


# --- the schedule ---------------------------------------------------------------------


def _span() -> diag.DiagnosisRange:
    return diag.DiagnosisRange(
        after=RunPoint(run="good", sequence=1, at=T0),
        through=RunPoint(run="bad", sequence=2, at=T0 + timedelta(hours=1)),
    )


def _b(model: str, run: str, minutes: float) -> ck.Build:
    at = T0 + timedelta(minutes=minutes)
    return ck.Build(model, run, at, at + timedelta(seconds=30))


PAIR = ("model.shop.fct", "model.shop.dim")
NAMES = {"model.shop.fct": "fct", "model.shop.dim": "dim"}


def _in_order_at_good() -> list[ck.Build]:
    return [_b("model.shop.dim", "good", 1), _b("model.shop.fct", "good", 2)]


def test_a_parent_built_after_its_child_only_at_the_failure_is_the_cause():
    builds = [
        *_in_order_at_good(),
        _b("model.shop.fct", "job-f", 40),
        _b("model.shop.dim", "job-d", 50),
    ]
    held = ck.schedule_candidate(PAIR, NAMES, builds, _span())

    assert held.status == "confirmed"
    assert held.evidence["failing_run"]["parent"]["invocation_id"] == "job-d"


def test_a_parent_built_first_at_the_failure_rules_the_schedule_out():
    builds = [
        *_in_order_at_good(),
        _b("model.shop.dim", "job-d", 40),
        _b("model.shop.fct", "job-f", 50),
    ]
    held = ck.schedule_candidate(PAIR, NAMES, builds, _span())

    assert (held.status, held.reason) == ("ruled_out", "parent_first")


def test_an_order_already_flipped_at_the_last_pass_did_not_change():
    builds = [
        _b("model.shop.fct", "f0", -20),
        _b("model.shop.dim", "d0", -10),
        _b("model.shop.fct", "job-f", 40),
        _b("model.shop.dim", "job-d", 50),
    ]
    held = ck.schedule_candidate(PAIR, NAMES, builds, _span())

    assert (held.status, held.reason) == ("ruled_out", "order_unchanged")


def test_a_build_the_failing_run_made_itself_is_the_one_its_test_read():
    # A later job's build of the parent is not what the failing run read.
    builds = [
        *_in_order_at_good(),
        _b("model.shop.dim", "bad", 61),
        _b("model.shop.fct", "bad", 62),
        _b("model.shop.dim", "later", 90),
    ]
    held = ck.schedule_candidate(PAIR, NAMES, builds, _span())

    assert (held.status, held.reason) == ("ruled_out", "parent_first")


def test_no_build_recorded_leaves_the_schedule_not_checked():
    # The dimension was never built, so its order cannot be read.
    builds = [_b("model.shop.fct", "good", 2)]
    held = ck.schedule_candidate(PAIR, NAMES, builds, _span())

    assert (held.status, held.reason) == ("not_checked", "no_build_recorded")


# --- the git helpers ------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=dex",
            "-c",
            "user.email=dex@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    )
    return done.stdout.strip()


def test_the_files_a_range_touched_are_listed_from_the_project_directory(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    project = repo / "dbt"
    (project / "seeds").mkdir(parents=True)
    _git(repo, "init", "-q")
    (project / "seeds" / "codes.csv").write_text("a\n1\n")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "root")
    root = _git(repo, "rev-parse", "HEAD")
    (project / "seeds" / "codes.csv").write_text("a\n2\n")
    (repo / "README.md").write_text("y\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "edit")
    head = _git(repo, "rev-parse", "HEAD")

    history = GitHistory(project)
    touched, count = history.touching(head, [root])

    # Spelled from the project directory, and nothing outside it.
    assert touched == ((head, ("seeds/codes.csv",)),)
    assert count == 1
    # The root commit itself is a range of one, read with no bound.
    assert history.touching(root, [])[0] == ((root, ("seeds/codes.csv",)),)
    assert history.blob_id(root, "seeds/codes.csv") != history.blob_id(
        head, "seeds/codes.csv"
    )
    assert history.blob(head, "seeds/codes.csv") == "a\n2\n"
    assert history.blob(root, "seeds/missing.csv") is None
    assert history.blob_id(root, "seeds/missing.csv") is None


def test_dex_s_own_state_and_dbt_s_output_belong_to_no_owner():
    owned = ck.partition(
        [
            ".dex/cache.json",
            ".dex/snapshot.json",
            "target/manifest.json",
            "models/a.sql",
        ],
        [],
    )

    assert owned == {"seed": (), "package": (), "project": ("models/a.sql",)}


def test_a_moved_package_past_the_evidence_cap_is_still_matched():
    names = [f"p{n:02d}" for n in range(25)]
    lock = "packages:\n" + "".join(
        f"  - package: x/{n}\n    version: {{v}}\n    name: {n}\n" for n in names
    )
    held = ck.package_candidate(
        _lineage(packages=("p24",)),
        ck.parse_lock(lock.format(v="1.0.0")),
        ck.parse_lock(lock.format(v="2.0.0")),
        declared=True,
        bounds=BOUNDS,
    )

    assert held.status == "confirmed"
    assert held.evidence["packages"] == ["p24"]
    assert "p24 1.0.0 to 2.0.0" in (held.detail or "")


def test_a_conflict_resolution_s_own_edit_is_a_change_in_the_range(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.sql").write_text("select 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "root")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "a.sql").write_text("select 2\n")
    _git(repo, "commit", "-qam", "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "a.sql").write_text("select 3\n")
    _git(repo, "commit", "-qam", "main")
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "merge", "-q", "side"],  # noqa: S607
        capture_output=True,
        env=git_env(),
        check=False,
    )
    # The breaking line exists only in the resolution.
    (repo / "a.sql").write_text("select broken\n")
    _git(repo, "commit", "-qam", "merge")
    merge = _git(repo, "rev-parse", "HEAD")
    # Bounded by both parents, only the merge itself is in the range.
    parents = _git(repo, "rev-parse", f"{merge}^1", f"{merge}^2").split()
    touched, count = GitHistory(repo).touching(merge, [base, *parents])

    assert count == 1
    assert touched == ((merge, ("a.sql",)),)
