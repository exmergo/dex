"""SLayer model files behind the semantic-source seam.

SLayer is a **semantic layer**, not a transformation project, and it sits beside
one the way native Ossie does: `project.format: dbt` keeps the transformation
tiers, and under `semantic.vendor: slayer` the configured SLayer model files are
the repository's one semantic layer. The engine builds this source from
`semantic.slayer` through :data:`..config.SEMANTIC_SOURCE_FACTORIES`, so
MetricFlow semantic models are not read while SLayer is configured.

**This release wires configuration and packaging only.** Construction validates
the coordinates and refuses by name when the pinned `[slayer]` extra is missing
or mismatched, so every command that consults the semantic layer under
`semantic.vendor: slayer` reports a prerequisite rather than an ImportError.
:meth:`SlayerSemanticLayer.semantic_catalog` then refuses because the
translation into dex's catalog is not part of this release. The class
deliberately does not implement
:class:`~..semantic_source.SemanticSnapshotSource`, so `maintain` reports the
SLayer baseline as absent rather than fingerprinting the MetricFlow layer in its
place.

SLayer never touches the warehouse under dex, and nothing here opens SLayer
storage: the coordinates are paths to committed model files and nothing else.
Datasource files and `priority.yaml` are refused at configuration, because a
datasource carries connection credentials.

Nothing in this module imports `slayer`. The reader that will is reached only
through a source that :func:`.dependency.ensure_available` let construct, so a
base install can import and run every non-SLayer command.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import ConfigurationError, ProjectError, RepoRootRequiredError
from .dependency import ensure_available

if TYPE_CHECKING:
    from ..semantic_catalog import SemanticCatalogView
    from ..semantic_source import SemanticSourceContext

__all__ = [
    "VENDOR_NAME",
    "SlayerSemanticLayer",
    "build_semantic_layer",
]

#: The semantic vendor this source answers for, as `semantic.vendor` spells it.
VENDOR_NAME = "slayer"


class SlayerSemanticLayer:
    """The SLayer semantic layer committed to this repository.

    Holds its coordinates and reads nothing, because construction is cheap by
    contract: dex builds a source per command, and one that read files here
    would pay for every command that never looks at a semantic layer. The one
    thing construction does check is the extra, through distribution metadata
    rather than an import, because a source built without it could answer
    nothing at all.
    """

    def __init__(
        self,
        repo_root: Path | str,
        base_dir: str,
        models: list[str],
        connector: str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.base_dir = base_dir
        self.models = list(models)
        self.connector = connector

    @classmethod
    def from_context(cls, context: SemanticSourceContext) -> SlayerSemanticLayer:
        """Build from configuration, refusing coordinates it cannot honor.

        The options are checked by the same model the committed file is, so a
        host that builds this source directly cannot hand it a path the
        configuration would have refused. Every coordinate refusal names a
        config line, and no repository file is read. The extra is checked last,
        so a typo in a path is reported before an install is asked for.
        """

        from pydantic import ValidationError

        from ..config import SlayerSemanticConfig

        if context.repo_root is None:
            raise RepoRootRequiredError(
                "the SLayer semantic source needs a repo root: its models are "
                "git-reviewable files in the repository, so build the engine "
                "with DexEngine.from_repo(repo_root) or pass repo_root="
            )
        options = dict(context.options or {})
        if unknown := sorted(set(options) - set(SlayerSemanticConfig.model_fields)):
            raise ConfigurationError(
                "the SLayer semantic source takes `base_dir` and `models`, and "
                f"got: {', '.join(unknown)}. An option dex accepted and ignored "
                "would be indistinguishable from one it honored"
            )
        try:
            section = SlayerSemanticConfig(**options)
        except ValidationError as exc:
            problems = "; ".join(
                str(error.get("ctx", {}).get("error") or error["msg"])
                for error in exc.errors()
            )
            raise ConfigurationError(problems) from exc
        if not section.models or section.base_dir is None:
            raise ConfigurationError(
                "the SLayer semantic source needs semantic.slayer.base_dir and "
                "semantic.slayer.models: the SLayer model files to read, named "
                "relative to that storage root"
            )
        ensure_available()
        return cls(
            context.repo_root, section.base_dir, section.models, context.connector
        )

    def model_paths(self) -> list[Path]:
        """Every configured model file, resolved inside the repository."""

        root = self.repo_root / self.base_dir
        return [root / name for name in self.models]

    def semantic_catalog(self) -> SemanticCatalogView:
        """Refuse by name: the translation into dex's catalog is not in this
        release.

        A :class:`~..errors.ProjectError`, the type a layer that cannot be read
        *yet* raises, so every caller that already degrades around an unreadable
        layer degrades around this one.
        """

        raise ProjectError(
            "semantic.vendor: slayer is configured and SLayer "
            "is installed, but this dex release does not read SLayer models into "
            "a semantic catalog yet. Configuration and packaging land first; the "
            "reader follows in a later release"
        )


def build_semantic_layer(context: SemanticSourceContext) -> SlayerSemanticLayer:
    """The factory :data:`..config.SEMANTIC_SOURCE_FACTORIES` names for SLayer."""

    return SlayerSemanticLayer.from_context(context)
