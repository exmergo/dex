{#- unpivot_json_object v1, scaffolded by dex.

    Unpivot a JSON object column with dynamic keys into one row per top-level
    key. Nested objects never surface their own keys as top-level rows; the
    value of a key is the whole nested value, kept in the warehouse's native
    semi-structured type (BigQuery JSON, Snowflake VARIANT, Databricks VARIANT,
    Postgres jsonb, Redshift SUPER, DuckDB JSON). The key is a plain string.

    Renders a complete SELECT:

        select key as related_id, value as attrs
        from (
            {{ unpivot_json_object(
                 relation=source('app', 'entities'),
                 json_column='attributes',
                 passthrough=['id']) }}
        )

    json_column may be a bare column name (qualified onto the relation
    automatically) or any expression over it. For a string-typed source pass
    the adapter's parse expression: parse_json(payload) on BigQuery, Snowflake,
    and Databricks, json_parse(payload) on Redshift; Postgres and DuckDB accept
    JSON-bearing text directly. A NULL object produces no rows. On Postgres a
    non-object value errors loudly (jsonb_each is strict); the other adapters
    yield no rows for it. Databricks needs VARIANT support (DBR 15.3+ or a
    current SQL warehouse).

    Edit freely: this file is yours. Re-running
    `dex transform macro unpivot_json_object` proposes a diff back to the
    shipped version.
-#}

{% macro unpivot_json_object(relation, json_column, key_alias='key', value_alias='value', passthrough=[]) %}
    {{ return(adapter.dispatch('unpivot_json_object')(relation, json_column, key_alias, value_alias, passthrough)) }}
{% endmacro %}


{% macro default__unpivot_json_object(relation, json_column, key_alias, value_alias, passthrough) %}
    {{ exceptions.raise_compiler_error('unpivot_json_object has no implementation for adapter ' ~ adapter.type()) }}
{% endmacro %}


{% macro bigquery__unpivot_json_object(relation, json_column, key_alias, value_alias, passthrough) %}
{%- set expr = 'base.' ~ json_column if modules.re.match('^[A-Za-z_][A-Za-z0-9_]*$', json_column) else json_column %}
{#- A JSON path argument must be a compile-time literal, so the value is read
    with the subscript operator (which accepts a computed key), never through a
    built path string. json_keys recurses into nested objects by default,
    silently surfacing nested field names as if they were top-level; the
    explicit depth limit of 1 pins the rows to real top-level keys. -#}
select
    {% for col in passthrough %}base.{{ col }},
    {% endfor -%}
    _dex_key as {{ key_alias }},
    {{ expr }}[_dex_key] as {{ value_alias }}
from {{ relation }} as base
cross join unnest(json_keys({{ expr }}, 1)) as _dex_key
{% endmacro %}


{% macro snowflake__unpivot_json_object(relation, json_column, key_alias, value_alias, passthrough) %}
{%- set expr = 'base.' ~ json_column if modules.re.match('^[A-Za-z_][A-Za-z0-9_]*$', json_column) else json_column %}
{#- flatten is non-recursive by default and emits key/value natively. -#}
select
    {% for col in passthrough %}base.{{ col }},
    {% endfor -%}
    _dex_f.key as {{ key_alias }},
    _dex_f.value as {{ value_alias }}
from {{ relation }} as base,
lateral flatten(input => {{ expr }}) as _dex_f
{% endmacro %}


{% macro databricks__unpivot_json_object(relation, json_column, key_alias, value_alias, passthrough) %}
{%- set expr = 'base.' ~ json_column if modules.re.match('^[A-Za-z_][A-Za-z0-9_]*$', json_column) else json_column %}
{#- variant_explode is top-level-only and keeps values as VARIANT. It needs
    DBR 15.3+ or a current SQL warehouse; a JSON string column reaches it via
    parse_json(col). -#}
select
    {% for col in passthrough %}base.{{ col }},
    {% endfor -%}
    _dex_f.key as {{ key_alias }},
    _dex_f.value as {{ value_alias }}
from {{ relation }} as base,
lateral variant_explode({{ expr }}) as _dex_f
{% endmacro %}


{% macro postgres__unpivot_json_object(relation, json_column, key_alias, value_alias, passthrough) %}
{%- set expr = 'base.' ~ json_column if modules.re.match('^[A-Za-z_][A-Za-z0-9_]*$', json_column) else json_column %}
{#- The cast is a no-op on jsonb and converts json and JSON-bearing text, so
    string sources need nothing extra here. jsonb_each errors on a non-object
    value: loud beats silently dropping malformed rows. -#}
select
    {% for col in passthrough %}base.{{ col }},
    {% endfor -%}
    _dex_kv.key as {{ key_alias }},
    _dex_kv.value as {{ value_alias }}
from {{ relation }} as base
cross join lateral jsonb_each(({{ expr }})::jsonb) as _dex_kv(key, value)
{% endmacro %}


{% macro redshift__unpivot_json_object(relation, json_column, key_alias, value_alias, passthrough) %}
{%- set expr = 'base.' ~ json_column if modules.re.match('^[A-Za-z_][A-Za-z0-9_]*$', json_column) else json_column %}
{#- PartiQL object UNPIVOT over SUPER; the AT key arrives as VARCHAR. Key case
    behavior follows enable_case_sensitive_super_attribute. A VARCHAR source
    reaches it via json_parse(col). -#}
select
    {% for col in passthrough %}base.{{ col }},
    {% endfor -%}
    _dex_key as {{ key_alias }},
    _dex_value as {{ value_alias }}
from {{ relation }} as base,
unpivot {{ expr }} as _dex_value at _dex_key
{% endmacro %}


{% macro duckdb__unpivot_json_object(relation, json_column, key_alias, value_alias, passthrough) %}
{%- set expr = 'base.' ~ json_column if modules.re.match('^[A-Za-z_][A-Za-z0-9_]*$', json_column) else json_column %}
{#- json_keys is top-level-only and -> accepts a computed key, mirroring the
    BigQuery shape with DuckDB's operators. -#}
select
    {% for col in passthrough %}base.{{ col }},
    {% endfor -%}
    _dex_key as {{ key_alias }},
    {{ expr }} -> _dex_key as {{ value_alias }}
from {{ relation }} as base
cross join unnest(json_keys({{ expr }})) as _dex_u(_dex_key)
{% endmacro %}
