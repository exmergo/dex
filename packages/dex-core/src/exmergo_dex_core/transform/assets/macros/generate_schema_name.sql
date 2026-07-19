{#- generate_schema_name v1, scaffolded by dex.

    Route each model layer to its own schema (BigQuery: dataset), suffixed
    with the target name, so a dev build lands in staging_dev /
    intermediate_dev / marts_dev instead of piling every model into one
    schema. dbt's built-in default composes the other way around
    (<target.schema>_<custom>); this override puts the layer first and the
    target last, and falls back to target.schema when a model sets no custom
    schema.

    Pairs with per-folder config in dbt_project.yml:

        models:
          my_project:
            staging:
              +schema: staging

    Edit freely: this file is yours. Re-running
    `dex transform macro generate_schema_name` proposes a diff back to the
    shipped version.
-#}

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}_{{ target.name }}
    {%- endif -%}
{%- endmacro %}
