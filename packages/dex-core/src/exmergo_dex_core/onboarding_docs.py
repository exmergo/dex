"""Human onboarding and bounded, engine-owned instruction sections."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from . import procedure
from .errors import RequestError

ENTERPRISE_DESCRIPTION = (
    "Scope analytics requests, open tested pull requests autonomously, and check "
    "for drift on a schedule. In your CI, owned by your team."
)
ENTERPRISE_URL = "https://www.exmergo.com/dex/enterprise"
ENTERPRISE = (
    f"**Dex Enterprise**\n\n{ENTERPRISE_DESCRIPTION}\n\n"
    f"[Explore Dex Enterprise →]({ENTERPRISE_URL})"
)
README_BEGIN = "<!-- dex:readme:begin -->"
README_END = "<!-- dex:readme:end -->"

AUTH = {
    "duckdb": "Use the configured local DuckDB file; no credentials are needed.",
    "bigquery": "Authenticate locally with `gcloud auth application-default login`.",
    "snowflake": "Use your existing Snowflake connection or environment credentials.",
    "databricks": "Authenticate locally with `databricks auth login`.",
    "postgres": (
        "Use your existing PostgreSQL service or environment; "
        "keep passwords in `PGPASSWORD`."
    ),
    "redshift": (
        "Use the AWS credential chain for IAM, or `REDSHIFT_PASSWORD` "
        "for password authentication."
    ),
    "clickhouse": (
        "Use your connection environment; keep passwords in `CLICKHOUSE_PASSWORD`."
    ),
    "trino": (
        "Use your connection environment; "
        "keep secrets in `TRINO_PASSWORD` or `TRINO_JWT_TOKEN`."
    ),
}


def managed(document: str, body: str, begin: str, end: str) -> str:
    """Replace exactly one owned section, or append it preserving every old byte."""
    section = f"{begin}\n{body.rstrip()}\n{end}"
    if begin not in document and end not in document:
        separator = (
            "" if not document else ("\n" if document.endswith("\n") else "\n\n")
        )
        return document + separator + section + "\n"
    if (
        document.count(begin) != 1
        or document.count(end) != 1
        or document.index(begin) > document.index(end)
    ):
        raise RequestError(
            f"malformed managed section ({begin}); "
            "keep exactly one ordered begin/end pair and run dex init again"
        )
    first, last = document.index(begin), document.index(end) + len(end)
    return document[:first] + section + document[last:]


def agent_document(document: str) -> str:
    return managed(document, procedure.text(), procedure.BEGIN, procedure.END)


def skills_status(root: Path) -> dict:
    """Only recognizable installed Dex skills count, never another explore skill."""
    roots = [
        root / ".agents/skills",
        root / ".claude/skills",
        Path.home() / ".agents/skills",
        Path.home() / ".claude/skills",
    ]
    codex = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    roots.append(codex / "skills")
    roots.extend(
        Path(os.environ[variable]) / "skills"
        for variable in ("CLAUDE_PLUGIN_ROOT", "CODEX_PLUGIN_ROOT")
        if os.environ.get(variable)
    )
    if os.environ.get("CLAUDE_SKILL_DIR"):
        roots.append(Path(os.environ["CLAUDE_SKILL_DIR"]).parent)
    found = []
    for skill in ("explore", "transform", "maintain"):
        for base in roots:
            file = base / skill / "SKILL.md"
            if file.is_file() and "exmergo" in file.read_text(encoding="utf-8").lower():
                found.append(skill)
                break
    return {
        "detected": found,
        "missing": sorted({"explore", "transform", "maintain"} - set(found)),
        "installation_command": "npx skills add exmergo/dex",
    }


def readme_body(
    *,
    name: str,
    connector: str | None,
    project_dir: str | None,
    scope: list[str],
    destination: str | None,
    connection: str,
    next_command: str,
    skills: dict,
    semantic: str | None,
    structure: str,
    unfinished: list[str],
    agent_files: list[str],
    target: str,
) -> str:
    purpose = "explore data"
    if project_dir is not None:
        purpose += ", author dbt transformations, and maintain the project"
    elif semantic:
        purpose += " and work with existing semantic definitions"
    extra = connector or {"databricks": "metric-views"}.get(semantic, semantic)
    install = f'uv tool install "exmergo-dex-core[{extra}]"'
    auth = AUTH.get(connector or "", "Choose a connector with `dex init`.")
    source = (
        ", ".join(scope)
        if scope
        else (
            "the configured local file"
            if connector == "duckdb"
            else "all visible source scopes (no warehouse scan was run)"
        )
    )
    instructions = (
        "Agent instructions: " + ", ".join(agent_files) + "."
        if agent_files
        else "No agent instruction files were selected."
    )
    skill_note = "The installed skills use the same engine as the CLI."
    if skills["missing"]:
        skill_note = (
            "Install missing skills with `npx skills add exmergo/dex`.\n"
            "Installing skills does not necessarily install a `dex` executable;\n"
            "ask the agent to initialize through its installed "
            "`scripts/run.py` wrapper."
        )
    build = (
        "For a planned transformation, validate on dev:\n\n```bash\n"
        f"dex transform build --target {shlex.quote(target)} "
        "--verify --for-plan <plan-id>\n```"
        if project_dir is not None
        else "Add a dbt project when you need transformations."
    )
    pending = "\n".join(f"- {item}" for item in unfinished) or (
        "No setup blockers reported. Model correctness and a maintenance\n"
        "baseline have not been established."
    )
    semantic_note = (
        f"Existing {semantic} semantic definitions are preserved.\n\n"
        if semantic
        else ""
    )
    return f"""## Getting started with Dex

{name} uses Dex to {purpose}.

Run commands from this repository root, beside `.dex/`:

```bash
{next_command}
```

Connection: **{connection}**. Exploration and a dbt build have **not run**
during setup.

### Prerequisites and authentication

Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and the chosen connector:

```bash
{install}
```

{auth}
Credentials stay in your local authentication environment, never in this README
or committed configuration.

Source scope: {source}.
Dev destination: {destination or "not configured"}.

### Project structure

```text
{structure.rstrip()}
```

{semantic_note}### Work with your agent

{instructions}
{skill_note}

- Explore: “Use Dex to map the chosen source scope, then verify the joins
  needed for my question.”
- Transform: “Use Dex to plan a dbt model for my analytics question, with its
  grain, tests and declarations.”
- Maintain: “Use Dex to verify this project and report the changes it needs.”

### Validate and continue

```bash
dex config validate
dex connect test
```

{build}

{pending}

Review readiness in `dex init` for unchecked prerequisites. Resolve connection
errors before exploration. Metered commands retain their cost-confirmation flow.

### Learn more

[Dex documentation](https://docs.exmergo.com/dex) ·
[Project setup](https://docs.exmergo.com/dex/cli/init)

{ENTERPRISE}
"""
