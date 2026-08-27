"""The executable contract a semantic backend has to satisfy, shipped for reuse.

The rules a backend owes its callers are stated on the protocol members in this
package's :class:`~.SemanticBackend`. This module is the same rules as assertions,
packaged so a backend living outside this distribution can run them in its own
test suite:

    from exmergo_dex_core.explore.semantic.conformance import SemanticBackendContract

    class TestMyBackend(SemanticBackendContract):
        def make_backend(self):
            return MyBackend(...)

That is the whole integration. pytest collects the inherited ``test_*`` methods
and runs the contract against your backend.

**Two classes, because a backend is a source rather than a sink.** Nothing here
can put a semantic layer *into* a vendor's deployment, so the assertions that
check what a catalog says have to be handed a backend already answering a layer
whose contents are known. :class:`SemanticBackendContract` asserts what holds of
any catalog from any backend and needs only ``make_backend``.
:class:`SemanticCatalogContract` asserts content, and takes a backend answering
:data:`REFERENCE_LAYER`.

**The reference layer is data in this module, not a live deployment.** Two
backends reporting different things about one layer is the failure this contract
exists to catch, and a suite pinned to a hosted environment re-documents itself
whenever someone edits the project behind it. :data:`REFERENCE_LAYER` is a
neutral description of a small layer that exercises every field the catalog can
carry, and :func:`reference_dbt_manifest` renders it in dbt's own compiled shape
for the two backends dex ships. A third format seeds its own deployment from the
neutral description.

**The assertion worth the most is the one about silence.** For every field the
reference layer declares, a backend either answers it on some element or names it
in ``catalog_gaps``. Undeclared silence fails. That is what turns a divergence
between two backends into either a stated asymmetry a caller can branch on or a
bug, rather than a difference nobody notices: two shipped backends already
reported 45 and 65 dimensions for one identical layer with nothing in either
payload saying why.

This module imports pytest, so it is deliberately not imported by
``exmergo_dex_core.explore.semantic``: a bare import of the package must not
require a test framework. Install the ``[semantic-conformance]`` extra to get what
running the suite needs, which is pytest and nothing else. Like the read path
itself, nothing here reaches the dialect engine or opens a warehouse.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ...semantic_catalog import (
    DIMENSIONS_PER_DECLARATION,
    DIMENSIONS_PER_QUERYABLE_PATH,
    derive_entity_type,
)
from . import (
    EXECUTION_DEX,
    EXECUTION_VENDOR,
    SemanticQueryRefusedError,
)

__all__ = [
    "REFERENCE_LAYER",
    "SemanticBackendContract",
    "SemanticCatalogContract",
    "declared_fields",
    "reference_dbt_manifest",
]


# A small semantic layer, described in the catalog's own neutral vocabulary
# rather than any vendor's file format, shaped so that every field a
# `SemanticCatalog` can carry is declared by something in it.
#
# What it deliberately contains, because each one is a case a backend gets wrong
# on its own terms:
#
# - two semantic models on two relations, joined by a shared entity whose key is
#   spelled differently on each side, which is the join no name-based inference
#   finds and the one a flat entity record used to misreport;
# - an entity that is primary in one model and foreign in another, beside one
#   that is primary in a single model, so a derived `type` has something to
#   derive from;
# - a time dimension and a categorical dimension, so an empty grain list can be
#   told apart from an unanswered one;
# - three measure shapes, because what a measure's expression compiles to decides
#   whether a physical column can be named at all: a plain sum over a bare column
#   (nameable however the expression is read), a count over a bare column
#   (nameable from the authored form, and not from the form dbt compiles it to),
#   and a conditional sum (nameable from neither, and absent rather than guessed);
# - a simple metric, a filtered metric, and a ratio whose two sides are real
#   measures, so composition is not a shape nobody fills;
# - a label and a description on everything, so a backend that drops the
#   project's own words has to say it drops them;
# - one dimension the name heuristic alone flags as personal data, so the gate
#   that must refuse a values request for it can be asserted at this seam rather
#   than once per backend. Its name is what flags it, which is deliberate: a
#   backend that reads no metadata at all still has to refuse it.
#
# `groupable` on each metric is the join-resolved set of tokens a query may group
# it by, stated rather than derived. It is part of what the layer *is*, it is the
# answer two shipped backends once disagreed about by 44% on one identical layer,
# and stating it means a backend author can check their read without running
# anybody's join resolver. Verified against MetricFlow's own resolver on the dbt
# rendering of this layer.
#
# Every element is reachable from some metric. That is a property of the layer
# rather than a nicety: a hosted catalog is read metric by metric, so an element
# no metric draws on is legitimately absent from it, and a fixture with orphans
# would fail a backend for the layer's shape instead of its own behavior.
REFERENCE_LAYER: dict[str, Any] = {
    "semantic_models": [
        {
            "name": "users_sm",
            "label": "Users",
            "description": "One row per registered user.",
            "model_ref": "stg_users",
            "relation": "wh.main.stg_users",
            "agg_time_dimension": "signed_up_at",
            "entities": [
                {
                    "name": "user",
                    "type": "primary",
                    "expr": "user_id",
                    "label": "User",
                    "description": "The registered user, keyed on user_id here.",
                }
            ],
            "dimensions": [
                {
                    "name": "signed_up_at",
                    "type": "time",
                    "grain": "day",
                    "label": "Signed up at",
                    "description": "When the user registered.",
                },
                {
                    "name": "pricing_tier",
                    "type": "categorical",
                    "label": "Pricing tier",
                    "description": "The plan the user is on.",
                },
                {
                    "name": "email",
                    "type": "categorical",
                    "label": "Email",
                    "description": "The user's email address. Personal data.",
                },
            ],
            "measures": [
                {
                    "name": "user_count",
                    "agg": "count",
                    "expr": "user_id",
                    "label": "Users",
                    "description": "Distinct registered users.",
                },
                {
                    "name": "paying_user_count",
                    "agg": "sum",
                    "expr": "case when is_paying then 1 else 0 end",
                    "label": "Paying users",
                    "description": "Users on a paid plan, as a conditional sum.",
                },
            ],
        },
        {
            "name": "sessions_sm",
            "label": "Sessions",
            "description": "One row per session.",
            "model_ref": "stg_sessions",
            "relation": "wh.main.stg_sessions",
            "agg_time_dimension": "started_at",
            "entities": [
                {
                    "name": "session",
                    "type": "primary",
                    "expr": "session_key",
                    "label": "Session",
                    "description": "The session, keyed on session_key.",
                },
                {
                    "name": "user",
                    "type": "foreign",
                    "expr": "visitor_id",
                    "label": "User",
                    "description": (
                        "The user who opened it. Null for anonymous sessions."
                    ),
                },
            ],
            "dimensions": [
                {
                    "name": "started_at",
                    "type": "time",
                    "grain": "day",
                    "label": "Started at",
                    "description": "When the session began.",
                },
                {
                    "name": "channel",
                    "type": "categorical",
                    "label": "Channel",
                    "description": "How the session arrived.",
                },
            ],
            "measures": [
                {
                    "name": "session_count",
                    "agg": "count",
                    "expr": "session_key",
                    "label": "Sessions",
                    "description": "Sessions opened.",
                },
                {
                    # A plain sum over a bare column, which is the one measure
                    # shape whose *compiled* expression is still a column
                    # reference. Without it every measure in this layer would
                    # resolve to no column on a backend that reads the compiled
                    # form, and "this backend can never name a measure's column"
                    # would be indistinguishable from "this layer has no measure
                    # whose column can be named".
                    "name": "session_seconds",
                    "agg": "sum",
                    "expr": "duration_seconds",
                    "label": "Session seconds",
                    "description": "Time spent in sessions.",
                },
            ],
        },
    ],
    "metrics": [
        {
            "name": "users",
            "type": "simple",
            "label": "Users",
            "description": "Registered users, groupable by user__pricing_tier.",
            "measure": "user_count",
            "groupable": [
                "metric_time",
                "user__email",
                "user__pricing_tier",
                "user__signed_up_at",
            ],
        },
        {
            "name": "paying_users",
            "type": "simple",
            "label": "Paying users",
            "description": "Users on a paid plan.",
            "measure": "paying_user_count",
            "filter": "{{ Dimension('user__pricing_tier') }} != 'free'",
            "groupable": [
                "metric_time",
                "user__email",
                "user__pricing_tier",
                "user__signed_up_at",
            ],
        },
        {
            # A ratio's two sides are other metrics rather than measures, which is
            # how MetricFlow and the dbt Cloud API both express one. The contract
            # accepts either, because a format that composes a ratio out of
            # measures directly is a reasonable format; what it does not accept is
            # a side that resolves to nothing in the payload.
            "name": "paying_user_share",
            "type": "ratio",
            "label": "Paying user share",
            "description": "Paying users over all users.",
            "numerator": "paying_users",
            "denominator": "users",
            "input_measures": ["paying_user_count", "user_count"],
            "groupable": [
                "metric_time",
                "user__email",
                "user__pricing_tier",
                "user__signed_up_at",
            ],
        },
        {
            "name": "sessions",
            "type": "simple",
            "label": "Sessions",
            "description": "Sessions opened, groupable by session__channel.",
            "measure": "session_count",
            # The join at work: a session metric reaches the user model's
            # dimensions through the shared `user` entity, which is exactly the set
            # a single-hop read of the declarations cannot name.
            "groupable": [
                "metric_time",
                "session__channel",
                "session__started_at",
                "user__email",
                "user__pricing_tier",
                "user__signed_up_at",
            ],
        },
        {
            "name": "session_seconds",
            "type": "simple",
            "label": "Session seconds",
            "description": "Time spent in sessions.",
            "measure": "session_seconds",
            "groupable": [
                "metric_time",
                "session__channel",
                "session__started_at",
                "user__email",
                "user__pricing_tier",
                "user__signed_up_at",
            ],
        },
    ],
}


# Which catalog fields the reference layer declares, per element kind. The
# contract reads this rather than a hand-kept list, so widening the layer above
# widens what a backend has to answer or declare, and the two cannot drift apart.
#
# `column` is here for every element that references one, and absent for
# `paying_user_count` alone, whose expression resolves to no single column: that
# is an element-level absence rather than a backend's silence, which is exactly
# the distinction the silence assertion has to survive.
_DECLARED_FIELDS: dict[str, tuple[str, ...]] = {
    "semantic_models": (
        "name",
        "label",
        "description",
        "model_ref",
        "agg_time_dimension",
        "primary_entity",
        "relation",
    ),
    "metrics": (
        "name",
        "type",
        "label",
        "description",
        "dimensions",
        "semantic_models",
        "input_measures",
        "composition",
        "filter",
        "time_axis",
        "queryable_granularities",
    ),
    "dimensions": (
        "name",
        "type",
        "label",
        "description",
        "definition",
        "semantic_model",
        "queryable_granularities",
        "column",
    ),
    "entities": ("name", "type", "label", "description", "roles"),
    "measures": (
        "name",
        "agg",
        "expr",
        "agg_time_dimension",
        "label",
        "description",
        "semantic_model",
        "column",
    ),
}


def declared_fields(kind: str) -> tuple[str, ...]:
    """The catalog fields :data:`REFERENCE_LAYER` declares for one element kind.

    Exposed so a backend author can see what the silence assertion will ask of
    them before running it, and so a format that seeds the reference layer into
    its own deployment can check it left nothing out.
    """

    return _DECLARED_FIELDS[kind]


def reference_dbt_manifest() -> dict[str, Any]:
    """:data:`REFERENCE_LAYER` as a compiled dbt ``semantic_manifest.json``.

    Provided because both backends dex ships read dbt: the local one reads this
    artifact through the project seam, and the hosted one is exercised against a
    transport answering the same layer. A non-dbt format ignores this and seeds
    its own deployment from the neutral description instead.
    """

    models = [
        {
            "name": model["name"],
            "label": model["label"],
            "description": model["description"],
            "node_relation": {
                "alias": model["model_ref"],
                "relation_name": model["relation"],
                # dex reads only the alias and the relation name. `database` and
                # `schema_name` are here because MetricFlow's own validator
                # requires them, and a dbt-shaped backend that asks MetricFlow to
                # resolve the join graph would otherwise get a refusal on this
                # fixture instead of a resolution.
                "database": model["relation"].split(".")[0],
                "schema_name": model["relation"].split(".")[1],
            },
            "defaults": {"agg_time_dimension": model["agg_time_dimension"]},
            "entities": [
                {
                    "name": entity["name"],
                    "type": entity["type"],
                    "expr": entity["expr"],
                    "label": entity["label"],
                    "description": entity["description"],
                }
                for entity in model["entities"]
            ],
            "dimensions": [
                {
                    "name": dimension["name"],
                    "type": dimension["type"],
                    "label": dimension["label"],
                    "description": dimension["description"],
                    **(
                        {"type_params": {"time_granularity": dimension["grain"]}}
                        if dimension.get("grain")
                        else {}
                    ),
                }
                for dimension in model["dimensions"]
            ],
            "measures": [
                {
                    "name": measure["name"],
                    "agg": measure["agg"],
                    "expr": measure["expr"],
                    "label": measure["label"],
                    "description": measure["description"],
                    "agg_time_dimension": model["agg_time_dimension"],
                }
                for measure in model["measures"]
            ],
        }
        for model in REFERENCE_LAYER["semantic_models"]
    ]

    metrics = []
    for metric in REFERENCE_LAYER["metrics"]:
        params: dict[str, Any] = {}
        if metric.get("measure"):
            params["measure"] = {"name": metric["measure"]}
            params["input_measures"] = [{"name": metric["measure"]}]
        if metric.get("numerator"):
            params["numerator"] = {"name": metric["numerator"]}
            params["denominator"] = {"name": metric["denominator"]}
        if metric.get("input_measures"):
            params["input_measures"] = [
                {"name": name} for name in metric["input_measures"]
            ]
        entry: dict[str, Any] = {
            "name": metric["name"],
            "type": metric["type"],
            "label": metric["label"],
            "description": metric["description"],
            "type_params": params,
        }
        if metric.get("filter"):
            entry["filter"] = {"where_sql_template": metric["filter"]}
        metrics.append(entry)

    return {
        "semantic_models": models,
        "metrics": metrics,
        # Required by MetricFlow's validator and read by nothing in dex. A time
        # spine is what lets it resolve a metric's own time axis, so a fixture
        # without one resolves no join graph and every dbt-shaped backend asking
        # for one would degrade on this layer rather than on its own behavior.
        "project_configuration": {
            "time_spine_table_configurations": [
                {
                    "location": "wh.main.dim_dates",
                    "column_name": "date_day",
                    "grain": "day",
                }
            ],
        },
    }


def write_reference_project(root) -> Any:
    """Write the reference layer's compiled manifest under ``root`` and return the
    project directory, which is what the local backend reads.

    Here rather than in dex's own tests because a dbt-shaped backend outside this
    distribution needs the same three lines, and because the path the artifact
    lives at is part of what "the reference layer" means for a dbt read.
    """

    project = root / "reference_layer"
    (project / "target").mkdir(parents=True, exist_ok=True)
    (project / "target" / "semantic_manifest.json").write_text(
        json.dumps(reference_dbt_manifest())
    )
    return project


class SemanticBackendContract:
    """What holds of any catalog from any semantic backend. Subclass and implement
    :meth:`make_backend`.

    Every assertion here is layer-independent: it asks what a caller may assume of
    a catalog whatever the layer behind it declares, never what a particular layer
    contains. Mix :class:`SemanticCatalogContract` in beside it for the assertions
    about content.
    """

    def make_backend(self) -> Any:
        """A backend of yours, answering whatever layer you can reach.

        The layer's contents do not matter here, so this is the cheap hook: point
        it at a fixture, a fake transport, or an empty deployment. What matters is
        that ``list_definitions()`` answers.
        """

        raise NotImplementedError

    # --- provenance: which layer answered, and who ran it ---------------------

    def test_declares_its_provenance_and_who_executes(self) -> None:
        """The four axes, and ``execution`` in particular.

        ``execution`` is the load-bearing one: it says whether dex ran the
        statement and therefore whether the cost guard could apply at all. A
        backend that leaves it unset inherits neither posture, so a vendor-executed
        query would be reported as guarded when nothing guarded it.
        """

        backend = self.make_backend()

        assert backend.name
        assert backend.vendor
        assert backend.deployment
        assert backend.execution in {EXECUTION_DEX, EXECUTION_VENDOR}

    def test_the_provenance_reaches_the_payload(self) -> None:
        payload = self.make_backend().list_definitions().to_data()

        for axis in ("backend", "vendor", "deployment", "execution"):
            assert payload[axis], f"{axis} is empty in the payload"

    def test_a_catalog_names_its_own_provenance_not_the_instance(self) -> None:
        """``name`` identifies the backend, so two instances agree on it.

        The mistake is easy to make by deriving it from a host or an environment
        id, and a name that varies per instance is one a registry cannot resolve
        and a reader cannot compare two payloads by.
        """

        first, second = self.make_backend(), self.make_backend()

        assert first.name == second.name
        assert first.execution == second.execution

    def test_reading_the_catalog_twice_agrees(self) -> None:
        """Two reads of an unchanged layer agree.

        Not a caching requirement: a backend may make the round trip again. What
        it rules out is a read that consumes something, which surfaces as a second
        command in the same session mysteriously seeing less than the first.
        """

        backend = self.make_backend()

        assert backend.list_definitions().to_data() == (
            backend.list_definitions().to_data()
        )

    # --- the shape of the answer ---------------------------------------------

    def test_the_dimension_row_scope_is_declared(self) -> None:
        catalog = self.make_backend().list_definitions()

        assert catalog.dimension_scope in {
            DIMENSIONS_PER_DECLARATION,
            DIMENSIONS_PER_QUERYABLE_PATH,
        }

    def test_a_queryable_path_scope_means_every_groupable_token_has_a_row(
        self,
    ) -> None:
        """``queryable_paths`` is a promise, not a label.

        It says a dimension row *is* a token a query may group by, so a token in a
        metric's list with no row of its own contradicts the declaration: the
        caller pastes the token into ``--group-by`` and can find nothing in the
        payload that describes what it means. Under ``declarations`` that gap is
        expected and declared, which is the whole reason the two scopes are named.
        """

        catalog = self.make_backend().list_definitions()
        if catalog.dimension_scope != DIMENSIONS_PER_QUERYABLE_PATH:
            pytest.skip(
                "this backend declares dimension rows per declaration, so a "
                "groupable token legitimately has no row of its own"
            )

        rows = {dimension.name for dimension in catalog.dimensions}
        missing = sorted(
            {
                token
                for metric in catalog.metrics
                for token in metric.dimensions
                if token not in rows
            }
        )

        assert not missing, (
            f"dimension_scope says every groupable token has a row, and these have "
            f"none: {', '.join(missing)}"
        )

    def test_every_element_points_at_something_the_payload_describes(self) -> None:
        """A reference a caller cannot resolve inside the same payload.

        This is the property that makes the catalog readable as a graph rather
        than five lists: a metric's measures, an element's owning model, and an
        entity's declarations all name things the reader is meant to look up. A
        dangling name reads as a typo in the project rather than as a gap in the
        read.
        """

        catalog = self.make_backend().list_definitions()
        models = {model.name for model in catalog.semantic_models}
        measures = {measure.name for measure in catalog.measures}

        for metric in catalog.metrics:
            for measure in metric.input_measures or ():
                assert measure in measures, (
                    f"metric {metric.name} reads measure {measure}, which the "
                    "payload does not describe"
                )
            for model in metric.semantic_models or ():
                assert model in models, (
                    f"metric {metric.name} names semantic model {model}, which the "
                    "payload does not describe"
                )
        for element in (*catalog.dimensions, *catalog.measures):
            if element.semantic_model:
                assert element.semantic_model in models
        for entity in catalog.entities:
            for role in entity.roles:
                if role.semantic_model:
                    assert role.semantic_model in models

    def test_an_entity_type_is_derived_from_its_declarations(self) -> None:
        """The single ``type`` is a summary of the declarations, never a copy of
        one of them.

        An entity's type is a property of the (entity, semantic model) pair: it is
        primary in the model that keys it and foreign in every model that joins to
        it. Two shipped backends once folded those copies into one record and
        reported whichever the iteration reached first, disagreeing with each other
        on the same layer and both misreporting the entity most joined in it. An
        agent reading that to work out the join graph is reading noise.
        """

        for entity in self.make_backend().list_definitions().entities:
            assert entity.type == derive_entity_type(entity.roles), (
                f"entity {entity.name} reports type {entity.type!r}, which is not "
                f"what its declarations derive to"
            )

    def test_a_ratio_metric_carries_both_sides(self) -> None:
        """Composition is what makes a ratio readable.

        Without it a ratio is a name and a type, and a caller cannot tell whether
        it is additive, cannot tell two ratios share a denominator, and cannot tell
        the two sides sit in different semantic models, which is what decides
        whether a group-by is valid on both.
        """

        catalog = self.make_backend().list_definitions()
        ratios = [metric for metric in catalog.metrics if metric.type == "ratio"]
        if not ratios:
            pytest.skip("this layer declares no ratio metric")
        # Either vocabulary resolves: MetricFlow's ratio sides are metrics, and a
        # format that divides two measures directly is equally legible. What has to
        # hold is that both sides name something the same payload describes.
        known = {measure.name for measure in catalog.measures}
        known.update(metric.name for metric in catalog.metrics)

        for metric in ratios:
            composition = metric.composition
            assert composition is not None
            assert composition.numerator and composition.denominator, (
                f"ratio metric {metric.name} carries no numerator or denominator"
            )
            assert composition.numerator in known
            assert composition.denominator in known

    def test_a_time_axis_names_a_measure_the_metric_reads(self) -> None:
        """``time_axis`` is what a time grouping actually aggregates by, so it has
        to be one of the metric's own measures' time columns.

        A layer's time token is one name over many physical columns, and this is
        the field that says which. An axis that belongs to no measure the metric
        reads is worse than an absent one, because a caller trusts it and buckets
        a number by a column that is not in the number.
        """

        catalog = self.make_backend().list_definitions()
        axes = {
            measure.name: measure.agg_time_dimension for measure in catalog.measures
        }

        for metric in catalog.metrics:
            if not metric.time_axis or not metric.input_measures:
                continue
            declared = {
                axes[measure] for measure in metric.input_measures if axes.get(measure)
            }
            if not declared:
                continue
            assert set(metric.time_axis) <= declared, (
                f"metric {metric.name} reports a time axis its measures do not "
                f"declare: {sorted(set(metric.time_axis) - declared)}"
            )

    # --- the payload -----------------------------------------------------------

    def test_the_payload_serializes_and_states_what_it_is(self) -> None:
        payload = self.make_backend().list_definitions().to_data()

        json.dumps(payload)
        assert payload["dimension_scope"]
        assert set(payload["elided"]) >= {
            "semantic_models",
            "metrics",
            "dimensions",
            "entities",
            "measures",
        }
        for kind in _DECLARED_FIELDS:
            assert isinstance(payload[kind], list)

    def test_the_pii_gate_lookup_never_reaches_the_payload(self) -> None:
        """``physical_columns`` resolves a token to a profiled column for the PII
        gate and is not part of what a caller reads.

        Asserted rather than assumed because it is a mapping of every dimension
        and entity token to a relation and column, so a backend that serialized it
        by adding a field would roughly double the payload and put the layer's
        physical addressing somewhere nothing documents.
        """

        payload = self.make_backend().list_definitions().to_data()

        assert "physical_columns" not in payload

    def test_a_capped_catalog_counts_what_it_cut_and_full_cuts_nothing(self) -> None:
        """Every cut counted, and liftable.

        A consumer that silently loses catalog entries is worse than a large
        payload, because the remainder reads as the layer. So a cap has to be
        visible in the payload and in a note, and there has to be a way to ask for
        everything.
        """

        catalog = self.make_backend().list_definitions()
        if not catalog.metrics:
            pytest.skip("this layer declares no metric, so there is nothing to cut")

        capped = catalog.capped(max_metrics=1)

        assert len(capped.metrics) == 1
        assert capped.elided["metrics"] == len(catalog.metrics) - 1
        assert len(capped.notes) > len(catalog.notes)
        assert capped.to_data()["elided"]["metrics"] == capped.elided["metrics"]

        whole = catalog.capped(full=True, max_metrics=1)

        assert len(whole.metrics) == len(catalog.metrics)
        assert set(whole.elided.values()) == {0}

    def test_capping_leaves_the_catalog_it_was_given_alone(self) -> None:
        """A cap returns a narrower catalog rather than editing the one in hand.

        A backend or a host may read the catalog once and answer several questions
        from it, and a cap that mutated the shared object would shrink every later
        answer by however much the first one was trimmed.
        """

        catalog = self.make_backend().list_definitions()
        if not catalog.metrics:
            pytest.skip("this layer declares no metric, so there is nothing to cut")
        before = catalog.to_data()

        catalog.capped(max_metrics=1, max_dimensions_per_metric=0)

        assert catalog.to_data() == before

    # --- the filter dialect ----------------------------------------------------

    def test_reading_a_foreign_filter_dialect_answers_or_declines(self) -> None:
        """``filter_refs`` returns the tokens a clause names, or None, and never
        raises.

        None is a real answer and the safe one: it means this backend cannot read
        its own filter dialect, and the neutral gate then refuses a filtered query
        rather than screening its group-by half and passing every dimension the
        filter named. A backend that raises instead turns a screening decision
        into a stack trace, and one that returns ``[]`` on a clause it did not
        understand claims the clause named nothing, which is the fail-open this
        whole seam exists to close.
        """

        backend = self.make_backend()

        for clause in (
            "{{ Dimension('user__pricing_tier') }} = 'pro'",
            '{"member": "users.pricing_tier", "operator": "equals"}',
            "not a filter in any dialect",
        ):
            found = backend.filter_refs([clause])
            assert found is None or isinstance(found, list)


class SemanticCatalogContract:
    """What the catalog has to say about a layer whose contents are known. Mix in
    beside :class:`SemanticBackendContract`::

        class TestMyBackend(SemanticBackendContract, SemanticCatalogContract):
            def make_backend(self):
                return MyBackend(...)

            def make_reference_backend(self):
                return MyBackend(seeded_with=REFERENCE_LAYER)

    Separate from the contract above for the reason a project format's content
    assertions are separate from its behavioral ones: nothing here can put a layer
    into your deployment, so the assertions that check what a catalog *says* have
    to be handed a backend already answering :data:`REFERENCE_LAYER`. Seed it
    however your format is seeded; :func:`reference_dbt_manifest` renders the layer
    in dbt's compiled shape for a format that reads that.
    """

    def make_reference_backend(self) -> Any:
        """A backend answering :data:`REFERENCE_LAYER`."""

        raise NotImplementedError

    def test_the_reference_layer_arrives_whole(self) -> None:
        """Every semantic model, metric, entity and measure the layer declares.

        The count, not just the shape. A backend that reaches a layer metric by
        metric and drops an element no metric reads would pass every structural
        assertion above and answer a smaller layer than the one it was pointed at,
        so the reference layer is built with no orphans precisely to make this
        assertion mean the backend rather than the fixture.
        """

        catalog = self.make_reference_backend().list_definitions()
        declared = REFERENCE_LAYER["semantic_models"]

        assert {model.name for model in catalog.semantic_models} == {
            model["name"] for model in declared
        }
        assert {metric.name for metric in catalog.metrics} == {
            metric["name"] for metric in REFERENCE_LAYER["metrics"]
        }
        assert {entity.name for entity in catalog.entities} == {
            entity["name"] for model in declared for entity in model["entities"]
        }
        assert {measure.name for measure in catalog.measures} == {
            measure["name"] for model in declared for measure in model["measures"]
        }

    def test_a_join_resolved_read_reports_every_token_a_query_can_use(self) -> None:
        """The dimension list is what an agent budgeting one discovery call reads.

        The reference layer's ``user`` entity joins the two models, so a session
        metric can be grouped by the user model's dimensions. A read that resolves
        the join graph has to say so: two shipped backends once reported 6 and 11
        groupable dimensions for one metric on one layer, and the token both short
        lists were missing was the one nearly every metric description in that
        project tells a caller to group by. So the catalog contradicted the prose
        it was carrying in the same payload.

        Only asserted under ``queryable_paths``, because ``declarations`` is a
        declared narrower answer rather than a wrong one. That is what makes
        ``dimension_scope`` a promise a caller can act on instead of a label.
        """

        catalog = self.make_reference_backend().list_definitions()
        if catalog.dimension_scope != DIMENSIONS_PER_QUERYABLE_PATH:
            pytest.skip(
                "this backend declares dimension rows per declaration, so its "
                "per-metric lists are the single-hop set by design"
            )
        declared = {
            metric["name"]: set(metric["groupable"])
            for metric in REFERENCE_LAYER["metrics"]
        }

        for metric in catalog.metrics:
            assert set(metric.dimensions) == declared[metric.name], (
                f"metric {metric.name} reports "
                f"{sorted(set(metric.dimensions) ^ declared[metric.name])} "
                "differently from what the reference layer says a query can group "
                "it by"
            )

    def test_a_field_this_backend_cannot_answer_is_declared_as_a_gap(self) -> None:
        """The assertion this whole contract is for.

        The reference layer declares every field in :func:`declared_fields`. So for
        each element kind, a field with no value on any element is a field this
        backend structurally cannot supply, and it has to say so in
        ``catalog_gaps``. Undeclared silence fails.

        Why this matters more than it looks: an absent field and an undeclared
        field are indistinguishable to a caller, so a consumer reads "the hosted
        API has no entity labels" as "this project labelled no entities" and
        stops looking. Two shipped backends reported 45 and 65 dimensions for one
        identical layer with nothing in either payload saying why, and this is the
        assertion that makes the next such difference a stated asymmetry or a
        failing test rather than a surprise in the field.

        An empty list counts as an answer, not silence. "This categorical
        dimension has no queryable grain" is a fact, and a backend that says it
        should not be told it declared a gap.
        """

        catalog = self.make_reference_backend().list_definitions()
        elements = {
            "semantic_models": catalog.semantic_models,
            "metrics": catalog.metrics,
            "dimensions": catalog.dimensions,
            "entities": catalog.entities,
            "measures": catalog.measures,
        }

        undeclared: list[str] = []
        for kind, kind_elements in elements.items():
            gaps = set(catalog.unavailable.get(kind, ()))
            for field_name in declared_fields(kind):
                answered = any(
                    getattr(element, field_name, None) is not None
                    for element in kind_elements
                )
                if not answered and field_name not in gaps:
                    undeclared.append(f"{kind}.{field_name}")

        assert not undeclared, (
            "these fields are declared by the reference layer, answered on no "
            "element, and named in no catalog gap, so a caller cannot tell a "
            f"structural absence from an undeclared field: {', '.join(undeclared)}"
        )

    def test_a_declared_gap_is_a_field_the_catalog_could_have_carried(self) -> None:
        """The reverse direction: a gap names a real field of a real element kind.

        A misspelled gap is worse than none. It reads as a declaration a consumer
        can branch on and matches nothing it will ever look for, so the field it
        was meant to cover goes back to being an unexplained absence.
        """

        catalog = self.make_reference_backend().list_definitions()

        for kind, gaps in catalog.unavailable.items():
            assert kind in _DECLARED_FIELDS, (
                f"catalog_gaps names element kind {kind!r}, which is not one of "
                f"{', '.join(sorted(_DECLARED_FIELDS))}"
            )
            for field_name in gaps:
                assert field_name in declared_fields(kind), (
                    f"catalog_gaps declares {kind}.{field_name}, which the "
                    "reference layer does not declare, so nothing can check it"
                )

    def test_the_shared_entity_carries_a_declaration_per_model_with_its_own_key(
        self,
    ) -> None:
        """The join graph, which is the part a flat entity record destroyed.

        ``user`` is primary in one model and foreign in the other, and the two
        spell the join key differently. That is the join no name-based inference
        can find and the one the layer states outright, so both declarations and
        both keys have to survive the read.
        """

        catalog = self.make_reference_backend().list_definitions()
        user = next(entity for entity in catalog.entities if entity.name == "user")
        by_model = {role.semantic_model: role for role in user.roles}

        assert set(by_model) == {"users_sm", "sessions_sm"}
        assert by_model["users_sm"].type == "primary"
        assert by_model["sessions_sm"].type == "foreign"
        assert by_model["users_sm"].column == "user_id"
        assert by_model["sessions_sm"].column == "visitor_id"
        assert user.type == "primary"

    def test_a_computed_element_carries_no_column_and_a_referencing_one_does(
        self,
    ) -> None:
        """Absent rather than guessed.

        The PII gate resolves a dimension to a physical column and reads that
        column's evidence, so a column guessed out of an expression makes the gate
        authoritative about the wrong thing.

        ``session_seconds`` sums a bare column, so its column is nameable whether
        a backend reads the authored expression or the one the warehouse compiles.
        ``paying_user_count`` is a conditional sum, so it is nameable from
        neither, and the answer has to be absence rather than a guess at the first
        identifier in the expression.

        ``session_count`` is deliberately not asserted either way. A count over a
        bare column is nameable from the authored form and not from the compiled
        one, so the two shipped backends legitimately differ on it, and a contract
        that picked a side would fail a correct backend for reading its own
        vendor's artifact.
        """

        catalog = self.make_reference_backend().list_definitions()
        measures = {measure.name: measure for measure in catalog.measures}

        assert measures["paying_user_count"].column is None
        if "expr" in set(catalog.unavailable.get("measures", ())):
            pytest.skip("this backend declares it cannot read a measure's expression")
        assert measures["session_seconds"].column == "duration_seconds"

    def test_a_categorical_dimension_has_no_grain_and_says_so(self) -> None:
        """An empty grain list is an answer, and a different one from silence.

        A caller that cannot tell them apart either asks for a grain that will be
        refused or never asks for one that would work.
        """

        catalog = self.make_reference_backend().list_definitions()
        rows = {dimension.name: dimension for dimension in catalog.dimensions}
        if "queryable_granularities" in set(catalog.unavailable.get("dimensions", ())):
            pytest.skip("this backend declares it cannot report queryable grains")

        tiers = [row for name, row in rows.items() if row.definition == "pricing_tier"]
        times = [row for name, row in rows.items() if row.definition == "signed_up_at"]
        assert tiers and times

        for row in tiers:
            assert row.queryable_granularities == []
        for row in times:
            assert row.queryable_granularities

    def test_a_dimension_row_names_the_token_a_query_groups_by(self) -> None:
        """``name`` is the token, ``definition`` is the bare name.

        Repointing ``name`` at the bare name would read more tidily and break
        every caller that builds a ``--group-by`` from it. ``definition`` is what
        lets a consumer see that two qualified paths reach one declaration, which
        is what reconciles the two backends' different dimension counts.
        """

        catalog = self.make_reference_backend().list_definitions()
        rows = {dimension.name: dimension for dimension in catalog.dimensions}
        groupable = {token for metric in catalog.metrics for token in metric.dimensions}

        tier = next(row for row in rows.values() if row.definition == "pricing_tier")
        assert tier.name in groupable
        assert tier.name != tier.definition
        assert tier.name.endswith("pricing_tier")

    def test_the_values_of_a_pii_dimension_are_refused(self) -> None:
        """PII is flagged, never surfaced, and this command's whole output is
        values.

        A metric query can drop a flagged dimension from the grouping and still
        answer something; here there is no reduced answer to fall back to, so the
        command refuses. Asserted at this seam rather than once per backend because
        the policy is the neutral layer's and only the evidence differs: a
        backend that read no metadata at all still refuses on the name.
        """

        backend = self.make_reference_backend()
        read = getattr(backend, "values", None)
        if read is None:
            pytest.skip("this backend does not read a dimension's value domain")

        catalog = backend.list_definitions()
        token = next(
            (
                dimension.name
                for dimension in catalog.dimensions
                if dimension.definition == "email"
            ),
            None,
        )
        assert token, "the reference layer declares an email dimension"

        with pytest.raises(SemanticQueryRefusedError):
            read(token, [])
