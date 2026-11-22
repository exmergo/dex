"""A rendered built-in on a column marked as personal data stores no failures (#1463).

The built-ins stay dbt's own `unique`, `not_null` and `relationships`; on a
column marked as personal data each one a plan adds also carries
`config.store_failures: false`, so a failure's values are not copied into an
audit table. A test the column listed before the plan is kept as written.
"""

from __future__ import annotations

import yaml

from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
from exmergo_dex_core.transform.declarations import (
    keep_no_failures_on_personal_columns,
    parse_declarations,
    render,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.rewrite import (
    _unsampled,
    column_tests,
    column_tests_span,
    is_unsampled_render,
    splice,
)
from exmergo_dex_core.transform.scaffold import _model_yaml

PATH = "models/marts/_marts.yml"

PERSONAL = """version: 2

models:
  - name: dim_users
    columns:
      - name: email
        config:
          meta:
            contains_pii: true
            pii_category: email
      - name: team_id
"""

DECLARATION = {
    "model": "dim_users",
    "grain": ["email"],
    "columns": {
        "team_id": {"references": "dim_teams.team_id", "null_rule": "never"},
    },
}


def _view(files: dict[str, str]) -> DbtProjectView:
    return DbtProjectView(
        root=".",
        project_name="p",
        profile_name="p",
        files={
            path: SourceFile(path=path, content=content, sha256="x")
            for path, content in files.items()
        },
    )


def _harden(prior: str | None, rendered: str) -> str:
    view = _view({} if prior is None else {PATH: prior})
    edit = PlanEdit(path=PATH, kind=EditKind.SCHEMA_YML, new_content=rendered)
    [out] = keep_no_failures_on_personal_columns(view, [edit])
    return out.new_content


def _columns(text: str, model: str = "dim_users") -> dict[str, dict]:
    entry = next(m for m in yaml.safe_load(text)["models"] if m["name"] == model)
    return {c["name"]: c for c in entry["columns"]}


def _config(test: dict) -> dict:
    [body] = test.values()
    return body["config"]


def test_the_built_ins_on_a_personal_column_store_no_failures():
    [declaration] = parse_declarations([DECLARATION])

    out = _harden(PERSONAL, render(PERSONAL, declaration))

    columns = _columns(out)
    email = columns["email"]["data_tests"]
    assert [next(iter(t)) for t in email] == ["unique", "not_null"]
    for test in email:
        config = _config(test)
        assert config["store_failures"] is False
        # The sampling flag and the claim stay beside it.
        assert config["meta"]["disable_test_samples"] is True
        assert config["meta"]["dex"]["claim"]
    # A column not marked as personal data keeps dbt's default.
    for test in columns["team_id"]["data_tests"]:
        assert "store_failures" not in _config(test)


def test_a_test_the_column_listed_before_the_plan_is_kept_as_written():
    [declaration] = parse_declarations([DECLARATION])
    earlier = render(PERSONAL, declaration)

    # Declared again: the listed tests are the earlier render, untouched.
    out = _harden(earlier, render(earlier, declaration))

    assert out == earlier


def test_a_mark_the_same_plan_writes_counts():
    [declaration] = parse_declarations([DECLARATION])
    unmarked = PERSONAL.replace(
        "        config:\n          meta:\n            contains_pii: true\n"
        "            pii_category: email\n",
        "",
    )
    rendered = render(unmarked, declaration)
    # The PII stamp (#1100) lands in the same file after the declaration.
    stamped = rendered.replace(
        "      - name: email\n",
        "      - name: email\n        config:\n          meta:\n"
        "            contains_pii: true\n",
    )

    out = _harden(unmarked, stamped)

    for test in _columns(out)["email"]["data_tests"]:
        assert _config(test)["store_failures"] is False


def test_a_flow_style_list_gains_the_key_on_its_own_item():
    prior = PERSONAL.replace(
        "            pii_category: email\n",
        "            pii_category: email\n        tests: [not_null]   # keep me\n",
    )
    [declaration] = parse_declarations([{"model": "dim_users", "grain": ["email"]}])

    out = _harden(prior, render(prior, declaration))

    assert "   # keep me\n" in out
    tests = _columns(out)["email"]["tests"]
    # The listed `not_null` is someone's and stays bare; the added `unique` is
    # hardened.
    assert tests[0] == "not_null"
    assert _config(tests[1])["store_failures"] is False


def test_someone_s_own_test_is_not_touched():
    hand = PERSONAL.replace(
        "            pii_category: email\n",
        "            pii_category: email\n"
        "        data_tests:\n"
        "          - unique:\n"
        "              config:\n"
        "                severity: warn\n",
    )

    assert _harden(PERSONAL, hand) == hand


def test_a_version_s_own_column_is_hardened():
    prior = """version: 2

models:
  - name: dim_users
    latest_version: 2
    versions:
      - v: 2
        columns:
          - name: email
            config:
              meta:
                contains_pii: true
"""
    [declaration] = parse_declarations([{"model": "dim_users.v2", "grain": ["email"]}])

    out = _harden(prior, render(prior, declaration))

    [version] = yaml.safe_load(out)["models"][0]["versions"]
    [email] = version["columns"]
    for test in email["data_tests"]:
        assert _config(test)["store_failures"] is False


def test_the_hardened_render_is_still_dex_s_to_move():
    assert is_unsampled_render(_unsampled("unique", personal=True), "unique")
    assert is_unsampled_render(
        _unsampled("not_null", claim="abc", personal=True), "not_null"
    )
    assert not is_unsampled_render(
        {"unique": {"config": {"store_failures": True, "meta": {}}}}, "unique"
    )


def test_reconcile_adds_a_hardened_test_to_a_personal_column():
    current = column_tests(PERSONAL, "dim_users", "email")
    assert current is not None and current.personal

    span = column_tests_span(PERSONAL, current, ["not_null"], key="data_tests")

    assert span is not None
    out = splice(PERSONAL, [span])
    [test] = _columns(out)["email"]["data_tests"]
    assert _config(test)["store_failures"] is False

    plain = column_tests(PERSONAL, "dim_users", "team_id")
    assert plain is not None and not plain.personal
    span = column_tests_span(PERSONAL, plain, ["not_null"], key="data_tests")
    [test] = _columns(splice(PERSONAL, [span]))["team_id"]["data_tests"]
    assert "store_failures" not in _config(test)


def test_the_scaffold_hardens_a_personal_key():
    from exmergo_dex_core.cache import ColumnProfile, Dataset, PIICategory, PIIFlag

    dataset = Dataset(
        identifier="raw.users",
        candidate_keys=[["email"]],
        columns=[
            ColumnProfile(
                name="email",
                data_type="varchar",
                null_fraction=0.0,
                pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.99),
            ),
            ColumnProfile(name="plan", data_type="varchar", null_fraction=0.0),
        ],
    )

    columns = _columns(_model_yaml(dataset), "stg_users")

    for test in columns["email"]["data_tests"]:
        assert _config(test)["store_failures"] is False
    [test] = columns["plan"]["data_tests"]
    assert "store_failures" not in _config(test)
