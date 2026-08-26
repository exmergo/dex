"""Where is this used: every reference to a name, and how sure dex is.

A caller renaming a column, removing a project variable, or deleting a model has
to find every use of it. A text search misses the ones that matter most, because
dbt's indirection is jinja: a ``var()`` read inside a macro body, a ``ref()``
composed from a variable, a column named only in a ``schema.yml`` test, a package
that ships a model the project shadows. Missing one is the normal outcome and the
failure is partial, so the project still compiles and the gap is found later by
somebody else.

Two properties matter more than breadth here.

**Jinja-aware.** The scan reads calls, not text, through
:func:`~..dbt_project.jinja_calls`, so a call written inside a macro counts and a
nested call is seen in its own right.

**Honest about its limits.** A reference dex cannot resolve statically is
reported as unresolved rather than dropped, and the report carries a completeness
verdict that is ``incomplete`` unless every reason to doubt it has been ruled
out. A completeness claim that is not complete is worse than no claim: it is the
one thing a caller would act on without checking.

Read-only and repo-only. Nothing here opens a connection, and it stays inside the
zero-extra install: ``sqlglot`` is reached lazily for column resolution and its
absence becomes a stated limit rather than a refusal.
"""

from __future__ import annotations

import csv
from collections import defaultdict, deque
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml
from pydantic import BaseModel, Field

from .dbt_project import (
    DbtProjectView,
    JinjaCall,
    jinja_calls,
    jinja_regions,
    metric_inputs,
    node_files,
    node_name,
    path_family,
    physical_column,
    semantic_yaml_entries,
    yaml_documents,
)
from .dbt_project import load as load_project
from .errors import RequestError
from .results import Result, to_envelope

if TYPE_CHECKING:
    import argparse

    from . import envelope as env
    from .engine import DexEngine

#: What a name can be used as. `model`, `seed` and `snapshot` are separate kinds
#: even though `ref()` reaches all three, because a caller renaming one needs to
#: know which it is looking at.
KINDS = (
    "model",
    "source",
    "seed",
    "snapshot",
    "macro",
    "var",
    "column",
    "metric",
    "entity",
    "dimension",
    "measure",
)

#: Jinja callees that are dbt's own and are therefore not macro references. A
#: `config()` call configures the node it sits in and names nothing; `ref`,
#: `source` and `var` are read as their own kinds above.
_BUILTIN_CALLEES = frozenset({"ref", "source", "var", "config"})

#: Statement keywords that open a definition rather than call one. `{% macro
#: foo() %}` reads as a call to `foo` to any scanner, and reporting it as a use
#: of the macro would mean every macro is used at least once by itself.
_DEFINING_KEYWORDS = ("macro", "test", "snapshot", "materialization")

RESOLVED = "resolved"
INDETERMINATE = "indeterminate"


class Reference(BaseModel):
    """One occurrence of one name in one file.

    ``kind`` is what the name is being used *as*; ``form`` is how the reference
    is *written*. They are separate because one kind arrives in several forms and
    a caller fixing them needs to know which: a column named in a ``schema.yml``
    test is edited differently from the same column selected in SQL.

    ``name`` is ``None`` exactly when ``resolution`` is ``indeterminate``: dex
    read a reference it could not resolve to a name, which is a fact about that
    call site, not about any particular target.
    """

    path: str
    line: int
    form: str
    kind: str
    name: str | None = None
    resolution: str = RESOLVED
    package: str | None = None
    note: str | None = None
    #: The node this occurrence belongs to, where the file does not say. A model's
    #: SQL file is named after its node, but a `schema.yml` holds entries for
    #: several and a semantic YAML points at one through `model:`. Lineage is
    #: answered from this, so a column documented in a shared `schema.yml` is
    #: placed in the model it documents rather than in a node called "schema".
    owner: str | None = None


class ReferenceIndex:
    """Every reference in one project, scanned once and queried many times.

    Built from a loaded view rather than a path so it stays a pure projection
    over what the project says, and so a caller holding an already-loaded view
    (the delete guard holds one, with its own in-memory edits applied) can index
    the state it cares about rather than what is on disk.

    Installed packages are indexed too, as their own projects, because a package
    that ships a model the project shadows is a reference to that name in both
    places and reporting only one of them is how an override goes unnoticed.
    """

    def __init__(self, view: DbtProjectView, *, scan_packages: bool = True) -> None:
        self.view = view
        self.limits: list[str] = []
        self._by_name: dict[tuple[str, str], list[Reference]] = defaultdict(list)
        self._indeterminate: list[Reference] = []
        self._model_refs: dict[str, set[str]] = defaultdict(set)
        self._sqlglot_missing = False
        self._macro_args: list[tuple[str, int, str, str]] = []

        self._scan(view, package=None)
        if scan_packages:
            self._scan_packages()
        self._resolve_macro_args()
        self._mark_shadowing()

    # --- querying -------------------------------------------------------------

    def kinds_of(self, name: str) -> list[str]:
        """Every kind ``name`` is used as, in :data:`KINDS` order.

        What makes ``--kind`` optional. A caller asking where ``department_name``
        is used usually does not know whether the project calls it a column, a
        var, or both, and answering only one of those is the failure this command
        exists to prevent.
        """

        kinds = [kind for kind in KINDS if self._by_name.get((kind, name))]
        if kinds or not self._is_qualified(name):
            return kinds
        # A dotted name is a source (`raw.orders`) as often as it is a qualified
        # column (`stg_orders.order_id`). The name as written wins; the column
        # reading is the fallback, so `raw.orders` is never answered as a column
        # called `orders` that happens to exist somewhere else.
        column = name.split(".", 1)[1]
        return ["column"] if self._by_name.get(("column", column)) else []

    def references_to(self, name: str, kind: str) -> tuple[list[Reference], list[str]]:
        """Every reference to ``name`` as ``kind``, plus any limits the query adds.

        A bare column name is matched by name across the whole project and says
        so; a qualified ``model.column`` is resolved through the ``ref()`` graph,
        and occurrences of that name outside the lineage are still returned,
        marked, rather than dropped. Neither answer is silently narrowed: a
        caller who asked the imprecise question gets the imprecise answer
        labelled, which is the only version of it worth having.
        """

        if kind != "column":
            return list(self._by_name.get((kind, name), [])), []

        query_limits = (
            [
                "columns in model SQL were not read: sqlglot is not installed, so "
                "dex could not tell a column from any other identifier. Install a "
                "connector extra, or `exmergo-dex-core[sql]`, for those"
            ]
            if self._sqlglot_missing
            else []
        )
        if not self._is_qualified(name):
            return list(self._by_name.get(("column", name), [])), query_limits

        model, _, column = name.partition(".")
        hits = list(self._by_name.get(("column", column), []))
        if not self._by_name.get(("model", model)) and model not in self._model_refs:
            return hits, [
                *query_limits,
                f"'{model}' is not a model in this project, so '{name}' was "
                "matched by column name alone",
            ]

        lineage = self._lineage(model)
        return [
            hit.model_copy(
                update={
                    "note": "lineage_resolved"
                    if (hit.owner or node_name(hit.path)) in lineage
                    else "same_name_elsewhere"
                }
            )
            for hit in hits
        ], query_limits

    def indeterminate_for(self, kind: str) -> list[Reference]:
        """Unresolved references that could have been references of ``kind``.

        These are why a report says ``incomplete``. A ``{{ ref(var('x')) }}``
        names a model dex cannot read, so it belongs in the answer to every
        question about a model, and belongs in none about a macro.
        """

        return [ref for ref in self._indeterminate if ref.kind == kind]

    def _lineage(self, model: str) -> set[str]:
        """``model`` and everything that reaches it through ``ref()``, transitively."""

        reached = {model}
        pending = deque([model])
        while pending:
            current = pending.popleft()
            for candidate, refs in self._model_refs.items():
                if current in refs and candidate not in reached:
                    reached.add(candidate)
                    pending.append(candidate)
        return reached

    @staticmethod
    def _is_qualified(name: str) -> bool:
        return "." in name

    # --- scanning -------------------------------------------------------------

    def _record(self, reference: Reference) -> None:
        if reference.resolution == INDETERMINATE or reference.name is None:
            self._indeterminate.append(reference)
            return
        self._by_name[(reference.kind, reference.name)].append(reference)

    def _scan(
        self, view: DbtProjectView, *, package: str | None, prefix: str = ""
    ) -> None:
        nodes = node_files(view)
        node_kinds = {node_name(path): self._node_kind(view, path) for path in nodes}
        for raw_path, source in sorted(view.files.items()):
            path = f"{prefix}{raw_path}"
            family = path_family(Path(view.root), raw_path, view)
            if raw_path in nodes:
                self._record(
                    Reference(
                        path=path,
                        line=1,
                        form="definition",
                        kind=node_kinds[node_name(path)],
                        name=node_name(path),
                        package=package,
                    )
                )
            if path.endswith(".sql"):
                self._scan_sql(source.content, path, node_kinds, package=package)
            elif family == "seed" and path.endswith(".csv"):
                self._scan_seed_header(source.content, path, package=package)
        self._scan_yaml(view, node_kinds, package=package, prefix=prefix)
        self._scan_project_vars(view, package=package, prefix=prefix)

    @staticmethod
    def _node_kind(view: DbtProjectView, path: str) -> str:
        family = path_family(Path(view.root), path, view)
        return family if family in ("seed", "snapshot") else "model"

    def _scan_sql(
        self,
        content: str,
        path: str,
        node_kinds: dict[str, str],
        *,
        package: str | None,
    ) -> None:
        regions, _masked = jinja_regions(content)
        defined: set[int] = set()
        for region in regions:
            keyword = region.body.strip().lstrip("-").strip().split("(")[0].split()
            if (
                region.kind == "statement"
                and keyword
                and keyword[0] in _DEFINING_KEYWORDS
                and region.calls
            ):
                first = region.calls[0]
                defined.add(id(first))
                if keyword[0] in ("macro", "materialization", "test"):
                    self._record(
                        Reference(
                            path=path,
                            line=first.line,
                            form="definition",
                            kind="macro",
                            name=first.callee,
                            package=package,
                        )
                    )

        for call in (c for region in regions for c in region.calls):
            if id(call) in defined:
                continue
            self._record_call(call, path, node_kinds, package=package)

        self._scan_columns(content, path, package=package)

    def _record_call(
        self,
        call: JinjaCall,
        path: str,
        node_kinds: dict[str, str],
        *,
        package: str | None,
    ) -> None:
        if call.callee == "config":
            return
        if call.callee == "ref":
            # ref('pkg', 'model') and source('name', 'table') both name the
            # relation last, which is the rule the row-attribution renderer
            # already relies on. Reading the first argument instead is what the
            # older regex did, and it silently reported the package as the model.
            target = call.args[-1] if call.args else None
            self._record(
                Reference(
                    path=path,
                    line=call.line,
                    form="ref_call",
                    kind=node_kinds.get(target or "", "model"),
                    name=target,
                    resolution=RESOLVED if target else INDETERMINATE,
                    package=package,
                )
            )
            if target and node_name(path) != target:
                self._model_refs[node_name(path)].add(target)
            return
        if call.callee == "source":
            resolved = len(call.args) >= 2 and None not in call.args[-2:]
            self._record(
                Reference(
                    path=path,
                    line=call.line,
                    form="source_call",
                    kind="source",
                    name=f"{call.args[-2]}.{call.args[-1]}" if resolved else None,
                    resolution=RESOLVED if resolved else INDETERMINATE,
                    package=package,
                )
            )
            return
        if call.callee == "var":
            target = call.args[0] if call.args else None
            self._record(
                Reference(
                    path=path,
                    line=call.line,
                    form="var_call",
                    kind="var",
                    name=target,
                    resolution=RESOLVED if target else INDETERMINATE,
                    package=package,
                )
            )
            return
        if call.callee not in _BUILTIN_CALLEES:
            self._record(
                Reference(
                    path=path,
                    line=call.line,
                    form="macro_call",
                    kind="macro",
                    name=call.callee,
                    package=package,
                )
            )
            # A macro is routinely handed a column name as a string, and that
            # string is invisible to the SQL parser because the whole call is
            # jinja. It is a use of the column all the same, and exactly the kind
            # a rename leaves behind. Held until the scan finishes, because
            # whether the string names a column is only knowable once every
            # column in the project is known.
            self._macro_args.extend(
                (path, call.line, arg, node_name(path)) for arg in call.args if arg
            )

    def _resolve_macro_args(self) -> None:
        """Literal macro arguments that name a column the project declares.

        Name-matched by construction, and labelled that way: dex knows the string
        was passed to a macro and that the project has a column of that name, not
        that the macro uses it as one. Reporting it is still right, because the
        alternative is a rename that compiles and quietly drops a column.
        """

        for path, line, arg, owner in self._macro_args:
            column = arg.rsplit(".", 1)[-1]
            if not self._by_name.get(("column", column)):
                continue
            self._record(
                Reference(
                    path=path,
                    line=line,
                    form="macro_arg_column",
                    kind="column",
                    name=column,
                    owner=owner,
                    note="passed to a macro as a string; matched by name",
                )
            )

    def _mark_shadowing(self) -> None:
        """Name every definition that a package also supplies, on both sides.

        A project model with the same name as a packaged one shadows it, and dbt
        resolves the project's. Reporting only the winner hides the fact that the
        package still ships the loser, which is what makes an override surprising
        when somebody removes the local file.
        """

        for (kind, name), references in self._by_name.items():
            definitions = [r for r in references if r.form == "definition"]
            if len(definitions) < 2:
                continue
            packaged = [r for r in definitions if r.package]
            local = [r for r in definitions if not r.package]
            if not packaged or not local:
                continue
            names = ", ".join(sorted({str(r.package) for r in packaged}))
            for reference in local:
                reference.note = f"shadows the {kind} of the same name in {names}"
            for reference in packaged:
                reference.note = f"shadowed by this project's own {kind} '{name}'"

    def _scan_columns(self, content: str, path: str, *, package: str | None) -> None:
        """Column identifiers in one model's SQL, positioned in the original file.

        Two readers, deliberately. ``sqlglot`` says which identifiers are columns,
        which is the part a text scan cannot do; the tokenizer says where each one
        sits, which is the part the parse tree does not carry. Both run over the
        same jinja-blanked text, which preserves every offset and newline, so a
        reported line is the line in the file a human will open.
        """

        try:
            import sqlglot
            from sqlglot import expressions as exp
        except ImportError:
            self._sqlglot_missing = True
            return

        blanked = _blank_jinja(content)
        try:
            parsed = sqlglot.parse_one(blanked)
        except Exception:
            self.limits.append(f"{path}: dex could not parse the SQL for columns")
            return
        if parsed is None:
            return

        columns = {column.name for column in parsed.find_all(exp.Column) if column.name}
        columns |= {alias.alias for alias in parsed.find_all(exp.Alias) if alias.alias}
        if not columns:
            return
        try:
            tokens = sqlglot.tokenize(blanked)
        except Exception:
            return
        for token in tokens:
            if token.text in columns:
                self._record(
                    Reference(
                        path=path,
                        line=token.line,
                        form="select_column",
                        kind="column",
                        name=token.text,
                        package=package,
                        owner=node_name(path),
                    )
                )

    def _scan_seed_header(
        self, content: str, path: str, *, package: str | None
    ) -> None:
        """A seed's column names, from its header row and nowhere else.

        The rows below the header are project *data*, and raw data values never
        enter agent context. The header names columns, so it is the one line of a
        seed this index may read.
        """

        header = next(csv.reader(StringIO(content)), [])
        for column in header:
            if column.strip():
                self._record(
                    Reference(
                        path=path,
                        line=1,
                        form="seed_header",
                        kind="column",
                        name=column.strip(),
                        package=package,
                        owner=node_name(path),
                    )
                )

    def _scan_yaml(
        self,
        view: DbtProjectView,
        node_kinds: dict[str, str],
        *,
        package: str | None,
        prefix: str = "",
    ) -> None:
        for parsed, raw_path in yaml_documents(view):
            path = f"{prefix}{raw_path}"
            source = view.files[raw_path].content
            lines = _ScalarLines(source, self.limits, path)
            for entry in _dicts(parsed.get("models")):
                self._scan_model_entry(entry, path, lines, node_kinds, package=package)
            for entry in _dicts(parsed.get("seeds")) + _dicts(parsed.get("snapshots")):
                self._scan_model_entry(entry, path, lines, node_kinds, package=package)
            for source_entry in _dicts(parsed.get("sources")):
                self._scan_source_entry(source_entry, path, lines, package=package)

        for kind, entry, raw_path in semantic_yaml_entries(view):
            path = f"{prefix}{raw_path}"
            lines = _ScalarLines(view.files[raw_path].content, self.limits, path)
            if kind == "semantic_model":
                self._scan_semantic_model(entry, path, lines, package=package)
            else:
                self._scan_metric(entry, path, lines, package=package)

    def _scan_model_entry(
        self,
        entry: dict[str, Any],
        path: str,
        lines: _ScalarLines,
        node_kinds: dict[str, str],
        *,
        package: str | None,
    ) -> None:
        name = entry.get("name")
        if isinstance(name, str):
            self._record(
                Reference(
                    path=path,
                    line=lines.take(name),
                    form="yaml_model_entry",
                    kind=node_kinds.get(name, "model"),
                    name=name,
                    package=package,
                )
            )
        self._scan_columns_block(
            entry,
            path,
            lines,
            package=package,
            owner=name if isinstance(name, str) else None,
        )

    def _scan_source_entry(
        self,
        entry: dict[str, Any],
        path: str,
        lines: _ScalarLines,
        *,
        package: str | None,
    ) -> None:
        source_name = entry.get("name")
        for table in _dicts(entry.get("tables")):
            table_name = table.get("name")
            if isinstance(source_name, str) and isinstance(table_name, str):
                self._record(
                    Reference(
                        path=path,
                        line=lines.take(table_name),
                        form="definition",
                        kind="source",
                        name=f"{source_name}.{table_name}",
                        package=package,
                    )
                )
            self._scan_columns_block(
                table,
                path,
                lines,
                package=package,
                owner=f"{source_name}.{table_name}"
                if isinstance(source_name, str) and isinstance(table_name, str)
                else None,
            )

    def _scan_columns_block(
        self,
        entry: dict[str, Any],
        path: str,
        lines: _ScalarLines,
        *,
        package: str | None,
        owner: str | None = None,
    ) -> None:
        for column in _dicts(entry.get("columns")):
            name = column.get("name")
            if not isinstance(name, str):
                continue
            self._record(
                Reference(
                    path=path,
                    line=lines.take(name),
                    form="yaml_column",
                    kind="column",
                    name=name,
                    package=package,
                    owner=owner,
                )
            )
            for test in _tests_of(column):
                self._scan_test(test, path, lines, package=package, owner=owner)
        for test in _tests_of(entry):
            self._scan_test(test, path, lines, package=package, owner=owner)

    def _scan_test(
        self,
        test: Any,
        path: str,
        lines: _ScalarLines,
        *,
        package: str | None,
        owner: str | None = None,
    ) -> None:
        """One declared test: the generic test it invokes and the names it passes.

        A column named only here is the case a grep over model SQL misses
        entirely, which is why a test's arguments are read rather than skipped as
        configuration.

        The test's own name is a macro reference, because in dbt a generic test
        *is* a macro. A project that renames a custom generic test has to update
        every `schema.yml` invoking it, and those invocations appear nowhere else.
        """

        if isinstance(test, str):
            self._record(
                Reference(
                    path=path,
                    line=lines.take(test),
                    form="yaml_test_ref",
                    kind="macro",
                    name=test,
                    package=package,
                )
            )
            return
        if not isinstance(test, dict):
            return
        for test_name, body in test.items():
            self._record(
                Reference(
                    path=path,
                    line=lines.take(str(test_name)),
                    form="yaml_test_ref",
                    kind="macro",
                    name=str(test_name),
                    package=package,
                )
            )
            if not isinstance(body, dict):
                continue
            for key, value in body.items():
                if key == "to":
                    target = _relation_from(value)
                    if target is not None:
                        self._record(
                            Reference(
                                path=path,
                                line=lines.peek(str(value)),
                                form="yaml_relationship_to",
                                kind="source" if "." in target else "model",
                                name=target,
                                package=package,
                            )
                        )
                    continue
                for column in _column_names(key, value):
                    self._record(
                        Reference(
                            path=path,
                            line=lines.take(column),
                            form="yaml_test_column",
                            kind="column",
                            name=column,
                            package=package,
                            owner=owner,
                        )
                    )

    def _scan_semantic_model(
        self,
        entry: dict[str, Any],
        path: str,
        lines: _ScalarLines,
        *,
        package: str | None,
    ) -> None:
        target = _relation_from(entry.get("model"))
        if target is not None:
            self._record(
                Reference(
                    path=path,
                    line=lines.peek(str(entry.get("model"))),
                    form="semantic_model_ref",
                    kind="source" if "." in target else "model",
                    name=target,
                    package=package,
                )
            )
        for role, form in (
            ("entities", "semantic_entity_ref"),
            ("dimensions", "semantic_definition"),
            ("measures", "semantic_definition"),
        ):
            kind = {"entities": "entity", "dimensions": "dimension"}.get(
                role, "measure"
            )
            for item in _dicts(entry.get(role)):
                name = item.get("name")
                if not isinstance(name, str):
                    continue
                self._record(
                    Reference(
                        path=path,
                        line=lines.take(name),
                        form=form,
                        kind=kind,
                        name=name,
                        package=package,
                    )
                )
                # `physical_column` returns None for a computed expression on
                # purpose: guessing a column out of an expression would make
                # every reader over-claim. That None is *unknown*, not *absent*,
                # so it is an unresolved column reference rather than none.
                column = physical_column(item)
                self._record(
                    Reference(
                        path=path,
                        line=lines.peek(column) if column else lines.peek(name),
                        form="semantic_expr",
                        kind="column",
                        name=column,
                        resolution=RESOLVED if column else INDETERMINATE,
                        package=package,
                        owner=target,
                        note=None if column else f"computed expr on '{name}'",
                    )
                )

    def _scan_metric(
        self,
        entry: dict[str, Any],
        path: str,
        lines: _ScalarLines,
        *,
        package: str | None,
    ) -> None:
        name = entry.get("name")
        if isinstance(name, str):
            self._record(
                Reference(
                    path=path,
                    line=lines.take(name),
                    form="definition",
                    kind="metric",
                    name=name,
                    package=package,
                )
            )
        measures, metrics = metric_inputs(entry)
        for measure in measures:
            self._record(
                Reference(
                    path=path,
                    line=lines.peek(measure),
                    form="metric_input_measure",
                    kind="measure",
                    name=measure,
                    package=package,
                )
            )
        for metric in metrics:
            self._record(
                Reference(
                    path=path,
                    line=lines.peek(metric),
                    form="metric_input_metric",
                    kind="metric",
                    name=metric,
                    package=package,
                )
            )

    def _scan_project_vars(
        self, view: DbtProjectView, *, package: str | None, prefix: str = ""
    ) -> None:
        """Declared project variables, wherever ``dbt_project.yml`` declares them.

        A var can be declared at the project root or scoped under a package or a
        model path, and a caller removing one has to remove every declaration as
        well as every read.
        """

        source = view.files.get("dbt_project.yml")
        if source is None:
            return
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            self.limits.append("dbt_project.yml could not be parsed")
            return
        if not isinstance(parsed, dict):
            return
        path = f"{prefix}dbt_project.yml"
        lines = _ScalarLines(source.content, self.limits, path)
        for name in _var_names(parsed):
            self._record(
                Reference(
                    path=path,
                    line=lines.peek_key(name),
                    form="project_yml_var",
                    kind="var",
                    name=name,
                    package=package,
                )
            )

    def _scan_packages(self) -> None:
        """Index each installed package as the project it is.

        A package is a dbt project, so the same scan applies to it unchanged.
        Loading each one through the ordinary loader means a package that
        configures its own model paths is read the way dbt reads it, rather than
        the way a hard-coded ``models/`` guess would.
        """

        root = Path(self.view.root)
        declared = any(
            (root / name).is_file() for name in ("packages.yml", "dependencies.yml")
        )
        packages_dir = root / "dbt_packages"
        if declared and not packages_dir.is_dir():
            self.limits.append(
                "packages are declared but not installed, so package contents "
                "were not scanned; run `transform deps` first"
            )
            return
        if not packages_dir.is_dir():
            return
        for entry in sorted(packages_dir.iterdir()):
            if not (entry / "dbt_project.yml").is_file():
                continue
            try:
                self._scan(
                    load_project(entry),
                    package=entry.name,
                    prefix=f"dbt_packages/{entry.name}/",
                )
            except Exception:
                self.limits.append(f"package '{entry.name}' could not be read")


class _ScalarLines:
    """Where each scalar value sits in a YAML file.

    The parsed structure says *what* a document declares and carries no
    positions; the composed node tree carries positions and is painful to walk
    structurally. Reading both and pairing them by value is what lets a finding
    name a line without rewriting every YAML reader in the engine.

    Repeated values are handed out in document order, which matches the order the
    structural walk visits them closely enough to be right in practice. Where it
    drifts, the line still points at a real occurrence of that exact scalar in
    that file, which is what a caller opening the file needs.
    """

    def __init__(self, content: str, limits: list[str], path: str) -> None:
        self._pending: dict[str, deque[int]] = defaultdict(deque)
        self._all: dict[str, list[int]] = defaultdict(list)
        self._keys: dict[str, int] = {}
        try:
            root = yaml.compose(content)
        except yaml.YAMLError:
            limits.append(f"{path}: positions unavailable, the YAML did not compose")
            return
        self._walk(root, is_key=False)

    def _walk(self, node: Any, *, is_key: bool) -> None:
        if node is None:
            return
        if isinstance(node, yaml.ScalarNode):
            line = node.start_mark.line + 1
            self._pending[node.value].append(line)
            self._all[node.value].append(line)
            if is_key:
                self._keys.setdefault(node.value, line)
            return
        if isinstance(node, yaml.SequenceNode):
            for item in node.value:
                self._walk(item, is_key=False)
            return
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                self._walk(key, is_key=True)
                self._walk(value, is_key=False)

    def take(self, value: str) -> int:
        """The next unclaimed line carrying ``value``, or the first one again."""

        queue = self._pending.get(value)
        if queue:
            return queue.popleft()
        return self.peek(value)

    def peek(self, value: str | None) -> int:
        """A line carrying ``value`` without claiming it. ``1`` when unknown."""

        if value is None:
            return 1
        found = self._all.get(value)
        return found[0] if found else 1

    def peek_key(self, value: str) -> int:
        return self._keys.get(value, self.peek(value))


def _blank_jinja(content: str) -> str:
    """``content`` with every jinja region blanked, offsets and newlines intact.

    An expression leaves a short identifier behind so the surrounding SQL still
    parses where a relation or a value was interpolated; a statement leaves
    nothing, because `{% if %}` has no value and standing one in would invent a
    token the author did not write. Length is preserved either way, so a token's
    reported line and column are the file's.
    """

    regions, masked = jinja_regions(content)
    out = list(masked)
    for region in regions:
        for index in range(region.start, region.end):
            out[index] = "\n" if masked[index] == "\n" else " "
        if region.kind != "expression":
            continue
        spanning = next((c for c in region.calls if c.spans_region), None)
        if spanning is not None and spanning.callee == "config":
            # A config header is a statement about the node, not a value in the
            # query. Standing an identifier in its place puts a bare token above
            # the SELECT and nothing parses after it.
            continue
        for offset, char in enumerate("_dx"):
            if region.start + offset < region.end:
                out[region.start + offset] = char
    return "".join(out)


def _tests_of(entry: dict[str, Any]) -> list[Any]:
    """Declared tests under either key dbt accepts, `tests` and `data_tests`.

    Both, not the first one present: a project mid-migration between the two
    spellings carries some of each, and reading only one would drop the rest.
    """

    return [test for key in ("tests", "data_tests") for test in (entry.get(key) or [])]


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _relation_from(value: Any) -> str | None:
    """A ``ref()`` / ``source()`` string as a referable name.

    Reads the call rather than the text, so the two-argument
    ``ref('package', 'model')`` form resolves to the model. The regex this
    replaces captured the package instead, which is why a semantic model or a
    relationship test pointed at a packaged model resolved to the wrong name.
    """

    if not isinstance(value, str):
        return None
    for call in jinja_calls(value if "{{" in value else "{{ " + value + " }}"):
        if call.callee == "ref" and call.args and call.args[-1]:
            return call.args[-1]
        if call.callee == "source" and len(call.args) >= 2 and None not in call.args:
            return f"{call.args[-2]}.{call.args[-1]}"
    return None


def _column_names(key: str, value: Any) -> list[str]:
    """Column names carried by one test argument.

    dbt's generic tests name columns under a handful of keys and each shape shows
    up in real projects: a bare string, a list, and the `relationships` pair.
    Anything else is configuration and contributes no column.
    """

    if key not in {"field", "column_name", "combination_of_columns", "compare_columns"}:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _var_names(parsed: dict[str, Any]) -> list[str]:
    """Every variable name ``dbt_project.yml`` declares, at any scope."""

    found: list[str] = []

    def walk(node: Any, *, inside_vars: bool) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in ("vars", "+vars") and isinstance(value, dict):
                found.extend(str(name) for name in value)
                walk(value, inside_vars=True)
                continue
            if not inside_vars:
                walk(value, inside_vars=False)

    walk(parsed, inside_vars=False)
    return found


class ReferenceReport(BaseModel):
    """What one ``transform references`` call found, and how sure it is.

    ``completeness`` is ``complete`` only when every reason to doubt the answer
    has been ruled out, which is why it is computed from ``limits`` rather than
    set. A caller acts differently on "these are all the uses" than on "these are
    the uses dex could see", and the difference has to survive being read
    quickly.
    """

    completeness: str
    limits: list[str] = Field(default_factory=list)
    indeterminate: list[Reference] = Field(default_factory=list)
    targets: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


#: How much comes back before the payload is capped. A var read across two dozen
#: models is the ordinary case, not the pathological one, and an uncapped answer
#: to it is a payload nobody can read. `--full` lifts both; every elision is
#: counted in a note, because a truncated answer that reads as a complete one is
#: the failure this whole command exists to prevent.
MAX_OCCURRENCES = 200
MAX_FILES = 50


def find_references(
    view: DbtProjectView,
    names: list[str],
    *,
    kind: str | None = None,
    full: bool = False,
    index: ReferenceIndex | None = None,
) -> ReferenceReport:
    """Every reference to each of ``names``, with a completeness verdict.

    ``kind`` narrows; omitting it answers for every kind each name is used as,
    which is the common case because a caller asking "where is this used" usually
    knows the name and not what the project calls it.
    """

    index = index or ReferenceIndex(view)
    limits = list(index.limits)
    notes: list[str] = []
    targets: list[dict[str, Any]] = []
    indeterminate: list[Reference] = []
    budget = None if full else MAX_OCCURRENCES

    for name in names:
        kinds = [kind] if kind else index.kinds_of(name)
        if not kinds:
            targets.append({"name": name, "kinds": [], "found": False, "files": []})
            continue
        for one_kind in kinds:
            hits, query_limits = index.references_to(name, one_kind)
            limits.extend(query_limits)
            indeterminate.extend(index.indeterminate_for(one_kind))
            kept, elided_files, elided_hits, budget = _cap(hits, budget, full)
            targets.append(
                {
                    "name": name,
                    "kind": one_kind,
                    "found": bool(hits),
                    "scope": _scope_of(name, one_kind),
                    "occurrence_count": len(hits),
                    "files": kept,
                }
            )
            if elided_hits:
                notes.append(
                    f"{elided_hits} occurrence(s) of '{name}' as a {one_kind} "
                    f"across {elided_files} file(s) are not listed: the payload is "
                    f"capped at {MAX_OCCURRENCES} occurrences and {MAX_FILES} "
                    "files. Pass --full to list them all"
                )

    seen: set[tuple[str, int, str]] = set()
    unique_indeterminate = []
    for reference in indeterminate:
        key = (reference.path, reference.line, reference.form)
        if key not in seen:
            seen.add(key)
            unique_indeterminate.append(reference)
    if unique_indeterminate:
        limits.append(
            f"{len(unique_indeterminate)} reference(s) could not be resolved "
            "statically and are listed under `indeterminate`; each one may or may "
            "not name what you asked about"
        )

    # Deduplicated, order preserved: one limit per reason, however many names
    # ran into it. A caller reading "sqlglot is not installed" five times learns
    # nothing the first line did not already say.
    limits = list(dict.fromkeys(limits))
    return ReferenceReport(
        completeness="complete" if not limits else "incomplete",
        limits=limits,
        indeterminate=unique_indeterminate,
        targets=targets,
        notes=notes,
    )


def _scope_of(name: str, kind: str) -> str:
    if kind != "column":
        return "exact"
    return "lineage_resolved" if "." in name else "name_matched"


def _cap(
    hits: list[Reference], budget: int | None, full: bool
) -> tuple[list[dict[str, Any]], int, int, int | None]:
    """Group occurrences by file, honouring the shared occurrence budget.

    Grouped by file because that is the unit a caller acts on: they open a file
    and fix every use in it. Names are values inside these records and never
    become JSON keys, which matters because the sanitizer that guards the
    envelope matches key names against secret-like substrings and a column
    legitimately called `access_token` would take the whole command down.
    """

    by_file: dict[str, list[Reference]] = defaultdict(list)
    for hit in hits:
        by_file[hit.path].append(hit)

    files: list[dict[str, Any]] = []
    elided_files = 0
    elided_hits = 0
    for path in sorted(by_file):
        occurrences = by_file[path]
        exhausted = budget is not None and budget <= 0
        if not full and (len(files) >= MAX_FILES or exhausted):
            elided_files += 1
            elided_hits += len(occurrences)
            continue
        take = occurrences if full or budget is None else occurrences[:budget]
        if budget is not None:
            budget -= len(take)
        elided_hits += len(occurrences) - len(take)
        files.append(
            {
                "path": path,
                "package": occurrences[0].package,
                "occurrences": [
                    {
                        "line": occurrence.line,
                        "form": occurrence.form,
                        "resolution": occurrence.resolution,
                        "note": occurrence.note,
                    }
                    for occurrence in take
                ],
            }
        )
    return files, elided_files, elided_hits, budget


class ReferencesResult(Result):
    """Where a name is used, and how sure dex is that the list is complete.

    ``data()`` emits the verdict before the occurrences, and that order is the
    payload's whole point. A long answer is the ordinary case here, so it will be
    read from the top and sometimes truncated from the bottom; putting
    ``completeness`` and ``limits`` last would make the honesty the first thing
    lost.

    ``always_reports_notes`` because an empty ``notes`` is a positive statement:
    nothing was capped, so nothing is missing for that reason.
    """

    completeness: str = "incomplete"
    limits: list[str] = Field(default_factory=list)
    indeterminate: list[dict[str, Any]] = Field(default_factory=list)
    targets: list[dict[str, Any]] = Field(default_factory=list)

    always_reports_notes: ClassVar[bool] = True

    def data(self) -> dict[str, Any]:
        return {
            "completeness": self.completeness,
            "limits": self.limits,
            "indeterminate": self.indeterminate,
            "targets": self.targets,
        }


def run(
    engine: DexEngine,
    names: list[str],
    *,
    kind: str | None = None,
    full: bool = False,
) -> ReferencesResult:
    """``transform references``: where each of ``names`` is used.

    Repo-only and free on every connector. It reads the project's files, opens no
    connection, and needs no dialect engine, which is why the CLI routes it ahead
    of the gate the rest of the authoring surface passes through.
    """

    if kind is not None and kind not in KINDS:
        raise RequestError(
            f"'{kind}' is not a kind of thing a dbt project names. "
            f"Use one of: {', '.join(KINDS)}; or omit --kind to report every "
            "kind each name is used as"
        )
    report = find_references(
        load_project(engine.project_dir()), names, kind=kind, full=full
    )
    return ReferencesResult(
        completeness=report.completeness,
        limits=report.limits,
        indeterminate=[
            reference.model_dump(exclude_none=True)
            for reference in report.indeterminate
        ],
        targets=report.targets,
        notes=report.notes,
    )


def cmd_references(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    result = run(
        engine,
        list(args.names),
        kind=getattr(args, "kind", None),
        full=getattr(args, "full", False),
    )
    hints = (
        {"next": 'propose the edits with `transform plan "<intent>"`'}
        if any(target.get("found") for target in result.targets)
        else None
    )
    return to_envelope(result, hints=hints)
