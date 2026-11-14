"""``dex feedback``: what a bug report or a feature request needs (#1299).

Routed in ``main`` before any engine is built, like ``dex procedure``: it opens no
connection, needs no connector extra, and works with no ``.dex/config.yml``,
because the moment a person or an agent wants to report something is often the
moment the project or the connection is broken.

Dex never sends anything. The command hands back links to the repository's issue
forms, prefilled with the environment, and filing stays the person's choice: a
public issue is an outward-facing act, and an agent drafts it rather than files it.

What the environment carries is chosen so that it can be pasted into a public
issue as it stands: versions, the platform, the connector and semantic-layer
*names*. Never a target, a credential, a path, a relation or a value. The links
carry the same and nothing more; in particular the internal-error pointer
(:func:`report_pointer`) never puts the error text in a link, since a message can
quote a relation, a path or a value.
"""

from __future__ import annotations

import argparse
import contextlib
import platform
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlencode

from . import envelope as env
from .results import Result, to_envelope

#: The public repository. Issues are filed here, and nowhere else.
REPOSITORY = "https://github.com/exmergo/dex"

#: The issue form files under ``.github/ISSUE_TEMPLATE/``, and the field ids each
#: link prefills. A test holds every id here to the form it targets, so renaming
#: a field there cannot silently empty a prefilled link.
BUG_TEMPLATE = "bug_report.yml"
FEATURE_TEMPLATE = "feature_request.yml"
BUG_PREFILL_FIELDS = ("version", "env", "command")
FEATURE_PREFILL_FIELDS = ("version", "env")

#: What a report should and should not contain, stated once for the envelope.
GUIDANCE = (
    "Search the existing issues first "
    f"({REPOSITORY}/issues?q=is%3Aissue); a comment on a match beats a duplicate.",
    "A bug report needs the command, what you expected, what happened, and the "
    "smallest steps that reproduce it. `dex demo` builds a public warehouse "
    "that often reproduces it without your data.",
    "Paste the envelope only after reading it: never a credential, a "
    "connection string, a raw row, a value, or a relation or path you would not "
    "publish. data.environment is safe to paste as it stands.",
    "A security problem is never a public issue: report it privately "
    f"({REPOSITORY}/security/advisories/new, see SECURITY.md).",
    "An agent drafts the issue and asks the person before filing it, since an "
    "issue is public; with nobody present, it puts the draft in its report.",
    "A feature request is welcome too: a capability Dex lacks, a refusal that "
    "stopped a legitimate change, or a workaround you keep repeating.",
)


def _engine_version() -> str:
    try:
        return version("exmergo-dex-core")
    except PackageNotFoundError:  # pragma: no cover - a source tree run
        return "0.0.0"


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def environment(
    *,
    connector: str | None = None,
    semantic_vendor: str | None = None,
    result_mode: str | None = None,
) -> dict[str, Any]:
    """The environment a report needs, safe to publish as it stands."""

    return {
        "engine_version": _engine_version(),
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.machine()}".strip(),
        "connector": connector,
        "semantic_vendor": semantic_vendor,
        "dbt_core": _package_version("dbt-core"),
        "result_mode": result_mode or "full",
    }


def _environment_text(environment: dict[str, Any]) -> str:
    """The environment as the form's ``env`` textarea reads it, one fact a line."""

    return "\n".join(
        f"{key}: {value}" for key, value in environment.items() if value is not None
    )


def issue_link(template: str, *, title: str, fields: dict[str, str]) -> str:
    """A link to one issue form with its title and text fields prefilled."""

    query = {"template": template, "title": title}
    query.update({key: value for key, value in fields.items() if value})
    return f"{REPOSITORY}/issues/new?{urlencode(query)}"


def bug_link(environment: dict[str, Any], *, command: str | None = None) -> str:
    title = f"[bug] dex {command}: " if command else "[bug] "
    return issue_link(
        BUG_TEMPLATE,
        title=title,
        fields={
            "version": str(environment.get("engine_version") or ""),
            "env": _environment_text(environment),
            "command": f"dex {command}" if command else "",
        },
    )


def feature_link(environment: dict[str, Any]) -> str:
    return issue_link(
        FEATURE_TEMPLATE,
        title="[feature] ",
        fields={
            "version": str(environment.get("engine_version") or ""),
            "env": _environment_text(environment),
        },
    )


def report_pointer(
    *,
    command: str | None,
    connector: str | None,
    exception: str | None,
    result_mode: str | None = None,
) -> dict[str, Any]:
    """What an internal-error envelope adds under ``data.report``.

    ``reason: internal`` is an exception Dex did not anticipate, so it is most
    likely a Dex defect rather than something the caller did. The link names the
    command, the connector and the exception's class only: the error text stays
    in the envelope, where the person can read it before choosing what to paste.
    """

    environment_ = environment(connector=connector, result_mode=result_mode)
    if exception:
        environment_["exception"] = exception
    return {
        "likely_dex_defect": True,
        "detail": "This failure is not a deliberate Dex refusal, so it is most "
        "likely a Dex defect. A report helps: open the link (it carries the "
        "versions, connector, command and exception class, never the error "
        "text), or run `dex feedback` for the full checklist. An agent asks the "
        "person before filing, since an issue is public.",
        "link": bug_link(environment_, command=command),
        "command": "dex feedback" + (f" --command '{command}'" if command else ""),
    }


class FeedbackResult(Result):
    """The environment, the two prefilled links, and the reporting checklist."""

    environment: dict[str, Any]
    command: str | None = None

    def data(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "links": {
                "bug": bug_link(self.environment, command=self.command),
                "feature": feature_link(self.environment),
                "issues": f"{REPOSITORY}/issues",
                "security": f"{REPOSITORY}/security/advisories/new",
            },
            "guidance": list(GUIDANCE),
        }


def _configured(repo_root: str) -> tuple[str | None, str | None]:
    """The connector and semantic vendor ``.dex/config.yml`` names, if it loads.

    Best-effort on purpose: a broken config is a common reason to report
    something, and it must not stop the command that helps report it.
    """

    from .config import load_config

    with contextlib.suppress(Exception):
        config = load_config(repo_root)
        if config is not None:
            # Only what the file writes: the schema's defaults (duckdb, dbt)
            # would otherwise report a connector or a layer nobody chose.
            written = config.model_fields_set
            return (
                config.connector if "connector" in written else None,
                config.semantic.vendor if "semantic" in written else None,
            )
    return None, None


def cmd_feedback(
    args: argparse.Namespace, *, repo_root: str, result_mode: str | None = None
) -> env.Envelope:
    """The CLI shim. ``main`` routes here before it builds an engine."""

    connector, vendor = _configured(repo_root)
    connector = getattr(args, "connector", None) or connector
    command = (getattr(args, "command", None) or "").strip()
    if command.startswith("dex "):
        command = command[len("dex ") :]
    result = FeedbackResult(
        environment=environment(
            connector=connector, semantic_vendor=vendor, result_mode=result_mode
        ),
        command=command or None,
    )
    return to_envelope(result)
