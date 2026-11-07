{# The fee and the net amount, or nothing when platform fees are off. #}
{% macro fee_columns() %}
    {%- if var('enable_platform_fees') -%}
    , platform_fee
    , gross_amount - platform_fee as net_amount
    {%- endif -%}
{% endmacro %}
