"""What counts as a node in the transform layer, and what an existing baseline
does the first time it is checked after the project view widened.

Loading snapshots and seeds is what makes them authorable, and it also moves
what `maintain` fingerprints: both derivations that read "the things this
project builds" used to take every `.sql` in the view by filename, which turned
macros into models and would now turn snapshots into them too. Scoping those to
the families that actually produce dbt nodes is the fix; this is where the
consequence for a baseline pinned before it is pinned down.
"""

from __future__ import annotations

import json
from pathlib import Path

from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.maintain.snapshot import transform_layer

SNAPSHOT = """{% snapshot snap_orders %}
{{ config(unique_key='order_id', strategy='timestamp', updated_at='ordered_at') }}
select * from {{ ref('stg_orders') }}
{% endsnapshot %}
"""

MACRO = "{% macro cents_to_dollars(column) %}({{ column }} / 100){% endmacro %}\n"

SEED = "status_code,label\nP,placed\nS,shipped\n"

SINGULAR_TEST = "select order_id from {{ ref('stg_orders') }} where order_id is null\n"

ANALYSIS = "select count(*) as n from {{ ref('stg_orders') }}\n"


def _populate(root: Path) -> None:
    (root / "snapshots").mkdir(exist_ok=True)
    (root / "snapshots" / "snap_orders.sql").write_text(SNAPSHOT, encoding="utf-8")
    (root / "macros").mkdir(exist_ok=True)
    (root / "macros" / "cents_to_dollars.sql").write_text(MACRO, encoding="utf-8")
    (root / "seeds").mkdir(exist_ok=True)
    (root / "seeds" / "status_labels.csv").write_text(SEED, encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "assert_order_id_present.sql").write_text(
        SINGULAR_TEST, encoding="utf-8"
    )
    (root / "analyses").mkdir(exist_ok=True)
    (root / "analyses" / "order_volume.sql").write_text(ANALYSIS, encoding="utf-8")


def test_a_node_is_a_model_a_snapshot_or_a_seed_and_nothing_else(maintain_repo):
    _populate(maintain_repo.root)
    layer = transform_layer(load_project(maintain_repo.root))

    # A snapshot and a seed each build a relation dbt names after the file and
    # each is ref()-able, so both are nodes. A macro builds nothing and never
    # was one, though it was counted as one before the families were scoped.
    assert set(layer.models) == {"stg_orders", "snap_orders", "status_labels"}
    assert "cents_to_dollars" not in layer.models

    # A singular test and an analysis are the case the scoping exists for. dbt
    # calls a singular test a node, and it is still not one here: it builds no
    # relation and nothing can ref() it, so counting it as a model would put a
    # name into the drift baseline that no warehouse table will ever back.
    assert "assert_order_id_present" not in layer.models
    assert "order_volume" not in layer.models

    # The file fingerprint stays the whole editable surface: it is a change
    # fingerprint over what a human can edit, not a node list.
    assert "macros/cents_to_dollars.sql" in layer.files
    assert "seeds/status_labels.csv" in layer.files
    assert "tests/assert_order_id_present.sql" in layer.files
    assert "analyses/order_volume.sql" in layer.files

    # And a test's ref() is not recorded as a model's dependency either, since
    # there is no model there to depend on anything.
    assert "assert_order_id_present" not in layer.model_refs

    # And a snapshot's ref() is recorded like a model's, which is what carries a
    # warehouse finding through to the snapshot standing on it.
    assert layer.model_refs["snap_orders"] == ["stg_orders"]


def test_a_baseline_pinned_before_the_widening_reports_no_phantom_drift(
    maintain_repo,
):
    """The risk this change carries, checked rather than assumed.

    A baseline pinned before the new families were loaded holds a smaller file
    set and a models list that counted macros. Adding those files must read as
    "nothing drifted", because no detector diffs the file set or the model list:
    they feed `orphan_relation` and the semantic dangling-reference check, both
    of which ask whether a *warehouse* table is still backed by the project.

    Re-run against every family as they are added, since the widening is what
    carries the risk and each new one widens it again.
    """

    maintain_repo.snapshot()

    # A pre-change baseline also counted macro filenames as models. Injected
    # rather than described, so the assertion is about the real shape.
    baseline_path = maintain_repo.root / ".dex" / "snapshot.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["transform_layer"]["models"].append("cents_to_dollars")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    _populate(maintain_repo.root)

    rc, payload = maintain_repo.dex("maintain", "check")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0, payload["data"]["findings"]
