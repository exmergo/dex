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

from typing import TYPE_CHECKING

import pytest

from ..dbt_project import ProjectDefinitions
from ..maintain.snapshot import Snapshot
from .project import ExploreProject, ProjectContext, tier_of

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from ..maintain.snapshot import SemanticLayer, TransformLayer

__all__ = [
    "DeclaringProjectContract",
    "EditableProjectContract",
    "ExploreProjectContract",
    "MaintainProjectContract",
    "ProjectFactoryContract",
    "SemanticProjectContract",
]


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
    """

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

    **The two write assertions have to be read as a pair.** Refusing an unconfirmed
    conflict is satisfied trivially by a ``write_edits`` that never writes anything,
    so the confirmed case is what rules that out. Neither is worth much alone.

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
