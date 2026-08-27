"""Backend-neutral request models for semantic queries and value domains."""

from __future__ import annotations

from dataclasses import dataclass, field


def split_tokens(raw: list[str]) -> list[str]:
    """Flatten repeated and comma-joined identifier lists."""

    return [part.strip() for entry in raw for part in entry.split(",") if part.strip()]


@dataclass
class SemanticQuery:
    """The query grammar shared by semantic backends."""

    metrics: list[str]
    group_by: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)
    grain: str | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        self.metrics = split_tokens(self.metrics)
        self.group_by = split_tokens(self.group_by)
        self.order_by = split_tokens(self.order_by)


@dataclass(frozen=True)
class ValuesRequest:
    """A values request resolved against the catalog that will answer it."""

    token: str
    name: str
    grain: str | None
    metrics: list[str]
    reachable: list[str]
    grains: tuple[str, ...]
