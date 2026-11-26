"""The portable plan lifecycle: export here, verify and apply somewhere else.

The reproduction on issue #441 walks four failures, and three of them are this
module's: the public plan surface was a summary rather than the plan, there was
no way to move a plan to a second checkout, and apply revalidated the preimage
but never the content it was about to write. Each has a test here that fails
without the contract.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from exmergo_dex_core import (
    DexConfig,
    DexEngine,
    PlanDigestMismatchError,
    PlanDocumentError,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.edits import EditOp, content_hash
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform.classify import ArtifactClassification
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.portable import (
    PLAN_DOCUMENT_SCHEMA_VERSION,
    PortableEdit,
    PortablePlan,
    plan_digest,
    verify_plan_document,
)

MART = "select 1 as id\n"
SCHEMA = "version: 2\nmodels:\n  - name: mart\n    description: A mart.\n"


def _engine(root: Path, **kwargs) -> DexEngine:
    return DexEngine.from_repo(str(root), **kwargs)


@pytest.fixture
def authored(dbt_project_dir: Path) -> tuple[Path, str, dict, str]:
    """A repo with one stored plan, plus its exported document and digest."""

    repo = dbt_project_dir.parent
    with _engine(repo) as engine:
        stored = engine.plan(
            "a mart and its schema",
            edits=[
                PlanEdit(
                    path="models/staging/mart.sql",
                    new_content=MART,
                    op=EditOp.UPSERT,
                    kind=EditKind.MODEL_SQL,
                ),
                PlanEdit(
                    path="models/staging/mart.yml",
                    new_content=SCHEMA,
                    op=EditOp.UPSERT,
                    kind=EditKind.SCHEMA_YML,
                ),
            ],
        )
        exported = engine.export_plan(stored.plan_id)
    return repo, stored.plan_id, exported.plan, exported.digest


def _second_checkout(
    repo: Path, tmp_path: Path, name: str, top: str = "analytics"
) -> Path:
    """A copy of the repo's git-tracked half: no `.dex/`, so no plan store.

    ``top`` is the directory under the repository root that holds the project.
    """

    target = tmp_path / name
    target.mkdir()
    shutil.copytree(repo / top, target / top)
    return target


def test_the_document_carries_the_plan_and_not_a_summary(authored):
    _repo, plan_id, document, digest = authored

    assert isinstance(document, PortablePlan)
    assert document["plan_id"] == plan_id
    assert document["schema_version"] == PLAN_DOCUMENT_SCHEMA_VERSION
    assert document["digest"] == digest
    # Every field the reproduction had to read out of `.dex/plans/<id>.json`.
    edit = next(e for e in document["edits"] if e["path"] == "models/staging/mart.sql")
    assert isinstance(edit, PortableEdit)
    assert edit["op"] == "upsert"
    assert edit["kind"] == "model_sql"
    assert edit["new_content"] == MART
    assert edit["new_content_hash"] == content_hash(MART)
    # A create pins no preimage, and that is a fact the applying side checks.
    assert edit["old_content_hash"] is None
    assert isinstance(edit.classification, ArtifactClassification)
    assert edit["classification"]["artifact_class"] == "executable"


def test_the_same_plan_exports_identically_from_two_checkouts(authored, tmp_path):
    repo, plan_id, document, digest = authored
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(repo, elsewhere)

    with _engine(elsewhere) as engine:
        again = engine.export_plan(plan_id)

    assert again.digest == digest
    assert isinstance(again.plan, PortablePlan)
    assert again.plan_model() is again.plan
    # Byte-identical, not merely equivalent: a host comparing two exports must
    # not have to know which fields are incidental.
    assert again.plan == document


def test_a_plan_applies_in_a_checkout_that_has_no_plan_store(authored, tmp_path):
    repo, _plan_id, document, digest = authored
    target = _second_checkout(repo, tmp_path, "fresh")
    assert not (target / ".dex").exists()

    with _engine(target) as engine:
        result = engine.apply_plan_document(document, expect_digest=digest)

    assert sorted(result.written) == [
        "models/staging/mart.sql",
        "models/staging/mart.yml",
    ]
    assert (
        target / "analytics" / "models" / "staging" / "mart.sql"
    ).read_text() == MART


def test_tampered_content_is_refused_and_nothing_is_written(authored, tmp_path):
    repo, _plan_id, document, digest = authored
    target = _second_checkout(repo, tmp_path, "tampered")
    doc = json.loads(json.dumps(document.data()))
    doc["edits"][0]["new_content"] += "\n-- added after authoring\n"

    with (
        _engine(target, confirmed=True) as engine,
        pytest.raises(
            PlanDigestMismatchError, match="does not match its recorded hash"
        ),
    ):
        engine.apply_plan_document(doc, expect_digest=digest)

    assert not (target / "analytics" / "models" / "staging" / "mart.sql").exists()


def test_a_repinned_digest_is_caught_by_the_digest_the_caller_carried(
    authored, tmp_path
):
    """Content, its hash, and the plan digest all changed together.

    This is the document a naive integrity check passes: it is internally
    consistent, and it is not the plan that was authored. Only the digest the
    caller carried through a channel it trusts catches it, which is why
    `expect_digest` exists and why the docs refuse to call the digest a
    signature.
    """

    repo, _plan_id, document, digest = authored
    target = _second_checkout(repo, tmp_path, "repinned")
    doc = json.loads(json.dumps(document.data()))
    doc["edits"][0]["new_content"] += "\n-- added after authoring\n"
    doc["edits"][0]["new_content_hash"] = content_hash(doc["edits"][0]["new_content"])
    doc["digest"] = plan_digest(PortablePlan.model_validate(doc))

    # It verifies on its own terms.
    assert verify_plan_document(doc).digest == doc["digest"]

    with (
        _engine(target, confirmed=True) as engine,
        pytest.raises(PlanDigestMismatchError, match="is not the"),
    ):
        engine.apply_plan_document(doc, expect_digest=digest)
    assert not (target / "analytics" / "models" / "staging" / "mart.sql").exists()


def test_a_changed_preimage_conflicts_rather_than_overwriting(authored, tmp_path):
    repo, _plan_id, document, _digest = authored
    target = _second_checkout(repo, tmp_path, "edited")
    existing = target / "analytics" / "models" / "staging" / "stg_customers.sql"
    original = existing.read_text()
    existing.write_text(original + "\n-- a human edited this here\n")

    doc = json.loads(json.dumps(document.data()))
    doc["edits"].append(
        {
            "path": "models/staging/stg_customers.sql",
            "op": "upsert",
            "kind": "model_sql",
            "old_content_hash": content_hash(original),
            "new_content_hash": content_hash("select 2 as id\n"),
            "new_content": "select 2 as id\n",
        }
    )
    doc["digest"] = plan_digest(PortablePlan.model_validate(doc))

    with _engine(target) as engine:
        result = engine.apply_plan_document(doc, expect_digest=doc["digest"])

    assert result.written == []
    assert [c["path"] for c in result.conflicts] == ["models/staging/stg_customers.sql"]
    assert result.pending_confirmation is not None
    # All-or-nothing: the two clean edits in the same plan are withheld too.
    assert not (target / "analytics" / "models" / "staging" / "mart.sql").exists()


def test_an_unreadable_document_refuses_rather_than_applying_part_of_it(
    authored, tmp_path
):
    repo, _plan_id, _document, _digest = authored
    target = _second_checkout(repo, tmp_path, "garbage")

    with _engine(target) as engine:
        for payload in ("not json at all", {"plan_id": "p1"}, {"edits": []}):
            with pytest.raises(PlanDocumentError):
                engine.apply_plan_document(payload)


def test_a_schema_version_this_engine_does_not_read_refuses_by_name(authored):
    _repo, _plan_id, document, _digest = authored
    doc = json.loads(json.dumps(document.data()))
    doc["schema_version"] = PLAN_DOCUMENT_SCHEMA_VERSION + 1

    with pytest.raises(PlanDocumentError, match="schema_version"):
        verify_plan_document(doc)


def test_apply_reaches_no_subprocess_no_socket_and_no_sql_parser(
    authored, tmp_path, monkeypatch
):
    """The offline half has to work in a sandbox that has none of those.

    Asserted by making each unavailable rather than by inspecting imports: a
    future refactor that reaches for one of them fails here rather than in a
    customer's disposable checkout with no network.
    """

    repo, _plan_id, document, digest = authored
    target = _second_checkout(repo, tmp_path, "offline")

    def refuse(*_args, **_kwargs):
        raise AssertionError("the offline apply path reached out")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    blocked = {"sqlglot", "jinja2", "dbt"} - set(sys.modules)

    class Blocker:
        def find_module(self, name, path=None):
            if name.split(".")[0] in blocked:
                raise AssertionError(f"the offline apply path imported {name}")

    monkeypatch.setattr(sys, "meta_path", [Blocker(), *sys.meta_path])

    with _engine(target) as engine:
        result = engine.apply_plan_document(document, expect_digest=digest)
    assert "models/staging/mart.sql" in result.written


def test_an_edit_outside_this_checkouts_surface_is_refused_not_written(
    authored, tmp_path
):
    """Containment is re-checked against the applying checkout, not trusted.

    A document is an artifact that crossed a boundary; what it was validated
    against is not what it is being written into.
    """

    repo, _plan_id, document, _digest = authored
    target = _second_checkout(repo, tmp_path, "escape")
    doc = json.loads(json.dumps(document.data()))
    doc["edits"] = [
        {
            "path": "../../etc/dex_was_here",
            "op": "upsert",
            "kind": "model_sql",
            "old_content_hash": None,
            "new_content_hash": content_hash("x"),
            "new_content": "x",
        }
    ]
    doc["digest"] = plan_digest(PortablePlan.model_validate(doc))

    with _engine(target, confirmed=True) as engine, pytest.raises(Exception) as caught:
        engine.apply_plan_document(doc, expect_digest=doc["digest"])
    assert "outside" in str(caught.value) or "surface" in str(caught.value)


def test_a_kind_that_disagrees_with_its_location_is_refused_in_the_second_checkout(
    authored, tmp_path
):
    repo, _plan_id, document, _digest = authored
    target = _second_checkout(repo, tmp_path, "misfiled")
    doc = json.loads(json.dumps(document.data()))
    doc["edits"] = [
        {
            # A macro filed under the model paths: dbt would parse it as a model.
            "path": "models/staging/helper.sql",
            "op": "upsert",
            "kind": "macro_sql",
            "old_content_hash": None,
            "new_content_hash": content_hash("{% macro helper() %}1{% endmacro %}"),
            "new_content": "{% macro helper() %}1{% endmacro %}",
        }
    ]
    doc["digest"] = plan_digest(PortablePlan.model_validate(doc))

    with _engine(target, confirmed=True) as engine, pytest.raises(Exception) as caught:
        engine.apply_plan_document(doc, expect_digest=doc["digest"])
    assert "macro" in str(caught.value)
    assert not (target / "analytics" / "models" / "staging" / "helper.sql").exists()


def test_export_with_no_id_takes_the_latest_unapplied_plan(dbt_project_dir: Path):
    repo = dbt_project_dir.parent
    with _engine(repo) as engine:
        engine.plan(
            "first",
            edits=[
                PlanEdit(
                    path="models/staging/a.sql",
                    new_content="select 1 as id\n",
                    op=EditOp.UPSERT,
                    kind=EditKind.MODEL_SQL,
                )
            ],
        )
        second = engine.plan(
            "second",
            edits=[
                PlanEdit(
                    path="models/staging/b.sql",
                    new_content="select 2 as id\n",
                    op=EditOp.UPSERT,
                    kind=EditKind.MODEL_SQL,
                )
            ],
        )
        assert engine.export_plan().plan_id == second.plan_id


def test_a_delete_carries_no_content_and_says_so(dbt_project_dir: Path):
    repo = dbt_project_dir.parent
    with _engine(repo) as engine:
        stored = engine.plan(
            "drop the schema entry",
            edits=[
                PlanEdit(
                    path="models/staging/schema.yml",
                    op=EditOp.DELETE,
                    kind=EditKind.SCHEMA_YML,
                )
            ],
        )
        document = engine.export_plan(stored.plan_id).plan

    edit = document["edits"][0]
    assert edit["op"] == "delete"
    assert edit["new_content"] is None
    assert edit["new_content_hash"] is None
    # A delete is classified from the file it removes, so the removal of
    # something executable is not waved through as nothing.
    assert edit["classification"]["basis"] == "existing_content"
    verify_plan_document(document)


def test_a_delete_carrying_content_is_refused(authored):
    _repo, _plan_id, document, _digest = authored
    doc = json.loads(json.dumps(document.data()))
    doc["edits"][0]["op"] = "delete"
    with pytest.raises(PlanDocumentError, match="carry no content"):
        verify_plan_document(doc)


def test_the_digest_ignores_when_the_plan_was_made(authored):
    """Two identical changes authored an hour apart are the same change.

    The plan id is already content-addressed, so a digest that moved with the
    clock would make two exports of one plan compare unequal for no reason a
    caller could act on.
    """

    _repo, _plan_id, document, digest = authored
    doc = json.loads(json.dumps(document.data()))
    doc["created_at"] = "2020-01-01T00:00:00+00:00"
    doc["engine_version"] = "0.0.0-something-else"
    assert plan_digest(PortablePlan.model_validate(doc)) == digest


def test_the_store_is_never_read_on_the_document_path(authored, tmp_path):
    """A host applying a document must not need dex's private plan store.

    Enforced by handing the engine a store whose plan tier raises, which is what
    a host with no plan store looks like from in here.
    """

    repo, _plan_id, document, digest = authored
    target = _second_checkout(repo, tmp_path, "no-store")

    class NoPlans(FilesystemStore):
        def load_plan(self, plan_id):
            raise AssertionError("the document path read the plan store")

        def latest_plan(self, kind=None):
            raise AssertionError("the document path read the plan store")

    with DexEngine(
        config=DexConfig(),
        store=NoPlans(target),
        repo_root=str(target),
    ) as engine:
        result = engine.apply_plan_document(document, expect_digest=digest)
    assert "models/staging/mart.sql" in result.written


# --- a project below the repository root --------------------------------------
#
# The reproduction on issue #527: a project pinned two directories down with
# `dbt_project_dir`, one model, and a plan that adds a second. Every field the
# digest covers is deterministic, so the plan id and both digests below are the
# same on any machine. The reporter measured the Windows digest on Windows, where
# `project_dir` used to be recorded as `wh\dbt`. Reproducing it here from the
# same plan is what lets these tests stand in for a Windows run on a POSIX one.

NESTED_INTENT = "add stg_two"
NESTED_PATH = "models/staging/stg_two.sql"
NESTED_SQL = "select id from {{ ref('stg_one') }}\n"
NESTED_PLAN_ID = "pfddea9171e"
NESTED_DIGEST = (
    "sha256:1972db2811c0804d2e6d9c0f59fc9fee6ab61fef40dca7eb6766d4233d465ce1"
)
WINDOWS_DIGEST = (
    "sha256:e356106129ffb09a3335e5aec15232847c4554aa7f058efd4c99710941a9bde7"
)


def _nested_repo(root: Path) -> Path:
    project = root / "wh" / "dbt"
    (project / "models" / "staging").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        "name: shop\nversion: '1.0.0'\nprofile: shop\nmodel-paths: ['models']\n",
        encoding="utf-8",
    )
    (project / "models" / "staging" / "stg_one.sql").write_text(
        "select 1 as id\n", encoding="utf-8"
    )
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        "dbt_project_dir: wh/dbt\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def nested(tmp_path: Path) -> tuple[Path, dict]:
    """A nested-project repo with one stored plan, plus its exported document."""

    repo = _nested_repo(tmp_path / "repo")
    with _engine(repo) as engine:
        stored = engine.plan(
            NESTED_INTENT,
            edits=[
                PlanEdit(
                    path=NESTED_PATH,
                    new_content=NESTED_SQL,
                    op=EditOp.UPSERT,
                    kind=EditKind.MODEL_SQL,
                )
            ],
        )
        exported = engine.export_plan(stored.plan_id)
    return repo, exported.plan


def _as_exported_on_windows(document: PortablePlan | dict) -> dict:
    """The same document as an engine that recorded `wh\\dbt` exported it.

    The digest is recomputed over the backslash, because that is the string the
    Windows export digested.
    """

    doc = json.loads(
        json.dumps(document.data() if hasattr(document, "data") else document)
    )
    doc["project_dir"] = "wh\\dbt"
    doc["digest"] = plan_digest(PortablePlan.model_validate(doc))
    return doc


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def test_a_nested_project_is_recorded_with_forward_slashes(nested):
    repo, document = nested

    assert FilesystemStore(repo).load_plan(NESTED_PLAN_ID).project_dir == "wh/dbt"
    assert document["project_dir"] == "wh/dbt"


def test_a_nested_plan_has_the_same_digest_on_every_platform(nested):
    """The plan id hashes only the intent and the edits, whose paths were already
    POSIX, so it agreed across platforms all along. The digest also covers
    `project_dir`, so it agrees only when that is spelled the same everywhere,
    and a host pinning `--expect-digest` from one platform refused the plan
    exported on the other."""

    _repo, document = nested

    assert document["plan_id"] == NESTED_PLAN_ID
    assert document["digest"] == NESTED_DIGEST
    # The backslash spelling reproduces the reporter's Windows measurement, so
    # the document the next test applies is the one Windows exported.
    assert _as_exported_on_windows(document)["digest"] == WINDOWS_DIGEST


def test_a_document_exported_on_windows_applies_in_a_posix_checkout(nested, tmp_path):
    """A document exported before `project_dir` was recorded with forward slashes
    still carries `wh\\dbt`. On POSIX that is one directory name, so the apply
    looked for a project that does not exist.

    The digest is still taken over the backslash as written: the document
    verifies against the digest a host pinned from it, and only the path it is
    joined into reads the backslash as a separator.
    """

    repo, document = nested
    legacy = _as_exported_on_windows(document)
    target = _second_checkout(repo, tmp_path, "posix", top="wh")

    with _engine(target) as engine:
        result = engine.apply_plan_document(legacy, expect_digest=WINDOWS_DIGEST)

    assert result.written == [NESTED_PATH]
    written = target / "wh" / "dbt" / "models" / "staging" / "stg_two.sql"
    assert written.read_text(encoding="utf-8") == NESTED_SQL
    assert [p.name for p in target.iterdir()] == ["wh"]


def test_a_nested_plan_applies_in_the_checkout_that_stored_it(nested):
    repo, _document = nested

    with _engine(repo) as engine:
        result = engine.apply(NESTED_PLAN_ID)

    assert result.written == [NESTED_PATH]
    assert result.commit_trailer == "Co-authored-by: Dex <dex@exmergo.com>"
    assert (repo / "wh" / "dbt" / NESTED_PATH).read_text(encoding="utf-8") == (
        NESTED_SQL
    )


def test_export_and_apply_plan_file_carry_a_nested_project_to_a_second_checkout(
    tmp_path, capsys
):
    """The command-line round trip a host runs: plan and export in one checkout,
    then `transform apply --plan-file` in another that has no `.dex/` at all."""

    repo = _nested_repo(tmp_path / "repo")
    edits_file = tmp_path / "edits.json"
    edits_file.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": NESTED_PATH,
                        "kind": "model_sql",
                        "op": "upsert",
                        "content": NESTED_SQL,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, planned = _run(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            NESTED_INTENT,
            "--edits-file",
            str(edits_file),
        ],
        capsys,
    )
    assert rc == 0, planned

    rc, exported = _run(["--repo-root", str(repo), "transform", "export"], capsys)
    assert rc == 0, exported
    assert exported["data"]["plan"]["project_dir"] == "wh/dbt"
    assert exported["data"]["digest"] == NESTED_DIGEST

    document_file = tmp_path / "plan.json"
    document_file.write_text(json.dumps(exported["data"]["plan"]), encoding="utf-8")
    target = _second_checkout(repo, tmp_path, "applying", top="wh")

    rc, applied = _run(
        [
            "--repo-root",
            str(target),
            "transform",
            "apply",
            "--plan-file",
            str(document_file),
            "--expect-digest",
            NESTED_DIGEST,
        ],
        capsys,
    )
    assert rc == 0, applied
    assert applied["data"]["written"] == [NESTED_PATH]
    assert applied["data"]["commit_trailer"] == (
        "Co-authored-by: Dex <dex@exmergo.com>"
    )
    assert (target / "wh" / "dbt" / NESTED_PATH).read_text(encoding="utf-8") == (
        NESTED_SQL
    )
