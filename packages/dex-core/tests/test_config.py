"""`.dex/config.yml` round-trips: what a user commits is what the engine reads."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.config import (
    BlobOverride,
    CacheConfig,
    ConventionWarnings,
    DexConfig,
    PIIOverride,
    SemanticConfig,
    blob_override_paths,
    load_config,
    pii_override_paths,
    save_config,
)
from exmergo_dex_core.config_document import ConfigDocument, InvalidConfigError
from exmergo_dex_core.errors import ConfigurationError


def test_pii_overrides_round_trip(tmp_path: Path):
    config = DexConfig(
        pii_overrides=[
            PIIOverride(
                column="MY_DB.PUBLIC.REGION.R_NAME",
                reason="TPC-H region labels; reviewed",
            ),
            PIIOverride(column="my_db.public.part.p_name"),
        ]
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert [e.column for e in loaded.pii_overrides] == [
        "MY_DB.PUBLIC.REGION.R_NAME",
        "my_db.public.part.p_name",
    ]
    assert loaded.pii_overrides[0].reason == "TPC-H region labels; reviewed"
    assert loaded.pii_overrides[1].reason is None


def test_override_paths_are_case_insensitive():
    paths = pii_override_paths([PIIOverride(column="  MY_DB.PUBLIC.REGION.R_NAME ")])
    assert "my_db.public.region.r_name" in paths


def test_pii_override_pattern_round_trip(tmp_path: Path):
    config = DexConfig(
        pii_overrides=[
            PIIOverride(
                column_name="document_name",
                scope="proj.raw_*",
                reason="Firestore resource path, not a person name",
            ),
        ]
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    (entry,) = loaded.pii_overrides
    assert entry.column is None
    assert entry.column_name == "document_name"
    assert entry.scope == "proj.raw_*"
    text = (tmp_path / ".dex" / "config.yml").read_text()
    assert "column:" not in text


@pytest.mark.parametrize(
    "kwargs",
    [
        # both shapes at once
        {"column": "db.t.c", "column_name": "c", "scope": "db.*"},
        # neither shape
        {},
        # pattern half-given
        {"column_name": "c"},
        {"scope": "db.*"},
    ],
)
def test_pii_override_rejects_invalid_shapes(kwargs):
    with pytest.raises(ValueError):
        PIIOverride(**kwargs)


def test_pii_override_pattern_matches_across_scope():
    paths = pii_override_paths(
        [PIIOverride(column_name="document_name", scope="db.raw_*")]
    )
    assert "db.raw_dev.t1.document_name" in paths
    assert "db.raw_qa.t2.document_name" in paths
    assert "db.other.t1.document_name" not in paths
    # a different column name on a matching table is untouched
    assert "db.raw_dev.t1.other_column" not in paths


def test_pii_override_pattern_matching_is_case_insensitive():
    paths = pii_override_paths(
        [PIIOverride(column_name="Document_Name", scope="DB.RAW_*")]
    )
    assert "db.raw_dev.t1.document_name" in paths


def test_config_without_overrides_stays_clean(tmp_path: Path):
    # exclude_unset keeps the committed file a record of explicit choices; an
    # untouched pii_overrides list must not appear in it.
    save_config(DexConfig(connector="duckdb"), tmp_path)
    text = (tmp_path / ".dex" / "config.yml").read_text()
    assert "pii_overrides" not in text
    assert load_config(tmp_path).pii_overrides == []


def test_blob_overrides_round_trip(tmp_path: Path):
    config = DexConfig(
        blob_overrides=[
            BlobOverride(
                column="MY_DB.PUBLIC.SESSIONS.PAYLOAD",
                reason="small serialized state; worth profiling",
            ),
            BlobOverride(column="my_db.public.events.raw_blob"),
        ]
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert [e.column for e in loaded.blob_overrides] == [
        "MY_DB.PUBLIC.SESSIONS.PAYLOAD",
        "my_db.public.events.raw_blob",
    ]
    assert loaded.blob_overrides[0].reason == "small serialized state; worth profiling"
    assert loaded.blob_overrides[1].reason is None


def test_blob_override_paths_are_case_insensitive():
    paths = blob_override_paths(
        [BlobOverride(column="  MY_DB.PUBLIC.SESSIONS.PAYLOAD ")]
    )
    assert paths == {"my_db.public.sessions.payload"}


def test_config_without_blob_overrides_stays_clean(tmp_path: Path):
    save_config(DexConfig(connector="duckdb"), tmp_path)
    text = (tmp_path / ".dex" / "config.yml").read_text()
    assert "blob_overrides" not in text
    assert load_config(tmp_path).blob_overrides == []


def test_the_cache_block_round_trips_and_defaults_to_the_filesystem(tmp_path: Path):
    # The default is what every existing repo depends on: a config that says
    # nothing about the cache keeps writing loose JSON under `.dex/`.
    assert DexConfig().cache.backend == "filesystem"
    assert DexConfig().cache.options == {}

    config = DexConfig(
        cache=CacheConfig(
            backend="mypkg.stores:my_store",
            options={"tenant": "acme", "project": "shop"},
        )
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.cache.backend == "mypkg.stores:my_store"
    # Options reach the backend verbatim, so they have to survive the YAML round
    # trip untouched rather than being coerced into a model dex defines.
    assert loaded.cache.options == {"tenant": "acme", "project": "shop"}


def test_an_unset_cache_block_is_not_written_into_the_committed_file(tmp_path: Path):
    # The committed file stays a record of explicit choices, so a repo that never
    # chose a backend does not grow a line claiming it chose the default.
    save_config(DexConfig(connector="duckdb"), tmp_path)
    raw = (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    assert "cache" not in raw


def test_the_semantic_block_derives_the_deployment_from_the_released_spelling(
    tmp_path: Path,
):
    # `backend` collapsed vendor, deployment and who executes into one enum. It
    # keeps working, and it now reads as one spelling of the deployment axis, so
    # a repo that committed it before the split needs no edit.
    assert DexConfig().semantic.vendor == "dbt"
    assert DexConfig().semantic.deployment == "local"

    for spelling in ("dbt_cloud", "api", "cloud"):
        assert SemanticConfig(backend=spelling).deployment == "dbt_cloud"

    config = DexConfig(
        semantic=SemanticConfig(
            backend="dbt_cloud", host="sl.example.com", environment_id="7"
        )
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.semantic.deployment == "dbt_cloud"
    assert loaded.semantic.host == "sl.example.com"


def test_the_semantic_deployment_axis_round_trips_on_its_own(tmp_path: Path):
    config = DexConfig(semantic=SemanticConfig(vendor="dbt", deployment="dbt_cloud"))
    # Either spelling answers correctly, so a reader of one is never told the
    # other's stale value.
    assert config.semantic.backend == "dbt_cloud"
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded is not None
    assert loaded.semantic.deployment == "dbt_cloud"


def test_a_contradictory_semantic_block_is_refused_rather_than_resolved():
    # Two spellings of one choice. Picking a winner would leave the other
    # accepted and ignored, which reads as a setting that took effect.
    with pytest.raises(ValueError, match="different deployments"):
        SemanticConfig(backend="local", deployment="dbt_cloud")
    # Agreeing is fine; only a contradiction is refused.
    assert SemanticConfig(backend="dbt_cloud", deployment="dbt_cloud").deployment == (
        "dbt_cloud"
    )


def test_a_semantic_vendor_or_deployment_dex_does_not_ship_is_refused():
    with pytest.raises(ValueError, match=r"semantic\.vendor"):
        SemanticConfig(vendor="cube")
    with pytest.raises(ValueError, match=r"semantic\.deployment"):
        SemanticConfig(deployment="dbt_core")
    with pytest.raises(ValueError, match=r"semantic\.backend"):
        SemanticConfig(backend="dbt_core")


def test_an_unset_semantic_block_is_not_written_into_the_committed_file(
    tmp_path: Path,
):
    # The derived deployment is derived, not chosen, so it must not appear in the
    # committed file as though the author had picked it.
    save_config(DexConfig(connector="duckdb"), tmp_path)
    raw = (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    assert "semantic" not in raw

    save_config(DexConfig(semantic=SemanticConfig(deployment="dbt_cloud")), tmp_path)
    raw = (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    assert "deployment: dbt_cloud" in raw
    assert "vendor" not in raw


def test_the_conventions_block_round_trips_and_defaults_to_on(tmp_path: Path):
    # House-convention warnings are the only checks a project can decline, so
    # the default has to be on: a repo with no convention hears nothing from
    # them anyway, and one that has a convention should not have to opt in to
    # having it read.
    assert DexConfig().conventions.resolved_keys is True

    save_config(
        DexConfig(conventions=ConventionWarnings(resolved_keys=False)), tmp_path
    )
    raw = (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    assert "resolved_keys: false" in raw
    assert load_config(tmp_path).conventions.resolved_keys is False


def test_an_unset_conventions_block_is_not_written_into_the_committed_file(
    tmp_path: Path,
):
    # Same rule the other blocks follow: the committed file records the choices
    # an author made, and leaving a check on is not one of them.
    save_config(DexConfig(connector="duckdb"), tmp_path)
    raw = (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    assert "conventions" not in raw


# --- reading the file: refusals by line, and what still loads ----------------------


def _write(root: Path, text: str) -> None:
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(text, encoding="utf-8")


def test_a_value_the_schema_refuses_is_named_by_setting_and_line(tmp_path: Path):
    _write(tmp_path, "connector: duckdb\nquery:\n  max_rows: lots\n")
    with pytest.raises(InvalidConfigError) as raised:
        load_config(tmp_path)
    message = str(raised.value)
    assert "line 3: query.max_rows" in message
    assert "dex config validate" in message


def test_a_yaml_syntax_error_is_a_configuration_error(tmp_path: Path):
    # It used to escape as a bare YAMLError, which the envelope could only call
    # `internal`, as though dex had failed rather than the file.
    _write(tmp_path, "connector: duckdb\nbudget: [\n")
    with pytest.raises(ConfigurationError, match="line 3"):
        load_config(tmp_path)


def test_unknown_and_repeated_keys_load_and_are_carried_as_warnings(tmp_path: Path):
    _write(tmp_path, "connector: duckdb\nconnector: duckdb\nbudget:\n  celing: 5\n")
    config = load_config(tmp_path)
    assert config.connector == "duckdb"
    assert config.load_warnings == [
        ".dex/config.yml line 2: connector is set twice; the value on line 2 wins "
        "and the one on line 1 is ignored",
        ".dex/config.yml line 4: budget.celing is not a setting dex reads, so it is "
        "ignored; did you mean budget.ceiling?",
    ]


def test_a_config_built_in_code_has_nothing_to_warn_about():
    assert DexConfig().load_warnings == []


_CORPUS = {
    "clean": "connector: duckdb\nduckdb:\n  path: x.duckdb\n",
    "empty": "",
    "comments-only": "# nothing yet\n",
    "unknown-key": "budget:\n  celing: 5\n",
    "repeated-key": "connector: a\nconnector: b\n",
    "credential-like-option": "cache:\n  options:\n    api_token: x\n",
    "bad-type": "query:\n  max_rows: lots\n",
    "bad-model-rule": "semantic:\n  vendor: nope\n",
    "bad-list-item": "pii_overrides:\n  - reason: no column at all\n",
    "bad-pairing": "clickhouse:\n  compute_unit_price_usd: 1.5\n",
    "syntax": "budget: [\n",
    "not-a-mapping": "- a\n",
}


@pytest.mark.parametrize("text", _CORPUS.values(), ids=_CORPUS.keys())
def test_load_refuses_exactly_what_validate_calls_fatal(tmp_path: Path, text: str):
    """One reading of the file, so the verb that checks it and the loader every
    command runs can never disagree about whether it is usable."""

    _write(tmp_path, text)
    fatal = any(problem.fatal for problem in ConfigDocument(text).problems())
    if fatal:
        with pytest.raises(ConfigurationError):
            load_config(tmp_path)
    else:
        assert load_config(tmp_path) is not None


@pytest.mark.parametrize(
    "text",
    [
        "connector: bigquery\nbigquery:\n  project: p\n  datasets: [raw]\n",
        "semantic:\n  backend: api\n  host: h.example\n  environment_id: '7'\n",
        "budget:\n  ceiling: 5\n  session_ceiling_declined: true\ncluster:\n"
        "  sample_seed: null\n",
        "pii_overrides:\n  - column_name: email\n    scope: '*_prod.*'\n",
    ],
)
def test_a_file_that_loaded_before_loads_to_the_same_settings(
    tmp_path: Path, text: str
):
    _write(tmp_path, text)
    expected = DexConfig.model_validate(yaml.safe_load(text) or {})
    assert load_config(tmp_path).model_dump() == expected.model_dump()
