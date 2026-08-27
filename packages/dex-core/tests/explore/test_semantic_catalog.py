"""Focused tests for semantic catalog response shaping."""

from exmergo_dex_core.explore.semantic import (
    EXECUTION_DEX,
    BackendDescriptor,
    MetricInfo,
    SemanticCatalog,
)


def _catalog() -> SemanticCatalog:
    return SemanticCatalog.from_backend(
        type(
            "Backend",
            (),
            {
                "descriptor": BackendDescriptor(
                    name="fixture",
                    vendor="fixture",
                    deployment="local",
                    execution=EXECUTION_DEX,
                )
            },
        )(),
        metrics=[
            MetricInfo(name="sessions", type="simple"),
            MetricInfo(
                name="queries_per_session",
                type="ratio",
                time_axis=["queried_at", "session_started_at"],
            ),
        ],
    )


def test_time_axis_warning_describes_only_metrics_left_after_scoping():
    scoped, unknown = _catalog().narrowed_to(["sessions"])

    assert unknown == []
    assert not any("queries_per_session" in note for note in scoped.capped().notes)


def test_time_axis_warning_is_added_when_the_metric_survives_scoping():
    scoped, unknown = _catalog().narrowed_to(["queries_per_session"])

    assert unknown == []
    assert any("queries_per_session" in note for note in scoped.capped().notes)
