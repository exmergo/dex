"""Per-kind validation of agent-authored edits, before they become a plan.

Model SQL must be a single read-only SELECT once its jinja is stripped (defense in
depth: the same guard the adapters apply to generated SQL). YAML edits must parse
to a mapping. Semantic YAML gets a further MetricFlow-shape check in
``semantic.py``; this module owns the checks common to every plan producer.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import sqlglot
import yaml
from sqlglot.errors import TokenError
from sqlglot.tokens import TokenType

from ..errors import DexError
from ..guards.sql_guard import assert_select_only

if TYPE_CHECKING:
    from ..dbt_project import DbtProjectView
    from .plans import PlanEdit


class EditValidationError(DexError):
    pass


# profiles.yml keys whose value dbt reads from the environment, never a literal
# in a committed file. A path-typed key (``private_key_path``) holds no secret,
# and method-based auth (externalbrowser/iam/oauth) carries none either, so both
# are exempt by not being listed. This mirrors init's "no persisted secret" rule
# and, at author time, keeps a credential out of the plan diff and thus out of
# agent context.
_SECRET_KEYS = frozenset(
    {
        "password",
        "pass",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "secret",
        "private_key",
        "private_key_passphrase",
    }
)
# The one safe form for a sensitive value: a dbt env_var() reference, resolved at
# runtime. Any other value for a sensitive key is a literal and is refused.
_ENV_VAR_REF = re.compile(r"\{\{\s*env_var\s*\(")


def find_inlined_secret(content: str) -> str | None:
    """The first profiles.yml key that inlines a literal secret, or ``None``.

    Walks the parsed mapping; a sensitive key is safe only when its value is an
    ``env_var()`` reference. A parse failure returns ``None`` here (the mapping
    check reports malformed YAML), so this never masks a structural error.
    """

    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    return _scan_for_secret(parsed)


def _scan_for_secret(node: object) -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            sensitive = isinstance(key, str) and key.lower() in _SECRET_KEYS
            if sensitive and not (
                isinstance(value, str) and _ENV_VAR_REF.search(value)
            ):
                return key
            hit = _scan_for_secret(value)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for item in node:
            hit = _scan_for_secret(item)
            if hit is not None:
                return hit
    return None


def assert_profiles_safe(view: DbtProjectView, edits: list[PlanEdit]) -> None:
    """Refuse a profiles.yml edit that inlines a literal credential, on either the
    current file (the diff's removed side) or the proposed content.

    Runs before the content reaches a diff or a dbt subprocess, so a credential
    never leaves the file. The message names the offending key, never its value.
    """

    from .plans import EditKind

    for edit in edits:
        if edit.kind is not EditKind.PROFILES_YML:
            continue
        current = view.files.get(edit.path)
        sides = (
            ("current", current.content if current is not None else None),
            ("proposed", edit.new_content),
        )
        for label, content in sides:
            if content is None:
                continue
            key = find_inlined_secret(content)
            if key is not None:
                raise EditValidationError(
                    f"{edit.path}: the {label} profiles.yml inlines a literal "
                    f"credential in '{key}'; reference it via "
                    "{{ env_var('NAME') }} so no credential enters the plan "
                    "diff or agent context"
                )


# Jinja expression / statement / comment blocks, non-greedy so adjacent blocks
# don't merge. Statements and comments vanish; expressions become a placeholder
# identifier so `from {{ ref('x') }}` stays parseable SQL.
_JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_STMT = re.compile(r"\{%.*?%\}", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_PLACEHOLDER = "__dex_jinja__"

# Macro definition delimiters, tolerant of whitespace-control markers. A macro
# file is jinja, not SQL, so its shape check is structural: definitions only,
# nothing loose between them. dbt's own parser is the authoritative gate.
_MACRO_OPEN = re.compile(r"\{%-?\s*macro\s+\w+\s*\(")
_MACRO_CLOSE = re.compile(r"\{%-?\s*endmacro\s*-?%\}")
_MACRO_BLOCK = re.compile(r"\{%-?\s*macro\s.*?\{%-?\s*endmacro\s*-?%\}", re.DOTALL)

# Generic test definitions, which are macros under another keyword and may live
# under the test paths as well as the macro paths. Their presence is what tells
# a `test_sql` edit apart from a singular test: same directory, two shapes, and
# only the file's own content says which one the caller wrote.
_TEST_OPEN = re.compile(r"\{%-?\s*test\s+\w+\s*\(")
_TEST_CLOSE = re.compile(r"\{%-?\s*endtest\s*-?%\}")
_TEST_BLOCK = re.compile(r"\{%-?\s*test\s.*?\{%-?\s*endtest\s*-?%\}", re.DOTALL)

# A test that names no table always passes, which is worse than no test at all,
# so a singular test with neither call is warned about rather than refused: a
# fixture-free assertion against a literal is unusual but not wrong.
_REF_OR_SOURCE = re.compile(r"\{\{-?\s*(?:ref|source)\s*\(")

# Snapshot delimiters, on the same principle as the macro ones. A snapshot file
# holds exactly one block: dbt names the snapshot after the block, not the file,
# and a second block in one file is a build-time surprise nobody reading the
# filename would expect.
_SNAPSHOT_OPEN = re.compile(r"\{%-?\s*snapshot\s+\w+\s*-?%\}")
_SNAPSHOT_CLOSE = re.compile(r"\{%-?\s*endsnapshot\s*-?%\}")
_SNAPSHOT_BLOCK = re.compile(
    r"\{%-?\s*snapshot\s.*?\{%-?\s*endsnapshot\s*-?%\}", re.DOTALL
)
_CONFIG_CALL = re.compile(r"\{\{-?\s*config\s*\(", re.DOTALL)
# The strategies dbt ships, and the field each one cannot work without: a
# timestamp strategy compares an updated-at column, a check strategy compares a
# named column list. Naming the required field in the refusal is the difference
# between "invalid snapshot" and a fix the caller can apply.
_SNAPSHOT_STRATEGY_FIELDS = {"timestamp": "updated_at", "check": "check_cols"}


def strip_jinja(sql: str) -> str:
    """Reduce a dbt model to plain SQL for the SELECT-only check.

    Inline expressions (``{{ ref(...) }}``) become an identifier placeholder;
    statement and comment blocks are removed. Before the query starts, a
    placeholder-only line is dropped at the top level (a ``{{ config(...) }}``
    header). Once the first SQL ``SELECT`` or ``WITH`` token has started the
    query, it is preserved as an identifier so a line-broken ``ref()`` remains
    parseable. Inside parentheses it becomes a placeholder subquery, because
    there it is a macro rendering a whole SELECT
    (``from ( {{ unpivot_json_object(...) }} )``), and dropping it would leave
    unparseable SQL. Depth counting is naive about parens inside string literals;
    a miscount only ever refuses, never admits.
    """

    text = _JINJA_COMMENT.sub("", sql)
    text = _JINJA_STMT.sub("", text)
    text = _JINJA_EXPR.sub(_PLACEHOLDER, text)
    # Token lines locate the query without mistaking keywords in comments or
    # strings for its start.
    try:
        query_start_line = next(
            (
                token.line
                for token in sqlglot.tokenize(text)
                if token.token_type in (TokenType.SELECT, TokenType.WITH)
            ),
            None,
        )
    except TokenError:
        query_start_line = None
    lines: list[str] = []
    depth = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.strip() == _PLACEHOLDER:
            if depth > 0:
                lines.append(line.replace(_PLACEHOLDER, f"select {_PLACEHOLDER}"))
            elif query_start_line is not None and line_number >= query_start_line:
                lines.append(line)
            continue
        depth += line.count("(") - line.count(")")
        lines.append(line)
    return "\n".join(lines).strip()


# A seed is data entering git, and it stays there. dbt itself warns against
# large seeds (it INSERTs them row by row), and a multi-megabyte CSV in a plan
# diff is a different kind of object from a twenty-row lookup: it is unreadable
# in review, which is the one thing the diff exists for. Module constants rather
# than config, the same call `explore`'s own caps make; a project that genuinely
# needs a bigger reference table wants a warehouse table, not a seed.
MAX_SEED_ROWS = 5_000
MAX_SEED_BYTES = 1 << 20


def assert_definitions_only(
    path: str,
    content: str,
    *,
    keyword: str,
    open_re: re.Pattern[str],
    close_re: re.Pattern[str],
    block_re: re.Pattern[str],
) -> None:
    """Refuse a jinja definition file that is not only definitions.

    Macros and generic tests are the same document shape under two keywords: a
    file of ``{% keyword name(...) %}`` blocks, balanced, with nothing loose
    between them but jinja comments. It is a structural check rather than a SQL
    one because the file is jinja, not SQL, and the body is a template that only
    becomes a query once dbt renders it against a model.

    Refusals name the delimiter at fault, so the caller reads what to write
    rather than which rule they broke.
    """

    opens = len(open_re.findall(content))
    closes = len(close_re.findall(content))
    if opens == 0:
        raise EditValidationError(
            f"{path}: a {keyword}_sql edit needs at least one "
            f"{{% {keyword} name(...) %}} definition"
        )
    if opens != closes:
        raise EditValidationError(
            f"{path}: unbalanced {keyword} definitions "
            f"({opens} {keyword}, {closes} end{keyword})"
        )
    outside = _JINJA_COMMENT.sub("", block_re.sub("", content))
    if outside.strip():
        raise EditValidationError(
            f"{path}: a {keyword} file holds only {keyword} definitions and "
            "jinja comments; found loose content outside them"
        )


def _assert_query_only(path: str, content: str, noun: str) -> list[str]:
    """Refuse SQL that is not a single read-only SELECT, once jinja is stripped.

    The check every authored query gets, whatever dbt will do with it: a model
    that materializes, a singular test dbt runs and counts the rows of, an
    analysis dbt only compiles. Read-only against data is a guarantee dex makes
    about its own writes, so it does not soften for the kinds dbt runs less
    often.

    ``noun`` names the kind in the one warning this raises: a file that is
    entirely jinja has no SQL left to check, which is legitimate for a macro-
    driven query and worth saying out loud rather than passing silently.
    """

    stripped = strip_jinja(content)
    if not stripped:
        return [f"{path}: {noun} is entirely jinja; SELECT-only check skipped"]
    try:
        assert_select_only(stripped)
    except Exception as exc:
        raise EditValidationError(f"{path}: {exc}") from exc
    return []


def validate_snapshot(path: str, content: str) -> None:
    """Refuse a snapshot file dbt could not build, naming the fix.

    Structural, in the same spirit as the macro check: exactly one
    ``{% snapshot %}`` block, a ``config()`` inside it carrying the fields the
    chosen strategy cannot work without, and a body that is a single read-only
    SELECT once its jinja is stripped. dbt's own parser is the authoritative
    gate (``transform plan`` runs it); this is the always-available one that
    still works on a machine where dbt is not installed, and it is the one that
    can say *which* field is missing.
    """

    opens = len(_SNAPSHOT_OPEN.findall(content))
    closes = len(_SNAPSHOT_CLOSE.findall(content))
    if opens == 0:
        raise EditValidationError(
            f"{path}: a snapshot_sql edit needs a "
            "{% snapshot name %} ... {% endsnapshot %} block"
        )
    if opens != closes:
        raise EditValidationError(
            f"{path}: unbalanced snapshot block "
            f"({opens} snapshot, {closes} endsnapshot)"
        )
    if opens > 1:
        raise EditValidationError(
            f"{path}: a snapshot file holds exactly one snapshot block, found "
            f"{opens}; dbt names the snapshot after the block, so put each one "
            "in its own file"
        )
    block = _SNAPSHOT_BLOCK.search(content)
    if block is None:
        raise EditValidationError(
            f"{path}: could not read the snapshot block; it must open with "
            "{% snapshot name %} and close with {% endsnapshot %}"
        )
    outside = _JINJA_COMMENT.sub("", content.replace(block.group(0), ""))
    if outside.strip():
        raise EditValidationError(
            f"{path}: a snapshot file holds one snapshot block and jinja "
            "comments; found loose content outside it"
        )

    inner = block.group(0)
    config_call = _CONFIG_CALL.search(inner)
    if config_call is None:
        raise EditValidationError(
            f"{path}: the snapshot block needs a {{{{ config(...) }}}} call "
            "naming unique_key and strategy"
        )
    config_args, config_end = _balanced_call(inner, config_call.end())
    if config_args is None:
        raise EditValidationError(
            f"{path}: the snapshot's config(...) call is never closed"
        )
    if not re.search(r"\bunique_key\s*=", config_args):
        raise EditValidationError(
            f"{path}: the snapshot's config needs a unique_key, the column "
            "dbt tracks each row by (e.g. unique_key='id')"
        )
    strategy = re.search(r"\bstrategy\s*=\s*['\"](\w+)['\"]", config_args)
    if strategy is None:
        raise EditValidationError(
            f"{path}: the snapshot's config needs a strategy of "
            + " or ".join(f"'{name}'" for name in sorted(_SNAPSHOT_STRATEGY_FIELDS))
        )
    name = strategy.group(1)
    required = _SNAPSHOT_STRATEGY_FIELDS.get(name)
    if required is None:
        raise EditValidationError(
            f"{path}: unknown snapshot strategy '{name}'; dbt ships "
            + " and ".join(f"'{s}'" for s in sorted(_SNAPSHOT_STRATEGY_FIELDS))
        )
    if not re.search(rf"\b{required}\s*=", config_args):
        raise EditValidationError(
            f"{path}: a '{name}' strategy snapshot needs {required} in its "
            "config ("
            + (
                "the column that changes when a row does"
                if name == "timestamp"
                else "the columns to compare, or 'all'"
            )
            + ")"
        )

    # Past the config call's own closing `}}` (whitespace-control marker and
    # all), so what is left is the query and nothing else.
    body = re.sub(r"^\s*-?\s*\}\}", "", inner[config_end:])
    body = _SNAPSHOT_CLOSE.sub("", body)
    stripped = strip_jinja(body)
    if not stripped:
        raise EditValidationError(
            f"{path}: the snapshot block has no query; it needs a SELECT over "
            "the source it captures"
        )
    try:
        assert_select_only(stripped)
    except Exception as exc:
        raise EditValidationError(f"{path}: {exc}") from exc


def _balanced_call(text: str, start: int) -> tuple[str | None, int]:
    """The argument text of a call whose opening paren was just consumed, and
    the offset just past its closing paren.

    Naive about parens inside string literals, which only ever costs a refusal
    on a snapshot nobody writes (a quoted paren in a config argument), never an
    admission.
    """

    depth = 1
    for offset in range(start, len(text)):
        if text[offset] == "(":
            depth += 1
        elif text[offset] == ")":
            depth -= 1
            if depth == 0:
                return text[start:offset], offset + 1
    return None, len(text)


def validate_seed(
    path: str,
    content: str,
    *,
    cache: object | None = None,
    pii_overrides: object | None = None,
) -> list[str]:
    """Refuse a seed dbt could not load, one too large to review, or one whose
    columns look like PII. Returns warnings.

    The PII gate is the part that is new in kind rather than in degree. Every
    other edit kind puts *logic* into a reviewable diff; a seed puts **values**
    into one, and a diff goes into git and stays there. So a seed's header is
    read two ways: through the same name-and-type detector `explore` profiles
    warehouse columns with, and against the PII flags already in the `.dex/`
    cache, which is where a column a human reviewed and cleared has already
    stopped being flagged. A hit at or above the block threshold refuses; a
    weaker one, and the detector's own provisional generic-name match, warns.

    **What this cannot see:** dex detects PII from names and types, never from
    values, everywhere. A seed column named ``code`` holding email addresses
    passes this gate. That is the standing limit of the whole PII subsystem
    rather than a gap in this check, and value inspection is a separate axis.
    """

    import csv
    import io

    size = len(content.encode("utf-8"))
    if size > MAX_SEED_BYTES:
        raise EditValidationError(
            f"{path}: seed is {size / 1024:.0f} KiB, over the "
            f"{MAX_SEED_BYTES // 1024} KiB cap; a seed that large is data, not "
            "a lookup: load it into the warehouse and source() it instead"
        )

    try:
        rows = list(csv.reader(io.StringIO(content)))
    except csv.Error as exc:
        raise EditValidationError(f"{path}: could not parse as CSV: {exc}") from exc
    if not rows:
        raise EditValidationError(
            f"{path}: a seed needs a header row naming its columns"
        )
    header = [column.strip() for column in rows[0]]
    if not header or not any(header):
        raise EditValidationError(
            f"{path}: the header row is empty; a seed's first row names its columns"
        )
    for index, column in enumerate(header, start=1):
        if not column:
            raise EditValidationError(
                f"{path}: header column {index} has no name; dbt needs every "
                "seed column named"
            )
    seen: dict[str, int] = {}
    for index, column in enumerate(header, start=1):
        # Case-insensitively: most warehouses fold identifier case, so two
        # headers differing only in case collide once the seed is loaded.
        lowered = column.lower()
        if lowered in seen:
            raise EditValidationError(
                f"{path}: duplicate column '{column}' at header columns "
                f"{seen[lowered]} and {index}; seed columns must be unique "
                "(case-insensitively, since warehouses fold identifier case)"
            )
        seen[lowered] = index

    data_rows = rows[1:]
    if len(data_rows) > MAX_SEED_ROWS:
        raise EditValidationError(
            f"{path}: seed has {len(data_rows)} data rows, over the "
            f"{MAX_SEED_ROWS} row cap; a seed that long is data, not a lookup: "
            "load it into the warehouse and source() it instead"
        )
    for number, row in enumerate(data_rows, start=2):
        if len(row) != len(header):
            raise EditValidationError(
                f"{path}: row {number} breaks at column "
                f"{min(len(row), len(header)) + 1}: it has {len(row)} "
                f"{'field' if len(row) == 1 else 'fields'} where the header "
                f"names {len(header)}; every row needs one field per column"
            )

    return _seed_pii_warnings(path, header, cache=cache, pii_overrides=pii_overrides)


def _seed_pii_warnings(
    path: str,
    header: list[str],
    *,
    cache: object | None,
    pii_overrides: object | None,
) -> list[str]:
    """The two detections, unioned at the strongest confidence per column.

    The header detector is what catches a seed built by hand; the cache lookup
    is what catches a seed built from a warehouse column a profile already
    flagged, including one whose name the author changed on the way in only in
    case. Neither reads a value.
    """

    from ..cache import PIIFlag
    from ..explore.profile import classify_pii
    from ..guards import PII_BLOCK_CONFIDENCE

    seed = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    cached: dict[str, PIIFlag] = {}
    for dataset in getattr(cache, "datasets", None) or []:
        for column in dataset.columns:
            if column.pii is None:
                continue
            known = cached.get(column.name.lower())
            if known is None or column.pii.confidence > known.confidence:
                cached[column.name.lower()] = column.pii

    blocked: list[tuple[str, PIIFlag]] = []
    warned: list[tuple[str, PIIFlag, str]] = []
    for column in header:
        # A seed column has no warehouse identifier, so the override path is the
        # dbt name of the thing being authored: `<seed>.<column>`, which is what
        # the suggested entry below spells out.
        if pii_overrides is not None and f"{seed}.{column}" in pii_overrides:
            continue
        # A seed's values are text on the way in, so the type-gated detectors
        # are asked about the only type a CSV column can have.
        detected, provisional = classify_pii(column, "varchar")
        from_cache = cached.get(column.lower())
        # The cache's flag has already been through value-shape refinement on a
        # real profile, so it is a verdict wherever it exists and outranks the
        # header guess at equal confidence.
        candidates = [
            (from_cache, False, "a profile already flagged this column name"),
            (detected, provisional, "the column name matches a PII pattern"),
        ]
        strongest = max(
            (c for c in candidates if c[0] is not None),
            key=lambda c: c[0].confidence,
            default=None,
        )
        if strongest is None:
            continue
        flag, unrefined, why = strongest
        if unrefined:
            # The generic `*_name` match is the one flag the detector itself
            # calls provisional: on a warehouse column `explore` refines it
            # against value shape, up or down. dex never reads a seed's values,
            # so that refinement cannot run here, and blocking on an
            # unrefined guess would claim a certainty the detector disclaims.
            # It warns instead, loudly, and every non-generic pattern (email,
            # phone, national id, address) still blocks.
            warned.append((column, flag, "unrefined"))
        elif flag.confidence >= PII_BLOCK_CONFIDENCE:
            blocked.append((column, flag))
        else:
            warned.append((column, flag, why))

    if blocked:
        detail = "; ".join(
            f"'{column}' looks like {flag.category.value} "
            f"(confidence {flag.confidence:g}), cleared by "
            f"`- {{column: {seed}.{column}}}`"
            for column, flag in blocked
        )
        raise EditValidationError(
            f"{path}: a seed's values land in git and stay there, and this one "
            f"has columns that look like personal data: {detail}. If the review "
            "says otherwise, add that entry under pii_overrides in "
            ".dex/config.yml and re-plan. Only names and types were read; no "
            "value was."
        )

    warnings: list[str] = []
    for column, flag, why in warned:
        if why == "unrefined":
            warnings.append(
                f"{path}: '{column}' matches the generic name pattern, which "
                "dex refines against values on a warehouse column and cannot "
                "refine on a seed (it never reads a seed's values); if these "
                "are people's names, do not commit them"
            )
        else:
            warnings.append(
                f"{path}: '{column}' may hold {flag.category.value} "
                f"(confidence {flag.confidence:g}, under the "
                f"{PII_BLOCK_CONFIDENCE:g} block threshold, {why}); a seed's "
                "values are committed, so check this column before applying"
            )
    return warnings


def validate_edit(
    edit: PlanEdit,
    *,
    cache: object | None = None,
    pii_overrides: object | None = None,
) -> list[str]:
    """Validate one edit for its kind. Returns warnings; raises on a hard failure.

    ``cache`` and ``pii_overrides`` are the exploration cache and the reviewed
    columns a human has cleared, and only the seed gate reads them. They are
    optional because most callers have no seed in hand, and a seed validated
    without a cache still gets the header detector: the cache widens the check,
    it is not what makes it run.
    """

    from .plans import EditKind

    warnings: list[str] = []
    if edit.kind is EditKind.MACRO_SQL:
        assert_definitions_only(
            edit.path,
            edit.new_content,
            keyword="macro",
            open_re=_MACRO_OPEN,
            close_re=_MACRO_CLOSE,
            block_re=_MACRO_BLOCK,
        )
    elif edit.kind is EditKind.TEST_SQL:
        # Two shapes share the test paths and only the content tells them apart.
        # A generic test is a macro under another keyword, so it gets the
        # structural check; anything else is a singular test, which is a query
        # and gets a model's. Reading the close delimiter too means an unclosed
        # or orphaned block is refused as the broken definition it is rather
        # than as a query that fails to parse.
        if _TEST_OPEN.search(edit.new_content) or _TEST_CLOSE.search(edit.new_content):
            assert_definitions_only(
                edit.path,
                edit.new_content,
                keyword="test",
                open_re=_TEST_OPEN,
                close_re=_TEST_CLOSE,
                block_re=_TEST_BLOCK,
            )
        else:
            unreadable = _assert_query_only(edit.path, edit.new_content, "test")
            warnings.extend(unreadable)
            # Only when the query was readable. A test that is entirely jinja
            # builds itself inside a macro dex does not follow, so "names no
            # ref()" would be a guess about content the warning above has just
            # said could not be read.
            if not unreadable and not _REF_OR_SOURCE.search(edit.new_content):
                warnings.append(
                    f"{edit.path}: this test names no ref() or source(), so it "
                    "runs against nothing and passes unconditionally"
                )
    elif edit.kind is EditKind.ANALYSIS_SQL:
        # An analysis is compiled and never run, so dbt asks less of it than of
        # a model. dex does not: the SELECT-only guard is a safety guarantee,
        # not a dbt requirement, and a DELETE sitting in analyses/ is one
        # copy-paste away from being run by hand.
        warnings.extend(_assert_query_only(edit.path, edit.new_content, "analysis"))
    elif edit.kind is EditKind.SNAPSHOT_SQL:
        validate_snapshot(edit.path, edit.new_content)
    elif edit.kind is EditKind.SEED_CSV:
        # Ahead of the YAML branch below, which would otherwise report a CSV as
        # invalid YAML, and ahead of anything that builds a diff: the refusal
        # this can raise is the one thing standing between a seed's values and
        # the transcript, since the envelope sanitizer walks `data` and never
        # `diffs`.
        warnings.extend(
            validate_seed(
                edit.path,
                edit.new_content,
                cache=cache,
                pii_overrides=pii_overrides,
            )
        )
    elif edit.kind is EditKind.MODEL_SQL:
        warnings.extend(_assert_query_only(edit.path, edit.new_content, "model"))
    else:
        try:
            parsed = yaml.safe_load(edit.new_content)
        except yaml.YAMLError as exc:
            raise EditValidationError(f"{edit.path}: invalid YAML: {exc}") from exc
        if not isinstance(parsed, dict):
            raise EditValidationError(
                f"{edit.path}: expected a YAML mapping, got "
                f"{type(parsed).__name__ if parsed is not None else 'nothing'}"
            )
        if edit.kind is EditKind.SEMANTIC_YML:
            from .semantic import validate_semantic_yaml

            warnings.extend(validate_semantic_yaml(edit.path, parsed))
        elif edit.kind is EditKind.PACKAGES_YML and not (
            parsed.get("packages") or parsed.get("dependencies")
        ):
            raise EditValidationError(
                f"{edit.path}: a packages manifest needs a 'packages:' (or "
                "'dependencies:') list"
            )
        elif edit.kind is EditKind.PROJECT_YML and not parsed.get("name"):
            # dbt keys the project on 'name'; without it dbt cannot load the
            # project at all. Structural gate before the authoritative dbt parse.
            raise EditValidationError(
                f"{edit.path}: dbt_project.yml must declare a 'name'"
            )
        elif edit.kind is EditKind.PROFILES_YML:
            secret_key = find_inlined_secret(edit.new_content)
            if secret_key is not None:
                raise EditValidationError(
                    f"{edit.path}: '{secret_key}' inlines a literal credential; a "
                    "profiles.yml edit must reference secrets via "
                    "{{ env_var('NAME') }} so no credential enters the plan diff"
                )
    return warnings
