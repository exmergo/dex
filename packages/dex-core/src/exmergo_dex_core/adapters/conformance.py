"""The executable contract a project format has to satisfy, shipped for reuse.

The rules a format owes its callers are stated on the protocol members in
:mod:`.project`. This module is the same rules as assertions, packaged so a format
living outside this distribution can run them in its own test suite:

    from exmergo_dex_core.adapters.conformance import ExploreProjectContract

    class TestMyProject(ExploreProjectContract):
        def make_project(self):
            return MyProject(nothing_declared())

That is the whole integration. pytest collects the inherited ``test_*`` methods and
runs the contract against your format.

**Pick the class that matches the tier you implement**, mirroring the protocols
exactly: :class:`ExploreProjectContract` for a format that can state what it
declares, :class:`MaintainProjectContract` when it can also produce the two
snapshot layers, :class:`EditableProjectContract` when it can also receive an edit.
Subclassing a wider contract than your format implements is how you find out you
have not finished, so the narrow class is a feature rather than a convenience.

**A project is a source, not a sink, and that shapes this suite.** The storage
contract can write through the protocol and read back, so it needs no fixtures from
the implementer. Nothing here can put a declaration *into* your format, so the
assertions that check content arrive have to be handed a project already in a known
state. That is what the hooks are for, and why the content half is a separate
mix-in: a format is worth reading before it is worth trusting to round-trip a key.

**The assertion that matters most is behavioural, not structural.**
``definitions()`` must not raise -- not on an absent project, not on an ambiguous
one, not on a source it cannot parse. Exploration runs against warehouses with no
project at all, so absence is an ordinary state, and a format that raises turns it
into an outage. A contract asserting only shapes cannot catch that, which is why
:meth:`ExploreProjectContract.make_unreadable_project` exists and why it is worth
overriding even though it defaults to skipping.

**If your format can be handed a declaration to read**, mix
:class:`DeclaringProjectContract` in as well. It checks that a declared key and a
declared join actually reach the engine, which is the whole reason a project is
read on the explore path: those two arrive through ``definitions()`` and through no
other channel.

**If you reach tier 3 you also want** :class:`PlacingProjectContract`, because
placement is what carries a proposal from reconcile to your write path, and its two
methods have to agree with each other: a format that places an edit outside the
surface it declares builds a proposal the plan store then refuses, which reads as
dex declining rather than as the format contradicting itself.

**If dex is going to build your format rather than be handed one**, mix
:class:`ProjectFactoryContract` in front. Construction is a separate contract from
the tiers for the same reason it is separate in storage, and a format that passes
the behavioral suite can still be unreachable from configuration, so "the suite is
green" should mean correct *and* constructable.

This module imports pytest, so it is deliberately not imported by
:mod:`exmergo_dex_core.adapters`: a bare ``import exmergo_dex_core`` must not
require a test framework. Install the ``[project-conformance]`` extra to get what
running the suite needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import pytest

from ..dbt_project import EditOp, ProjectDefinitions
from ..maintain.snapshot import Snapshot
from .project import ExploreProject, ProjectContext, tier_of

if TYPE_CHECKING:
    from typing import Any

    from ..maintain.snapshot import SemanticLayer, TransformLayer

__all__ = [
    "DeclaringProjectContract",
    "EditableProjectContract",
    "ExploreProjectContract",
    "MaintainProjectContract",
    "PlacingProjectContract",
    "ProjectFactoryContract",
    "SemanticProjectContract",
]


def _probe_content(path: str) -> str:
    """Content for an edit that must never land, commented for the file it names.

    A refusal is what is being asserted, so nothing should ever read this. It is
    still written as a comment in the target's own language, because a format
    that parses what it is handed before refusing it should refuse it for the
    reason under test rather than for being unparseable.
    """

    marker = "-- " if path.endswith(".sql") else "# "
    return f"{marker}dex conformance probe: this must never be written\n"


def _edit_like(template: Any, **changes: Any) -> Any:
    """A copy of an edit the format was handed, with fields replaced.

    The assertions below vary one field of a real edit (its path, or the hash it
    is pinned to) rather than constructing one, so what reaches ``write_edits``
    is the same shape dex passes it everywhere. Every caller in the engine passes
    ``Edit`` or ``PlanEdit``, which is what makes this safe to assume.
    """

    copier = getattr(template, "model_copy", None)
    assert copier is not None, (
        "the edits returned by an_edit_against_a_changed_target() are not dex's "
        f"`Edit`/`PlanEdit` models but {type(template).__name__}. write_edits() is "
        "called with those models from every path in the engine, so the hook has "
        "to stage what the format will really be handed"
    )
    return copier(update=changes)


class ExploreProjectContract:
    """Tier 1. Subclass and implement :meth:`make_project`.

    Every assertion here is format-independent: it asks what dex may assume about
    any project it is handed, never what a particular format looks like.
    """

    def make_project(self) -> ExploreProject:
        """A project of your format with nothing declared in it.

        The empty case rather than the interesting one, because it is the case dex
        actually meets most often: a repo with no project, a warehouse explored
        bare. If your format cannot represent "nothing declared", that is worth
        knowing before you go further -- dex reaches this state on every explore
        run that does not ask for a project.
        """

        raise NotImplementedError

    def make_unreadable_project(self) -> ExploreProject | None:
        """A project whose source your format cannot parse, or ``None``.

        Override this. It is the hook behind the contract's most valuable
        assertion, and it defaults to ``None`` -- which skips those assertions with
        a message saying so -- only because a format may genuinely have no
        unparseable state: one reduced from an object already in memory cannot be
        malformed the way a file can.

        A default that skips is a compromise, and it is the right one here: making
        it mandatory would lock out a format for which the state does not exist,
        and the skip is explicit in the report rather than silent.
        """

        return None

    # --- what dex may assume of any project -----------------------------------

    def test_satisfies_the_explore_tier(self) -> None:
        assert tier_of(self.make_project()) >= 1

    def test_names_its_format_and_not_the_instance(self) -> None:
        """``name`` identifies the format, so two instances agree on it.

        A name that varies per instance is a name a registry cannot resolve, and
        the mistake is easy to make by deriving it from a path or an id.
        """

        first = self.make_project()
        second = self.make_project()
        assert first.name
        assert first.name == second.name

    def test_an_empty_project_declares_nothing_without_raising(self) -> None:
        definitions = self.make_project().definitions()

        assert isinstance(definitions, ProjectDefinitions)
        assert definitions.declared_keys == []
        assert definitions.declared_composite_keys == []
        assert definitions.foreign_keys == []

    def test_definitions_is_repeatable(self) -> None:
        """Two reads of an unchanged project agree.

        Not a caching requirement -- a format may re-read its source every call.
        What it rules out is a read that consumes something, which is a real
        failure mode for a format reduced from a stream or an iterator, and one
        that surfaces as a second command mysteriously seeing less than the first.
        """

        project = self.make_project()

        assert project.definitions() == project.definitions()

    def test_an_unreadable_project_does_not_raise(self) -> None:
        project = self.make_unreadable_project()
        if project is None:
            pytest.skip(
                "make_unreadable_project() returned None: this format declares it "
                "has no unparseable state"
            )

        definitions = project.definitions()

        assert isinstance(definitions, ProjectDefinitions)

    def test_an_unreadable_project_says_why_rather_than_going_quiet(self) -> None:
        """Degrading quietly is not the same as degrading silently.

        An empty result with no note is indistinguishable from a project that
        genuinely declares nothing, so the operator cannot tell a typo in their
        source from a correct empty read. ``notes`` is where that difference goes.
        """

        project = self.make_unreadable_project()
        if project is None:
            pytest.skip(
                "make_unreadable_project() returned None: this format declares it "
                "has no unparseable state"
            )

        assert project.definitions().notes


class DeclaringProjectContract:
    """Opt-in: the declarations dex reads a project *for* actually arrive.

    Mix in beside the contract for your tier::

        class TestMyProject(DeclaringProjectContract, ExploreProjectContract):
            def make_project(self): ...
            def a_project_declaring_a_unique_key(self): ...
            def a_project_declaring_a_join(self): ...

    Separate from the tier contract because the two answer different questions. The
    tier contract asks whether dex can safely call your format at all; this asks
    whether reading it was worth doing.
    """

    def a_project_declaring_a_unique_key(self) -> tuple[ExploreProject, str, str]:
        """A project declaring one single-column unique key.

        Returns ``(project, model, column)`` -- the project, and what dex should
        see in it. Naming the expectation here rather than fixing it in the suite
        keeps your format's own vocabulary out of the assertion: whatever you call
        the model, the contract checks that dex learns that name.
        """

        raise NotImplementedError

    def a_project_declaring_a_join(
        self,
    ) -> tuple[ExploreProject, str, str, str, str]:
        """A project declaring one single-column join between two models.

        Returns ``(project, model, column, to_model, to_column)``.
        """

        raise NotImplementedError

    def a_project_declaring_a_composite_key(
        self,
    ) -> tuple[ExploreProject, str, tuple[str, ...]] | None:
        """A project whose grain needs more than one column, or ``None``.

        Returns ``(project, model, columns)`` with ``columns`` in declared order.

        **Declare more than two columns.** A format that handles a composite key
        by special-casing the pair satisfies a two-column fixture and fails a
        four-column one, so a pair cannot tell you what you came to find out.

        ``None`` skips, because a format may genuinely have no way to express a
        multi-column grain: dbt's own ``unique`` test is column level, and a
        format modelling grain one column at a time is not incomplete, it is
        differently shaped. Override it if you can express one, because
        ``declared_composite_keys`` is a separate field from ``declared_keys``
        and nothing else in this suite reaches it.

        **What getting this wrong costs, now that the field is read.** It is no
        longer decoration on a diagram. The grain axis re-verifies the
        combinations that arrive here, and reconcile checks them before proposing
        a column-level ``unique``, so a format that leaves this empty loses grain
        verification on exactly the tables whose grain is composite, and gets
        offered ``unique`` tests on their member columns: assertions its own
        parser is entitled to discard and dbt is entitled to fail every build on.
        """

        return None

    def a_project_declaring_a_join_with_differently_named_sides(
        self,
    ) -> tuple[ExploreProject, str, str, str, str] | None:
        """A join whose two ends are spelled differently, or ``None``.

        Returns ``(project, model, column, to_model, to_column)``, and ``column``
        must not equal ``to_column`` -- this contract checks that and refuses a
        mirrored fixture, because a mirrored one cannot fail for the right reason.

        **This is a distinct case from :meth:`a_project_declaring_a_join` and not
        a redundant one.** If both ends of your fixture are named ``date``, an
        implementation that reads the source column and copies it onto the target
        satisfies the assertion exactly, and the defect ships. A foreign key whose
        two ends are spelled differently is the ordinary case rather than the
        exotic one, and a format that mirrors would join on a column the target may
        not have, surfacing as a wrong answer rather than as a failure.
        """

        return None

    def test_a_declared_unique_key_reaches_the_engine(self) -> None:
        project, model, column = self.a_project_declaring_a_unique_key()

        declared = project.definitions().declared_keys
        matching = [k for k in declared if k.model == model and k.column == column]

        assert matching, f"expected a declared key on {model}.{column}, got {declared}"
        assert matching[0].unique, (
            "a key declared unique must arrive with unique=True; a grain that "
            "arrives without it is read as an ordinary column"
        )

    def test_a_declared_join_carries_both_sides(self) -> None:
        """Both ends, because a half-read join is worse than an unread one.

        A join naming the wrong side sends the relationship detector looking for a
        key that is not there, and the finding it produces reads like a data
        problem rather than a misread declaration.
        """

        project, model, column, to_model, to_column = self.a_project_declaring_a_join()

        declared = project.definitions().foreign_keys
        matching = [
            fk
            for fk in declared
            if fk.model == model
            and fk.column == column
            and fk.to_model == to_model
            and fk.to_column == to_column
        ]

        assert matching, (
            f"expected a declared join {model}.{column} -> {to_model}.{to_column}, "
            f"got {declared}"
        )

    def test_a_composite_grain_keeps_every_column_and_their_order(self) -> None:
        """A truncated composite key is silent, which is what makes it expensive.

        It does not read as a missing declaration. It reads as a declared grain
        that is simply narrower than the truth, so every check downstream runs
        against a grain the author never claimed and the findings look like data
        problems rather than a misread declaration.
        """

        supplied = self.a_project_declaring_a_composite_key()
        if supplied is None:
            pytest.skip(
                "a_project_declaring_a_composite_key() returned None: this format "
                "declares it cannot express a multi-column grain, so "
                "declared_composite_keys goes unchecked"
            )
        project, model, columns = supplied

        declared = project.definitions().declared_composite_keys
        matching = [k for k in declared if k.model == model]

        assert matching, (
            f"expected a composite key on {model}, got {declared}. A multi-column "
            "grain belongs in declared_composite_keys, not as several entries in "
            "declared_keys: those say each column is unique on its own, which is a "
            "different and much stronger claim"
        )
        assert tuple(matching[0].columns) == tuple(columns), (
            f"expected columns {tuple(columns)} in order, got "
            f"{tuple(matching[0].columns)}"
        )
        assert len(matching) == 1, (
            f"expected one composite key on {model}, got {len(matching)}: "
            f"{matching}. One declaration is one grain, and splitting it across "
            "entries makes the grain axis verify combinations the project never "
            "declared"
        )
        leaked = sorted(
            key.column
            for key in project.definitions().declared_keys
            if key.model == model
            and key.unique
            and key.column.lower() in {c.lower() for c in columns}
        )
        assert not leaked, (
            f"{model} reports {leaked} as unique on their own while also declaring "
            f"the composite grain {tuple(columns)}. The fixture's grain needs every "
            "one of those columns, so no single one of them is unique, and the "
            "stronger claim is the one that gets acted on: reconcile reads it as a "
            "grain the project already asserts and proposes edits against it"
        )

    def test_a_join_keeps_its_two_sides_apart_when_they_are_named_differently(
        self,
    ) -> None:
        """The case :meth:`test_a_declared_join_carries_both_sides` cannot reach.

        An implementation that mirrors the source column onto the target passes
        that one whenever the fixture's two ends share a name, and this is the
        assertion that separates them.
        """

        supplied = self.a_project_declaring_a_join_with_differently_named_sides()
        if supplied is None:
            pytest.skip(
                "a_project_declaring_a_join_with_differently_named_sides() returned "
                "None: a join whose ends are spelled differently goes unchecked, so "
                "an implementation that mirrors one side onto the other would pass "
                "this suite"
            )
        project, model, column, to_model, to_column = supplied

        assert column != to_column, (
            "this fixture has to name its two sides differently, or it cannot "
            "detect the mirroring it exists to detect"
        )

        declared = project.definitions().foreign_keys
        matching = [
            fk
            for fk in declared
            if fk.model == model and fk.column == column and fk.to_model == to_model
        ]

        assert matching, (
            f"expected a declared join from {model}.{column} to {to_model}, "
            f"got {declared}"
        )
        assert matching[0].to_column == to_column, (
            f"the join's target column arrived as {matching[0].to_column!r}, "
            f"expected {to_column!r}. Reading it as {column!r} is the mirroring "
            "failure: the far side is a column the target may not even have"
        )


class SemanticProjectContract:
    """Opt-in, tier 2: a populated semantic layer keeps the column behind each field.

    Mix in beside :class:`MaintainProjectContract` when your format declares
    semantics at all::

        class TestMyProject(SemanticProjectContract, MaintainProjectContract):
            def make_project(self): ...
            def a_project_declaring_a_semantic_model(self): ...

    **Why this is separate from the tier contract, and worth the trouble.**
    :class:`MaintainProjectContract` asserts that an *empty* project produces an
    empty semantic layer. Nothing there looks at a populated one, so a format that
    reads every dimension and measure name and drops the physical column behind
    each passes the tier suite completely.

    That is not a hypothetical shape. ``SemanticModelDef`` keys every field to a
    warehouse column, and ``maintain``'s drift detector skips any field whose column
    is ``None`` -- correctly, because it cannot resolve what it was not given. So a
    layer mapped entirely to ``None`` validates, serializes, and compares clean
    forever: the check does not fail, it never runs, and a dropped warehouse column
    that should raise ``dangling_reference`` at high severity raises nothing. The
    absence is indistinguishable from agreement, which is the worst property a
    check can have.

    A format that genuinely declares no semantics should not mix this in. Its empty
    layer is correct and the tier contract already covers it.

    **Semantics presuppose tier 2, and this checks that first.** ``semantic_layer``
    is a tier-2 member, so every assertion below would otherwise fail with
    ``AttributeError: 'MyProject' object has no attribute 'semantic_layer'`` for a
    format that mixed this in beside :class:`ExploreProjectContract` -- an error
    about a missing attribute, when the thing to say is that the format has not
    reached the tier the attribute belongs to. The tier is asserted against the
    project this contract is actually given rather than against ``make_project()``,
    so the mixin keeps depending only on the one fixture it declares.
    """

    def test_declaring_semantics_presupposes_the_maintain_tier(self) -> None:
        project, _, _, _ = self.a_project_declaring_a_semantic_model()

        assert tier_of(project) >= 2, (
            "a format declaring semantics has to reach tier 2, because "
            "semantic_layer() is a MaintainProject member and every assertion in "
            "this contract calls it. Implement transform_layer() and "
            "semantic_layer(), or drop this mixin if the format declares no "
            "semantics -- an empty layer is already correct under the tier contract"
        )

    def a_project_declaring_a_semantic_model(
        self,
    ) -> tuple[Any, str, Mapping[str, str | None], Mapping[str, str | None]]:
        """A project declaring one semantic model.

        Returns ``(project, name, dimensions, measures)``, where the two mappings
        are ``field name -> the warehouse column behind it``, exactly as you expect
        them to arrive on ``SemanticModelDef``.

        **Map a field to ``None`` when your format has no bare column for it**, and
        include at least one such field if your format can produce one: a computed
        field, or one whose expression is not a plain column name. ``None`` is the
        honest answer there and an invented column is not, because a consumer
        resolving column names would treat a fabricated one as a reference that no
        longer resolves.
        """

        raise NotImplementedError(
            "a semantic conformance subclass must implement "
            "a_project_declaring_a_semantic_model() -> (project, name, dimensions, "
            "measures), mapping each field to the warehouse column behind it"
        )

    def test_a_semantic_field_carries_the_column_behind_it(self) -> None:
        project, name, dimensions, measures = (
            self.a_project_declaring_a_semantic_model()
        )

        layer = project.semantic_layer()
        matching = [m for m in layer.semantic_models if m.name == name]

        assert matching, (
            f"expected a semantic model named {name!r}, got "
            f"{[m.name for m in layer.semantic_models]}"
        )
        model = matching[0]
        assert dict(model.dimensions) == dict(dimensions), (
            "the dimension to column mapping did not survive. A layer whose columns "
            "are all None still validates and still compares clean, so the drift "
            "check simply never runs"
        )
        assert dict(model.measures) == dict(measures), (
            "the measure to column mapping did not survive; see above"
        )

    def test_a_categorical_dimension_maps_only_to_a_real_column(self) -> None:
        """``categorical_dimensions`` takes ``str``, not ``str | None``.

        So a field that is categorical *and* unresolved cannot be represented
        there, and the two properties have to stay independent: being categorical
        says how the field behaves, having a column says whether it can be checked.
        A format that collapses them either drops a categorical field that happens
        to lack a column, or supplies an invented column to keep it. Both are worse
        than leaving it out of this one mapping, which is what the typing asks for.
        """

        project, name, _, _ = self.a_project_declaring_a_semantic_model()

        model = next(
            m for m in project.semantic_layer().semantic_models if m.name == name
        )

        columns = model.categorical_dimensions.values()
        assert all(isinstance(c, str) and c for c in columns), (
            "categorical_dimensions holds a null or empty column: its values are "
            f"required strings, got {model.categorical_dimensions!r}. Leave an "
            "unresolved categorical field out of this mapping rather than "
            "inventing a column to keep it in"
        )
        assert set(model.categorical_dimensions) <= set(model.dimensions), (
            "categorical_dimensions names a field that is not a dimension: "
            f"{sorted(set(model.categorical_dimensions) - set(model.dimensions))}"
        )


class MaintainProjectContract(ExploreProjectContract):
    """Tier 2. Inherits every tier-1 assertion.

    The layers are what a snapshot is taken of, so a format reaching this tier can
    be a drift baseline. What is asserted here is deliberately thin: the layers'
    contents are the format's business, and the one cross-tier rule worth holding
    is that the two channels agree about which models exist.
    """

    def test_satisfies_the_maintain_tier(self) -> None:
        assert tier_of(self.make_project()) >= 2

    def test_both_layers_are_produced_with_nothing_declared(self) -> None:
        """Tier 2 is reached with nothing declared, not only with content.

        A format that only produces layers once something is declared cannot be
        snapshotted on its first run, which is the run that establishes the
        baseline every later drift report compares against.

        Note what is *not* asserted: that the layers are empty. A project can build
        models while declaring nothing about them -- that is the ordinary state of
        an uninstrumented project -- so emptiness here would be a claim about the
        fixture rather than about the format. What has to hold is that both
        channels answer at all.
        """

        project = self.make_project()

        transform: TransformLayer = project.transform_layer()
        semantic: SemanticLayer = project.semantic_layer()

        assert isinstance(transform.models, list)
        assert isinstance(transform.model_refs, dict)
        assert semantic.semantic_models == []
        assert semantic.metrics == []

    def test_the_two_channels_agree_about_which_models_exist(self) -> None:
        """A model in the transform layer is a model ``definitions()`` built.

        The layers and the definitions are read by different commands, and a
        format producing them from two different traversals can disagree. Drift
        then reports a model as added or orphaned on the strength of which channel
        happened to be read.
        """

        project = self.make_project()

        layer_models = set(project.transform_layer().models)
        built = {name.lower() for name in project.definitions().built_relation_names}

        assert {m.lower() for m in layer_models} <= built or not built, (
            f"transform_layer() reports models {sorted(layer_models)} that "
            f"definitions() does not list as built: {sorted(built)}"
        )

    def test_the_layers_survive_a_snapshot_round_trip(self) -> None:
        """A tier-2 format's layers can be persisted as a baseline and read back.

        This is what reaching tier 2 buys, so it is worth asserting rather than
        assuming: the layers go into a `Snapshot`, a store serializes it, and a
        later command loads it and diffs against it. A layer that cannot survive
        that trip is not a baseline, and the failure would surface on the *next*
        run rather than the one that produced it.

        The assertion is equality after a JSON round trip specifically, not a
        deep copy, because the in-memory store copies rather than serializing and
        would hide the whole class of defect this catches: a value the format
        chose that the model accepts in Python and rejects on the way back.
        """

        project = self.make_project()

        snap = Snapshot(
            created_at="2026-01-01T00:00:00+00:00",
            transform_layer=project.transform_layer(),
            semantic_layer=project.semantic_layer(),
        )
        restored = Snapshot.model_validate_json(snap.model_dump_json())

        assert restored.transform_layer == snap.transform_layer
        assert restored.semantic_layer == snap.semantic_layer


class EditableProjectContract(MaintainProjectContract):
    """Tier 3. Inherits every tier-1 and tier-2 assertion.

    The write tier is the one where getting it wrong costs someone their work
    rather than costing an inaccurate report, so what is asserted here is
    behavioural and none of it is optional.

    **The hook below is mandatory, unlike :meth:`make_unreadable_project`.** That
    one defaults to skipping because a format may genuinely have no unparseable
    state. This one gets no such excuse: a format reaching tier 3 writes into a
    source of truth a human can also edit, so the state where the human got there
    first exists for every format that reaches it. A tier-3 format that cannot
    stage that scenario cannot detect it either, and a format that cannot detect it
    overwrites work silently, which is the one failure this seam exists to prevent.

    **The first two write assertions have to be read as a pair.** Refusing an
    unconfirmed conflict is satisfied trivially by a ``write_edits`` that never
    writes anything, so the confirmed case is what rules that out. Neither is worth
    much alone.

    **The pair rules out a writer that never writes. It does not rule out a writer
    that writes too much**, which is the direction the rest of the assertions
    cover: an apply that lands the clean half of a refused edit set, and a create
    pinned to no prior content that overwrites a file which appeared during
    review. Both leave the project holding something nobody proposed, both report
    themselves as a clean refusal, and neither is visible to a contract that
    stages one edit and asks only what came back. Supplying
    :meth:`a_clean_edit` upgrades the first from checking what the writer said to
    checking what the project holds.

    **Deliberately not asserted here: which directory the edits land in.**
    ``project_dir`` is a slot for the formats keyed by one, so an assertion about it
    would really be an assertion about being a filesystem format. The shipped dbt
    format asserts it where it means something, in
    ``tests/adapters/test_project_parity.py``.
    """

    def an_edit_against_a_changed_target(self) -> tuple[Any, Any, Any, Any]:
        """A staged conflict: ``(project, project_dir, edits, read_target)``.

        Build a project, plan an edit against something in it, then change that
        target behind the plan's back, which is what a human editing during review
        does. Return, in order:

        - ``project``: the tier-3 project the edits are written through.
        - ``project_dir``: whatever the caller should pass as ``write_edits``'s
          second argument. A format not keyed by a directory returns ``None``.
        - ``edits``: the edit set, pinned to the content from *before* the change.
        - ``read_target``: a zero-argument callable returning what the target holds
          right now. Any value that compares equal to itself will do; these
          assertions only ask whether it moved.

        Return a freshly staged scenario on every call. Both assertions below use
        it and one of them writes, so a shared fixture would let the second see what
        the first left behind.
        """

        raise NotImplementedError(
            "a tier-3 conformance subclass must implement "
            "an_edit_against_a_changed_target() -> (project, project_dir, edits, "
            "read_target), staging the case the write tier exists to get right: a "
            "human edited the target after the plan pinned its content"
        )

    def a_clean_edit(self, project: Any) -> tuple[Any, Any] | None:
        """An edit that is *not* in conflict, and a callable reading its target.

        Optional, and worth supplying. It is the oracle for
        :meth:`test_a_refused_apply_leaves_every_target_alone`: without it that
        assertion can only ask what ``write_edits`` *reported*, so a writer that
        writes half an edit set and says it wrote nothing still passes. With it,
        the target is read directly and the claim is checked against the project.

        Return ``(edit, read_target)``, where ``edit`` is pinned truthfully
        against current content (it must be clean, since the conflict under test
        is the other one) and ``read_target`` is a zero-argument callable
        returning what its target holds right now. Any value that compares equal
        to itself will do.

        ``project`` is the one :meth:`an_edit_against_a_changed_target` just
        staged, and the edit has to be valid against *that* project rather than
        against a fresh one: both edits are written through it in a single call,
        and the reader has to be looking at the same place the write lands.

        Returning ``None`` falls back to an edit derived from the staged
        conflict's own path, which is inside your surface by construction (it is
        a sibling of a path you already accept) and needs nothing from you.
        """

        return None

    def test_satisfies_the_editable_tier(self) -> None:
        assert tier_of(self.make_project()) >= 3

    def test_an_unconfirmed_write_refuses_a_target_that_moved(self) -> None:
        """The human edit survives, and nothing is written.

        This is propose-don't-impose at the only layer that can enforce it. dex
        pins the content an edit was planned against precisely so the window
        between planning and applying, which is a human review and can be long, does
        not end with someone's work replaced by a proposal written before it
        existed.
        """

        project, project_dir, edits, read_target = (
            self.an_edit_against_a_changed_target()
        )
        before = read_target()

        project.write_edits(edits, project_dir)

        assert read_target() == before, (
            "write_edits() overwrote a target that changed after the edit was "
            "planned, with confirmed left at its default. A conflict has to refuse "
            "the whole apply and write nothing until a human confirms it"
        )

    def test_a_confirmed_write_overrides_the_conflict(self) -> None:
        """``confirmed=True`` is the human saying they looked and meant it.

        Without this the refusal above is satisfied by a write path that never
        writes, and a format could pass the contract by doing nothing at all.
        """

        project, project_dir, edits, read_target = (
            self.an_edit_against_a_changed_target()
        )
        before = read_target()

        project.write_edits(edits, project_dir, confirmed=True)

        assert read_target() != before, (
            "write_edits() left the target unchanged with confirmed=True. The "
            "override is what a human reaches for after reading the conflict "
            "diffs, so a format that refuses either way has no apply path"
        )

    def test_a_refused_apply_leaves_every_target_alone(self) -> None:
        """A conflict refuses the apply, not the conflicting edit within it.

        ``write_edits`` is all-or-nothing, and the edit set is the unit. A writer
        that lands the clean edits and holds back the conflicting one leaves the
        project in a state neither the proposal nor the human intended, with no
        record of which half arrived: the plan reads as refused, the tree does
        not match it, and the diffs a reviewer was shown describe a change that
        partly happened.

        It is the failure worth catching most and the one a single-edit set
        cannot see, which is why this stages two. The clean edit goes first, so a
        writer working through the set in order reaches it before it discovers
        the conflict.
        """

        project, project_dir, edits, read_target = (
            self.an_edit_against_a_changed_target()
        )
        staged = list(edits)
        assert staged, (
            "an_edit_against_a_changed_target() returned no edits, so there is no "
            "conflict staged and nothing here can be asserted"
        )
        supplied = self.a_clean_edit(project)
        if supplied is None:
            template = staged[0]
            sibling = PurePosixPath(template.path)
            clean_path = str(sibling.with_name(f"dex_conformance_clean_{sibling.name}"))
            clean = _edit_like(
                template,
                path=clean_path,
                old_content_hash=None,
                op=EditOp.UPSERT,
                new_content=_probe_content(clean_path),
            )
            read_clean = None
        else:
            clean, read_clean = supplied

        before = read_target()
        before_clean = read_clean() if read_clean is not None else None

        result = project.write_edits([clean, *staged], project_dir)

        assert read_target() == before, (
            "write_edits() overwrote the conflicting target inside a mixed edit "
            "set, with confirmed left at its default"
        )
        assert not getattr(result, "written", []), (
            "write_edits() refused an unconfirmed conflict and still reported "
            f"{list(getattr(result, 'written', []))} as written. The apply is "
            "all-or-nothing: one conflict refuses the whole set, so the clean "
            "edits beside it do not land either"
        )
        if read_clean is not None:
            assert read_clean() == before_clean, (
                "write_edits() wrote the clean edit while refusing the "
                "conflicting one beside it. That leaves the project matching "
                "neither the proposal nor what the human had, and the apply "
                "reports itself as refused, so nothing records which half landed"
            )

    def test_a_create_pinned_absent_refuses_a_target_that_now_exists(self) -> None:
        """``old_content_hash=None`` is a claim about the target, and it is checked.

        A create is pinned to nothing because there was nothing there when the
        plan was made. If a file has appeared at that path since, the claim is
        false and the write is exactly the overwrite this tier exists to refuse:
        the human who created it during review loses the whole file rather than
        the lines that diverged.

        A writer that reads ``None`` as "nothing to compare, go ahead" passes
        every other assertion here, because in the staged conflict the pinned
        hash is a real one.
        """

        project, project_dir, edits, read_target = (
            self.an_edit_against_a_changed_target()
        )
        staged = list(edits)
        assert staged, (
            "an_edit_against_a_changed_target() returned no edits, so there is no "
            "target to re-pin and nothing here can be asserted"
        )
        template = staged[0]
        before = read_target()
        create = _edit_like(
            template,
            old_content_hash=None,
            op=EditOp.UPSERT,
            new_content=_probe_content(template.path),
        )

        result = project.write_edits([create], project_dir)

        assert read_target() == before, (
            "write_edits() honored an edit pinned to no prior content over a "
            "target that exists, overwriting it whole. A pin of None says the "
            "file was absent at plan time; a file being there now is a conflict, "
            "not a create. (If the target your hook stages does not exist, the "
            "hook is not staging the case it documents.)"
        )
        assert not getattr(result, "written", []), (
            "write_edits() reported the create as written over an existing "
            "target, so the caller marks the plan applied and no one is asked "
            "about the file that appeared"
        )

    def test_the_write_result_reports_what_happened(self) -> None:
        """The caller has to be able to tell a refusal from an apply.

        ``transform apply`` reads ``written`` to decide whether the plan is now
        applied and ``conflicts`` to decide whether to surface the divergence for
        a human to confirm. Both readings fail closed on a result that answers
        neither, and they fail in opposite directions: a plan recorded as applied
        that wrote nothing, or a conflict that never reaches the person it was
        raised for.

        Asserted on the refusing case because that is the one where the two
        fields disagree, and so the one a result that reports nothing gets wrong.
        """

        project, project_dir, edits, _read_target = (
            self.an_edit_against_a_changed_target()
        )

        result = project.write_edits(edits, project_dir)

        assert hasattr(result, "written") and hasattr(result, "conflicts"), (
            "write_edits() returned an object exposing no `written` and "
            "`conflicts`, so the caller applying a plan cannot tell whether it "
            "was written or refused. Return an ApplyResult, or something "
            "answering both"
        )
        assert not result.written, (
            "write_edits() reported paths in `written` while refusing an "
            "unconfirmed conflict. The apply is what did not happen, so the "
            "caller marks the plan applied and no one is asked about the "
            "conflict"
        )
        assert result.conflicts, (
            "write_edits() refused the write but reported no `conflicts`, so the "
            "divergence it refused over never reaches a human and the apply "
            "looks like a clean no-op"
        )


class PlacingProjectContract:
    """Beside tier 3: where an edit lands, and the surface it must land in.

    Mix it in beside :class:`EditableProjectContract`. Placement is what carries a
    reconcile proposal to your write path, so a format that implements tier 3 and
    not this one gets advisory proposals and no stored plan, which is the same
    outcome as declining the tier.

    **What this asserts is that your three answers agree.** Each is checkable
    alone and none is interesting alone: ``load`` reads a keyspace, ``edit_path``
    names a key in it, and ``editing_surface`` says which region those keys may
    fall in. A format placing an edit outside its own declared surface builds a
    proposal the plan store then refuses, and the refusal reads as dex declining
    rather than as the format contradicting itself, which is a bad afternoon to
    debug from the outside.

    **Placement presupposes tier 3, and this checks that first**, the way
    :class:`SemanticProjectContract` checks tier 2. Every assertion below needs
    either the write path or the staged conflict that
    :class:`EditableProjectContract` stages, so a format mixing this in alone
    would meet an error about a missing attribute where the thing to say is that
    the contract is missing its other half.
    """

    def _staged_conflict(self) -> tuple[Any, Any, Any, Any]:
        """The tier-3 hook, with a message when this mixin is used on its own."""

        hook = getattr(self, "an_edit_against_a_changed_target", None)
        assert hook is not None, (
            "the assertions here write through your format, so they need the "
            "tier-3 hook an_edit_against_a_changed_target(). Mix "
            "EditableProjectContract in beside this contract, which is how "
            "placement is meant to be run: a format that places and cannot "
            "receive an edit has nowhere for the placement to lead"
        )
        return hook()

    def test_placement_presupposes_the_editable_tier(self) -> None:
        assert tier_of(self.make_project()) >= 3, (
            "a format that places an edit has to reach tier 3, because placement "
            "is where a proposal is carried to `write_edits`. Implement the write "
            "tier, or drop this mixin: placing an edit a format cannot receive "
            "describes a path that stops halfway"
        )

    def test_the_view_pins_what_an_edit_is_written_against(self) -> None:
        """``load()`` answers with the keyspace the other two members describe.

        This is what ``transform plan`` pins each edit against and what
        ``reconcile`` reads before it extends a declaration. Three things are
        needed and each fails silently in its own way when it is absent:
        ``root``, which the plan records as the directory it was pinned against;
        ``content``, which the diff a human reviews is built from; and
        ``sha256``, without which every existing file pins as a create, so a
        one-line change is rendered as a whole-file overwrite and the apply that
        follows conflicts on a file nobody touched.

        The hash need only be consistent with what your own writer re-checks. It
        is your keyspace, and dex compares your value against your value.
        """

        view = self.make_project().load()

        root = getattr(view, "root", None)
        assert isinstance(root, str) and root, (
            "load() returned a view with no `root`. A plan records the directory "
            "its edits were pinned against, and reads it from here"
        )
        files = getattr(view, "files", None)
        assert isinstance(files, Mapping), (
            "load() returned a view whose `files` is not a mapping. It has to be "
            "keyed the way edit_path() keys, because that is how a placed edit is "
            f"looked up: got {type(files).__name__}"
        )

        project, _project_dir, edits, _read_target = self._staged_conflict()
        staged = list(edits)
        assert staged, "an_edit_against_a_changed_target() returned no edits"
        target = staged[0].path
        entry = project.load().files.get(target)
        assert entry is not None, (
            f"load().files has no entry for '{target}', which is the path your own "
            "hook staged an edit against. dex looks a placed path up in this "
            "mapping: absent, it pins the edit as a create, renders a whole-file "
            "diff, and conflicts at apply on a file nobody edited"
        )
        assert isinstance(getattr(entry, "content", None), str), (
            f"the entry for '{target}' carries no `content` string, so no diff "
            "can be built for a human to review"
        )
        sha = getattr(entry, "sha256", None)
        assert isinstance(sha, str) and sha, (
            f"the entry for '{target}' carries no `sha256`, so the edit pinned "
            "against it pins nothing. A create is what a pin of None means, and "
            "an existing file that pins as a create is one an apply overwrites "
            "whole"
        )

    def test_write_edits_refuses_a_path_outside_the_declared_surface(self) -> None:
        """Your writer honors the surface you declared, not just your placements.

        Containment is checked at plan time and re-checked before dex hands a
        stored plan to your writer, but ``write_edits`` is a public method of
        your format and is reachable directly, so the surface has to hold there
        too. What is asserted is the case a prefix comparison gets wrong: a
        sibling that merely starts with the same characters. ``declarations``
        admits ``declarations/orders.yml`` and does not admit
        ``declarations_backup/orders.yml``, and a format matching by string
        prefix accepts both while passing every other assertion here.

        Refusing by raising and refusing by writing nothing are both refusals.
        """

        project, project_dir, edits, read_target = self._staged_conflict()
        surface = [str(prefix) for prefix in project.editing_surface()]
        if not surface:
            pytest.skip(
                "this format declares no editing surface, so it already refuses "
                "every edit and there is no sibling prefix to probe"
            )
        staged = list(edits)
        assert staged, "an_edit_against_a_changed_target() returned no edits"
        outside = f"{surface[0]}_dex_conformance_outside/probe.yml"
        before = read_target()
        escaping = _edit_like(
            staged[0],
            path=outside,
            old_content_hash=None,
            op=EditOp.UPSERT,
            new_content=_probe_content(outside),
        )

        try:
            result = project.write_edits([escaping], project_dir)
        except Exception:
            return

        assert not getattr(result, "written", []), (
            f"write_edits() wrote '{outside}', which editing_surface() does not "
            f"admit ({', '.join(surface)}). Prefixes match by path segment, so a "
            "sibling that shares the first characters is outside the surface. A "
            "format matching by string prefix admits the whole neighborhood of "
            "every region it owns"
        )
        assert read_target() == before, (
            f"write_edits() left '{outside}' unwritten but moved another target "
            "while doing it"
        )

    def placeable_model(self) -> str:
        """A warehouse table your format would place an edit for.

        The table name as the warehouse spells it, not as your format names the
        model derived from it. Reconcile passes what the finding is about, and the
        distinction is the one ``edit_path`` is most often gotten wrong on.
        """

        raise NotImplementedError(
            "a PlacingProjectContract subclass must implement placeable_model() "
            "-> str, naming a warehouse table this format would place an edit "
            "for. Reconcile's findings arrive keyed by table, and placement is "
            "asked per table"
        )

    def test_satisfies_the_placing_protocol(self) -> None:
        from .project import PlacingProject

        project = self.make_project()
        from .project import placement_gap

        assert isinstance(project, PlacingProject), placement_gap(project) or (
            "the format does not satisfy PlacingProject, so reconcile has no path "
            "to plan an edit against and every proposal it makes stays advisory. "
            "All three of `load`, `edit_path` and `editing_surface` are required"
        )

    def test_it_places_at_least_one_kind(self) -> None:
        """A format that declines every kind has implemented nothing.

        ``None`` per kind is a complete answer and the protocol exists to allow
        it, but ``None`` for all of them is the format saying it has nowhere for
        any edit to go, which is what declining tier 3 already says more directly.
        """

        from ..transform.plans import EditKind

        project = self.make_project()
        model = self.placeable_model()
        placed = {
            kind: project.edit_path(kind, model)
            for kind in EditKind
            if project.edit_path(kind, model) is not None
        }

        assert placed, (
            f"edit_path() answered None for every kind on '{model}', so no "
            "proposal can ever reach this format's write path. A format with "
            "nowhere for any edit to land should decline tier 3 instead"
        )

    def test_every_placement_lands_inside_the_declared_surface(self) -> None:
        """The two methods have to describe the same project.

        Containment is checked against ``editing_surface`` at plan time, whatever
        ``edit_path`` returned, so a placement outside it is an edit built and
        then refused.
        """

        from ..transform.plans import EditKind, PlanError, contained_key

        project = self.make_project()
        model = self.placeable_model()
        surface = list(project.editing_surface())

        for kind in EditKind:
            path = project.edit_path(kind, model)
            if path is None:
                continue
            try:
                contained_key(path, surface)
            except PlanError as exc:
                raise AssertionError(
                    f"edit_path({kind.value}, {model!r}) placed the edit at "
                    f"'{path}', which editing_surface() does not admit "
                    f"({', '.join(surface) or 'nothing'}). The plan store checks "
                    f"the path against the surface, so this proposal would be "
                    f"built and then refused: {exc}"
                ) from exc

    def test_the_declared_surface_cannot_reach_outside_the_project(self) -> None:
        """A surface is a region of the project, not a way out of it.

        Escapes are refused ahead of the surface no matter what is declared, so
        this cannot widen anything; declaring one means the format believes it
        owns something it does not, and the belief is worth catching here rather
        than as a refusal on the first edit that uses it.
        """

        from pathlib import PurePosixPath

        for prefix in self.make_project().editing_surface():
            candidate = PurePosixPath(str(prefix).replace("\\", "/"))
            assert not candidate.is_absolute() and ".." not in candidate.parts, (
                f"editing_surface() declares '{prefix}', which is absolute or "
                "climbs out of the project. dex refuses those regardless, so "
                "every edit placed under this prefix is refused"
            )


class ProjectFactoryContract:
    """The construction half, for a format dex builds rather than receives.

    Mix it in front of the contract for your tier, and the whole behavioral suite
    runs against projects built the way dex builds them::

        class TestMyProject(ProjectFactoryContract, MaintainProjectContract):
            tier = MaintainProject

            def build(self, context):
                return my_project_factory(context)

            def empty_context(self):
                return ProjectContext(options={"graph": "empty"})

    Two hooks rather than one factory attribute, because a plain function assigned
    to a class attribute binds as a method and would arrive with ``self`` in front
    of the context, which is a confusing failure to meet on your first run.

    ``empty_context`` is where your format says what it is keyed by. Build the
    context the way a real configuration entry would produce it: a
    directory-keyed format sets ``repo_root`` and ``project_dir``, a format
    reduced from something already running sets neither and reads ``options``.
    Return the context that reaches the project :meth:`make_project` returns, so
    the behavioral assertions and the construction assertions describe the same
    project.
    """

    #: The tier :meth:`build` promises to return. Widen it to ``MaintainProject``
    #: or ``EditableProject`` alongside the matching contract class; the default is
    #: the floor every format meets.
    tier: Any = ExploreProject

    def build(self, context: ProjectContext) -> Any:
        """Your factory, called with a context. One line in most formats."""

        raise NotImplementedError(
            "a factory conformance subclass must implement "
            "build(context) -> project, calling the factory under test"
        )

    def empty_context(self) -> ProjectContext:
        """The context that builds a project with nothing declared in it."""

        raise NotImplementedError(
            "a factory conformance subclass must implement "
            "empty_context() -> ProjectContext, keying the project the way a "
            "real configuration entry would"
        )

    def make_project(self) -> ExploreProject:
        return self.build(self.empty_context())

    def test_the_factory_builds_a_project_of_the_declared_tier(self) -> None:
        built = self.build(self.empty_context())
        assert isinstance(built, self.tier), (
            f"the factory returned {type(built).__name__}, which does not satisfy "
            f"{self.tier.__name__}. A format that passes the behavioral suite and "
            "fails here is unusable from configuration: dex builds it, checks the "
            "tier the command needs, and refuses. Check that build() returns the "
            "project rather than a class, a coroutine, or a wrapper"
        )

    def test_an_option_the_format_cannot_honor_is_refused(self) -> None:
        """Accepted-and-ignored is worse than refused, and this is where it starts.

        ``options`` reaches your factory verbatim and dex never validates a key,
        so a factory that ignores what it does not recognize turns a typo into a
        project silently read from somewhere other than where the configuration
        said. The failure surfaces as drift nobody can reproduce, arbitrarily far
        from the config line that caused it.

        The key below is deliberately one no format would define. If yours has a
        reason to accept unknown keys, override this with the assertion that is
        true for your format rather than deleting it.
        """

        context = self.empty_context()
        polluted = ProjectContext(
            repo_root=context.repo_root,
            project_dir=context.project_dir,
            options={**context.options, "dex_conformance_unknown_option": "x"},
        )

        with pytest.raises(Exception) as refusal:
            self.build(polluted)

        assert "dex_conformance_unknown_option" in str(refusal.value), (
            "the factory refused, but the message does not name the option it "
            "refused. A reader has to be able to find the line to delete"
        )
