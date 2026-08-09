"""maintain snapshot: what the baseline pins, its fallbacks, and its hygiene."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.maintain.commands import _layer_notes
from exmergo_dex_core.maintain.snapshot import TransformLayer
from exmergo_dex_core.storage import FilesystemStore


def test_snapshot_pins_cache_and_fingerprints_layers(maintain_repo):
    payload = maintain_repo.snapshot()
    data = payload["data"]

    assert data["baseline"]["from"] == "cache"
    assert data["baseline"]["dataset_count"] == 3
    assert data["baseline"]["relationship_count"] >= 1
    assert data["baseline"]["grain_baseline_count"] >= 2
    assert data["transform_layer"]["model_count"] == 1
    assert data["transform_layer"]["source_count"] == 2
    assert data["semantic_layer"]["semantic_model_count"] == 1
    assert data["semantic_layer"]["metric_count"] == 2
    assert "commit .dex/snapshot.json" in data["hint"]

    snap = FilesystemStore(maintain_repo.root).load_snapshot()
    identifiers = {d.identifier for d in snap.warehouse.datasets}
    assert identifiers == {
        "warehouse.main.customers",
        "warehouse.main.orders",
        "warehouse.main.stg_orders",
    }

    orders = next(
        d for d in snap.warehouse.datasets if d.identifier.endswith(".orders")
    )
    assert ["order_id"] in orders.candidate_keys
    verified = [r for r in snap.warehouse.relationships if r.verified]
    assert verified and all(r.orphan_fraction == 0.0 for r in verified)

    sources = {s.table for s in snap.transform_layer.sources}
    assert sources == {"customers", "orders"}
    assert "stg_orders" in snap.transform_layer.models
    assert "models/staging/stg_orders.sql" in snap.transform_layer.files

    model = snap.semantic_layer.semantic_models[0]
    assert model.model_ref == "stg_orders"
    assert model.categorical_dimensions == {"status": "status"}
    assert model.entities == {"order_id": "order_id"}
    assert model.measures == {"order_amount": "amount", "order_count": "order_id"}
    assert snap.transform_layer.model_sources == {"stg_orders": ["main.orders"]}
    revenue = next(m for m in snap.semantic_layer.metrics if m.name == "revenue")
    assert revenue.input_measures == ["order_amount"]


def test_layer_notes_are_collected_from_every_side_and_deduplicated():
    """Both layers a command read, baseline and current, with repeats folded.

    Both sides are collected because the two provenance payloads read different
    ones: `dangling_source` compares against the baseline's declared sources
    while `definition_changed` reads the current project. In practice the same
    format produces both and says the same thing, so the repeat has to collapse
    or every warning list carries it twice.
    """

    shared = "sources are declared in configuration"
    baseline = TransformLayer(notes=[shared])
    current = TransformLayer(notes=[shared, "no file hashes: there are no files"])

    assert _layer_notes(baseline, current, None) == [
        shared,
        "no file hashes: there are no files",
    ]


def test_a_baseline_layer_note_surfaces_on_every_detection_run(maintain_repo):
    """A note pinned into the baseline is reported by `check`, not only at pin time.

    The baseline is what bounds what the schema axis can compare, and a host
    re-runs detection far more often than it re-pins. A note that surfaced only
    when the snapshot was taken would explain the limits of a run nobody was
    reading and stay silent on the ones they were.
    """

    maintain_repo.snapshot()
    store = FilesystemStore(maintain_repo.root)
    snap = store.load_snapshot()
    snap.transform_layer.notes = ["the transform layer is reduced from an asset graph"]
    snap.semantic_layer.notes = ["metrics are Python objects, so none names a file"]
    store.save_snapshot(snap)

    rc, payload = maintain_repo.dex("maintain", "check")
    assert rc == 0 and payload["status"] == "ok", payload
    assert "the transform layer is reduced from an asset graph" in payload["warnings"]
    assert "metrics are Python objects, so none names a file" in payload["warnings"]


def test_snapshot_without_cache_is_metadata_only(dex, tmp_path: Path):
    duckdb = pytest.importorskip("duckdb")
    root = tmp_path / "bare"
    root.mkdir()
    db_path = root / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE items (id INTEGER, label VARCHAR)")
    conn.execute("INSERT INTO items VALUES (1, 'a'), (2, 'b')")
    conn.close()
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db_path}\n", encoding="utf-8"
    )

    rc, payload = dex("--repo-root", str(root), "maintain", "snapshot")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["baseline"]["from"] == "metadata"
    assert payload["data"]["transform_layer"] is None
    warnings = " ".join(payload["warnings"])
    assert "metadata-only" in warnings
    assert "no dbt project" in warnings

    snap = FilesystemStore(root).load_snapshot()
    assert snap.warehouse_from == "metadata"
    items = snap.warehouse.datasets[0]
    assert items.row_count == 2
    assert {c.name for c in items.columns} == {"id", "label"}
    # Metadata-only means no uniqueness verdicts: nothing for grain to lean on.
    assert items.candidate_keys == []


def test_snapshot_recaptures_on_connector_mismatch(maintain_repo):
    store = FilesystemStore(maintain_repo.root)
    cache = store.load_cache()
    cache.provenance.connector = "bigquery"
    store.save_cache(cache)

    payload = maintain_repo.snapshot()
    assert payload["data"]["baseline"]["from"] == "metadata"
    assert any("bigquery" in w for w in payload["warnings"])


def test_snapshot_file_carries_no_string_values(maintain_repo):
    maintain_repo.snapshot()
    raw = (maintain_repo.root / ".dex" / "snapshot.json").read_text(encoding="utf-8")
    # No customer value may land in the baseline: string min/max are suppressed
    # at profile time and the snapshot pins profiles, never rows.
    assert "example.com" not in raw
    assert "name_" not in raw

    snap = json.loads(raw)
    for dataset in snap["warehouse"]["datasets"]:
        for column in dataset["columns"]:
            if "VARCHAR" in column["data_type"].upper():
                assert column["min_value"] is None
                assert column["max_value"] is None


def test_the_snapshot_hint_only_promises_git_when_the_baseline_is_a_file(
    tmp_path: Path,
):
    """The hint is advice about where the baseline landed, so it has to follow the
    backend.

    Telling a host whose snapshot is a row or a document to commit
    `.dex/snapshot.json` names a file it does not have. The half that holds
    everywhere, re-pinning after a known-good build, is what a backend dex does
    not ship should get; `snapshot_path` carries the real location either way.
    The filesystem half is asserted by the first test in this file.
    """

    import argparse

    from exmergo_dex_core import DexConfig, DexEngine, MemoryStore
    from exmergo_dex_core.maintain.commands import cmd_snapshot

    duckdb = pytest.importorskip("duckdb")
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE items (id INTEGER)")
    conn.close()

    with DexEngine(
        connector="duckdb",
        path=str(db_path),
        config=DexConfig(connector="duckdb"),
        store=MemoryStore(),
    ) as engine:
        envelope = cmd_snapshot(argparse.Namespace(), engine)

    hint = envelope.data["hint"]
    assert "commit" not in hint
    assert ".dex/snapshot.json" not in hint
    assert "re-run `maintain snapshot`" in hint
    # And the location is still reported, just not as a path anyone can commit.
    assert envelope.data["snapshot_path"] == "memory:snapshot"


# --- a present baseline is not a valid one ---------------------------------------
#
# `snapshot` pinned the cache on a presence test (`cache is not None and
# bool(cache.datasets)`) where a validity test was needed. Three ways a present
# cache can be invalid: too thin (partially profiled), too old, and full of
# ghosts (fixed separately). The first two are pinned here, at the moment the
# baseline is written, because `maintain snapshot` is the command a host wires to
# "accept current state" and both make that accept silently partial.


def test_a_thin_cache_is_pinned_but_says_so(maintain_repo):
    """Past the rank cutoff most of a cache has no column detail, and pinning it
    used to report a healthy object count and nothing else."""

    maintain_repo.dex("explore", "map")
    stripped = maintain_repo.strip_column_detail(".customers", ".stg_orders")

    payload = maintain_repo.snapshot()
    baseline = payload["data"]["baseline"]
    # Still pinned: a thin baseline is better than none, and refusing here would
    # break the ordinary large-warehouse flow.
    assert baseline["dataset_count"] == 3
    # The coverage is now visible structurally, not only in prose, so a host
    # automating the accept can gate on it.
    assert baseline["column_detail_count"] == 1
    thin = [w for w in payload["warnings"] if "without column detail" in w]
    assert len(thin) == 1
    assert "explore map --full" in thin[0]
    for identifier in stripped:
        assert identifier in thin[0]


def test_a_fully_profiled_cache_pins_without_a_coverage_warning(maintain_repo):
    payload = maintain_repo.snapshot()
    assert payload["data"]["baseline"]["column_detail_count"] == 3
    assert not [w for w in payload["warnings"] if "without column detail" in w]


def test_pinning_an_aging_cache_says_the_accept_is_partial(maintain_repo):
    """The reported case: a baseline written at 16:21Z from an 02:00Z cache.

    Anything created since the capture is absent from the baseline and will
    report as drift, which is the opposite of what the operator just asked for.
    """

    maintain_repo.backdate_cache("2020-01-01T02:00:00+00:00")
    payload = maintain_repo.snapshot()
    aged = [w for w in payload["warnings"] if "captured" in w and "freshness" in w]
    assert len(aged) == 1
    assert "2020-01-01T02:00:00+00:00" in aged[0]
    assert "explore map" in aged[0]


def test_a_metadata_baseline_makes_no_cache_age_claim(dex, tmp_path: Path):
    """No cache was pinned, so there is no capture time to be stale."""

    root = tmp_path / "bare"
    (root / ".dex").mkdir(parents=True)
    db = root / "warehouse.duckdb"
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.close()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db}\n", encoding="utf-8"
    )

    _rc, payload = dex("--repo-root", str(root), "maintain", "snapshot")
    assert payload["data"]["baseline"]["from"] == "metadata"
    # A metadata capture reads live columns, so it is thin in grain terms but not
    # in column terms, and neither the coverage nor the age warning applies.
    assert payload["data"]["baseline"]["column_detail_count"] == 1
    assert not [w for w in payload["warnings"] if "without column detail" in w]
    assert not [w for w in payload["warnings"] if "freshness" in w]


# --- schema_version: written since v1, read by nothing until now (#243) ------


def _rewrite_baseline(root: Path, **changes) -> None:
    """Edit the stored baseline in place, the way a bad migration or a hand-edit
    would. Goes through the file rather than the model so the document can hold
    a shape the model would refuse to construct."""

    path = root / ".dex" / "snapshot.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.update(changes)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_a_baseline_from_an_unknown_schema_version_is_refused(maintain_repo):
    """The gap this closes: the field was stamped on every write and read by
    nothing, so a document from a schema this engine does not understand was
    handed to the detectors as though it were current. That failure is the bad
    kind, because it is not an error: it is a drift report measured against a
    shape the engine misread."""

    maintain_repo.snapshot()
    _rewrite_baseline(maintain_repo.root, schema_version=99)

    code, payload = maintain_repo.dex("maintain", "check")

    assert code != 0
    assert payload["reason"] == "prerequisite", (
        "an unreadable baseline is a prerequisite failure: the call is well formed "
        "and a named command fixes it"
    )
    message = " ".join(payload["errors"])
    assert "99" in message and "maintain snapshot" in message


def test_a_corrupt_baseline_is_a_prerequisite_not_a_bad_request(maintain_repo):
    """`ValidationError` subclasses `ValueError`, so a corrupt baseline reached
    the CLI catch-all and was classified as a REQUEST error. That tells an
    operator they typed something wrong when the fix is `maintain snapshot`, and
    it is the retry-versus-stop distinction `PrerequisiteError` exists to carry."""

    maintain_repo.snapshot()
    _rewrite_baseline(maintain_repo.root, warehouse="not an object")

    code, payload = maintain_repo.dex("maintain", "check")

    assert code != 0
    assert payload["reason"] == "prerequisite", (
        "a corrupt baseline must not report as a bad request"
    )
    assert "maintain snapshot" in " ".join(payload["errors"])


def test_an_absent_baseline_still_reads_as_absent(maintain_repo):
    """The arm that keeps the two states apart. Both remediate the same way, so
    a single error would have been defensible and would have thrown away the
    distinction between "you never took a baseline" and "yours is unreadable" —
    and only the second suggests something went wrong."""

    (maintain_repo.root / ".dex" / "snapshot.json").unlink(missing_ok=True)

    code, payload = maintain_repo.dex("maintain", "check")

    assert code != 0
    assert payload["reason"] == "prerequisite"
    message = " ".join(payload["errors"])
    assert "no drift baseline yet" in message
    assert "could not be read" not in message


def test_a_current_baseline_is_not_refused(maintain_repo):
    """The quiet arm. Without it, the three assertions above are equally
    consistent with every baseline being refused."""

    maintain_repo.snapshot()

    code, payload = maintain_repo.dex("maintain", "check")

    assert code == 0, payload.get("errors")
    assert payload["reason"] is None
