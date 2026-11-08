{{ config(enabled=false) }}
{# Retired with the old fee schedule; kept for reference, never built. #}
select
    merchant_id
    {% if var('enable_platform_fees') %}, sum(platform_fee) as lifetime_fees{% endif %}
from {{ ref('fct_orders') }}
group by merchant_id
