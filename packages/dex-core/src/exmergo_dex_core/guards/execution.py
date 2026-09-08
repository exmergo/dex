"""What actually stops a guarded build overrunning, per connector.

`transform build` refuses a prod target, prices the run from a free dry run, and
takes the confirm handshake. All three happen in this process. A host running the
build in a sandbox is asking a different question: once dex has handed the work
to a dbt subprocess, what does the *provider* enforce, and what does it not.

The two answers differ more than they look. On BigQuery the binding control is
``maximum_bytes_billed``, and it is written into the project's ``profiles.yml``
by ``transform init`` from the ceiling configured at that moment, so a later
``--budget`` does not move it and a hand-written profile may not carry it at all.
On the compute-time connectors it is a statement timeout in the same file. On
Postgres and ClickHouse dex winds the confirmed budget into environment variables
the subprocess reads, which is the one case where the cap follows the budget of
the run. On DuckDB nothing provider-side binds anything, because there is no
provider: the file is opened read-only, and that is a different guarantee from a
spend control.

So this reads the rendered profile rather than reporting what dex would have
written. A control dex intended and the project does not carry is not a control,
and reporting it would be the specific failure this module exists to prevent: a
build that reports it was capped when it was not.

**A target named ``dev`` is not by itself evidence of anything.** ``binding`` is
the field that says whether any provider-side control was actually found, and it
is false far more often than a reader expects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..envelope import EstimateQuality, Paradigm, paradigm_unit


class ProviderControl(BaseModel):
    """One control the warehouse itself enforces on a build statement.

    ``source`` is where the value came from and is the field that decides how
    much it is worth: ``profile`` means it is written into the project's
    ``profiles.yml`` and is whatever was configured when that file was rendered,
    ``budget`` means dex winds the confirmed budget of *this* run into it, and
    ``connection`` means the adapter sets it on the handle it opens.
    """

    name: str
    binds: str
    source: str
    unit: str | None = None
    value: float | None = None
    detail: str | None = None


class GuardedExecution(BaseModel):
    """What a guarded build can and cannot be held to on this connector.

    ``binding`` is the headline: whether anything provider-side was actually
    found to enforce a limit on the statements this build will run. ``controls``
    is the evidence for it and ``unsupported`` is what this connector cannot be
    asked for at all, named rather than left to be inferred from an absence.
    """

    connector: str
    paradigm: Paradigm | None = None
    estimate_quality: EstimateQuality | None = None
    binding: bool = False
    controls: list[ProviderControl] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "paradigm": self.paradigm.value if self.paradigm else None,
            "estimate_quality": (
                self.estimate_quality.value if self.estimate_quality else None
            ),
            "binding": self.binding,
            "unsupported": self.unsupported,
            "controls": [c.model_dump(exclude_none=True) for c in self.controls],
        }


#: Per connector: the profile keys that carry a provider-side limit, and what
#: each binds. Keyed by connector rather than by paradigm because two
#: compute-time connectors cap by different mechanisms, and handing one of them
#: the other's key would report a build as capped when nothing capped it.
_PROFILE_CONTROLS: dict[str, list[tuple[str, str, str]]] = {
    "bigquery": [("maximum_bytes_billed", "statement", "bytes")],
    "snowflake": [
        ("query_tag", "statement", ""),
        ("statement_timeout_in_seconds", "statement", "seconds"),
    ],
    "databricks": [("session_properties", "session", "seconds")],
    "redshift": [("statement_timeout", "statement", "seconds")],
    "postgres": [("keepalives_idle", "connection", "seconds")],
}

#: Coverage each connector structurally cannot give a guarded build, stated by
#: name. An empty list would read as full coverage, which is never true.
_UNSUPPORTED: dict[str, list[str]] = {
    "bigquery": [
        "no server-side wall-clock cap on a build statement: the control is "
        "bytes billed, so a slow query inside the byte cap is not bounded by time"
    ],
    "snowflake": [
        "no bytes-scanned cap: the guarded quantity is warehouse time, so a "
        "statement that reads more than expected inside its timeout is not refused"
    ],
    "databricks": [
        "no bytes-scanned cap, and the estimate is a floor rather than a "
        "prediction, so the budget bounds time and not volume"
    ],
    "redshift": [
        "no bytes-scanned cap; Serverless additionally bills a 60-second wake "
        "minimum that no statement-level control avoids"
    ],
    "postgres": [
        "nothing is billed in currency, so the guarded quantity is load on the "
        "operational database rather than spend"
    ],
    "clickhouse": [
        "time alone is checked only at block boundaries, which is why the byte "
        "cap is set alongside it rather than instead of it"
    ],
    "duckdb": [
        "no provider-side spend control of any kind exists: DuckDB is a local "
        "file with no service to enforce a limit, so a build is bounded by the "
        "machine and by dex's own wall-clock watchdog and by nothing else"
    ],
}


def guarded_execution_preflight(
    adapter: Any,
    *,
    project_dir: Path | str | None = None,
    target: str = "dev",
    config: Any = None,
) -> GuardedExecution:
    """What the provider will enforce on this project's next guarded build.

    ``adapter`` supplies the connector, its paradigm, and how good its estimates
    are. ``project_dir`` is read for the rendered profile, and omitting it means
    the report describes the connector rather than this project: ``binding`` is
    then false and a note says why, because "dex did not look" and "nothing binds"
    are different answers and only one of them is safe to act on.

    Free, connectionless, and read-only. It opens no warehouse connection and
    spends nothing on any connector.
    """

    connector = str(getattr(adapter, "name", "") or "")
    paradigm = getattr(adapter, "paradigm", None)
    report = GuardedExecution(
        connector=connector,
        paradigm=paradigm if isinstance(paradigm, Paradigm) else None,
        estimate_quality=(
            adapter_quality
            if isinstance(
                adapter_quality := getattr(adapter, "estimate_quality", None),
                EstimateQuality,
            )
            else None
        ),
        unsupported=list(_UNSUPPORTED.get(connector, [])),
    )

    if connector == "duckdb":
        report.controls.append(
            ProviderControl(
                name="read_only",
                binds="connection",
                source="connection",
                detail="the database file is opened read-only, which bounds what "
                "dex reads and bounds nothing a dbt build writes to the dev target",
            )
        )
        report.notes.append(
            "a dev target on DuckDB is a local file. Nothing here is a spend "
            "control, and the absence is the report rather than an omission from it"
        )
        return report

    if project_dir is None:
        report.notes.append(
            "no project was read, so this describes the connector and not the "
            "build: run this from the project to learn what its profile actually sets"
        )
        return report

    from ..transform.dev_target import target_output

    project = Path(project_dir)
    output = target_output(project, target)
    if not output:
        report.notes.append(
            f"no '{target}' output found in the project's profiles.yml, so dex "
            "could not read what binds this build"
        )
        return report

    unit = paradigm_unit(report.paradigm)
    for key, binds, key_unit in _PROFILE_CONTROLS.get(connector, []):
        if key not in output:
            continue
        value = output[key]
        numeric: float | None = None
        detail = None
        if isinstance(value, dict):
            # Databricks carries the timeout inside session_properties.
            for inner, inner_value in value.items():
                if isinstance(inner_value, (int, float)):
                    numeric = float(inner_value)
                    detail = str(inner)
                    break
            if numeric is None:
                continue
        elif isinstance(value, (int, float)):
            numeric = float(value)
        else:
            # A tag or a string setting says something ran under dex, not that
            # anything was capped, so it is not counted as a control.
            continue
        report.controls.append(
            ProviderControl(
                name=detail or key,
                binds=binds,
                source="profile",
                unit=key_unit or unit,
                value=numeric,
                detail=(
                    "written into profiles.yml when the project was initialized, "
                    "from the ceiling configured then; a later --budget does not "
                    "move it"
                ),
            )
        )

    if connector in {"postgres", "clickhouse"}:
        ceiling = getattr(getattr(config, "budget", None), "ceiling", None)
        report.controls.append(
            ProviderControl(
                name=(
                    "max_execution_time and max_bytes_to_read"
                    if connector == "clickhouse"
                    else "statement_timeout"
                ),
                binds="statement",
                source="budget",
                unit=unit,
                value=float(ceiling) if isinstance(ceiling, (int, float)) else None,
                detail="wound into the dbt subprocess environment from this run's "
                "confirmed budget, so it follows the budget rather than the profile",
            )
        )
        if connector == "clickhouse":
            from ..transform.dev_target import _raw_target_key
            from ..transform.init import (
                CH_MAX_BYTES_TO_READ_ENV,
                CH_MAX_EXECUTION_TIME_ENV,
            )

            settings = _raw_target_key(project, target, "custom_settings")
            rendered = " ".join(str(v) for v in settings.values()) if settings else ""
            missing = [
                name
                for name in (CH_MAX_EXECUTION_TIME_ENV, CH_MAX_BYTES_TO_READ_ENV)
                if name not in rendered
            ]
            if missing:
                report.controls = [c for c in report.controls if c.source != "budget"]
                report.notes.append(
                    "this profile's custom_settings do not reference "
                    f"{' or '.join(missing)}, so nothing winds the budget into a "
                    "server-side cap: the budget bounds the estimate and nothing "
                    "bounds a statement that outruns it"
                )

    report.binding = bool(report.controls)
    if not report.binding:
        report.notes.append(
            "no provider-side control was found in this project's profile, so "
            "the confirmed budget bounds the estimate and nothing bounds a "
            "statement that outruns it. Re-run `transform init`, or set the cap "
            "in profiles.yml by hand"
        )
    return report


class StatementVerdict(BaseModel):
    """Whether one compiled statement may run under the guarded subset.

    ``node`` names the dbt node the statement compiled from, because a refusal a
    reviewer cannot locate in the project is a refusal they cannot act on.
    """

    node: str
    allowed: bool
    reason: str | None = None
    detail: str | None = None


def guarded_statement_verdict(
    sql: str,
    *,
    node: str,
    dialect: str,
    approved_functions: frozenset[str] | set[str] | None = None,
) -> StatementVerdict:
    """Adjudicate one compiled model statement for a guarded build.

    Two layers. The structural one is the same SELECT-only guard every statement
    dex sends already passes, so a compiled model carrying a ``CALL``, an
    ``EXECUTE IMMEDIATE``, a write, or DDL is refused with the reason named
    rather than with a parser's node type.

    The second is the function allowlist, and it runs **only when a caller
    supplies one**. An empty or absent set leaves it off, which is what keeps an
    interactive build working against a warehouse full of local functions the
    engine has never heard of. sqlglot parses a function it does not model as an
    anonymous call, so the check is over exactly those: a dialect builtin is
    modelled and never reaches it, and a project's own UDF does.
    """

    import sqlglot
    from sqlglot import expressions as exp

    from .sql_guard import NotSelectOnlyError, assert_select_only

    try:
        assert_select_only(sql, dialect=dialect)
    except NotSelectOnlyError as exc:
        return StatementVerdict(
            node=node,
            allowed=False,
            reason=exc.reason.value if exc.reason else None,
            detail=str(exc),
        )
    except Exception as exc:
        return StatementVerdict(
            node=node,
            allowed=False,
            reason="not_a_query",
            detail=f"could not be parsed, so it could not be adjudicated: {exc}",
        )

    if not approved_functions:
        return StatementVerdict(node=node, allowed=True)

    allowed = {name.lower() for name in approved_functions}
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return StatementVerdict(
            node=node,
            allowed=False,
            reason="not_a_query",
            detail="could not be parsed for the function allowlist",
        )
    called = {
        str(call.this).lower() for call in parsed.find_all(exp.Anonymous) if call.this
    }
    unapproved = sorted(called - allowed)
    if unapproved:
        return StatementVerdict(
            node=node,
            allowed=False,
            reason="unapproved_function",
            detail=(
                "calls "
                + ", ".join(unapproved)
                + ", which guards.approved_functions does not list"
            ),
        )
    return StatementVerdict(node=node, allowed=True)
