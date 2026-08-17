"""Unit tests for the skill wrapper (`skills/*/scripts/run.py`).

The wrapper is a stdlib-only PEP 723 script that runs before the engine is
installed and decides which `exmergo-dex-core[<extra>]` to install. These tests
guard the decoupling of the version pin from the connector extra: the version is
pinned, the extra is resolved at runtime from the active connector. They also guard
the missing-`uv` refusal, which is the one refusal the wrapper has to build itself
because it happens before the engine (and so `exmergo_dex_core.envelope`) exists.
The script is not importable as a package module, so it is loaded via importlib (its
`if __name__ == "__main__"` guard means importing does not run `main()`).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SKILLS = ("explore", "transform", "maintain")


def _wrapper_path(skill: str) -> Path:
    return _REPO / "skills" / skill / "scripts" / "run.py"


def _load(skill: str = "explore"):
    path = _wrapper_path(skill)
    spec = importlib.util.spec_from_file_location(f"dex_run_{skill}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def wrapper():
    return _load()


def test_all_three_wrappers_are_byte_identical():
    # The wrappers are copies by design; a per-skill edit that drifts one of them
    # (or a release sed that misses one) must fail loudly here.
    contents = {s: _wrapper_path(s).read_bytes() for s in _SKILLS}
    baseline = contents["explore"]
    for skill, data in contents.items():
        assert data == baseline, f"{skill}/scripts/run.py drifted from explore"


def test_pin_carries_no_extra(wrapper):
    # The whole point of this change: the version pin must be connector-neutral.
    version = wrapper.DEX_CORE_VERSION
    assert "[" not in version and "]" not in version
    assert "@" not in version
    assert version.strip() == version and version


# --- the missing-uv refusal --------------------------------------------------
#
# Parametrized over all three wrappers rather than resting on the byte-identity
# test above: this guard is exactly the kind of thing a refactor drops from one
# copy, and proving the behavior in each one costs nothing.


def _run_without_uv(skill, monkeypatch, argv):
    """Drive `main()` on a machine where uv is absent, and return (code, stdout).

    `subprocess.call` is replaced with a raiser rather than a stub, so a guard
    that let execution through fails loudly here instead of quietly shelling out
    to a `uv` that is not there.
    """

    module = _load(skill)
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        module.subprocess,
        "call",
        lambda *a, **k: pytest.fail("the guard let execution reach uv"),
    )
    monkeypatch.setattr(module.sys, "argv", ["run.py", *argv])
    code = module.main()
    return code, module


@pytest.mark.parametrize("skill", _SKILLS)
def test_missing_uv_refuses_with_one_error_envelope(skill, monkeypatch, capsys):
    code, _module = _run_without_uv(skill, monkeypatch, ["explore", "inventory"])
    assert code == 1  # the engine's own exit code for an error envelope

    out = capsys.readouterr().out
    envelope = json.loads(out)  # exactly one object, or this raises
    assert envelope["status"] == "error"
    assert envelope["reason"] == "prerequisite"
    assert set(envelope) == {
        "status",
        "data",
        "cost",
        "warnings",
        "diffs",
        "errors",
        "reason",
    }
    assert envelope["cost"] == {"paradigm": None, "estimate": None, "ceiling": None}
    assert envelope["data"] == {}


@pytest.mark.parametrize("skill", _SKILLS)
def test_missing_uv_message_names_uv_and_how_to_install_it(skill, monkeypatch, capsys):
    # The whole point of the refusal: a user who is not a Python developer has to
    # be able to act on it without reading our source.
    _run_without_uv(skill, monkeypatch, ["connect", "test"])
    message = json.loads(capsys.readouterr().out)["errors"][0]
    assert "uv" in message
    assert "astral.sh/uv/install.sh" in message


def test_uv_present_still_execs_the_engine(monkeypatch, capsys):
    # The guard must not fire on the ordinary path, and must not print anything
    # of its own onto the single-envelope stdout the engine owns.
    module = _load()
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/local/bin/uv")
    seen: list[list[str]] = []
    monkeypatch.setattr(
        module.subprocess, "call", lambda cmd, **_kwargs: seen.append(cmd) or 0
    )
    monkeypatch.setattr(module.sys, "argv", ["run.py", "explore", "inventory"])

    assert module.main() == 0
    assert capsys.readouterr().out == ""
    assert seen and seen[0][:2] == ["uv", "run"]
    assert seen[0][-2:] == ["explore", "inventory"]


def test_refusal_matches_the_engines_envelope_shape(monkeypatch, capsys):
    """The hand-built envelope must stay in step with the one the engine emits.

    Skipped where the engine is not installed, which includes CI's
    `uvx pytest evals -q` job: this is a local-dev and synced-environment guard
    against the two shapes drifting apart, not a gate.
    """

    envelope_module = pytest.importorskip("exmergo_dex_core.envelope")

    _run_without_uv("explore", monkeypatch, ["explore", "inventory"])
    refusal = json.loads(capsys.readouterr().out)
    engine_built = envelope_module.Envelope(
        status=envelope_module.Status.ERROR,
        errors=refusal["errors"],
        reason=envelope_module.Reason.PREREQUISITE,
    ).model_dump(mode="json")
    assert refusal == engine_built


# --- connector resolution (mirrors the engine's flag > config > duckdb order) ---


def test_explicit_flag_beats_config(wrapper, tmp_path):
    _write_config(tmp_path, "connector: bigquery")
    assert (
        wrapper._resolve_connector(["--connector", "snowflake"], tmp_path)
        == "snowflake"
    )


def test_flag_equals_form(wrapper, tmp_path):
    assert wrapper._resolve_connector(["--connector=bigquery"], tmp_path) == "bigquery"


def test_config_used_when_no_flag(wrapper, tmp_path):
    _write_config(tmp_path, "connector: postgres")
    assert wrapper._resolve_connector(["explore", "inventory"], tmp_path) == "postgres"


def test_repo_root_flag_locates_config(wrapper, tmp_path):
    sub = tmp_path / "project"
    _write_config(sub, "connector: databricks")
    assert (
        wrapper._resolve_connector(["--repo-root", "project"], tmp_path) == "databricks"
    )


def test_defaults_to_duckdb_when_nothing_set(wrapper, tmp_path):
    assert wrapper._resolve_connector(["connect", "test"], tmp_path) == "duckdb"


def test_unknown_connector_falls_back_to_duckdb(wrapper, tmp_path):
    # A bad guess must not produce a bogus extra; installing duckdb lets the engine
    # emit its canonical "unknown connector" error instead.
    assert wrapper._resolve_connector(["--connector", "oracle"], tmp_path) == "duckdb"


def test_redshift_resolves_to_its_own_extra(wrapper, tmp_path):
    assert (
        wrapper._resolve_connector(["--connector", "redshift"], tmp_path) == "redshift"
    )


def test_resolution_does_not_consume_forwarded_args(wrapper, tmp_path):
    # parse_known_args must tolerate the engine's own flags/positionals unharmed.
    argv = ["explore", "profile", "db.s.t", "--path", "x.duckdb", "--budget", "5"]
    assert wrapper._resolve_connector(argv, tmp_path) == "duckdb"


# --- the minimal config scan ---


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("connector: snowflake\n", "snowflake"),
        ('connector: "snowflake"\n', "snowflake"),
        ("connector: snowflake  # the warehouse\n", "snowflake"),
        ("profile_top_n: 25\nconnector: bigquery\n", "bigquery"),
        ("connector:\n", None),  # empty value
        ("  connector: snowflake\n", None),  # indented => not a top-level key
        ("dbt_target: dev\n", None),  # no connector key
    ],
)
def test_connector_from_config_variants(wrapper, tmp_path, body, expected):
    path = _write_config(tmp_path, body)
    assert wrapper._connector_from_config(path) == expected


def test_connector_from_config_missing_file(wrapper, tmp_path):
    assert wrapper._connector_from_config(tmp_path / ".dex" / "config.yml") is None


# --- the uv --with spec ---


def test_engine_spec_local_monorepo_path(wrapper):
    # In this checkout the real packages/dex-core resolves, so the spec is the
    # local path form, carrying the connector extra.
    spec = wrapper._engine_spec("snowflake")
    assert spec[0] == "--with"
    assert spec[1].startswith("exmergo-dex-core[snowflake] @ file://")
    assert spec[1].endswith("packages/dex-core")


def test_engine_spec_pinned_release_when_no_local_pkg(wrapper, tmp_path):
    # A skill_dir with no packages/dex-core above it forces the published-release
    # form: version pinned, extra chosen from the connector.
    spec = wrapper._engine_spec("bigquery", skill_dir=tmp_path)
    assert spec == ["--with", f"exmergo-dex-core[bigquery]=={wrapper.DEX_CORE_VERSION}"]


def _write_config(repo_root: Path, body: str) -> Path:
    dex_dir = repo_root / ".dex"
    dex_dir.mkdir(parents=True, exist_ok=True)
    path = dex_dir / "config.yml"
    path.write_text(body, encoding="utf-8")
    return path
