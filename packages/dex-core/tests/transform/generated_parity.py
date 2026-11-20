"""The parity harness: each generated test and ``maintain verify`` agree (#1450, #1451).

A declared fact Dex renders as a generated test has two implementations: the
test's SQL macro, which runs in the project's ``dbt build``, and the check
``maintain verify`` runs. This harness holds them to one answer.

**A row** names a generated test, the ``maintain verify`` finding codes it must
agree with, the declaration both fixture models carry, and two fixture models:
one that violates the declaration and one that meets it. For each row the
harness

1. declares both fixture models through ``transform plan`` with
   ``declarations.render_tests`` on, so the test is rendered (and its macro
   written) exactly as a customer's plan renders it;
2. applies the plan and builds both models and their tests with
   ``transform build --target dev``;
3. runs ``maintain verify --target dev`` on both models;
4. reports a disagreement wherever the violating model does not both fail the
   test and get the finding, or the clean model does not both pass the test
   and get no finding, and wherever the test's failure count differs from the
   number the finding states.

It drives everything through the CLI, the same commands a customer runs, so the
one runner serves DuckDB offline (``test_generated_parity.py``) and every
warehouse in ``integration.yml`` (each connector's ``*_transform.py`` calls
:func:`run_parity` with its own seeding). A row whose test does not support the
connector (``GeneratedTestKind.dialects``) must render nothing there and say so
under ``data.generated_tests``; the harness checks that instead (#1451).

Adding a generated test (#1453 onward) adds a :class:`ParityRow` to
:data:`ROWS`. ``freshness`` and continuity have no ``maintain verify`` check, so
they are proved by their own acceptance pairs, not here.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from exmergo_dex_core import generated_tests as gt

#: Where the fixture models live in the project, apart from anything else.
FOLDER = "models/dex_parity"


@dataclass(frozen=True)
class ParityRow:
    """One generated test held to ``maintain verify``.

    ``violating`` and ``clean`` render a SELECT over literal rows given the
    connector's spelling of a typed integer NULL, so one row serves every
    dialect. ``count`` reads, from the finding, the number the test's dbt
    ``failures`` must equal (``None`` where the finding states no count the
    test's ``fail_calc`` reproduces).
    """

    test: str
    codes: frozenset[str]
    declaration: Mapping[str, Any]
    violating: Callable[[str], str]
    clean: Callable[[str], str]
    count: Callable[[Mapping[str, Any]], int] | None = None
    #: Whether ``maintain verify``'s payload shows it measured the model at all,
    #: so a clean fixture's absent finding is a verdict and not a check that
    #: never ran (a refused statement, an unreadable relation). The payload is
    #: verify's ``data`` with its envelope's ``warnings`` under ``_warnings``.
    measured: Callable[[Mapping[str, Any], str], bool] | None = None
    #: The row's own name, where one test has several rows (#1453: an
    #: ``includes`` list and an ``excludes`` list); the test's name otherwise.
    name: str | None = None
    #: Undeclared models the row's fixtures read (#1454: the declared parent),
    #: by suffix: each is built as ``dex_parity_<slug>_<suffix>`` in the same
    #: selection, and the declaration names it so.
    fixtures: Mapping[str, Callable[[str], str]] = field(default_factory=dict)
    #: Arguments that pick this row's test out of several of one name on one
    #: model (#1454: a parent's ``missing`` and ``unexpected`` directions),
    #: matched against the rendered spec and the manifest's test kwargs.
    arguments: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.test

    @property
    def slug(self) -> str:
        return self.label.removeprefix("dex_")

    def model(self, side: str) -> str:
        return f"dex_parity_{self.slug}_{side}"

    def picks(self, arguments: Mapping[str, Any] | None) -> bool:
        """Whether a test with ``arguments`` is this row's test."""

        arguments = arguments or {}
        return all(arguments.get(k) == v for k, v in self.arguments.items())


def _rows(*rows: tuple[str, str]) -> str:
    """A SELECT over ``rows`` as (order_id, line_number). The first row names
    the columns, so it never holds a NULL whose type the warehouse would guess."""

    (order_id, line_number), *rest = rows
    lines = [f"select {order_id} as order_id, {line_number} as line_number"]
    lines += [f"union all select {a}, {b}" for a, b in rest]
    return "\n".join(lines) + "\n"


def _grain_measured(verify: Mapping[str, Any], model: str) -> bool:
    return any(
        record.get("model") == model and record.get("status") in {"proven", "broken"}
        for record in verify.get("grain_coverage") or []
    )


#: #1452: a composite grain. Three rows share one grain value and two share
#: another through a NULL in a grain column, which both sides count as a value
#: (``SELECT DISTINCT`` and ``GROUP BY`` alike). The violating model has three
#: rows beyond the first of each value; a macro counting duplicate *groups*
#: would say two, so the count comparison tells the two apart.
UNIQUE_COMBINATION = ParityRow(
    test=gt.UNIQUE_COMBINATION,
    codes=frozenset({"grain_broken"}),
    declaration={
        "description": "Parity fixture: one row per order line.",
        "grain": ["order_id", "line_number"],
    },
    violating=lambda null: _rows(
        ("1", "1"),
        ("1", "1"),
        ("1", "1"),
        ("1", "2"),
        ("2", null),
        ("2", null),
        ("3", "1"),
    ),
    clean=lambda null: _rows(("1", "1"), ("1", "2"), ("2", null), ("3", "1")),
    count=lambda finding: int(finding["data"]["duplicate_count"]),
    measured=_grain_measured,
)


def _statuses(*rows: tuple[str, str]) -> str:
    """A SELECT over ``rows`` as (id, status). ``None`` writes a NULL status,
    spelled ``nullif('x', 'x')``: a text NULL every dialect types from the
    expression, with no cast to spell per warehouse. The first row names the
    columns, so it is never NULL."""

    def value(status: str | None) -> str:
        return "nullif('x', 'x')" if status is None else status

    (first_id, first), *rest = rows
    lines = [f"select {first_id} as id, {value(first)} as status"]
    lines += [f"union all select {i}, {value(v)}" for i, v in rest]
    return "\n".join(lines) + "\n"


def _population_measured(verify: Mapping[str, Any], model: str) -> bool:
    """``maintain verify`` ran the population check over ``model``: the class
    is not suppressed (it is, when no model's filter could be measured) and no
    warning names this model's population, or its whole declared-facts
    statement, as unmeasured (a column the relation lacks, a refused
    statement). Another model's measured filter keeps the class unsuppressed,
    so the per-model warnings are what tell this model apart."""

    if "population" in (verify.get("suppressed") or {}):
        return False
    unmeasured = (
        f"declared population on {model} ",
        f"declared facts on {model} could not be measured",
    )
    return not any(
        marker in str(line)
        for line in verify.get("_warnings") or []
        for marker in unmeasured
    )


def _contradicting_rows(finding: Mapping[str, Any]) -> int:
    return int(finding["data"]["contradicting_rows"])


def _population_row(name: str, filters: dict, violating: str, clean: str) -> ParityRow:
    return ParityRow(
        test=gt.POPULATION_FILTER,
        name=name,
        codes=frozenset({"population_contradicted"}),
        declaration={
            "description": "Parity fixture: one row per order.",
            "population": {"filters": [{"column": "status", **filters}]},
        },
        violating=lambda _null: violating,
        clean=lambda _null: clean,
        count=_contradicting_rows,
        measured=_population_measured,
    )


#: #1453: an ``includes`` list. One row holds a value outside it.
POPULATION_INCLUDES = _population_row(
    "dex_population_filter_includes",
    {"includes": ["paid", "shipped"]},
    _statuses(("1", "'paid'"), ("2", "'shipped'"), ("3", "'refunded'")),
    _statuses(("1", "'paid'"), ("2", "'shipped'")),
)

#: #1453: a NULL is outside an ``includes`` list, on both sides.
POPULATION_INCLUDES_NULL = _population_row(
    "dex_population_filter_includes_null",
    {"includes": ["paid", "shipped"]},
    _statuses(("1", "'paid'"), ("2", "'shipped'"), ("3", None)),
    _statuses(("1", "'paid'"), ("2", "'shipped'")),
)

#: #1453: an ``excludes`` list. Two rows hold the excluded value, so the count
#: is compared too, and a NULL that breaks no ``excludes`` list sits in both
#: fixtures: counted by either side, the clean fixture would fail. The list also
#: holds a quote and a backslash, which each warehouse writes in its own literal
#: syntax: a literal the warehouse cannot parse errors the test.
POPULATION_EXCLUDES = _population_row(
    "dex_population_filter_excludes",
    {"excludes": ["cancelled", "it's", "a\\b"]},
    _statuses(("1", "'paid'"), ("2", "'cancelled'"), ("3", "'cancelled'"), ("4", None)),
    _statuses(("1", "'paid'"), ("2", "'shipped'"), ("4", None)),
)

#: #1453: text values against a number column, compared as text on both sides
#: (the column cast to the warehouse's string type).
POPULATION_NUMERIC = _population_row(
    "dex_population_filter_numeric",
    {"excludes": ["7"]},
    _statuses(("1", "1"), ("2", "7"), ("3", "8")),
    _statuses(("1", "1"), ("2", "8")),
)


def _customers(null_int: str, *rows: tuple[str, str | None]) -> str:
    """A SELECT over ``rows`` as (customer_id, status), the first naming the
    columns. A ``None`` status is a text NULL, as :func:`_statuses` writes it;
    a ``None`` id is the connector's typed integer NULL."""

    def status(value: str | None) -> str:
        return "nullif('x', 'x')" if value is None else f"'{value}'"

    def ident(value: str | None) -> str:
        return null_int if value is None else value

    (first_id, first), *rest = rows
    lines = [f"select {ident(first_id)} as customer_id, {status(first)} as status"]
    lines += [f"union all select {ident(i)}, {status(v)}" for i, v in rest]
    return "\n".join(lines) + "\n"


def _parent_measured(verify: Mapping[str, Any], model: str) -> bool:
    """``maintain verify`` judged ``model``'s declared parent."""

    return any(
        record.get("model") == model
        and record.get("status") in {"satisfied", "contradicted"}
        for record in verify.get("parent_population") or []
    )


def _parent_row(
    name: str,
    direction: str,
    parent: Callable[[str], str],
    violating: Callable[[str], str],
    clean: Callable[[str], str],
    filters: list[dict] | None = None,
) -> ParityRow:
    """A ``dex_parent_population`` row: the parent is a fixture the row
    builds, and both the violating and the clean model declare it."""

    slug = name.removeprefix("dex_")
    population: dict[str, Any] = {
        "parent": {
            "model": f"dex_parity_{slug}_parent",
            "key": {"customer_id": "customer_id"},
        }
    }
    if filters:
        population["filters"] = filters
    code, measure = {
        "missing": ("parent_population_missing", "missing_parents"),
        "unexpected": ("parent_population_unexpected", "unexpected_keys"),
    }[direction]
    return ParityRow(
        test=gt.PARENT_POPULATION,
        name=name,
        codes=frozenset({code}),
        declaration={
            "description": "Parity fixture: one row per customer.",
            "population": population,
        },
        violating=violating,
        clean=clean,
        count=lambda finding: int(finding["data"][measure]),
        measured=_parent_measured,
        fixtures={"parent": parent},
        arguments={"direction": direction},
    )


def _parent(null: str) -> str:
    """The declared parent: 3 is churned, and 4's status is NULL."""

    return _customers(
        null, ("1", "active"), ("2", "active"), ("3", "churned"), ("4", None)
    )


#: #1454: a parent key the model lacks. Two are missing, so the count is
#: compared too.
PARENT_MISSING = _parent_row(
    "dex_parent_population_missing",
    "missing",
    _parent,
    lambda null: _customers(null, ("1", "active"), ("2", "active")),
    lambda null: _customers(
        null, ("1", "active"), ("2", "active"), ("3", "churned"), ("4", None)
    ),
)

#: #1454: keys the parent lacks; 5 twice is one distinct key, so the count is
#: two, and a NULL key in both fixtures is no key on either side.
PARENT_UNEXPECTED = _parent_row(
    "dex_parent_population_unexpected",
    "unexpected",
    _parent,
    lambda null: _customers(
        null,
        ("1", "active"),
        ("2", "active"),
        ("3", "churned"),
        ("4", None),
        ("5", "active"),
        ("5", "active"),
        ("6", "active"),
        (None, "active"),
    ),
    lambda null: _customers(
        null,
        ("1", "active"),
        ("2", "active"),
        ("3", "churned"),
        ("4", None),
        (None, "active"),
    ),
)

#: #1454: a declared ``excludes`` filter scopes the eligible parents. 3 is
#: churned and not owed; 4's NULL status is no excluded value, so 4 is owed.
PARENT_MISSING_FILTERED = _parent_row(
    "dex_parent_population_missing_filtered",
    "missing",
    _parent,
    lambda null: _customers(null, ("1", "active"), ("2", "active")),
    lambda null: _customers(null, ("1", "active"), ("2", "active"), ("4", None)),
    filters=[{"column": "status", "excludes": ["churned"]}],
)

#: #1454: a filter on a column the parent does not have is skipped on both
#: sides (maintain verify lowers its finding to medium; the test fails), so
#: the count is the parents the filter cannot rule out.
PARENT_MISSING_UNAPPLIED = _parent_row(
    "dex_parent_population_missing_unapplied",
    "missing",
    lambda _null: (
        "select 1 as customer_id, 'web' as channel\n"
        "union all select 2, 'web'\nunion all select 3, 'store'\n"
    ),
    lambda null: _customers(null, ("1", "active"), ("2", "active")),
    lambda null: _customers(null, ("1", "active"), ("2", "active"), ("3", "paid")),
    filters=[{"column": "status", "includes": ["active", "paid"]}],
)

#: Every row the harness runs, in the order the generated tests shipped.
ROWS: tuple[ParityRow, ...] = (
    UNIQUE_COMBINATION,
    POPULATION_INCLUDES,
    POPULATION_INCLUDES_NULL,
    POPULATION_EXCLUDES,
    POPULATION_NUMERIC,
    PARENT_MISSING,
    PARENT_UNEXPECTED,
    PARENT_MISSING_FILTERED,
    PARENT_MISSING_UNAPPLIED,
)

#: The finding codes no row may raise on a clean model, whichever row it is.
ALL_CODES: frozenset[str] = frozenset().union(*(row.codes for row in ROWS))


@dataclass
class ParityReport:
    """What one connector's run established. ``disagreements`` is empty when
    every row agreed; ``unsupported`` names the rows whose test declares the
    connector unsupported and rendered nothing there, as it must."""

    connector: str | None
    disagreements: list[str] = field(default_factory=list)
    agreed: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    envelopes: dict[str, Any] = field(default_factory=dict)
    #: Per command: seconds on the wall clock and the envelope's ``cost``, so a
    #: billed job can say what the harness spent (#1451).
    spend: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, step: str, envelope: Mapping[str, Any], seconds: float) -> None:
        self.envelopes[step] = envelope
        self.spend[step] = {
            "seconds": round(seconds, 1),
            "status": envelope.get("status"),
            "cost": envelope.get("cost"),
        }


Runner = Callable[[list[str]], tuple[int, dict]]


def payload(
    rows: tuple[ParityRow, ...] = ROWS,
    *,
    null_int: str = "cast(null as integer)",
    extra_edits: list[dict] | None = None,
) -> dict:
    """The edits payload declaring both fixture models of every row."""

    edits: list[dict] = list(extra_edits or [])
    declarations: list[dict] = []
    for row in rows:
        for suffix, render in row.fixtures.items():
            edits.append(
                {
                    "path": f"{FOLDER}/{row.model(suffix)}.sql",
                    "kind": "model_sql",
                    "content": render(null_int),
                }
            )
        for side, render in (("violating", row.violating), ("clean", row.clean)):
            name = row.model(side)
            edits.append(
                {
                    "path": f"{FOLDER}/{name}.sql",
                    "kind": "model_sql",
                    "content": render(null_int),
                }
            )
            declarations.append({"model": name, **row.declaration})
    return {"edits": edits, "declarations": declarations}


def switch_on(repo: Path, rows: tuple[ParityRow, ...] = ROWS) -> None:
    """Turn on exactly the rows' generated tests in ``.dex/config.yml``."""

    config_path = repo / ".dex" / "config.yml"
    config: dict[str, Any] = {}
    if config_path.is_file():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config.setdefault("declarations", {})["render_tests"] = sorted(
        {row.test for row in rows}
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _paid(budget: float | None) -> list[str]:
    return [] if budget is None else ["--confirm", "--budget", str(budget)]


def run_parity(
    run: Runner,
    repo: Path,
    project: Path | None = None,
    *,
    connector: str | None,
    budget: float | None = None,
    rows: tuple[ParityRow, ...] = ROWS,
    null_int: str = "cast(null as integer)",
    extra_edits: list[dict] | None = None,
) -> ParityReport:
    """Run every row on the connector ``repo``'s ``.dex/config.yml`` names.

    ``project`` is the dbt project directory (its ``target/`` holds the run
    results the test's failure count is read from), by default the one
    directory under ``repo`` holding a ``dbt_project.yml``. ``budget`` is the
    connector's magnitude for ``--confirm --budget`` on a metered connector,
    ``None`` on a free one. ``extra_edits`` ride in the same plan: a planted
    per-dialect macro, which is how the harness is shown to catch a broken
    macro.
    """

    report = ParityReport(connector=connector)

    def step(name: str, argv: list[str]) -> tuple[int, dict]:
        started = time.monotonic()
        rc, envelope = run(argv)
        report.record(name, envelope, time.monotonic() - started)
        return rc, envelope

    if project is None:
        [project] = [p.parent for p in repo.glob("*/dbt_project.yml")]
    root = ["--repo-root", str(repo)]
    switch_on(repo, rows)
    edits_file = repo / "dex_parity_edits.json"
    edits_file.write_text(
        json.dumps(payload(rows, null_int=null_int, extra_edits=extra_edits)),
        encoding="utf-8",
    )
    rc, planned = step(
        "plan",
        [
            *root,
            "transform",
            "plan",
            "parity fixtures",
            "--edits-file",
            str(edits_file),
        ],
    )
    if rc != 0 or planned["status"] != "ok":
        report.disagreements.append(f"the plan failed: {planned.get('errors')}")
        return report
    listed = (planned["data"].get("generated_tests") or {}).get("tests") or []

    live: list[ParityRow] = []
    for row in rows:
        kind = next(k for k in gt.kinds() if k.name == row.test)
        models = {row.model("violating"), row.model("clean")}
        claims = _claims(row, kind)
        mine = [
            t
            for t in listed
            if t["test"] == row.test
            and t["model"] in models
            and (claims is None or t["claim"] in claims)
        ]
        if gt.supports(kind, connector):
            if sorted(t["status"] for t in mine) != ["added", "added"]:
                report.disagreements.append(
                    f"{row.label}: the plan did not render the test on both "
                    f"fixtures: {mine}"
                )
                continue
            live.append(row)
            continue
        # #1451: a dialect the test does not support renders nothing there.
        statuses = {(t["status"], t.get("reason")) for t in mine}
        rendered = any(row.test in d.get("diff", "") for d in planned.get("diffs", []))
        if statuses != {("not_rendered", gt.DIALECT_UNSUPPORTED)} or rendered:
            report.disagreements.append(
                f"{row.label}: {connector} is unsupported, yet the plan says {mine}"
            )
        else:
            report.unsupported.append(row.label)
        report.models.extend(sorted(models))

    rc, applied = step("apply", [*root, "transform", "apply"])
    if rc != 0:
        report.disagreements.append(f"apply failed: {applied.get('errors')}")
        return report
    selected = [
        row.model(side)
        for row in rows
        for side in (*row.fixtures, "violating", "clean")
    ]
    report.models = sorted(set(report.models) | set(selected))

    _rc, built = step(
        "build",
        [
            *root,
            "transform",
            "build",
            "--target",
            "dev",
            "--select",
            " ".join(selected),
            *_paid(budget),
        ],
    )
    results, tests = _artifacts(project)
    model_status = {
        n["name"]: n["status"] for n in (built.get("data") or {}).get("nodes", [])
    }
    for name in dict.fromkeys(selected):
        if model_status.get(name) != "success":
            report.disagreements.append(
                f"{name} did not build ({model_status.get(name)}): "
                f"{built.get('errors')}"
            )
    if not live and not report.unsupported:
        return report

    _rc, verified = step(
        "verify",
        [*root, "maintain", "verify", "--target", "dev", *selected, *_paid(budget)],
    )
    verify_data = {
        **(verified.get("data") or {}),
        "_warnings": verified.get("warnings") or [],
    }
    findings = verify_data.get("findings") or []

    for row in live:
        _judge(row, results, tests, findings, report, verify_data)
    for row in rows:
        if row.label in report.unsupported:
            _judge_unsupported(row, tests, findings, report, verify_data)
    return report


def _judge(
    row: ParityRow,
    results: Mapping[str, Mapping[str, Any]],
    tests: list[tuple[str, str, dict[str, Any], str]],
    findings: list[Mapping[str, Any]],
    report: ParityReport,
    verify: Mapping[str, Any],
) -> None:
    for side, should_fail in (("violating", True), ("clean", False)):
        model = row.model(side)
        uid = _test_uid(tests, model, row)
        result = results.get(uid or "", {})
        status = result.get("status")
        found = [
            f for f in findings if f["identifier"] == model and f["code"] in row.codes
        ]
        test_failed = status == "fail"
        test_passed = status == "pass"
        where = f"{row.label} on {model}"
        if should_fail:
            if not test_failed:
                report.disagreements.append(
                    f"{where}: the test should fail on the violating fixture, "
                    f"dbt says {status!r}"
                )
            if not found:
                report.disagreements.append(
                    f"{where}: maintain verify raised none of "
                    f"{sorted(row.codes)} on the violating fixture"
                )
            if test_failed and found and row.count is not None:
                expected = row.count(found[0])
                if int(result.get("failures") or 0) != expected:
                    report.disagreements.append(
                        f"{where}: dbt counts {result.get('failures')} failure(s), "
                        f"maintain verify counts {expected}"
                    )
        else:
            if row.measured is not None and not row.measured(verify, model):
                report.disagreements.append(
                    f"{where}: maintain verify did not measure the clean fixture, "
                    "so its absent finding is no verdict"
                )
            if not test_passed:
                report.disagreements.append(
                    f"{where}: the test should pass on the clean fixture, dbt "
                    f"says {status!r}"
                )
            if found:
                report.disagreements.append(
                    f"{where}: maintain verify raised {[f['code'] for f in found]} "
                    "on the clean fixture"
                )
    if not any(
        d.startswith((f"{row.label} ", f"{row.label}:")) for d in report.disagreements
    ):
        report.agreed.append(row.label)


def _judge_unsupported(
    row: ParityRow,
    tests: list[tuple[str, str, dict[str, Any], str]],
    findings: list[Mapping[str, Any]],
    report: ParityReport,
    verify: Mapping[str, Any],
) -> None:
    """On an unsupported dialect no test exists, and ``maintain verify`` alone
    answers: still the finding on the violating fixture and none on the clean."""

    for side, owed in (("violating", True), ("clean", False)):
        model = row.model(side)
        if _test_uid(tests, model, row) is not None:
            report.disagreements.append(
                f"{row.label}: a test built on {model} where the dialect is unsupported"
            )
        found = any(
            f["identifier"] == model and f["code"] in row.codes for f in findings
        )
        if not owed and row.measured is not None and not row.measured(verify, model):
            report.disagreements.append(
                f"{row.label}: maintain verify did not measure {model}"
            )
        if found is not owed:
            report.disagreements.append(
                f"{row.label}: maintain verify {'missed' if owed else 'raised'} "
                f"{sorted(row.codes)} on {model}"
            )


def _claims(row: ParityRow, kind: gt.GeneratedTestKind) -> set[str] | None:
    """The claims of ``row``'s own test on its two fixtures, where its
    ``arguments`` pick one of several instances of the kind on one model, so
    the plan's ``data.generated_tests`` rows (which name a claim, not the
    arguments) are told apart. ``None`` where every instance is the row's."""

    if not row.arguments:
        return None
    from exmergo_dex_core.transform.claims import claim_id
    from exmergo_dex_core.transform.declarations import parse_declarations

    out: set[str] = set()
    for side in ("violating", "clean"):
        model = row.model(side)
        [declaration] = parse_declarations([{"model": model, **row.declaration}])
        for instance in kind.instances(declaration):
            if row.picks(instance.arguments):
                out.add(
                    claim_id(
                        model, instance.field, instance.columns, dict(instance.params)
                    )
                )
    return out


def _test_uid(
    tests: list[tuple[str, str, dict[str, Any], str]], model: str, row: ParityRow
) -> str | None:
    """The unique id of ``row``'s test on ``model``, or ``None``."""

    for attached, name, kwargs, uid in tests:
        if attached == model and name == row.test and row.picks(kwargs):
            return uid
    return None


def _artifacts(
    project: Path,
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, dict[str, Any], str]]]:
    """The newest run results under the project's target path, by unique id,
    and each generic test as (attached model, test name, kwargs, unique id)."""

    candidates = sorted(
        (project / "target").rglob("run_results.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        return {}, []
    folder = candidates[-1].parent
    results = {
        r["unique_id"]: r
        for r in json.loads(candidates[-1].read_text(encoding="utf-8"))["results"]
    }
    tests: list[tuple[str, str, dict[str, Any], str]] = []
    manifest = folder / "manifest.json"
    if manifest.is_file():
        for uid, node in json.loads(manifest.read_text(encoding="utf-8"))[
            "nodes"
        ].items():
            meta = node.get("test_metadata") or {}
            attached = node.get("attached_node") or ""
            if node.get("resource_type") == "test" and meta.get("name"):
                tests.append(
                    (
                        attached.rsplit(".", 1)[-1],
                        str(meta["name"]),
                        dict(meta.get("kwargs") or {}),
                        uid,
                    )
                )
    return results, tests
