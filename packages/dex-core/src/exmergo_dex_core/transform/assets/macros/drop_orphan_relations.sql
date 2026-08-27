{#- drop_orphan_relations v1, scaffolded by dex.

    Drop warehouse relations that no longer have a backing dbt model or
    source: the residue a rename or removal leaves behind, since dbt only
    ever creates and never drops the object a renamed or removed model used
    to build. `maintain check` names these as orphan_relation findings and
    `maintain reconcile` proposes running this macro with the specific
    identifiers it found; nothing here is inferred, the caller always names
    exactly what to drop.

    Dry-run by default. Nothing is dropped until you pass dry_run=false:

        dbt run-operation drop_orphan_relations \
            --args '{relations: ["analytics.marts.old_fct_orders"], dry_run: false}'

    Each entry in `relations` is a dot-separated `schema.identifier` or
    `database.schema.identifier` (a bare `schema.identifier` resolves against
    the current target's database). Any entry that still names a live model,
    seed, or snapshot in the current manifest refuses the whole run before
    anything is dropped: a typo in the list must never delete something real.
    A named relation that does not exist in the warehouse is skipped, not an
    error, so the same list is safe to re-run per target.

    Edit freely: this file is yours. Re-running
    `dex transform macro drop_orphan_relations` proposes a diff back to the
    shipped version.
-#}

{% macro drop_orphan_relations(relations=[], dry_run=true) %}
    {%- if relations | length == 0 -%}
        {{ log("drop_orphan_relations: no relations given; nothing to do. Pass --args '{relations: [...]}'", info=True) }}
        {% do return(none) %}
    {%- endif -%}

    {%- set built = [] -%}
    {%- for node in graph.nodes.values() if node.resource_type in ('model', 'seed', 'snapshot') -%}
        {%- do built.append((node.database, node.schema, node.alias) | map('lower') | join('.')) -%}
    {%- endfor -%}

    {%- set live = [] -%}
    {%- for entry in relations -%}
        {%- set parts = entry.split('.') -%}
        {%- if parts | length == 3 -%}
            {%- set key = parts | map('lower') | join('.') -%}
        {%- elif parts | length == 2 -%}
            {%- set key = ([target.database] + parts) | map('lower') | join('.') -%}
        {%- else -%}
            {{ exceptions.raise_compiler_error(
                "drop_orphan_relations: '" ~ entry ~
                "' is not schema.identifier or database.schema.identifier"
            ) }}
        {%- endif -%}
        {%- if key in built -%}
            {%- do live.append(entry) -%}
        {%- endif -%}
    {%- endfor -%}
    {%- if live | length > 0 -%}
        {{ exceptions.raise_compiler_error(
            "drop_orphan_relations refuses to run: " ~ (live | join(', ')) ~
            " still names a live model, seed, or snapshot in this manifest. "
            ~ "Nothing was dropped; fix the list and re-run"
        ) }}
    {%- endif -%}

    {%- set dropped = [] -%}
    {%- set would_drop = [] -%}
    {%- set skipped = [] -%}
    {%- for entry in relations -%}
        {%- set parts = entry.split('.') -%}
        {%- if parts | length == 3 -%}
            {%- set relation = adapter.get_relation(database=parts[0], schema=parts[1], identifier=parts[2]) -%}
        {%- else -%}
            {%- set relation = adapter.get_relation(database=target.database, schema=parts[0], identifier=parts[1]) -%}
        {%- endif -%}
        {%- if relation is none -%}
            {%- do skipped.append(entry) -%}
            {{ log("skip (not found): " ~ entry, info=True) }}
        {%- elif dry_run -%}
            {%- do would_drop.append(entry) -%}
            {{ log("would drop: " ~ relation, info=True) }}
        {%- else -%}
            {%- do adapter.drop_relation(relation) -%}
            {%- do dropped.append(entry) -%}
            {{ log("dropped: " ~ relation, info=True) }}
        {%- endif -%}
    {%- endfor -%}

    {{ log(
        "drop_orphan_relations: " ~ (dropped | length) ~ " dropped, "
        ~ (would_drop | length) ~ " would drop (dry_run), "
        ~ (skipped | length) ~ " skipped (not found)",
        info=True
    ) }}
{% endmacro %}
